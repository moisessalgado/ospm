"""Fetch auxiliary OSM layers for the município to check real data availability
before building filters for driveways, bus stops, poles, hydrants, trees,
crossings, overhead wiring and sidewalk width.
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

    out_path = data_dir / "auxiliary_raw.json"
    if out_path.exists():
        print(f"[auxiliary] cache hit -> {out_path}")
        return

    bbox = json.loads((data_dir / "bbox.json").read_text())
    south, west, north, east = bbox["south"], bbox["west"], bbox["north"], bbox["east"]
    bb = f"{south},{west},{north},{east}"
    query = f"""
    [out:json][timeout:300];
    (
      node["highway"="bus_stop"]({bb});
      node["public_transport"="platform"]({bb});
      way["public_transport"="platform"]({bb});
      node["natural"="tree"]({bb});
      way["highway"="footway"]["footway"="crossing"]({bb});
      node["highway"="crossing"]({bb});
      node["amenity"="parking_entrance"]({bb});
      way["service"="driveway"]({bb});
      node["barrier"="kerb"]({bb});
      node["man_made"="street_lamp"]({bb});
      node["power"="pole"]({bb});
      node["emergency"="fire_hydrant"]({bb});
      node["traffic_sign"]({bb});
      way["power"="line"]({bb});
      way["power"="minor_line"]({bb});
      way["highway"="footway"]["footway"="sidewalk"]["width"]({bb});
      way["highway"="footway"]["footway"="sidewalk"]["sidewalk:width"]({bb});
    );
    out body geom;
    """
    for attempt in range(1, 4):
        try:
            resp = httpx.post(OVERPASS_URL, data={"data": query}, headers=HEADERS, timeout=320)
            resp.raise_for_status()
            data = resp.json()
            out_path.write_text(json.dumps(data))
            print(f"[auxiliary] saved -> {out_path} ({len(data.get('elements', []))} elements)")
            return
        except Exception as exc:  # noqa: BLE001
            print(f"[auxiliary] attempt {attempt} failed: {exc}")
            time.sleep(15)
    raise RuntimeError("Failed to fetch auxiliary layers after 3 attempts")


if __name__ == "__main__":
    main()
