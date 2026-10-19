"""Data layout: every file the pipeline reads or writes is named here.

File names are in pt-BR (they are what users browse); source files keep the
names they are published with, for provenance.
"""

from __future__ import annotations

from pathlib import Path

from planting_slots.municipalities import Municipality

MODELING_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = MODELING_DIR / "data"
OUTPUT_DIR = MODELING_DIR / "output"
WEBMAP_DATA_DIR = MODELING_DIR.parent / "webmap" / "data"

OSM_ROADS_LAYER = "vias"


class MunicipalityFiles:
    def __init__(self, municipality: Municipality):
        self.municipality = municipality
        self.data_dir = DATA_DIR / municipality.slug
        self.output_dir = OUTPUT_DIR / municipality.slug

        self.boundary = self.data_dir / "limite_municipio.geojson"
        self.bbox = self.data_dir / "bbox.json"
        self.osm_roads_raw = self.data_dir / "vias_osm.json"
        self.osm_roads = self.data_dir / "vias_osm.gpkg"
        self.osm_obstacles = self.data_dir / "obstaculos_osm.json"
        self.buildings = self.data_dir / "edificacoes_overture.parquet"
        self.canopy = self.data_dir / "cobertura_copa_gfw_10m.tif"

        abbr, code = municipality.state_abbr, municipality.ibge_code
        self.ibge_tracts = DATA_DIR / "ibge" / "setores_censitarios_2022" / f"{abbr}_setores_CD2022.gpkg"
        self.ibge_street_faces = DATA_DIR / "ibge" / "faces_logradouros_2022" / abbr / f"{code}_faces_de_logradouros_2022.shp"

        self.allocation_report = self.output_dir / "relatorio_alocacao.json"

    def slots(self, variant: str, extension: str) -> Path:
        return self.output_dir / f"vagas_plantio_{variant}.{extension}"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
