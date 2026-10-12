"""Fetch auxiliary OSM layers for Sorocaba to check real data availability
before building filters for driveways, bus stops, poles, hydrants, trees,
crossings, overhead wiring and sidewalk width.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
HEADERS = {"User-Agent": "coordenada-geo-arvores-prototype/0.1 (contato: moises.pgser@gmail.com)"}

# bbox computed earlier for Sorocaba (south, west, north, east)
BBOX = (-23.5893162, -47.5706612, -23.349555, -47.3023274)


def main() -> None:
    out_path = DATA_DIR / "sorocaba_auxiliary_raw.json"
    if out_path.exists():
        print(f"[auxiliary] cache hit -> {out_path}")
        return

    south, west, north, east = BBOX
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
