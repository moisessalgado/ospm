"""OpenStreetMap data: obstacles, carriageway width estimates and divided roads."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString, Point

# Curb-to-curb width used when a road has no usable width=/lanes= tag.
# Deliberately wide: erring toward the sidewalk, never toward the street.
DEFAULT_WIDTH_BY_CLASS = {
    "motorway": 22.0, "motorway_link": 8.0, "trunk": 16.0, "trunk_link": 8.0,
    "primary": 14.0, "primary_link": 8.0, "secondary": 12.0, "secondary_link": 8.0,
    "tertiary": 10.0, "tertiary_link": 7.0, "unclassified": 9.0, "residential": 9.0,
    "living_street": 6.0, "service": 6.0, "track": 4.0, "busway": 7.0, "road": 9.0,
}
LANE_WIDTH_M = 3.5
LANE_EXTRA_M = 2.0  # gutters / parking lane
MIN_PLAUSIBLE_WIDTH_M = 3.0
MAX_PLAUSIBLE_WIDTH_FACTOR = 2.5  # width= above 2.5x the class default is ignored
DIVIDED_ROAD_FACTOR = 0.6  # each carriageway of a divided road mapped as its own way
CORNER_CLASSES = set(DEFAULT_WIDTH_BY_CLASS) - {"service", "track"}
MEDIAN_MAX_GAP_M = 45.0
PAIR_SEARCH_RADIUS_M = 50.0
BEARING_TOLERANCE_DEG = 35.0


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


def junction_disks(roads: gpd.GeoDataFrame, corner_setback_m: float, lateral_offset_m: float) -> list:
    """Corner exclusion disks at every street junction (incl. T-junctions on
    interior nodes). Radius keeps the pit axis CORNER_SETBACK_M past the
    crossing street's curb (lateral_offset_m = curb setback + half pit)."""
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
            r = float(np.hypot(h + corner_setback_m, h + lateral_offset_m))
            disks.append(Point(key).buffer(r, quad_segs=8))
    return disks
