"""Allocate tree-planting slots — four comparable methods, two spacings each.

Methods (increasing use of data, decreasing distance from the carriageway):
  - face_ibge: the production method. Each slot is linked to an IBGE street
    face (sector, block, face, street name, position along the face), then
    clamped to the matched OSM road — see clamp_to_road. Slowest to compute,
    best metadata, and the only one with median (canteiro_central) slots.
  - osm_eixo: walks the raw OSM road centerline itself, no offset at all —
    slots sit in the middle of the street on purpose. A deliberately wrong
    baseline, kept only so the other methods can be judged against it.
  - osm_calcada: walks the raw OSM road centerline, offset to the estimated
    curb (carriageway_half_width + CURB_SETBACK_M) on both sides — no
    buildings involved. Cheap, but the offset is a guess: on an irregular
    block it can land the slot short of the real sidewalk or past it.
  - osm_calcada_lado: osm_calcada on one side of each road only — the side
    whose IBGE faces add up to fewer addresses (see choose_side_by_addresses).
  - osm_overture: same offset as osm_calcada, but computed on the OSM road
    already co-registered to the Overture buildings (see coregistration_field)
    and capped by the nearest facade (CURB_SETBACK_M short of it) — closest
    to face_ibge's safety without needing IBGE at all.

Every method's slots get IBGE metadata (sector/block/face, street name) from
the nearest street face within IBGE_MATCH_M, when there is one close enough
— see enrich_with_ibge. For face_ibge the metadata already comes from the
linked face and this is a no-op.

Finally, no two slots of the same variant (method + spacing) are closer than
the spacing for the larger-porte slot of the pair (10 m flat for the generic
spacing; 6/8/11 m by porte otherwise) — including between sidewalk and
median slots.

Every candidate dropped along the way is exported too (one
`<variante>_sem_alocacao.geojson` per variant, same porte it would have had),
tagged with why — "esquina" (corner setback), an obstacle name (see
OBSTACLES), "sem_espaco_seguro" (no safe room before the nearest facade),
"via_transversal" (cleared its own road but still landed inside a different,
intersecting one — see drop_crossing_road_points), or "espacamento_minimo"
(closer than the variant's spacing to a slot of another street, almost
always near a corner) — so the webmap can show
*where* and *why* a slot is missing instead of leaving an unexplained gap.
See blocked_intervals/allocate_face and allocate_from_roads/generate_face_ibge.
"""

from __future__ import annotations

import json
from collections import Counter

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import Point

from planting_slots.allocation import enforce_min_separation, greedy_walk
from planting_slots.buildings import facade_probes, load_overture_buildings, local_facade
from planting_slots.ibge import load_street_faces, load_urban_area
from planting_slots.municipalities import get_municipality, municipality_arg_parser
from planting_slots.osm import (
    CORNER_CLASSES, DEFAULT_WIDTH_BY_CLASS, angle_diff, bearing, carriageway_half_width, find_divided_pairs,
    junction_disks, load_osm_elements,
)
from planting_slots.paths import OSM_ROADS_LAYER, MunicipalityFiles
from planting_slots.registration import (
    apply_shift, coregistration_field, ibge_registration_uncertainty, normalize_street_name,
)

# --- Setbacks (MMA "Coletânea Brasileira de Arborização Urbana", 2026) -------
CORNER_SETBACK_M = 6.0
CURB_SETBACK_M = 0.5
PIT_HALF_WIDTH_M = 0.5
SERVICE_SETBACK_M = 1.5
SAMPLE_STEP_M = 1.0

OBSTACLES = {  # name -> (OSM tag filter, exclusion radius in m)
    "travessia": (lambda t: t.get("footway") == "crossing" or t.get("highway") == "crossing", 3.0),
    "acesso_garagem": (lambda t: t.get("amenity") == "parking_entrance" or t.get("service") == "driveway", 2.0),
    "ponto_onibus": (lambda t: t.get("highway") == "bus_stop" or t.get("public_transport") == "platform", 12.0),
    "arvore_existente": (lambda t: t.get("natural") == "tree", 4.0),
}
OBSTACLE_TO_FACE_M = 8.0  # OSM obstacles within this distance are projected onto the face

# --- Face -> road matching -------------------------------------------------------
FACE_STREET_MATCH_M = 15.0
FACE_STREET_MAX_ANGLE_DEG = 30.0
EXCLUDED_FACE_TYPES = {"VIELA", "BECO", "ESCADARIA", "TRILHOS", "MURO", "FUNDOS"}
GENERATION_CLASSES = {
    "living_street", "residential", "unclassified", "tertiary", "tertiary_link",
    "secondary", "secondary_link", "primary", "primary_link",
}

# --- Tree size by road class (sidewalk width is not measurable) ---------------
PORTE_BY_CLASS = {
    "living_street": "pequeno", "residential": "medio", "unclassified": "medio",
    "tertiary": "medio", "tertiary_link": "medio",
    "secondary": "grande", "secondary_link": "grande",
    "primary": "grande", "primary_link": "grande",
}
MEDIAN_PORTE_BY_WIDTH = [(2.0, "pequeno"), (3.5, "medio"), (np.inf, "grande")]
MIN_MEDIAN_WIDTH_M = 1.0

