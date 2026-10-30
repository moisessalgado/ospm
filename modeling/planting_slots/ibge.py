"""IBGE data (Censo 2022): urban census tracts and street faces (faces de logradouro)."""

from __future__ import annotations

import zipfile

import geopandas as gpd
import httpx
import shapely

from planting_slots.paths import MunicipalityFiles

URBAN_SITUATION_CODES = ["1", "2", "3"]  # high/low building density urban area, urban nucleus
IBGE_TRACTS_URL = (
    "https://geoftp.ibge.gov.br/organizacao_do_territorio/malhas_territoriais/"
    "malhas_de_setores_censitarios__divisoes_intramunicipais/censo_2022/setores/gpkg/UF/{uf}/{uf}_setores_CD2022.gpkg"
)
SUBDIVISIONS_URL = (
    "https://geoftp.ibge.gov.br/organizacao_do_territorio/malhas_territoriais/"
    "malhas_de_setores_censitarios__divisoes_intramunicipais/censo_2022/subdistritos/gpkg/UF/{uf}/{uf}_subdistritos_CD2022.gpkg"
)
STREET_FACES_URL = (
    "https://geoftp.ibge.gov.br/recortes_para_fins_estatisticos/malha_de_setores_censitarios/censo_2022/"
    "base_de_faces_de_logradouros_versao_2022_censo_demografico/shp/{uf}_faces_de_logradouros_2022_shp.zip"
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


def load_subdivisions(files: MunicipalityFiles, crs: str) -> gpd.GeoDataFrame:
    """The municipality's IBGE sub-districts (for the Distrito Federal, its
    administrative regions), downloaded from IBGE's public FTP when missing."""
    path = files.ibge_subdivisions
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        url = SUBDIVISIONS_URL.format(uf=files.municipality.state_abbr)
        print(f"Baixando subdistritos: {url}")
        with httpx.stream("GET", url, timeout=300) as resp, open(path, "wb") as fh:
            resp.raise_for_status()
            for chunk in resp.iter_bytes():
                fh.write(chunk)
    divisions = gpd.read_file(path, where=f"CD_MUN='{files.municipality.ibge_code}'").to_crs(crs)
    # a division in several parts comes as several rows with the same code
    return divisions.dissolve(by="CD_SUBDIST", as_index=False)


def load_street_faces(files: MunicipalityFiles, crs: str) -> gpd.GeoDataFrame:
    """Street faces of the municipality, from IBGE's public FTP (one zip per
    state, extracted next to the other states' faces) when missing."""
    path = files.ibge_street_faces
    if not path.exists():
        uf = files.municipality.state_abbr
        url = STREET_FACES_URL.format(uf=uf)
        print(f"Baixando faces de logradouro: {url}")
        archive = path.parent / f"{uf}_faces_de_logradouros_2022_shp.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        with httpx.stream("GET", url, timeout=900) as resp, open(archive, "wb") as fh:
            resp.raise_for_status()
            for chunk in resp.iter_bytes():
                fh.write(chunk)
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():  # the zip holds a <UF>/ folder; extract it under faces_logradouros_2022/
                target = (path.parent.parent / member).resolve()
                if path.parent.parent.resolve() not in target.parents:
                    raise SystemExit(f"Arquivo suspeito no zip de faces: {member}")
            zf.extractall(path.parent.parent)
        archive.unlink()
        if not path.exists():
            raise SystemExit(f"Faces de logradouro não encontradas em {path} após extrair {url}.")
    return gpd.read_file(path).to_crs(crs)
