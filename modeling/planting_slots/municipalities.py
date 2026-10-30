"""Registry of municipalities supported by the modeling pipeline.

To add a municipality, append an entry to MUNICIPALITIES with its 7-digit IBGE
code and the SIRGAS 2000 / UTM EPSG code for its longitude (see epsg.io).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

REGION_BY_STATE = {
    **dict.fromkeys(["Acre", "Amapá", "Amazonas", "Pará", "Rondônia", "Roraima", "Tocantins"], "Norte"),
    **dict.fromkeys(["Alagoas", "Bahia", "Ceará", "Maranhão", "Paraíba", "Pernambuco", "Piauí",
                     "Rio Grande do Norte", "Sergipe"], "Nordeste"),
    **dict.fromkeys(["Distrito Federal", "Goiás", "Mato Grosso", "Mato Grosso do Sul"], "Centro-Oeste"),
    **dict.fromkeys(["Espírito Santo", "Minas Gerais", "Rio de Janeiro", "São Paulo"], "Sudeste"),
    **dict.fromkeys(["Paraná", "Rio Grande do Sul", "Santa Catarina"], "Sul"),
}

STATE_ABBR_BY_IBGE_CODE = {
    "11": "RO", "12": "AC", "13": "AM", "14": "RR", "15": "PA", "16": "AP", "17": "TO",
    "21": "MA", "22": "PI", "23": "CE", "24": "RN", "25": "PB", "26": "PE", "27": "AL", "28": "SE", "29": "BA",
    "31": "MG", "32": "ES", "33": "RJ", "35": "SP", "41": "PR", "42": "SC", "43": "RS",
    "50": "MS", "51": "MT", "52": "GO", "53": "DF",
}


@dataclass(frozen=True)
class Subdivisions:
    """A municipality published to the webmap as several entries — one per IBGE
    intramunicipal division (e.g. the Distrito Federal's administrative regions).
    Every division gets the `face_ibge` method; only `full_divisions` get all the
    methods, since the OSM-only ones walk every road and, for a territory as large
    as the Distrito Federal, reach millions of slots (too heavy for the browser
    and for the repository: GitHub rejects files over 100 MB). The processing
    itself still runs on the whole municipality."""
    dataset: str  # IBGE folder under censo_2022/: "subdistritos", "distritos" or "bairros"
    name_column: str
    code_column: str
    full_divisions: tuple[str, ...]  # names of the divisions that get every method
    focus_division: str  # the division that carries `focus` (the webmap's default view)


@dataclass(frozen=True)
class Municipality:
    slug: str
    name: str
    state: str
    ibge_code: str
    epsg: str  # SIRGAS 2000 / UTM zone for the municipality's longitude
    # Where the webmap opens when this is the default municipality: (lon, lat, zoom).
    # Without it, the map centers on the municipality's bounding box.
    focus: tuple[float, float, float] | None = None
    nominatim_query: dict | None = None  # overrides the name/state search for the boundary
    subdivisions: Subdivisions | None = None

    @property
    def state_abbr(self) -> str:
        return STATE_ABBR_BY_IBGE_CODE[self.ibge_code[:2]]

    @property
    def region(self) -> str:
        return REGION_BY_STATE[self.state]



MUNICIPALITIES: dict[str, Municipality] = {
    "sorocaba": Municipality(slug="sorocaba", name="Sorocaba", state="São Paulo", ibge_code="3552205", epsg="EPSG:31983"),
    "suzano": Municipality(slug="suzano", name="Suzano", state="São Paulo", ibge_code="3552502", epsg="EPSG:31983"),
    "tremembe": Municipality(slug="tremembe", name="Tremembé", state="São Paulo", ibge_code="3554805", epsg="EPSG:31983"),
    "distrito_federal": Municipality(
        slug="distrito_federal", name="Distrito Federal", state="Distrito Federal", ibge_code="5300108",
        epsg="EPSG:31983", focus=(-47.8825, -15.7942, 13.0), nominatim_query={"q": "Distrito Federal, Brasil"},
        subdivisions=Subdivisions(dataset="subdistritos", name_column="NM_SUBDIST", code_column="CD_SUBDIST",
                                  full_divisions=("Plano Piloto",), focus_division="Plano Piloto")),
    "florianopolis": Municipality(slug="florianopolis", name="Florianópolis", state="Santa Catarina",
                                   ibge_code="4205407", epsg="EPSG:31982"),
}


def get_municipality(slug: str) -> Municipality:
    try:
        return MUNICIPALITIES[slug]
    except KeyError:
        available = ", ".join(sorted(MUNICIPALITIES))
        raise SystemExit(f"Município '{slug}' não está no registro (planting_slots/municipalities.py). "
                         f"Disponíveis: {available}")


def municipality_arg_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--municipio", required=True, choices=sorted(MUNICIPALITIES),
                        help="Município a processar")
    return parser
