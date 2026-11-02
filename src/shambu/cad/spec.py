"""The CAD gate spec (`shambu.cad/1`): one JSON file that holds the scene, the print intent and the rules, so the
numbers of a product stay in the user's repository, not in shambu.

Shape (paths are relative to the spec file):

    {"schema": "shambu.cad/1",
     "provenance": {"inputs": ["params.json", "parts/*.js"], "rev": "B"},
     "intent": {"material": "PETG", "density_g_cm3": 1.27, "hole_allowance_mm": {"vertical": 0.1, "horizontal": 0.25},
                "bed_face": [0, 0, -1], "general_tolerance_mm": 0.2, "critical": [...]},
     "fits": [{"name": "insert-M3", "kind": "insert", "hole_mm": 4.0, "depth_min_mm": 6, "basis": "insert datasheet"}],
     "parts": [{"name": "base", "mesh": "out/base@B.stl", "record": "out/base@B.cad.json", "role": "rigid",
                "group": "frame", "placement": {"origin": [0, 0, 0], "x": [1, 0, 0], "y": [0, 1, 0]},
                "expected_volume_mm3": 41980, "volume_tol_mm3": 60, "expected_size_mm": [62, 40, 24], "size_tol_mm": 0.05,
                "expected_features": [{"kind": "hole", "diameter_mm": 4.0, "count": 4}],
                "holes": [{"name": "h1", "fit": "insert-M3", "mouth": [5, 5, 24], "axis": [0, 0, -1]}]}],
     "zones": {"seat": {"z": [23.9, 24.1]}},
     "poses": {"open": {"lid": {"origin": [0, 0, 24], "x": [1, 0, 0], "y": [0, 0, 1]}}},
     "rules": [{"id": "solids", "type": "check", "parts": "role:!reference"}, ...]}

Rule types: `check`, `features`, `fit`, `clearance`, `sweep`. Selectors (`part:NAME`, `role:ROLE`, `role:!ROLE`,
`group:GROUP`, `all`) pick parts, never file names, so a renamed mesh is still found by its role.

What it handles: parsing and validation (an exemption without a reason, a non-orthonormal placement, an unknown rule
type, a missing required key are spec errors: `CadSpecError`, exit 1), the unknown keys (listed, never dropped
silently) and the keys no rule reads. What it does not do: no file reading beyond the JSON text the caller passes,
no mesh loading (load.py) and no rule running (rules.py, run.py).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..errors import CadSpecError
from . import fits as F
from .linalg import Placement, Vec, cross, dot, norm

SCHEMA = "shambu.cad/1"
ROLES = ("rigid", "moving", "breakaway", "reference")
RULE_TYPES = ("check", "features", "fit", "clearance", "sweep")
ORTHO_TOL = 1e-6  # a placement's axes must be unit and orthogonal to this (they come from a CAD frame, exactly)

_TOP = {"schema", "provenance", "intent", "fits", "parts", "zones", "poses", "rules", "description"}
_PART = {
    "name",
    "mesh",
    "object",
    "record",
    "role",
    "group",
    "placement",
    "expected_volume_mm3",
    "volume_tol_mm3",
    "expected_size_mm",
    "size_tol_mm",
    "expected_features",
    "holes",
    "density_g_cm3",
    "bed_face",
}
_INTENT_READ = {"density_g_cm3", "hole_allowance_mm", "bed_face"}  # the intent keys a rule reads; the rest are for people
_INTENT = _INTENT_READ | {"material", "process", "general_tolerance_mm", "critical", "coupons", "notes"}
_PROVENANCE = {"inputs", "rev"}
_HOLE = {"name", "fit", "mouth", "axis", "offset_max_mm"}
_FEATURE = {"kind", "diameter_mm", "count", "diameter_tol_mm", "pcd_mm", "angles_deg", "angle_tol_deg", "axis"}
_EXEMPT = {"a", "b", "reason", "zone", "max_overlap_mm3"}
_RULE_KEYS = {
    "check": {"id", "type", "parts"},
    "features": {"id", "type", "parts"},
    "fit": {"id", "type", "parts"},
    "clearance": {"id", "type", "a", "b", "margin_mm", "exempt", "skip_same_group", "pose"},
    "sweep": {
        "id",
        "type",
        "moving",
        "fixed",
        "motion",
        "axis",
        "point",
        "from_mm",
        "step_mm",
        "resolution_mm",
        "from_deg",
        "to_deg",
        "step_deg",
        "resolution_deg",
        "margin_mm",
        "contact_zone",
        "first_contact_after_mm",
        "first_contact_after_deg",
        "max_overlap_mm3",
    },
}


# ---------------------------------------------------------------------------- selectors and zones
@dataclass(frozen=True)
class Selector:
    kind: str  # "part", "role", "group" or "all"
    value: str = ""
    negate: bool = False

    @classmethod
    def parse(cls, text: Any, where: str) -> Selector:
        if not isinstance(text, str) or not text:
            raise CadSpecError(f"{where}: a selector is a string such as 'part:base', 'role:rigid', 'role:!reference', 'group:frame'")
        if text == "all":
            return cls("all")
        kind, sep, value = text.partition(":")
        if not sep or kind not in ("part", "role", "group") or not value.lstrip("!"):
            raise CadSpecError(f"{where}: bad selector {text!r} (part:NAME, role:ROLE, role:!ROLE, group:GROUP or all)")
        neg = value.startswith("!")
        value = value[1:] if neg else value
        if kind == "role" and value not in ROLES:
            raise CadSpecError(f"{where}: role {value!r} is not one of {ROLES}")
        return cls(kind, value, neg)

    def match(self, part: PartSpec) -> bool:
        if self.kind == "all":
            return True
        got = {"part": part.name, "role": part.role, "group": part.group}[self.kind]
        return (got == self.value) != self.negate

    def text(self) -> str:
        return "all" if self.kind == "all" else f"{self.kind}:{'!' if self.negate else ''}{self.value}"


@dataclass(frozen=True)
class Zone:
    name: str
    ranges: tuple[tuple[int, float, float], ...]  # (axis index, low, high)

    def contains(self, p: Sequence[float], pad: float = 0.0) -> bool:
        return all(lo - pad <= p[i] <= hi + pad for i, lo, hi in self.ranges)

    def contains_box(self, lo: Sequence[float], hi: Sequence[float], pad: float = 0.0) -> bool:
        return all(a - pad <= lo[i] and hi[i] <= b + pad for i, a, b in self.ranges)

    def to_json(self) -> dict:
        return {"xyz"[i]: [lo, hi] for i, lo, hi in self.ranges}


# ---------------------------------------------------------------------------- parts
@dataclass(frozen=True)
class HoleSpec:
    name: str
    fit: str
    mouth: Vec
    axis: Vec
    offset_max_mm: float | None = None


@dataclass(frozen=True)
class ExpectedFeature:
    kind: str  # "hole" or "boss"
    diameter_mm: float
    count: int
    diameter_tol_mm: float = 0.02  # the vertex circle fit reads the CAD diameter to rounding (features.py)
    pcd_mm: float | None = None
    angles_deg: tuple[float, ...] | None = None
    angle_tol_deg: float = 0.5
    axis: Vec | None = None


@dataclass(frozen=True)
class PartSpec:
    name: str
    mesh: str
    object: str | None = None  # a 3MF object name when `mesh` is a .3mf
    record: str | None = None
    role: str = "rigid"
    role_from: str = "default"  # "spec" when the spec names it
    group: str = ""
    placement: Placement = field(default_factory=Placement)
    expected_volume_mm3: float | None = None
    volume_tol_mm3: float | None = None
    expected_size_mm: Vec | None = None
    size_tol_mm: float | None = None
    expected_features: tuple[ExpectedFeature, ...] = ()
    holes: tuple[HoleSpec, ...] = ()
    density_g_cm3: float | None = None
    bed_face: Vec | None = None


# ---------------------------------------------------------------------------- rules
@dataclass(frozen=True)
class Exemption:
    a: Selector
    b: Selector
    reason: str
    zone: str | None = None
    max_overlap_mm3: float | None = None

    def covers(self, pa: PartSpec, pb: PartSpec) -> bool:
        return (self.a.match(pa) and self.b.match(pb)) or (self.a.match(pb) and self.b.match(pa))


@dataclass(frozen=True)
class RuleSpec:
    id: str
    type: str


@dataclass(frozen=True)
class PartsRuleSpec(RuleSpec):
    """`check`, `features` and `fit`: one selector of parts."""

    parts: Selector = Selector("all")


@dataclass(frozen=True)
class ClearanceRuleSpec(RuleSpec):
    a: Selector = Selector("all")
    b: Selector = Selector("all")
    margin_mm: float = 0.0
    exempt: tuple[Exemption, ...] = ()
    skip_same_group: bool = False
    pose: str | None = None


@dataclass(frozen=True)
class SweepRuleSpec(RuleSpec):
    moving: Selector = Selector("all")
    fixed: Selector = Selector("all")
    motion: str = "translate"  # or "rotate"
    axis: Vec = (0.0, 0.0, -1.0)
    point: Vec = (0.0, 0.0, 0.0)
    start: float = 0.0  # translate: from_mm (travel to the seat); rotate: from_deg
    end: float = 0.0  # translate: 0 (seated); rotate: to_deg
    step: float = 1.0
    resolution: float = 0.02
    margin_mm: float = 0.0
    contact_zone: str | None = None
    first_contact_after: float | None = None  # travel (mm or deg from the start) before which contact is a FAIL
    max_overlap_mm3: float | None = None

    @property
    def unit(self) -> str:
        return "mm" if self.motion == "translate" else "deg"


@dataclass(frozen=True)
class CadSpec:
    path: str
    base: str  # the directory paths are relative to
    parts: tuple[PartSpec, ...]
    rules: tuple[RuleSpec, ...]
    zones: dict[str, Zone] = field(default_factory=dict)
    poses: dict[str, dict[str, Placement]] = field(default_factory=dict)
    fits: dict[str, F.Fit] = field(default_factory=dict)
    fit_notes: tuple[str, ...] = ()
    provenance_inputs: tuple[str, ...] = ()
    rev: str | None = None
    intent: dict = field(default_factory=dict)
    unknown_keys: tuple[str, ...] = ()
    unread: tuple[str, ...] = ()

    def part(self, name: str) -> PartSpec:
        for p in self.parts:
            if p.name == name:
                return p
        raise CadSpecError(f"{self.path}: no part named {name!r}; parts: {[p.name for p in self.parts]}")

    def select(self, sel: Selector) -> list[PartSpec]:
        return [p for p in self.parts if sel.match(p)]

    def density(self, part: PartSpec) -> float | None:
        d = part.density_g_cm3 if part.density_g_cm3 is not None else self.intent.get("density_g_cm3")
        return float(d) if d is not None else None

    def bed_face(self, part: PartSpec) -> Vec | None:
        if part.bed_face is not None:
            return part.bed_face
        v = self.intent.get("bed_face")
        return _unit3(v, "intent.bed_face") if v is not None else None

    def hole_allowance(self) -> dict:
        v = self.intent.get("hole_allowance_mm")
        return dict(v) if isinstance(v, Mapping) else {}


# ---------------------------------------------------------------------------- parsing
def _vec3(v: Any, where: str) -> Vec:
    if not isinstance(v, (list, tuple)) or len(v) != 3 or any(isinstance(x, bool) for x in v):
        raise CadSpecError(f"{where}: expected [x, y, z] (numbers), got {v!r}")
    try:
        out = (float(v[0]), float(v[1]), float(v[2]))
    except (TypeError, ValueError) as e:
        raise CadSpecError(f"{where}: {e}") from e
    if not all(math.isfinite(x) for x in out):
        raise CadSpecError(f"{where}: not finite: {v!r}")
    return out


def _unit3(v: Any, where: str) -> Vec:
    x = _vec3(v, where)
    n = norm(x)
    if n == 0:
        raise CadSpecError(f"{where}: a zero-length axis")
    return (x[0] / n, x[1] / n, x[2] / n)


def _num(d: Mapping, k: str, where: str, default: float | None = None, positive: bool = False, nonneg: bool = False) -> float | None:
    """The number at `d[k]`, `default` when it is absent or null. A JSON boolean is not a number. `positive` refuses
    zero and below; `nonneg` (tolerances, margins, caps) refuses below zero."""
    if d.get(k) is None:
        return default
    if isinstance(d[k], bool):
        raise CadSpecError(f"{where}.{k}: must be a number, got {d[k]!r}")
    try:
        v = float(d[k])
    except (TypeError, ValueError) as e:
        raise CadSpecError(f"{where}.{k}: {e}") from e
    if not math.isfinite(v) or (positive and v <= 0) or (nonneg and v < 0):
        want = "positive " if positive else "non-negative " if nonneg else ""
        raise CadSpecError(f"{where}.{k}: must be a {want}number, got {d[k]!r}")
    return v


def _num_or(d: Mapping, k: str, where: str, default: float, positive: bool = False, nonneg: bool = False) -> float:
    """`_num` with a default: an explicit 0 stays 0 (never the default)."""
    v = _num(d, k, where, default, positive, nonneg)
    return default if v is None else v


def _intent(intent: Mapping) -> None:
    """Validates the intent keys a rule reads."""
    _num(intent, "density_g_cm3", "intent", positive=True)
    if intent.get("bed_face") is not None:
        _unit3(intent["bed_face"], "intent.bed_face")
    allow = intent.get("hole_allowance_mm")
    if allow is not None:
        if not isinstance(allow, Mapping):
            raise CadSpecError(f"intent.hole_allowance_mm: an object such as {{'vertical': 0.1, 'horizontal': 0.25}}, got {allow!r}")
        for k in allow:
            _num(allow, k, "intent.hole_allowance_mm", nonneg=True)


def placement(d: Any, where: str) -> Placement:
    if not isinstance(d, Mapping):
        raise CadSpecError(f"{where}: a placement is {{origin, x, y}}")
    unknown = sorted(set(d) - {"origin", "x", "y"})
    if unknown:
        raise CadSpecError(f"{where}: unknown placement keys {unknown}")
    o = _vec3(d.get("origin", [0, 0, 0]), f"{where}.origin")
    x = _vec3(d.get("x", [1, 0, 0]), f"{where}.x")
    y = _vec3(d.get("y", [0, 1, 0]), f"{where}.y")
    if abs(norm(x) - 1) > ORTHO_TOL or abs(norm(y) - 1) > ORTHO_TOL or abs(dot(x, y)) > ORTHO_TOL:
        raise CadSpecError(f"{where}: x and y must be orthonormal (unit length, at right angles); got x {list(x)}, y {list(y)}")
    if norm(cross(x, y)) < 1 - ORTHO_TOL:
        raise CadSpecError(f"{where}: x and y are parallel")
    return Placement(o, x, y)


class _Reader:
    def __init__(self, path: str) -> None:
        self.path = path
        self.unknown: list[str] = []

    def keys(self, d: Mapping, allowed: set[str], where: str) -> None:
        self.unknown += [f"{where}.{k}" if where else k for k in sorted(set(d) - allowed)]


def _feature(d: Any, where: str) -> ExpectedFeature:
    if not isinstance(d, Mapping):
        raise CadSpecError(f"{where}: an expected feature is an object")
    for req in ("kind", "diameter_mm"):
        if req not in d:
            raise CadSpecError(f"{where}: needs {req!r}")
    if d["kind"] not in ("hole", "boss"):
        raise CadSpecError(f"{where}.kind: 'hole' or 'boss', got {d['kind']!r}")
    angles = d.get("angles_deg")
    if angles is not None and (
        not isinstance(angles, list) or not all(isinstance(a, (int, float)) and not isinstance(a, bool) for a in angles)
    ):
        raise CadSpecError(f"{where}.angles_deg: a list of numbers")
    count = d.get("count", len(angles) if angles else 1)
    if not isinstance(count, int) or count < 1:
        raise CadSpecError(f"{where}.count: a positive integer")
    return ExpectedFeature(
        d["kind"],
        _num(d, "diameter_mm", where, positive=True) or 0.0,
        count,
        _num_or(d, "diameter_tol_mm", where, 0.02, nonneg=True),
        _num(d, "pcd_mm", where),
        tuple(float(a) for a in angles) if angles else None,
        _num_or(d, "angle_tol_deg", where, 0.5, nonneg=True),
        _unit3(d["axis"], f"{where}.axis") if d.get("axis") is not None else None,
    )


def _part(d: Any, i: int, r: _Reader) -> PartSpec:
    where = f"parts[{i}]"
    if not isinstance(d, Mapping):
        raise CadSpecError(f"{where}: a part is an object")
    for req in ("name", "mesh"):
        if not d.get(req) or not isinstance(d[req], str):
            raise CadSpecError(f"{where}: needs {req!r} (a string)")
    r.keys(d, _PART, where)
    role = d.get("role")
    if role is not None and role not in ROLES:
        raise CadSpecError(f"{where}.role: {role!r} is not one of {ROLES}")
    holes = []
    for k, h in enumerate(d.get("holes") or []):
        hw = f"{where}.holes[{k}]"
        if not isinstance(h, Mapping):
            raise CadSpecError(f"{hw}: a hole is an object")
        r.keys(h, _HOLE, hw)
        for req in ("name", "fit", "mouth", "axis"):
            if req not in h:
                raise CadSpecError(f"{hw}: needs {req!r}")
        holes.append(
            HoleSpec(
                str(h["name"]),
                str(h["fit"]),
                _vec3(h["mouth"], f"{hw}.mouth"),
                _unit3(h["axis"], f"{hw}.axis"),
                _num(h, "offset_max_mm", hw, nonneg=True),
            )
        )
    feats = []
    for k, fd in enumerate(d.get("expected_features") or []):
        if isinstance(fd, Mapping):
            r.keys(fd, _FEATURE, f"{where}.expected_features[{k}]")
        feats.append(_feature(fd, f"{where}.expected_features[{k}]"))
    if d.get("expected_volume_mm3") is not None and d.get("volume_tol_mm3") is None:
        raise CadSpecError(f"{where}: expected_volume_mm3 needs volume_tol_mm3 (no hidden default)")
    if d.get("expected_size_mm") is not None and d.get("size_tol_mm") is None:
        raise CadSpecError(f"{where}: expected_size_mm needs size_tol_mm (no hidden default)")
    return PartSpec(
        d["name"],
        d["mesh"],
        d.get("object"),
        d.get("record"),
        role or "rigid",
        "spec" if role else "default",
        str(d.get("group") or ""),
        placement(d["placement"], f"{where}.placement") if d.get("placement") is not None else Placement(),
        _num(d, "expected_volume_mm3", where),
        _num(d, "volume_tol_mm3", where, nonneg=True),
        _vec3(d["expected_size_mm"], f"{where}.expected_size_mm") if d.get("expected_size_mm") is not None else None,
        _num(d, "size_tol_mm", where, nonneg=True),
        tuple(feats),
        tuple(holes),
        _num(d, "density_g_cm3", where, positive=True),
        _unit3(d["bed_face"], f"{where}.bed_face") if d.get("bed_face") is not None else None,
    )


def _rule(d: Any, i: int, r: _Reader, zones: Mapping[str, Zone], poses: Mapping[str, Any]) -> RuleSpec:
    where = f"rules[{i}]"
    if not isinstance(d, Mapping):
        raise CadSpecError(f"{where}: a rule is an object")
    rid, rtype = d.get("id"), d.get("type")
    if not rid or not isinstance(rid, str):
        raise CadSpecError(f"{where}: needs an 'id' (a string)")
    if rtype not in RULE_TYPES:
        raise CadSpecError(f"{where} ({rid}): type {rtype!r} is not one of {RULE_TYPES}")
    where = f"rules[{i}] ({rid})"
    r.keys(d, _RULE_KEYS[rtype], f"rules[{i}]")
    if rtype in ("check", "features", "fit"):
        return PartsRuleSpec(rid, rtype, Selector.parse(d.get("parts", "all"), f"{where}.parts"))
    if rtype == "clearance":
        exempt = []
        for k, e in enumerate(d.get("exempt") or []):
            ew = f"{where}.exempt[{k}]"
            if not isinstance(e, Mapping):
                raise CadSpecError(f"{ew}: an exemption is an object")
            r.keys(e, _EXEMPT, f"rules[{i}].exempt[{k}]")
            if not str(e.get("reason") or "").strip():
                raise CadSpecError(f"{ew}: every exemption needs a non-empty 'reason'")
            zone = e.get("zone")
            if zone is not None and zone not in zones:
                raise CadSpecError(f"{ew}: zone {zone!r} is not in 'zones' ({sorted(zones)})")
            exempt.append(
                Exemption(
                    Selector.parse(e.get("a"), f"{ew}.a"),
                    Selector.parse(e.get("b"), f"{ew}.b"),
                    str(e["reason"]).strip(),
                    zone,
                    _num(e, "max_overlap_mm3", ew, nonneg=True),
                )
            )
        pose = d.get("pose")
        if pose is not None and pose not in poses:
            raise CadSpecError(f"{where}.pose: {pose!r} is not in 'poses' ({sorted(poses)})")
        return ClearanceRuleSpec(
            rid,
            rtype,
            Selector.parse(d.get("a", "all"), f"{where}.a"),
            Selector.parse(d.get("b", "all"), f"{where}.b"),
            _num_or(d, "margin_mm", where, 0.0, nonneg=True),
            tuple(exempt),
            bool(d.get("skip_same_group", False)),
            pose,
        )
    motion = d.get("motion")
    if motion not in ("translate", "rotate"):
        raise CadSpecError(f"{where}.motion: 'translate' or 'rotate', got {motion!r}")
    zone = d.get("contact_zone")
    if zone is not None and zone not in zones:
        raise CadSpecError(f"{where}.contact_zone: {zone!r} is not in 'zones' ({sorted(zones)})")
    if "axis" not in d:
        raise CadSpecError(f"{where}: needs 'axis'")
    common = {
        "moving": Selector.parse(d.get("moving"), f"{where}.moving"),
        "fixed": Selector.parse(d.get("fixed"), f"{where}.fixed"),
        "motion": motion,
        "axis": _unit3(d["axis"], f"{where}.axis"),
        "margin_mm": _num_or(d, "margin_mm", where, 0.0, nonneg=True),
        "contact_zone": zone,
        "max_overlap_mm3": _num(d, "max_overlap_mm3", where, nonneg=True),
    }
    if motion == "translate":
        if "from_mm" not in d:
            raise CadSpecError(f"{where}: a translation needs 'from_mm' (travel to the seat)")
        start = _num(d, "from_mm", where, positive=True) or 0.0
        return SweepRuleSpec(
            rid,
            rtype,
            start=start,
            end=0.0,
            step=_num(d, "step_mm", where, 1.0, positive=True) or 1.0,
            resolution=_num(d, "resolution_mm", where, 0.02, positive=True) or 0.02,
            first_contact_after=_num(d, "first_contact_after_mm", where),
            **common,
        )
    for req in ("point", "from_deg", "to_deg"):
        if req not in d:
            raise CadSpecError(f"{where}: a rotation needs {req!r}")
    a0, a1 = _num(d, "from_deg", where) or 0.0, _num(d, "to_deg", where) or 0.0
    if a0 == a1:
        raise CadSpecError(f"{where}: from_deg and to_deg are equal")
    return SweepRuleSpec(
        rid,
        rtype,
        point=_vec3(d["point"], f"{where}.point"),
        start=a0,
        end=a1,
        step=_num(d, "step_deg", where, 1.0, positive=True) or 1.0,
        resolution=_num(d, "resolution_deg", where, 0.05, positive=True) or 0.05,
        first_contact_after=_num(d, "first_contact_after_deg", where),
        **common,
    )


def parse(doc: Any, path: str = "cad.json", base: str = ".") -> CadSpec:
    """A `CadSpec` from the decoded JSON. Raises `CadSpecError` for a schema break."""
    if not isinstance(doc, Mapping):
        raise CadSpecError(f"{path}: the spec is a JSON object")
    if doc.get("schema") != SCHEMA:
        raise CadSpecError(f"{path}: 'schema' must be {SCHEMA!r}, got {doc.get('schema')!r}")
    r = _Reader(path)
    r.keys(doc, _TOP, "")
    zones: dict[str, Zone] = {}
    for name, z in (doc.get("zones") or {}).items():
        if not isinstance(z, Mapping) or not z or set(z) - {"x", "y", "z"}:
            raise CadSpecError(f"{path}: zone {name!r} is {{x|y|z: [low, high]}}")
        ranges = []
        for ax, rng in sorted(z.items()):
            if not isinstance(rng, list) or len(rng) != 2 or float(rng[0]) > float(rng[1]):
                raise CadSpecError(f"{path}: zone {name!r}.{ax}: [low, high]")
            ranges.append(("xyz".index(ax), float(rng[0]), float(rng[1])))
        zones[name] = Zone(name, tuple(ranges))
    raw_parts = doc.get("parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        raise CadSpecError(f"{path}: 'parts' must be a non-empty list")
    parts = tuple(_part(p, i, r) for i, p in enumerate(raw_parts))
    names = [p.name for p in parts]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise CadSpecError(f"{path}: part names must be unique: {dupes}")
    poses: dict[str, dict[str, Placement]] = {}
    for pname, pd in (doc.get("poses") or {}).items():
        if not isinstance(pd, Mapping):
            raise CadSpecError(f"{path}: pose {pname!r} maps part names to placements")
        for part_name in pd:
            if part_name not in names:
                raise CadSpecError(f"{path}: pose {pname!r} names no part {part_name!r}")
        poses[pname] = {k: placement(v, f"poses.{pname}.{k}") for k, v in pd.items()}
    intent = doc.get("intent") or {}
    if not isinstance(intent, Mapping):
        raise CadSpecError(f"{path}: 'intent' is an object")
    r.keys(intent, _INTENT, "intent")
    _intent(intent)
    prov = doc.get("provenance") or {}
    if not isinstance(prov, Mapping):
        raise CadSpecError(f"{path}: 'provenance' is an object")
    r.keys(prov, _PROVENANCE, "provenance")
    user_fits = [F.Fit.from_json(f, f"fits[{i}]") for i, f in enumerate(doc.get("fits") or [])]
    fit_table, notes = F.merge(user_fits)
    raw_rules = doc.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise CadSpecError(f"{path}: 'rules' must be a non-empty list")
    rules = tuple(_rule(d, i, r, zones, poses) for i, d in enumerate(raw_rules))
    ids = [x.id for x in rules]
    dupes = sorted({x for x in ids if ids.count(x) > 1})
    if dupes:
        raise CadSpecError(f"{path}: rule ids must be unique: {dupes}")
    for p in parts:
        for h in p.holes:
            if h.fit not in fit_table:
                raise CadSpecError(f"{path}: part {p.name!r} hole {h.name!r} names fit {h.fit!r}, which is neither built in nor in 'fits'")
    unread = sorted(f"intent.{k}" for k in intent if k not in _INTENT_READ)
    if "zones" in doc and not any(isinstance(x, (ClearanceRuleSpec, SweepRuleSpec)) for x in rules):
        unread.append("zones")
    return CadSpec(
        path,
        base,
        parts,
        rules,
        zones,
        poses,
        fit_table,
        tuple(notes),
        tuple(str(x) for x in prov.get("inputs") or []),
        str(prov["rev"]) if prov.get("rev") is not None else None,
        dict(intent),
        tuple(r.unknown),
        tuple(unread),
    )
