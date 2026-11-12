"""National batch: the `osm_calcada` planting slots (10 m) of every Brazilian
municipality, ready to be ingested into the GEOCAU database.

Subcommands (run in this order, or `tudo` for all of them):
  baixar     downloads the Geofabrik regional extracts, IBGE (municipal mesh, census
             tracts, street faces) and the tree-canopy tiles (GFW needs GFW_API_KEY)
  extrair    reads each region's .osm.pbf once into GeoParquet roads/obstacles
  processar  generates the points of each municipality, in parallel and resumable
  validar    checks every output and writes resumo.csv and metadados.json

Every step is idempotent: rerunning skips what is already done (`--refazer`
redoes it). The product is one GeoParquet per municipality,
<saida>/pontos/<UF>/<cd_mun>.parquet, carrying `cd_mun` on every point. The file holds the
viable slots (`viavel` true) and the candidates dropped along the way (`viavel` false, with
`motivo`, using OSM and IBGE only). Select
a subset with --uf, --regiao or --municipio (repeatable); with none, all 5,570.

Examples:
  python scripts/05_pontos_brasil.py tudo --uf AC --workers 4
  python scripts/05_pontos_brasil.py tudo --municipio 3554805 --fontes-copa glad_30m
  python scripts/05_pontos_brasil.py processar --workers 20 && python scripts/05_pontos_brasil.py validar
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from planting_slots.brasil import REGIONS, Layout, load_municipalities, region_of
from planting_slots.copa_brasil import DEFAULT_SOURCES, download_tiles, require_api_key, tiles_for_municipalities
from planting_slots.ibge_brasil import prepare_ibge, prepare_municipalities, ufs_of
from planting_slots.lote import ERROR, run_batch, validate, write_metadata
from planting_slots.osm_pbf import download_region, extract_region


def select(layout: Layout, args) -> list[str]:
    table = load_municipalities(layout)
    if args.municipio:
        unknown = sorted(set(args.municipio) - set(table["cd_mun"]))
        if unknown:
            raise SystemExit(f"Códigos IBGE desconhecidos: {', '.join(unknown)}")
        table = table[table["cd_mun"].isin(args.municipio)]
    if args.uf:
        table = table[table["uf"].isin([u.upper() for u in args.uf])]
    if args.regiao:
        table = table[table["regiao"].isin(args.regiao)]
    codes = sorted(table["cd_mun"])
    if args.limite:
        codes = codes[:args.limite]
    if not codes:
        raise SystemExit("Nenhum município selecionado.")
    return codes


def cmd_baixar(layout: Layout, args) -> list[str]:
    require_api_key(args.fontes_copa)  # fail before hours of downloads
    prepare_municipalities(layout)
    codes = select(layout, args)
    table = load_municipalities(layout, codes)
    regions = sorted({region_of(c) for c in codes})
    print(f"[baixar] {len(codes)} municípios; regiões {', '.join(regions)}")
    for region in regions:
        print(f"[geofabrik] {region}")
        download_region(layout, region)
    prepare_ibge(layout, ufs_of(codes), workers=min(args.workers, 4))
    download_tiles(layout, tiles_for_municipalities(table), args.fontes_copa)
    return codes


def _extract(args):
    layout, region = args
    return region, extract_region(layout.pbf(region), layout.osm_roads(region), layout.osm_obstacles(region))


def cmd_extrair(layout: Layout, args) -> list[str]:
    codes = select(layout, args)
    regions = sorted({region_of(c) for c in codes})
    todo = [r for r in regions if args.refazer or not layout.osm_roads(r).with_name("extracao.json").exists()]
    for region in todo:
        if not layout.pbf(region).exists():
            raise SystemExit(f"{layout.pbf(region)} ausente; rode antes o subcomando 'baixar'.")
    print(f"[extrair] regiões: {', '.join(todo) or 'nenhuma (já extraídas)'}")
    if todo:
        with ProcessPoolExecutor(min(args.workers, len(todo))) as pool:
            for region, counts in pool.map(_extract, [(layout, r) for r in todo]):
                print(f"[extrair] {region}: {counts}")
    return codes


def cmd_processar(layout: Layout, args) -> list[str]:
    codes = select(layout, args)
    results = run_batch(layout, codes, args.workers, tuple(args.fontes_copa), args.refazer)
    if any(r["status"] == ERROR for r in results):
        print("[processar] há municípios com erro; corrija e rode de novo (só eles serão refeitos).")
    return codes


def cmd_validar(layout: Layout, args) -> list[str]:
    codes = select(layout, args)
    summary = validate(layout, codes)
    write_metadata(layout, summary, tuple(args.fontes_copa))
    counts = summary["status"].value_counts().to_dict()
    bad = summary[summary["problemas"].fillna("") != ""]
    print(f"[validar] {len(summary)} municípios: {counts}; {int(summary['pontos'].sum())} pontos")
    print(f"[validar] resumo: {layout.summary}\n[validar] metadados: {layout.metadata}")
    if len(bad) or counts.get(ERROR) or counts.get("nao_processado"):
        for row in bad.itertuples():
            print(f"  PROBLEMA {row.cd_mun} {row.nm_mun}: {row.problemas}")
        raise SystemExit(1)
    return codes


COMMANDS = {"baixar": cmd_baixar, "extrair": cmd_extrair, "processar": cmd_processar, "validar": cmd_validar}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("etapa", choices=[*COMMANDS, "tudo"])
    parser.add_argument("--uf", nargs="+", metavar="UF", help="Siglas das UFs (ex.: SP SC)")
    parser.add_argument("--regiao", nargs="+", choices=REGIONS, help="Regiões do Geofabrik")
    parser.add_argument("--municipio", nargs="+", metavar="CD_MUN", help="Códigos IBGE de 7 dígitos")
    parser.add_argument("--limite", type=int, help="Processa só os N primeiros (por código) da seleção")
    parser.add_argument("--workers", type=int, default=1, help="Processos paralelos (padrão 1)")
    parser.add_argument("--refazer", action="store_true", help="Refaz o que já está pronto (extrair/processar)")
    parser.add_argument("--fontes-copa", nargs="+", choices=["gfw_10m", "glad_30m"], default=list(DEFAULT_SOURCES),
                        help="Fontes de copa em ordem de prioridade (padrão: gfw_10m glad_30m)")
    parser.add_argument("--dados-dir", type=Path, help="Onde ficam downloads e caches (padrão: modeling/data/brasil)")
    parser.add_argument("--saida-dir", type=Path, help="Onde fica o produto (padrão: modeling/output/brasil)")
    args = parser.parse_args()

    defaults = Layout()
    layout = Layout(data=args.dados_dir or defaults.data, out=args.saida_dir or defaults.out)
    for step in (COMMANDS if args.etapa == "tudo" else [args.etapa]):
        print(f"=== {step} ===")
        COMMANDS[step](layout, args)


if __name__ == "__main__":
    sys.exit(main())
