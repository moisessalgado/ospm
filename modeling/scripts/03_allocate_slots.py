"""Allocate tree-planting slots linked to IBGE street faces (Censo 2022).

No source available nationwide locates the curb better than ±2-3 m (OSM x IBGE
x buildings), which exceeds a typical sidewalk width. Each slot is therefore
linked to an IBGE street face (sector, block, face, street name, position along
the face): counts and spacing along the face are exact, while the lateral
position, drawn on the face line, is indicative.

Sidewalk slots, for each face in an urban census tract that faces an eligible
OSM road (parallel and within FACE_STREET_MATCH_M):
  - keep CORNER_SETBACK_M clear at both ends of the face (corners);
  - skip stretches near OSM obstacles projected onto the face (crossings,
    driveways, bus stops, existing trees);
  - place slots with each variant's spacing.

Median slots come from OSM divided roads co-registered to the buildings; a
slot is accepted only outside every carriageway grown by the registration
uncertainty measured against IBGE (worst case), corners and obstacles.

Finally, no two slots are closer than MIN_POINT_SEPARATION_M.
"""

from __future__ import annotations

import json

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from planting_slots.allocation import enforce_min_separation, greedy_walk
from planting_slots.buildings import facade_probes, load_overture_buildings
from planting_slots.ibge import load_street_faces, load_urban_area
from planting_slots.municipalities import get_municipality, municipality_arg_parser
from planting_slots.osm import (
    DEFAULT_WIDTH_BY_CLASS, angle_diff, bearing, carriageway_half_width, find_divided_pairs,
    junction_disks, load_osm_elements,
)
from planting_slots.paths import OSM_ROADS_LAYER, MunicipalityFiles
from planting_slots.registration import (
    apply_shift, coregistration_field, ibge_registration_uncertainty, normalize_street_name,
)

# --- Setbacks (MMA "Coletânea Brasileira de Arborização Urbana", 2026) -------
CORNER_SETBACK_M = 6.0
CURB_SETBACK_M = 0.5
PIT_HALF_WIDTH_M = 0.5
SERVICE_SETBACK_M = 1.5
MIN_POINT_SEPARATION_M = 6.0
SAMPLE_STEP_M = 1.0

OBSTACLES = {  # name -> (OSM tag filter, exclusion radius in m)
    "travessia": (lambda t: t.get("footway") == "crossing" or t.get("highway") == "crossing", 3.0),
    "acesso_garagem": (lambda t: t.get("amenity") == "parking_entrance" or t.get("service") == "driveway", 2.0),
    "ponto_onibus": (lambda t: t.get("highway") == "bus_stop" or t.get("public_transport") == "platform", 12.0),
    "arvore_existente": (lambda t: t.get("natural") == "tree", 4.0),
}
OBSTACLE_TO_FACE_M = 8.0  # OSM obstacles within this distance are projected onto the face

# --- Face -> road matching -------------------------------------------------------
FACE_STREET_MATCH_M = 15.0
FACE_STREET_MAX_ANGLE_DEG = 30.0
EXCLUDED_FACE_TYPES = {"VIELA", "BECO", "ESCADARIA", "TRILHOS", "MURO", "FUNDOS"}
GENERATION_CLASSES = {
    "living_street", "residential", "unclassified", "tertiary", "tertiary_link",
    "secondary", "secondary_link", "primary", "primary_link",
}

# --- Tree size by road class (sidewalk width is not measurable) ---------------
PORTE_BY_CLASS = {
    "living_street": "pequeno", "residential": "medio", "unclassified": "medio",
    "tertiary": "medio", "tertiary_link": "medio",
    "secondary": "grande", "secondary_link": "grande",
    "primary": "grande", "primary_link": "grande",
}
MEDIAN_PORTE_BY_WIDTH = [(2.0, "pequeno"), (3.5, "medio"), (np.inf, "grande")]
MIN_MEDIAN_WIDTH_M = 1.0

# --- Spacing variants (pending MMA criteria) -----------------------------------
VARIANTS = {
    "espacamento_10m": {"label": "Espaçamento genérico de 10 m", "spacing": lambda porte: 10.0},
    "espacamento_porte": {"label": "Espaçamento por porte (6/8/11 m)",
                          "spacing": lambda porte: {"pequeno": 6.0, "medio": 8.0, "grande": 11.0}[porte]},
}


def load_ways(files: MunicipalityFiles) -> gpd.GeoDataFrame:
    ways = gpd.read_file(files.osm_roads, layer=OSM_ROADS_LAYER)
    ways = ways.explode(index_parts=False).reset_index(drop=True)
    return ways[(ways.geometry.geom_type == "LineString") & (ways.geometry.length > 1.0)].copy()


