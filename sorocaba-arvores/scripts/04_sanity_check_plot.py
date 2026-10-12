"""Quick visual sanity check: streets + candidate planting points in a small
sample window near the Sorocaba city center (not the whole city — 768k points
would be unreadable and slow to render)."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_DIR = Path(__file__).resolve().parent.parent / "output"
TARGET_CRS = "EPSG:31983"

# Centro de Sorocaba, janela de ~600x600m
CENTER_LON, CENTER_LAT = -47.4578, -23.5015
HALF_WINDOW_M = 300


def main() -> None:
    ways = gpd.read_file(DATA_DIR / "sorocaba_highways_clipped.gpkg", layer="highways")
    points = gpd.read_file(OUT_DIR / "sorocaba_vagas_plantio_v0.gpkg", layer="vagas_plantio")

    center = gpd.GeoSeries(gpd.points_from_xy([CENTER_LON], [CENTER_LAT]), crs="EPSG:4326").to_crs(TARGET_CRS).iloc[0]
    window = center.buffer(HALF_WINDOW_M)

    ways_sub = ways[ways.intersects(window)]
    points_sub = points[points.within(window)]

    fig, ax = plt.subplots(figsize=(10, 10))
    ways_sub.plot(ax=ax, color="#999999", linewidth=1, zorder=1)
    for side, color in [("left", "#2e7d32"), ("right", "#c62828")]:
        sub = points_sub[points_sub["side"] == side]
        sub.plot(ax=ax, color=color, markersize=6, zorder=2, label=f"lado {side}")

    ax.set_xlim(window.bounds[0], window.bounds[2])
    ax.set_ylim(window.bounds[1], window.bounds[3])
    ax.set_title("Sorocaba — amostra: vias (cinza) + vagas candidatas de plantio (v0)")
    ax.legend()
    ax.set_aspect("equal")
    out_path = OUT_DIR / "sorocaba_sample_centro.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"Via sub-amostra: {len(ways_sub)} segmentos, {len(points_sub)} pontos")
    print(f"Salvo -> {out_path}")


if __name__ == "__main__":
    main()