# --- Spacing variants (pending MMA criteria) -----------------------------------
SPACINGS = {
    "10m": {"label": "Espaçamento genérico de 10 m", "spacing": lambda porte: 10.0},
    "porte": {"label": "Espaçamento por porte (6/8/11 m)",
             "spacing": lambda porte: {"pequeno": 6.0, "medio": 8.0, "grande": 11.0}[porte]},
}

# --- Methods ------------------------------------------------------------------
# face_ibge is handled by its own (pre-existing) code path below; these three
# share allocate_from_roads, parametrized by whether they offset off the
# centerline at all, and whether that offset is capped by a building facade.
ROAD_METHODS = {
    "osm_eixo": {"label": "Eixo da via (OSM)", "offset": False, "clamp_building": False},
    "osm_calcada": {"label": "Calçada alocada nos dois lados da via (OSM)", "offset": True, "clamp_building": False},
    "osm_calcada_lado": {"label": "Calçada alocada em um dos lados da via (OSM)", "offset": True,
                         "clamp_building": False, "one_side": True},
    "osm_overture": {"label": "Calçada ajustada com prédios (OSM + Overture)", "offset": True, "clamp_building": True},
}
IBGE_MATCH_M = 25.0  # metadata-only nearest-face search radius for non-face_ibge methods


def load_ways(files: MunicipalityFiles) -> gpd.GeoDataFrame:
    ways = gpd.read_file(files.osm_roads, layer=OSM_ROADS_LAYER)
    ways = ways.explode(index_parts=False).reset_index(drop=True)
    return ways[(ways.geometry.geom_type == "LineString") & (ways.geometry.length > 1.0)].copy()


CHAIN_NODE_TOL_M = 0.05


