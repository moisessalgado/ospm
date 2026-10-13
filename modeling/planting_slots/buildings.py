"""Building footprints (Overture Maps) and perpendicular probes to the facades."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
from shapely.geometry import LineString

FACADE_PROBE_STEP_M = 5.0
FACADE_PROBE_LENGTH_M = 40.0
FACADE_WINDOW_M = 15.0
FACADE_PERCENTILE = 25
FACADE_MIN_HITS = 2


def download_overture_buildings(bbox: dict, path: Path) -> None:
    """Overture Maps buildings (Google Open Buildings + Microsoft + OSM) for a
    bbox, via the official overturemaps CLI."""
    import subprocess
    import sys
    path.parent.mkdir(parents=True, exist_ok=True)
    extent = f"{bbox['west']},{bbox['south']},{bbox['east']},{bbox['north']}"
    cli = Path(sys.executable).with_name("overturemaps")
    subprocess.run([str(cli), "download", f"--bbox={extent}", "-f", "geoparquet", "--type=building", "-o", str(path)],
                   check=True)


def load_overture_buildings(path: Path, crs: str) -> gpd.GeoDataFrame:
    gdf = gpd.read_parquet(path, columns=["geometry"]).set_crs("EPSG:4326", allow_override=True)
    gdf = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].explode(index_parts=False)
    return gdf.to_crs(crs).reset_index(drop=True)


def probe_frames(line: LineString, pos: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Centerline coordinates and left unit normals at the probe positions."""
    c = shapely.get_coordinates(shapely.line_interpolate_point(line, pos))
    ahead = shapely.get_coordinates(shapely.line_interpolate_point(line, np.minimum(pos + 0.5, line.length)))
    behind = shapely.get_coordinates(shapely.line_interpolate_point(line, np.maximum(pos - 0.5, 0)))
    t = ahead - behind
    t /= np.maximum(np.linalg.norm(t, axis=1, keepdims=True), 1e-9)
    return c, np.column_stack([-t[:, 1], t[:, 0]])


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
