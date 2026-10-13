"""Download OpenStreetMap inputs for a municipality: boundary (Nominatim),
road network clipped to the boundary, and point/line obstacles (Overpass).

Downloads are cached in the municipality's data dir; delete a file to refresh it.
"""

from __future__ import annotations

import json
import time

import geopandas as gpd
import httpx
from shapely.geometry import LineString, shape

from planting_slots.municipalities import get_municipality, municipality_arg_parser
from planting_slots.paths import OSM_ROADS_LAYER, MunicipalityFiles

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
HEADERS = {"User-Agent": "ospm-vagas-plantio/0.1 (contato: moises.pgser@gmail.com)"}

ROAD_TAGS = ["highway", "name", "oneway", "lanes", "sidewalk", "width", "surface", "junction"]
OBSTACLE_QUERY = """
  node["highway"="bus_stop"]({bb});
  node["public_transport"="platform"]({bb});
  way["public_transport"="platform"]({bb});
  node["natural"="tree"]({bb});
  way["highway"="footway"]["footway"="crossing"]({bb});
  node["highway"="crossing"]({bb});
  node["amenity"="parking_entrance"]({bb});
  way["service"="driveway"]({bb});
"""


def overpass(query: str, timeout_s: int) -> dict:
    for attempt in range(1, 4):
        try:
            resp = httpx.post(OVERPASS_URL, data={"data": query}, headers=HEADERS, timeout=timeout_s + 30)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 — Overpass fails transiently under load
            print(f"  tentativa {attempt} falhou: {exc}")
            time.sleep(15 * attempt)
    raise RuntimeError("Overpass falhou após 3 tentativas")


def fetch_boundary(files: MunicipalityFiles) -> dict:
    if files.boundary.exists():
        return json.loads(files.boundary.read_text())
    m = files.municipality
    params = {"city": m.name, "state": m.state, "country": "Brazil", "format": "json", "polygon_geojson": 1, "limit": 1}
    resp = httpx.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise SystemExit(f"Nominatim não encontrou {m.name}, {m.state}")
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": results[0]["geojson"],
         "properties": {"display_name": results[0]["display_name"], "osm_id": results[0]["osm_id"]}}
    ]}
    files.boundary.write_text(json.dumps(fc))
    print(f"[limite] salvo -> {files.boundary}")
    return fc


def bbox_of(boundary: dict) -> dict:
    west, south, east, north = shape(boundary["features"][0]["geometry"]).bounds
    return {"south": south, "west": west, "north": north, "east": east}


def fetch_roads(files: MunicipalityFiles, bb: str) -> dict:
    if files.osm_roads_raw.exists():
        return json.loads(files.osm_roads_raw.read_text())
    print("[vias] consultando Overpass…")
    data = overpass(f'[out:json][timeout:180];way["highway"]({bb});out body geom;', 180)
    files.osm_roads_raw.write_text(json.dumps(data))
    print(f"[vias] {len(data['elements'])} vias -> {files.osm_roads_raw}")
    return data


def fetch_obstacles(files: MunicipalityFiles, bb: str) -> None:
    if files.osm_obstacles.exists():
        return
    print("[obstáculos] consultando Overpass…")
    data = overpass(f"[out:json][timeout:300];({OBSTACLE_QUERY.format(bb=bb)});out body geom;", 300)
    files.osm_obstacles.write_text(json.dumps(data))
    print(f"[obstáculos] {len(data['elements'])} elementos -> {files.osm_obstacles}")


def clip_roads(files: MunicipalityFiles, raw: dict, boundary: dict) -> None:
    rows = []
    for el in raw["elements"]:
        if el.get("type") != "way" or len(el.get("geometry", [])) < 2:
            continue
        tags = el.get("tags", {})
        rows.append({"osm_id": el["id"], **{t: tags.get(t) for t in ROAD_TAGS},
                     "geometry": LineString([(p["lon"], p["lat"]) for p in el["geometry"]])})
    roads = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    limit = gpd.GeoDataFrame(geometry=[shape(boundary["features"][0]["geometry"])], crs="EPSG:4326")
    clipped = gpd.clip(roads, limit).to_crs(files.municipality.epsg)
    clipped.to_file(files.osm_roads, layer=OSM_ROADS_LAYER, driver="GPKG")
    print(f"[vias] {len(clipped)} vias no limite do município -> {files.osm_roads}")


def main() -> None:
    files = MunicipalityFiles(get_municipality(municipality_arg_parser(__doc__).parse_args().municipio))
    files.ensure_dirs()
    boundary = fetch_boundary(files)
    bbox = bbox_of(boundary)
    files.bbox.write_text(json.dumps(bbox))
    bb = f"{bbox['south']},{bbox['west']},{bbox['north']},{bbox['east']}"
    clip_roads(files, fetch_roads(files, bb), boundary)
    fetch_obstacles(files, bb)


if __name__ == "__main__":
    main()
