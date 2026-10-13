"""Render the hand-back report for a build: plain statements with numbers, Markdown."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import layout as L
from .errors import BuildError

if TYPE_CHECKING:
    from .build import BuildResult

STANDING_WARNINGS = (
    "This project is unsliced: slice in Bambu Studio (or build it with --slice for a .gcode.3mf with estimates).",
    "First print of a new fit: one coupon or one part, in the same orientation as the feature.",
    "Holes print undersize: about 0.1 mm vertical, 0.2 to 0.3 mm horizontal (bridged), more on long bridged bores.",
)


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
    for i, f in enumerate(pl.filaments, 1):
        col = spec.filaments[i - 1].colour or "colour unset"
        ext = pl.filament_map[i - 1]
        ftype = (f.get("filament_type") or ["?"])[0]
        out.append(
            f"Filament {i}: `{f['name']}` ({ftype}), {col}, extruder {ext} ({pl.facts['extruders'][ext - 1]['type']})."
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
                o = x.spec.orientation
                extras.append(
                    "oriented: "
                    + (f"face {list(o.lay_on_face)} on the bed" if o.lay_on_face else "")
                    + (f" rotated {list(o.rotate)} deg" if any(o.rotate) else "")
                    + f" (copy {x.files[0][0].name})"
                )
            if x.spec.recipes:
                extras.append("recipes " + ", ".join(x.spec.recipes))
            if x.spec.set:
                extras.append("settings " + ", ".join(f"{k}={v}" for k, v in x.spec.set.items()))
            if x.spec.height_ranges:
                extras.append("height ranges " + "; ".join(f"z {h.min_z}..{h.max_z}: {h.set}" for h in x.spec.height_ranges))
            if x.assembled:
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
        out += [("- OK: " if c.ok else "- FAIL: ") + c.text for c in p.checks]
    if pl.problems:
        out += ["", "## Problems"] + ["- " + x for x in pl.problems]
    if pl.warnings:
        out += ["", "## Warnings"] + ["- " + x for x in pl.warnings]
    if r.run:
        out += ["", "## Export", "- " + r.run.summary()]
        out += ["- patched: " + x for x in r.patches]
        if r.verify_problems == []:
            out.append(
                "- verified: bed type (project and plates), process name and every override, filament presets, colours and map, "
                "plate and object counts, object names, machine start/end/filament-change G-code" + (", G-code present" if r.sliced else "")
            )
        for plate_s in (r.summary or {}).get("plates", []):
            if plate_s.get("slice"):
                sl = plate_s["slice"]
                out.append(
                    f"- plate {plate_s['id']} estimate: {sl['prediction_s'] // 3600} h {sl['prediction_s'] % 3600 // 60} min, {sl['weight_g']:.1f} g ("
                    + ", ".join(f"filament {f['id']} {f['type']} {f['used_g']:.1f} g" for f in sl["filaments"])
                    + ")"
                    + (f"; slicer warnings: {sl['warnings']}" if sl["warnings"] else "")
                )
                ft = sl.get("features")
                if ft:
                    out.append(
                        f"- plate {plate_s['id']} filament by feature: model {ft['model_g']:.1f} g, support {ft['support_g']:.1f} g, "
                        f"prime tower {ft['tower_g']:.1f} g, flush {ft['flush_g']:.1f} g; {ft['filament_changes']} filament change(s)"
                    )
        out += ["- VERIFY FAIL: " + x for x in (r.verify_problems or [])]
    out += ["", "## Owner steps in Bambu Studio"]
    step = 1
    out.append(
        f'{step}. Open the project (`open -a BambuStudio "{r.project}"`), confirm the plate type ({spec.bed_type}) and the filament colours.'
    )
    if pl.map_applies:
        step += 1
        out.append(
            f"{step}. Check the filament-to-nozzle map on the plate (Manual: "
            + ", ".join(f"filament {i + 1} -> nozzle {e}" for i, e in enumerate(pl.filament_map))
            + ")."
        )
    step += 1
    if r.sliced:
        out.append(
            f"{step}. The plate is already sliced (the estimate above comes from the slicer). Check the preview, then Print. Re-slice in Studio after any change."
        )
    else:
        out.append(f"{step}. Slice Plate, read the time and filament estimate, then Print.")
    warnings = [w for w in STANDING_WARNINGS if not (r.sliced and w.startswith("This project is unsliced"))]
    out += ["", "## Standing warnings"] + ["- " + w for w in warnings]
    return "\n".join(out) + "\n"
