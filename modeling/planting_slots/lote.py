"""National batch: the `osm_calcada` sidewalk slots (10 m spacing) of every
municipality, urban areas only, with IBGE attributes and tree-canopy cover.

Per municipality (`process_municipality`): OSM roads/obstacles of the region's
extract clipped to the IBGE boundary -> sidewalk.generate_osm_calcada (the very
method of scripts/03_allocate_slots.py, on raw OSM geometry) -> keep the slots
inside urban census tracts -> tract/district/neighbourhood by containment
-> canopy -> one GeoParquet. Each municipality is independent, atomic and
resumable (status/<cd_mun>.json), and one failure never stops the batch.
"""

from __future__ import annotations

import json
import subprocess
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from multiprocessing import get_context
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from planting_slots.brasil import Layout, load_municipalities, region_of, uf_of, utm_epsg
from planting_slots.copa_brasil import DEFAULT_SOURCES, canopy_for_points
from planting_slots.criteria import (
    CLEARANCE_M, GENERIC_SPACING_M, MIN_SIDEWALK_WIDTH_BY_PORTE_M, SPACING_BY_PORTE_M,
)
from planting_slots.downloads import write_atomic
from planting_slots.ibge_brasil import load_faces, load_tracts
from planting_slots.osm_pbf import load_obstacles, load_roads, obstacles_by_name
from planting_slots.sidewalk import enrich_with_ibge, generate_osm_calcada

SPACING_ID = "10m"
URBAN_BUFFER_M = 100.0  # roads this close to an urban tract are walked (keeps the cross streets at its edge)
MAX_TASKS_PER_WORKER = 20  # recycle workers so memory from big cities is returned

# Why a candidate was dropped (names used by sidewalk.py) -> id used by the prototype.
# "facade" (no room before the facade) and "sidewalk" (sidewalk too narrow) need building
# footprints, which osm_calcada does not use, so they never occur here. A candidate carries
# the first reason that blocked it.
REASON_IDS = {
    "esquina": "corner", "via_transversal": "cross-road", "espacamento_minimo": "spacing",
    "travessia": "crossing", "acesso_garagem": "driveway", "ponto_onibus": "bus-stop",
    "arvore_existente": "existing-tree", "semaforo": "traffic-light", "poste": "utility-pole",
    "transformador": "transformer", "boca_de_lobo": "drain",
}

COLUMNS = [
    "id_ponto", "cd_mun", "viavel", "motivo", "porte", "via_osm", "logradouro", "posicao_via_m",
    "cd_setor", "cd_setor_face", "cd_quadra", "cd_face",
    "cd_bairro", "nm_bairro", "cd_dist", "nm_dist", "cd_subdist", "nm_subdist",
    "cobertura_copa_pct", "raio_copa_m", "cobertura_copa_fonte", "geometry",
]
OK, EMPTY, ERROR = "ok", "sem_pontos", "erro"


