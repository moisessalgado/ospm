"""v2 point allocation: points are only accepted inside a "zona plantável".

The zone is the complement of an exclusion layer built from:
  - the carriageway (leito) of EVERY vehicle road, not only the one being
    processed, estimated from OSM width/lanes or a conservative per-class
    default, grown by the curb setback (afastamento do meio-fio);
  - corners (esquinas): a disk around every street junction, including
    T-junctions on interior nodes, sized so the tree stays CORNER_SETBACK_M
    past the crossing street's curb;
  - driveways/service roads, crossings, bus stops, existing mapped trees;
  - building footprints (as polygons) and a minimum clearance to them.

Candidates are sampled along a line at curb + CURB_SETBACK_M + PIT_HALF_WIDTH_M
(center of a 1 m planting pit) on each sidewalk side, plus the midline of
divided roads (canteiro central), and every accepted point is guaranteed to be
outside the exclusion layer. Two spacing variants are produced (see VARIANTS).

Parameters follow the MMA "Coletânea Brasileira de Arborização Urbana" (2026):
meio-fio 0,5 m, esquinas 5–7 m (6 m adopted), área livre de 1,0 m.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

import geopandas as gpd
import httpx
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import LineString, Point, Polygon

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.municipios import get_municipio, municipio_arg_parser  # noqa: E402

# --- Afastamentos (coletâneas MMA) -------------------------------------------
CURB_SETBACK_M = 0.5        # distância mínima do meio-fio
PIT_HALF_WIDTH_M = 0.5      # metade da área livre de 1,0 m -> eixo da cova
CORNER_SETBACK_M = 6.0      # esquinas
SERVICE_SETBACK_M = 1.5     # acessos de veículos / guia rebaixada
MIN_BUILDING_CLEARANCE_M = 1.0
MIN_MEDIAN_WIDTH_M = 1.0    # canteiro central mais estreito que isto não recebe árvore
MIN_POINT_SEPARATION_M = 6.0
SAMPLE_STEP_M = 1.0

BUFFER_CROSSING_M = 3.0
BUFFER_DRIVEWAY_M = 2.0
BUFFER_BUS_STOP_M = 12.0
BUFFER_EXISTING_TREE_M = 4.0
CYCLEWAY_HALF_WIDTH_M = 1.25

# --- Largura do leito (meio-fio a meio-fio), padrão conservador ---------------
# Usado só quando a via não tem width= nem lanes=. Valores deliberadamente
# largos: errar a favor da calçada, nunca a favor da rua.
DEFAULT_WIDTH_BY_CLASS = {
    "motorway": 22.0, "motorway_link": 8.0, "trunk": 16.0, "trunk_link": 8.0,
    "primary": 14.0, "primary_link": 8.0, "secondary": 12.0, "secondary_link": 8.0,
    "tertiary": 10.0, "tertiary_link": 7.0, "unclassified": 9.0, "residential": 9.0,
    "living_street": 6.0, "service": 6.0, "track": 4.0, "busway": 7.0, "road": 9.0,
}
LANE_WIDTH_M = 3.5
LANE_EXTRA_M = 2.0           # sarjetas / faixa de estacionamento
MIN_PLAUSIBLE_WIDTH_M = 3.0
MAX_PLAUSIBLE_WIDTH_FACTOR = 2.5  # width= acima de 2,5x o padrão da classe é ignorado

# --- Alinhamento predial estimado pelas fachadas -------------------------------
FACADE_PROBE_STEP_M = 5.0
FACADE_PROBE_LENGTH_M = 40.0
FACADE_WINDOW_M = 15.0
FACADE_PERCENTILE = 25
FACADE_MIN_HITS = 2

# --- Escopo: apenas setores censitários urbanos (IBGE, Censo 2022) ----------
URBAN_SITUATION_CODES = ["1", "2", "3"]  # alta/baixa densidade de edificações, núcleo urbano
IBGE_SETORES_URL = (
    "https://geoftp.ibge.gov.br/organizacao_do_territorio/malhas_territoriais/"
    "malhas_de_setores_censitarios__divisoes_intramunicipais/censo_2022/setores/gpkg/UF/{uf}/{uf}_setores_CD2022.gpkg"
)

# --- Incerteza de registro entre fontes (eixo OSM x edificações) ---------------
# Medida por município: em vias de mão dupla com fachadas dos dois lados, a
# assimetria (esq - dir)/2 estima o deslocamento do eixo OSM. Usa-se o p90 como
# margem de pior caso (superestima o erro, pois inclui recuos assimétricos).
REGISTRATION_PERCENTILE = 90
REGISTRATION_MAX_FACADE_M = 25.0
REGISTRATION_MIN_PAIRS = 4
REGISTRATION_MIN_WAYS = 30
DEFAULT_REGISTRATION_UNCERTAINTY_M = 3.0

# --- Eixo das faces de logradouro do IBGE (Censo 2022) ------------------------
IBGE_SAMPLE_STEP_M = 10.0
IBGE_MIN_FACE_GAP_M = 2.0
IBGE_MAX_FACE_GAP_M = 30.0
IBGE_MAX_AXIS_MATCH_M = 15.0

# --- Co-registro do OSM às edificações -----------------------------------------
COREG_CELL_M = 250.0
COREG_RADIUS_M = 400.0
COREG_MIN_OBS = 40
COREG_RIDGE = 20.0     # equivale a ~20 medidas "puxando" o deslocamento para zero
COREG_HUBER_M = 1.5
DIVIDED_ROAD_FACTOR = 0.6    # cada pista de uma via duplicada mapeada separadamente
CORNER_CLASSES = set(DEFAULT_WIDTH_BY_CLASS) - {"service", "track"}

# Classes que recebem linhas de pontos de calçada
GENERATION_CLASSES = {
    "living_street", "residential", "unclassified", "tertiary", "tertiary_link",
    "secondary", "secondary_link", "primary", "primary_link",
}

# --- Porte -------------------------------------------------------------------
PEQUENO_MAX_M = 2.0
MEDIO_MAX_M = 3.5
PORTE_RANK = {"pequeno": 0, "medio": 1, "grande": 2}
DEFAULT_PORTE_BY_CLASS = {
    "living_street": "pequeno", "residential": "medio", "unclassified": "medio",
    "tertiary": "medio", "tertiary_link": "medio",
    "secondary": "grande", "secondary_link": "grande",
    "primary": "grande", "primary_link": "grande",
}

# --- Variantes de espaçamento (aguardando critérios do MMA) ------------------
VARIANTS = {
    "generico10": {"label": "Espaçamento genérico 10 m", "spacing": lambda porte: 10.0},
    "porte": {"label": "Espaçamento por porte (6/8/11 m)",
              "spacing": lambda porte: {"pequeno": 6.0, "medio": 8.0, "grande": 11.0}[porte]},
}

# --- Pareamento de pistas duplas (mesma lógica do v1) -------------------------
MEDIAN_MAX_GAP_M = 45.0
PAIR_SEARCH_RADIUS_M = 50.0
BEARING_TOLERANCE_DEG = 35.0

SIDEWALK_NORMALIZE = {
    "r": "right", "rught": "right", "right;both": "both", "both;left": "both",
    "n": "no", "yes": "both", "separate": "both",
}


def parse_number(value) -> float | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    m = re.match(r"^\s*([0-9]+(?:[.,][0-9]+)?)", str(value))
    return float(m.group(1).replace(",", ".")) if m else None


def tag_half_width(row, default_width: float) -> tuple[float | None, str | None]:
    """Half carriageway width claimed by OSM tags, or None if absent/implausible."""
    width = parse_number(row["width"])
    if width is not None and MIN_PLAUSIBLE_WIDTH_M <= width <= MAX_PLAUSIBLE_WIDTH_FACTOR * default_width:
        return width / 2, "width"
    lanes = parse_number(row["lanes"])
    if lanes is not None and 1 <= lanes <= 10:
        return (lanes * LANE_WIDTH_M + LANE_EXTRA_M) / 2, "lanes"
    return None, None


def carriageway_half_width(row, paired: bool, facade_median: float, calcada_min: float) -> tuple[float, str]:
    """Half carriageway width. Geometry decides how much to trust the tags:
    a tag never narrows the class default (conservative), is accepted as the
    carriageway only if it still leaves calcada_min before the facades, and
    is otherwise read as the total street width (sidewalks included)."""
    default_width = DEFAULT_WIDTH_BY_CLASS.get(row["highway"], 9.0)
    default_half = default_width * (DIVIDED_ROAD_FACTOR if paired else 1.0) / 2
    tag_half, source = tag_half_width(row, default_width)
    if tag_half is None or tag_half <= default_half:
        return default_half, "padrão por classe"
    if not np.isnan(facade_median) and tag_half + calcada_min <= facade_median:
        return tag_half, f"{source} compatível com fachadas"
    return max(default_half, tag_half - calcada_min), f"{source} tratado como largura total"


def facade_probes(line: LineString, tree: shapely.STRtree, buildings: np.ndarray):
    """Perpendicular probes from the centerline every FACADE_PROBE_STEP_M:
    distance to the first building on each side (NaN when none within
    FACADE_PROBE_LENGTH_M). Returns (positions, left_dist, right_dist)."""
    if line.length < FACADE_PROBE_STEP_M:
        return np.empty(0), np.empty(0), np.empty(0)
    pos = np.arange(FACADE_PROBE_STEP_M / 2, line.length, FACADE_PROBE_STEP_M)
    c = shapely.get_coordinates(shapely.line_interpolate_point(line, pos))
    ahead = shapely.get_coordinates(shapely.line_interpolate_point(line, np.minimum(pos + 0.5, line.length)))
    behind = shapely.get_coordinates(shapely.line_interpolate_point(line, np.maximum(pos - 0.5, 0)))
    t = ahead - behind
    t /= np.maximum(np.linalg.norm(t, axis=1, keepdims=True), 1e-9)
    normal_left = np.column_stack([-t[:, 1], t[:, 0]])
    origins = shapely.points(c)
    out = []
    for sign in (1, -1):
        ends = c + sign * normal_left * FACADE_PROBE_LENGTH_M
        probes = shapely.linestrings(np.stack([c, ends], axis=1))
        dist = np.full(len(pos), np.nan)
        hits = tree.query(probes, predicate="intersects")
        if hits.shape[1]:
            d = shapely.distance(origins[hits[0]], buildings[hits[1]])
            np.fmin.at(dist, hits[0], d)
        out.append(dist)
    return pos, out[0], out[1]


def probe_frames(line: LineString, pos: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Centerline coordinates and left unit normals at the probe positions."""
    c = shapely.get_coordinates(shapely.line_interpolate_point(line, pos))
    ahead = shapely.get_coordinates(shapely.line_interpolate_point(line, np.minimum(pos + 0.5, line.length)))
    behind = shapely.get_coordinates(shapely.line_interpolate_point(line, np.maximum(pos - 0.5, 0)))
    t = ahead - behind
    t /= np.maximum(np.linalg.norm(t, axis=1, keepdims=True), 1e-9)
    return c, np.column_stack([-t[:, 1], t[:, 0]])


