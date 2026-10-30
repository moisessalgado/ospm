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

A municipality with `subdivisions` (the Distrito Federal) is exported as one entry per
IBGE division instead — see Subdivisions in planting_slots/municipalities.py.

A municipality with canopy cover (03b) also gets <municipality>/copa.png, the
raster that cover came from as a colored image, and a "canopy" entry in the
manifest: the image's corners, the source's label and credit, and the color
ramp (the legend's gradient) — drawn by the webmap as an optional layer.
"""

from __future__ import annotations

import json
import re
import unicodedata

import geopandas as gpd
from shapely.geometry import shape

from planting_slots.ibge import load_subdivisions
from planting_slots.canopy import CANOPY_RAMP, CANOPY_SOURCES, export_canopy_image
from planting_slots.municipalities import get_municipality, municipality_arg_parser
from planting_slots.paths import WEBMAP_DATA_DIR, MunicipalityFiles

COORD_DECIMALS = 5  # ~1.1 m — enough for display
POINT_STRIDE = 7  # 8 with the canopy-cover column
REJECTED_STRIDE = 4
LEGEND = {
    "porte": ["pequeno", "medio", "grande"],
    "tipo_vaga": ["calcada", "canteiro_central", "eixo_via"],
    "motivo": ["sem_espaco_seguro", "esquina", "travessia", "acesso_garagem", "ponto_onibus", "arvore_existente",
              "via_transversal", "espacamento_minimo", "calcada_estreita", "semaforo", "poste", "transformador",
              "boca_de_lobo"],
}
METHODS = {  # id -> label, in selector order
    "osm_eixo": "Eixo da via (OSM)",
    "osm_calcada": "Calçada alocada nos dois lados da via (OSM)",
    "face_ibge": "Face de quadra (IBGE)",
}
SPACINGS = {
    "10m": "Espaçamento padrão de 10 m (maior porte)",
    "porte": "Espaçamento por porte (10 / 6 / 4 m)",
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


def write_variant(out_dir, method_id: str, spacing_id: str, points: dict, rejected_fc: dict | None) -> tuple[dict, str | None]:
    """Write one variant's JSON; returns its manifest entry and the canopy source label."""
    variant_id = f"{method_id}_{spacing_id}"
    payload = compact(points)
    rejected = compact_rejected(rejected_fc) if rejected_fc else []
    payload["rejeitados"] = rejected
    out = out_dir / f"{variant_id}.json"
    out.write_text(json.dumps(payload, separators=(",", ":")))
    count = len(payload["points"]) // payload["stride"]
    rejected_count = len(rejected) // REJECTED_STRIDE
    print(f"[webmap] {out.parent.name}/{variant_id}: {count} vagas, {rejected_count} sem alocação "
          f"({out.stat().st_size / 1e6:.1f} MB)")
    return ({"id": variant_id, "method": method_id, "method_label": METHODS[method_id],
             "spacing": spacing_id, "spacing_label": SPACINGS[spacing_id], "count": count,
             "rejected_count": rejected_count}, payload.get("copa_fonte"))


def canopy_entry(files: MunicipalityFiles, label: str | None, out_dir, slug: str, bounds=None) -> dict | None:
    """The manifest's "canopy" entry (and its image) for the raster the slots' canopy came from."""
    source = next((s for s, cfg in CANOPY_SOURCES.items() if cfg["label"] == label), None)
    if not source or not files.canopy_rasters[source].exists():
        return None
    image = out_dir / "copa.png"
    corners = export_canopy_image(files.canopy_rasters[source], image, bounds)
    print(f"[webmap] raster de copa ({label}) -> {image} ({image.stat().st_size / 1e3:.0f} kB)")
    return {"image": f"{slug}/copa.png", "coordinates": corners, "source": label,
            "credit": CANOPY_SOURCES[source]["credit"], "ramp": CANOPY_RAMP}


def update_manifest(entries: list[dict], replaces) -> None:
    manifest_path = WEBMAP_DATA_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    kept = [m for m in manifest.get("municipalities", []) if not replaces(m["slug"])]
    manifest = {"municipalities": sorted(kept + entries, key=lambda m: m["name"])}
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"[webmap] manifesto atualizado -> {manifest_path}")


def export_whole(municipality, files: MunicipalityFiles) -> None:
    out_dir = WEBMAP_DATA_DIR / municipality.slug
    out_dir.mkdir(parents=True, exist_ok=True)
    variants, canopy_label = [], None
    for method_id in METHODS:
        for spacing_id in SPACINGS:
            src = files.slots(f"{method_id}_{spacing_id}", "geojson")
            if not src.exists():
                continue
            rejected_src = files.slots(f"{method_id}_{spacing_id}_sem_alocacao", "geojson")
            variant, label = write_variant(out_dir, method_id, spacing_id, json.loads(src.read_text()),
                                           json.loads(rejected_src.read_text()) if rejected_src.exists() else None)
            variants.append(variant)
            canopy_label = label or canopy_label
    if not variants:
        raise SystemExit(f"Nenhuma saída de alocação em {files.output_dir}; rode antes o 03_allocate_slots.py.")

    boundary = shape(json.loads(files.boundary.read_text())["features"][0]["geometry"])
    west, south, east, north = boundary.bounds
    entry = {"slug": municipality.slug, "name": municipality.name, "state": municipality.state,
             "center": [(west + east) / 2, (south + north) / 2], "variants": variants}
    if municipality.focus:
        entry["focus"] = list(municipality.focus)
    canopy = canopy_entry(files, canopy_label, out_dir, municipality.slug)
    if canopy:
        entry["canopy"] = canopy
    update_manifest([entry], lambda slug: slug == municipality.slug)


