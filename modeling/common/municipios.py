"""Registro de municípios suportados pelo pipeline de modelagem.

Para adicionar um novo município:
  1. Descubra o EPSG da zona UTM SIRGAS 2000 correta (ex.: via epsg.io,
     buscando pela longitude do centro do município).
  2. Adicione uma entrada em MUNICIPIOS abaixo.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass(frozen=True)
class Municipio:
    slug: str
    nome: str
    uf: str
    epsg: str  # SIRGAS 2000 / UTM, zona apropriada para a longitude do município


MUNICIPIOS: dict[str, Municipio] = {
    "sorocaba": Municipio(slug="sorocaba", nome="Sorocaba", uf="São Paulo", epsg="EPSG:31983"),
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
