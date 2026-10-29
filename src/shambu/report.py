"""Render the hand-back report for a build: plain statements with numbers, Markdown."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from . import conf
from . import layout as L
from .errors import BambuError, BuildError

if TYPE_CHECKING:
    from .build import BuildResult, PlannedPart

STANDING_WARNINGS = (
    "This project is unsliced: slice in Bambu Studio (or build it with --slice for a .gcode.3mf with estimates).",
    "First print of a new fit: one coupon or one part, in the same orientation as the feature.",
    "Holes print undersize: about 0.1 mm vertical, 0.2 to 0.3 mm horizontal (bridged), more on long bridged bores.",
)


FEATURE_TIME_ROLES = ("Support", "Support interface", "Prime tower", "Flush", "Travel")


def checks_skipped_lines(cs: dict | None) -> list[str]:
    """The `--no-check` record (build.two_pass_slice) as report lines under the estimates: one `Checks skipped:` line
    per plate whose checked pass found a skippable problem, or the one line that says why no unchecked pass ran."""
    if not cs:
        return []
    if not cs.get("skipped"):
        return ["- " + cs["report_line"]]
    recs = [r for r in cs.get("plates", []) if r.get("kind") == "skippable"]
    per_plate = any(r["plate"] for r in cs.get("plates", []))
    out = []
    for r in recs:
        where = f"plate {r['plate']}" if r["plate"] else ("all plates together" if per_plate else "plate 1")
        out.append(
            f"- {where}: Checks skipped: {r['reason']}. Exported with --no-check as the GUI would: the conflict and "
            "support-necessity checks did not run on this G-code, so look at that spot in the preview before printing."
        )
    return out


def estimate_lines(sl: dict, calibration: list[dict] | None = None, printer_model: str | None = None) -> list[str]:
    """Report lines for one entry of BuildResult.estimates(). Each number names its source: slice_info.config,
    result.json (seconds per feature, tower plus flush grams) or the G-code parser (support grams, filament changes
    as tool switches, and everything by feature when the run left no result.json). The time is always "Studio
    estimate"; `calibration` (the stored measured prints) adds their range, or a calibrated time (startup offset + rate x Studio) when n >= 3 eligible prints agree."""
    from .calibrate import handback, material_key

    n = sl["plate"]
    cal = handback(calibration or [], printer_model, material_key(f["type"] for f in sl["filaments"]), sl["prediction_s"])
    out = [
        f"- plate {n}: Studio estimate {sl['prediction_s'] // 3600} h {sl['prediction_s'] % 3600 // 60} min{cal}, {sl['weight_g']:.1f} g ("
        + ", ".join(f"filament {f['id']} {f['type']} {f['used_g']:.1f} g" for f in sl["filaments"])
        + ")"
        + (f"; slicer warnings: {sl['warnings']}" if sl["warnings"] else "")
    ]
    ft = sl.get("features")
    native_waste = [f["waste_g"] for f in sl["filaments"] if "waste_g" in f]
    if "feature_times_s" in sl or native_waste:
        if ft:
            support = f"support {ft['support_g']:.1f} g (G-code parser)"
        elif sl.get("support_used") == "false":
            support = "support 0.0 g (the slicer used none)"
        else:
            support = "support not measured"
        if native_waste:
            waste = f"prime tower + flush {sum(float(w) for w in native_waste):.1f} g (result.json)"
        elif ft:
            waste = f"prime tower + flush {ft['waste_g']:.1f} g (G-code parser)"
        else:
            waste = "prime tower + flush not available"
        changes = sl.get("filament_changes")
        swaps = (
            "filament changes not measured"
            if changes is None
            else f"{changes} filament change(s) ({'G-code parser' if ft else 'one filament'})"
        )
        out.append(f"- plate {n} filament by feature: {support}, {waste}; {swaps}")
        times = sl.get("feature_times_s") or {}
        shown = [f"{k.lower()} {times[k] / 60:.1f} min" for k in FEATURE_TIME_ROLES if times.get(k)]
        if shown:
            out.append(f"- plate {n} time by feature (result.json): " + ", ".join(shown))
        tower_s = times.get("Prime tower")
        if tower_s and len(sl["filaments"]) >= 2:
            out.append(tower_options_line(n, tower_s, changes))
    elif ft:
        out.append(
            f"- plate {n} filament by feature (G-code parser, no result.json grams): model {ft['model_g']:.1f} g, support {ft['support_g']:.1f} g, "
            f"prime tower {ft['tower_g']:.1f} g, flush {ft['flush_g']:.1f} g; {ft['filament_changes']} filament change(s)"
        )
    return out


