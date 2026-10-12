"""Visual sanity check for v1: streets + planting slots (colored by porte),
median points highlighted, in a sample window near the Sorocaba center."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_DIR = Path(__file__).resolve().parent.parent / "output"
TARGET_CRS = "EPSG:31983"

CENTER_LON, CENTER_LAT = -47.480000, -23.459483  # Avenida Itavuvu (pista dupla)
HALF_WINDOW_M = 350

PORTE_COLOR = {"pequeno": "#66bb6a", "medio": "#1565c0", "grande": "#6a1b9a"}


def main() -> None:
    ways = gpd.read_file(DATA_DIR / "sorocaba_highways_clipped.gpkg", layer="highways")
    points = gpd.read_file(OUT_DIR / "sorocaba_vagas_plantio_v1.gpkg", layer="vagas_plantio")

    center = gpd.GeoSeries(gpd.points_from_xy([CENTER_LON], [CENTER_LAT]), crs="EPSG:4326").to_crs(TARGET_CRS).iloc[0]
    window = center.buffer(HALF_WINDOW_M)

    ways_sub = ways[ways.intersects(window)]
    points_sub = points[points.within(window)]

    fig, ax = plt.subplots(figsize=(11, 11))
    ways_sub.plot(ax=ax, color="#bbbbbb", linewidth=1, zorder=1)

    sidewalks = points_sub[points_sub["slot_type"] == "calcada"]
    for porte, color in PORTE_COLOR.items():
        sub = sidewalks[sidewalks["porte"] == porte]
        sub.plot(ax=ax, color=color, markersize=7, zorder=2, label=f"calçada — porte {porte}")

    medians = points_sub[points_sub["slot_type"] == "canteiro_central"]
    if len(medians):
        medians.plot(ax=ax, color="#e65100", markersize=16, marker="D", zorder=3, label="canteiro central")

    ax.set_xlim(window.bounds[0], window.bounds[2])
    ax.set_ylim(window.bounds[1], window.bounds[3])
    ax.set_title("Sorocaba — amostra v1: vagas por porte + canteiro central")
    ax.legend(fontsize=8, loc="upper left")
    ax.set_aspect("equal")
    out_path = OUT_DIR / "sorocaba_sample_itavuvu_v1.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"Sub-amostra: {len(ways_sub)} vias, {len(points_sub)} pontos "
          f"({len(sidewalks)} calçada, {len(medians)} canteiro)")
    print(f"Salvo -> {out_path}")


if __name__ == "__main__":
    main()
