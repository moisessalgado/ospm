"""Co-registration between sources (OSM x buildings x IBGE) and positional uncertainty."""

from __future__ import annotations

import re
import unicodedata

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from planting_slots.buildings import probe_frames

REGISTRATION_PERCENTILE = 90
REGISTRATION_MAX_FACADE_M = 25.0
REGISTRATION_MIN_PAIRS = 4
REGISTRATION_MIN_WAYS = 30
DEFAULT_REGISTRATION_UNCERTAINTY_M = 3.0
IBGE_SAMPLE_STEP_M = 10.0
IBGE_MIN_FACE_GAP_M = 2.0
IBGE_MAX_FACE_GAP_M = 30.0
IBGE_MAX_AXIS_MATCH_M = 15.0
COREG_CELL_M = 250.0
COREG_RADIUS_M = 400.0
COREG_MIN_OBS = 40
COREG_RIDGE = 20.0  # acts like ~20 observations pulling the shift toward zero
COREG_HUBER_M = 1.5


def normalize_street_name(*parts) -> str:
    text = " ".join(str(p) for p in parts if isinstance(p, str))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text.upper()).strip()


def registration_uncertainty(eligible: gpd.GeoDataFrame, probes: dict) -> tuple[float, int]:
    offsets = []
    for idx in eligible.index[eligible["oneway"].isna() & ~eligible["paired"]]:
        _, left, right = probes[idx]
        both = ~np.isnan(left) & ~np.isnan(right) & (left < REGISTRATION_MAX_FACADE_M) & (right < REGISTRATION_MAX_FACADE_M)
        if both.sum() >= REGISTRATION_MIN_PAIRS:
            offsets.append(abs(np.median((left[both] - right[both]) / 2)))
    if len(offsets) < REGISTRATION_MIN_WAYS:
        return DEFAULT_REGISTRATION_UNCERTAINTY_M, len(offsets)
    return float(np.percentile(offsets, REGISTRATION_PERCENTILE)), len(offsets)


