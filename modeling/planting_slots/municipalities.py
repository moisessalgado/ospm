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

# Minimum sidewalk width for a planting slot, per the MMA "Coletânea Brasileira
# de Arborização Urbana" (2026) of each region. Sudeste, Norte and Nordeste give
# no value; the minimum derived from rules common to all volumes is used:
# 0.5 m from the curb + 0.5 m minimum pit + 1.2 m clear path (NBR 9050).
MIN_SIDEWALK_WIDTH_BY_REGION_M = {
    "Sul": 3.0,
    "Centro-Oeste": 1.5,
    "Sudeste": 2.2,
    "Norte": 2.2,
    "Nordeste": 2.2,
}

STATE_ABBR_BY_IBGE_CODE = {
    "11": "RO", "12": "AC", "13": "AM", "14": "RR", "15": "PA", "16": "AP", "17": "TO",
    "21": "MA", "22": "PI", "23": "CE", "24": "RN", "25": "PB", "26": "PE", "27": "AL", "28": "SE", "29": "BA",
    "31": "MG", "32": "ES", "33": "RJ", "35": "SP", "41": "PR", "42": "SC", "43": "RS",
    "50": "MS", "51": "MT", "52": "GO", "53": "DF",
}


@dataclass(frozen=True)
class Municipality:
    slug: str
    name: str
    state: str
    ibge_code: str
    epsg: str  # SIRGAS 2000 / UTM zone for the municipality's longitude

    @property
    def state_abbr(self) -> str:
        return STATE_ABBR_BY_IBGE_CODE[self.ibge_code[:2]]

    @property
    def region(self) -> str:
        return REGION_BY_STATE[self.state]

    @property
    def min_sidewalk_width_m(self) -> float:
        return MIN_SIDEWALK_WIDTH_BY_REGION_M[self.region]


MUNICIPALITIES: dict[str, Municipality] = {
    "sorocaba": Municipality(slug="sorocaba", name="Sorocaba", state="São Paulo", ibge_code="3552205", epsg="EPSG:31983"),
    "suzano": Municipality(slug="suzano", name="Suzano", state="São Paulo", ibge_code="3552502", epsg="EPSG:31983"),
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