def build_road_chains(roads: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Generation-class roads as continuous chains, for the road-walking
    methods. OSM splits a street into many ways wherever any tag changes
    (bridge, surface, speed limit...), and each split used to restart the
    spacing walk and get a 6 m corner setback on both sides — a gap of 12 m
    or more where there is no corner at all. Here consecutive ways that meet
    end-to-end with nothing else touching (a plain split) and share name,
    class and carriageway width are merged into one line; and each chain end
    is flagged as a corner only if it is an actual intersection — a node
    shared by 3+ streets, or a T onto another street's middle — not a split
    or a dead end."""
    corner_roads = roads[roads["highway"].isin(CORNER_CLASSES)]
    clabels = corner_roads.index.to_numpy()
    tree = shapely.STRtree(corner_roads.geometry.to_numpy())
    gen = roads[roads["highway"].isin(GENERATION_CLASSES)]
    names = {lbl: (n if isinstance(n, str) else None) for lbl, n in gen["name"].items()}
    group = {lbl: (h, round(float(w), 3), names[lbl]) for lbl, h, w in zip(gen.index, gen["highway"], gen["half"])}

    parent = {lbl: lbl for lbl in gen.index}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def key(c):
        return round(c[0], 2), round(c[1], 2)

    corner_at = {}
    for lbl, g in gen.geometry.items():
        for c in (g.coords[0], g.coords[-1]):
            pt = Point(c)
            others = [o for o in clabels[tree.query(pt, predicate="dwithin", distance=CHAIN_NODE_TOL_M)] if o != lbl]
            if not others:
                corner_at[key(c)] = False  # dead end
            elif len(others) == 1:
                og = roads.geometry.loc[others[0]]
                split = min(Point(og.coords[0]).distance(pt), Point(og.coords[-1]).distance(pt)) <= CHAIN_NODE_TOL_M
                corner_at[key(c)] = not split  # T onto another street's middle is a corner; a split is not
                if split and others[0] in parent and group[others[0]] == group[lbl]:
                    parent[find(lbl)] = find(others[0])
            else:
                corner_at[key(c)] = True

    members = {}
    for lbl in gen.index:
        members.setdefault(find(lbl), []).append(lbl)
    rows = []
    for labels in members.values():
        geoms = [gen.geometry.loc[m] for m in labels]
        merged = shapely.line_merge(shapely.MultiLineString(geoms)) if len(geoms) > 1 else geoms[0]
        first = gen.loc[labels[0]]
        for part in ([merged] if merged.geom_type == "LineString" else list(merged.geoms)):
            rows.append({
                "highway": first["highway"], "half": first["half"], "name": names[labels[0]],
                "start_corner": corner_at.get(key(part.coords[0]), True),
                "end_corner": corner_at.get(key(part.coords[-1]), True),
                "geometry": part,
            })
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=roads.crs)


def match_faces_to_streets(faces: gpd.GeoDataFrame, roads: gpd.GeoDataFrame) -> pd.DataFrame:
    """Nearest parallel vehicle road to each face (by the face midpoint)."""
    mid = faces.geometry.interpolate(0.5, normalized=True).values
    fi, ri = shapely.STRtree(roads.geometry.values).query(mid, predicate="dwithin", distance=FACE_STREET_MATCH_M)
    face_bearing = np.array([bearing(g) for g in faces.geometry])
    rows = []
    for f, r in zip(fi, ri):
        road = roads.geometry.values[r]
        pos = road.project(mid[f])
        a = road.interpolate(max(pos - 5, 0))
        b = road.interpolate(min(pos + 5, road.length))
        local = float(np.degrees(np.arctan2(b.y - a.y, b.x - a.x))) % 360
        diff = min(angle_diff(face_bearing[f], local), angle_diff(face_bearing[f], (local + 180) % 360))
        if diff <= FACE_STREET_MAX_ANGLE_DEG:
            rows.append((f, r, road.distance(mid[f])))
    match = pd.DataFrame(rows, columns=["face", "road", "dist"]).sort_values("dist").drop_duplicates("face")
    return match.set_index("face")


def local_bearing(line, pos: float, probe: float = 2.0) -> float:
    """Direction of travel (degrees) right around arc-length pos — used to
    tell a genuine crossing street apart from the next OSM way segment of
    the *same* street (see drop_crossing_road_points), which shares roughly
    the same bearing where they meet."""
    a = line.interpolate(max(pos - probe, 0.0))
    b = line.interpolate(min(pos + probe, line.length))
    return float(np.degrees(np.arctan2(b.y - a.y, b.x - a.x))) % 360


def blocked_intervals(line, obstacles: dict[str, np.ndarray], start_corner: bool = True,
                      end_corner: bool = True) -> list[tuple[float, float, str]]:
    """Intervals (m from the line start) where a slot can't go, each tagged
    with why — the corner setback at each end that is an actual intersection
    (see build_road_chains), plus a radius around every nearby obstacle. Fed
    to allocate_face, which uses the tag to report not just that a candidate
    was dropped there but which rule dropped it."""
    length = line.length
    out = []
    if start_corner:
        out.append((0.0, CORNER_SETBACK_M, "esquina"))
    if end_corner:
        out.append((max(length - CORNER_SETBACK_M, 0.0), length, "esquina"))
    for name, (geoms, radius) in obstacles.items():
        if not len(geoms):
            continue
        near = geoms[shapely.dwithin(geoms, line, OBSTACLE_TO_FACE_M)]
        for g in near:
            p = line.project(g.centroid if g.geom_type != "Point" else g)
            out.append((p - radius, p + radius, name))
    return out


SPACING_TOL_M = 1e-6  # float slack: a chord of 9.9999999996 m counts as 10 m
ADVANCE_STEP_M = 0.1


def advance(line, pos: float, spacing: float, offset: float = 0.0, sides=(True, False)) -> float:
    """The first arc-length after pos whose straight-line distance to the
    point at pos is at least `spacing` — on a straight road exactly pos +
    spacing; on a bend a little more, since the chord is shorter than the
    arc. With an offset, the check is on the points shifted to both sides
    (the inner side of a bend is shorter than the centerline). Walking by
    chord (not arc) is what keeps consecutive slots from being thrown out
    later by enforce_min_separation, which measures straight-line distance."""
    sides = list(sides) if offset else [None]

    def at(p, side):
        return point_at_offset(line, p, side, offset) if offset else line.interpolate(p)

    origins = [at(pos, s) for s in sides]
    p = pos + spacing
    while p <= line.length and any(at(p, s).distance(o) < spacing - SPACING_TOL_M for s, o in zip(sides, origins)):
        p += ADVANCE_STEP_M
    return p


def allocate_face(line, blocked, spacing_at, offset: float = 0.0,
                  sides=(True, False)) -> tuple[list[float], list[tuple[float, str]]]:
    """Positions (m from the line start) of the slots on one face/road, and
    the positions+reasons where a slot was attempted but excluded by a
    blocked interval (corner, obstacle — see blocked_intervals) instead of
    silently leaving a gap. Each slot is placed `spacing` (straight-line, see
    advance) after the previous one; a blocked candidate is recorded and the
    walk resumes at whichever comes first — the next spacing step, or the
    end of the blocked stretch — so an obstacle only costs the slots it
    actually covers, not a whole extra spacing after it."""
    length = line.length
    placed, rejected = [], []
    pos = 0.0
    while pos <= length + 1e-9:
        spacing = spacing_at(pos)
        hits = [(a, b, m) for a, b, m in blocked if a <= pos <= b]
        if not hits:
            placed.append(pos)
            pos = advance(line, pos, spacing, offset, sides)
            continue
        rejected.append((pos, hits[0][2]))
        end = max(b for _, b, _ in hits)
        grew = True
        while grew:  # overlapping intervals chain into one blocked stretch
            more = [b for a, b, _ in blocked if a <= end < b]
            grew = bool(more)
            end = max([end, *more])
        pos = min(advance(line, pos, spacing, offset, sides), end + ADVANCE_STEP_M)
    return placed, rejected


CLAMP_MAX_ITER = 6


def clamp_to_road(point: Point, road, min_clear: float, probe_entry) -> Point | None:
    """Push a face-derived point, perpendicular to the road, so it clears the
    carriageway (min_clear) without crossing the nearest building facade.
    None if there is no room between the two, or if a safe spot can't be
    found within CLAMP_MAX_ITER tries (a bend or junction nearby can put the
    pushed point closer to a *different* stretch of the same road than the
    one it was projected from) — skipping the slot there is safer than
    risking a point still inside the carriageway."""
    pos_arr, left_d, right_d = probe_entry
    p = point
    for _ in range(CLAMP_MAX_ITER):
        proj = road.project(p)
        r = road.interpolate(proj)
        vx, vy = p.x - r.x, p.y - r.y
        dist = float(np.hypot(vx, vy))

        ahead = road.interpolate(min(proj + 0.5, road.length))
        behind = road.interpolate(max(proj - 0.5, 0.0))
        tx, ty = ahead.x - behind.x, ahead.y - behind.y
        tn = np.hypot(tx, ty) or 1.0
        tx, ty = tx / tn, ty / tn
        nx, ny = -ty, tx  # left unit normal

        if dist < 1e-6:
            ux, uy = nx, ny
            side_left = True
        else:
            ux, uy = vx / dist, vy / dist
            side_left = (ux * nx + uy * ny) >= 0

        facade_d = np.nan
        if len(pos_arr):
            facade_d = local_facade(np.array([proj]), pos_arr, left_d if side_left else right_d)[0]
        ceiling = (facade_d - CURB_SETBACK_M) if not np.isnan(facade_d) else np.inf
        if min_clear > ceiling:
            return None
        target = min(max(dist, min_clear), ceiling)
        candidate = Point(r.x + ux * target, r.y + uy * target)
        if road.distance(candidate) >= min_clear - 1e-6:
            return candidate
        p = candidate  # too close to a different stretch of the road: re-project and retry
    return None


def point_at_offset(road, pos: float, side_left: bool, offset: float) -> Point:
    """The point offset perpendicular to the road at arc-length pos, on the
    given side, by offset metres (0 = on the centerline)."""
    r = road.interpolate(pos)
    ahead = road.interpolate(min(pos + 0.5, road.length))
    behind = road.interpolate(max(pos - 0.5, 0.0))
    tx, ty = ahead.x - behind.x, ahead.y - behind.y
    tn = np.hypot(tx, ty) or 1.0
    nx, ny = -ty / tn, tx / tn
    sign = 1 if side_left else -1
    return Point(r.x + sign * nx * offset, r.y + sign * ny * offset)


def allocate_from_roads(roads: gpd.GeoDataFrame, probes: dict, obstacles: dict, offset: bool,
                        clamp_building: bool, spacing_fn, sides: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Slots walked directly along eligible roads (no IBGE face involved),
    for the OSM-only comparison methods — see module docstring. Also
    returns every candidate dropped along the way — by the corner setback,
    by a nearby obstacle (see blocked_intervals), or for lack of safe room
    between the carriageway and the nearest facade (motivo=sem_espaco_seguro)
    — at its best-guess position, so the webmap can show *where* and *why*
    a slot is missing instead of just leaving a silent gap. `sides`, when
    given, maps a road to the one side (True = left) that gets slots — see
    choose_side_by_addresses; otherwise both sides do."""
    empty_probe = (np.array([]), np.array([]), np.array([]))
    records, rejected = [], []
    for idx, row in roads.iterrows():
        porte = PORTE_BY_CLASS[row["highway"]]
        road_geom = row.geometry
        street_name = normalize_street_name(row.get("name"))
        floor = row["half"] + CURB_SETBACK_M
        side_options = ([sides[idx]] if sides is not None and idx in sides else [True, False]) if offset else [None]
        blocked = blocked_intervals(road_geom, obstacles, row["start_corner"], row["end_corner"])
        positions, blocked_rej = allocate_face(road_geom, blocked, lambda _: spacing_fn(porte),
                                               floor if offset else 0.0, side_options)
        probe_entry = probes.get(idx, empty_probe) if clamp_building else empty_probe

        for pos, motivo in blocked_rej:
            for side_left in side_options:
                point = point_at_offset(road_geom, pos, side_left, floor) if offset else road_geom.interpolate(pos)
                rejected.append({"motivo": motivo, "porte": porte, "logradouro": street_name, "geometry": point})

        for pos in positions:
            own_bearing = local_bearing(road_geom, pos)
            for side_left in side_options:
                if not offset:
                    point = road_geom.interpolate(pos)
                else:
                    ceiling = np.inf
                    if clamp_building and len(probe_entry[0]):
                        facade_d = local_facade(np.array([pos]), probe_entry[0],
                                                probe_entry[1] if side_left else probe_entry[2])[0]
                        ceiling = (facade_d - CURB_SETBACK_M) if not np.isnan(facade_d) else np.inf
                    if floor > ceiling:
                        rejected.append({"motivo": "sem_espaco_seguro", "porte": porte,
                                        "logradouro": street_name, "geometry": road_geom.interpolate(pos)})
                        continue
                    point = point_at_offset(road_geom, pos, side_left, floor)
                    if clamp_building and road_geom.distance(point) < floor - 1e-6:
                        rejected.append({"motivo": "sem_espaco_seguro", "porte": porte,
                                        "logradouro": street_name, "geometry": point})
                        continue
                records.append({
                    "tipo_vaga": "calcada" if offset else "eixo_via", "porte": porte, "via_osm": row["highway"],
                    "logradouro": street_name, "bearing": own_bearing,
                    "cd_setor": None, "cd_quadra": None, "cd_face": None, "posicao_face_m": round(pos, 1),
                    "posicao_lateral": "sobre o eixo da via" if not offset else
                                      ("deslocada e validada contra a fachada mais próxima (Overture)" if clamp_building else
                                       "deslocada por largura estimada, só no lado com menos endereços (IBGE)"
                                       if sides is not None else "deslocada por largura estimada (sem prédios)"),
                    "geometry": point,
                })
    return records, rejected


def choose_side_by_addresses(chains: gpd.GeoDataFrame, faces: gpd.GeoDataFrame) -> tuple[dict, Counter]:
    """For the one-side variant: which side of each road (True = left of
    its drawn direction) gets slots. The IBGE street faces along the road
    (within FACE_STREET_MATCH_M and roughly parallel, each face counted for
    its nearest road only) are split by side, and the side whose faces add
    up to fewer addresses (TOT_GERAL) wins — fewer gates and garage
    entrances to work around. If only one side has an IBGE face (a street
    along a square, a river, an empty lot), that side; if neither does, the
    left side (no IBGE data to go on)."""
    mid = faces.geometry.interpolate(0.5, normalized=True).to_numpy()
    face_bearing = np.array([bearing(g) for g in faces.geometry])
    total = faces["TOT_GERAL"].fillna(0).to_numpy()
    geoms = chains.geometry.to_numpy()
    fi, ci = shapely.STRtree(geoms).query(mid, predicate="dwithin", distance=FACE_STREET_MATCH_M)
    best = {}  # face -> (distance, chain, left?)
    for f, c in zip(fi, ci):
        road = geoms[c]
        pos = road.project(mid[f])
        local = local_bearing(road, pos)
        if min(angle_diff(face_bearing[f], local), angle_diff(face_bearing[f], (local + 180) % 360)) > FACE_STREET_MAX_ANGLE_DEG:
            continue
        d = road.distance(mid[f])
        if f in best and best[f][0] <= d:
            continue
        r = road.interpolate(pos)
        t = np.radians(local)
        best[f] = (d, c, np.cos(t) * (mid[f].y - r.y) - np.sin(t) * (mid[f].x - r.x) > 0)
    tot = np.zeros((len(chains), 2))
    seen = np.zeros((len(chains), 2), dtype=bool)
    for f, (_, c, left) in best.items():
        tot[c, int(left)] += total[f]
        seen[c, int(left)] = True
    sides, how = {}, Counter()
    for c, idx in enumerate(chains.index):
        right_seen, left_seen = seen[c]
        if left_seen and right_seen:
            sides[idx] = bool(tot[c, 1] <= tot[c, 0])
            how["menos endereços"] += 1
        elif left_seen or right_seen:
            sides[idx] = bool(left_seen)
            how["único lado com face IBGE"] += 1
        else:
            sides[idx] = True
            how["sem face IBGE (lado esquerdo)"] += 1
    return sides, how


def enrich_with_ibge(points: gpd.GeoDataFrame, faces: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Attach IBGE sector/block/face and, when the slot has no OSM street
    name, the IBGE one — from the nearest street face within IBGE_MATCH_M.
    Metadata only: it never moves a point. A no-op for rows that already
    have cd_setor (the face_ibge method links its own face upstream)."""
    todo = points[points["cd_setor"].isna()]
    if not todo.empty:
        cols = ["CD_SETOR", "CD_QUADRA", "CD_FACE", "NM_TIP_LOG", "NM_TIT_LOG", "NM_LOG", "geometry"]
        nearest = gpd.sjoin_nearest(todo, faces[cols], max_distance=IBGE_MATCH_M, how="left", distance_col="_d")
        nearest = nearest[~nearest.index.duplicated(keep="first")]
        points.loc[todo.index, "cd_setor"] = nearest["CD_SETOR"]
        points.loc[todo.index, "cd_quadra"] = nearest["CD_QUADRA"]
        points.loc[todo.index, "cd_face"] = nearest["CD_FACE"]
        ibge_name = [normalize_street_name(t, ti, n)
                    for t, ti, n in zip(nearest["NM_TIP_LOG"], nearest["NM_TIT_LOG"], nearest["NM_LOG"])]
        has_name = points.loc[todo.index, "logradouro"].astype(bool).to_numpy()
        points.loc[todo.index, "logradouro"] = np.where(has_name, points.loc[todo.index, "logradouro"], ibge_name)
    return points


def build_road_buffer_tree(roads: gpd.GeoDataFrame) -> shapely.STRtree:
    """An STRtree of every eligible road's carriageway, each buffered out to
    where a slot must clear it (half-width + CURB_SETBACK_M) — not just the
    generation-class roads a slot is linked to, so a cross street of any
    class still counts. Used to catch a slot that cleared *its own* matched
    road but, near a junction, still lands inside a different, intersecting
    one (see drop_crossing_road_points)."""
    return shapely.STRtree(list(roads.geometry.buffer((roads["half"] + CURB_SETBACK_M).to_numpy())))


CROSSING_ANGLE_MIN_DEG = 45.0  # above this, a road the point falls inside of counts as an actual crossing street


def drop_crossing_road_points(points: gpd.GeoDataFrame, tree: shapely.STRtree,
                              road_geoms: np.ndarray) -> tuple[gpd.GeoDataFrame, list[dict]]:
    """Points that cleared their own matched/source road but still fall
    inside some *other* road's carriageway — almost always a slot pushed
    sideways near a junction landing in the middle of a crossing street.
    `tree` indexes buffered `road_geoms` (see build_road_buffer_tree); a hit
    only counts if that road's local bearing differs from the point's own
    (points["bearing"], set when the record was built) by at least
    CROSSING_ANGLE_MIN_DEG — the next OSM way segment of the *same* street
    hits the same buffer test (two consecutive segments' buffers overlap
    near the shared node) but shares roughly the same bearing there, so it's
    not flagged. Dropped points come back as motivo=via_transversal so they
    still show up in the webmap's "vagas inviáveis" layer instead of
    silently vanishing."""
    if not len(points):
        return points, []
    hit_pt, hit_road = tree.query(points.geometry.to_numpy(), predicate="within")
    if not len(hit_pt):
        return points, []
    pts = points.geometry.to_numpy()
    own_bearing = points["bearing"].to_numpy()
    bad_mask = np.zeros(len(points), dtype=bool)
    for pi, ri in zip(hit_pt, hit_road):
        if bad_mask[pi]:
            continue
        road = road_geoms[ri]
        hit_bearing = local_bearing(road, road.project(pts[pi]))
        diff = min(angle_diff(own_bearing[pi], hit_bearing), angle_diff(own_bearing[pi], (hit_bearing + 180) % 360))
        if diff >= CROSSING_ANGLE_MIN_DEG:
            bad_mask[pi] = True
    if not bad_mask.any():
        return points, []
    bad = points[bad_mask]
    rejected = [{"motivo": "via_transversal", "porte": row.porte, "logradouro": row.logradouro,
                "geometry": row.geometry} for row in bad.itertuples()]
    return points[~bad_mask].reset_index(drop=True), rejected


def main() -> None:
    municipality = get_municipality(municipality_arg_parser(__doc__).parse_args().municipio)
    files = MunicipalityFiles(municipality)
    files.ensure_dirs()
    crs = municipality.epsg

    urban_area = load_urban_area(files, crs)
    faces = load_street_faces(files, crs)
    mid = faces.geometry.interpolate(0.5, normalized=True)
    faces = faces[shapely.contains_xy(urban_area, mid.x.to_numpy(), mid.y.to_numpy())].reset_index(drop=True)
    print(f"Faces de logradouro em setores urbanos: {len(faces)}")

    # --- OSM co-registered to the buildings (road class, obstacles, medians)
    ways = load_ways(files)
    ways_raw_geometry = ways.geometry.copy()  # pre-shift: osm_eixo/osm_calcada use raw OSM, not Overture-corrected
    pairs = find_divided_pairs(ways)
    ways["paired"] = ways.index.isin(pairs.keys())
    buildings = load_overture_buildings(files.buildings, crs)
    btree, bgeoms = shapely.STRtree(buildings.geometry.values), buildings.geometry.values
    eligible = ways[ways["highway"].isin(GENERATION_CLASSES)]
    probes = {idx: facade_probes(g, btree, bgeoms) for idx, g in eligible.geometry.items()}
    gx, gy, shift, _ = coregistration_field(ways.loc[eligible.index], probes)
    ways["geometry"] = apply_shift(ways.geometry.values, gx, gy, shift)
    uncertainty, n_ways, _ = ibge_registration_uncertainty(ways.loc[eligible.index], faces)
    print(f"Incerteza de registro OSM x IBGE (p90, {n_ways} vias): {uncertainty:.1f} m")

    obstacles = {}
    obstacles_raw = {}
    for name, (flt, radius) in OBSTACLES.items():
        geoms = load_osm_elements(files.osm_obstacles, flt, crs).geometry.values
        obstacles_raw[name] = (geoms, radius)
        obstacles[name] = (apply_shift(geoms, gx, gy, shift), radius)

    roads = ways[ways["highway"].isin(DEFAULT_WIDTH_BY_CLASS)].copy()
    roads["half"] = [carriageway_half_width(r, p, np.nan, municipality.min_sidewalk_width_m)[0]
                     for r, p in zip(roads.to_dict("records"), roads["paired"])]
    roads_raw = roads.copy()
    roads_raw["geometry"] = ways_raw_geometry.loc[roads.index]
    road_chains = build_road_chains(roads)
    road_chains_raw = build_road_chains(roads_raw)
    chain_probes = {i: facade_probes(g, btree, bgeoms) for i, g in road_chains.geometry.items()}
    chain_sides_raw, side_how = choose_side_by_addresses(road_chains_raw, faces)
    print("Lado único (osm_calcada_lado):", dict(side_how))
    print(f"Vias geradoras: {int(roads['highway'].isin(GENERATION_CLASSES).sum())} trechos OSM -> "
          f"{len(road_chains)} vias contínuas")
    road_tree = build_road_buffer_tree(roads)
    road_tree_raw = build_road_buffer_tree(roads_raw)
    road_geoms = roads.geometry.to_numpy()
    road_geoms_raw = roads_raw.geometry.to_numpy()

    # --- sidewalk slots from the faces ------------------------------------------
    match = match_faces_to_streets(faces, roads)
    faces["highway"] = pd.Series(roads["highway"].to_numpy()[match["road"].to_numpy()], index=match.index)
    faces["tipo"] = faces["NM_TIP_LOG"].fillna("").str.upper()
    face_ok = faces["highway"].isin(GENERATION_CLASSES) & ~faces["tipo"].isin(EXCLUDED_FACE_TYPES)
    summary = {
        "faces_urbanas": len(faces),
        "faces_sem_via_osm": int(faces["highway"].isna().sum()),
        "faces_via_nao_elegivel": int((faces["highway"].notna() & ~faces["highway"].isin(GENERATION_CLASSES)).sum()),
        "faces_tipo_excluido": int(faces["tipo"].isin(EXCLUDED_FACE_TYPES).sum()),
        "faces_elegiveis": int(face_ok.sum()),
    }
    print("Faces:", summary)
    sel = faces[face_ok]
    blocked = {i: blocked_intervals(g, obstacles) for i, g in sel.geometry.items()}

    # Per-face context to clamp the lateral position to the matched road (see
    # clamp_to_road): the road geometry, its nominal carriageway half-width,
    # and the facade-probe arrays already computed above for that road.
    empty_probe = (np.array([]), np.array([]), np.array([]))
    sel_road_pos = match.loc[sel.index, "road"].to_numpy()
    sel_road_label = roads.index.to_numpy()[sel_road_pos]
    clamp_ctx = {
        i: (roads.geometry.to_numpy()[p], roads["half"].to_numpy()[p] + CURB_SETBACK_M, probes.get(lbl, empty_probe))
        for i, p, lbl in zip(sel.index, sel_road_pos, sel_road_label)
    }

    # --- median slots (OSM, worst case) -----------------------------------------
    roads["half_worst"] = roads["half"] + uncertainty
    margin = np.where(roads["highway"] == "service", SERVICE_SETBACK_M, CURB_SETBACK_M)
    exclusion = list(roads.geometry.buffer(roads["half_worst"].to_numpy() + margin))
    exclusion += junction_disks(roads.assign(half=roads["half_worst"]), CORNER_SETBACK_M,
                                CURB_SETBACK_M + PIT_HALF_WIDTH_M)
    for geoms, radius in obstacles.values():
        exclusion += list(shapely.buffer(geoms, radius))
    etree = shapely.STRtree(exclusion)
    median_groups = []
    for a, b in {tuple(sorted(p)) for p in pairs.items()}:
        if ways.loc[a, "highway"] not in GENERATION_CLASSES:
            continue
        way_a, way_b = ways.geometry.loc[a], ways.geometry.loc[b]
        d = np.arange(SAMPLE_STEP_M / 2, way_a.length, SAMPLE_STEP_M)
        pts = shapely.line_interpolate_point(way_a, d)
        near = shapely.line_interpolate_point(way_b, shapely.line_locate_point(way_b, pts))
        mids = shapely.points((shapely.get_x(pts) + shapely.get_x(near)) / 2, (shapely.get_y(pts) + shapely.get_y(near)) / 2)
        width = shapely.distance(pts, near) - roads.loc[a, "half_worst"] - roads.loc[b, "half_worst"]
        ok = (width >= MIN_MEDIAN_WIDTH_M) & shapely.contains_xy(urban_area, shapely.get_x(mids), shapely.get_y(mids))
        hit = etree.query(mids, predicate="within")[0]
        ok[np.unique(hit)] = False
        median_groups.append({"row": ways.loc[a], "dists": d, "pts": mids, "width": width, "ok": ok})

    # --- allocation per method x spacing -----------------------------------------
    report = {
        "municipio": municipality.slug,
        "parametros": {
            "afastamento_esquina_m": CORNER_SETBACK_M,
            "incerteza_registro_canteiros_m": round(uncertainty, 2),
        },
        "faces": summary,
        "variantes": {},
    }

    def generate_face_ibge(spacing_fn):
        records, rejected, fora_de_alcance = [], [], 0
        for i, face in sel.iterrows():
            porte = PORTE_BY_CLASS[face["highway"]]
            road_geom, min_clear, probe_entry = clamp_ctx[i]
            street_name = normalize_street_name(face["NM_TIP_LOG"], face["NM_TIT_LOG"], face["NM_LOG"])
            positions, blocked_rej = allocate_face(face.geometry, blocked[i], lambda _: spacing_fn(porte))
            for pos, motivo in blocked_rej:
                rejected.append({"motivo": motivo, "porte": porte, "logradouro": street_name,
                                "geometry": face.geometry.interpolate(pos)})
            for pos in positions:
                candidate = face.geometry.interpolate(pos)
                point = clamp_to_road(candidate, road_geom, min_clear, probe_entry)
                if point is None:
                    fora_de_alcance += 1
                    rejected.append({"motivo": "sem_espaco_seguro", "porte": porte,
                                    "logradouro": street_name, "geometry": candidate})
                    continue
                records.append({
                    "tipo_vaga": "calcada", "porte": porte, "via_osm": face["highway"],
                    "logradouro": street_name, "bearing": local_bearing(road_geom, road_geom.project(point)),
                    "cd_setor": face["CD_SETOR"], "cd_quadra": face["CD_QUADRA"], "cd_face": face["CD_FACE"],
                    "posicao_face_m": round(pos, 1), "posicao_lateral": "indicativa (ajustada à via OSM)",
                    "geometry": point,
                })
        for g in median_groups:
            portes = [next(p for lim, p in MEDIAN_PORTE_BY_WIDTH if w < lim) for w in g["width"]]
            for i in greedy_walk(g["dists"], g["ok"], lambda i: spacing_fn(portes[i])):
                records.append({
                    "tipo_vaga": "canteiro_central", "porte": portes[i], "via_osm": g["row"]["highway"],
                    "logradouro": normalize_street_name(g["row"]["name"]),
                    "cd_setor": None, "cd_quadra": None, "cd_face": None, "posicao_face_m": None,
                    "posicao_lateral": "pior caso (OSM co-registrado)", "geometry": g["pts"][i],
                })
        return records, rejected, {"removidos_sem_espaco_seguro_ate_fachada": fora_de_alcance}

    for spacing_id, spacing_cfg in SPACINGS.items():
        spacing_fn = spacing_cfg["spacing"]

        def generate_road_method(spacing_fn, roads, obs, mcfg):
            records, rejected = allocate_from_roads(roads, chain_probes, obs, mcfg["offset"], mcfg["clamp_building"],
                                                    spacing_fn, chain_sides_raw if mcfg.get("one_side") else None)
            skipped = sum(1 for r in rejected if r["motivo"] == "sem_espaco_seguro")
            stats = {"removidos_sem_espaco_seguro_ate_fachada": skipped} if mcfg["clamp_building"] else {}
            return records, rejected, stats

        methods = {"face_ibge": generate_face_ibge}
        for method_id, mcfg in ROAD_METHODS.items():
            roads_for_method = road_chains if mcfg["clamp_building"] else road_chains_raw
            obstacles_for_method = obstacles if mcfg["clamp_building"] else obstacles_raw
            methods[method_id] = lambda spacing_fn, roads=roads_for_method, obs=obstacles_for_method, mcfg=mcfg: (
                generate_road_method(spacing_fn, roads, obs, mcfg)
            )

        for method_id, generate in methods.items():
            records, rejected, extra_stats = generate(spacing_fn)
            points = gpd.GeoDataFrame(records, geometry="geometry", crs=crs).reset_index(drop=True)
            if method_id != "face_ibge":
                points = enrich_with_ibge(points, faces)

            # osm_eixo sits on the carriageway on purpose (see module
            # docstring) — every other method offsets off it, so a slot
            # still inside *some* road's carriageway near a junction is a
            # cross street it was pushed into, not a legitimate position.
            if method_id != "osm_eixo":
                co_registered = method_id == "face_ibge" or ROAD_METHODS[method_id]["clamp_building"]
                tree = road_tree if co_registered else road_tree_raw
                geoms = road_geoms if co_registered else road_geoms_raw
                points, crossing_rejected = drop_crossing_road_points(points, tree, geoms)
                rejected += crossing_rejected
            points = points.drop(columns="bearing", errors="ignore")

            before = len(points)
            min_sep = points["porte"].map(lambda porte: spacing_fn(porte)).to_numpy()
            kept = enforce_min_separation(points, min_sep)
            rejected += [{"motivo": "espacamento_minimo", "porte": row.porte, "logradouro": row.logradouro,
                          "geometry": row.geometry} for row in points.drop(kept.index).itertuples()]
            points = kept.reset_index(drop=True)

            variant = f"{method_id}_{spacing_id}"
            points.to_file(files.slots(variant, "gpkg"), layer="vagas_plantio", driver="GPKG")
            points.to_crs("EPSG:4326").to_file(files.slots(variant, "geojson"), driver="GeoJSON")
            if rejected:
                rejected_gdf = gpd.GeoDataFrame(rejected, geometry="geometry", crs=crs)
                rejected_gdf.to_crs("EPSG:4326").to_file(
                    files.slots(f"{variant}_sem_alocacao", "geojson"), driver="GeoJSON")
            report["variantes"][variant] = {
                "metodo": method_id, "espacamento": spacing_id, "pontos": len(points),
                "removidos_espacamento_minimo": before - len(points), "pontos_sem_alocacao": len(rejected),
                "sem_alocacao_por_motivo": dict(Counter(r["motivo"] for r in rejected)),
                **extra_stats,
                "por_tipo": points["tipo_vaga"].value_counts().to_dict(),
                "por_porte": points["porte"].value_counts().to_dict(),
            }
            print(f"[{variant}] {len(points)} pontos (espaçamento mínimo removeu {before - len(points)}, "
                  f"sem alocação {len(rejected)})",
                  points["tipo_vaga"].value_counts().to_dict(), points["porte"].value_counts().to_dict())

    files.allocation_report.write_text(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
