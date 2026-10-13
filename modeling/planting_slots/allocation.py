"""Placing slots along lines and enforcing a global minimum spacing."""

from __future__ import annotations

import geopandas as gpd
import numpy as np


def greedy_walk(dists, ok, spacing_at) -> list[int]:
    """Place a point at the first allowed sample, then skip ahead by the
    spacing implied at that sample."""
    placed, next_allowed = [], -np.inf
    for i, d in enumerate(dists):
        if d < next_allowed or not ok[i]:
            continue
        placed.append(i)
        next_allowed = d + spacing_at(i)
    return placed


def greedy_walk_reasons(dists, blocked, spacing_at) -> tuple[list[int], list[tuple[int, str]]]:
    """Like greedy_walk, but instead of silently skipping a blocked sample it
    attempts a slot there too — same spacing cadence as a real placement —
    and records why: `blocked` is [(start, end, motivo), ...] (first match
    wins), so the caller gets back not just where slots were placed but
    where one was attempted and excluded, and by which rule."""
    n = len(dists)
    motivo = np.full(n, None, dtype=object)
    for a, b, m in blocked:
        mask = (dists >= a) & (dists <= b) & (motivo == None)  # noqa: E711 (vectorized None check)
        motivo[mask] = m

    placed, rejected, next_allowed = [], [], -np.inf
    for i, d in enumerate(dists):
        if d < next_allowed:
            continue
        if motivo[i] is None:
            placed.append(i)
        else:
            rejected.append((i, motivo[i]))
        next_allowed = d + spacing_at(i)
    return placed, rejected


def enforce_min_separation(points: gpd.GeoDataFrame, min_sep) -> gpd.GeoDataFrame:
    """Greedy dedup guaranteeing no two kept points closer than min_sep.

    min_sep is each point's own required clearance (a scalar applied to all,
    or a per-point array/Series, e.g. by porte) — two points must be at
    least max(min_sep[i], min_sep[j]) apart, so a large-porte slot keeps its
    full radius free even from a small-porte neighbour. Grid hash with cell
    size max(min_sep), checking the 3x3 neighbourhood."""
    radius = np.full(len(points), min_sep, dtype=float) if np.isscalar(min_sep) else np.asarray(min_sep, dtype=float)
    cell = radius.max() if len(radius) else 1.0
    grid: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    xs, ys = points.geometry.x.to_numpy(), points.geometry.y.to_numpy()
    keep = np.zeros(len(points), dtype=bool)
    for i in range(len(points)):
        x, y, r = xs[i], ys[i], radius[i]
        cx, cy = int(x // cell), int(y // cell)
        ok = True
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for ox, oy, orad in grid.get((cx + dx, cy + dy), ()):
                    req = max(r, orad)
                    if (ox - x) ** 2 + (oy - y) ** 2 < req * req:
                        ok = False
                        break
                if not ok:
                    break
            if not ok:
                break
        if ok:
            grid.setdefault((cx, cy), []).append((x, y, r))
            keep[i] = True
    return points[keep]