# Measured on plate 14 (PETG part, PLA interface, n=1 each; reference 06 section 10 and the 2026-10-21 replay):
# the auto 35 mm tower 24 h 13 min, a 22 mm tower without the rib wall 19 h 15, no tower 18 h 48
TOWER_OPTIONS = (
    ('`wipe_tower` {"width": 22, "rib_wall": false}', "19 h 15 min against 24 h 13 with the 35 mm tower"),
    ('`"wipe_tower": false`', "18 h 48 min; the idle nozzle is then not wiped before the interface layers"),
)


def tower_options_line(n: int, tower_s: float, changes: int | None) -> str:
    """The prime tower's cost on a two-filament plate and the two measured alternatives, for the estimate section."""
    swaps = f", {changes} filament change(s)" if changes is not None else ""
    opts = "; ".join(f"{k}: {v}" for k, v in TOWER_OPTIONS)
    return f"- plate {n} prime tower: {tower_s / 60:.0f} min{swaps} (result.json). Options measured on plate 14 (n=1): {opts}"


def check_line(c: L.Check) -> str:
    """One check as a report line, its kind first so a reader (and the skill) can sort them: `PLACEMENT NOT DONE:`
    (the packer gave up, no position exists), `FAIL body overlap:` (the parts themselves meet), `FAIL margin rule:`
    (a measured or spec margin, the bodies are apart), `WARN margin rule:` (an estimate; the slice decides),
    `WARN brim:` (a brim Studio will clip at the bed edge or a nozzle's reach), `WARN outline:` (the box stands in for
    a mesh that did not rasterise),
    `FAIL:` and `OK:` for the rest."""
    if c.kind == "measured":
        return c.text  # "Spreads measured: ...": the plan re-ran with the support probe
    if c.kind == "placement" and not c.ok:
        return "PLACEMENT NOT DONE: " + c.text
    if c.kind in ("margin", L.REACH_ESTIMATE):
        return ("WARN margin rule: " if c.ok else "FAIL margin rule: ") + c.text
    if c.kind == "brim" and c.ok:
        return "WARN brim: " + c.text
    if c.kind == "outline" and c.ok:
        return "WARN outline: " + c.text
    if c.kind == "body" and not c.ok:
        return "FAIL body overlap: " + c.text
    return ("OK: " if c.ok else "FAIL: ") + c.text


def _vec(v) -> str:
    return "[" + ", ".join(f"{x:g}" for x in v) + "]"


def orientation_text(x: PlannedPart) -> str:
    """How the part was turned: Studio's auto-orient (pass 1), shambu's own pick for a merged auto part, or the spec's
    explicit lay_on_face / rotate."""
    o = x.spec.orientation
    copy = f"copy {x.files[0][0].name}"
    if x.orient is not None:
        b = x.orient.best
        if b is None:
            return f"oriented by Studio (no candidate table in its log; {copy})"
        return f"oriented by Studio: best {_vec(b.vector)}, unprintability {b.unprintability:g} (lay_on_face {_vec(b.lay_on_face)}; {copy})"
    if x.auto_face is not None:
        return f"oriented by shambu's own score (orientation auto on a merged part): face {_vec(x.auto_face)} on the bed ({copy})"
    return (
        "oriented: "
        + (f"face {list(o.lay_on_face)} on the bed" if o.lay_on_face else "")
        + (f" rotated {list(o.rotate)} deg" if any(o.rotate) else "")
        + f" ({copy})"
    )