def coregistration_field(ways: gpd.GeoDataFrame, probes: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Systematic shift (dx, dy) of the OSM centerlines relative to the
    building footprints, on a COREG_CELL_M grid.

    Each probe with facades on both sides of a two-way street measures the
    centerline offset along its normal: o = (left - right) / 2 ≈ n·t + e,
    where t is the local shift and e the asymmetry of building setbacks.
    Streets with different bearings constrain both components of t and the
    setback noise averages out; a ridge term pulls t to zero where the
    geometry is poorly conditioned or measurements are scarce.
    """
    xy, normals, obs = [], [], []
    for idx in ways.index[ways["oneway"].isna() & ~ways["paired"]]:
        pos, left, right = probes[idx]
        both = ~np.isnan(left) & ~np.isnan(right) & (left < REGISTRATION_MAX_FACADE_M) & (right < REGISTRATION_MAX_FACADE_M)
        if both.sum() < REGISTRATION_MIN_PAIRS:
            continue
        c, n = probe_frames(ways.geometry.loc[idx], pos[both])
        xy.append(c), normals.append(n), obs.append((left[both] - right[both]) / 2)
    xy, normals, obs = np.concatenate(xy), np.concatenate(normals), np.concatenate(obs)
    gx, gy, shift = fit_shift_field(xy, normals, obs)
    return gx, gy, shift, xy


def fit_shift_field(xy: np.ndarray, normals: np.ndarray, obs: np.ndarray):
    """Robust local least squares of obs ≈ normal·t on a COREG_CELL_M grid."""
    from scipy.spatial import cKDTree
    x0, y0 = xy.min(axis=0) - COREG_CELL_M
    nx = int((xy[:, 0].max() - x0) // COREG_CELL_M) + 3
    ny = int((xy[:, 1].max() - y0) // COREG_CELL_M) + 3
    gx = x0 + np.arange(nx) * COREG_CELL_M
    gy = y0 + np.arange(ny) * COREG_CELL_M
    shift = np.zeros((ny, nx, 2))
    kd = cKDTree(xy)
    for j, cy in enumerate(gy):
        for i, cx in enumerate(gx):
            sel = kd.query_ball_point((cx, cy), COREG_RADIUS_M)
            if len(sel) < COREG_MIN_OBS:
                continue
            N, o = normals[sel], obs[sel]
            w = np.ones(len(sel))
            t = np.zeros(2)
            for _ in range(5):  # IRLS with Huber weights
                A = (N * w[:, None]).T @ N + COREG_RIDGE * np.eye(2)
                t = np.linalg.solve(A, (N * w[:, None]).T @ o)
                r = np.abs(o - N @ t)
                w = np.where(r <= COREG_HUBER_M, 1.0, COREG_HUBER_M / np.maximum(r, 1e-9))
            shift[j, i] = t
    return gx, gy, shift


def apply_shift(geom, gx: np.ndarray, gy: np.ndarray, shift: np.ndarray):
    """Translate every vertex by the bilinearly interpolated shift field. The
    shift depends only on position, so vertices shared by several ways stay
    shared (topology preserved)."""
    def move(coords: np.ndarray) -> np.ndarray:
        fx = np.clip((coords[:, 0] - gx[0]) / COREG_CELL_M, 0, len(gx) - 1.001)
        fy = np.clip((coords[:, 1] - gy[0]) / COREG_CELL_M, 0, len(gy) - 1.001)
        i0, j0 = fx.astype(int), fy.astype(int)
        ax, ay = (fx - i0)[:, None], (fy - j0)[:, None]
        s = (shift[j0, i0] * (1 - ax) * (1 - ay) + shift[j0, i0 + 1] * ax * (1 - ay)
             + shift[j0 + 1, i0] * (1 - ax) * ay + shift[j0 + 1, i0 + 1] * ax * ay)
        return coords + s
    return shapely.transform(geom, move)


def ibge_axis_points(faces: gpd.GeoDataFrame) -> np.ndarray:
    """Street axis according to IBGE: midpoints between each face and the
    opposite face of the same street (other block)."""
    faces = faces.reset_index(drop=True)
    name = np.array([normalize_street_name(a, b, c) for a, b, c in
                     zip(faces["NM_TIP_LOG"], faces["NM_TIT_LOG"], faces["NM_LOG"])])
    block = (faces["CD_SETOR"] + faces["CD_QUADRA"]).to_numpy()
    samples, owner = [], []
    for i, g in enumerate(faces.geometry):
        if g.length < 2 * IBGE_SAMPLE_STEP_M:
            continue
        d = np.arange(IBGE_SAMPLE_STEP_M, g.length - IBGE_SAMPLE_STEP_M / 2, IBGE_SAMPLE_STEP_M)
        samples.append(shapely.line_interpolate_point(g, d))
        owner.append(np.full(len(d), i))
    samples, owner = np.concatenate(samples), np.concatenate(owner)
    tree = shapely.STRtree(faces.geometry.values)
    si, fj = tree.query(samples, predicate="dwithin", distance=IBGE_MAX_FACE_GAP_M)
    keep = (name[fj] == name[owner[si]]) & (block[fj] != block[owner[si]]) & (name[fj] != "")
    si, fj = si[keep], fj[keep]
    gap = shapely.distance(samples[si], faces.geometry.values[fj])
    ok = gap >= IBGE_MIN_FACE_GAP_M
    si, fj, gap = si[ok], fj[ok], gap[ok]
    best = pd.DataFrame({"s": si, "f": fj, "gap": gap}).sort_values("gap").drop_duplicates("s")
    pts = samples[best["s"].to_numpy()]
    opp = faces.geometry.values[best["f"].to_numpy()]
    near = shapely.line_interpolate_point(opp, shapely.line_locate_point(opp, pts))
    return (shapely.get_coordinates(pts) + shapely.get_coordinates(near)) / 2


def ibge_registration_uncertainty(ways: gpd.GeoDataFrame, faces: gpd.GeoDataFrame) -> tuple[float, int, int]:
    """p90 (per street) of the local discrepancy between the co-registered
    OSM axis and the IBGE axis, after removing IBGE's own systematic shift.
    Independent sources and no setback effect: bounds the residual error."""
    axis = ibge_axis_points(faces)
    pts = shapely.points(axis)
    tree = shapely.STRtree(ways.geometry.values)
    pi, wi = tree.query_nearest(pts, max_distance=IBGE_MAX_AXIS_MATCH_M, all_matches=False)
    xy, normals, obs, way_of = [], [], [], []
    for w in np.unique(wi):
        sel = pi[wi == w]
        line = ways.geometry.values[w]
        c, n = probe_frames(line, shapely.line_locate_point(line, pts[sel]))
        xy.append(c), normals.append(n), way_of.append(np.full(len(sel), w))
        obs.append(np.einsum("ij,ij->i", axis[sel] - c, n))
    xy, normals, obs, way_of = map(np.concatenate, (xy, normals, obs, way_of))
    gx, gy, shift = fit_shift_field(xy, normals, obs)
    field = shapely.get_coordinates(apply_shift(shapely.points(xy), gx, gy, shift)) - xy
    residual = np.abs(obs - np.einsum("ij,ij->i", normals, field))
    per_way = pd.Series(residual).groupby(way_of).agg(["median", "size"])
    per_way = per_way[per_way["size"] >= 3]["median"]
    return float(np.percentile(per_way, REGISTRATION_PERCENTILE)), len(per_way), len(axis)
