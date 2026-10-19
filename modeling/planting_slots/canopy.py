"""Tree-canopy cover around each slot, from one of two rasters (CANOPY_SOURCES).

The raster holds the canopy percentage per pixel (255 = no data; GFW 10 m
comes in classes of 10 %, GLAD 30 m is continuous). A slot's value is the maximum over every pixel the circle of
CANOPY_RADIUS_FRACTION x its spacing touches — a half-spacing radius, so
neighbouring slots' circles tile the street without overlapping.
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import rasterio

from planting_slots.allocation import slot_spacing_m

CANOPY_RADIUS_FRACTION = 0.5
CANOPY_NODATA = 255

# WGS 84 length of one degree, from the ellipsoid's radii of curvature
_A, _E2 = 6378137.0, 0.00669437999014


def _meters_per_degree(lat_deg: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(per degree of longitude, per degree of latitude) at the given latitudes."""
    phi = np.radians(lat_deg)
    w = np.sqrt(1.0 - _E2 * np.sin(phi) ** 2)
    return np.radians(1.0) * _A * np.cos(phi) / w, np.radians(1.0) * _A * (1.0 - _E2) / w ** 3


def canopy_radius_m(points: gpd.GeoDataFrame, spacing_id: str) -> np.ndarray:
    return np.array([CANOPY_RADIUS_FRACTION * slot_spacing_m(spacing_id, porte) for porte in points["porte"]])


def max_canopy_within(points: gpd.GeoDataFrame, radius_m: np.ndarray, raster_path) -> np.ndarray:
    """Maximum canopy % over the pixels each point's circle touches, as float
    (NaN: every touched pixel is no data, or outside the raster).

    A pixel counts when its rectangle is within radius_m of the point (the
    same "all touched" rule as rasterio.features, so a circle only grazing a
    pixel still sees it). Pixel sizes are converted to meters at each point's
    latitude, so the circle stays a circle in the raster's degrees grid."""
    with rasterio.open(raster_path) as src:
        band = src.read(1)
        transform, raster_crs = src.transform, src.crs
    if abs(transform.b) > 0 or abs(transform.d) > 0:
        raise ValueError("Raster rotacionado não suportado")
    lonlat = points.geometry.to_crs(raster_crs)
    lon, lat = lonlat.x.to_numpy(), lonlat.y.to_numpy()
    per_lon, per_lat = _meters_per_degree(lat)
    pixel_w_m, pixel_h_m = transform.a * per_lon, -transform.e * per_lat

    col_f, row_f = (lon - transform.c) / transform.a, (lat - transform.f) / transform.e
    col0, row0 = np.floor(col_f).astype(int), np.floor(row_f).astype(int)
    reach = int(np.ceil(radius_m.max() / min(pixel_w_m.min(), pixel_h_m.min()))) + 1
    rows, cols = band.shape

    best = np.full(len(points), -1, dtype=np.int32)
    for dr in range(-reach, reach + 1):
        for dc in range(-reach, reach + 1):
            r, c = row0 + dr, col0 + dc
            # distance from the point to the pixel's rectangle, in meters
            x_lo, x_hi = (c - col_f) * pixel_w_m, (c + 1 - col_f) * pixel_w_m
            y_lo, y_hi = (r - row_f) * pixel_h_m, (r + 1 - row_f) * pixel_h_m
            dx, dy = np.maximum.reduce([x_lo, -x_hi, np.zeros_like(x_lo)]), np.maximum.reduce([y_lo, -y_hi, np.zeros_like(y_lo)])
            touched = (dx * dx + dy * dy <= radius_m * radius_m) & (r >= 0) & (r < rows) & (c >= 0) & (c < cols)
            value = np.full(len(points), -1, dtype=np.int32)
            value[touched] = band[r[touched], c[touched]]
            best = np.maximum(best, np.where(value == CANOPY_NODATA, -1, value))
    return np.where(best >= 0, best, np.nan)


CANOPY_SOURCES = {
    "gfw_10m": {"label": "GFW Tropical Tree Cover (10 m, 2020)", "file": "cobertura_copa_gfw_10m.tif",
                "tile_url": "https://data-api.globalforestwatch.org/dataset/wri_tropical_tree_cover_extent/v20220922/"
                            "download/geotiff?grid=10/100000&tile_id={tile}&pixel_meaning=decile"},
    "glad_30m": {"label": "GLAD Global Tree Cover (30 m, 2010)", "file": "cobertura_copa_glad_30m.tif",
                 "tile_url": "https://gladxfer.umd.edu/Potapov/TCC_2010/treecover2010_{tile}.tif"},
}
BBOX_MARGIN_DEG = 0.005  # ~550 m: keeps the radius around slots at the municipal edge inside the clip


def gfw_tile_id(lon: float, lat: float) -> str:
    """GFW 10-degree tile name: its upper-left corner, e.g. 20S_050W."""
    top, left = int(np.ceil(lat / 10) * 10), int(np.floor(lon / 10) * 10)
    return f"{abs(top):02d}{'N' if top >= 0 else 'S'}_{abs(left):03d}{'E' if left >= 0 else 'W'}"


def download_canopy(bbox: dict, path, source: str, api_key: str | None = None) -> None:
    """Clip the source's canopy tile to the bbox (plus margin) by reading only
    that window over HTTP range requests — a whole tile is 250-600 MB. The
    GFW source needs its API key (it answers with a redirect to a signed URL)."""
    import httpx
    from rasterio.windows import from_bounds

    west, south = bbox["west"] - BBOX_MARGIN_DEG, bbox["south"] - BBOX_MARGIN_DEG
    east, north = bbox["east"] + BBOX_MARGIN_DEG, bbox["north"] + BBOX_MARGIN_DEG
    tiles = {gfw_tile_id(lon, lat) for lon in (west, east) for lat in (south, north)}
    if len(tiles) > 1:
        raise SystemExit(f"O município cruza mais de um tile de 10° ({sorted(tiles)}); mosaico ainda não implementado.")
    tile = tiles.pop()

    url = CANOPY_SOURCES[source]["tile_url"].format(tile=tile)
    if source == "gfw_10m":
        response = httpx.get(url, headers={"x-api-key": api_key}, follow_redirects=False)
        if response.status_code != 307:
            raise SystemExit(f"API do GFW respondeu {response.status_code} (chave inválida ou tile inexistente: {tile}).")
        url = response.headers["location"]
    with rasterio.open(f"/vsicurl/{url}") as src:
        window = from_bounds(west, south, east, north, src.transform).round_offsets().round_lengths()
        data = src.read(1, window=window)
        if (data == CANOPY_NODATA).all():
            raise SystemExit(f"O tile {tile} de {source} não tem dado de copa nesta região (todo o recorte é nodata); "
                             "tente a outra fonte (--fonte).")
        profile = src.profile | {"height": data.shape[0], "width": data.shape[1], "dtype": "uint8",
                                 "transform": src.window_transform(window), "nodata": CANOPY_NODATA,
                                 "tiled": True, "blockxsize": 256, "blockysize": 256, "compress": "deflate"}
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data.astype("uint8"), 1)
