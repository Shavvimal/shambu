"""Run the rules of a CAD gate spec in their file order and build one `CadReport`.

What it handles: `--only` and `--skip` (a rule left out is `not-checked` with its reason), the spec notes (unknown
keys as `cad.spec.unknown_key` WARN, parts with no role as `cad.default_role` INFO) in a first `spec` rule, the
inputs list and the gate's build id. What it does not do: no file reading (the scene reads), no printing.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from ..findings import INFO, MEASURED, WARN, Finding
from ..printability import NOT_CHECKED_STATUS
from . import rules as R
from .report import CadReport, RuleResult, status_of
from .spec import CadSpec, RuleSpec


def spec_rule(spec: CadSpec) -> RuleResult:
    fs = [Finding("cad.spec.unknown_key", WARN, "spec", f"unknown key {k}: not read (a typo?)", MEASURED) for k in spec.unknown_keys]
    fs += [
        Finding("cad.default_role", INFO, "spec", f"part {p.name} has no role: treated as rigid", MEASURED, p.name)
        for p in spec.parts
        if p.role_from == "default"
    ]
    return RuleResult(
        "spec", "spec", status_of(fs), data={"unknown_keys": list(spec.unknown_keys), "unread": list(spec.unread)}, findings=fs
    )


def run_rules(
    spec: CadSpec,
    scene: R.SceneLike,
    rules: Sequence[RuleSpec],
    verb: str,
    only: Sequence[str] = (),
    skip: Sequence[str] = (),
    cull: bool = True,
    build_id: str | None = None,
    factory: Callable[[RuleSpec], R.CadRule] = R.build,
) -> CadReport:
    """Every rule in order (`factory` makes the rule object; tests pass a fake), skipped ones `not-checked`."""
    t0 = time.monotonic()
    ctx = R.Context(spec, scene, cull)
    results = [spec_rule(spec)]
    for rs in rules:
        if only and rs.id not in only:
            results.append(RuleResult(rs.id, rs.type, NOT_CHECKED_STATUS, "not requested (--only)"))
            continue
        if rs.id in skip:
            results.append(RuleResult(rs.id, rs.type, NOT_CHECKED_STATUS, f"skipped: --skip {rs.id}"))
            continue
        results.append(R.timed(factory(rs), ctx))
    loaded = scene.loaded()
    fresh = ctx.notes.get("fresh", {})
    inputs = []
    for p in spec.parts:
        row: dict = {"part": p.name, "path": p.mesh, "role": p.role, "role_from": p.role_from}
        s = loaded.get(p.name)
        if s is not None:
            row.update({"sha256": s.sha256, "facets": len(s.facets)})
        if p.name in fresh:
            row["fresh"] = fresh[p.name]
        inputs.append(row)
    report = CadReport(
        verb,
        results,
        spec.path,
        build_id,
        inputs,
        {"unknown_keys": list(spec.unknown_keys), "unread": list(spec.unread)},
    )
    report.elapsed_s = time.monotonic() - t0
    return report
