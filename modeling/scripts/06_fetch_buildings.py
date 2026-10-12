"""Fetch building footprints for the município — used as a proxy for real
sidewalk width (distance from the street edge to the nearest building
facade), since OSM has essentially no sidewalk width tagging here.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.municipios import get_municipio, municipio_arg_parser  # noqa: E402

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
HEADERS = {"User-Agent": "coordenada-geo-arvores-prototype/0.1 (contato: moises.pgser@gmail.com)"}


def main() -> None:
    args = municipio_arg_parser(__doc__).parse_args()
    municipio = get_municipio(args.municipio)
    data_dir = Path(__file__).resolve().parent.parent / "data" / municipio.slug

    out_path = data_dir / "buildings_raw.json"
    if out_path.exists():
        print(f"[buildings] cache hit -> {out_path}")
        return

    bbox = json.loads((data_dir / "bbox.json").read_text())
    south, west, north, east = bbox["south"], bbox["west"], bbox["north"], bbox["east"]
    query = f"""
    [out:json][timeout:500];
    way["building"]({south},{west},{north},{east});
    out geom;
    """
    for attempt in range(1, 7):
        try:
            resp = httpx.post(OVERPASS_URL, data={"data": query}, headers=HEADERS, timeout=540)
            resp.raise_for_status()
            data = resp.json()
            out_path.write_text(json.dumps(data))
            print(f"[buildings] saved -> {out_path} ({len(data.get('elements', []))} buildings)")
            return
        except Exception as exc:  # noqa: BLE001
            print(f"[buildings] attempt {attempt} failed: {exc}")
            time.sleep(30)
    raise RuntimeError("Failed to fetch buildings after 6 attempts")


if __name__ == "__main__":
    main()
