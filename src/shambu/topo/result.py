"""The result of every `shambu topo` verb: one `TopoReport` with the verb's data, its findings and the not-checked
list, as one JSON document and as text lines.

Findings use the shared finding model (`shambu.findings`): a stable dotted code, a severity and an evidence level.
`CODES` lists every topology code with its severity, evidence and area; `finding()` builds one. Five codes are FAIL
(`topo.equilibrium`, `topo.passive_not_one`, `topo.clearance`, `topo.under_constrained`, `topo.non_finite`): each
means the run broke its own invariant or has no valid answer, so the verb exits 1. Every other code is WARN or INFO
and leaves the exit code alone: the run is a map, not a proof. `seal` replaces a NaN or an infinity in a report's
data with null and adds `topo.non_finite`, so every document is strict JSON and never reads ok with a non-number.

What it does not do: no checking, no numpy, no file I/O and no printing (cli.py prints `lines()`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ..findings import FAIL, HEURISTIC, INFO, MEASURED, WARN, Finding, tally

SCHEMAS = {
    "check": "shambu.topo.check/1",
    "voxelise": "shambu.topo.domain/1",
    "solve": "shambu.topo.run/1",
    "validate": "shambu.topo.validate/1",
    "trace": "shambu.topo.trace/1",
}

# code: (severity, evidence, area, the standing message)
CODES: dict[str, tuple[str, str, str, str]] = {
    "topo.map_not_proof": (
        WARN,
        HEURISTIC,
        "result",
        "a linear, isotropic, static stiffness map: no stress, buckling, fatigue, layer anisotropy or proof load. "
        "A low-density cell works less than its neighbours under these cases; that does not prove it can go",
    ),
    "topo.stiffness_not_strength": (
        WARN,
        HEURISTIC,
        "result",
        "compliance ratios measure stiffness: under fixed loads a ratio of 1.46 means 46 % more deflection under the case, "
        "not 46 % more stress. With self-weight that scales with density the lighter part also carries less load, so "
        "the ratio is not a deflection ratio",
    ),
    "topo.pitch_dependent": (
        INFO,
        HEURISTIC,
        "result",
        "compliance changes with the cell pitch; compare runs at one pitch only",
    ),
    "topo.rigid_load_patch": (
        INFO,
        HEURISTIC,
        "loads",
        "each load is spread over its nodes as a rigid insert would spread it; a soft or local contact concentrates more",
    ),
    "topo.clamped_support": (
        INFO,
        HEURISTIC,
        "loads",
        "supports clamp their nodes; a clamp is stiffer than a bolted joint, so results near a support are optimistic",
    ),
    "topo.not_converged": (WARN, MEASURED, "solver", "the run stopped at its iteration limit before the change fell below the tolerance"),
    "topo.budget_stop": (WARN, MEASURED, "solver", "the time budget stopped the run: the result is a partial run"),
    "topo.voxel_volume": (WARN, MEASURED, "domain", "the voxel volume differs from the mesh volume by more than 2 %"),
    "topo.odd_parity": (WARN, MEASURED, "domain", "some ray columns crossed the mesh an odd number of times: the mesh is open there"),
    "topo.open_mesh": (WARN, MEASURED, "domain", "the mesh has open or non-manifold edges: the fill can be wrong near them"),
    "topo.inverted_mesh": (
        WARN,
        MEASURED,
        "domain",
        "the mesh's signed volume is negative (its facets wind inward); the fill ignores winding, other tools may not",
    ),
    "topo.dropped_pieces": (WARN, MEASURED, "domain", "cells outside the largest connected piece were removed before the solve"),
    "topo.thin_walls": (WARN, HEURISTIC, "domain", "most design cells lie within 3 cells of the surface: walls this thin limit the map"),
    "topo.filter_small": (
        WARN,
        HEURISTIC,
        "solver",
        "the filter radius is small: checkerboards or members thinner than the printer can make",
    ),
    "topo.unloaded_region": (
        WARN,
        MEASURED,
        "result",
        "nothing loads this region at full density, so it reads low because it is idle, not spare",
    ),
    "topo.inert_case": (
        WARN,
        MEASURED,
        "result",
        "this case's compliance did not change: it loads only passive material and does not shape the map",
    ),
    "topo.case_dominates": (WARN, MEASURED, "result", "one case makes more than 80 % of the objective and sets the map alone"),
    "topo.sens_clipped": (
        WARN,
        MEASURED,
        "solver",
        "some sensitivities were positive (added mass hurts there) and were clipped: the update ignores that",
    ),
    "topo.load_fallback": (WARN, MEASURED, "loads", "a load selection was empty and grew: the load sits on material near the stated place"),
    "topo.passive_not_one": (FAIL, MEASURED, "result", "a passive region's density is below 1: a solver bug"),
    "topo.equilibrium": (FAIL, MEASURED, "solver", "reactions do not balance the loads at full density: a model or solver bug"),
    "topo.under_constrained": (
        FAIL,
        MEASURED,
        "solver",
        "the supports leave a rigid-body mode free, or the stiffness is singular: no displacement is unique",
    ),
    "topo.non_finite": (FAIL, MEASURED, "result", "a result holds NaN or infinity: it is not a number to act on"),
    "topo.volume_missed": (
        WARN,
        MEASURED,
        "solver",
        "the design volume missed its target by more than 0.005: the map answers another volume than the one asked",
    ),
    "topo.trace_rules_dominate": (WARN, HEURISTIC, "trace", "the rules, not the map, removed most of the candidate area"),
    "topo.roof_bridge": (WARN, MEASURED, "trace", "a window roof is flatter than the report angle: a short bridge to check in the slicer"),
    "topo.clearance": (FAIL, MEASURED, "trace", "a window comes closer to a keep-out than the clearance rule allows"),
}

NOT_CHECKED: tuple[dict[str, str], ...] = (
    {
        "what": "strength: stress, yield and fracture",
        "why": "the objective is compliance (stiffness)",
        "use": "a stress analysis or a proof-load test print",
    },
    {
        "what": "buckling, large deflection, fatigue and creep",
        "why": "the model is linear and static",
        "use": "a test print under the real load",
    },
    {
        "what": "print anisotropy: layer adhesion, walls and infill",
        "why": "the material is solid and isotropic",
        "use": "a test print in the print orientation",
    },
    {
        "what": "the real joints: bolts, contact and preload",
        "why": "supports are clamps and loads are rigid patches",
        "use": "the joint's own check",
    },
    {"what": "the redesigned part", "why": "the map is not the part", "use": "a new solve or a stress check on the new CAD export"},
    {
        "what": "printability of the result",
        "why": "the map ignores overhangs, bridges and walls",
        "use": "shambu printability on the new export",
    },
)

ALWAYS = ("topo.map_not_proof", "topo.stiffness_not_strength", "topo.pitch_dependent")


def finding(code: str, message: str | None = None, measure: dict | None = None, obj: str | None = None) -> Finding:
    sev, ev, area, text = CODES[code]
    return Finding(code, sev, area, message or text, ev, object=obj, measure=dict(measure or {}))


def _non_finite(x: Any, path: str, found: list[str]) -> Any:
    """`x` with every NaN or infinity nulled; dicts and lists change in place (callers hold references into them)."""
    if isinstance(x, dict):
        for k, v in x.items():
            x[k] = _non_finite(v, f"{path}.{k}", found)
        return x
    if isinstance(x, list):
        for i, v in enumerate(x):
            x[i] = _non_finite(v, f"{path}[{i}]", found)
        return x
    if isinstance(x, tuple):
        return [_non_finite(v, f"{path}[{i}]", found) for i, v in enumerate(x)]
    if isinstance(x, (bool, int, str)) or x is None or not hasattr(x, "__float__"):
        return x
    if math.isfinite(float(x)):
        return x
    found.append(path)
    return None


def seal(rep: TopoReport) -> TopoReport:
    """Null every NaN or infinity in the data and add a `topo.non_finite` FAIL that names where they were."""
    found: list[str] = []
    rep.data = _non_finite(rep.data, "", found)
    if found:
        shown = ", ".join(p.lstrip(".") for p in found[:8]) + (f" and {len(found) - 8} more" if len(found) > 8 else "")
        rep.findings.append(
            finding("topo.non_finite", f"{len(found)} value(s) were NaN or infinite, now null: {shown}", {"count": len(found)})
        )
    return rep


def standing() -> list[Finding]:
    """The findings every solve, validate and trace result carries."""
    return [finding(c) for c in ALWAYS]


@dataclass
class TopoReport:
    verb: str
    data: dict
    findings: list[Finding] = field(default_factory=list)
    not_checked: list[dict] = field(default_factory=lambda: [dict(x) for x in NOT_CHECKED])
    text: list[str] = field(default_factory=list)  # verb summary lines for the text output
    failed: bool = False  # a verb-level failure that is not a finding (validate's shape test)

    @property
    def counts(self) -> dict[str, int]:
        return tally(self.findings)

    @property
    def ok(self) -> bool:
        return not self.failed and self.counts[FAIL] == 0

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def ordered(self) -> list[Finding]:
        rank = {FAIL: 0, WARN: 1, INFO: 2}
        return sorted(self.findings, key=lambda f: (rank[f.severity], f.code, f.object or "", f.message))

    def to_json(self) -> dict:
        return {
            "schema": SCHEMAS[self.verb],
            "verb": self.verb,
            "ok": self.ok,
            **self.data,
            "findings": [f.to_json() for f in self.ordered()],
            "not_checked": self.not_checked,
            "counts": self.counts,
        }

    def lines(self) -> list[str]:
        out = list(self.text)
        out += [f.text() for f in self.ordered()]
        if self.not_checked:
            out.append("not checked: " + "; ".join(f"{x['what']} ({x['use']})" for x in self.not_checked))
        c = self.counts
        word = "ok" if self.ok else "fail"
        out.append(f"topo {self.verb}: {word} ({c[FAIL]} FAIL, {c[WARN]} WARN, {c[INFO]} INFO)")
        return out
