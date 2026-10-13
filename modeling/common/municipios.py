"""Registro de municípios suportados pelo pipeline de modelagem.

Para adicionar um novo município:
  1. Descubra o EPSG da zona UTM SIRGAS 2000 correta (ex.: via epsg.io,
     buscando pela longitude do centro do município).
  2. Adicione uma entrada em MUNICIPIOS abaixo, com o código IBGE de 7 dígitos.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass


REGIAO_POR_UF = {
    **dict.fromkeys(["Acre", "Amapá", "Amazonas", "Pará", "Rondônia", "Roraima", "Tocantins"], "Norte"),
    **dict.fromkeys(["Alagoas", "Bahia", "Ceará", "Maranhão", "Paraíba", "Pernambuco", "Piauí",
                     "Rio Grande do Norte", "Sergipe"], "Nordeste"),
    **dict.fromkeys(["Distrito Federal", "Goiás", "Mato Grosso", "Mato Grosso do Sul"], "Centro-Oeste"),
    **dict.fromkeys(["Espírito Santo", "Minas Gerais", "Rio de Janeiro", "São Paulo"], "Sudeste"),
    **dict.fromkeys(["Paraná", "Rio Grande do Sul", "Santa Catarina"], "Sul"),
}

# Largura mínima de calçada para alocar uma vaga, conforme a Coletânea Brasileira
# de Arborização Urbana (MMA, 2026) de cada região. Sudeste, Norte e Nordeste
# não trazem um valor; usa-se o mínimo derivado das regras comuns às coletâneas:
# 0,5 m do meio-fio + 0,5 m de área livre mínima + 1,2 m de faixa livre (NBR 9050).
CALCADA_MINIMA_POR_REGIAO_M = {
    "Sul": 3.0,
    "Centro-Oeste": 1.5,
    "Sudeste": 2.2,
    "Norte": 2.2,
    "Nordeste": 2.2,
}


SIGLA_POR_CODIGO_UF = {
    "11": "RO", "12": "AC", "13": "AM", "14": "RR", "15": "PA", "16": "AP", "17": "TO",
    "21": "MA", "22": "PI", "23": "CE", "24": "RN", "25": "PB", "26": "PE", "27": "AL", "28": "SE", "29": "BA",
    "31": "MG", "32": "ES", "33": "RJ", "35": "SP", "41": "PR", "42": "SC", "43": "RS",
    "50": "MS", "51": "MT", "52": "GO", "53": "DF",
}


@dataclass(frozen=True)
class Municipio:
    slug: str
    nome: str
    uf: str
    codigo_ibge: str
    epsg: str  # SIRGAS 2000 / UTM, zona apropriada para a longitude do município

    @property
    def uf_sigla(self) -> str:
        return SIGLA_POR_CODIGO_UF[self.codigo_ibge[:2]]

    @property
    def regiao(self) -> str:
        return REGIAO_POR_UF[self.uf]

    @property
    def calcada_minima_m(self) -> float:
        return CALCADA_MINIMA_POR_REGIAO_M[self.regiao]


MUNICIPIOS: dict[str, Municipio] = {
    "sorocaba": Municipio(slug="sorocaba", nome="Sorocaba", uf="São Paulo", codigo_ibge="3552205", epsg="EPSG:31983"),
    "suzano": Municipio(slug="suzano", nome="Suzano", uf="São Paulo", codigo_ibge="3552502", epsg="EPSG:31983"),
}


def get_municipio(slug: str) -> Municipio:
    try:
        return MUNICIPIOS[slug]
    except KeyError:
        disponiveis = ", ".join(sorted(MUNICIPIOS))
        raise SystemExit(f"Município '{slug}' não configurado em common/municipios.py. Disponíveis: {disponiveis}")


def municipio_arg_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--municipio", default="sorocaba", help="Slug do município (ver common/municipios.py)")
    return parser