def coregistration_field(ways: gpd.GeoDataFrame, probes: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Systematic shift (dx, dy) of the OSM centerlines relative to the
    building footprints, on a COREG_CELL_M grid.

    Each probe with facades on both sides of a two-way street measures the
    centerline offset along its normal: o = (left - right) / 2 ≈ n·t + e,
    where t is the local shift and e the asymmetry of building setbacks.
    Streets with different bearings constrain both components of t and the
    setback noise averages out; a ridge term pulls t to zero where the
    geometry is poorly conditioned or measurements are scarce.
    """
    xy, normals, obs = [], [], []
    for idx in ways.index[ways["oneway"].isna() & ~ways["paired"]]:
        pos, left, right = probes[idx]
        both = ~np.isnan(left) & ~np.isnan(right) & (left < REGISTRATION_MAX_FACADE_M) & (right < REGISTRATION_MAX_FACADE_M)
        if both.sum() < REGISTRATION_MIN_PAIRS:
            continue
        c, n = probe_frames(ways.geometry.loc[idx], pos[both])
        xy.append(c), normals.append(n), obs.append((left[both] - right[both]) / 2)
    xy, normals, obs = np.concatenate(xy), np.concatenate(normals), np.concatenate(obs)
    gx, gy, shift = fit_shift_field(xy, normals, obs)
    return gx, gy, shift, xy


def fit_shift_field(xy: np.ndarray, normals: np.ndarray, obs: np.ndarray):
    """Robust local least squares of obs ≈ normal·t on a COREG_CELL_M grid."""
    from scipy.spatial import cKDTree
    x0, y0 = xy.min(axis=0) - COREG_CELL_M
    nx = int((xy[:, 0].max() - x0) // COREG_CELL_M) + 3
    ny = int((xy[:, 1].max() - y0) // COREG_CELL_M) + 3
    gx = x0 + np.arange(nx) * COREG_CELL_M
    gy = y0 + np.arange(ny) * COREG_CELL_M
    shift = np.zeros((ny, nx, 2))
    kd = cKDTree(xy)
    for j, cy in enumerate(gy):
        for i, cx in enumerate(gx):
            sel = kd.query_ball_point((cx, cy), COREG_RADIUS_M)
            if len(sel) < COREG_MIN_OBS:
                continue
            N, o = normals[sel], obs[sel]
            w = np.ones(len(sel))
            t = np.zeros(2)
            for _ in range(5):  # IRLS com pesos de Huber
                A = (N * w[:, None]).T @ N + COREG_RIDGE * np.eye(2)
                t = np.linalg.solve(A, (N * w[:, None]).T @ o)
                r = np.abs(o - N @ t)
                w = np.where(r <= COREG_HUBER_M, 1.0, COREG_HUBER_M / np.maximum(r, 1e-9))
            shift[j, i] = t
    return gx, gy, shift


def apply_shift(geom, gx: np.ndarray, gy: np.ndarray, shift: np.ndarray):
    """Translate every vertex by the bilinearly interpolated shift field. The
    shift depends only on position, so vertices shared by several ways stay
    shared (topology preserved)."""
    def move(coords: np.ndarray) -> np.ndarray:
        fx = np.clip((coords[:, 0] - gx[0]) / COREG_CELL_M, 0, len(gx) - 1.001)
        fy = np.clip((coords[:, 1] - gy[0]) / COREG_CELL_M, 0, len(gy) - 1.001)
        i0, j0 = fx.astype(int), fy.astype(int)
        ax, ay = (fx - i0)[:, None], (fy - j0)[:, None]
        s = (shift[j0, i0] * (1 - ax) * (1 - ay) + shift[j0, i0 + 1] * ax * (1 - ay)
             + shift[j0 + 1, i0] * (1 - ax) * ay + shift[j0 + 1, i0 + 1] * ax * ay)
        return coords + s
    return shapely.transform(geom, move)


def local_facade(sample_pos: np.ndarray, probe_pos: np.ndarray, probe_dist: np.ndarray) -> np.ndarray:
    """Facade distance at each sample: low percentile of the probes within
    ±FACADE_WINDOW_M (robust to gaps between buildings and odd shapes)."""
    out = np.full(len(sample_pos), np.nan)
    valid = ~np.isnan(probe_dist)
    if valid.sum() < FACADE_MIN_HITS:
        return out
    pp, pd_ = probe_pos[valid], probe_dist[valid]
    lo = np.searchsorted(pp, sample_pos - FACADE_WINDOW_M)
    hi = np.searchsorted(pp, sample_pos + FACADE_WINDOW_M)
    for i, (a, b) in enumerate(zip(lo, hi)):
        if b - a >= FACADE_MIN_HITS:
            out[i] = np.percentile(pd_[a:b], FACADE_PERCENTILE)
    return out


def normalize_sidewalk(value):
    if pd.isna(value):
        return None
    value = str(value).strip().lower()
    return SIDEWALK_NORMALIZE.get(value, value)


def sides_for(sidewalk_norm):
    if sidewalk_norm == "no":
        return []
    if sidewalk_norm == "left":
        return ["left"]
    if sidewalk_norm == "right":
        return ["right"]
    return ["left", "right"]


def bearing(line: LineString) -> float:
    (x0, y0), (x1, y1) = line.coords[0], line.coords[-1]
    return float(np.degrees(np.arctan2(y1 - y0, x1 - x0))) % 360


def angle_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return min(d, 360 - d)


def find_divided_pairs(ways: gpd.GeoDataFrame) -> dict[int, int]:
    candidates = ways[
        ways["oneway"].isin(["yes", "-1"])
        & ways["name"].notna()
        & ways["highway"].isin(["primary", "primary_link", "secondary", "secondary_link", "trunk", "trunk_link", "tertiary"])
    ].copy()
    pairs: dict[int, int] = {}
    if candidates.empty:
        return pairs
    candidates["bearing"] = candidates.geometry.apply(bearing)
    candidates["centroid"] = candidates.geometry.centroid
    for _, group in candidates.groupby("name"):
        if len(group) < 2:
            continue
        idxs = group.index.to_list()
        centroids = np.array([[p.x, p.y] for p in group["centroid"]])
        bearings = group["bearing"].to_numpy()
        best_match = {}
        for i, idx_i in enumerate(idxs):
            best_dist, best_j = None, None
            for j, idx_j in enumerate(idxs):
                if i == j:
                    continue
                dist = np.linalg.norm(centroids[i] - centroids[j])
                if dist > PAIR_SEARCH_RADIUS_M:
                    continue
                if angle_diff(bearings[i], (bearings[j] + 180) % 360) > BEARING_TOLERANCE_DEG:
                    continue
                gap = group.geometry.loc[idx_i].distance(group.geometry.loc[idx_j])
                if not (1.0 <= gap <= MEDIAN_MAX_GAP_M):
                    continue
                if best_dist is None or dist < best_dist:
                    best_dist, best_j = dist, idx_j
            if best_j is not None:
                best_match[idx_i] = best_j
        for idx_i, idx_j in best_match.items():
            if best_match.get(idx_j) == idx_i and idx_i < idx_j:
                pairs[idx_i] = idx_j
                pairs[idx_j] = idx_i
    return pairs


def side_facing(line: LineString, other: Point) -> str:
    left, right = line.offset_curve(1.0), line.offset_curve(-1.0)
    if left.is_empty or right.is_empty:
        return "right"
    return "left" if left.distance(other) < right.distance(other) else "right"


def densify(line, step: float) -> tuple[np.ndarray, np.ndarray]:
    """Return (distances, points) along each part of a (Multi)LineString."""
    parts = list(line.geoms) if line.geom_type == "MultiLineString" else [line]
    dists, pts, offset = [], [], 0.0
    for part in parts:
        if part.length < step:
            continue
        d = np.arange(step / 2, part.length, step)
        dists.append(d + offset)
        pts.append(shapely.line_interpolate_point(part, d))
        offset += part.length + 1e6  # parts are separate runs: break spacing continuity
    if not dists:
        return np.empty(0), np.empty(0, dtype=object)
    return np.concatenate(dists), np.concatenate(pts)


def classify_porte(clearance: float, class_default: str) -> str:
    if clearance < PEQUENO_MAX_M:
        by_width = "pequeno"
    elif clearance < MEDIO_MAX_M:
        by_width = "medio"
    else:
        by_width = "grande"
    return min([by_width, class_default], key=lambda p: PORTE_RANK[p])


def load_osm_elements(path: Path, tag_filter, crs: str) -> gpd.GeoDataFrame:
    raw = json.loads(path.read_text())
    geoms = []
    for el in raw["elements"]:
        if not tag_filter(el.get("tags", {})):
            continue
        if el["type"] == "node":
            geoms.append(Point(el["lon"], el["lat"]))
        elif el["type"] == "way" and "geometry" in el:
            coords = [(p["lon"], p["lat"]) for p in el["geometry"]]
            if len(coords) >= 2:
                geoms.append(LineString(coords))
    return gpd.GeoDataFrame(geometry=geoms, crs="EPSG:4326").to_crs(crs)


def load_buildings(path: Path, crs: str) -> gpd.GeoDataFrame:
    raw = json.loads(path.read_text())
    geoms = []
    for el in raw["elements"]:
        if el["type"] != "way" or "geometry" not in el:
            continue
        coords = [(p["lon"], p["lat"]) for p in el["geometry"]]
        if len(coords) >= 4 and coords[0] == coords[-1]:
            poly = Polygon(coords)
            geoms.append(poly if poly.is_valid else poly.buffer(0))
    return gpd.GeoDataFrame(geometry=geoms, crs="EPSG:4326").to_crs(crs)


def load_overture_buildings(path: Path, crs: str) -> gpd.GeoDataFrame:
    gdf = gpd.read_parquet(path, columns=["geometry"]).set_crs("EPSG:4326", allow_override=True)
    gdf = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].explode(index_parts=False)
    return gdf.to_crs(crs).reset_index(drop=True)


def junction_disks(roads: gpd.GeoDataFrame) -> list:
    """Corner exclusion disks at every street junction (incl. T-junctions on
    interior nodes). Radius keeps the pit axis CORNER_SETBACK_M past the
    crossing street's curb."""
    incidence: dict[tuple, int] = defaultdict(int)
    way_ids: dict[tuple, set] = defaultdict(set)
    max_half: dict[tuple, float] = defaultdict(float)
    streets = roads[roads["highway"].isin(CORNER_CLASSES)]
    for idx, geom, half in zip(streets.index, streets.geometry, streets["half"]):
        coords = list(geom.coords)
        for i, c in enumerate(coords):
            key = (round(c[0], 2), round(c[1], 2))
            incidence[key] += 1 if i in (0, len(coords) - 1) else 2
            way_ids[key].add(idx)
            max_half[key] = max(max_half[key], half)
    disks = []
    for key, inc in incidence.items():
        if inc >= 3 and len(way_ids[key]) >= 2:
            h = max_half[key]
            r = float(np.hypot(h + CORNER_SETBACK_M, h + CURB_SETBACK_M + PIT_HALF_WIDTH_M))
            disks.append(Point(key).buffer(r, quad_segs=8))
    return disks


def load_urban_area(municipio, crs: str):
    """Union of the municipality's urban census tracts (IBGE Censo 2022,
    CD_SIT 1-3), downloaded from IBGE's public FTP when missing."""
    path = Path(__file__).resolve().parent.parent / "data" / "ibge" / f"{municipio.uf_sigla}_setores_CD2022.gpkg"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        url = IBGE_SETORES_URL.format(uf=municipio.uf_sigla)
        print(f"Baixando setores censitários: {url}")
        with httpx.stream("GET", url, timeout=900) as resp, open(path, "wb") as fh:
            resp.raise_for_status()
            for chunk in resp.iter_bytes():
                fh.write(chunk)
    tracts = gpd.read_file(path, where=f"CD_MUN='{municipio.codigo_ibge}'")
    urban = tracts[tracts["CD_SIT"].isin(URBAN_SITUATION_CODES)].to_crs(crs)
    area = urban.geometry.union_all()
    shapely.prepare(area)
    return area


def normalize_street_name(*parts) -> str:
    text = " ".join(str(p) for p in parts if isinstance(p, str))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text.upper()).strip()


