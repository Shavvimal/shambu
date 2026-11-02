"""The rules of a CAD gate spec, one class per rule type behind the `CadRule` Protocol: `CheckRule`, `FeatureRule`,
`FitRule` (pure Python) and `ClearanceRule`, `SweepRule` (the `geom` extra, imported when they run). Tests add a fake.

Every rule reads its parts through a `SceneLike` (load.Scene in use, a fake in tests) and turns a part it cannot
evaluate into a finding, never a pass: a selector with no match is `cad.incomplete.selector_empty`, a missing mesh
`cad.incomplete.mesh_missing`, an unreadable one `cad.incomplete.unreadable`, a declared record that is missing
`cad.incomplete.no_record`. Each of these is FAIL with area `inputs`; the gate word is `incomplete` when nothing else
fails.

What it does not do: no file reading of its own (the scene does it), no printing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..errors import CadError, MeshMissing
from ..findings import FAIL, INFO, MEASURED, WARN, Finding, Fix
from ..printability import NOT_CHECKED_STATUS
from . import features as FT
from . import gauge as GA
from . import provenance as P
from . import solid as SO
from .linalg import Placement
from .report import RuleResult, Subject, areas_json, status_of
from .spec import CadSpec, ClearanceRuleSpec, PartSpec, PartsRuleSpec, RuleSpec, Selector, SweepRuleSpec


class SceneLike(Protocol):
    spec: CadSpec

    def subject(self, part: PartSpec) -> Subject: ...
    def record(self, part: PartSpec) -> P.Record | None: ...
    def loaded(self) -> dict[str, Subject]: ...


@dataclass
class Context:
    spec: CadSpec
    scene: SceneLike
    cull: bool = True
    notes: dict = field(default_factory=dict)


class CadRule(Protocol):
    @property
    def spec(self) -> RuleSpec: ...

    def run(self, ctx: Context) -> RuleResult: ...


def _incomplete(code: str, rule: str, message: str, obj: str | None = None, fix: str = "") -> Finding:
    return Finding(f"cad.incomplete.{code}", FAIL, "inputs", f"rule {rule}: {message}", MEASURED, obj, fix=Fix(fix) if fix else None)


def select(ctx: Context, rule: RuleSpec, sel: Selector, findings: list[Finding], drop_reference: bool = False) -> list[PartSpec]:
    parts = ctx.spec.select(sel)
    if drop_reference:
        parts = [p for p in parts if p.role != "reference"]
    if not parts:
        findings.append(
            _incomplete(
                "selector_empty",
                rule.id,
                f"selector {sel.text()} matches no part" + (" that is not a reference" if drop_reference else ""),
                fix="fix the selector or the parts' roles and groups in the spec",
            )
        )
    return parts


def load(ctx: Context, rule: RuleSpec, part: PartSpec, findings: list[Finding]) -> Subject | None:
    try:
        return ctx.scene.subject(part)
    except MeshMissing as e:
        findings.append(_incomplete("mesh_missing", rule.id, f"part {part.name}: {e}", part.name, "export the part, or fix its mesh path"))
    except CadError as e:
        findings.append(_incomplete("unreadable", rule.id, f"part {part.name}: {e}", part.name, "re-export the file"))
    return None


def _record(ctx: Context, rule: RuleSpec, part: PartSpec, findings: list[Finding]) -> tuple[bool, P.Record | None]:
    """(ok, record): ok False when a declared record is missing or does not read (a finding says which)."""
    try:
        return True, ctx.scene.record(part)
    except MeshMissing as e:
        findings.append(_incomplete("no_record", rule.id, f"part {part.name}: {e}", part.name, "run shambu cad stamp on the export"))
    except CadError as e:
        findings.append(_incomplete("unreadable", rule.id, f"part {part.name}: {e}", part.name, "stamp the export again"))
    return False, None


def expect_of(spec: CadSpec, part: PartSpec) -> SO.Expect:
    return SO.Expect(part.expected_volume_mm3, part.volume_tol_mm3, part.expected_size_mm, part.size_tol_mm, spec.density(part), "spec")


def check_subject(subject: Subject, record: P.Record | None, expect: SO.Expect) -> dict:
    """The provenance, solid, volume, size and mass areas of one subject."""
    areas = {}
    if record is None:
        areas["provenance"] = SO.skip("no record (run shambu cad stamp)")
    else:
        measures, fs = P.freshness(record, Path(subject.path), subject.sha256)
        areas["provenance"] = SO.result(measures, fs)
    areas.update(SO.check(subject.name, subject.facets, record.data if record else None, expect))
    return areas


class CheckRule:
    def __init__(self, spec: PartsRuleSpec):
        self.spec = spec

    def run(self, ctx: Context) -> RuleResult:
        fs: list[Finding] = []
        out: dict = {}
        for part in select(ctx, self.spec, self.spec.parts, fs, drop_reference=True):
            subject = load(ctx, self.spec, part, fs)
            ok, record = _record(ctx, self.spec, part, fs)
            if subject is None or not ok:
                continue
            areas = check_subject(subject, record, expect_of(ctx.spec, part))
            fs += [f for a in areas.values() for f in a.findings]
            out[part.name] = {"areas": areas_json(areas)}
            ctx.notes.setdefault("fresh", {})[part.name] = _fresh(areas)
        return RuleResult(self.spec.id, self.spec.type, status_of(fs), data={"parts": out}, findings=fs)


def _fresh(areas: dict) -> str:
    prov = areas.get("provenance")
    if prov is None or prov.status == NOT_CHECKED_STATUS:
        return "no record"
    return "stale" if any(f.code.startswith("cad.stale.") for f in prov.findings) else "ok"


def bed_face(spec: CadSpec, part: PartSpec, record: P.Record | None) -> tuple[float, float, float] | None:
    bf = spec.bed_face(part)
    if bf is None and record is not None:
        v = record.section("print").get("bed_face")
        if isinstance(v, list) and len(v) == 3:
            bf = (float(v[0]), float(v[1]), float(v[2]))
    return bf


def axis_tol(record: P.Record | None) -> float:
    if record is not None:
        a = record.section("stl").get("angle_tol_deg")
        if a is not None:
            return float(a) + 1.0
    return FT.DEFAULT_AXIS_TOL_DEG


class FeatureRule:
    def __init__(self, spec: PartsRuleSpec):
        self.spec = spec

    def run(self, ctx: Context) -> RuleResult:
        fs: list[Finding] = []
        out: dict = {}
        for part in select(ctx, self.spec, self.spec.parts, fs, drop_reference=True):
            subject = load(ctx, self.spec, part, fs)
            ok, record = _record(ctx, self.spec, part, fs)
            if subject is None or not ok:
                continue
            inv = FT.detect(subject.facets, axis_tol(record), 0.0, bed_face(ctx.spec, part, record))
            fs += FT.compare(part.name, inv, part.expected_features)
            out[part.name] = inv.to_json()
        return RuleResult(self.spec.id, self.spec.type, status_of(fs), data={"parts": out}, findings=fs)


class FitRule:
    def __init__(self, spec: PartsRuleSpec):
        self.spec = spec

    def run(self, ctx: Context) -> RuleResult:
        fs: list[Finding] = []
        out: dict = {}
        for note in ctx.spec.fit_notes:
            fs.append(Finding("fit.shadowed", WARN, "fit", note, MEASURED))
        parts = select(ctx, self.spec, self.spec.parts, fs, drop_reference=True)
        for part in parts:
            if not part.holes:
                out[part.name] = {"holes": [], "note": "the part declares no holes"}
                continue
            subject = load(ctx, self.spec, part, fs)
            ok, record = _record(ctx, self.spec, part, fs)
            if subject is None or not ok:
                continue
            gauge = GA.Gauge(subject.facets)
            rows = []
            for h in part.holes:
                fit = ctx.spec.fits[h.fit]
                bore = gauge.bore(h.mouth, h.axis, fit.hole_mm)
                fs += GA.judge(
                    part.name,
                    h.name,
                    fit,
                    bore,
                    h.axis,
                    h.mouth,
                    h.offset_max_mm,
                    bed_face(ctx.spec, part, record),
                    ctx.spec.hole_allowance(),
                )
                rows.append({"name": h.name, "fit": fit.to_json(), "bore": bore.to_json() if bore else None})
            out[part.name] = {"holes": rows}
        return RuleResult(self.spec.id, self.spec.type, status_of(fs), data={"parts": out}, findings=fs)


def _placed(ctx: Context, rule: RuleSpec, parts: list[PartSpec], pose: str | None, fs: list[Finding]) -> list:
    """The parts as geom `Placed` meshes in the shared frame (the pose's placement first, then the part's)."""
    from .geom.clearance import Placed
    from .geom.engines import GMesh

    out = []
    overrides = ctx.spec.poses.get(pose, {}) if pose else {}
    for part in parts:
        subject = load(ctx, rule, part, fs)
        if subject is None:
            continue
        pl: Placement = overrides.get(part.name, part.placement)
        out.append(Placed(part, GMesh.from_facets(part.name, pl.facets(subject.facets))))
    return out


class ClearanceRule:
    def __init__(self, spec: ClearanceRuleSpec):
        self.spec = spec

    def run(self, ctx: Context) -> RuleResult:
        from .geom import require

        require()
        from .geom.clearance import check_pair
        from .geom.engines import GMesh, PrunedDistance
        from .judge import pairs_of

        fs: list[Finding] = []
        a = select(ctx, self.spec, self.spec.a, fs)
        b = select(ctx, self.spec, self.spec.b, fs)
        names = {p.name for p in a} | {p.name for p in b}
        placed = _placed(ctx, self.spec, [p for p in ctx.spec.parts if p.name in names], self.spec.pose, fs)
        by_mesh: dict[str, PrunedDistance] = {}

        def distance(mesh: GMesh) -> PrunedDistance:
            if mesh.name not in by_mesh:
                by_mesh[mesh.name] = PrunedDistance.build(mesh)
            return by_mesh[mesh.name]

        rows = []
        by_name = {p.part.name: p for p in placed}
        for a_part, b_part in pairs_of(self.spec, [p.part for p in placed]):
            row, pf = check_pair(self.spec, by_name[a_part.name], by_name[b_part.name], ctx.spec.zones, distance)
            rows.append(row)
            fs += pf
        if a and b and not rows and len(placed) == len(names):
            fs.append(
                Finding("clash.no_pairs", INFO, self.spec.id, "the selectors leave no pair to check (same part or same group)", MEASURED)
            )
        return RuleResult(self.spec.id, self.spec.type, status_of(fs), data={"engine": "boolean+pruned", "pairs": rows}, findings=fs)


class SweepRule:
    def __init__(self, spec: SweepRuleSpec):
        self.spec = spec

    def run(self, ctx: Context) -> RuleResult:
        from .geom import require

        require()
        from .geom.sweep import SweepRunner, judge

        fs: list[Finding] = []
        moving = select(ctx, self.spec, self.spec.moving, fs)
        fixed = [p for p in select(ctx, self.spec, self.spec.fixed, fs) if p not in moving]
        pm = _placed(ctx, self.spec, moving, None, fs)
        pf = _placed(ctx, self.spec, fixed, None, fs)
        if any(f.code.startswith("cad.incomplete.") for f in fs) or not pm or not pf:
            return RuleResult(self.spec.id, self.spec.type, status_of(fs), findings=fs)
        runner = SweepRunner(self.spec, pm, pf, ctx.spec.zones, ctx.cull)
        run = runner.run()
        block, jf = judge(self.spec, run, runner.motion)
        fs += jf
        return RuleResult(self.spec.id, self.spec.type, status_of(fs), data=block, findings=fs)


def build(spec: RuleSpec) -> CadRule:
    if isinstance(spec, ClearanceRuleSpec):
        return ClearanceRule(spec)
    if isinstance(spec, SweepRuleSpec):
        return SweepRule(spec)
    if isinstance(spec, PartsRuleSpec):
        if spec.type == "check":
            return CheckRule(spec)
        if spec.type == "features":
            return FeatureRule(spec)
        return FitRule(spec)
    raise TypeError(f"no rule for {spec!r}")


def timed(rule: CadRule, ctx: Context) -> RuleResult:
    t0 = time.monotonic()
    r = rule.run(ctx)
    r.seconds = time.monotonic() - t0
    return r
