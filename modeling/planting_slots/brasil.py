"""Layout and registry for the national batch (every Brazilian municipality).

Unlike the single-municipality pipeline (planting_slots/paths.py), nothing is
registered by hand: the municipalities, their boundaries and their 7-digit IBGE
codes come from IBGE's municipal mesh, and the metric CRS is derived from the
location.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import pyproj

from planting_slots.municipalities import STATE_ABBR_BY_IBGE_CODE
from planting_slots.paths import DATA_DIR, OUTPUT_DIR

# First digit of the IBGE UF code -> Geofabrik's Brazil sub-region file name.
REGION_BY_UF_DIGIT = {"1": "norte", "2": "nordeste", "3": "sudeste", "4": "sul", "5": "centro-oeste"}
REGIONS = tuple(REGION_BY_UF_DIGIT.values())

GEOFABRIK_URL = "https://download.geofabrik.de/south-america/brazil/{region}-latest.osm.pbf"
IBGE_MUNICIPALITIES_URL = (
    "https://geoftp.ibge.gov.br/organizacao_do_territorio/malhas_territoriais/malhas_municipais/"
    "municipio_2022/Brasil/BR/BR_Municipios_2022.zip"
)

# SIRGAS 2000 / UTM zones that cover Brazil (EPSG registry): 17S..25S and 18N..22N.
_UTM_SOUTH = {zone: 31977 + (zone - 17) for zone in range(17, 26)}
_UTM_NORTH = {zone: 31972 + (zone - 18) for zone in range(18, 23)}


def utm_epsg(lon: float, lat: float) -> str:
    """SIRGAS 2000 / UTM EPSG code for a point (zone of its longitude,
    hemisphere of its latitude)."""
    zone = int((lon + 180) // 6) + 1
    table = _UTM_SOUTH if lat < 0 else _UTM_NORTH
    if zone not in table:
        raise ValueError(f"Fuso UTM {zone} fora do Brasil (lon={lon}, lat={lat})")
    return f"EPSG:{table[zone]}"


def utm_label(epsg: str) -> str:
    return pyproj.CRS.from_user_input(epsg).name


def uf_of(cd_mun: str) -> str:
    return STATE_ABBR_BY_IBGE_CODE[cd_mun[:2]]


def region_of(cd_mun: str) -> str:
    return REGION_BY_UF_DIGIT[cd_mun[0]]


@dataclass(frozen=True)
class Layout:
    """Every directory and file of the batch. `data` holds inputs (downloads
    and their derived caches), `out` holds the product."""
    data: Path = DATA_DIR / "brasil"
    out: Path = OUTPUT_DIR / "brasil"

    # --- inputs -------------------------------------------------------------
    def pbf(self, region: str) -> Path:
        return self.data / "geofabrik" / f"{region}-latest.osm.pbf"

    def osm_roads(self, region: str) -> Path:
        return self.data / "osm" / region / "vias.parquet"

    def osm_obstacles(self, region: str) -> Path:
        return self.data / "osm" / region / "obstaculos.parquet"

    @property
    def municipalities_zip(self) -> Path:
        return self.data / "ibge" / "municipios_2022" / "BR_Municipios_2022.zip"

    @property
    def municipalities(self) -> Path:
        return self.data / "ibge" / "municipios_2022" / "municipios.parquet"

    def tracts_gpkg(self, uf: str) -> Path:
        return self.data / "ibge" / "setores_censitarios_2022" / f"{uf}_setores_CD2022.gpkg"

    def tracts(self, cd_mun: str) -> Path:
        return self.data / "ibge" / "setores_2022_por_municipio" / uf_of(cd_mun) / f"{cd_mun}.parquet"

    def faces_dir(self, uf: str) -> Path:
        return self.data / "ibge" / "faces_logradouros_2022" / uf

    def faces(self, cd_mun: str) -> Path:
        return self.faces_dir(uf_of(cd_mun)) / f"{cd_mun}_faces_de_logradouros_2022.shp"

    def canopy_tile(self, source: str, tile: str) -> Path:
        return self.data / "copa" / source / f"{tile}.tif"

    # --- product ------------------------------------------------------------
    def points(self, cd_mun: str) -> Path:
        return self.out / "pontos" / uf_of(cd_mun) / f"{cd_mun}.parquet"

    def status(self, cd_mun: str) -> Path:
        return self.out / "status" / f"{cd_mun}.json"

    @property
    def summary(self) -> Path:
        return self.out / "resumo.csv"

    @property
    def metadata(self) -> Path:
        return self.out / "metadados.json"


def load_municipalities(layout: Layout, cd_mun: list[str] | None = None) -> gpd.GeoDataFrame:
    """The municipal table (cd_mun, nm_mun, uf, regiao, geometry in EPSG:4326),
    optionally only the given codes (read with a parquet filter, so a worker
    does not load all 5,570 boundaries)."""
    if not layout.municipalities.exists():
        raise SystemExit(f"Malha municipal ausente ({layout.municipalities}); rode antes o subcomando 'baixar'.")
    filters = [("cd_mun", "in", list(cd_mun))] if cd_mun else None
    return gpd.read_parquet(layout.municipalities, filters=filters)
