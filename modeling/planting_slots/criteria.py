"""Official planting criteria, per tree size (porte), from the MMA/GEOCAU
document "Informações sobre distanciamento" (final definitions, 2026-10).

Everything in meters. Dimensions the pipeline cannot check with open data
(planter shapes, underground networks) are not here — see the methodology
page for the full list of what is and is not applied.
"""

from __future__ import annotations

PORTES = ("pequeno", "medio", "grande")


def by_porte(pequeno: float, medio: float, grande: float) -> dict[str, float]:
    return {"pequeno": pequeno, "medio": medio, "grande": grande}


# 1) Minimum distance between two trees.
SPACING_BY_PORTE_M = by_porte(4.0, 6.0, 10.0)

# The project default: one spacing for every porte, the largest one (10 m).
GENERIC_SPACING_M = SPACING_BY_PORTE_M["grande"]

# 2) Minimum width of the sidewalk (a slot is not placed where the facade
# leaves less than this between the curb and the building) and of the planting
# strip / median strip (canteiro) per porte.
MIN_SIDEWALK_WIDTH_BY_PORTE_M = by_porte(1.5, 2.1, 2.8)
MIN_PLANTER_WIDTH_BY_PORTE_M = by_porte(0.45, 0.75, 1.45)

# 4) Minimum longitudinal clearance from a tree to each element of the urban
# infrastructure. Keys are the names the allocator reports as the reason a
# candidate was dropped (see OBSTACLES in scripts/03_allocate_slots.py).
CLEARANCE_M = {
    "esquina": by_porte(5, 5, 5),
    "semaforo": by_porte(10, 10, 10),  # signalized intersection
    "travessia": by_porte(1, 2, 2),  # lowered curb / pedestrian crossing
    "acesso_garagem": by_porte(2, 2, 2),  # vehicle entrance
    "poste": by_porte(3, 4, 5),  # pole or public lighting
    "transformador": by_porte(5, 8, 12),
    "rede_subterranea": by_porte(1, 2, 2),  # no open data: not applied
    "boca_de_lobo": by_porte(2, 2, 3),  # storm drain / inspection box
    "ponto_onibus": by_porte(1.5, 2, 2),
}
CORNER_SETBACK_M = max(CLEARANCE_M["esquina"].values())
