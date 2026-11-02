"""The `shambu cad` verbs behind `Bambu.cad`: each takes plain arguments, reads files through load.py and returns one
`CadReport`.

What it handles: stamp (the one verb that writes: the JSON record, never the STL), check (with `--promote`, which
writes only NEW files), features, fit, clearance, sweep, diff and gate, in the single-file form (flags) and the spec
form (`--spec cad.json --part NAME`). What it does not do: no argument parsing and no printing (cli.py), no CAD
program (the skill drives it).
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import __version__
from ..errors import CadError, CadSpecError
from ..findings import INFO, MEASURED, WARN, Finding
from . import diff as D
from . import features as FT
from . import fits as F
from . import gauge as GA
from . import provenance as P
from . import solid as SO
from .linalg import Vec
from .load import Scene, load_json, load_spec, load_subject
from .report import CadReport, RuleResult, Subject, area_findings, areas_json, status_of
from .rules import axis_tol, check_subject, expect_of
from .run import run_rules
from .spec import CadSpec, ClearanceRuleSpec, HoleSpec, PartSpec, SweepRuleSpec


def _input_row(s: Subject, part: str | None = None) -> dict:
    return {"part": part or s.name, "path": s.path, "sha256": s.sha256, "facets": len(s.facets), "object": s.object}


def _spec_part(spec_path: str | None, part: str | None) -> tuple[CadSpec | None, PartSpec | None]:
    if spec_path is None:
        return None, None
    spec = load_spec(spec_path)
    if part is None:
        raise CadSpecError("--spec needs --part NAME for this verb")
    return spec, spec.part(part)


def _replace_text(path: Path, text: str) -> None:
    """Write `text` to `path` through a temp file and os.replace (a re-stamp replaces the old record)."""
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp", encoding="utf-8") as fh:
            tmp = Path(fh.name)
            fh.write(text)
        os.replace(tmp, path)
        tmp = None
    except OSError as e:
        raise CadError(f"cannot write {path}: {e}") from e
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _refuse_overwrite(out: Path, protected: Sequence[str]) -> None:
    """Stamp writes only its record: refuse an output that is the STL, the STEP or a declared input, or any existing
    file that is not a shambu record (a re-stamp replaces its own earlier record). Raises CadError (exit 3)."""
    target = out.resolve()
    for p in protected:
        if Path(p).resolve() == target:
            raise CadError(f"{out} is {p}, an input of this stamp: the record goes to a file of its own (-o PART.cad.json)")
    if out.exists():
        try:
            P.load_record(out)
        except CadError as e:
            raise CadError(f"{out} exists and is not a shambu record: stamp replaces only its own earlier record") from e


@dataclass(frozen=True)
class StampArgs:
    stl: str
    part: str
    rev: str | None = None
    inputs: tuple[str, ...] = ()
    cad_volume_mm3: float | None = None
    cad_area_mm2: float | None = None
    cad_cog: Vec | None = None
    step: str | None = None
    step_volume_mm3: float | None = None
    faceting_tol_mm: float | None = None
    angle_tol_deg: float | None = None
    bed_face: Vec | None = None
    out: str | None = None


def stamp(a: StampArgs) -> CadReport:
    """Write the provenance record beside the STL (or at `out`). Raises CadError when a file does not read or the
    record cannot be written (exit 3)."""
    t0 = time.monotonic()
    subject = load_subject(a.stl, a.part)
    out = Path(a.out) if a.out else Path(a.stl).with_name(Path(a.stl).stem + ".cad.json")
    _refuse_overwrite(out, [a.stl, *([a.step] if a.step else []), *a.inputs])
    cad = None
    if a.cad_volume_mm3 is not None or a.cad_area_mm2 is not None or a.cad_cog is not None:
        cad = {
            "volume_mm3": a.cad_volume_mm3,
            "area_mm2": a.cad_area_mm2,
            "cog": list(a.cad_cog) if a.cad_cog else None,
            "from": "calculateMassProperties",
        }
    missing = [i for i in a.inputs if not Path(i).is_file()]
    if missing:
        raise CadError(f"declared inputs not found: {missing} (the build id hashes the saved script and parameter files)")
    rec = P.stamp_record(
        Path(a.stl),
        out,
        a.part,
        a.rev,
        a.inputs,
        SO.facts(subject.facets),
        cad,
        Path(a.step) if a.step else None,
        a.step_volume_mm3,
        a.faceting_tol_mm,
        a.angle_tol_deg,
        list(a.bed_face) if a.bed_face else None,
        __version__,
        subject.sha256,
    )
    _replace_text(out, json.dumps(rec, indent=1, allow_nan=False) + "\n")
    r = RuleResult(
        "stamp",
        "stamp",
        "no-finding",
        data={"record": str(out), "build_id": rec["build_id"]},
        text=[f"wrote {out}: build id {rec['build_id']}"],
    )
    rep = CadReport("stamp", [r], None, rec["build_id"], [_input_row(subject, a.part)], extra={"record": rec}, not_checked=[])
    rep.elapsed_s = time.monotonic() - t0
    return rep


@dataclass(frozen=True)
class CheckArgs:
    stl: str
    record: str | None = None
    expect_volume_mm3: float | None = None
    volume_tol_mm3: float | None = None
    expect_size_mm: Vec | None = None
    size_tol_mm: float | None = None
    density_g_cm3: float | None = None
    promote: str | None = None
    spec: str | None = None
    part: str | None = None
    object: str | None = None


def check(a: CheckArgs) -> CadReport:
    t0 = time.monotonic()
    spec, part = _spec_part(a.spec, a.part)
    subject = load_subject(a.stl, part.name if part else None, a.object)
    expect = expect_of(spec, part) if spec is not None and part is not None else SO.Expect()
    if a.expect_volume_mm3 is not None:
        if a.volume_tol_mm3 is None:
            raise CadSpecError("--expect-volume needs --volume-tol (no hidden default)")
        expect = SO.Expect(a.expect_volume_mm3, a.volume_tol_mm3, expect.size_mm, expect.size_tol_mm, expect.density_g_cm3, "flag")
    if a.expect_size_mm is not None:
        if a.size_tol_mm is None:
            raise CadSpecError("--expect-size needs --size-tol (no hidden default)")
        expect = SO.Expect(expect.volume_mm3, expect.volume_tol_mm3, a.expect_size_mm, a.size_tol_mm, expect.density_g_cm3, "flag")
    if a.density_g_cm3 is not None:
        expect = SO.Expect(expect.volume_mm3, expect.volume_tol_mm3, expect.size_mm, expect.size_tol_mm, a.density_g_cm3, expect.source)
    rec_path = a.record
    if rec_path is None and spec is not None and part is not None and part.record:
        rec_path = str(Path(spec.base) / part.record)
    record = P.load_record(rec_path) if rec_path else None
    areas = check_subject(subject, record, expect)
    fs = area_findings(areas)
    rule = RuleResult("check", "check", status_of(fs), data={"parts": {subject.name: {"areas": areas_json(areas)}}}, findings=fs)
    rep = CadReport("check", [rule], a.spec, record.data.get("build_id") if record else None, [_input_row(subject)])
    if a.promote:
        if rep.gate == "pass":
            done = P.promote(Path(a.stl), subject.raw, subject.sha256, Path(a.promote), record)
            rep.extra["promote"] = {"done": True, **done}
            rep.text.append(f"promoted to {done['stl']}" + (f" with record {done['record']}" if done["record"] else ""))
        else:
            rep.extra["promote"] = {"done": False, "reason": f"gate {rep.gate}: {a.promote} not written"}
            rep.text.append(f"not promoted: gate {rep.gate}; {a.promote} was not written")
    rep.elapsed_s = time.monotonic() - t0
    return rep


def features(
    stl: str,
    axis_tol_deg: float | None = None,
    min_diameter_mm: float = 0.0,
    spec_path: str | None = None,
    part_name: str | None = None,
    record_path: str | None = None,
    obj: str | None = None,
) -> CadReport:
    t0 = time.monotonic()
    spec, part = _spec_part(spec_path, part_name)
    subject = load_subject(stl, part.name if part else None, obj)
    rp = record_path or (str(Path(spec.base) / part.record) if spec is not None and part is not None and part.record else None)
    record = P.load_record(rp) if rp else None
    tol = axis_tol_deg if axis_tol_deg is not None else axis_tol(record)
    bed = None
    if spec is not None and part is not None:
        bed = spec.bed_face(part)
    if bed is None and record is not None:
        v = record.section("print").get("bed_face")
        bed = (float(v[0]), float(v[1]), float(v[2])) if isinstance(v, list) and len(v) == 3 else None
    inv = FT.detect(subject.facets, tol, min_diameter_mm, bed)
    fs = FT.compare(subject.name, inv, part.expected_features if part else ())
    text = [
        f"{f.kind:<5} d {f.diameter_mm:8.3f}  depth {f.depth_mm:7.3f}  {f.ends or '':<7}  at ({', '.join(f'{x:.2f}' for x in f.centre)})  axis ({', '.join(f'{x:.3f}' for x in f.axis)})  {f.print_axis}"
        for f in inv.features
    ]
    text += [
        f"pattern {p.id}: {len(p.members)} x {p.kind} d {p.diameter_mm:.3f}"
        + (
            f", pcd {p.pcd_mm:.3f}, angles {[round(a, 2) for a in p.angles_deg]}"
            if p.pcd_mm is not None
            else f", spacing {[round(s, 3) for s in p.spacing_mm]}"
        )
        for p in inv.patterns
    ]
    rule = RuleResult("features", "features", status_of(fs), data={"parts": {subject.name: inv.to_json()}}, findings=fs, text=text)
    rep = CadReport("features", [rule], spec_path, None, [_input_row(subject)])
    rep.elapsed_s = time.monotonic() - t0
    return rep


def parse_hole(text: str) -> HoleSpec:
    """`NAME:FIT:X,Y,Z:AX,AY,AZ` (the mouth point and the axis into the material). Raises ValueError."""
    bits = text.split(":")
    if len(bits) != 4:
        raise ValueError(f"--hole {text!r}: NAME:FIT:X,Y,Z:AX,AY,AZ")
    name, fit, mouth, axis = bits
    m = tuple(float(x) for x in mouth.split(","))
    ax = tuple(float(x) for x in axis.split(","))
    if len(m) != 3 or len(ax) != 3 or not name or not fit:
        raise ValueError(f"--hole {text!r}: NAME:FIT:X,Y,Z:AX,AY,AZ")
    if not all(math.isfinite(x) for x in (*m, *ax)):
        raise ValueError(f"--hole {text!r}: the point and the axis must be finite numbers")
    n = sum(x * x for x in ax) ** 0.5
    if n == 0:
        raise ValueError(f"--hole {text!r}: a zero axis")
    return HoleSpec(name, fit, (m[0], m[1], m[2]), (ax[0] / n, ax[1] / n, ax[2] / n))


def fit(
    stl: str,
    holes: Sequence[HoleSpec],
    fits_path: str | None = None,
    spec_path: str | None = None,
    part_name: str | None = None,
    obj: str | None = None,
) -> CadReport:
    t0 = time.monotonic()
    spec, part = _spec_part(spec_path, part_name)
    subject = load_subject(stl, part.name if part else None, obj)
    table = dict(spec.fits) if spec is not None else dict(F.merge(())[0])
    notes = list(spec.fit_notes) if spec is not None else []
    if fits_path:
        doc = load_json(fits_path, "fits file")
        rows = doc.get("fits") if isinstance(doc, dict) else doc
        if not isinstance(rows, list):
            raise CadSpecError(f'{fits_path}: a list of fits, or {{"fits": [...]}}')
        user = [F.Fit.from_json(r, f"{fits_path}[{i}]") for i, r in enumerate(rows)]
        notes += F.merge(user)[1]
        table.update({u.name: u for u in user})
    all_holes = list(part.holes if part else ()) + list(holes)
    if not all_holes:
        raise CadSpecError("fit needs --hole NAME:FIT:X,Y,Z:AX,AY,AZ (or a spec part with holes)")
    unknown = sorted({h.fit for h in all_holes if h.fit not in table})
    if unknown:
        raise CadSpecError(
            f"unknown fits {unknown}; built in: {sorted(f.name for f in F.FITS)[:6]}... (shambu cad fit --help), or pass --fits FILE"
        )
    fs: list[Finding] = [Finding("fit.shadowed", WARN, "fit", n, MEASURED) for n in notes]
    gauge = GA.Gauge(subject.facets)
    bed = spec.bed_face(part) if spec is not None and part is not None else None
    allowance = spec.hole_allowance() if spec is not None else {}
    rows_out, text = [], []
    for h in all_holes:
        f = table[h.fit]
        bore = gauge.bore(h.mouth, h.axis, f.hole_mm)
        fs += GA.judge(subject.name, h.name, f, bore, h.axis, h.mouth, h.offset_max_mm, bed, allowance)
        rows_out.append({"name": h.name, "fit": f.to_json(), "bore": bore.to_json() if bore else None})
        text.append(
            f"{h.name} ({f.name}, {f.hole_mm:g} mm): "
            + (
                f"d {bore.diameter_mm:.3f}, depth {bore.depth_mm:.2f}, {bore.ends}, wall {bore.wall_mm if bore.wall_mm is None else round(bore.wall_mm, 2)}, offset {bore.offset_mm:.3f}"
                if bore
                else "not found"
            )
        )
    rule = RuleResult("fit", "fit", status_of(fs), data={"parts": {subject.name: {"holes": rows_out}}}, findings=fs, text=text)
    rep = CadReport("fit", [rule], spec_path, None, [_input_row(subject)])
    rep.elapsed_s = time.monotonic() - t0
    return rep


def _rules_of(spec: CadSpec, kind: type, ids: Sequence[str]) -> list:
    chosen = [r for r in spec.rules if isinstance(r, kind) and (not ids or r.id in ids)]
    missing = sorted(set(ids) - {r.id for r in chosen})
    if missing:
        raise CadSpecError(f"{spec.path}: no {kind.__name__.replace('RuleSpec', '').lower()} rule {missing}")
    if not chosen:
        raise CadSpecError(f"{spec.path}: the spec has no {kind.__name__.replace('RuleSpec', '').lower()} rule")
    return chosen


def clearance(spec_path: str, rule_ids: Sequence[str] = (), pose: str | None = None) -> CadReport:
    from .geom import require

    require()
    spec = load_spec(spec_path)
    chosen = _rules_of(spec, ClearanceRuleSpec, rule_ids)
    if pose is not None:
        if pose not in spec.poses:
            raise CadSpecError(f"{spec_path}: no pose {pose!r}; poses: {sorted(spec.poses)}")
        chosen = [ClearanceRuleSpec(r.id, r.type, r.a, r.b, r.margin_mm, r.exempt, r.skip_same_group, pose) for r in chosen]
    scene = Scene(spec)
    return run_rules(spec, scene, chosen, "clearance", build_id=scene.build_id())


def sweep(spec_path: str, rule_ids: Sequence[str] = (), cull: bool = True) -> CadReport:
    from .geom import require

    require()
    spec = load_spec(spec_path)
    chosen = _rules_of(spec, SweepRuleSpec, rule_ids)
    scene = Scene(spec)
    return run_rules(spec, scene, chosen, "sweep", cull=cull, build_id=scene.build_id())


def gate(spec_path: str, only: Sequence[str] = (), skip: Sequence[str] = (), cull: bool = True) -> CadReport:
    spec = load_spec(spec_path)
    ids = {r.id for r in spec.rules}
    unknown = sorted((set(only) | set(skip)) - ids)
    if unknown:
        raise CadSpecError(f"{spec_path}: no rule {unknown}; rules: {sorted(ids)}")
    if any(isinstance(r, (ClearanceRuleSpec, SweepRuleSpec)) and (not only or r.id in only) and r.id not in skip for r in spec.rules):
        from .geom import require

        require()
    scene = Scene(spec)
    return run_rules(spec, scene, spec.rules, "gate", only, skip, cull, scene.build_id())


@dataclass(frozen=True)
class DiffArgs:
    old: str
    new: str
    object: str | None = None
    spec: str | None = None
    part: str | None = None
    plate: str | None = None
    expect: str | None = None
    sections: int = 24
    surface: bool = False


def _side(path: str, obj: str | None) -> tuple[Subject, dict | None, list[Finding]]:
    """A diff input: an STL, a 3MF object, or a record (its STL, refused when the STL is not the one stamped). A record
    is history: the revision workflow edits the script and parameters in place and re-exports the STEP, so the build id
    and the STEP hash of an old record no longer recompute. Only "this artifact matches its record" is judged here;
    "the current build is fresh" is `shambu cad check`'s question."""
    if path.endswith(".cad.json"):
        rec = P.load_record(path)
        stl = rec.resolve(str(rec.section("stl").get("path")))
        s = load_subject(stl, str(rec.data.get("part")))
        _, fs = P.freshness(rec, stl, s.sha256)
        return s, rec.data, [f for f in fs if f.code == "cad.stale.stl_sha"]
    return load_subject(path, None, obj), None, []


