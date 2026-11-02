"""Evaluate a spec selection (`spec.py` documents the forms) on points: element centres or node coordinates.

What it handles: boxes (with `abs_*` bounds), balls, cylinders, annuli, half-spaces and their union, intersection
and difference, and growing a selection by a distance (the `grow_if_empty` fallback). What it does not do: no
validation (spec.py did it), no file I/O, no printing. Needs numpy (the `topo` extra).
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .spec import AXES


def mask(sel: dict, pts: np.ndarray, grow: float = 0.0) -> np.ndarray:
    """A boolean mask over `pts` (n x 3): the points inside `sel`, every primitive grown by `grow` mm."""
    ((kind, body),) = sel.items()
    if kind == "union":
        return np.any(np.stack([mask(s, pts, grow) for s in body]), axis=0)
    if kind == "intersect":
        return np.all(np.stack([mask(s, pts, grow) for s in body]), axis=0)
    if kind == "minus":
        return mask(body[0], pts, grow) & ~mask(body[1], pts, 0.0)
    return _PRIM[kind](body, pts, grow)


def _box(b: dict[str, Any], pts: np.ndarray, g: float) -> np.ndarray:
    m = np.ones(len(pts), dtype=bool)
    for i, a in enumerate(AXES):
        if a in b:
            lo, hi = b[a]
            m &= (pts[:, i] >= lo - g) & (pts[:, i] <= hi + g)
        if f"abs_{a}" in b:
            lo, hi = b[f"abs_{a}"]
            m &= (np.abs(pts[:, i]) >= lo - g) & (np.abs(pts[:, i]) <= hi + g)
    return m


def _ball(b: dict[str, Any], pts: np.ndarray, g: float) -> np.ndarray:
    return np.linalg.norm(pts - np.asarray(b["c"], dtype=float), axis=1) <= b["r"] + g


def _radial(b: dict[str, Any], pts: np.ndarray, g: float) -> tuple[np.ndarray, np.ndarray]:
    n = AXES.index(b["axis"])
    i, j = (k for k in range(3) if k != n)
    r = np.hypot(pts[:, i] - b["c"][0], pts[:, j] - b["c"][1])
    m = np.ones(len(pts), dtype=bool)
    if "range" in b:
        lo, hi = b["range"]
        m &= (pts[:, n] >= lo - g) & (pts[:, n] <= hi + g)
    return r, m


def _cyl(b: dict[str, Any], pts: np.ndarray, g: float) -> np.ndarray:
    r, m = _radial(b, pts, g)
    return m & (r <= b["r"] + g)


def _annulus(b: dict[str, Any], pts: np.ndarray, g: float) -> np.ndarray:
    r, m = _radial(b, pts, g)
    return m & (r >= b["r_in"] - g) & (r <= b["r_out"] + g)


def _half(b: dict[str, Any], pts: np.ndarray, g: float) -> np.ndarray:
    n = np.asarray(b["n"], dtype=float)
    n = n / np.linalg.norm(n)
    return (pts - np.asarray(b["p"], dtype=float)) @ n >= -g


_PRIM = {"box": _box, "ball": _ball, "cyl": _cyl, "annulus": _annulus, "half": _half}