def _process_slot(r: BuildResult) -> tuple[str, list[str]]:
    """The process combo's base preset and the keys Studio keeps on open: `plan.bookkeeping[0]` when the plan filled
    it, else the spec's base and its `set` keys (a plan from before the bookkeeping, or a test plan)."""
    pl = r.plan
    if pl.bookkeeping:
        _, base, keys = pl.bookkeeping[0]
        return base, sorted(keys)
    return pl.spec.process.base, sorted(pl.spec.process.set)


def _process_claim(r: BuildResult) -> str:
    """The process part of the `verified:` line: the base preset and the override list the file carries."""
    base, keys = _process_slot(r)
    return f"process `{base}` + {len(keys)} override(s)" + (f" ({', '.join(keys)})" if keys else "")


def _hm(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600} h {s % 3600 // 60} min"


def owner_steps(r: BuildResult) -> list[str]:
    """The numbered steps for the user's final pass in the GUI, written for what Studio 02.08 does on open
    (spec 2026-10-29 section 5.5): the process combo names `<base>` with the listed keys kept (Preset.cpp:2258-2269
    and :2330-2345 at v02.08.02.61). It shows the base as modified only when a listed value differs from the base:
    Studio selects the preset clean when every value equals it (Preset.cpp:2292-2305), and the exporter lists the
    explicit keys even when they equal shambu's base, so the step checks the values, not the suffix. A project with
    objects opens unsliced because Studio loads the embedded G-code only when the model has no objects
    (Plater.cpp:9578-9581). The GUI slice is therefore the check, and it must land on this report's estimate."""
    pl, spec = r.plan, r.plan.spec
    base, keys = _process_slot(r)
    spec_path = r.inputs.get("spec")
    against = f'"{spec_path}"' if spec_path else "SPEC"
    steps = [f'Open the project with `shambu open "{r.project}"`.']
    if keys:
        vals = ", ".join(f"{k} = {pl.process.get(k, spec.process.set.get(k, '?'))}" for k in keys)
        steps.append(
            f"The process combo shows `{base}` (shown as modified when any override differs from the base). The values are what "
            "to check, not the suffix: open its tab and confirm each value (type the key into the tab's search "
            f"box: the tab shows labels, not keys): {vals}."
        )
    else:
        steps.append(f"The process combo shows `{base}` (no process overrides).")
    check = f"Confirm the plate type ({spec.bed_type}) and the filament colours"
    if len(pl.filaments) >= 2:
        where = ", ".join(
            f"filament {i} on the {'auxiliary (right)' if e == 2 else 'main (left)'} nozzle" for i, e in enumerate(pl.filament_map, 1)
        )
        check += (
            f", and that the Filament grouping shows {where}. Leave the Filament grouping dialog alone unless it shows the wrong nozzle"
        )
    steps.append(check + ".")
    proj = Path(str(r.project))
    saved = proj.with_name(f"{proj.name.split('.')[0]}-checked.3mf")
    # Studio holds what it changed on open in memory: verifying the export would check the unchanged file
    fallback = (
        f"save the project in Studio under a new name (File > Save project as, e.g. `{saved.name}`), then run "
        f'`shambu verify "{saved}" --against {against}`, which lists the settings that differ. Do not Print until it '
        "exits 0. If a FAIL is a change you want, put it in the spec and rebuild."
    )
    est = r.estimates() if r.sliced else []
    if est:
        figures = "; ".join(
            (f"plate {sl['plate']}: " if len(est) > 1 else "") + f"{_hm(sl['prediction_s'])}, {sl['weight_g']:.1f} g" for sl in est
        )
        steps.append(
            "Slice plate: Studio does not load shambu's embedded G-code for a project with objects (Plater.cpp:9578-9581 at "
            f"v02.08.02.61), so the plate opens unsliced. The GUI estimate must land within 1 min and 1 g of {figures} above. "
            "If it does not, a setting changed on open: " + fallback
        )
    else:
        steps.append(
            "Slice plate, then compare the GUI estimate with a `shambu slice` of the same file: they must agree within 1 min and 1 g. "
            "If they do not, a setting changed on open: " + fallback
        )
    steps.append(
        "In Preview, set the colour scheme to Line Type and look at the Overhang wall, Bridge and Support rows and at the first layer."
    )
    steps.append("Print only when the GUI estimate matched, or `shambu verify` on the saved copy exited 0.")
    return [f"{i}. {t}" for i, t in enumerate(steps, 1)]


def render(r: BuildResult) -> str:
    from .build import effective_filaments

    pl = r.plan
    spec = pl.spec
    out = [f"# {spec.name}", ""]
    state = (
        "written and verified"
        if r.ok
        else ("written, verify problems below" if r.run and r.run.ok else ("export FAILED" if r.run else "not exported"))
    )
    out.append(f"Project `{r.project}`: {state}.")
    out.append(
        f"Printer `{pl.machine['name']}`; process `{pl.process['name']}` (from `{pl.process.get('inherits', pl.process['name'])}`); bed {spec.bed_type}."
    )
    res = pl.filament_resolution
    if res is not None:
        out.append(res.headline + ".")
    for i, f in enumerate(pl.filaments, 1):
        col = spec.filaments[i - 1].colour or "colour unset"
        ext = pl.filament_map[i - 1]
        ftype = (f.get("filament_type") or ["?"])[0]
        src = next((s for s in (res.sources if res else []) if s.slot == i), None)
        out.append(
            f"Filament {i}: `{f['name']}` ({ftype}), {col}, extruder {ext} ({pl.facts['extruders'][ext - 1]['type']})."
            + (f" Source: {src.text}." if src else "")
            + (" Dry it before printing." if ftype.upper() not in ("PLA",) else "")
        )
    if spec.process.recipes:
        out.append("Process recipes: " + ", ".join(spec.process.recipes) + ".")
    if spec.process.set:
        out.append("Process settings: " + ", ".join(f"{k}={v}" for k, v in spec.process.set.items()) + ".")
    for p in pl.plates:
        out += ["", f"## Plate: {p.name}"]
        if p.print_sequence == "by object":
            out.append(
                "- print sequence: by object (objects print one after another in the order listed"
                + ("; Studio builds no prime tower on such a plate)" if len(p.parts) > 1 else ")")
            )
        out += ["- " + line for line in L.describe([x.placed for x in p.parts])]
        for x in p.parts:
            extras = []
            if not x.spec.orientation.is_identity:
                extras.append(orientation_text(x))
            if x.spec.recipes:
                extras.append("recipes " + ", ".join(x.spec.recipes))
            if x.spec.set:
                extras.append("settings " + ", ".join(f"{k}={v}" for k, v in x.spec.set.items()))
            if x.spec.height_ranges:
                extras.append("height ranges " + "; ".join(f"z {h.min_z}..{h.max_z}: {h.set}" for h in x.spec.height_ranges))
            if x.assembled and not x.grouped:
                extras.append("parts " + ", ".join(f"{fp.name} on filament {fil}" for fp, fil, _ in x.files))
            try:
                eff = effective_filaments(x.spec, x.files)
            except BuildError:
                eff = set()
            if eff != {x.files[0][1]}:
                extras.append("prints with filament(s) " + ", ".join(str(f) for f in sorted(eff)))
            if not x.rename:
                extras.append(f"keeps the CLI name '{x.cli_name}' in the project")
            if extras:
                out.append(f"  - {x.key}: " + "; ".join(extras))
            if x.grouped:
                out.append(
                    f"  - {x.key} is one object of {len(x.members)} parts on filament {x.placed.filament} (group_by_filament): "
                    "they print together, layer by layer, with no nozzle change between them"
                )
                for m, (dx, dy) in zip(x.members, x.offsets, strict=True):
                    sx, sy, sz = m.placed.info.size
                    o = m.spec.orientation
                    turned = "" if o.is_identity else f"; {orientation_text(m)}"
                    out.append(
                        f"    - {m.key}: filament {m.placed.filament}, {sx:.1f} x {sy:.1f} x {sz:.1f} mm, "
                        f"at x {dx:.1f} y {dy:.1f} in the group (bed x {m.placed.x0:.1f} y {m.placed.y0:.1f}){turned}"
                    )
        out += ["- " + check_line(c) for c in p.checks]
    if pl.problems:
        out += ["", "## Problems"] + ["- " + x for x in pl.problems]
    if pl.warnings:
        out += ["", "## Warnings"] + ["- " + x for x in pl.warnings]
    if r.run:
        out += ["", "## Export", "- " + r.run.summary()]
        ep = r.export_problem()
        if ep:
            out.append("- " + ep)
        out += ["- FAIL: " + x for x in r.export_fails()]
        for w in r.slicer_warnings():
            out.append(f"- slicer warning: {w}")
        out += ["- patched: " + x for x in r.patches]
        if r.verify_problems == []:
            out.append(
                f"- verified: bed type (project and plates), {_process_claim(r)}, filament presets, colours and map, "
                "plate and object counts, object names, machine start/end/filament-change G-code" + (", G-code present" if r.sliced else "")
            )
        try:
            cal = conf.load_calibration() if r.estimates() else []
        except BambuError:  # an unreadable calibration file must not hide the estimate; its path stays out of the report
            cal = []
            out.append("- calibration file not read: `shambu calibrate --show` names the problem")
        for sl in r.estimates():
            out += estimate_lines(sl, cal, pl.facts.get("printer_model"))
        out += checks_skipped_lines(r.checks_skipped)
        out += ["- VERIFY FAIL: " + x for x in (r.verify_problems or [])]
    if r.run is not None and not r.run.ok:
        out += ["", "## Next", "- The export failed (above): nothing to open. Fix the spec or the part and build again."]
        out += ["", "## Standing warnings"] + ["- " + w for w in STANDING_WARNINGS[1:]]
        return "\n".join(out) + "\n"
    if r.run is None and not pl.ok:  # the build stopped at the plan: a project at the output path is absent or stale
        out += [
            "",
            "## Next",
            "- The checks failed (see Problems and the FAIL lines above): nothing was exported, so there is no new project to open. "
            "Fix the spec and build again."
            + (
                f" `--keep-going` never exports a plate with parts that have no position ({', '.join(pl.unplaced)})." if pl.unplaced else ""
            ),
        ]
        out += ["", "## Standing warnings"] + ["- " + w for w in STANDING_WARNINGS[1:]]
        return "\n".join(out) + "\n"
    if r.run is None:  # a check-only run (`shambu check`): the plan passed, nothing was exported
        out += [
            "",
            "## Next",
            f"- Check only: the plan passed, but nothing was exported, so there is no project at `{r.project}` from this run. "
            "Run `shambu build` on the spec to write it.",
        ]
        out += ["", "## Standing warnings"] + ["- " + w for w in STANDING_WARNINGS[1:]]
        return "\n".join(out) + "\n"
    out += ["", "## Owner steps in Bambu Studio"] + owner_steps(r)
    warnings = [w for w in STANDING_WARNINGS if not (r.sliced and w.startswith("This project is unsliced"))]
    out += ["", "## Standing warnings"] + ["- " + w for w in warnings]
    return "\n".join(out) + "\n"
