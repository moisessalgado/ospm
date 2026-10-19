"""Download the tree-canopy cover raster (GFW Tropical Tree Cover, 10 m) for a
municipality's bbox. Optional step, run after 01.

Needs the Global Forest Watch API key (the one in the download links of the
Tropical_Tree_Cover shapefile) in the GFW_API_KEY environment variable — it
is not stored in the repository. Municipalities that cross a 10-degree tile
border are not supported yet.
"""

from __future__ import annotations

import json
import os

from planting_slots.canopy import download_canopy
from planting_slots.municipalities import get_municipality, municipality_arg_parser
from planting_slots.paths import MunicipalityFiles


def main() -> None:
    files = MunicipalityFiles(get_municipality(municipality_arg_parser(__doc__).parse_args().municipio))
    if files.canopy.exists():
        print(f"[copa] já existe: {files.canopy}")
        return
    if not files.bbox.exists():
        raise SystemExit("Rode antes o 01_download_osm.py para este município.")
    api_key = os.environ.get("GFW_API_KEY")
    if not api_key:
        raise SystemExit("Defina GFW_API_KEY (chave dos links de download do shapefile Tropical_Tree_Cover).")
    download_canopy(json.loads(files.bbox.read_text()), files.canopy, api_key)
    print(f"[copa] salvo -> {files.canopy}")


if __name__ == "__main__":
    main()