def diff(a: DiffArgs) -> CadReport:
    t0 = time.monotonic()
    if a.sections < 2:
        raise CadSpecError("--sections must be at least 2")
    spec = load_spec(a.spec) if a.spec else None
    part: PartSpec | None = None
    old, rec_old, fs_old = _side(a.old, a.object)
    new, rec_new, fs_new = _side(a.new, a.object)
    if spec is not None:
        if a.part:
            part = spec.part(a.part)
        else:
            for p in spec.parts:
                mp = (Path(spec.base) / p.mesh).resolve()
                if mp in (Path(old.path).resolve(), Path(new.path).resolve()):
                    part = p
    fs: list[Finding] = fs_old + fs_new
    m_old, m_new = D.measures(old.facets), D.measures(new.facets)
    deltas = D.measure_deltas(m_old, m_new)
    tol, tol_from = D.faceting([rec_old, rec_new])
    size_moved = any(abs(deltas[ax]["delta"]) > tol for ax in D.SIZE_AXES)
    if size_moved:
        fs.append(
            Finding(
                "diff.size",
                INFO,
                "diff",
                "size " + ", ".join(f"{ax} {deltas[ax]['delta']:+.3f}" for ax in D.SIZE_AXES) + " mm",
                MEASURED,
                measure={ax: deltas[ax]["delta"] for ax in D.SIZE_AXES},
            )
        )
    v0 = m_old["volume_mm3"]
    dv = m_new["volume_mm3"] - v0
    vol_tol = max(m_old["area_mm2"], m_new["area_mm2"]) * tol
    if abs(dv) > vol_tol:
        fs.append(
            Finding(
                "diff.volume",
                INFO,
                "diff",
                f"volume {dv:+.1f} mm3 ({100 * dv / v0 if v0 else 0:+.2f} %)",
                MEASURED,
                measure={"delta_mm3": dv, "tolerance_mm3": vol_tol},
            )
        )
    secs, sfs = D.sections(old.facets, new.facets, a.sections, tol)
    fs += sfs
    inv_old = FT.detect(old.facets)
    inv_new = FT.detect(new.facets)
    changes = D.match_features(
        inv_old.features, inv_new.features, D.hole_names(part, inv_old.features), D.hole_names(part, inv_new.features)
    )
    fs += D.feature_findings(changes, spec, part)
    extra: dict = {
        "measures": deltas,
        "faceting_tol_mm": tol,
        "faceting_from": tol_from,
        "sections": secs,
        "features": {k: [c.to_json() for c in changes if c.kind == k] for k in ("same", "resized", "moved", "gone", "new")},
    }
    if a.surface:
        extra["surface"] = _surface(old, new, tol)
    if a.plate:
        doc = load_json(a.plate, "plate spec")
        if not isinstance(doc, dict):
            raise CadSpecError(f"{a.plate}: not a plate spec")
        bed_changed = (rec_old or {}).get("print", {}) != (rec_new or {}).get("print", {}) if rec_old and rec_new else False
        block, ifs = D.impact(doc, a.plate, old.path, new.path, size_moved, bool(bed_changed))
        extra["impact"] = block
        fs += ifs
    if a.expect:
        exp = load_json(a.expect, "expectation file")
        if not isinstance(exp, dict):
            raise CadSpecError(f"{a.expect}: an object with 'changes'")
        fit_names: set[str] = {h.name for h in part.holes} if part is not None else set()
        shape = [f for f in fs if f.code in ("diff.volume", "diff.section")]
        fs += D.expect_findings(exp, deltas, changes, tol, fit_names, vol_tol, shape)
    text = [f"{ax}: {deltas[ax]['old']:.3f} -> {deltas[ax]['new']:.3f} ({deltas[ax]['delta']:+.3f})" for ax in ("volume_mm3", *D.SIZE_AXES)]
    text += [f"feature {c.kind}: {c.id}" for c in changes if c.kind != "same"]
    if "impact" in extra:
        text.append(f"reslice plates: {extra['impact']['plates'] or 'none'}; replace-mesh: {extra['impact']['replace_mesh']}")
    rule = RuleResult("diff", "diff", status_of(fs), findings=fs, text=text)
    rep = CadReport(
        "diff",
        [rule],
        a.spec,
        None,
        [_input_row(old, "old"), _input_row(new, "new")],
        extra=extra,
        not_checked=[dict(x) for x in D.NOT_CHECKED],
    )
    rep.elapsed_s = time.monotonic() - t0
    return rep


def _surface(old: Subject, new: Subject, tol: float) -> dict:
    """Symmetric surface deviation by samples and PrunedDistance (the geom extra), floored at the faceting tolerance."""
    from .geom import require

    require()
    import numpy as np

    from .geom.engines import GMesh, PrunedDistance

    a = PrunedDistance.build(GMesh.from_facets("old", old.facets), max(tol, 0.05))
    b = PrunedDistance.build(GMesh.from_facets("new", new.facets), max(tol, 0.05))
    d_ab = b.distances(a.samples).d
    d_ba = a.distances(b.samples).d
    both = np.concatenate([d_ab, d_ba])
    return {
        "hausdorff_mm": float(both.max()),
        "mean_mm": float(both.mean()),
        "floor_mm": tol,
        "note": "deviations under floor_mm are below what the meshes resolve",
    }
