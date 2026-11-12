"""IBGE inputs for the national batch, prepared once so each municipality's
worker reads a small file instead of scanning a whole state:

  * the municipal mesh (BR_Municipios_2022) -> one table with the boundaries;
  * the Censo 2022 census tracts of each UF -> one parquet per municipality
    with its urban tracts and their district/sub-district/neighbourhood;
  * the street faces of each UF (already one shapefile per municipality).
"""

from __future__ import annotations

import shutil
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import geopandas as gpd
import pandas as pd

from planting_slots.brasil import (
    IBGE_MUNICIPALITIES_URL, REGION_BY_UF_DIGIT, Layout, uf_of,
)
from planting_slots.downloads import download_file
from planting_slots.ibge import IBGE_TRACTS_URL, STREET_FACES_URL, URBAN_SITUATION_CODES, extract_zip_safely

TRACT_COLUMNS = {"CD_SETOR": "cd_setor", "CD_SIT": "cd_sit", "CD_BAIRRO": "cd_bairro", "NM_BAIRRO": "nm_bairro",
                 "CD_DIST": "cd_dist", "NM_DIST": "nm_dist", "CD_SUBDIST": "cd_subdist", "NM_SUBDIST": "nm_subdist"}


def prepare_municipalities(layout: Layout) -> Path:
    """Download the municipal mesh and keep the columns the batch uses."""
    if layout.municipalities.exists():
        return layout.municipalities
    download_file(IBGE_MUNICIPALITIES_URL, layout.municipalities_zip, timeout=600)
    mesh = gpd.read_file(f"zip://{layout.municipalities_zip}").to_crs("EPSG:4326")
    table = gpd.GeoDataFrame({
        "cd_mun": mesh["CD_MUN"].astype(str).str.zfill(7),
        "nm_mun": mesh["NM_MUN"],
        "uf": mesh["SIGLA_UF"],
        "regiao": mesh["CD_MUN"].astype(str).str[0].map(REGION_BY_UF_DIGIT),
    }, geometry=mesh.geometry.make_valid().values, crs="EPSG:4326").sort_values("cd_mun")
    tmp = layout.municipalities.with_name(layout.municipalities.name + ".tmp")
    table.to_parquet(tmp, compression="zstd", row_group_size=100, write_covering_bbox=True, index=False)
    tmp.replace(layout.municipalities)
    return layout.municipalities


def split_tracts(layout: Layout, uf: str) -> int:
    """Download the UF's tract mesh and write each municipality's urban tracts
    to its own parquet. Returns the number of municipalities written."""
    done = layout.tracts_gpkg(uf).with_suffix(".particionado")
    out_dir = layout.data / "ibge" / "setores_2022_por_municipio" / uf
    if done.exists():
        return len(list(out_dir.glob("*.parquet")))
    url = IBGE_TRACTS_URL.format(uf=uf)
    download_file(url, layout.tracts_gpkg(uf), timeout=600)
    tracts = gpd.read_file(layout.tracts_gpkg(uf), columns=[*TRACT_COLUMNS, "CD_MUN"])
    tracts = tracts[tracts["CD_SIT"].astype(str).isin(URBAN_SITUATION_CODES)].to_crs("EPSG:4326")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for cd_mun, group in tracts.groupby("CD_MUN"):
        frame = group.rename(columns=TRACT_COLUMNS).drop(columns="CD_MUN")
        for col in ("cd_setor", "cd_sit", "cd_bairro", "cd_dist", "cd_subdist"):
            frame[col] = frame[col].astype("string")
        tmp = out_dir / f"{cd_mun}.parquet.tmp"
        frame.to_parquet(tmp, compression="zstd", index=False)
        tmp.replace(out_dir / f"{cd_mun}.parquet")
        written += 1
    done.touch()
    return written


def download_faces(layout: Layout, uf: str) -> None:
    """Download and extract the UF's street faces (a zip with a <UF>/ folder of per-municipality shapefiles)."""
    directory = layout.faces_dir(uf)
    if directory.with_suffix(".pronto").exists():
        return
    archive = directory.parent / f"{uf}_faces_de_logradouros_2022_shp.zip"
    download_file(STREET_FACES_URL.format(uf=uf), archive, timeout=900)
    extract_zip_safely(archive, directory.parent)
    archive.unlink()
    if not any(directory.glob("*.shp")):
        shutil.rmtree(directory, ignore_errors=True)
        raise SystemExit(f"Nenhuma face de logradouro encontrada em {directory} após extrair {archive.name}.")
    directory.with_suffix(".pronto").touch()


def prepare_uf(args: tuple[Layout, str]) -> tuple[str, int]:
    layout, uf = args
    download_faces(layout, uf)
    return uf, split_tracts(layout, uf)


def prepare_ibge(layout: Layout, ufs: list[str], workers: int = 4) -> None:
    prepare_municipalities(layout)
    if workers <= 1 or len(ufs) == 1:
        results = [prepare_uf((layout, uf)) for uf in ufs]
    else:
        with ProcessPoolExecutor(min(workers, len(ufs))) as pool:
            results = list(pool.map(prepare_uf, [(layout, uf) for uf in ufs]))
    for uf, n in results:
        print(f"[ibge] {uf}: setores urbanos de {n} municípios, faces prontas")


def ufs_of(cd_muns) -> list[str]:
    return sorted({uf_of(c) for c in cd_muns})


def load_tracts(layout: Layout, cd_mun: str) -> gpd.GeoDataFrame:
    path = layout.tracts(cd_mun)
    if not path.exists():
        return gpd.GeoDataFrame({c: pd.Series(dtype="string") for c in TRACT_COLUMNS.values()},
                                geometry=gpd.GeoSeries([], crs="EPSG:4326"))
    return gpd.read_parquet(path)


def load_faces(layout: Layout, cd_mun: str, crs: str) -> gpd.GeoDataFrame | None:
    path = layout.faces(cd_mun)
    return gpd.read_file(path).to_crs(crs) if path.exists() else None
