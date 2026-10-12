"""Visual sanity check for v1: streets + planting slots (colored by porte),
median points highlighted, in a sample window near the município's center."""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.municipios import get_municipio, municipio_arg_parser  # noqa: E402

HALF_WINDOW_M = 350

PORTE_COLOR = {"pequeno": "#66bb6a", "medio": "#1565c0", "grande": "#6a1b9a"}


def main() -> None:
    parser = municipio_arg_parser(__doc__)
    parser.add_argument("--center-lon", type=float, default=None)
    parser.add_argument("--center-lat", type=float, default=None)
    args = parser.parse_args()
    municipio = get_municipio(args.municipio)
    data_dir = Path(__file__).resolve().parent.parent / "data" / municipio.slug
    out_dir = Path(__file__).resolve().parent.parent / "output" / municipio.slug
    target_crs = municipio.epsg

    ways = gpd.read_file(data_dir / "highways_clipped.gpkg", layer="highways")
    points = gpd.read_file(out_dir / "vagas_plantio_v1.gpkg", layer="vagas_plantio")

    if args.center_lon is not None and args.center_lat is not None:
        center_lon, center_lat = args.center_lon, args.center_lat
    else:
        boundary = gpd.read_file(data_dir / "boundary.geojson").to_crs("EPSG:4326")
        centroid = boundary.geometry.iloc[0].centroid
        center_lon, center_lat = centroid.x, centroid.y

    center = gpd.GeoSeries(gpd.points_from_xy([center_lon], [center_lat]), crs="EPSG:4326").to_crs(target_crs).iloc[0]
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
    ax.set_title(f"{municipio.nome} — amostra v1: vagas por porte + canteiro central")
    ax.legend(fontsize=8, loc="upper left")
    ax.set_aspect("equal")
    out_path = out_dir / "sample_v1.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"Sub-amostra: {len(ways_sub)} vias, {len(points_sub)} pontos "
          f"({len(sidewalks)} calçada, {len(medians)} canteiro)")
    print(f"Salvo -> {out_path}")


if __name__ == "__main__":
    main()
