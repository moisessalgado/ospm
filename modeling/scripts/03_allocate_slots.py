"""Allocate tree-planting slots — comparable methods, two spacings each.

Methods (increasing use of data, decreasing distance from the carriageway):
  - face_ibge: the production method. Each slot is linked to an IBGE street
    face (sector, block, face, street name, position along the face), then
    clamped to the matched OSM road — see clamp_to_road. Slowest to compute,
    best metadata, and the only one with median (canteiro_central) slots.
  - osm_eixo: walks the raw OSM road centerline itself, no offset at all —
    slots sit in the middle of the street on purpose. Shows the spacing and
    exclusions along each street; the other methods are judged against it
    for lateral position. The webmap's default method.
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
the spacing for the larger-porte slot of the pair (10 m flat for the default
"10m" spacing; 10, 6 or 4 m for grande, medio or pequeno under "porte" —
planting_slots/criteria.py) — including between sidewalk
and median slots. The sidewalk is also required to be at least the porte's
minimum width wide (carriageway edge to the nearest facade) wherever a
facade is known — "calcada_estreita" otherwise.

Every candidate dropped along the way is exported too (one
`<variante>_sem_alocacao.geojson` per variant, same porte it would have had),
tagged with why — "esquina" (corner setback), an obstacle name (see
OBSTACLES), "calcada_estreita" (sidewalk narrower than the porte's minimum) or
"sem_espaco_seguro" (no safe room before the nearest facade),
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

from planting_slots.allocation import enforce_min_separation, greedy_walk, slot_spacing_m
from planting_slots.criteria import (
    CLEARANCE_M, CORNER_SETBACK_M, MIN_PLANTER_WIDTH_BY_PORTE_M, MIN_SIDEWALK_WIDTH_BY_PORTE_M, SPACING_BY_PORTE_M,
)
from planting_slots.buildings import facade_probes, load_overture_buildings, local_facade
from planting_slots.ibge import load_street_faces, load_urban_area
from planting_slots.municipalities import get_municipality, municipality_arg_parser
from planting_slots.osm import (
    DEFAULT_WIDTH_BY_CLASS, angle_diff, bearing, carriageway_half_width, find_divided_pairs, junction_disks,
    load_osm_elements,
)
from planting_slots.paths import OSM_ROADS_LAYER, MunicipalityFiles
from planting_slots.registration import (
    apply_shift, coregistration_field, ibge_registration_uncertainty, normalize_street_name,
)
from planting_slots.sidewalk import (
    CURB_SETBACK_M, GENERATION_CLASSES, OBSTACLES, PORTE_BY_CLASS, allocate_face, allocate_from_roads,
    blocked_intervals, build_road_buffer_tree, build_road_chains, drop_crossing_road_points, enrich_with_ibge,
    local_bearing,
)

# --- Setbacks (official criteria: planting_slots/criteria.py) ---------------------
PIT_HALF_WIDTH_M = 0.5
SERVICE_SETBACK_M = 1.5
SAMPLE_STEP_M = 1.0

# --- Face -> road matching -------------------------------------------------------
FACE_STREET_MATCH_M = 15.0
FACE_STREET_MAX_ANGLE_DEG = 30.0
EXCLUDED_FACE_TYPES = {"VIELA", "BECO", "ESCADARIA", "TRILHOS", "MURO", "FUNDOS"}
# A median strip hosts the largest porte whose minimum planter width it meets.
MEDIAN_PORTE_BY_WIDTH = [(MIN_PLANTER_WIDTH_BY_PORTE_M["medio"], "pequeno"),
                         (MIN_PLANTER_WIDTH_BY_PORTE_M["grande"], "medio"), (np.inf, "grande")]
MIN_MEDIAN_WIDTH_M = MIN_PLANTER_WIDTH_BY_PORTE_M["pequeno"]

# --- Spacing (official criteria: 10 / 6 / 4 m for grande / medio / pequeno) ---------
# "10m" — the largest porte's spacing for every tree — is the project default.
SPACINGS = {
    "10m": {"label": "Espaçamento padrão de 10 m (maior porte)", "spacing": lambda porte: slot_spacing_m("10m", porte)},
    "porte": {"label": "Espaçamento por porte (10 / 6 / 4 m)", "spacing": lambda porte: slot_spacing_m("porte", porte)},
}

# --- Methods ------------------------------------------------------------------
# face_ibge is handled by its own (pre-existing) code path below; these four
# share allocate_from_roads, parametrized by whether they offset off the
# centerline at all, and whether that offset is capped by a building facade.
ROAD_METHODS = {
    "osm_eixo": {"label": "Eixo da via (OSM)", "offset": False, "clamp_building": False},
    "osm_calcada": {"label": "Calçada alocada nos dois lados da via (OSM)", "offset": True, "clamp_building": False},
    "osm_calcada_lado": {"label": "Calçada alocada em um dos lados da via (OSM)", "offset": True,
                         "clamp_building": False, "one_side": True},
    "osm_overture": {"label": "Calçada ajustada com prédios (OSM + Overture)", "offset": True, "clamp_building": True},
}
# The methods generated by default; the other two stay available through --metodos.
DEFAULT_METHODS = ["osm_eixo", "osm_calcada", "face_ibge"]


def load_ways(files: MunicipalityFiles) -> gpd.GeoDataFrame:
    ways = gpd.read_file(files.osm_roads, layer=OSM_ROADS_LAYER)
    ways = ways.explode(index_parts=False).reset_index(drop=True)
    return ways[(ways.geometry.geom_type == "LineString") & (ways.geometry.length > 1.0)].copy()



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






CLAMP_MAX_ITER = 6


def clamp_to_road(point: Point, road, min_clear: float, probe_entry,
                  min_sidewalk: float) -> tuple[Point | None, str | None]:
    """Push a face-derived point, perpendicular to the road, so it clears the
    carriageway (min_clear) without crossing the nearest building facade.
    Returns (point, None), or (None, reason) when it can't be placed:
    "calcada_estreita" if the sidewalk between the carriageway edge and the
    facade is narrower than min_sidewalk (the porte's minimum width), and
    "sem_espaco_seguro" if there is no room between the two, or if a safe
    spot can't be found within CLAMP_MAX_ITER tries (a bend or junction
    nearby can put the pushed point closer to a *different* stretch of the
    same road than the one it was projected from) — skipping the slot there
    is safer than risking a point still inside the carriageway."""
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
        if not np.isnan(facade_d) and facade_d - (min_clear - CURB_SETBACK_M) < min_sidewalk:
            return None, "calcada_estreita"
        ceiling = (facade_d - CURB_SETBACK_M) if not np.isnan(facade_d) else np.inf
        if min_clear > ceiling:
            return None, "sem_espaco_seguro"
        target = min(max(dist, min_clear), ceiling)
        candidate = Point(r.x + ux * target, r.y + uy * target)
        if road.distance(candidate) >= min_clear - 1e-6:
            return candidate, None
        p = candidate  # too close to a different stretch of the road: re-project and retry
    return None, "sem_espaco_seguro"




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





def main() -> None:
    parser = municipality_arg_parser(__doc__)
    parser.add_argument("--metodos", nargs="+", choices=["face_ibge", *ROAD_METHODS], metavar="METODO",
                        help="Métodos a gerar (padrão: " + ", ".join(DEFAULT_METHODS) + "); todos: face_ibge, " + ", ".join(ROAD_METHODS))
    args = parser.parse_args()
    municipality = get_municipality(args.municipio)
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
    roads["half"] = [carriageway_half_width(r, p, np.nan,
                                            MIN_SIDEWALK_WIDTH_BY_PORTE_M[PORTE_BY_CLASS.get(r["highway"], "medio")])[0]
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
    blocked = {i: blocked_intervals(g, obstacles, PORTE_BY_CLASS[sel.loc[i, "highway"]]) for i, g in sel.geometry.items()}

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
    # one exclusion tree per porte: the obstacle clearances depend on it
    etrees = {porte: shapely.STRtree(exclusion + [b for geoms, radii in obstacles.values()
                                                  for b in shapely.buffer(geoms, radii[porte])])
              for porte in SPACING_BY_PORTE_M}
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
        sample_porte = np.array([next(p for lim, p in MEDIAN_PORTE_BY_WIDTH if w < lim) for w in width])
        for porte, etree in etrees.items():
            idx = np.flatnonzero(sample_porte == porte)
            if len(idx):
                ok[idx[np.unique(etree.query(mids[idx], predicate="within")[0])]] = False
        median_groups.append({"row": ways.loc[a], "dists": d, "pts": mids, "width": width, "ok": ok})

    # --- allocation per method x spacing -----------------------------------------
    report = {
        "municipio": municipality.slug,
        "parametros": {
            "afastamento_esquina_m": CORNER_SETBACK_M,
            "espacamento_por_porte_m": SPACING_BY_PORTE_M,
            "largura_minima_calcada_por_porte_m": MIN_SIDEWALK_WIDTH_BY_PORTE_M,
            "largura_minima_canteiro_por_porte_m": MIN_PLANTER_WIDTH_BY_PORTE_M,
            "distancias_minimas_infraestrutura_m": CLEARANCE_M,
            "incerteza_registro_canteiros_m": round(uncertainty, 2),
        },
        "faces": summary,
        "variantes": {},
    }

    def generate_face_ibge(spacing_fn):
        records, rejected = [], []
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
                point, motivo = clamp_to_road(candidate, road_geom, min_clear, probe_entry,
                                              MIN_SIDEWALK_WIDTH_BY_PORTE_M[porte])
                if point is None:
                    rejected.append({"motivo": motivo, "porte": porte,
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
        return records, rejected, {
            "removidos_sem_espaco_seguro_ate_fachada": sum(r["motivo"] == "sem_espaco_seguro" for r in rejected),
            "removidos_calcada_estreita": sum(r["motivo"] == "calcada_estreita" for r in rejected)}

    for spacing_id, spacing_cfg in SPACINGS.items():
        spacing_fn = spacing_cfg["spacing"]

        def generate_road_method(spacing_fn, roads, obs, mcfg):
            records, rejected = allocate_from_roads(roads, chain_probes, obs, mcfg["offset"], mcfg["clamp_building"],
                                                    spacing_fn, chain_sides_raw if mcfg.get("one_side") else None)
            stats = ({"removidos_sem_espaco_seguro_ate_fachada": sum(r["motivo"] == "sem_espaco_seguro" for r in rejected),
                      "removidos_calcada_estreita": sum(r["motivo"] == "calcada_estreita" for r in rejected)}
                     if mcfg["clamp_building"] else {})
            return records, rejected, stats

        methods = {"face_ibge": generate_face_ibge}
        for method_id, mcfg in ROAD_METHODS.items():
            roads_for_method = road_chains if mcfg["clamp_building"] else road_chains_raw
            obstacles_for_method = obstacles if mcfg["clamp_building"] else obstacles_raw
            methods[method_id] = lambda spacing_fn, roads=roads_for_method, obs=obstacles_for_method, mcfg=mcfg: (
                generate_road_method(spacing_fn, roads, obs, mcfg)
            )

        for method_id, generate in methods.items():
            if method_id not in (args.metodos or DEFAULT_METHODS):
                continue
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
