"""Quick visual sanity check (v0): streets + candidate planting points in a
small sample window near the município's center (not the whole city — could
be hundreds of thousands of points, unreadable and slow to render)."""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.municipios import get_municipio, municipio_arg_parser  # noqa: E402

HALF_WINDOW_M = 300


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
    points = gpd.read_file(out_dir / "vagas_plantio_v0.gpkg", layer="vagas_plantio")

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

    fig, ax = plt.subplots(figsize=(10, 10))
    ways_sub.plot(ax=ax, color="#999999", linewidth=1, zorder=1)
    for side, color in [("left", "#2e7d32"), ("right", "#c62828")]:
        sub = points_sub[points_sub["side"] == side]
        sub.plot(ax=ax, color=color, markersize=6, zorder=2, label=f"lado {side}")

    ax.set_xlim(window.bounds[0], window.bounds[2])
    ax.set_ylim(window.bounds[1], window.bounds[3])
    ax.set_title(f"{municipio.nome} — amostra: vias (cinza) + vagas candidatas de plantio (v0)")
    ax.legend()
    ax.set_aspect("equal")
    out_path = out_dir / "sample_centro.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"Via sub-amostra: {len(ways_sub)} segmentos, {len(points_sub)} pontos")
    print(f"Salvo -> {out_path}")


if __name__ == "__main__":
    main()
