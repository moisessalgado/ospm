"""Download the municipal boundary (Nominatim) and road network (Overpass)
for a given município.

Pilot for the tree-planting-slot modeling prototype. Single city at a time,
so Overpass API is fine here (country-wide would instead use a Geofabrik
.osm.pbf extract).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.municipios import get_municipio, municipio_arg_parser  # noqa: E402

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

HEADERS = {"User-Agent": "coordenada-geo-arvores-prototype/0.1 (contato: moises.pgser@gmail.com)"}


def fetch_boundary(data_dir: Path, nome: str, uf: str) -> dict:
    out_path = data_dir / "boundary.geojson"
    if out_path.exists():
        print(f"[boundary] cache hit -> {out_path}")
        return json.loads(out_path.read_text())

    params = {
        "city": nome,
        "state": uf,
        "country": "Brazil",
        "format": "json",
        "polygon_geojson": 1,
        "limit": 1,
    }
    resp = httpx.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise RuntimeError(f"Nominatim returned no results for {nome}, {uf}")

    feature = {
        "type": "Feature",
        "geometry": results[0]["geojson"],
        "properties": {
            "display_name": results[0]["display_name"],
            "osm_id": results[0]["osm_id"],
            "osm_type": results[0]["osm_type"],
        },
    }
    fc = {"type": "FeatureCollection", "features": [feature]}
    out_path.write_text(json.dumps(fc))
    print(f"[boundary] saved -> {out_path}")
    return fc


def fetch_highways(data_dir: Path, bbox: tuple[float, float, float, float]) -> dict:
    out_path = data_dir / "highways_raw.json"
    if out_path.exists():
        print(f"[highways] cache hit -> {out_path}")
        return json.loads(out_path.read_text())

    south, west, north, east = bbox
    query = f"""
    [out:json][timeout:180];
    (
      way["highway"]({south},{west},{north},{east});
    );
    out body geom;
    """
    for attempt in range(1, 4):
        try:
            resp = httpx.post(OVERPASS_URL, data={"data": query}, headers=HEADERS, timeout=200)
            resp.raise_for_status()
            data = resp.json()
            out_path.write_text(json.dumps(data))
            print(f"[highways] saved -> {out_path} ({len(data.get('elements', []))} ways)")
            return data
        except Exception as exc:  # noqa: BLE001 — retry on transient Overpass failures
            print(f"[highways] attempt {attempt} failed: {exc}")
            time.sleep(10)
    raise RuntimeError("Failed to fetch highways from Overpass after 3 attempts")


def main() -> None:
    args = municipio_arg_parser(__doc__).parse_args()
    municipio = get_municipio(args.municipio)

    data_dir = Path(__file__).resolve().parent.parent / "data" / municipio.slug
    data_dir.mkdir(parents=True, exist_ok=True)

    boundary_fc = fetch_boundary(data_dir, municipio.nome, municipio.uf)
    geom = boundary_fc["features"][0]["geometry"]

    coords = []
    if geom["type"] == "Polygon":
        rings = geom["coordinates"]
    else:  # MultiPolygon
        rings = [ring for poly in geom["coordinates"] for ring in poly]
    for ring in rings:
        coords.extend(ring)
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    bbox = (min(lats), min(lons), max(lats), max(lons))
    print(f"[bbox] south,west,north,east = {bbox}")

    bbox_path = data_dir / "bbox.json"
    bbox_path.write_text(json.dumps({"south": bbox[0], "west": bbox[1], "north": bbox[2], "east": bbox[3]}))

    fetch_highways(data_dir, bbox)


if __name__ == "__main__":
    main()