def match_faces_to_streets(faces: gpd.GeoDataFrame, roads: gpd.GeoDataFrame) -> pd.DataFrame:
    """Nearest parallel vehicle road to each face (by the face midpoint)."""
    mid = faces.geometry.interpolate(0.5, normalized=True).values
    fi, ri = shapely.STRtree(roads.geometry.values).query(mid, predicate="dwithin", distance=FACE_STREET_MATCH_M)
    face_bearing = np.array([bearing(g) for g in faces.geometry])
    rows = []
    for f, r in zip(fi, ri):
        road = roads.geometry.values[r]
        pos = road.project(mid[f])
        a = road.interpolate(max(pos - 5, 0))
        b = road.interpolate(min(pos + 5, road.length))
        local = float(np.degrees(np.arctan2(b.y - a.y, b.x - a.x))) % 360
        diff = min(angle_diff(face_bearing[f], local), angle_diff(face_bearing[f], (local + 180) % 360))
        if diff <= FACE_STREET_MAX_ANGLE_DEG:
            rows.append((f, r, road.distance(mid[f])))
    match = pd.DataFrame(rows, columns=["face", "road", "dist"]).sort_values("dist").drop_duplicates("face")
    return match.set_index("face")


def blocked_intervals(face, obstacles: dict[str, np.ndarray]) -> list[tuple[float, float]]:
    out = []
    for name, (geoms, radius) in obstacles.items():
        if not len(geoms):
            continue
        near = geoms[shapely.dwithin(geoms, face, OBSTACLE_TO_FACE_M)]
        for g in near:
            p = face.project(g.centroid if g.geom_type != "Point" else g)
            out.append((p - radius, p + radius))
    return out


def allocate_face(face, blocked, spacing_at) -> list[float]:
    """Positions (m from the face start) of the slots on one face."""
    dists = np.arange(CORNER_SETBACK_M, face.length - CORNER_SETBACK_M + 1e-9, SAMPLE_STEP_M)
    if not len(dists):
        return []
    ok = np.ones(len(dists), dtype=bool)
    for a, b in blocked:
        ok &= ~((dists >= a) & (dists <= b))
    return [float(dists[i]) for i in greedy_walk(dists, ok, spacing_at)]


