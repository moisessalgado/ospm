"""OpenStreetMap inputs from a Geofabrik regional extract (.osm.pbf).

One streaming pass per region (pyosmium) writes two spatially sorted
GeoParquet files — the roads and the obstacle features the sidewalk method
needs — so each municipality can later read just its own bounding box. It
replaces the per-municipality Overpass queries of scripts/01_download_osm.py
with the same selection:

  * roads: every `highway` way whose class has a default carriageway width
    (planting_slots.osm.DEFAULT_WIDTH_BY_CLASS), with the tags of ROAD_TAGS;
  * obstacles: every node matching one of sidewalk.OBSTACLES, plus the ways
    the Overpass query fetched (platforms, footway crossings, driveways).
    The obstacle name comes from those same OBSTACLES filters, so the rule
    lives in one place.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import osmium
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from planting_slots.downloads import write_atomic
from planting_slots.osm import DEFAULT_WIDTH_BY_CLASS
from planting_slots.sidewalk import OBSTACLES

ROAD_TAGS = ["highway", "name", "oneway", "lanes", "sidewalk", "width", "surface", "junction"]
# Tag keys that can make a feature interesting; everything else is skipped inside libosmium.
KEYS = ["highway", "power", "public_transport", "amenity", "service", "man_made", "natural", "manhole",
        "transformer", "footway"]
OBSTACLE_TAGS = ["highway", "footway", "amenity", "service", "public_transport", "power", "transformer",
                 "man_made", "manhole", "natural"]
CHUNK_ROWS = 250_000
ROW_GROUP_ROWS = 20_000


def obstacle_names(tags: dict) -> list[str]:
    """Names of every OBSTACLES rule the tags satisfy."""
    return [name for name, (match, _) in OBSTACLES.items() if match(tags)]


def way_is_obstacle(tags: dict) -> bool:
    """Which ways the Overpass query used to fetch as obstacles (nodes are
    fetched whenever a rule matches, ways only in these three cases)."""
    return (tags.get("public_transport") == "platform"
            or (tags.get("highway") == "footway" and tags.get("footway") == "crossing")
            or tags.get("service") == "driveway")


def _flush(writer: pq.ParquetWriter | None, path: Path, rows: dict[str, list]) -> pq.ParquetWriter | None:
    if not rows["wkb"]:
        return writer
    table = pa.table({k: pa.array(v, type=pa.binary() if k == "wkb" else (pa.int64() if k == "osm_id" else pa.string()))
                      for k, v in rows.items()})
    if writer is None:
        writer = pq.ParquetWriter(path, table.schema, compression="zstd")
    writer.write_table(table)
    for v in rows.values():
        v.clear()
    return writer


def _sorted_geoparquet(raw: Path, dest: Path) -> int:
    """Raw (unsorted) rows -> Hilbert-ordered GeoParquet with a bbox covering,
    written atomically. Returns the number of features."""
    if not raw.exists():
        frame = gpd.GeoDataFrame({"osm_id": pd.Series(dtype="int64")}, geometry=gpd.GeoSeries([], crs="EPSG:4326"))
    else:
        table = pq.read_table(raw)
        frame = gpd.GeoDataFrame(table.drop(["wkb"]).to_pandas(),
                                 geometry=gpd.GeoSeries.from_wkb(table.column("wkb").to_numpy(zero_copy_only=False),
                                                                 crs="EPSG:4326"))
    if len(frame):
        frame = frame.iloc[np.argsort(frame.geometry.hilbert_distance().to_numpy(), kind="stable")]
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    frame.to_parquet(tmp, compression="zstd", row_group_size=ROW_GROUP_ROWS, write_covering_bbox=True, index=False)
    tmp.replace(dest)
    return len(frame)


def extract_region(pbf: Path, roads_out: Path, obstacles_out: Path) -> dict:
    """One pass over `pbf`; writes `roads_out` and `obstacles_out`. Returns counts."""
    work = roads_out.parent
    work.mkdir(parents=True, exist_ok=True)
    raw_roads, raw_obstacles = work / "vias.bruto.parquet", work / "obstaculos.bruto.parquet"
    for stale in (raw_roads, raw_obstacles):
        stale.unlink(missing_ok=True)

    roads = {"osm_id": [], **{t: [] for t in ROAD_TAGS}, "wkb": []}
    obstacles = {"osm_id": [], **{t: [] for t in OBSTACLE_TAGS}, "obstaculo": [], "wkb": []}
    road_writer = obstacle_writer = None
    n_roads = n_obstacles = n_bad = 0

    processor = (osmium.FileProcessor(str(pbf), osmium.osm.NODE | osmium.osm.WAY)
                 .with_locations().with_filter(osmium.filter.KeyFilter(*KEYS)))
    wkb = osmium.geom.WKBFactory()
    for obj in processor:
        tags = {t.k: t.v for t in obj.tags}
        if obj.is_node():
            names = obstacle_names(tags)
            if not names:
                continue
            geometry = bytes.fromhex(wkb.create_point(obj))
        else:
            is_road = tags.get("highway") in DEFAULT_WIDTH_BY_CLASS
            is_obstacle = way_is_obstacle(tags)
            if not (is_road or is_obstacle):
                continue
            try:
                geometry = bytes.fromhex(wkb.create_linestring(obj))
            except (osmium.InvalidLocationError, RuntimeError, ValueError):
                n_bad += 1  # way with a node outside the extract, or degenerate
                continue
            if is_road:
                roads["osm_id"].append(obj.id)
                for t in ROAD_TAGS:
                    roads[t].append(tags.get(t))
                roads["wkb"].append(geometry)
                n_roads += 1
                if len(roads["wkb"]) >= CHUNK_ROWS:
                    road_writer = _flush(road_writer, raw_roads, roads)
            names = obstacle_names(tags) if is_obstacle else []
            if not names:
                continue
        for name in names:
            obstacles["osm_id"].append(obj.id)
            for t in OBSTACLE_TAGS:
                obstacles[t].append(tags.get(t))
            obstacles["obstaculo"].append(name)
            obstacles["wkb"].append(geometry)
            n_obstacles += 1
        if len(obstacles["wkb"]) >= CHUNK_ROWS:
            obstacle_writer = _flush(obstacle_writer, raw_obstacles, obstacles)

    road_writer = _flush(road_writer, raw_roads, roads)
    obstacle_writer = _flush(obstacle_writer, raw_obstacles, obstacles)
    for w in (road_writer, obstacle_writer):
        if w is not None:
            w.close()
    _sorted_geoparquet(raw_roads, roads_out)
    _sorted_geoparquet(raw_obstacles, obstacles_out)
    for stale in (raw_roads, raw_obstacles):
        stale.unlink(missing_ok=True)
    counts = {"vias": n_roads, "obstaculos": n_obstacles, "ways_invalidos": n_bad}
    write_atomic(work / "extracao.json", json.dumps({"pbf": pbf.name, **counts}))
    return counts


def load_roads(path: Path, bbox: tuple[float, float, float, float], boundary) -> gpd.GeoDataFrame:
    """The region's roads inside a municipality: bbox read, then clipped to its
    boundary (EPSG:4326), as scripts/01_download_osm.py's clip_roads did."""
    roads = gpd.read_parquet(path, bbox=bbox)
    if roads.empty:
        return roads
    return gpd.clip(roads, gpd.GeoDataFrame(geometry=[boundary], crs="EPSG:4326"))


