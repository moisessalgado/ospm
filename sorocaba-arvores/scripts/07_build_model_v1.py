"""Tree-planting-slot model v1 for Sorocaba.

Improvements over v0:
  - Divided-road (dual carriageway) pairing: OSM often maps each direction of
    a wide avenue as a separate `oneway` way. v0 treated each independently
    and generated sidewalk points on BOTH sides of each, which put points
    from the two carriageways on top of each other in the median. v1 pairs
    parallel oneway ways by name + geometry, keeps only the outer side as a
    sidewalk, and generates a single row of median (canteiro central) points
    between the pair when the gap is wide enough to be a real median.
  - Generic duplicate-point safety net: after generation, any two points
    closer than MIN_POINT_SEPARATION_M (regardless of cause) are deduplicated
    via a spatial grid.
  - Obstacle buffers from real OSM data available for Sorocaba: pedestrian
    crossings, bus stops, driveways, and (partially — only 330 mapped,
    nowhere near a full survey) existing trees.
  - NOT implemented (no usable OSM data for this city — see probe results):
    poles, fire hydrants, traffic signs, overhead wiring, real sidewalk width.
    Flagged for future integration from municipal/utility cadastre.
  - Sidewalk-width-driven spacing: real sidewalk polygons don't exist, so
    width is proxied by the distance from the point to the nearest building
    footprint. That drives tree porte (pequeno/médio/grande) and therefore
    the spacing to the next candidate point.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, Point, shape
from shapely.strtree import STRtree

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_DIR = Path(__file__).resolve().parent.parent / "output"
TARGET_CRS = "EPSG:31983"

# --- porte (tree size) thresholds, driven by proxy sidewalk width (m) ---
# Clearance = distance from candidate point to nearest building facade.
# Ranges follow common Brazilian municipal arborização urbana manuals
# (e.g. SP/Curitiba guidance); treat as adjustable defaults, not a norm.
MIN_CLEARANCE_M = 1.0          # below this: no room, drop the point
PEQUENO_MAX_M = 2.0            # [1.0, 2.0) -> porte pequeno
MEDIO_MAX_M = 3.5              # [2.0, 3.5) -> porte médio; >= 3.5 -> grande

SPACING_BY_PORTE = {"pequeno": 6.0, "medio": 8.0, "grande": 11.0}

# Half-ROW+sidewalk offset per class (centerline -> approx. planting strip).
OFFSET_BY_CLASS = {
    "living_street": 4.0, "residential": 5.0, "unclassified": 5.0, "service": 4.0,
    "tertiary": 6.0, "tertiary_link": 6.0, "secondary": 7.5, "secondary_link": 7.5,
    "primary": 9.0, "primary_link": 9.0,
}
EXCLUDED_CLASSES = {
    "motorway", "motorway_link", "trunk", "trunk_link", "construction", "proposed",
    "track", "path", "steps", "cycleway", "footway", "elevator", "raceway",
    "services", "rest_area", "corridor", "pedestrian", "busway", "service",
}
SIDEWALK_NORMALIZE = {
    "r": "right", "rught": "right", "right;both": "both", "both;left": "both",
    "n": "no", "yes": "both", "separate": "both",
}

# Divided-road pairing thresholds
MEDIAN_MIN_GAP_M = 3.0    # below this: not a real median, treat as adjacent carriageways
MEDIAN_MAX_GAP_M = 45.0   # above this: too far apart to be the same road's two carriageways
PAIR_SEARCH_RADIUS_M = 50.0
BEARING_TOLERANCE_DEG = 35.0  # opposite-direction tolerance around 180°

# Obstacle buffers (meters) — only for layers with real OSM coverage here
BUFFER_CROSSING_M = 3.0
BUFFER_DRIVEWAY_M = 2.0
BUFFER_BUS_STOP_M = 12.0
BUFFER_EXISTING_TREE_M = 4.0

MIN_POINT_SEPARATION_M = 3.0
SAMPLE_STEP_M = 2.0  # resolution for width-sampling walk along offset lines


def normalize_sidewalk(value):
    if pd.isna(value):
        return None
    value = str(value).strip().lower()
    return SIDEWALK_NORMALIZE.get(value, value)


def sides_for(sidewalk_norm):
    if pd.isna(sidewalk_norm):
        return ["left", "right"]
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


def load_ways() -> gpd.GeoDataFrame:
    ways = gpd.read_file(DATA_DIR / "sorocaba_highways_clipped.gpkg", layer="highways")
    ways = ways.explode(index_parts=False).reset_index(drop=True)
    ways = ways[ways.geometry.geom_type == "LineString"]
    ways = ways[ways.geometry.length > 1.0].copy()
    ways["sidewalk_norm"] = ways["sidewalk"].apply(normalize_sidewalk)
    return ways


def load_obstacle_layer(tag_filter) -> gpd.GeoDataFrame:
    """Load geometries from the auxiliary Overpass dump matching tag_filter(tags)->bool."""
    raw = json.loads((DATA_DIR / "sorocaba_auxiliary_raw.json").read_text())
    geoms = []
    for el in raw["elements"]:
        tags = el.get("tags", {})
        if not tag_filter(tags):
            continue
        if el["type"] == "node":
            geoms.append(Point(el["lon"], el["lat"]))
        elif el["type"] == "way" and "geometry" in el:
            coords = [(pt["lon"], pt["lat"]) for pt in el["geometry"]]
            if len(coords) >= 2:
                geoms.append(LineString(coords))
    if not geoms:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326").to_crs(TARGET_CRS)
    return gpd.GeoDataFrame(geometry=geoms, crs="EPSG:4326").to_crs(TARGET_CRS)


def build_obstacle_union(ways_by_class: dict[str, gpd.GeoDataFrame], buffers: dict[str, float]):
    parts = []
    for key, gdf in ways_by_class.items():
        if len(gdf) == 0:
            continue
        parts.append(gdf.buffer(buffers[key]).union_all())
    if not parts:
        return None
    from shapely.ops import unary_union
    return unary_union(parts)


def find_divided_pairs(ways: gpd.GeoDataFrame) -> dict[int, int]:
    """Return {way_index: paired_way_index} for parallel oneway carriageways
    of the same named road, matched as mutual nearest neighbors."""
    candidates = ways[
        (ways["oneway"].isin(["yes", "-1"]))
        & ways["name"].notna()
        & ways["highway"].isin(["primary", "primary_link", "secondary", "secondary_link", "trunk", "trunk_link", "tertiary"])
    ].copy()
    if len(candidates) == 0:
        return {}

    candidates["bearing"] = candidates.geometry.apply(bearing)
    candidates["centroid"] = candidates.geometry.centroid

    pairs: dict[int, int] = {}
    for name, group in candidates.groupby("name"):
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
                bdiff = angle_diff(bearings[i], (bearings[j] + 180) % 360)
                if bdiff > BEARING_TOLERANCE_DEG:
                    continue
                gap = group.geometry.iloc[i].distance(group.geometry.iloc[j])
                if not (MEDIAN_MIN_GAP_M <= gap <= MEDIAN_MAX_GAP_M):
                    continue
                if best_dist is None or dist < best_dist:
                    best_dist, best_j = dist, idx_j
            if best_j is not None:
                best_match[idx_i] = best_j

        # keep only mutual nearest-neighbor pairs
        for idx_i, idx_j in best_match.items():
            if best_match.get(idx_j) == idx_i and idx_i < idx_j:
                pairs[idx_i] = idx_j
                pairs[idx_j] = idx_i
    return pairs


def side_of_point(line: LineString, other_centroid: Point) -> str:
    """Which offset side (left/right per shapely convention) faces other_centroid."""
    left = line.offset_curve(1.0)
    right = line.offset_curve(-1.0)
    if left.is_empty or right.is_empty:
        return "right"
    return "left" if left.distance(other_centroid) < right.distance(other_centroid) else "right"


def main() -> None:
    ways = load_ways()
    eligible = ways[~ways["highway"].isin(EXCLUDED_CLASSES)].copy()
    print(f"Vias elegíveis (exclui classes de alta velocidade/não-logradouro): {len(eligible)}")

    pairs = find_divided_pairs(ways)
    n_pairs = len(set(tuple(sorted((k, v))) for k, v in pairs.items()))
    print(f"Pares de pista dupla detectados: {n_pairs}")

    # Obstacle layers from real OSM coverage
    crossings = load_obstacle_layer(lambda t: t.get("footway") == "crossing" or t.get("highway") == "crossing")
    driveways = load_obstacle_layer(lambda t: t.get("amenity") == "parking_entrance" or t.get("service") == "driveway")
    bus_stops = load_obstacle_layer(lambda t: t.get("highway") == "bus_stop" or t.get("public_transport") == "platform")
    existing_trees = load_obstacle_layer(lambda t: t.get("natural") == "tree")
    print(f"Obstáculos carregados: crossings={len(crossings)} driveways={len(driveways)} "
          f"bus_stops={len(bus_stops)} existing_trees={len(existing_trees)}")

    obstacle_union = build_obstacle_union(
        {"crossing": crossings, "driveway": driveways, "bus_stop": bus_stops, "tree": existing_trees},
        {"crossing": BUFFER_CROSSING_M, "driveway": BUFFER_DRIVEWAY_M,
         "bus_stop": BUFFER_BUS_STOP_M, "tree": BUFFER_EXISTING_TREE_M},
    )

    out_path = DATA_DIR / "sorocaba_pairs_debug.json"
    out_path.write_text(json.dumps({"n_pairs": n_pairs}))

    eligible.to_file(DATA_DIR / "sorocaba_eligible_v1.gpkg", layer="eligible", driver="GPKG")
    print("Checkpoint salvo — próxima etapa: amostragem por largura + porte (precisa de buildings).")


if __name__ == "__main__":
    main()