def main() -> None:
    municipality = get_municipality(municipality_arg_parser(__doc__).parse_args().municipio)
    files = MunicipalityFiles(municipality)
    files.ensure_dirs()
    crs = municipality.epsg

    urban_area = load_urban_area(files, crs)
    faces = load_street_faces(files, crs)
    mid = faces.geometry.interpolate(0.5, normalized=True)
    faces = faces[shapely.contains_xy(urban_area, mid.x.to_numpy(), mid.y.to_numpy())].reset_index(drop=True)
    print(f"Faces de logradouro em setores urbanos: {len(faces)}")

    # --- OSM co-registered to the buildings (road class, obstacles, medians)
    ways = load_ways(files)
    pairs = find_divided_pairs(ways)
    ways["paired"] = ways.index.isin(pairs.keys())
    buildings = load_overture_buildings(files.buildings, crs)
    btree, bgeoms = shapely.STRtree(buildings.geometry.values), buildings.geometry.values
    eligible = ways[ways["highway"].isin(GENERATION_CLASSES)]
    probes = {idx: facade_probes(g, btree, bgeoms) for idx, g in eligible.geometry.items()}
    gx, gy, shift, _ = coregistration_field(ways.loc[eligible.index], probes)
    ways["geometry"] = apply_shift(ways.geometry.values, gx, gy, shift)
    uncertainty, n_ways, _ = ibge_registration_uncertainty(ways.loc[eligible.index], faces)
    print(f"Incerteza de registro OSM x IBGE (p90, {n_ways} vias): {uncertainty:.1f} m")

    obstacles = {}
    for name, (flt, radius) in OBSTACLES.items():
        geoms = load_osm_elements(files.osm_obstacles, flt, crs).geometry.values
        obstacles[name] = (apply_shift(geoms, gx, gy, shift), radius)

    roads = ways[ways["highway"].isin(DEFAULT_WIDTH_BY_CLASS)].copy()
    roads["half"] = [carriageway_half_width(r, p, np.nan, municipality.min_sidewalk_width_m)[0]
                     for r, p in zip(roads.to_dict("records"), roads["paired"])]

    # --- sidewalk slots from the faces ------------------------------------------
    match = match_faces_to_streets(faces, roads)
    faces["highway"] = pd.Series(roads["highway"].to_numpy()[match["road"].to_numpy()], index=match.index)
    faces["tipo"] = faces["NM_TIP_LOG"].fillna("").str.upper()
    face_ok = faces["highway"].isin(GENERATION_CLASSES) & ~faces["tipo"].isin(EXCLUDED_FACE_TYPES)
    summary = {
        "faces_urbanas": len(faces),
        "faces_sem_via_osm": int(faces["highway"].isna().sum()),
        "faces_via_nao_elegivel": int((faces["highway"].notna() & ~faces["highway"].isin(GENERATION_CLASSES)).sum()),
        "faces_tipo_excluido": int(faces["tipo"].isin(EXCLUDED_FACE_TYPES).sum()),
        "faces_elegiveis": int(face_ok.sum()),
    }
    print("Faces:", summary)
    sel = faces[face_ok]
    blocked = {i: blocked_intervals(g, obstacles) for i, g in sel.geometry.items()}

    # --- median slots (OSM, worst case) -----------------------------------------
    roads["half_worst"] = roads["half"] + uncertainty
    margin = np.where(roads["highway"] == "service", SERVICE_SETBACK_M, CURB_SETBACK_M)
    exclusion = list(roads.geometry.buffer(roads["half_worst"].to_numpy() + margin))
    exclusion += junction_disks(roads.assign(half=roads["half_worst"]), CORNER_SETBACK_M,
                                CURB_SETBACK_M + PIT_HALF_WIDTH_M)
    for geoms, radius in obstacles.values():
        exclusion += list(shapely.buffer(geoms, radius))
    etree = shapely.STRtree(exclusion)
    median_groups = []
    for a, b in {tuple(sorted(p)) for p in pairs.items()}:
        if ways.loc[a, "highway"] not in GENERATION_CLASSES:
            continue
        way_a, way_b = ways.geometry.loc[a], ways.geometry.loc[b]
        d = np.arange(SAMPLE_STEP_M / 2, way_a.length, SAMPLE_STEP_M)
        pts = shapely.line_interpolate_point(way_a, d)
        near = shapely.line_interpolate_point(way_b, shapely.line_locate_point(way_b, pts))
        mids = shapely.points((shapely.get_x(pts) + shapely.get_x(near)) / 2, (shapely.get_y(pts) + shapely.get_y(near)) / 2)
        width = shapely.distance(pts, near) - roads.loc[a, "half_worst"] - roads.loc[b, "half_worst"]
        ok = (width >= MIN_MEDIAN_WIDTH_M) & shapely.contains_xy(urban_area, shapely.get_x(mids), shapely.get_y(mids))
        hit = etree.query(mids, predicate="within")[0]
        ok[np.unique(hit)] = False
        median_groups.append({"row": ways.loc[a], "dists": d, "pts": mids, "width": width, "ok": ok})

    # --- allocation per variant -------------------------------------------------
    report = {
        "municipio": municipality.slug,
        "metodo": "vagas vinculadas às faces de logradouro IBGE 2022; posição lateral indicativa",
        "parametros": {
            "afastamento_esquina_m": CORNER_SETBACK_M, "espacamento_minimo_global_m": MIN_POINT_SEPARATION_M,
            "incerteza_registro_canteiros_m": round(uncertainty, 2),
        },
        "faces": summary,
        "variantes": {},
    }
    for variant, cfg in VARIANTS.items():
        records = []
        for i, face in sel.iterrows():
            porte = PORTE_BY_CLASS[face["highway"]]
            for pos in allocate_face(face.geometry, blocked[i], lambda _: cfg["spacing"](porte)):
                records.append({
                    "tipo_vaga": "calcada", "porte": porte, "via_osm": face["highway"],
                    "logradouro": normalize_street_name(face["NM_TIP_LOG"], face["NM_TIT_LOG"], face["NM_LOG"]),
                    "cd_setor": face["CD_SETOR"], "cd_quadra": face["CD_QUADRA"], "cd_face": face["CD_FACE"],
                    "posicao_face_m": round(pos, 1), "posicao_lateral": "indicativa (linha da face IBGE)",
                    "geometry": face.geometry.interpolate(pos),
                })
        for g in median_groups:
            portes = [next(p for lim, p in MEDIAN_PORTE_BY_WIDTH if w < lim) for w in g["width"]]
            for i in greedy_walk(g["dists"], g["ok"], lambda i: cfg["spacing"](portes[i])):
                records.append({
                    "tipo_vaga": "canteiro_central", "porte": portes[i], "via_osm": g["row"]["highway"],
                    "logradouro": normalize_street_name(g["row"]["name"]),
                    "cd_setor": None, "cd_quadra": None, "cd_face": None, "posicao_face_m": None,
                    "posicao_lateral": "pior caso (OSM co-registrado)", "geometry": g["pts"][i],
                })

        points = gpd.GeoDataFrame(records, geometry="geometry", crs=crs)
        before = len(points)
        points = enforce_min_separation(points, MIN_POINT_SEPARATION_M).reset_index(drop=True)
        points.to_file(files.slots(variant, "gpkg"), layer="vagas_plantio", driver="GPKG")
        points.to_crs("EPSG:4326").to_file(files.slots(variant, "geojson"), driver="GeoJSON")
        report["variantes"][variant] = {
            "label": cfg["label"], "pontos": len(points), "removidos_espacamento_minimo": before - len(points),
            "por_tipo": points["tipo_vaga"].value_counts().to_dict(),
            "por_porte": points["porte"].value_counts().to_dict(),
        }
        print(f"[{variant}] {len(points)} pontos (espaçamento mínimo removeu {before - len(points)})",
              points["tipo_vaga"].value_counts().to_dict(), points["porte"].value_counts().to_dict())

    files.allocation_report.write_text(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
