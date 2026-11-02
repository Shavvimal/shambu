"""The Michell cantilever: the textbook check of a stiffness optimiser.

A block clamped on its x = 0 face carries a downward force at mid-height of its free end. The stiffest layout at a
low volume puts material in the top and bottom chords at the clamp and leaves the middle of the clamp face empty
(Michell, 1904). `shambu topo validate` and the tests run it.

What it handles: the spec document of each size (`small`: 24 x 1 x 8 cells of 1 mm, about a second; `paper`: 60 x
4 x 20 cells of 3 mm, the larger reference grid) and the shape metrics with their pass bands. What it does not do:
no solving (service.py), no numpy, no file I/O, no printing.
"""

from __future__ import annotations

from collections.abc import Sequence

SIZES = {
    # name: (cells x, y, z), pitch mm, volfrac, maxit, force N
    "small": ((24, 1, 8), 1.0, 0.4, 40, 1.0),
    "paper": ((60, 4, 20), 3.0, 0.3, 80, 100.0),
}
CHORD_MIN = 0.6  # the top and bottom chords at the clamp must read at least this
MIDDLE_MAX = 0.1  # the middle of the clamp face must read at most this
MONOTONE_FROM = 5  # the objective may rise only within MONOTONE_TOL after this iteration
MONOTONE_TOL = 1e-3


def spec_doc(size: str = "small") -> dict:
    (nx, ny, nz), h, volfrac, maxit, force = SIZES[size]
    lx, ly, lz = nx * h, ny * h, nz * h
    e = h / 100  # a selection margin well inside one cell
    return {
        "schema": "shambu.topo/1",
        "units": "mm-N-MPa",
        "domain": {"box": {"min": [0, 0, 0], "max": [lx, ly, lz]}, "pitch": h},
        "simp": {"volfrac": [volfrac], "rmin_cells": 1.5, "maxit": maxit, "tol": 0.01},
        "supports": [{"name": "clamp", "nodes": {"box": {"x": [-e, e]}}, "dofs": "xyz"}],
        "cases": [
            {
                "name": "tip",
                "loads": [{"nodes": {"box": {"x": [lx - e, lx + e], "z": [lz / 2 - e, lz / 2 + e]}}, "force": [0, 0, -force]}],
            }
        ],
        "regions": [
            {"name": "top chord at the clamp", "cells": {"box": {"x": [0, 2 * h], "z": [lz - h, lz]}}},
            {"name": "bottom chord at the clamp", "cells": {"box": {"x": [0, 2 * h], "z": [0, h]}}},
            {"name": "middle of the clamp", "cells": {"box": {"x": [0, 2 * h], "z": [lz / 2 - h, lz / 2 + h]}}},
        ],
    }


def metrics(region_means: dict[str, float | None], history: Sequence[float]) -> dict:
    """The pass bands against the region means of a finished run."""
    top = region_means.get("top chord at the clamp") or 0.0
    bottom = region_means.get("bottom chord at the clamp") or 0.0
    middle = region_means.get("middle of the clamp")
    middle = 1.0 if middle is None else middle
    rises = [
        i + 1 for i in range(MONOTONE_FROM, len(history)) if history[i] > history[i - 1] * (1 + MONOTONE_TOL)
    ]  # 1-based iterations whose objective rose
    checks = {
        "top_chord": {"value": top, "pass": top > CHORD_MIN, "band": f"> {CHORD_MIN}"},
        "bottom_chord": {"value": bottom, "pass": bottom > CHORD_MIN, "band": f"> {CHORD_MIN}"},
        "clamp_middle": {"value": middle, "pass": middle < MIDDLE_MAX, "band": f"< {MIDDLE_MAX}"},
        "objective_monotone": {
            "value": rises,
            "pass": not rises,
            "band": f"no rise over {100 * MONOTONE_TOL:g} % after iteration {MONOTONE_FROM}",
        },
    }
    return {"checks": checks, "pass": all(c["pass"] for c in checks.values())}
