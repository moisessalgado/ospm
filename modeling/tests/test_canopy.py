import geopandas as gpd
import numpy as np
from shapely.geometry import Point

from planting_slots.canopy import max_canopy_within
from planting_slots.copa_brasil import canopy_for_points, tiles_for_bounds
from helpers import TILE, make_inputs, write_canopy_tile


def points_at(coords, porte="medio"):
    return gpd.GeoDataFrame({"porte": [porte] * len(coords)}, geometry=[Point(c) for c in coords], crs="EPSG:4326")


def test_window_read_matches_value_and_outside_is_nan(tmp_path):
    layout = make_inputs(tmp_path, canopy=40)
    tile = layout.canopy_tile("glad_30m", TILE)
    pts = points_at([(-45.50, -22.90), (-45.30, -22.90)])  # second one lies outside the raster
    value = max_canopy_within(pts, np.array([5.0, 5.0]), tile)
    assert value[0] == 40 and np.isnan(value[1])


def test_max_over_circle_not_just_center_pixel(tmp_path):
    layout = make_inputs(tmp_path, canopy=None)
    path = layout.canopy_tile("glad_30m", TILE)
    write_canopy_tile(layout, "glad_30m", 10)
    import rasterio
    with rasterio.open(path, "r+") as ds:
        r, c = ds.index(-45.5003, -22.9)  # a single dense pixel ~33 m west of the point's pixel
        ds.write(np.array([[90]], dtype="uint8"), 1, window=((r, r + 1), (c, c + 1)))
    near = points_at([(-45.5003, -22.9)])
    assert max_canopy_within(near, np.array([5.0]), path)[0] == 90
    far = points_at([(-45.4990, -22.9)])
    assert max_canopy_within(far, np.array([5.0]), path)[0] == 10


def test_fallback_to_second_source_and_source_recorded(tmp_path):
    layout = make_inputs(tmp_path, canopy=55)
    # GFW: no data over the western half; GLAD fills everything
    write_canopy_tile(layout, "gfw_10m", 20, nodata_left_half=True)
    pts = points_at([(-45.51, -22.90), (-45.49, -22.90)])  # west, east
    out = canopy_for_points(pts, layout, ("gfw_10m", "glad_30m"))
    assert list(out["cobertura_copa_fonte"]) == ["glad_30m", "gfw_10m"]
    assert list(out["cobertura_copa_pct"]) == [55, 20]
    assert (out["raio_copa_m"] == 5.0).all()


def test_all_sources_without_data_gives_null(tmp_path):
    layout = make_inputs(tmp_path, canopy=255)
    out = canopy_for_points(points_at([(-45.5, -22.9)]), layout, ("glad_30m",))
    assert out["cobertura_copa_pct"].isna().all() and out["cobertura_copa_fonte"].isna().all()


def test_tiles_for_bounds_straddling_a_tile_edge():
    assert tiles_for_bounds(-45.5, -22.9, -45.4, -22.8) == {"20S_050W"}
    assert tiles_for_bounds(-50.1, -22.9, -49.9, -22.8) == {"20S_060W", "20S_050W"}
