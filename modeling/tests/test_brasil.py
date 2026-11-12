import pyproj
import pytest

from planting_slots.brasil import Layout, region_of, uf_of, utm_epsg


@pytest.mark.parametrize("lon, lat, name", [
    (-45.55, -22.95, "zone 23S"),   # Tremembé
    (-48.55, -27.60, "zone 22S"),   # Florianópolis
    (-47.90, -15.80, "zone 23S"),   # Distrito Federal
    (-60.67, 2.82, "zone 20N"),     # Boa Vista (northern hemisphere)
    (-51.07, 0.03, "zone 22N"),     # Macapá
    (-73.50, -7.60, "zone 18S"),    # western Acre
    (-34.90, -8.05, "zone 25S"),    # Recife
])
def test_utm_epsg_is_sirgas2000_zone(lon, lat, name):
    crs = pyproj.CRS.from_user_input(utm_epsg(lon, lat))
    assert crs.name == f"SIRGAS 2000 / UTM {name}"


def test_utm_outside_brazil():
    with pytest.raises(ValueError):
        utm_epsg(10.0, 50.0)


def test_region_and_uf_from_ibge_code():
    assert region_of("3554805") == "sudeste" and uf_of("3554805") == "SP"
    assert region_of("1400100") == "norte" and uf_of("1400100") == "RR"
    assert region_of("5300108") == "centro-oeste" and uf_of("5300108") == "DF"
    assert region_of("4205407") == "sul" and region_of("2927408") == "nordeste"


def test_layout_paths(tmp_path):
    layout = Layout(data=tmp_path / "d", out=tmp_path / "o")
    assert layout.points("3554805") == tmp_path / "o" / "pontos" / "SP" / "3554805.parquet"
    assert layout.faces("3554805").name == "3554805_faces_de_logradouros_2022.shp"