def load_obstacles(path: Path, bbox: tuple[float, float, float, float]) -> gpd.GeoDataFrame:
    return gpd.read_parquet(path, bbox=bbox)


def obstacles_by_name(obstacles: gpd.GeoDataFrame, crs: str) -> dict[str, tuple]:
    """{name: (geometries in crs, radii by porte)} as sidewalk.allocate_from_roads expects."""
    projected = obstacles.to_crs(crs)
    out = {}
    for name, (_, radii) in OBSTACLES.items():
        sel = projected[projected["obstaculo"] == name] if len(projected) else projected
        out[name] = (np.asarray(sel.geometry.values, dtype=object) if len(sel) else np.array([], dtype=object), radii)
    return out



def download_region(layout, region: str) -> Path:
    """Download (once) the Geofabrik extract of a Brazilian region and verify
    it against the publisher's MD5. An existing file is kept as is, so a run
    stays reproducible; delete it to take a newer snapshot."""
    from planting_slots.brasil import GEOFABRIK_URL
    from planting_slots.downloads import download_file, file_md5

    pbf = layout.pbf(region)
    if pbf.exists():
        return pbf
    url = GEOFABRIK_URL.format(region=region)
    md5_file = pbf.with_name(pbf.name + ".md5")
    md5_file.unlink(missing_ok=True)
    download_file(url + ".md5", md5_file)
    expected = md5_file.read_text().split()[0]
    download_file(url, pbf, timeout=600)
    if file_md5(pbf) != expected:
        pbf.unlink()
        raise RuntimeError(f"MD5 de {pbf.name} não confere com o publicado pelo Geofabrik; baixe novamente.")
    return pbf
