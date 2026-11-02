"""One finding model for the pre-flight checks: `shambu printability` (mesh) and the file integrity lines of
`shambu inspect` and `shambu verify`.

A finding has a stable dotted `code`, a severity (FAIL, WARN, INFO: the words of `verify.Line` and `layout.Check`),
an `area`, a one-line message, the evidence level behind it, and optionally the object, a location, the numbers it
measured (the unit is in each key) and a fix (a sentence, a spec fragment, a shambu command).

Evidence levels (reference 08 section 12, reference 03 section 2.9):

- `measured`: computed from the file or the mesh in hand. Only a measured finding can be FAIL.
- `observed`: seen in a Bambu Studio 02.08.02.61 X2D export and recorded in a shambu reference. WARN at most.
- `external`: reported by a third party on another printer or Studio version and not reproduced on the X2D. WARN at
  most; the checks in this release keep every external finding at INFO.
- `heuristic`: shambu's own rule or band. WARN at most.

The constructor refuses a finding that breaks these ceilings, so no check can raise an external or heuristic claim to
a FAIL by mistake.

What this module does not do: it runs no check, reads no file and prints nothing (cli.py prints).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

FAIL, WARN, INFO = "FAIL", "WARN", "INFO"
SEVERITIES = (FAIL, WARN, INFO)
MEASURED, OBSERVED, EXTERNAL, HEURISTIC = "measured", "observed", "external", "heuristic"
EVIDENCE = (MEASURED, OBSERVED, EXTERNAL, HEURISTIC)
_RANK = {INFO: 0, WARN: 1, FAIL: 2}
CEILING = {MEASURED: FAIL, OBSERVED: WARN, EXTERNAL: WARN, HEURISTIC: WARN}
_TAG = {
    MEASURED: "[measured]",
    OBSERVED: "[observed]",
    EXTERNAL: "[external, not reproduced on X2D]",
    HEURISTIC: "[heuristic]",
}

Vec = tuple[float, float, float]


def evidence_tag(evidence: str) -> str:
    """The text form of an evidence level, e.g. `[measured]`."""
    return _TAG[evidence]


def _r(v: float, nd: int = 2) -> float:
    return round(v + 0.0, nd)  # + 0.0 turns -0.0 into 0.0, so JSON never shows "-0.0"


def _vec(v: Vec) -> list[float]:
    return [_r(x) for x in v]


@dataclass(frozen=True)
class Location:
    frame: str  # "posed" (an STL after the pose, bed at z 0) or "plate" (plate-local mm, as `inspect`)
    point: Vec  # a region centroid or the thinnest sample
    z: tuple[float, float] | None = None  # z range of the region
    bbox: tuple[Vec, Vec] | None = None

    def text(self) -> str:
        s = "at (" + ", ".join(f"{v:.1f}".replace("-0.0", "0.0") if abs(v) < 0.05 else f"{v:.1f}" for v in self.point) + ")"
        if self.z is not None:
            s += f" z {self.z[0]:.1f}..{self.z[1]:.1f}"
        return s

    def to_json(self) -> dict:
        return {
            "frame": self.frame,
            "point": _vec(self.point),
            "z": [_r(self.z[0]), _r(self.z[1])] if self.z is not None else None,
            "bbox": [_vec(self.bbox[0]), _vec(self.bbox[1])] if self.bbox is not None else None,
        }


@dataclass(frozen=True)
class Fix:
    text: str  # one sentence: the action
    spec: dict | None = None  # a spec fragment, e.g. {"recipes": ["tree-supports"]}
    command: str | None = None  # a shambu command line

    def to_json(self) -> dict:
        return {"text": self.text, "spec": self.spec, "command": self.command}


@dataclass(frozen=True)
class Finding:
    code: str  # stable and dotted: "overhang.region"
    severity: str  # FAIL, WARN or INFO
    area: str  # "support", "walls", "gcode", ...
    message: str
    evidence: str  # one of EVIDENCE
    object: str | None = None  # the object or file the finding is about
    location: Location | None = None
    measure: dict = field(default_factory=dict)  # numbers with the unit in the key: {"area_mm2": 210.4}
    fix: Fix | None = None
    plate: int | None = None  # integrity findings: the plate they are about

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"{self.code}: severity {self.severity!r} is not one of {SEVERITIES}")
        if self.evidence not in EVIDENCE:
            raise ValueError(f"{self.code}: evidence {self.evidence!r} is not one of {EVIDENCE}")
        if _RANK[self.severity] > _RANK[CEILING[self.evidence]]:
            raise ValueError(f"{self.code}: {self.evidence} evidence cannot carry {self.severity} (at most {CEILING[self.evidence]})")

    def text(self) -> str:
        """`WARN overhang.region bracket at (12.0, 4.5, 18.2) z 17.0..19.5: 210 mm2 ... [measured]. Fix: ...`"""
        head = f"{self.severity} {self.code}"
        if self.object:
            head += f" {self.object}"
        if self.plate is not None:
            head += f" plate {self.plate}"
        if self.location is not None:
            head += f" {self.location.text()}"
        s = f"{head}: {self.message} {evidence_tag(self.evidence)}"
        if self.fix is not None:
            s += f". Fix: {self.fix.text}"
        return s

    def to_json(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "area": self.area,
            "evidence": self.evidence,
            "object": self.object,
            "plate": self.plate,
            "message": self.message,
            "location": self.location.to_json() if self.location is not None else None,
            "measure": {k: (_r(v, 3) if isinstance(v, float) else v) for k, v in self.measure.items()},
            "fix": self.fix.to_json() if self.fix is not None else None,
        }


def tally(findings: Iterable[Finding]) -> dict[str, int]:
    """Findings counted by severity: {"FAIL": n, "WARN": n, "INFO": n}. Gates count codes and severities, never a score."""
    out = dict.fromkeys(SEVERITIES, 0)
    for f in findings:
        out[f.severity] += 1
    return out
