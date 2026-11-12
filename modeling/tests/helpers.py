"""Synthetic inputs for the batch tests: a tiny PBF, municipal table, tracts and canopy tiles."""

import geopandas as gpd
import numpy as np
import osmium
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from planting_slots.brasil import Layout

CD_MUN = "3554805"  # Tremembé (SP -> region sudeste, UTM 23S)
BOUNDARY = box(-45.51, -22.96, -45.49, -22.89)
URBAN = box(-45.504, -22.903, -45.496, -22.897)
TILE = "20S_050W"  # tile of the area


def write_pbf(path):
    """Two residential streets meeting at a junction inside the urban box, a rural road outside
    it, a bus stop and a footway (which is not a road for this method)."""
    M = osmium.osm.mutable
    w = osmium.SimpleWriter(str(path))
    nodes = {1: (-45.502, -22.900), 2: (-45.498, -22.900),   # street A, west-east
             3: (-45.500, -22.902), 4: (-45.500, -22.898),   # street B, south-north
             9: (-45.500, -22.900),                          # where A and B meet (a real junction)
             5: (-45.505, -22.950), 6: (-45.495, -22.950),   # rural road, outside the urban box
             7: (-45.5015, -22.90005), 8: (-45.5016, -22.90005)}  # bus stop ~5 m north of street A
    for node_id, loc in nodes.items():
        tags = {"highway": "bus_stop"} if node_id == 7 else {}
        w.add_node(M.Node(id=node_id, location=loc, tags=tags))
    w.add_way(M.Way(id=10, nodes=[1, 9], tags={"highway": "residential", "name": "Rua A"}))
    w.add_way(M.Way(id=14, nodes=[9, 2], tags={"highway": "residential", "name": "Rua A"}))
    w.add_way(M.Way(id=11, nodes=[3, 9], tags={"highway": "residential", "name": "Rua B"}))
    w.add_way(M.Way(id=15, nodes=[9, 4], tags={"highway": "residential", "name": "Rua B"}))
    w.add_way(M.Way(id=12, nodes=[5, 6], tags={"highway": "unclassified"}))
    w.add_way(M.Way(id=13, nodes=[7, 8], tags={"highway": "footway"}))
    w.close()


def write_municipality_table(layout: Layout, cd_mun=CD_MUN):
    table = gpd.GeoDataFrame({"cd_mun": [cd_mun], "nm_mun": ["Município de Teste"], "uf": ["SP"],
                              "regiao": ["sudeste"]}, geometry=[BOUNDARY], crs="EPSG:4326")
    layout.municipalities.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(layout.municipalities, index=False)


def write_tracts(layout: Layout, cd_mun=CD_MUN):
    frame = gpd.GeoDataFrame({"cd_setor": ["355480505000001P"], "cd_sit": ["1"], "cd_bairro": ["3554805001"],
                              "nm_bairro": ["Centro"], "cd_dist": ["355480505"], "nm_dist": ["Tremembé"],
                              "cd_subdist": [None], "nm_subdist": [None]}, geometry=[URBAN], crs="EPSG:4326")
    path = layout.tracts(cd_mun)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)


def write_canopy_tile(layout: Layout, source: str, value: int, nodata_left_half=False):
    """A 0.0003-degree-pixel raster over the area, filled with `value` (255 = no data)."""
    path = layout.canopy_tile(source, TILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    west, north, res = -45.52, -22.88, 0.0003
    width, height = int(0.04 / res), int(0.09 / res)
    data = np.full((height, width), value, dtype="uint8")
    if nodata_left_half:
        data[:, : width // 2] = 255
    with rasterio.open(path, "w", driver="GTiff", width=width, height=height, count=1, dtype="uint8",
                       crs="EPSG:4326", transform=from_origin(west, north, res, res), nodata=255) as dst:
        dst.write(data, 1)
    return path


def make_inputs(tmp_path, canopy=40):
    layout = Layout(data=tmp_path / "dados", out=tmp_path / "saida")
    pbf = layout.pbf("sudeste")
    pbf.parent.mkdir(parents=True)
    write_pbf(pbf)
    write_municipality_table(layout)
    write_tracts(layout)
    if canopy is not None:
        write_canopy_tile(layout, "glad_30m", canopy)
    return layout
