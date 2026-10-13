"""Export allocated slots to the webmap in a compact format and update its manifest.

Each variant becomes webmap/data/<municipality>/<variant>.json:
  {"legend": {...}, "points": [lon, lat, porte, tipo_vaga, logradouro, face, posicao_face_m, ...]}
i.e. a flat numeric array (7 numbers per slot) whose categorical attributes
are indices into "legend" — no per-feature GeoJSON boilerplate. "face" indexes
legend.face, a [cd_setor, cd_quadra, cd_face] triple (-1, and posicao_face_m
-1, for median slots, which have none). The webmap rebuilds the GeoJSON
client-side.
"""

from __future__ import annotations

import json

from shapely.geometry import shape

from planting_slots.municipalities import get_municipality, municipality_arg_parser
from planting_slots.paths import WEBMAP_DATA_DIR, MunicipalityFiles

COORD_DECIMALS = 5  # ~1.1 m — enough for display
POINT_STRIDE = 7
LEGEND = {
    "porte": ["pequeno", "medio", "grande"],
    "tipo_vaga": ["calcada", "canteiro_central"],
}
VARIANTS = {  # id -> label shown in the webmap, in selector order
    "espacamento_10m": "Espaçamento genérico de 10 m",
    "espacamento_porte": "Espaçamento por porte (6/8/11 m)",
}


def vocabulary():
    """A values list plus a get(value) -> index that appends new values."""
    values: list = []
    index: dict = {}

    def get(value):
        if value not in index:
            index[value] = len(values)
            values.append(value)
        return index[value]

    return values, get


def compact(geojson: dict) -> dict:
    porte_index = {v: i for i, v in enumerate(LEGEND["porte"])}
    tipo_index = {v: i for i, v in enumerate(LEGEND["tipo_vaga"])}
    logradouro_values, get_logradouro = vocabulary()
    face_values, get_face = vocabulary()

    flat: list[float | int] = []
    for feat in geojson["features"]:
        lon, lat = feat["geometry"]["coordinates"]
        props = feat["properties"]
        setor = props.get("cd_setor")
        face_i = get_face((setor, props.get("cd_quadra"), props.get("cd_face"))) if setor is not None else -1
        pos = props.get("posicao_face_m")
        flat += [
            round(lon, COORD_DECIMALS), round(lat, COORD_DECIMALS),
            porte_index.get(props.get("porte"), -1), tipo_index.get(props.get("tipo_vaga"), -1),
            get_logradouro(props.get("logradouro") or ""), face_i, pos if pos is not None else -1,
        ]
    legend = {**LEGEND, "logradouro": logradouro_values, "face": face_values}
    return {"legend": legend, "points": flat}


def main() -> None:
    municipality = get_municipality(municipality_arg_parser(__doc__).parse_args().municipio)
    files = MunicipalityFiles(municipality)
    out_dir = WEBMAP_DATA_DIR / municipality.slug
    out_dir.mkdir(parents=True, exist_ok=True)

    variants = []
    for variant_id, label in VARIANTS.items():
        src = files.slots(variant_id, "geojson")
        if not src.exists():
            continue
        payload = compact(json.loads(src.read_text()))
        out = out_dir / f"{variant_id}.json"
        out.write_text(json.dumps(payload, separators=(",", ":")))
        count = len(payload["points"]) // POINT_STRIDE
        variants.append({"id": variant_id, "label": label, "count": count})
        print(f"[webmap] {variant_id}: {count} vagas -> {out} ({out.stat().st_size / 1e6:.1f} MB)")
    if not variants:
        raise SystemExit(f"Nenhuma saída de alocação em {files.output_dir}; rode antes o 03_allocate_slots.py.")

    boundary = shape(json.loads(files.boundary.read_text())["features"][0]["geometry"])
    west, south, east, north = boundary.bounds
    entry = {"slug": municipality.slug, "name": municipality.name, "state": municipality.state,
             "center": [(west + east) / 2, (south + north) / 2], "variants": variants}

    manifest_path = WEBMAP_DATA_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    municipalities = [m for m in manifest.get("municipalities", []) if m["slug"] != municipality.slug] + [entry]
    manifest = {"municipalities": sorted(municipalities, key=lambda m: m["name"])}
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"[webmap] manifesto atualizado -> {manifest_path}")


if __name__ == "__main__":
    main()
