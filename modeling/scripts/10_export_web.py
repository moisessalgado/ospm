"""Export the planting-slot points for the webmap in a compact custom format
(not GeoJSON): points are a flat numeric array with no per-feature
"type":"Feature"/geometry/properties boilerplate, and attribute values
(porte, slot_type) are written as small integer codes with a "legend" table
to decode them. The webmap's JS rebuilds a GeoJSON FeatureCollection
client-side from this before handing it to MapLibre. Also updates
webmap/data/manifest.json with the list of municípios available to the
município selector.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.municipios import get_municipio, municipio_arg_parser  # noqa: E402

COORD_DECIMALS = 5  # ~1.1 m at the equator — plenty for point display on a webmap

# Ordem fixa das categorias -> índice usado como código no array de pontos.
PORTE_CODES = ["pequeno", "medio", "grande"]
SLOT_TYPE_CODES = ["calcada", "canteiro_central"]

# Versões exportadas para o webmap, na ordem do seletor (só as que existirem).
VERSOES = {
    "v2_generico10": "v2 — espaçamento genérico 10 m",
    "v2_porte": "v2 — espaçamento por porte (6/8/11 m)",
    "v1": "v1 — modelo anterior (referência)",
}


def main() -> None:
    args = municipio_arg_parser(__doc__).parse_args()
    municipio = get_municipio(args.municipio)
    out_dir = Path(__file__).resolve().parent.parent / "output" / municipio.slug
    data_dir = Path(__file__).resolve().parent.parent / "data" / municipio.slug
    webmap_data_dir = Path(__file__).resolve().parent.parent.parent / "webmap" / "data"
    webmap_data_dir.mkdir(parents=True, exist_ok=True)

    porte_idx = {v: i for i, v in enumerate(PORTE_CODES)}
    slot_idx = {v: i for i, v in enumerate(SLOT_TYPE_CODES)}

    versoes = []
    for versao_id, label in VERSOES.items():
        src_path = out_dir / f"vagas_plantio_{versao_id}.geojson"
        if not src_path.exists():
            continue
        fc = json.loads(src_path.read_text())

        # "points" é um array plano [lon, lat, porte_code, slot_code, lon, lat, ...]
        # — 4 números por ponto, sem chaves/objetos repetidos.
        flat: list[float | int] = []
        for feat in fc["features"]:
            lon, lat = feat["geometry"]["coordinates"]
            props = feat.get("properties", {})
            flat.append(round(lon, COORD_DECIMALS))
            flat.append(round(lat, COORD_DECIMALS))
            flat.append(porte_idx.get(props.get("porte"), -1))
            flat.append(slot_idx.get(props.get("slot_type"), -1))

        payload = {"legend": {"porte": PORTE_CODES, "slot_type": SLOT_TYPE_CODES}, "points": flat}
        out_path = webmap_data_dir / f"{municipio.slug}__{versao_id}.json"
        out_path.write_text(json.dumps(payload, separators=(",", ":")))
        n_points = len(flat) // 4
        versoes.append({"id": versao_id, "label": label, "count": n_points})
        print(f"[export] {versao_id}: {n_points} pontos -> {out_path} ({out_path.stat().st_size / 1e6:.1f} MB)")

    if not versoes:
        raise SystemExit(f"Nenhuma saída de modelagem encontrada em {out_dir}")

    boundary = json.loads((data_dir / "boundary.geojson").read_text())
    geom = boundary["features"][0]["geometry"]
    rings = geom["coordinates"] if geom["type"] == "Polygon" else [r for poly in geom["coordinates"] for r in poly]
    coords = [c for ring in rings for c in ring]
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    center = [(min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2]

    manifest_path = webmap_data_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"municipios": []}
    manifest["municipios"] = [m for m in manifest["municipios"] if m["slug"] != municipio.slug]
    manifest["municipios"].append(
        {
            "slug": municipio.slug,
            "nome": municipio.nome,
            "uf": municipio.uf,
            "center": center,
            "versoes": versoes,
        }
    )
    manifest["municipios"].sort(key=lambda m: m["nome"])
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"[export] manifest atualizado -> {manifest_path}")


if __name__ == "__main__":
    main()
