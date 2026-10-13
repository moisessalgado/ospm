"""IBGE data (Censo 2022): urban census tracts and street faces (faces de logradouro)."""

from __future__ import annotations

import geopandas as gpd
import httpx
import shapely

from planting_slots.paths import MunicipalityFiles

URBAN_SITUATION_CODES = ["1", "2", "3"]  # high/low building density urban area, urban nucleus
IBGE_TRACTS_URL = (
    "https://geoftp.ibge.gov.br/organizacao_do_territorio/malhas_territoriais/"
    "malhas_de_setores_censitarios__divisoes_intramunicipais/censo_2022/setores/gpkg/UF/{uf}/{uf}_setores_CD2022.gpkg"
)
STREET_FACES_PAGE = (
    "https://www.ibge.gov.br/geociencias/organizacao-do-territorio/malhas-territoriais/"
    "28971-base-de-faces-de-logradouros-do-brasil.html"
)


def load_urban_area(files: MunicipalityFiles, crs: str):
    """Union of the municipality's urban census tracts, downloaded from IBGE's
    public FTP when missing. Prepared for fast point-in-polygon tests."""
    path = files.ibge_tracts
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        url = IBGE_TRACTS_URL.format(uf=files.municipality.state_abbr)
        print(f"Baixando setores censitários: {url}")
        with httpx.stream("GET", url, timeout=900) as resp, open(path, "wb") as fh:
            resp.raise_for_status()
            for chunk in resp.iter_bytes():
                fh.write(chunk)
    tracts = gpd.read_file(path, where=f"CD_MUN='{files.municipality.ibge_code}'")
    urban = tracts[tracts["CD_SIT"].isin(URBAN_SITUATION_CODES)].to_crs(crs)
    area = urban.geometry.union_all()
    shapely.prepare(area)
    return area


def load_street_faces(files: MunicipalityFiles, crs: str) -> gpd.GeoDataFrame:
    """Street faces of the municipality. IBGE serves them behind a bot check,
    so they must be downloaded manually (per state) from STREET_FACES_PAGE."""
    path = files.ibge_street_faces
    if not path.exists():
        raise SystemExit(
            f"Faces de logradouro não encontradas em {path}.\n"
            f"Baixe a 'Base de Faces de Logradouros por UF/Municípios (shp)' em {STREET_FACES_PAGE}\n"
            f"e descompacte os arquivos da UF em {path.parent}/."
        )
    return gpd.read_file(path).to_crs(crs)