def _line_parts(roads: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """As scripts/03's load_ways: one LineString per row, longer than 1 m."""
    roads = roads.explode(index_parts=False).reset_index(drop=True)
    return roads[(roads.geometry.geom_type == "LineString") & (roads.geometry.length > 1.0)].copy()


def _assign_tracts(points: gpd.GeoDataFrame, tracts: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Tract/district/neighbourhood of the tract each point lies in. The face's
    own tract (often the neighbour across the street) is kept apart."""
    points = points.rename(columns={"cd_setor": "cd_setor_face"})
    cols = ["cd_setor", "cd_bairro", "nm_bairro", "cd_dist", "nm_dist", "cd_subdist", "nm_subdist"]
    joined = gpd.sjoin(points, tracts[[*cols, "geometry"]], how="left", predicate="intersects")
    joined = joined[~joined.index.duplicated(keep="first")].drop(columns="index_right")
    return joined


def _unviable_points(rejected: list[dict], urban, faces, epsg: str) -> gpd.GeoDataFrame:
    """The dropped candidates inside the urban area, as rows like the viable ones
    (same IBGE metadata; no road class or position along the road), flagged
    `viavel` = False with the prototype's reason id."""
    columns = ["viavel", "motivo", "porte", "logradouro", "cd_setor", "cd_quadra", "cd_face", "geometry"]
    if not rejected:
        return gpd.GeoDataFrame({c: [] for c in columns}, geometry="geometry", crs=epsg)
    frame = gpd.GeoDataFrame(rejected, geometry="geometry", crs=epsg)
    frame = frame[shapely.contains_xy(urban, frame.geometry.x.to_numpy(), frame.geometry.y.to_numpy())]
    frame = frame.reset_index(drop=True)
    frame["viavel"] = False
    frame["motivo"] = frame["motivo"].map(REASON_IDS)  # an unknown reason is a bug: fail, do not hide it
    if frame["motivo"].isna().any():
        raise ValueError("motivo de descarte sem id no protótipo (atualize REASON_IDS)")
    for col in ("cd_setor", "cd_quadra", "cd_face"):
        frame[col] = None
    if faces is not None and len(faces) and len(frame):
        frame = enrich_with_ibge(frame, faces)
    return frame[columns]


def build_points(layout: Layout, cd_mun: str, sources=DEFAULT_SOURCES) -> tuple[gpd.GeoDataFrame | None, dict]:
    """The municipality's final points (EPSG:4326, COLUMNS order) or None,
    plus a dict of counters/diagnostics for the status file."""
    info: dict = {}
    muni = load_municipalities(layout, [cd_mun]).iloc[0]
    boundary = muni.geometry
    west, south, east, north = boundary.bounds
    centroid = boundary.centroid
    epsg = utm_epsg(centroid.x, centroid.y)
    info["epsg"] = epsg

    region = region_of(cd_mun)
    roads = load_roads(layout.osm_roads(region), (west, south, east, north), boundary)
    info["trechos_osm"] = len(roads)
    if roads.empty:
        return None, {**info, "motivo": "sem_vias_osm"}
    tracts = load_tracts(layout, cd_mun).to_crs(epsg)
    info["setores_urbanos"] = len(tracts)
    if tracts.empty:
        return None, {**info, "motivo": "sem_setores_urbanos"}
    urban = tracts.geometry.union_all()
    shapely.prepare(urban)

    roads = _line_parts(roads.to_crs(epsg))
    near_urban = shapely.intersects(roads.geometry.values, urban.buffer(URBAN_BUFFER_M))
    roads = roads[near_urban].reset_index(drop=True)
    info["trechos_urbanos"] = len(roads)
    if roads.empty:
        return None, {**info, "motivo": "sem_vias_urbanas"}

    obstacles = obstacles_by_name(load_obstacles(layout.osm_obstacles(region), (west, south, east, north)), epsg)
    faces = load_faces(layout, cd_mun, epsg)
    if faces is not None and len(faces):
        mid = faces.geometry.interpolate(0.5, normalized=True)
        faces = faces[shapely.contains_xy(urban, mid.x.to_numpy(), mid.y.to_numpy())].reset_index(drop=True)
    info["faces_urbanas"] = 0 if faces is None else len(faces)

    points, rejected = generate_osm_calcada(roads, obstacles, faces, SPACING_ID)
    if points.empty:
        return None, {**info, "motivo": "sem_candidatos"}
    inside = shapely.contains_xy(urban, points.geometry.x.to_numpy(), points.geometry.y.to_numpy())
    info["pontos_fora_da_area_urbana"] = int((~inside).sum())
    points = points[inside].reset_index(drop=True)
    if points.empty:
        return None, {**info, "motivo": "sem_pontos_em_area_urbana"}
    points["viavel"], points["motivo"] = True, None

    unviable = _unviable_points(rejected, urban, faces, epsg)
    info["inviaveis_por_motivo"] = {str(k): int(v) for k, v in unviable["motivo"].value_counts().items()}
    info["pontos_viaveis"], info["pontos_inviaveis"] = len(points), len(unviable)
    points = pd.concat([points, unviable], ignore_index=True)

    points = _assign_tracts(points, tracts)
    points = points.join(canopy_for_points(points, layout, sources, SPACING_ID))
    points = points.to_crs("EPSG:4326")
    points["cd_mun"] = cd_mun
    points["posicao_via_m"] = points["posicao_face_m"].astype("float32")
    points["via_osm"] = points["via_osm"].astype("object")
    points["id_ponto"] = [f"{cd_mun}-{i:08d}" for i in range(1, len(points) + 1)]  # viable first, then unviable
    points = points[COLUMNS].copy()
    for col in COLUMNS:
        if col in ("geometry", "viavel", "posicao_via_m", "raio_copa_m", "cobertura_copa_pct"):
            continue
        points[col] = points[col].astype("string")
    viable = points[points["viavel"]]
    info["pontos_sem_setor"] = int(points["cd_setor"].isna().sum())
    info["pontos_sem_face"] = int(viable["cd_face"].isna().sum())
    info["pontos_sem_copa"] = int(points["cobertura_copa_pct"].isna().sum())
    return points, info


def process_municipality(layout: Layout, cd_mun: str, sources=DEFAULT_SOURCES) -> dict:
    """Generate and write one municipality; always returns (and persists) a status dict."""
    started = time.time()
    status = {"cd_mun": cd_mun, "uf": uf_of(cd_mun)}
    try:
        points, info = build_points(layout, cd_mun, sources)
        status.update(info)
        if points is None:
            status.update(status=EMPTY, pontos=0)
        else:
            dest = layout.points(cd_mun)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")
            points.to_parquet(tmp, compression="zstd", row_group_size=100_000, index=False)
            tmp.replace(dest)
            status.update(status=OK, pontos=len(points))  # rows: viable + unviable (see pontos_viaveis)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — one municipality must not stop the batch
        status.update(status=ERROR, erro=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
    status["tempo_s"] = round(time.time() - started, 1)
    write_atomic(layout.status(cd_mun), json.dumps(status, ensure_ascii=False, indent=1))
    return status


def read_status(layout: Layout, cd_mun: str) -> dict | None:
    path = layout.status(cd_mun)
    return json.loads(path.read_text()) if path.exists() else None


def _work_size(layout: Layout, cd_mun: str) -> int:
    """Rough cost, to start the biggest municipalities first."""
    path = layout.tracts(cd_mun)
    return path.stat().st_size if path.exists() else 0


def _run_one(args) -> dict:
    return process_municipality(*args)


def run_batch(layout: Layout, cd_muns: list[str], workers: int = 1, sources=DEFAULT_SOURCES,
              redo: bool = False) -> list[dict]:
    """Process the municipalities that are not done yet (or all, with `redo`)."""
    todo = [c for c in cd_muns if redo or (read_status(layout, c) or {}).get("status") not in (OK, EMPTY)]
    skipped = len(cd_muns) - len(todo)
    print(f"[lote] {len(todo)} municípios a processar ({skipped} já concluídos), {workers} processos")
    todo.sort(key=lambda c: _work_size(layout, c), reverse=True)
    results: list[dict] = []

    def report(status: dict) -> None:
        results.append(status)
        extra = status.get("motivo") or status.get("erro") or ""
        print(f"[{len(results)}/{len(todo)}] {status['cd_mun']} {status['status']} "
              f"{status.get('pontos', 0)} pontos {status['tempo_s']}s {extra}", flush=True)

    if workers <= 1:
        for c in todo:
            report(process_municipality(layout, c, sources))
    else:
        with ProcessPoolExecutor(workers, mp_context=get_context("spawn"),
                                 max_tasks_per_child=MAX_TASKS_PER_WORKER) as pool:
            futures = [pool.submit(_run_one, (layout, c, sources)) for c in todo]
            for future in as_completed(futures):
                report(future.result())
    failed = [r["cd_mun"] for r in results if r["status"] == ERROR]
    if failed:
        print(f"[lote] {len(failed)} municípios com erro (veja status/<cd_mun>.json): {', '.join(failed[:20])}"
              + (" ..." if len(failed) > 20 else ""))
    return results


# --- validation and metadata --------------------------------------------------

def validate_municipality(layout: Layout, cd_mun: str, boundary) -> list[str]:
    """Problems found in a municipality's output (empty list = fine)."""
    problems: list[str] = []
    path = layout.points(cd_mun)
    if not path.exists():
        return ["arquivo ausente"]
    points = gpd.read_parquet(path)
    if list(points.columns) != COLUMNS:
        problems.append(f"colunas diferentes do esquema: {list(points.columns)}")
        return problems
    if points.crs is None or points.crs.to_epsg() != 4326:
        problems.append("CRS diferente de EPSG:4326")
    if not (points["cd_mun"] == cd_mun).all():
        problems.append("cd_mun diferente do nome do arquivo")
    if points["id_ponto"].duplicated().any():
        problems.append("id_ponto duplicado")
    if not (points.geometry.geom_type == "Point").all() or points.geometry.is_empty.any():
        problems.append("geometria não é ponto ou está vazia")
        return problems
    if points["cd_setor"].isna().any():
        problems.append(f"{int(points['cd_setor'].isna().sum())} pontos sem cd_setor")
    outside = ~points.geometry.within(boundary.buffer(0.002))  # ~200 m: urban tracts can overhang the boundary
    if outside.any():
        problems.append(f"{int(outside.sum())} pontos fora do limite municipal")
    copa = points["cobertura_copa_pct"].dropna()
    if len(copa) and not copa.between(0, 100).all():
        problems.append("cobertura_copa_pct fora de 0-100")
    if points["viavel"].isna().any() or (points["viavel"] != points["motivo"].isna()).any():
        problems.append("viavel e motivo inconsistentes (inviável sem motivo ou viável com motivo)")
    if not points.loc[~points["viavel"], "motivo"].isin(set(REASON_IDS.values())).all():
        problems.append("motivo fora dos ids conhecidos")
    projected = points[points["viavel"]].to_crs(utm_epsg(boundary.centroid.x, boundary.centroid.y))
    xy = np.c_[projected.geometry.x, projected.geometry.y]
    from scipy.spatial import cKDTree
    pairs = cKDTree(xy).query_pairs(GENERIC_SPACING_M - 0.01)
    if pairs:
        problems.append(f"{len(pairs)} pares de pontos viáveis a menos de {GENERIC_SPACING_M:g} m")
    return problems


def validate(layout: Layout, cd_muns: list[str]) -> pd.DataFrame:
    """Check every municipality, write resumo.csv and return it."""
    municipalities = load_municipalities(layout, cd_muns).set_index("cd_mun")
    rows = []
    for cd_mun in cd_muns:
        status = read_status(layout, cd_mun)
        row = {"cd_mun": cd_mun, "nm_mun": municipalities.loc[cd_mun, "nm_mun"] if cd_mun in municipalities.index else None,
               "uf": uf_of(cd_mun), "status": (status or {}).get("status", "nao_processado"),
               "pontos": (status or {}).get("pontos", 0),
               "pontos_viaveis": (status or {}).get("pontos_viaveis"),
               "pontos_inviaveis": (status or {}).get("pontos_inviaveis"), "tempo_s": (status or {}).get("tempo_s"),
               "motivo": (status or {}).get("motivo") or (status or {}).get("erro"),
               "pontos_sem_setor": (status or {}).get("pontos_sem_setor"),
               "pontos_sem_face": (status or {}).get("pontos_sem_face"),
               "pontos_sem_copa": (status or {}).get("pontos_sem_copa"), "problemas": ""}
        if row["status"] == OK:
            row["problemas"] = "; ".join(validate_municipality(layout, cd_mun, municipalities.loc[cd_mun, "geometry"]))
        rows.append(row)
    summary = pd.DataFrame(rows)
    layout.summary.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(layout.summary, index=False)
    return summary


def git_sha() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
                              cwd=Path(__file__).resolve().parent).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def write_metadata(layout: Layout, summary: pd.DataFrame, sources=DEFAULT_SOURCES) -> None:
    from planting_slots.brasil import GEOFABRIK_URL, IBGE_MUNICIPALITIES_URL, REGIONS
    from planting_slots.canopy import CANOPY_SOURCES
    from planting_slots.ibge import IBGE_TRACTS_URL, STREET_FACES_URL

    pbfs = {}
    for region in REGIONS:
        pbf = layout.pbf(region)
        if pbf.exists():
            md5 = pbf.with_name(pbf.name + ".md5")
            pbfs[region] = {"url": GEOFABRIK_URL.format(region=region), "bytes": pbf.stat().st_size,
                            "baixado_em": datetime.fromtimestamp(pbf.stat().st_mtime, timezone.utc).isoformat(),
                            "md5": md5.read_text().split()[0] if md5.exists() else None}
    metadata = {
        "gerado_em": datetime.now(timezone.utc).isoformat(),
        "git_sha": git_sha(),
        "metodologia": "osm_calcada", "espacamento": SPACING_ID, "apenas_area_urbana": True,
        "parametros": {"espacamento_m": GENERIC_SPACING_M, "espacamento_por_porte_m": SPACING_BY_PORTE_M,
                       "largura_minima_calcada_por_porte_m": MIN_SIDEWALK_WIDTH_BY_PORTE_M,
                       "distancias_minimas_infraestrutura_m": CLEARANCE_M},
        "esquema": COLUMNS,
        "fontes": {"osm": pbfs, "ibge": {"municipios": IBGE_MUNICIPALITIES_URL, "setores": IBGE_TRACTS_URL,
                                           "faces": STREET_FACES_URL},
                   "copa": {s: {"rotulo": CANOPY_SOURCES[s]["label"], "credito": CANOPY_SOURCES[s]["credit"]}
                            for s in sources}},
        "municipios": {"total": len(summary), **{str(k): int(v) for k, v in summary["status"].value_counts().items()}},
        "pontos": int(summary["pontos"].sum()),
        "pontos_viaveis": int(summary["pontos_viaveis"].fillna(0).sum()),
        "pontos_inviaveis": int(summary["pontos_inviaveis"].fillna(0).sum()),
        "motivos_inviabilidade": {
            "ids": sorted(set(REASON_IDS.values())),
            "nao_gerados_neste_metodo": ["facade", "sidewalk"],
            "observacao": "cada candidato descartado carrega só o primeiro motivo que o bloqueou",
        },
        "com_problemas": summary.loc[summary["problemas"].fillna("") != "", "cd_mun"].tolist(),
    }
    write_atomic(layout.metadata, json.dumps(metadata, ensure_ascii=False, indent=1))