def ibge_axis_points(faces: gpd.GeoDataFrame) -> np.ndarray:
    """Street axis according to IBGE: midpoints between each face and the
    opposite face of the same street (other block)."""
    faces = faces.reset_index(drop=True)
    name = np.array([normalize_street_name(a, b, c) for a, b, c in
                     zip(faces["NM_TIP_LOG"], faces["NM_TIT_LOG"], faces["NM_LOG"])])
    block = (faces["CD_SETOR"] + faces["CD_QUADRA"]).to_numpy()
    samples, owner = [], []
    for i, g in enumerate(faces.geometry):
        if g.length < 2 * IBGE_SAMPLE_STEP_M:
            continue
        d = np.arange(IBGE_SAMPLE_STEP_M, g.length - IBGE_SAMPLE_STEP_M / 2, IBGE_SAMPLE_STEP_M)
        samples.append(shapely.line_interpolate_point(g, d))
        owner.append(np.full(len(d), i))
    samples, owner = np.concatenate(samples), np.concatenate(owner)
    tree = shapely.STRtree(faces.geometry.values)
    si, fj = tree.query(samples, predicate="dwithin", distance=IBGE_MAX_FACE_GAP_M)
    keep = (name[fj] == name[owner[si]]) & (block[fj] != block[owner[si]]) & (name[fj] != "")
    si, fj = si[keep], fj[keep]
    gap = shapely.distance(samples[si], faces.geometry.values[fj])
    ok = gap >= IBGE_MIN_FACE_GAP_M
    si, fj, gap = si[ok], fj[ok], gap[ok]
    best = pd.DataFrame({"s": si, "f": fj, "gap": gap}).sort_values("gap").drop_duplicates("s")
    pts = samples[best["s"].to_numpy()]
    opp = faces.geometry.values[best["f"].to_numpy()]
    near = shapely.line_interpolate_point(opp, shapely.line_locate_point(opp, pts))
    return (shapely.get_coordinates(pts) + shapely.get_coordinates(near)) / 2


