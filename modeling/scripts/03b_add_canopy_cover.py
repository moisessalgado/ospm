"""Add the canopy-cover attribute to every slot already allocated. Optional
step, run after 03 and before 04 (which exports it to the webmap when present).

For each slot, `cobertura_copa_pct` is the maximum canopy % among the raster
pixels within a buffer of half the slot's spacing (5 m for 10 m spacing; 3,
4 and 5.5 m for the pequeno/medio/grande porte under the porte spacing),
and `raio_copa_m` is that buffer radius. Nothing is removed: the value is
only an attribute, for filtering downstream. Null where the raster has no
data. Rewrites the variants' .gpkg/.geojson in place; rerunning 03 drops the
columns, so rerun this afterwards.
"""

from __future__ import annotations

import geopandas as gpd
import pandas as pd

from planting_slots.canopy import canopy_radius_m, max_canopy_within
from planting_slots.municipalities import get_municipality, municipality_arg_parser
from planting_slots.paths import MunicipalityFiles

METHODS = ["osm_eixo", "osm_calcada_lado", "osm_calcada", "face_ibge", "osm_overture"]
SPACINGS = ["10m", "porte"]


def main() -> None:
    files = MunicipalityFiles(get_municipality(municipality_arg_parser(__doc__).parse_args().municipio))
    if not files.canopy.exists():
        raise SystemExit(f"Raster de copa ausente ({files.canopy}); rode antes o 02b_download_canopy.py.")

    for method in METHODS:
        for spacing in SPACINGS:
            variant = f"{method}_{spacing}"
            gpkg = files.slots(variant, "gpkg")
            if not gpkg.exists():
                continue
            points = gpd.read_file(gpkg, layer="vagas_plantio")
            radius = canopy_radius_m(points, spacing)
            canopy = max_canopy_within(points, radius, files.canopy)
            points["cobertura_copa_pct"] = pd.array(canopy, dtype="Int16")
            points["raio_copa_m"] = radius
            points.to_file(gpkg, layer="vagas_plantio", driver="GPKG")
            points.to_crs("EPSG:4326").to_file(files.slots(variant, "geojson"), driver="GeoJSON")
            valid = points["cobertura_copa_pct"].dropna()
            print(f"[{variant}] {len(points)} vagas: copa média {valid.mean():.1f} %, "
                  f"{(valid >= 50).mean():.0%} com >= 50 %, {points['cobertura_copa_pct'].isna().sum()} sem dado")


if __name__ == "__main__":
    main()
