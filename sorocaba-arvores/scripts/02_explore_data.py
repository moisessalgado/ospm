"""Load the raw Overpass JSON into a GeoDataFrame, clip to Sorocaba, inspect tags."""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, shape

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

TARGET_CRS = "EPSG:31983"  # SIRGAS 2000 / UTM zone 23S — covers Sorocaba (lon ~-47.45)


def load_ways() -> gpd.GeoDataFrame:
    raw = json.loads((DATA_DIR / "sorocaba_highways_raw.json").read_text())
    rows = []
    for el in raw["elements"]:
        if el.get("type") != "way" or "geometry" not in el:
            continue
        coords = [(pt["lon"], pt["lat"]) for pt in el["geometry"]]
        if len(coords) < 2:
            continue
        tags = el.get("tags", {})
        rows.append(
            {
                "osm_id": el["id"],
                "geometry": LineString(coords),
                "highway": tags.get("highway"),
                "name": tags.get("name"),
                "oneway": tags.get("oneway"),
                "lanes": tags.get("lanes"),
                "sidewalk": tags.get("sidewalk"),
                "surface": tags.get("surface"),
                "junction": tags.get("junction"),
                "width": tags.get("width"),
            }
        )
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    return gdf


def load_boundary() -> gpd.GeoDataFrame:
    fc = json.loads((DATA_DIR / "sorocaba_boundary.geojson").read_text())
    geom = shape(fc["features"][0]["geometry"])
    return gpd.GeoDataFrame([{"geometry": geom}], geometry="geometry", crs="EPSG:4326")


def main() -> None:
    ways = load_ways()
    boundary = load_boundary()
    print(f"Total ways baixadas (bbox): {len(ways)}")

    clipped = gpd.clip(ways, boundary)
    print(f"Ways após clip no polígono do município: {len(clipped)}")

    print("\n--- Distribuição por highway= ---")
    print(clipped["highway"].value_counts())

    print("\n--- Cobertura da tag sidewalk= (calçada) ---")
    print(clipped["sidewalk"].value_counts(dropna=False))
    pct_with_sidewalk_tag = clipped["sidewalk"].notna().mean() * 100
    print(f"% de vias com tag sidewalk=: {pct_with_sidewalk_tag:.1f}%")

    print("\n--- oneway= (pistas separadas / mão única) ---")
    print(clipped["oneway"].value_counts(dropna=False))

    clipped_m = clipped.to_crs(TARGET_CRS)
    clipped_m["length_m"] = clipped_m.geometry.length
    total_km = clipped_m["length_m"].sum() / 1000
    print(f"\nExtensão total da malha (todas as classes): {total_km:.1f} km")

    relevant = clipped_m[
        clipped_m["highway"].isin(
            ["motorway", "trunk", "primary", "secondary", "tertiary", "residential", "unclassified", "living_street"]
        )
    ]
    print(f"Extensão das classes relevantes p/ plantio (vias de carro, exclui footway/path/etc.): "
          f"{relevant['length_m'].sum() / 1000:.1f} km em {len(relevant)} segmentos")

    out_path = DATA_DIR / "sorocaba_highways_clipped.gpkg"
    clipped_m.to_file(out_path, layer="highways", driver="GPKG")
    print(f"\nSalvo -> {out_path}")


if __name__ == "__main__":
    main()