def ibge_registration_uncertainty(ways: gpd.GeoDataFrame, faces: gpd.GeoDataFrame) -> tuple[float, int, int]:
    """p90 (per street) of the local discrepancy between the co-registered
    OSM axis and the IBGE axis, after removing IBGE's own systematic shift.
    Independent sources and no setback effect: bounds the residual error."""
    axis = ibge_axis_points(faces)
    pts = shapely.points(axis)
    tree = shapely.STRtree(ways.geometry.values)
    pi, wi = tree.query_nearest(pts, max_distance=IBGE_MAX_AXIS_MATCH_M, all_matches=False)
    xy, normals, obs, way_of = [], [], [], []
    for w in np.unique(wi):
        sel = pi[wi == w]
        line = ways.geometry.values[w]
        c, n = probe_frames(line, shapely.line_locate_point(line, pts[sel]))
        xy.append(c), normals.append(n), way_of.append(np.full(len(sel), w))
        obs.append(np.einsum("ij,ij->i", axis[sel] - c, n))
    xy, normals, obs, way_of = map(np.concatenate, (xy, normals, obs, way_of))
    gx, gy, shift = fit_shift_field(xy, normals, obs)
    field = shapely.get_coordinates(apply_shift(shapely.points(xy), gx, gy, shift)) - xy
    residual = np.abs(obs - np.einsum("ij,ij->i", normals, field))
    per_way = pd.Series(residual).groupby(way_of).agg(["median", "size"])
    per_way = per_way[per_way["size"] >= 3]["median"]
    return float(np.percentile(per_way, REGISTRATION_PERCENTILE)), len(per_way), len(axis)


