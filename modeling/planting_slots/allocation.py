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


def enforce_min_separation(points: gpd.GeoDataFrame, min_sep: float) -> gpd.GeoDataFrame:
    """Greedy dedup guaranteeing no two kept points closer than min_sep
    (grid hash of cell size min_sep, checking the 3x3 neighbourhood)."""
    grid: dict[tuple[int, int], list[tuple[float, float]]] = {}
    xs, ys = points.geometry.x.to_numpy(), points.geometry.y.to_numpy()
    keep = np.zeros(len(points), dtype=bool)
    sq = min_sep * min_sep
    for i in range(len(points)):
        x, y = xs[i], ys[i]
        cx, cy = int(x // min_sep), int(y // min_sep)
        ok = True
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for ox, oy in grid.get((cx + dx, cy + dy), ()):
                    if (ox - x) ** 2 + (oy - y) ** 2 < sq:
                        ok = False
                        break
                if not ok:
                    break
            if not ok:
                break
        if ok:
            grid.setdefault((cx, cy), []).append((x, y))
            keep[i] = True
    return points[keep]
