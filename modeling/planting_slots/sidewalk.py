"""The road-walking core of the sidewalk methods: continuous road chains,
spacing walk with corner/obstacle exclusions, lateral offset to the estimated
curb, IBGE metadata and the final clean-ups.

Shared by scripts/03_allocate_slots.py (all methods, one municipality at a
time, with its webmap export) and the national batch (planting_slots/lote.py,
osm_calcada only). Nothing here depends on how the inputs were obtained:
callers hand in roads, obstacles and IBGE faces already in a metric CRS.
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import shapely
from shapely.geometry import Point

from planting_slots.allocation import enforce_min_separation, slot_spacing_m
from planting_slots.buildings import local_facade
from planting_slots.criteria import (
    CLEARANCE_M, CORNER_SETBACK_M, MIN_SIDEWALK_WIDTH_BY_PORTE_M, SPACING_BY_PORTE_M,
)
from planting_slots.osm import (
    CORNER_CLASSES, DEFAULT_WIDTH_BY_CLASS, angle_diff, carriageway_half_width, find_divided_pairs,
)
from planting_slots.registration import normalize_street_name

# --- Setbacks (official criteria: planting_slots/criteria.py) ---------------------
CURB_SETBACK_M = 0.5

OBSTACLES = {  # name -> (OSM tag filter, exclusion radius in m by porte)
    "semaforo": (lambda t: t.get("highway") == "traffic_signals", CLEARANCE_M["semaforo"]),
    "travessia": (lambda t: t.get("footway") == "crossing" or t.get("highway") == "crossing", CLEARANCE_M["travessia"]),
    "acesso_garagem": (lambda t: t.get("amenity") == "parking_entrance" or t.get("service") == "driveway",
                       CLEARANCE_M["acesso_garagem"]),
    "ponto_onibus": (lambda t: t.get("highway") == "bus_stop" or t.get("public_transport") == "platform",
                     CLEARANCE_M["ponto_onibus"]),
    "poste": (lambda t: t.get("highway") == "street_lamp" or t.get("power") == "pole", CLEARANCE_M["poste"]),
    "transformador": (lambda t: t.get("power") == "transformer" or bool(t.get("transformer")),
                      CLEARANCE_M["transformador"]),
    "boca_de_lobo": (lambda t: t.get("man_made") == "manhole" or "manhole" in t, CLEARANCE_M["boca_de_lobo"]),
    # An existing tree is a tree: the new one keeps the minimum distance between trees from it.
    "arvore_existente": (lambda t: t.get("natural") == "tree", SPACING_BY_PORTE_M),
}
OBSTACLE_TO_FACE_M = 8.0  # OSM obstacles within this distance are projected onto the face

# Road classes that get slots, and the tree size each one gets (sidewalk width is not measurable).
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

IBGE_MATCH_M = 25.0  # metadata-only nearest-face search radius for non-face_ibge methods


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
    if not rows:  # no walkable street in the extent: an empty frame that still has its geometry column
        return gpd.GeoDataFrame({"highway": [], "half": [], "name": [], "start_corner": [], "end_corner": []},
                                geometry=gpd.GeoSeries([], crs=roads.crs), crs=roads.crs)
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=roads.crs)


def local_bearing(line, pos: float, probe: float = 2.0) -> float:
    """Direction of travel (degrees) right around arc-length pos — used to
    tell a genuine crossing street apart from the next OSM way segment of
    the *same* street (see drop_crossing_road_points), which shares roughly
    the same bearing where they meet."""
    a = line.interpolate(max(pos - probe, 0.0))
    b = line.interpolate(min(pos + probe, line.length))
    return float(np.degrees(np.arctan2(b.y - a.y, b.x - a.x))) % 360


def blocked_intervals(line, obstacles: dict[str, tuple], porte: str, start_corner: bool = True,
                      end_corner: bool = True) -> list[tuple[float, float, str]]:
    """Intervals (m from the line start) where a slot can't go, each tagged
    with why — the corner setback at each end that is an actual intersection
    (see build_road_chains), plus the porte's clearance around every nearby obstacle. Fed
    to allocate_face, which uses the tag to report not just that a candidate
    was dropped there but which rule dropped it."""
    length = line.length
    out = []
    if start_corner:
        out.append((0.0, CORNER_SETBACK_M, "esquina"))
    if end_corner:
        out.append((max(length - CORNER_SETBACK_M, 0.0), length, "esquina"))
    for name, (geoms, radii) in obstacles.items():
        if not len(geoms):
            continue
        radius = radii[porte]
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
        blocked = blocked_intervals(road_geom, obstacles, porte, row["start_corner"], row["end_corner"])
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
                    ceiling, facade_d = np.inf, np.nan
                    if clamp_building and len(probe_entry[0]):
                        facade_d = local_facade(np.array([pos]), probe_entry[0],
                                                probe_entry[1] if side_left else probe_entry[2])[0]
                        ceiling = (facade_d - CURB_SETBACK_M) if not np.isnan(facade_d) else np.inf
                    if clamp_building and len(probe_entry[0]) and not np.isnan(facade_d) \
                            and facade_d - row["half"] < MIN_SIDEWALK_WIDTH_BY_PORTE_M[porte]:
                        rejected.append({"motivo": "calcada_estreita", "porte": porte,
                                        "logradouro": street_name, "geometry": road_geom.interpolate(pos)})
                        continue
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


def generate_osm_calcada(ways: gpd.GeoDataFrame, obstacles: dict[str, tuple], faces: gpd.GeoDataFrame | None = None,
                         spacing_id: str = "10m") -> tuple[gpd.GeoDataFrame, list[dict]]:
    """The `osm_calcada` method end to end, on raw OSM geometry: slots on both
    sides of every generation-class road, offset to the estimated curb, with
    IBGE sector/block/face from the nearest street face (when `faces` is
    given), minus the slots that landed inside a crossing road and those
    closer than the spacing to another one.

    `ways` are the municipality's OSM roads (columns highway, name, oneway,
    lanes, width; one LineString per row, metric CRS); `obstacles` maps each
    OBSTACLES name to (geometries, radius by porte). Returns the slots and
    every candidate dropped along the way, tagged with why (as in
    scripts/03_allocate_slots.py)."""
    spacing_fn = lambda porte: slot_spacing_m(spacing_id, porte)  # noqa: E731
    pairs = find_divided_pairs(ways)
    roads = ways[ways["highway"].isin(DEFAULT_WIDTH_BY_CLASS)].copy()
    paired = roads.index.isin(pairs.keys())
    roads["half"] = [carriageway_half_width(r, p, np.nan,
                                            MIN_SIDEWALK_WIDTH_BY_PORTE_M[PORTE_BY_CLASS.get(r["highway"], "medio")])[0]
                     for r, p in zip(roads.to_dict("records"), paired)]
    chains = build_road_chains(roads)
    records, rejected = allocate_from_roads(chains, {}, obstacles, True, False, spacing_fn, None)
    if not records:
        return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs=ways.crs), rejected
    points = gpd.GeoDataFrame(records, geometry="geometry", crs=ways.crs).reset_index(drop=True)
    if faces is not None and len(faces):
        points = enrich_with_ibge(points, faces)
    points, crossing_rejected = drop_crossing_road_points(points, build_road_buffer_tree(roads),
                                                          roads.geometry.to_numpy())
    rejected += crossing_rejected
    points = points.drop(columns="bearing", errors="ignore")
    min_sep = points["porte"].map(spacing_fn).to_numpy()
    kept = enforce_min_separation(points, min_sep)
    rejected += [{"motivo": "espacamento_minimo", "porte": row.porte, "logradouro": row.logradouro,
                  "geometry": row.geometry} for row in points.drop(kept.index).itertuples()]
    return kept.reset_index(drop=True), rejected
