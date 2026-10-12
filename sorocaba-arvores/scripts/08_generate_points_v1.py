"""v1 point generation: divided-road pairing + median points + building-width
porte classification + obstacle buffers + generic dedup.

Run after 06 (buildings) and uses the pairing logic from 07 inline (kept
self-contained here so this script can be iterated on its own).
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_DIR = Path(__file__).resolve().parent.parent / "output"
TARGET_CRS = "EPSG:31983"

MIN_CLEARANCE_M = 1.0
PEQUENO_MAX_M = 2.0
MEDIO_MAX_M = 3.5
SPACING_BY_PORTE = {"pequeno": 6.0, "medio": 8.0, "grande": 11.0}
PORTE_RANK = {"pequeno": 0, "medio": 1, "grande": 2}

# Default porte by road class, used as a CEILING (building clearance can only
# shrink it, never raise it). Rationale: in Brazilian residential streets the
# house is often set back from the lot line (recuo frontal) — distance to the
# building then reflects yard depth, not the public sidewalk strip, which has
# a legally fixed width regardless of how far back the house sits. Only a
# building genuinely close to the curb (commercial/center frontage, no recuo)
# should be allowed to shrink the slot's porte.
DEFAULT_PORTE_BY_CLASS = {
    "living_street": "pequeno", "residential": "medio", "unclassified": "medio",
    "tertiary": "medio", "tertiary_link": "medio",
    "secondary": "grande", "secondary_link": "grande",
    "primary": "grande", "primary_link": "grande", "trunk": "grande",
}

OFFSET_BY_CLASS = {
    "living_street": 4.0, "residential": 5.0, "unclassified": 5.0,
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

MEDIAN_MIN_GAP_M = 3.0
MEDIAN_MAX_GAP_M = 45.0
PAIR_SEARCH_RADIUS_M = 50.0
BEARING_TOLERANCE_DEG = 35.0

BUFFER_CROSSING_M = 3.0
BUFFER_DRIVEWAY_M = 2.0
BUFFER_BUS_STOP_M = 12.0
BUFFER_EXISTING_TREE_M = 4.0

MIN_POINT_SEPARATION_M = 3.0
SAMPLE_STEP_M = 2.0
BUILDING_SEARCH_RADIUS_M = 20.0


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


def load_buildings() -> gpd.GeoDataFrame:
    raw = json.loads((DATA_DIR / "sorocaba_buildings_raw.json").read_text())
    geoms = []
    for el in raw["elements"]:
        if el["type"] != "way" or "geometry" not in el:
            continue
        coords = [(pt["lon"], pt["lat"]) for pt in el["geometry"]]
        if len(coords) >= 4:
            geoms.append(LineString(coords))  # boundary only; distance-to-boundary is what we need
    gdf = gpd.GeoDataFrame(geometry=geoms, crs="EPSG:4326").to_crs(TARGET_CRS)
    return gdf


def find_divided_pairs(ways: gpd.GeoDataFrame) -> dict[int, int]:
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
                gap = group.geometry.loc[idx_i].distance(group.geometry.loc[idx_j])
                if not (MEDIAN_MIN_GAP_M <= gap <= MEDIAN_MAX_GAP_M):
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


def side_facing(line: LineString, other_centroid: Point) -> str:
    left = line.offset_curve(1.0)
    right = line.offset_curve(-1.0)
    if left.is_empty or right.is_empty:
        return "right"
    return "left" if left.distance(other_centroid) < right.distance(other_centroid) else "right"


def densify(line: LineString, step: float) -> list[tuple[float, Point]]:
    if line.geom_type == "MultiLineString":
        line = max(line.geoms, key=lambda g: g.length)
    if not isinstance(line, LineString) or line.length < 1.0:
        return []
    n = max(int(line.length // step), 1)
    return [(d, line.interpolate(d)) for d in np.arange(0, line.length, step)] or [(0.0, line.interpolate(0))]


def classify_porte(clearance: float, class_default: str) -> str | None:
    if clearance < MIN_CLEARANCE_M:
        return None
    if clearance < PEQUENO_MAX_M:
        building_based = "pequeno"
    elif clearance < MEDIO_MAX_M:
        building_based = "medio"
    else:
        building_based = "grande"
    # clearance can only shrink the class-based default, never inflate it
    return min([building_based, class_default], key=lambda p: PORTE_RANK[p])


def greedy_walk(samples: list[tuple[float, Point]], clearances: list[float], obstacle_union, class_default: str) -> list[dict]:
    """Walk along ordered (distance, point) samples; place a point when clear
    of obstacles, then skip ahead by the spacing implied by local porte."""
    placed = []
    next_allowed = -np.inf
    for (d, pt), clearance in zip(samples, clearances):
        if d < next_allowed:
            continue
        porte = classify_porte(clearance, class_default)
        if porte is None:
            continue
        if obstacle_union is not None and pt.within(obstacle_union):
            continue
        placed.append({"dist": d, "point": pt, "porte": porte, "clearance_m": clearance})
        next_allowed = d + SPACING_BY_PORTE[porte]
    return placed


def main() -> None:
    ways = load_ways()
    eligible = ways[~ways["highway"].isin(EXCLUDED_CLASSES)].copy()
    print(f"Vias elegíveis: {len(eligible)}")

    pairs = find_divided_pairs(ways)
    paired_idx = set(pairs.keys())
    n_pairs = len({tuple(sorted((k, v))) for k, v in pairs.items()})
    print(f"Pares pista dupla: {n_pairs} ({len(paired_idx)} ways pareadas)")

    crossings = load_obstacle_layer(lambda t: t.get("footway") == "crossing" or t.get("highway") == "crossing")
    driveways = load_obstacle_layer(lambda t: t.get("amenity") == "parking_entrance" or t.get("service") == "driveway")
    bus_stops = load_obstacle_layer(lambda t: t.get("highway") == "bus_stop" or t.get("public_transport") == "platform")
    existing_trees = load_obstacle_layer(lambda t: t.get("natural") == "tree")
    buildings = load_buildings()
    print(f"Obstáculos: crossings={len(crossings)} driveways={len(driveways)} "
          f"bus_stops={len(bus_stops)} trees={len(existing_trees)} buildings={len(buildings)}")

    obstacle_parts = []
    for gdf, buf in [(crossings, BUFFER_CROSSING_M), (driveways, BUFFER_DRIVEWAY_M),
                      (bus_stops, BUFFER_BUS_STOP_M), (existing_trees, BUFFER_EXISTING_TREE_M)]:
        if len(gdf):
            obstacle_parts.append(gdf.buffer(buf).union_all())
    obstacle_union = unary_union(obstacle_parts) if obstacle_parts else None

    def clearance_to_buildings_batch(points: list[Point]) -> list[float]:
        """One vectorized nearest-building query for ALL points at once."""
        if len(points) == 0 or len(buildings) == 0:
            return [BUILDING_SEARCH_RADIUS_M] * len(points)
        pts_gdf = gpd.GeoDataFrame({"_pid": range(len(points))}, geometry=points, crs=TARGET_CRS)
        joined = gpd.sjoin_nearest(
            pts_gdf, buildings[["geometry"]], max_distance=BUILDING_SEARCH_RADIUS_M,
            distance_col="dist", how="left",
        )
        joined = joined.groupby("_pid")["dist"].min()
        return joined.reindex(range(len(points))).fillna(BUILDING_SEARCH_RADIUS_M).tolist()

    # --- sidewalk sides: collect all samples first, one batched building query ---
    groups: list[dict] = []  # one entry per (way, side) with its samples
    flat_points: list[Point] = []
    for idx, row in eligible.iterrows():
        offset = OFFSET_BY_CLASS.get(row["highway"], 5.0)
        if idx in paired_idx:
            partner_idx = pairs[idx]
            partner_centroid = ways.geometry.loc[partner_idx].centroid
            inner_side = side_facing(row.geometry, partner_centroid)
            sides = [s for s in ["left", "right"] if s != inner_side]
        else:
            sides = sides_for(row["sidewalk_norm"])

        for side in sides:
            sign = 1 if side == "left" else -1
            try:
                offset_line = row.geometry.offset_curve(sign * offset)
            except Exception:
                continue
            samples = densify(offset_line, SAMPLE_STEP_M)
            if not samples:
                continue
            start = len(flat_points)
            flat_points.extend(pt for _, pt in samples)
            groups.append({
                "idx": idx, "row": row, "side": side, "samples": samples,
                "slice": slice(start, start + len(samples)),
            })

    print(f"Amostras (resolução {SAMPLE_STEP_M}m) para consulta de prédios: {len(flat_points)}")
    all_clearances = clearance_to_buildings_batch(flat_points)

    records = []
    for g in groups:
        clearances = all_clearances[g["slice"]]
        row = g["row"]
        class_default = DEFAULT_PORTE_BY_CLASS.get(row["highway"], "medio")
        placed = greedy_walk(g["samples"], clearances, obstacle_union, class_default)
        for p in placed:
            records.append({
                "osm_id": row["osm_id"], "street_name": row["name"],
                "highway_class": row["highway"], "slot_type": "calcada",
                "side": g["side"], "porte": p["porte"], "clearance_m": round(p["clearance_m"], 2),
                "paired_road": g["idx"] in paired_idx,
                "geometry": p["point"],
            })

    # --- median (canteiro central) points for paired roads ---
    seen_pairs = set()
    for idx, partner_idx in pairs.items():
        key = tuple(sorted((idx, partner_idx)))
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        way_a = ways.geometry.loc[idx]
        way_b = ways.geometry.loc[partner_idx]
        samples_a = densify(way_a, SAMPLE_STEP_M)
        median_samples = []
        for d, pt in samples_a:
            nearest_b = way_b.interpolate(way_b.project(pt))
            gap = pt.distance(nearest_b)
            if MEDIAN_MIN_GAP_M <= gap <= MEDIAN_MAX_GAP_M:
                mid = Point((pt.x + nearest_b.x) / 2, (pt.y + nearest_b.y) / 2)
                median_samples.append((d, mid, gap))
        if not median_samples:
            continue
        # porte for a median: driven by the gap width itself (how much room
        # the median has), not building clearance.
        placed = []
        next_allowed = -np.inf
        for d, pt, gap in median_samples:
            if d < next_allowed:
                continue
            porte = "pequeno" if gap < 6 else "medio" if gap < 10 else "grande"
            if obstacle_union is not None and pt.within(obstacle_union):
                continue
            placed.append({"dist": d, "point": pt, "porte": porte, "gap_m": gap})
            next_allowed = d + SPACING_BY_PORTE[porte]
        row_a = ways.loc[idx]
        for p in placed:
            records.append({
                "osm_id": idx, "street_name": row_a["name"],
                "highway_class": row_a["highway"], "slot_type": "canteiro_central",
                "side": "median", "porte": p["porte"], "clearance_m": round(p["gap_m"], 2),
                "paired_road": True,
                "geometry": p["point"],
            })

    points = gpd.GeoDataFrame(records, geometry="geometry", crs=TARGET_CRS)
    print(f"\nPontos antes do dedup global: {len(points)}")

    # --- generic dedup: snap to a grid of MIN_POINT_SEPARATION_M and keep first per cell ---
    points["_gx"] = (points.geometry.x // MIN_POINT_SEPARATION_M).astype(int)
    points["_gy"] = (points.geometry.y // MIN_POINT_SEPARATION_M).astype(int)
    points = points.drop_duplicates(subset=["_gx", "_gy"]).drop(columns=["_gx", "_gy"])
    print(f"Pontos após dedup global (grid {MIN_POINT_SEPARATION_M}m): {len(points)}")

    out_gpkg = OUT_DIR / "sorocaba_vagas_plantio_v1.gpkg"
    points.to_file(out_gpkg, layer="vagas_plantio", driver="GPKG")
    points.to_crs("EPSG:4326").to_file(OUT_DIR / "sorocaba_vagas_plantio_v1.geojson", driver="GeoJSON")
    print(f"Salvo -> {out_gpkg}")

    print("\n--- Por tipo de vaga ---")
    print(points["slot_type"].value_counts())
    print("\n--- Por porte ---")
    print(points["porte"].value_counts())
    print("\n--- Por classe de via ---")
    print(points["highway_class"].value_counts())


if __name__ == "__main__":
    main()
