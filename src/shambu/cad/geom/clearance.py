"""`shambu cad clearance`: pairwise clash between placed parts (the `geom` extra). The measuring half; the verdicts
are in `cad/judge.py`.

For each pair from the rule's two selectors (pairs inside one group skipped when the rule says so): a box cull with
the report bound, then the overlap from the intersection engine (`ManifoldBoolean` for two closed meshes,
`SampledContainment` otherwise), then, with no overlap, the exact minimum distance between the meshes
(`engines.mesh_distance`: every vertex against the other's triangles, then the near edge pairs). A distance beyond max(2 x margin, 1 mm) is `null` with
`beyond_mm`, never `inf`. A `GeomError` from the engine is passed to the verdict as a failed Boolean.

What it does not do: no file reading, no selector parsing (spec.py), no verdicts (judge.py), no printing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from ...errors import GeomError
from ...findings import Finding
from ..judge import clearance_verdict, overlap_floor, report_bound, sample_spacing
from ..spec import ClearanceRuleSpec, PartSpec, Zone
from .engines import GMesh, IntersectionEngine, ManifoldBoolean, PrunedDistance, SampledContainment, mesh_distance


@dataclass
class Placed:
    part: PartSpec
    mesh: GMesh


def box_gap(a: GMesh, b: GMesh) -> float:
    gap = np.maximum(0.0, np.maximum(a.lo - b.hi, b.lo - a.hi))
    return float(np.linalg.norm(gap))


def check_pair(
    rule: ClearanceRuleSpec,
    pa: Placed,
    pb: Placed,
    zones: dict[str, Zone],
    distance: Callable[[GMesh], PrunedDistance],
    engine: IntersectionEngine | None = None,
) -> tuple[dict, list[Finding]]:
    """The JSON row and the findings of one pair. `distance(mesh)` builds (or reuses) the mesh's distance engine."""
    a, b = pa.mesh, pb.mesh
    bound = report_bound(rule.margin_mm)
    if box_gap(a, b) > bound:
        row, fs = clearance_verdict(rule, pa.part, pb.part, zones, beyond_mm=bound)
        row["culled"] = "box"
        return row, fs
    eng = engine or (ManifoldBoolean() if a.watertight and b.watertight else SampledContainment(sample_spacing(rule.margin_mm)))
    try:
        ov = eng.overlap(a, b)
    except GeomError as e:
        return clearance_verdict(rule, pa.part, pb.part, zones, error=str(e))
    floor = overlap_floor(a.volume(), b.volume())
    overlapping = (ov.volume_mm3 is not None and ov.volume_mm3 > floor) or (ov.volume_mm3 is None and bool(ov.inside_points))
    if overlapping:
        return clearance_verdict(rule, pa.part, pb.part, zones, ov, floor)
    found = mesh_distance(distance(a), distance(b), bound)
    best = None
    if found is not None:
        d, p, q = found
        best = (d, (float(p[0]), float(p[1]), float(p[2])), (float(q[0]), float(q[1]), float(q[2])))
    return clearance_verdict(rule, pa.part, pb.part, zones, ov, floor, distance=best, sampling_mm=0.0, beyond_mm=bound)
