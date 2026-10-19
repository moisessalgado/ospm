"""Export allocated slots to the webmap in a compact format and update its manifest.

Each variant (method x spacing) becomes webmap/data/<municipality>/<variant>.json:
  {"legend": {...}, "stride": 7 | 8,
   "points": [lon, lat, porte, tipo_vaga, logradouro, face, posicao_face_m, (cobertura_copa_pct), ...],
   "rejeitados": [lon, lat, porte, motivo, ...]}
"points" is a flat numeric array ("stride" numbers per slot) whose categorical
attributes are indices into "legend" — no per-feature GeoJSON boilerplate.
The 8th number, present only when 03b_add_canopy_cover.py ran for the
municipality, is the slot's canopy cover % (-1 where there is no data); "copa_fonte"
names the raster it came from.
"face" indexes legend.face, a [cd_setor, cd_quadra, cd_face] triple (-1, and
posicao_face_m -1, for slots with no IBGE match close enough). "rejeitados" is
every candidate the allocator dropped (4 numbers per point: lon, lat, porte,
motivo — the porte and position it would have had, and why it didn't become a
slot: corner setback, an obstacle, or no safe room before the nearest
facade), indexing legend.porte and legend.motivo the same way. The webmap
rebuilds the GeoJSON client-side. The manifest lists each variant's method
and spacing separately so the webmap can offer them as two selectors.
"""

from __future__ import annotations

import json

from shapely.geometry import shape

from planting_slots.municipalities import get_municipality, municipality_arg_parser
from planting_slots.paths import WEBMAP_DATA_DIR, MunicipalityFiles

COORD_DECIMALS = 5  # ~1.1 m — enough for display
POINT_STRIDE = 7  # 8 with the canopy-cover column
REJECTED_STRIDE = 4
LEGEND = {
    "porte": ["pequeno", "medio", "grande"],
    "tipo_vaga": ["calcada", "canteiro_central", "eixo_via"],
    "motivo": ["sem_espaco_seguro", "esquina", "travessia", "acesso_garagem", "ponto_onibus", "arvore_existente",
              "via_transversal", "espacamento_minimo"],
}
METHODS = {  # id -> label, in selector order
    "osm_eixo": "Eixo da via (OSM)",
    "osm_calcada_lado": "Calçada alocada em um dos lados da via (OSM)",
    "osm_calcada": "Calçada alocada nos dois lados da via (OSM)",
    "face_ibge": "Face de quadra (IBGE)",
    "osm_overture": "Calçada ajustada com prédios (OSM + Overture)",
}
SPACINGS = {
    "10m": "Espaçamento genérico de 10 m",
    "porte": "Espaçamento por porte (6/8/11 m)",
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

    has_canopy = any("cobertura_copa_pct" in feat["properties"] for feat in geojson["features"][:1])
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
        if has_canopy:
            canopy = props.get("cobertura_copa_pct")
            flat.append(canopy if canopy is not None else -1)
    legend = {**LEGEND, "logradouro": logradouro_values, "face": face_values}
    payload = {"legend": legend, "stride": POINT_STRIDE + has_canopy, "points": flat}
    if has_canopy:
        payload["copa_fonte"] = geojson["features"][0]["properties"].get("cobertura_copa_fonte")
    return payload


def compact_rejected(geojson: dict) -> list[float | int]:
    porte_index = {v: i for i, v in enumerate(LEGEND["porte"])}
    motivo_index = {v: i for i, v in enumerate(LEGEND["motivo"])}
    flat: list[float | int] = []
    for feat in geojson["features"]:
        lon, lat = feat["geometry"]["coordinates"]
        props = feat["properties"]
        flat += [round(lon, COORD_DECIMALS), round(lat, COORD_DECIMALS),
                porte_index.get(props.get("porte"), -1), motivo_index.get(props.get("motivo"), -1)]
    return flat


def main() -> None:
    municipality = get_municipality(municipality_arg_parser(__doc__).parse_args().municipio)
    files = MunicipalityFiles(municipality)
    out_dir = WEBMAP_DATA_DIR / municipality.slug
    out_dir.mkdir(parents=True, exist_ok=True)

    variants = []
    for method_id, method_label in METHODS.items():
        for spacing_id, spacing_label in SPACINGS.items():
            variant_id = f"{method_id}_{spacing_id}"
            src = files.slots(variant_id, "geojson")
            if not src.exists():
                continue
            payload = compact(json.loads(src.read_text()))
            rejected_src = files.slots(f"{variant_id}_sem_alocacao", "geojson")
            rejected = compact_rejected(json.loads(rejected_src.read_text())) if rejected_src.exists() else []
            payload["rejeitados"] = rejected
            out = out_dir / f"{variant_id}.json"
            out.write_text(json.dumps(payload, separators=(",", ":")))
            count = len(payload["points"]) // payload["stride"]
            rejected_count = len(rejected) // REJECTED_STRIDE
            variants.append({
                "id": variant_id, "method": method_id, "method_label": method_label,
                "spacing": spacing_id, "spacing_label": spacing_label, "count": count,
                "rejected_count": rejected_count,
            })
            print(f"[webmap] {variant_id}: {count} vagas, {rejected_count} sem alocação -> "
                  f"{out} ({out.stat().st_size / 1e6:.1f} MB)")
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