def division_slug(municipality, name: str, code: str, duplicated: bool) -> str:
    base = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_")
    return f"{municipality.slug}_{base}" + (f"_{code[-2:]}" if duplicated else "")


def frame_to_featurecollection(frame: gpd.GeoDataFrame) -> dict:
    """GeoJSON-like dict of a point GeoDataFrame, with nulls (NaN / <NA>) as None."""
    props = frame.drop(columns="geometry").astype(object).where(frame.drop(columns="geometry").notna(), None)
    records = props.to_dict("records")
    xs, ys = frame.geometry.x.to_numpy(), frame.geometry.y.to_numpy()
    return {"features": [{"geometry": {"coordinates": [x, y]}, "properties": r} for x, y, r in zip(xs, ys, records)]}


def export_divisions(municipality, files: MunicipalityFiles) -> None:
    """One webmap entry per IBGE division of the municipality (see Subdivisions)."""
    cfg = municipality.subdivisions
    divisions = load_subdivisions(files, "EPSG:4326")
    names = divisions[cfg.name_column]
    divisions["slug"] = [division_slug(municipality, n, c, names.duplicated(keep=False)[i])
                         for i, (n, c) in enumerate(zip(names, divisions[cfg.code_column]))]
    divisions["label"] = [f"{n} ({c[-2:]})" if names.duplicated(keep=False)[i] else n
                          for i, (n, c) in enumerate(zip(names, divisions[cfg.code_column]))]
    entries: dict[str, dict] = {}
    labels: dict[str, str | None] = {}
    for method_id in METHODS:
        for spacing_id in SPACINGS:
            variant_id = f"{method_id}_{spacing_id}"
            if not files.slots(variant_id, "gpkg").exists():
                continue
            targets = divisions if method_id == "face_ibge" else divisions[divisions[cfg.name_column].isin(cfg.full_divisions)]
            if targets.empty:
                continue
            # only read the targets' area: the full OSM-only variants are millions of slots
            bbox_local = tuple(targets.to_crs(municipality.epsg).total_bounds)
            bbox_wgs = tuple(targets.total_bounds)
            points = gpd.read_file(files.slots(variant_id, "gpkg"), layer="vagas_plantio", bbox=bbox_local).to_crs("EPSG:4326")
            rejected_src = files.slots(f"{variant_id}_sem_alocacao", "geojson")
            rejected = gpd.read_file(rejected_src, bbox=bbox_wgs) if rejected_src.exists() else None
            point_div = gpd.sjoin(points, targets[["slug", "geometry"]], predicate="within").drop(columns="index_right")
            rejected_div = (gpd.sjoin(rejected, targets[["slug", "geometry"]], predicate="within").drop(columns="index_right")
                            if rejected is not None else None)
            for slug, group in point_div.groupby("slug"):
                out_dir = WEBMAP_DATA_DIR / slug
                out_dir.mkdir(parents=True, exist_ok=True)
                rej_group = rejected_div[rejected_div["slug"] == slug].drop(columns="slug") if rejected_div is not None else None
                variant, label = write_variant(out_dir, method_id, spacing_id,
                                               frame_to_featurecollection(group.drop(columns="slug")),
                                               frame_to_featurecollection(rej_group) if rej_group is not None else None)
                entries.setdefault(slug, {"variants": []})["variants"].append(variant)
                labels[slug] = label or labels.get(slug)
            del points, point_div
    result = []
    for row in divisions.itertuples():
        info = entries.get(row.slug)
        if not info:
            continue
        west, south, east, north = row.geometry.bounds
        entry = {"slug": row.slug, "name": row.label, "state": municipality.state,
                 "center": [(west + east) / 2, (south + north) / 2], "variants": info["variants"]}
        if getattr(row, cfg.name_column) == cfg.focus_division and municipality.focus:
            entry["focus"] = list(municipality.focus)
        canopy = canopy_entry(files, labels.get(row.slug), WEBMAP_DATA_DIR / row.slug, row.slug,
                              (west - 0.002, south - 0.002, east + 0.002, north + 0.002))
        if canopy:
            entry["canopy"] = canopy
        result.append(entry)
    update_manifest(result, lambda slug: slug == municipality.slug or slug.startswith(f"{municipality.slug}_"))


def main() -> None:
    municipality = get_municipality(municipality_arg_parser(__doc__).parse_args().municipio)
    files = MunicipalityFiles(municipality)
    if municipality.subdivisions:
        export_divisions(municipality, files)
    else:
        export_whole(municipality, files)


if __name__ == "__main__":
    main()
