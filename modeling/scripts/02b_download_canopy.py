"""Download the tree-canopy cover raster for a municipality's bbox. Optional
step, run after 01. Two sources (--fonte): gfw_10m (GFW Tropical Tree Cover,
10 m, 2020 — the default; no data in Santa Catarina and the extreme south of
Rio Grande do Sul) and glad_30m (GLAD Global Tree Cover, 30 m, 2010).

gfw_10m needs the Global Forest Watch API key (the one in the download links
of the Tropical_Tree_Cover shapefile) in the GFW_API_KEY environment variable
— it is not stored in the repository. Municipalities that cross a 10-degree
tile border are not supported yet.
"""

from __future__ import annotations

import json
import os

from planting_slots.canopy import CANOPY_SOURCES, download_canopy
from planting_slots.municipalities import get_municipality, municipality_arg_parser
from planting_slots.paths import MunicipalityFiles


def main() -> None:
    parser = municipality_arg_parser(__doc__)
    parser.add_argument("--fonte", choices=sorted(CANOPY_SOURCES), default="gfw_10m", help="Raster de copa")
    args = parser.parse_args()
    files = MunicipalityFiles(get_municipality(args.municipio))
    path = files.canopy_rasters[args.fonte]
    if path.exists():
        print(f"[copa] já existe: {path}")
        return
    if not files.bbox.exists():
        raise SystemExit("Rode antes o 01_download_osm.py para este município.")
    api_key = os.environ.get("GFW_API_KEY")
    if args.fonte == "gfw_10m" and not api_key:
        raise SystemExit("Defina GFW_API_KEY (chave dos links de download do shapefile Tropical_Tree_Cover).")
    download_canopy(json.loads(files.bbox.read_text()), path, args.fonte, api_key)
    print(f"[copa] salvo -> {path}")


if __name__ == "__main__":
    main()
