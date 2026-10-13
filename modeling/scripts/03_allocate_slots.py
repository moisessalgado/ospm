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

Two things that keep a slot from existing somewhere are exported too, so the
webmap can show *why* a gap is there instead of leaving it unexplained:
obstacles (one file per municipality, shared by every variant — see
OBSTACLES) and candidates dropped for lack of safe room between the
carriageway and the nearest facade (one `<variante>_sem_alocacao.geojson`
per variant — see allocate_from_roads/generate_face_ibge).
"""

from __future__ import annotations

import json

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
    DEFAULT_WIDTH_BY_CLASS, angle_diff, bearing, carriageway_half_width, find_divided_pairs,
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
    "osm_eixo": {"label": "Eixo da via (OSM) — sem deslocamento", "offset": False, "clamp_building": False},
    "osm_calcada": {"label": "Calçada estimada por OSM (sem prédios)", "offset": True, "clamp_building": False},
    "osm_overture": {"label": "Calçada validada com prédios (OSM + Overture)", "offset": True, "clamp_building": True},
}
IBGE_MATCH_M = 25.0  # metadata-only nearest-face search radius for non-face_ibge methods


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


def blocked_intervals(face, obstacles: dict[str, np.ndarray]) -> list[tuple[float, float]]:
    out = []
    for name, (geoms, radius) in obstacles.items():
        if not len(geoms):
            continue
        near = geoms[shapely.dwithin(geoms, face, OBSTACLE_TO_FACE_M)]
        for g in near:
            p = face.project(g.centroid if g.geom_type != "Point" else g)
            out.append((p - radius, p + radius))
    return out


def allocate_face(face, blocked, spacing_at) -> list[float]:
    """Positions (m from the face start) of the slots on one face."""
    dists = np.arange(CORNER_SETBACK_M, face.length - CORNER_SETBACK_M + 1e-9, SAMPLE_STEP_M)
    if not len(dists):
        return []
    ok = np.ones(len(dists), dtype=bool)
    for a, b in blocked:
        ok &= ~((dists >= a) & (dists <= b))
    return [float(dists[i]) for i in greedy_walk(dists, ok, spacing_at)]


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
                        clamp_building: bool, spacing_fn) -> tuple[list[dict], list[dict], int]:
    """Slots walked directly along eligible roads (no IBGE face involved),
    for the three OSM-only comparison methods — see module docstring. Also
    returns the candidates that had to be dropped for lack of safe room
    between the carriageway and the nearest facade (motivo=sem_espaco_seguro),
    kept at their best-guess position so the webmap can show *where* and
    *why* a slot is missing, instead of just leaving a silent gap."""
    empty_probe = (np.array([]), np.array([]), np.array([]))
    records, rejected, skipped = [], [], 0
    for idx, row in roads.iterrows():
        porte = PORTE_BY_CLASS[row["highway"]]
        road_geom = row.geometry
        street_name = normalize_street_name(row.get("name"))
        positions = allocate_face(road_geom, blocked_intervals(road_geom, obstacles), lambda _: spacing_fn(porte))
        if not positions:
            continue
        probe_entry = probes.get(idx, empty_probe) if clamp_building else empty_probe
        for pos in positions:
            for side_left in ([True, False] if offset else [None]):
                if not offset:
                    point = road_geom.interpolate(pos)
                else:
                    floor = row["half"] + CURB_SETBACK_M
                    ceiling = np.inf
                    if clamp_building and len(probe_entry[0]):
                        facade_d = local_facade(np.array([pos]), probe_entry[0],
                                                probe_entry[1] if side_left else probe_entry[2])[0]
                        ceiling = (facade_d - CURB_SETBACK_M) if not np.isnan(facade_d) else np.inf
                    if floor > ceiling:
                        skipped += 1
                        rejected.append({"motivo": "sem_espaco_seguro", "porte": porte,
                                        "logradouro": street_name, "geometry": road_geom.interpolate(pos)})
                        continue
                    point = point_at_offset(road_geom, pos, side_left, floor)
                    if clamp_building and road_geom.distance(point) < floor - 1e-6:
                        skipped += 1
                        rejected.append({"motivo": "sem_espaco_seguro", "porte": porte,
                                        "logradouro": street_name, "geometry": point})
                        continue
                records.append({
                    "tipo_vaga": "calcada" if offset else "eixo_via", "porte": porte, "via_osm": row["highway"],
                    "logradouro": street_name,
                    "cd_setor": None, "cd_quadra": None, "cd_face": None, "posicao_face_m": round(pos, 1),
                    "posicao_lateral": "sobre o eixo da via" if not offset else
                                      ("deslocada por largura estimada (sem prédios)" if not clamp_building else
                                       "deslocada e validada contra a fachada mais próxima (Overture)"),
                    "geometry": point,
                })
    return records, rejected, skipped


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

    # Exported once (shared by every method/spacing) so the webmap can show
    # *why* the generic spacing looks irregular in places — the gaps are
    # these exclusion zones, not noise in the allocation.
    obstacle_records = [
        {"motivo": name, "geometry": g.centroid if g.geom_type != "Point" else g}
        for name, (geoms, _radius) in obstacles_raw.items() for g in geoms
    ]
    if obstacle_records:
        gpd.GeoDataFrame(obstacle_records, geometry="geometry", crs=crs) \
            .to_crs("EPSG:4326").to_file(files.obstacles("geojson"), driver="GeoJSON")

    roads = ways[ways["highway"].isin(DEFAULT_WIDTH_BY_CLASS)].copy()
    roads["half"] = [carriageway_half_width(r, p, np.nan, municipality.min_sidewalk_width_m)[0]
                     for r, p in zip(roads.to_dict("records"), roads["paired"])]
    roads_raw = roads.copy()
    roads_raw["geometry"] = ways_raw_geometry.loc[roads.index]
    gen_roads = roads[roads["highway"].isin(GENERATION_CLASSES)]
    gen_roads_raw = roads_raw[roads_raw["highway"].isin(GENERATION_CLASSES)]

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
            for pos in allocate_face(face.geometry, blocked[i], lambda _: spacing_fn(porte)):
                candidate = face.geometry.interpolate(pos)
                point = clamp_to_road(candidate, road_geom, min_clear, probe_entry)
                if point is None:
                    fora_de_alcance += 1
                    rejected.append({"motivo": "sem_espaco_seguro", "porte": porte,
                                    "logradouro": street_name, "geometry": candidate})
                    continue
                records.append({
                    "tipo_vaga": "calcada", "porte": porte, "via_osm": face["highway"],
                    "logradouro": street_name,
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
            records, rejected, skipped = allocate_from_roads(roads, probes, obs, mcfg["offset"], mcfg["clamp_building"], spacing_fn)
            stats = {"removidos_sem_espaco_seguro_ate_fachada": skipped} if mcfg["clamp_building"] else {}
            return records, rejected, stats

        methods = {"face_ibge": generate_face_ibge}
        for method_id, mcfg in ROAD_METHODS.items():
            roads_for_method = gen_roads if mcfg["clamp_building"] else gen_roads_raw
            obstacles_for_method = obstacles if mcfg["clamp_building"] else obstacles_raw
            methods[method_id] = lambda spacing_fn, roads=roads_for_method, obs=obstacles_for_method, mcfg=mcfg: (
                generate_road_method(spacing_fn, roads, obs, mcfg)
            )

        for method_id, generate in methods.items():
            records, rejected, extra_stats = generate(spacing_fn)
            points = gpd.GeoDataFrame(records, geometry="geometry", crs=crs)
            if method_id != "face_ibge":
                points = enrich_with_ibge(points, faces)
            before = len(points)
            min_sep = points["porte"].map(lambda porte: spacing_fn(porte)).to_numpy()
            points = enforce_min_separation(points, min_sep).reset_index(drop=True)

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