def registration_uncertainty(eligible: gpd.GeoDataFrame, probes: dict) -> tuple[float, int]:
    offsets = []
    for idx in eligible.index[eligible["oneway"].isna() & ~eligible["paired"]]:
        _, left, right = probes[idx]
        both = ~np.isnan(left) & ~np.isnan(right) & (left < REGISTRATION_MAX_FACADE_M) & (right < REGISTRATION_MAX_FACADE_M)
        if both.sum() >= REGISTRATION_MIN_PAIRS:
            offsets.append(abs(np.median((left[both] - right[both]) / 2)))
    if len(offsets) < REGISTRATION_MIN_WAYS:
        return DEFAULT_REGISTRATION_UNCERTAINTY_M, len(offsets)
    return float(np.percentile(offsets, REGISTRATION_PERCENTILE)), len(offsets)


def allowed_mask(points: np.ndarray, tree: shapely.STRtree) -> np.ndarray:
    hit = tree.query(points, predicate="within")
    mask = np.ones(len(points), dtype=bool)
    mask[np.unique(hit[0])] = False
    return mask


def enforce_min_separation(points: gpd.GeoDataFrame, min_sep: float) -> gpd.GeoDataFrame:
    """Greedy dedup guaranteeing no two kept points closer than min_sep
    (grid hash of cell size min_sep, checking the 3x3 neighbourhood)."""
    grid: dict[tuple[int, int], list[tuple[float, float]]] = {}
    xs, ys = points.geometry.x.to_numpy(), points.geometry.y.to_numpy()
    keep = np.zeros(len(points), dtype=bool)
    sq = min_sep * min_sep
    for i in range(len(points)):
        x, y = xs[i], ys[i]
        cx, cy = int(x // min_sep), int(y // min_sep)
        ok = True
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for ox, oy in grid.get((cx + dx, cy + dy), ()):
                    if (ox - x) ** 2 + (oy - y) ** 2 < sq:
                        ok = False
                        break
                if not ok:
                    break
            if not ok:
                break
        if ok:
            grid.setdefault((cx, cy), []).append((x, y))
            keep[i] = True
    return points[keep]


def greedy_walk(dists, ok, spacing_at) -> list[int]:
    """Place a point at the first allowed sample, then skip ahead by the
    spacing implied at that sample."""
    placed, next_allowed = [], -np.inf
    for i, d in enumerate(dists):
        if d < next_allowed or not ok[i]:
            continue
        placed.append(i)
        next_allowed = d + spacing_at(i)
    return placed


def main() -> None:
    parser = municipio_arg_parser(__doc__)
    parser.add_argument("--fonte-edificacoes", choices=["osm", "overture"], default="osm",
                        help="osm = buildings_raw.json; overture = overture_buildings.parquet (Google/Microsoft/OSM)")
    args = parser.parse_args()
    municipio = get_municipio(args.municipio)
    root = Path(__file__).resolve().parent.parent
    data_dir, out_dir = root / "data" / municipio.slug, root / "output" / municipio.slug
    out_dir.mkdir(parents=True, exist_ok=True)
    crs = municipio.epsg

    ways = gpd.read_file(data_dir / "highways_clipped.gpkg", layer="highways")
    ways = ways.explode(index_parts=False).reset_index(drop=True)
    ways = ways[(ways.geometry.geom_type == "LineString") & (ways.geometry.length > 1.0)].copy()
    ways["sidewalk_norm"] = ways["sidewalk"].apply(normalize_sidewalk)

    calcada_min = municipio.calcada_minima_m
    urban_area = load_urban_area(municipio, crs)
    print(f"Região: {municipio.regiao} | calçada mínima para vaga: {calcada_min} m")

    if args.fonte_edificacoes == "overture":
        buildings = load_overture_buildings(data_dir / "overture_buildings.parquet", crs)
    else:
        buildings = load_buildings(data_dir / "buildings_raw.json", crs)
    print(f"Edificações ({args.fonte_edificacoes}): {len(buildings)}")
    building_geoms = buildings.geometry.values
    building_tree = shapely.STRtree(building_geoms)

    pairs = find_divided_pairs(ways)
    ways["paired"] = ways.index.isin(pairs.keys())
    eligible = ways[ways["highway"].isin(GENERATION_CLASSES)]

    # --- alinhamento predial pela geometria das fachadas ----------------------
    def probe_all():
        probes, closest = {}, pd.Series(np.nan, index=ways.index)
        for idx, geom in ways.loc[eligible.index].geometry.items():
            pos, left, right = facade_probes(geom, building_tree, building_geoms)
            probes[idx] = (pos, left, right)
            side_medians = [np.nanmedian(d) for d in (left, right) if np.count_nonzero(~np.isnan(d)) >= FACADE_MIN_HITS]
            if side_medians:
                closest[idx] = min(side_medians)
        return probes, closest

    probes, _ = probe_all()
    uncertainty_raw, _ = registration_uncertainty(ways.loc[eligible.index], probes)

    # --- co-registro: desloca o OSM para o referencial das edificações ---------
    gx, gy, shift, _ = coregistration_field(ways.loc[eligible.index], probes)
    magnitude = np.linalg.norm(shift, axis=2)
    print(f"Co-registro: deslocamento por célula de {COREG_CELL_M:.0f} m — "
          f"mediana {np.median(magnitude[magnitude > 0]):.1f} m, p90 {np.percentile(magnitude[magnitude > 0], 90):.1f} m")
    ways["geometry"] = apply_shift(ways.geometry.values, gx, gy, shift)
    cells = [(x, y, *shift[j, i]) for j, y in enumerate(gy) for i, x in enumerate(gx) if magnitude[j, i] > 0]
    gpd.GeoDataFrame(
        {"dx_m": [c[2] for c in cells], "dy_m": [c[3] for c in cells]},
        geometry=gpd.points_from_xy([c[0] for c in cells], [c[1] for c in cells]), crs=crs,
    ).to_file(data_dir / "coregistro_osm_edificacoes.gpkg", layer="deslocamento", driver="GPKG")

    probes, facade_closest_side = probe_all()
    print(f"Vias com fachadas detectadas: {facade_closest_side.notna().sum()} de {len(eligible)}")
    asym_after, _ = registration_uncertainty(ways.loc[eligible.index], probes)
    print(f"Assimetria de fachadas (p{REGISTRATION_PERCENTILE}): antes {uncertainty_raw:.1f} m -> depois {asym_after:.1f} m "
          "(inclui recuos; não usada como margem)")
    faces_path = root / "data" / municipio.uf_sigla / f"{municipio.codigo_ibge}_faces_de_logradouros_2022.shp"
    if faces_path.exists():
        faces = gpd.read_file(faces_path).to_crs(crs)
        uncertainty, n_measured, n_axis = ibge_registration_uncertainty(ways.loc[eligible.index], faces)
        uncertainty_source = "faces IBGE 2022"
        print(f"Incerteza de registro OSM x IBGE (p{REGISTRATION_PERCENTILE}, {n_measured} vias, "
              f"{n_axis} pontos de eixo): {uncertainty:.1f} m")
    else:
        uncertainty, n_measured, uncertainty_source = DEFAULT_REGISTRATION_UNCERTAINTY_M, 0, "padrão (sem faces IBGE)"
        print(f"AVISO: faces do IBGE não encontradas em {faces_path}; margem padrão {uncertainty} m")

    roads = ways[ways["highway"].isin(DEFAULT_WIDTH_BY_CLASS)].copy()
    half_and_source = [
        carriageway_half_width(r, p, f, calcada_min)
        for r, p, f in zip(roads.to_dict("records"), roads["paired"], facade_closest_side.loc[roads.index])
    ]
    roads["half"] = [h for h, _ in half_and_source]
    roads["fonte_leito"] = [s for _, s in half_and_source]
    ways["half"] = roads["half"]
    # pior caso: o leito real pode estar deslocado até `uncertainty` para qualquer lado
    roads["half_worst"] = roads["half"] + uncertainty
    ways["half_worst"] = roads["half_worst"]
    print(f"Vias de veículos (leito): {len(roads)} | pares de pista dupla: {len(pairs) // 2}")
    print("Fonte da largura do leito:", roads["fonte_leito"].value_counts().to_dict())

    # --- camada temporária de exclusão ---------------------------------------
    margin = np.where(roads["highway"] == "service", SERVICE_SETBACK_M, CURB_SETBACK_M)
    leito = list(roads.geometry.buffer(roads["half_worst"].to_numpy() + margin))
    cycleways = ways[ways["highway"] == "cycleway"]
    ciclovias = list(cycleways.geometry.buffer(CYCLEWAY_HALF_WIDTH_M + CURB_SETBACK_M))
    esquinas = junction_disks(roads.assign(half=roads["half_worst"]))

    aux = data_dir / "auxiliary_raw.json"
    obstacles = []
    for flt, buf in [
        (lambda t: t.get("footway") == "crossing" or t.get("highway") == "crossing", BUFFER_CROSSING_M),
        (lambda t: t.get("amenity") == "parking_entrance" or t.get("service") == "driveway", BUFFER_DRIVEWAY_M),
        (lambda t: t.get("highway") == "bus_stop" or t.get("public_transport") == "platform", BUFFER_BUS_STOP_M),
        (lambda t: t.get("natural") == "tree", BUFFER_EXISTING_TREE_M),
    ]:
        osm_layer = apply_shift(load_osm_elements(aux, flt, crs).geometry.values, gx, gy, shift)
        obstacles += list(shapely.buffer(osm_layer, buf))

    predios = list(buildings.geometry.buffer(MIN_BUILDING_CLEARANCE_M))

    layers = {"leito_vias": leito, "ciclovias": ciclovias, "esquinas": esquinas,
              "obstaculos": obstacles, "edificacoes": predios}
    print("Camada de exclusão:", {k: len(v) for k, v in layers.items()})
    excl = gpd.GeoDataFrame(
        {"tipo": [k for k, v in layers.items() for _ in v]},
        geometry=[g for v in layers.values() for g in v], crs=crs,
    )
    excl.to_file(data_dir / "v2_exclusao.gpkg", layer="exclusao", driver="GPKG")
    tree = shapely.STRtree(excl.geometry.values)

    # --- candidatos de calçada ----------------------------------------------
    groups = []
    eligible = ways.loc[eligible.index]
    for idx, row in eligible.iterrows():
        if row["paired"]:
            inner = side_facing(row.geometry, ways.geometry.loc[pairs[idx]].centroid)
            sides = [s for s in ("left", "right") if s != inner]
        else:
            sides = sides_for(row["sidewalk_norm"])
        offset = row["half_worst"] + CURB_SETBACK_M + PIT_HALF_WIDTH_M
        for side in sides:
            try:
                line = row.geometry.offset_curve(offset if side == "left" else -offset)
            except Exception:  # noqa: BLE001 — degenerate geometry
                continue
            if line.is_empty:
                continue
            dists, pts = densify(line, SAMPLE_STEP_M)
            if not len(pts):
                continue
            # calçada estimada = alinhamento (fachadas) - meia-largura do leito
            probe_pos, left, right = probes[idx]
            facade = local_facade(shapely.line_locate_point(row.geometry, pts), probe_pos,
                                  left if side == "left" else right)
            groups.append({"idx": idx, "row": row, "side": side, "dists": dists, "pts": pts,
                           "calcada": facade - row["half_worst"]})

    all_pts = np.concatenate([g["pts"] for g in groups])
    all_calcada = np.concatenate([g["calcada"] for g in groups])
    print(f"Amostras de calçada (a cada {SAMPLE_STEP_M} m): {len(all_pts)}")
    outside_exclusion = allowed_mask(all_pts, tree)
    wide_enough = np.isnan(all_calcada) | (all_calcada >= calcada_min)
    in_urban = shapely.contains_xy(urban_area, shapely.get_x(all_pts), shapely.get_y(all_pts))
    all_ok = outside_exclusion & wide_enough & in_urban
    print(f"  em setor urbano: {in_urban.mean() * 100:.1f}% | "
          f"fora da camada de exclusão: {outside_exclusion.mean() * 100:.1f}% | "
          f"calçada estimada < {calcada_min} m: {(~wide_enough).mean() * 100:.1f}% | "
          f"sem fachada para estimar: {np.isnan(all_calcada).mean() * 100:.1f}% | "
          f"zona plantável final: {all_ok.mean() * 100:.1f}%")

    # distância ao prédio só para quem sobrou (para o porte)
    clear = np.full(len(all_pts), 20.0)
    if all_ok.any() and len(buildings):
        cand = gpd.GeoDataFrame(geometry=all_pts[all_ok], crs=crs)
        near = gpd.sjoin_nearest(cand, buildings[["geometry"]], max_distance=20.0, distance_col="d", how="left")
        clear[np.flatnonzero(all_ok)] = near.groupby(level=0)["d"].min().reindex(range(len(cand))).fillna(20.0).to_numpy()

    # --- candidatos de canteiro central --------------------------------------
    median_groups = []
    for a, b in {tuple(sorted(p)) for p in pairs.items()}:
        if ways.loc[a, "highway"] not in GENERATION_CLASSES:
            continue
        way_a, way_b = ways.geometry.loc[a], ways.geometry.loc[b]
        dists, pts = densify(way_a, SAMPLE_STEP_M)
        if not len(pts):
            continue
        near_b = shapely.line_interpolate_point(way_b, shapely.line_locate_point(way_b, pts))
        mids = shapely.points((shapely.get_x(pts) + shapely.get_x(near_b)) / 2,
                              (shapely.get_y(pts) + shapely.get_y(near_b)) / 2)
        width = shapely.distance(pts, near_b) - ways.loc[a, "half_worst"] - ways.loc[b, "half_worst"]
        median_groups.append({"idx": a, "row": ways.loc[a], "dists": dists, "pts": mids, "width": width})

    if median_groups:
        med_pts = np.concatenate([g["pts"] for g in median_groups])
        med_ok = allowed_mask(med_pts, tree) & shapely.contains_xy(
            urban_area, shapely.get_x(med_pts), shapely.get_y(med_pts))
    else:
        med_pts, med_ok = np.empty(0, dtype=object), np.empty(0, dtype=bool)

    # --- distribuição por variante -------------------------------------------
    report = {
        "municipio": municipio.slug,
        "parametros": {
            "incerteza_registro_m": round(uncertainty, 2), "vias_medidas_incerteza": n_measured,
            "fonte_incerteza": uncertainty_source,
            "regiao": municipio.regiao, "calcada_minima_m": calcada_min, "afastamento_meio_fio_m": CURB_SETBACK_M,
            "afastamento_esquina_m": CORNER_SETBACK_M, "espacamento_minimo_global_m": MIN_POINT_SEPARATION_M,
        },
        "fonte_leito": roads["fonte_leito"].value_counts().to_dict(),
        "versoes": {},
    }
    for variant, cfg in VARIANTS.items():
        records, pos = [], 0
        for g in groups:
            n = len(g["pts"])
            ok, cl, calc = all_ok[pos:pos + n], clear[pos:pos + n], all_calcada[pos:pos + n]
            pos += n
            row = g["row"]
            default = DEFAULT_PORTE_BY_CLASS.get(row["highway"], "medio")
            portes = [classify_porte(c, default) for c in cl]
            for i in greedy_walk(g["dists"], ok, lambda i: cfg["spacing"](portes[i])):
                records.append({
                    "osm_id": row["osm_id"], "street_name": row["name"], "highway_class": row["highway"],
                    "slot_type": "calcada", "side": g["side"], "porte": portes[i],
                    "clearance_m": round(float(cl[i]), 2), "leito_m": round(2 * row["half"], 1),
                    "calcada_m": None if np.isnan(calc[i]) else round(float(calc[i]), 1),
                    "geometry": g["pts"][i],
                })
        pos = 0
        for g in median_groups:
            n = len(g["pts"])
            ok = med_ok[pos:pos + n] & (g["width"] >= MIN_MEDIAN_WIDTH_M)
            pos += n
            w = g["width"]
            portes = ["pequeno" if x < PEQUENO_MAX_M else "medio" if x < MEDIO_MAX_M else "grande" for x in w]
            for i in greedy_walk(g["dists"], ok, lambda i: cfg["spacing"](portes[i])):
                records.append({
                    "osm_id": g["row"]["osm_id"], "street_name": g["row"]["name"],
                    "highway_class": g["row"]["highway"], "slot_type": "canteiro_central", "side": "median",
                    "porte": portes[i], "clearance_m": round(float(w[i]), 2), "leito_m": None, "calcada_m": None,
                    "geometry": g["pts"][i],
                })

        points = gpd.GeoDataFrame(records, geometry="geometry", crs=crs)
        before = len(points)
        points = enforce_min_separation(points, MIN_POINT_SEPARATION_M).reset_index(drop=True)

        # verificação: nenhum ponto pode estar na camada de exclusão
        inside = int(len(np.unique(tree.query(points.geometry.values, predicate="within")[0])))
        stem = f"vagas_plantio_v2_{variant}" + ("_overture" if args.fonte_edificacoes == "overture" else "")
        points.to_file(out_dir / f"{stem}.gpkg", layer="vagas_plantio", driver="GPKG")
        points.to_crs("EPSG:4326").to_file(out_dir / f"{stem}.geojson", driver="GeoJSON")
        report["versoes"][variant] = {
            "label": cfg["label"], "pontos": len(points), "removidos_dedup_6m": before - len(points),
            "pontos_na_exclusao": inside,
            "por_tipo": points["slot_type"].value_counts().to_dict(),
            "por_porte": points["porte"].value_counts().to_dict(),
        }
        print(f"\n[{variant}] {len(points)} pontos (dedup 6 m removeu {before - len(points)}) "
              f"| pontos dentro da exclusão: {inside}")
        print("  ", points["slot_type"].value_counts().to_dict(), points["porte"].value_counts().to_dict())

    report["parametros"]["fonte_edificacoes"] = args.fonte_edificacoes
    (out_dir / f"v2_relatorio_{args.fonte_edificacoes}.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
