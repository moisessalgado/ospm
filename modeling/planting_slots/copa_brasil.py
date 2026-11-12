"""Tree-canopy cover for the national batch: whole 10-degree tiles cached once,
then read by window per municipality.

Same attribute as scripts/03b_add_canopy_cover.py (the maximum canopy % over
the pixels within half the spacing of the point — see planting_slots/canopy.py),
with two additions the national scale needs: municipalities that straddle tiles
(each tile is evaluated and the maxima combined), and a per-point fallback
from the first source to the next where the first has no data (GFW has none in
Santa Catarina and the extreme south of Rio Grande do Sul). The source that
produced each value is recorded per point.
"""

from __future__ import annotations

import os

import geopandas as gpd
import httpx
import numpy as np
import pandas as pd

from planting_slots.brasil import Layout
from planting_slots.canopy import BBOX_MARGIN_DEG, CANOPY_SOURCES, canopy_radius_m, gfw_tile_id, max_canopy_within
from planting_slots.downloads import HEADERS, DownloadNotFound, download_file

DEFAULT_SOURCES = ("gfw_10m", "glad_30m")
API_KEY_ENV = "GFW_API_KEY"


def tiles_for_bounds(west: float, south: float, east: float, north: float) -> set[str]:
    """10-degree tile ids touched by a bbox (plus the clip margin canopy.py uses)."""
    return {gfw_tile_id(lon, lat)
            for lon in (west - BBOX_MARGIN_DEG, east + BBOX_MARGIN_DEG)
            for lat in (south - BBOX_MARGIN_DEG, north + BBOX_MARGIN_DEG)}


def tiles_for_municipalities(municipalities: gpd.GeoDataFrame) -> set[str]:
    tiles: set[str] = set()
    for west, south, east, north in municipalities.geometry.bounds.itertuples(index=False):
        tiles |= tiles_for_bounds(west, south, east, north)
    return tiles


def require_api_key(sources) -> str | None:
    key = os.environ.get(API_KEY_ENV)
    if "gfw_10m" in sources and not key:
        raise SystemExit(f"A fonte gfw_10m exige a chave da API do Global Forest Watch na variável {API_KEY_ENV}. "
                         "Exporte-a (ela não fica no repositório) ou use --fontes-copa glad_30m.")
    return key


def _signed_gfw_url(tile: str, api_key: str) -> str:
    url = CANOPY_SOURCES["gfw_10m"]["tile_url"].format(tile=tile)
    response = httpx.get(url, headers={**HEADERS, "x-api-key": api_key}, follow_redirects=False, timeout=120)
    if response.status_code == 404:
        raise DownloadNotFound(f"GFW sem o tile {tile}")
    if response.status_code != 307:
        raise SystemExit(f"API do GFW respondeu {response.status_code} para o tile {tile} (chave inválida?).")
    return response.headers["location"]


def download_tile(layout: Layout, source: str, tile: str, api_key: str | None = None) -> bool:
    """Cache one tile of `source`. Returns False when the source has no such
    tile (a `.sem_dado` marker is left so it is not asked again)."""
    dest = layout.canopy_tile(source, tile)
    marker = dest.with_suffix(".sem_dado")
    if dest.exists():
        return True
    if marker.exists():
        return False
    try:
        for attempt in range(3):  # the signed GFW URL expires: ask for a fresh one on each try
            url = _signed_gfw_url(tile, api_key) if source == "gfw_10m" else \
                CANOPY_SOURCES[source]["tile_url"].format(tile=tile)
            try:
                download_file(url, dest, timeout=600)
                return True
            except RuntimeError as exc:
                if attempt == 2 or isinstance(exc, DownloadNotFound):
                    raise
    except DownloadNotFound:
        dest.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return False
    return False


def download_tiles(layout: Layout, tiles: set[str], sources=DEFAULT_SOURCES) -> None:
    api_key = require_api_key(sources)
    for source in sources:
        for tile in sorted(tiles):
            print(f"[copa] {source} {tile}")
            if not download_tile(layout, source, tile, api_key):
                print(f"  (sem tile {tile} em {source})")


def canopy_for_points(points: gpd.GeoDataFrame, layout: Layout, sources=DEFAULT_SOURCES,
                      spacing_id: str = "10m") -> pd.DataFrame:
    """cobertura_copa_pct / raio_copa_m / cobertura_copa_fonte for each point
    (aligned to points.index). Tiles must already be cached (`baixar`)."""
    n = len(points)
    radius = canopy_radius_m(points, spacing_id) if n else np.array([])
    value = np.full(n, np.nan)
    source_of = np.full(n, None, dtype=object)
    if n:
        west, south, east, north = points.geometry.to_crs("EPSG:4326").total_bounds
        tiles = tiles_for_bounds(west, south, east, north)
        for source in sources:
            pending = np.flatnonzero(np.isnan(value))
            if not len(pending):
                break
            best = np.full(len(pending), np.nan)
            sub = points.iloc[pending]
            for tile in sorted(tiles):
                path = layout.canopy_tile(source, tile)
                if not path.exists():
                    if path.with_suffix(".sem_dado").exists():
                        continue
                    raise FileNotFoundError(f"Tile de copa ausente: {path}. Rode antes o subcomando 'baixar'.")
                best = np.fmax(best, max_canopy_within(sub, radius[pending], path))
            got = ~np.isnan(best)
            value[pending[got]] = best[got]
            source_of[pending[got]] = source
    return pd.DataFrame({
        "cobertura_copa_pct": pd.array(value, dtype="Int16"),
        "raio_copa_m": radius.astype("float32") if n else pd.Series(dtype="float32"),
        "cobertura_copa_fonte": pd.Series(source_of, dtype="string"),
    }, index=points.index)
