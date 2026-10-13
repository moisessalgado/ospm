"""Download Overture Maps building footprints for a municipality's bbox.

Overture merges Google Open Buildings, Microsoft ML Buildings and OSM, giving
far better coverage in Brazilian cities than OSM alone. Run after 01.
"""

from __future__ import annotations

import json

from planting_slots.buildings import download_overture_buildings
from planting_slots.municipalities import get_municipality, municipality_arg_parser
from planting_slots.paths import MunicipalityFiles


def main() -> None:
    files = MunicipalityFiles(get_municipality(municipality_arg_parser(__doc__).parse_args().municipio))
    if files.buildings.exists():
        print(f"[edificações] já existe: {files.buildings}")
        return
    if not files.bbox.exists():
        raise SystemExit("Rode antes o 01_download_osm.py para este município.")
    download_overture_buildings(json.loads(files.bbox.read_text()), files.buildings)
    print(f"[edificações] salvo -> {files.buildings}")


if __name__ == "__main__":
    main()
