"""From a plate spec to a checked Bambu Studio project (Template Method pipeline).

A spec is one JSON document the calling agent writes (see `spec.py`). The pipeline runs the fixed
sequence plan -> write inputs -> export -> patch -> verify -> report. Each step is a method, so a
subclass can replace one (a dry run skips export; a test injects a fake StudioCLI). Listeners
(Observer) get an event per step for progress output.

Design: the adapters (StudioCLI, PresetResolver, Placer, LimitChecker, SpreadProbe) are injected. `plan` measures and
places the parts per plate with layout.py (footprints, spreads, the limit rules) and plans the prime towers with
tower.py after every plate is packed (one tower size per project, the spot tested against the parts' silhouettes).
"Has support" is `layout.needs_support` everywhere (filament accounting, support pinning, the probe). It does not
place parts itself, model the tower, or decide what Studio's slice will accept: the export and verify steps report it.
"""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
import time
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import assemble as A
from . import layout as L
from . import mesh as M
from . import presets as P
from . import recipes as R
from . import tower as T
from .errors import BambuError, BuildError
from .loaded import FilamentResolution
from .patches import (
    AddCustomGcode,
    PatchSet,
    RenameObject,
    RepairNozzleStats,
    SetBedTypeEverywhere,
    SetFilamentColours,
    SetFilamentMap,
    SetPresetBookkeeping,
)
from .probe import ProbeRequest, SpreadProbe, StudioProbe
from .spec import FileRef, LayoutSpec, PartSpec, Spec
from .studio import (
    NO_PAIR,
    CliCommand,
    MeshInfo,
    OrientResult,
    RunResult,
    SlicedPlate,
    StudioCLI,
    arrange_failed_plate,
    conflict_phrase,
    conflicts_in_log,
)
from .threemf import Project

log = logging.getLogger(__name__)
Listener = Callable[[str, dict], None]
DEFAULT_COLOURS = ("#1A1A1A", "#F0F0F0", "#00AE42", "#0A2989")  # used when the spec gives none; slicing needs one per filament
WIPE_TOWER_XY = (208.0, 208.0)  # plates without a tower of their own: the CLI default (15, 220) runs off the bed (-104)
PRINTING_SUBTYPES = ("normal_part", "modifier_part")
TOWER_KEYS = (
    "prime_tower_width",
    "prime_tower_rib_wall",
    "prime_tower_brim_width",
    "enable_prime_tower",
)  # set by tower planning  # sub-parts whose filament reaches the nozzle
UNRESOLVED_FILAMENT = "loaded (unresolved)"  # placeholder preset name for a "loaded" slot the printer did not fill
# The GUI couples these keys to their source when the user edits the source (Tab.cpp:2106-2119, Studio v02.08.02.61),
# so a GUI save of an untouched shambu file writes the same diff list. The skin and skeleton densities change the fill
# only for the locked-zag pattern (Fill.cpp:307-329).
GUI_DERIVED_KEYS = {
    "sparse_infill_density": ("skeleton_infill_density", "skin_infill_density"),
    "sparse_infill_line_width": ("skin_infill_line_width", "skeleton_infill_line_width"),
}
FileRefs = Sequence[tuple[Path, int, str]]


def with_gui_derived(overrides: Mapping[str, str]) -> dict[str, str]:
    """The explicit process overrides plus the keys the GUI derives from them (GUI_DERIVED_KEYS): an explicit
    `sparse_infill_density` also sets `skeleton_infill_density` and `skin_infill_density` unless those are set
    explicitly too; the same for the line widths. Only an explicit override couples, never a base value."""
    out = dict(overrides)
    for src, derived in GUI_DERIVED_KEYS.items():
        if src in overrides:
            for k in derived:
                out.setdefault(k, overrides[src])
    return out


def system_base(resolver: P.PresetResolver, kind: str, name: str) -> str:
    """The system preset a preset name stands on: `name` itself, or for a user preset (`from: User`) the first system
    preset up its `inherits` chain. Studio resolves a slot's id by name against its installed library (Preset.cpp:
    2258-2345), so the file names the system preset and lists the user preset's keys as overrides."""
    seen: list[str] = []
    while name not in seen:
        seen.append(name)
        raw = resolver.load_raw(kind, name)
        parent = raw.get("inherits")
        if str(raw.get("from", "")).lower() != "user" or not parent:
            return name
        name = str(parent)
    raise BuildError(f"{kind} preset inherits loop: {' -> '.join(seen + [name])}")


def slot_keys(resolver: P.PresetResolver, kind: str, base: str, composed: Mapping, explicit: Collection[str] = ()) -> list[str]:
    """One slot of `different_settings_to_system`: the explicit overrides united with the keys whose value differs
    from shambu's resolved `base` (`PresetResolver.diff_keys`), sorted. The explicit keys are listed even when they
    equal shambu's base: Studio's effective base can differ (it applies only the include keys that differ from the
    collection default, PresetBundle.cpp:5141-5143, and may load another bundle version), and a key it does not find
    listed is reset on open. An equal listed key is harmless: Studio drops it on save (PresetBundle.cpp:3306-3313)."""
    keys = {k for k in explicit if k in composed and k not in P.BOOKKEEPING_KEYS}
    if kind == "filament":
        keys.discard("filament_colour")
    return sorted(keys | set(resolver.diff_keys(kind, base, dict(composed))))


def _bookkeeping(plan: Plan, resolver: P.PresetResolver) -> list[tuple[str, str, list[str]]]:
    """`Plan.bookkeeping` from the final presets (after tower planning wrote its keys): the process, each filament,
    the printer, in Studio's slot order. A filament still at the "loaded" placeholder gets no keys."""
    spec = plan.spec
    proc_base = system_base(resolver, "process", spec.process.base)
    explicit = set(with_gui_derived(spec.process.set)) | {k for k in TOWER_KEYS if k in plan.process}
    out = [("process", proc_base, slot_keys(resolver, "process", proc_base, plan.process, explicit))]
    for f, fspec in zip(plan.filaments, spec.filaments, strict=True):
        if f.get("name") == UNRESOLVED_FILAMENT:
            out.append(("filament", UNRESOLVED_FILAMENT, []))
            continue
        start = str(f["inherits"]) if f.get("from") == "User" and f.get("inherits") else str(f["name"])
        base = system_base(resolver, "filament", start)
        out.append(("filament", base, slot_keys(resolver, "filament", base, f, fspec.set)))
    machine_base = system_base(resolver, "machine", spec.printer)
    out.append(("machine", machine_base, slot_keys(resolver, "machine", machine_base, plan.machine)))
    return out


def _as_text(v) -> str:
    """A setting as one string for comparison: a list joined with `;` (Studio's vector form)."""
    return ";".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v)


def _slot(value: object, where: str) -> int:
    try:
        return int(str(value).strip())
    except ValueError:
        raise BuildError(f"{where} = {value!r} is not a filament slot number") from None


def effective_filaments(
    part_spec: PartSpec, files: FileRefs | None = None, process: Mapping | None = None, skip: Sequence[str] = ()
) -> set[int]:
    """Every filament slot an object prints with, after all overrides.

    A single file prints with its `filament` unless `set.extruder` replaces it. A merged object prints with
    the filaments of its normal and modifier sub-parts (plus `set.extruder`, kept as a conservative extra).
    Support, wall and infill filament keys in `set` and every height range's filament keys add their slot.
    With `process` (the resolved process: recipes and process.set included), its wall and infill filament keys
    add their slot for every part, and its support and support-interface filament keys for every part with
    `enable_support` on: the `dual-material-interface` recipe sets support_interface_filament 2 at process level,
    and a checker that missed it passed a plate that exited -102 (reference 06 section 5). "Has support" is
    `layout.needs_support` (a raft prints with the support filaments too). `skip` leaves out those keys of the part
    `set` and the process (`interface_uses` asks which slots the rest uses).
    Raises BuildError for a value that is not an integer. Slots are not range-checked here.
    """
    refs = list(files) if files is not None else [(f.path, f.filament, f.subtype) for f in part_spec.files()]
    ext = _slot(part_spec.set["extruder"], "set.extruder") if "extruder" in part_spec.set else 0
    base: set[int] = set()
    if len(refs) == 1:
        base.add(ext or refs[0][1])
    else:
        base |= {fil for _, fil, sub in refs if sub in PRINTING_SUBTYPES}
        if ext:
            base.add(ext)
    ranges = [(f"height range {r.min_z}..{r.max_z}", r.set) for r in part_spec.height_ranges]
    return L.filament_slots(base, part_spec.set, ranges, process, _slot, skip=skip)


def interface_uses(part_spec: PartSpec, process: Mapping, spread: float, files: FileRefs | None = None) -> dict[int, tuple[float, str]]:
    """The slot that prints only this supported part's support interface, {slot: (spread, source)} for
    `Placed.interface` (`layout.interface_only`). Raises BuildError for a value that is not a slot number."""
    others = effective_filaments(part_spec, files, process, skip=(L.INTERFACE_KEY,))
    ranges = [(f"height range {r.min_z}..{r.max_z}", r.set) for r in part_spec.height_ranges]
    return L.interface_only(part_spec.set, process, others, spread, _slot, ranges=ranges)


def _num(v, default: float) -> float:
    try:
        return float(v if not isinstance(v, list) else v[0])
    except (TypeError, ValueError, IndexError):
        return default


def _unique_key(base: str, used: set[str]) -> str:
    """`base`, or `base #n` with the lowest free n >= 2. Object names are global across plates."""
    key, n = base, 2
    while key in used:
        key, n = f"{base} #{n}", n + 1
    used.add(key)
    return key


def _rename_steps(pairs: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """Order renames so that no step targets a name another pending object still holds; a cycle goes through a
    temporary name. Every old name and every new name must be unique."""
    pending = dict(pairs)
    out: list[tuple[str, str]] = []
    while pending:
        ready = [old for old, new in pending.items() if new not in pending]
        if not ready:
            old = next(iter(pending))
            tmp = _unique_key(f"{pending[old]} (renaming)", set(pending) | set(pending.values()))
            out.append((old, tmp))
            pending[tmp] = pending.pop(old)
            continue
        for old in ready:
            out.append((old, pending.pop(old)))
    return out


@dataclass
class PlannedPart:
    spec: PartSpec
    key: str
    files: list[tuple[Path, int, str]]  # (path, filament, subtype); files[0] is the body
    infos: list[MeshInfo]  # one per entry of `files`, in the same order
    placed: L.Placed
    cli_name: str = ""  # the object name the CLI gives it (<stem>_<n> or assemble_<k>); renamed to `key` after export
    rename: bool = True  # False when `cli_name` is not unique in the project: the object keeps `cli_name`
    # group_by_filament: the single-file parts merged into this one object, and each one's bbox offset from the
    # group's bbox start (x0, y0). Their `placed` holds bed coordinates once `place_members` ran.
    members: list[PlannedPart] = field(default_factory=list)
    offsets: list[tuple[float, float]] = field(default_factory=list)
    # "orientation": "auto": Studio's pass-1 result (a single-file part), or the lay_on_face shambu's own scorer chose
    # (a merged part, which must turn in one frame)
    orient: OrientResult | None = None
    auto_face: tuple[float, float, float] | None = None

    @property
    def assembled(self) -> bool:
        return len(self.files) > 1

    @property
    def grouped(self) -> bool:
        return bool(self.members)

    def place_members(self) -> None:
        """Move every member to the group's bed position plus its offset inside the group."""
        for m, (dx, dy) in zip(self.members, self.offsets, strict=True):
            m.placed.x0, m.placed.y0 = self.placed.x0 + dx, self.placed.y0 + dy

    @property
    def file_name(self) -> str:
        """The object's name in the patched project."""
        return self.key if self.rename else self.cli_name


@dataclass
class PlannedPlate:
    name: str
    parts: list[PlannedPart]
    checks: list[L.Check] = field(default_factory=list)
    print_sequence: str = "by layer"
    tower: T.TowerPlan | None = None  # the prime tower of a plate that prints two or more filaments by layer
    packed_plates: int = 1  # how many plates the packer needed for this plate's free parts


@dataclass
class Plan:
    spec: Spec
    outdir: Path
    workdir: Path
    machine: dict
    process: dict
    filaments: list[dict]
    facts: dict
    filament_map: list[int]
    plates: list[PlannedPlate]
    assemble: A.AssembleList
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # The preset bookkeeping Studio honours on open (spec 2026-10-29 section 5.1), one entry per Studio slot in its
    # order: process, filament 1..N, printer. Each is (kind, base system preset name, sorted keys whose value differs
    # from that base). An empty key list means the slot is the unmodified system preset. Filled by `plan()`, written
    # into the export by `SetPresetBookkeeping`, read by the report and the verifier.
    bookkeeping: list[tuple[str, str, list[str]]] = field(default_factory=list)
    filament_resolution: FilamentResolution | None = None  # how each filament was chosen: the printer, the GUI or the spec

    @property
    def ok(self) -> bool:
        return not self.problems and all(c.ok for p in self.plates for c in p.checks)

    @property
    def unresolved_filaments(self) -> list[int]:
        """Slots still at the `"loaded"` placeholder: nothing to export for them, whatever `--keep-going` says."""
        return [i for i, f in enumerate(self.filaments, 1) if f.get("name") == UNRESOLVED_FILAMENT]

    @property
    def unplaced(self) -> list[str]:
        """Parts with no computed position (PLACEMENT NOT DONE, or past plate 1): nothing to export for them, whatever
        `--keep-going` says. Their pos would be a position nobody chose."""
        return [x.key for x in self.parts if not x.placed.placed]

    @property
    def exportable(self) -> bool:
        """The structural prerequisites of an export: every filament resolved and every part placed."""
        return not self.unresolved_filaments and not self.unplaced

    @property
    def map_applies(self) -> bool:
        """The filament -> extruder map is written (CLI, patch, verify) when the spec gives one or there are 2+ filaments."""
        return bool(self.spec.filament_map) or len(self.filaments) > 1

    @property
    def parts(self) -> list[PlannedPart]:
        return [x for p in self.plates for x in p.parts]


def used_filaments(slice_d: dict) -> int:
    """Filaments with grams on a plate per slice_info.config."""
    return sum(1 for f in slice_d.get("filaments") or [] if float(f.get("used_g") or 0) > 0)


def _estimate(plate: dict, sp: SlicedPlate | None) -> dict:
    e = {"plate": plate["id"], **plate["slice"]}
    ft = e.get("features")
    if ft:  # tool switches in the G-code: on a two-nozzle X2D a nozzle switch is the swap (result.json counts 0 there)
        e["filament_changes"] = ft["filament_changes"]
    elif used_filaments(e) <= 1:
        e["filament_changes"] = 0
    if sp is None:
        return e
    by_slot = {f.id: f for f in sp.filaments}
    fils = []
    for f in e.get("filaments") or []:
        try:
            rf = by_slot.get(int(f.get("id")))
        except (TypeError, ValueError):
            rf = None
        if rf is not None:  # only the fields the result.json entry carries: an absent field is not 0 g
            native = {"total_g": rf.total_g, "main_g": rf.main_g, "waste_g": rf.waste_g}
            f = {**f, **{k: round(v, 2) for k, v in native.items() if v is not None}}
        fils.append(f)
    e["filaments"] = fils
    if sp.feature_times_s is not None:
        e["feature_times_s"] = {k: round(v, 1) for k, v in sp.feature_times_s.items()}
    if sp.main_s is not None:
        e["main_prediction_s"] = int(sp.main_s)
    if sp.filament_change_times is not None:
        e["studio_filament_change_times"] = sp.filament_change_times
    return e


def needs_parse(slice_d: dict, sp: SlicedPlate | None, loaded: int) -> bool:
    """The G-code parser runs on a plate when it adds something: no result.json tower and flush grams for THIS plate,
    support used (support grams), or more than one filament loaded in the project (the tool-switch count; a slot's
    grams in slice_info do not prove the other slot never printed). A one-filament project without support skips."""
    return sp is None or not sp.has_waste or slice_d.get("support_used") != "false" or loaded > 1


def summarise(project: Path, run: RunResult | None) -> dict:
    """Project.summary() plus the G-code parser only where it adds something (`needs_parse`), decided per plate: a
    plate without its own result.json grams parses, a plate with them takes the native numbers."""
    pr = Project(str(project))
    s = pr.summary()
    by_id = {sp.id: sp for sp in (run.plates() if run else [])}
    loaded = len(s.get("filaments") or [])
    for p in s["plates"]:
        sl = p.get("slice")
        if sl is not None and needs_parse(sl, by_id.get(p["id"]), loaded):
            sl["features"] = pr.feature_usage(p["id"])
    return s


@dataclass
class BuildResult:
    plan: Plan
    project: Path
    inputs: dict[str, Path] = field(default_factory=dict)
    run: RunResult | None = None
    patches: list[str] = field(default_factory=list)
    verify_problems: list[str] | None = None
    summary: dict | None = None
    report_path: Path | None = None
    report: str = ""
    sliced: bool = False
    checks_skipped: dict | None = None  # the --no-check second pass: first exit, the pair, the report line

    @property
    def ok(self) -> bool:
        return self.plan.ok and self.run is not None and self.run.ok and not self.verify_problems

    @property
    def exported(self) -> bool:
        return self.run is not None and self.run.ok

    def export_problem(self) -> str | None:
        if self.run is None or self.run.ok:
            return None
        err = (self.run.result or {}).get("error_string") or ""
        return f"export failed: {self.run.summary()}" + (f"; Studio: {err}" if err else "")

    def export_fails(self) -> list[str]:
        """What a failed slice means for the plate, from Studio's log. Exit -101 names the two objects whose paths
        cross (Print.cpp:2434; the log has no z, so none is given). Exit -21 is Studio's own arrange giving up
        (BambuStudio.cpp:5477-5485). The pre-slice WARN lines are estimates: this is the measured answer."""
        if self.run is None or self.run.ok or self.run.code not in (-101, -21):
            return []
        try:
            text = self.run.log.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            what = "the conflicting objects" if self.run.code == -101 else "the plate"
            return [f"Studio failed with exit {self.run.code}, and its log {self.run.log} could not be read ({e}): {what} are unknown."]
        if self.run.code == -21:
            n = arrange_failed_plate(text)
            return [
                f"Studio's arrange could not fit the parts on plate {n if n is not None else '(the log does not say which)'} "
                "(it keeps 24 mm between objects when any part has tree supports)"
            ]
        by_cli: dict[str, list[PlannedPart]] = {}
        for x in self.plan.parts:
            if x.cli_name:
                by_cli.setdefault(x.cli_name, []).append(x)
        pairs = conflicts_in_log(text, by_cli)
        if not pairs:
            return [f"Studio found {NO_PAIR}. Move the parts apart and build again."]
        out = []
        for a, b in pairs:
            if b is None:
                out.append(f"Studio found {conflict_phrase(a, None)}. Move the parts apart and build again.")
                continue

            def name(cli: str) -> str:
                got = by_cli.get(cli, [])
                if len(got) == 1:
                    return got[0].key
                if got:
                    return f"{cli} (one of {', '.join(x.key for x in got)}: they share that CLI name, so the log cannot tell which)"
                return cli

            pa, pb = by_cli.get(a, []), by_cli.get(b, [])
            reach = ""
            if len(pa) == 1 and len(pb) == 1:
                qa, qb = pa[0].placed, pb[0].placed
                _, (dx, dy) = L.rect_gap(qa.rect(with_margin=False), qb.rect(with_margin=False))
                fa, sa = qa.spread.facing(dx, dy)
                fb, sb = qb.spread.facing(-dx, -dy)
                who, f, side, src = (qa, fa, sa, qa.spread.source) if fa >= fb else (qb, fb, sb, qb.spread.source)
                if f > 0:
                    reach = f" ({who.key}'s support reaches {f:.1f} mm on {side}" + (", estimated)" if src == "estimate" else ")")
            out.append(f"Studio found {conflict_phrase(name(a), name(b))}. Move them apart{reach} and build again.")
        return out

    def slicer_warnings(self) -> list[str]:
        """Studio's own slicing warnings from result.json (floating cantilevers, regions), not a failure."""
        w = (self.run.result or {}).get("warning_message") if self.run else None
        return [str(w)] if w else []

    def estimates(self) -> list[dict]:
        """Per sliced plate: slice_info.config (time, grams, filaments; `features` from the G-code parser when present),
        merged with the run's result.json: `feature_times_s` (role -> seconds), `main_prediction_s` (without prepare,
        tower and flush), `studio_filament_change_times` (filament loads, 0 on a two-nozzle plate), and per filament
        `total_g`, `main_g` (model + support) and `waste_g` (flush + tower). Each result.json key is absent when the
        run left no result.json or the plate's entry lacks that field. `filament_changes` is the parser's tool-switch
        count (0 on a one-filament plate)."""
        by_id = {sp.id: sp for sp in (self.run.plates() if self.run else [])}
        return [_estimate(p, by_id.get(p["id"])) for p in (self.summary or {}).get("plates", []) if p.get("slice")]

    def to_json(self) -> dict:
        problems = list(self.plan.problems) + ([self.export_problem()] if self.export_problem() else []) + self.export_fails()
        return {
            "name": self.plan.spec.name,
            "project": str(self.project) if self.exported else None,
            "ok": self.ok,
            "plan_ok": self.plan.ok,
            "sliced": self.sliced and self.exported,
            "estimates": self.estimates(),
            "problems": problems,
            "warnings": list(dict.fromkeys(self.plan.warnings + self.slicer_warnings())),
            "filaments": self.plan.filament_resolution.to_json() if self.plan.filament_resolution else None,
            "plates": [
                {
                    "name": p.name,
                    "print_sequence": p.print_sequence,
                    "layout": L.describe([x.placed for x in p.parts]),
                    "groups": [
                        {
                            "name": x.key,
                            "filament": x.placed.filament,
                            "members": [
                                {
                                    "name": m.key,
                                    "filament": m.placed.filament,
                                    "size": [round(v, 2) for v in m.placed.info.size],
                                    "offset": [round(dx, 2), round(dy, 2)],
                                    "bbox": [round(v, 2) for v in m.placed.rect(with_margin=False)],
                                }
                                for m, (dx, dy) in zip(x.members, x.offsets, strict=True)
                            ],
                        }
                        for x in p.parts
                        if x.grouped
                    ],
                    "checks": [[c.ok, c.text] for c in p.checks],
                    "check_kinds": [c.kind for c in p.checks],
                    "check_levels": [("warn" if c.warn else "ok") if c.ok else "fail" for c in p.checks],
                }
                for p in self.plan.plates
            ],
            "cli": self.run.summary() if self.run else None,
            "patches": self.patches,
            "verify_problems": self.verify_problems,
            "inputs": {k: str(v) for k, v in self.inputs.items()},
            "report": str(self.report_path) if self.report_path else None,
            "checks_skipped": self.checks_skipped,
        }


# `--no-check` (BambuStudio.cpp:6192, 6418, 6974): it skips the G-code path conflict check (exit -101), the
# support-necessity check, and the exit -100 on empty-layer or overlap warnings. It does not skip -102..-105 or any
# other validate error. With it Studio reports nothing about a conflict, so the flag is only ever a second pass after
# checked passes, and the report line comes from the checked logs (Print.cpp:2434). Studio exits on the FIRST plate
# that conflicts (BambuStudio.cpp:7105-7109), so a project with several plates gets one checked pass per plate. Exit
# -100 is also the generic exception exit (BambuStudio.cpp:7413-7418): only a -100 whose log shows Studio's
# `found slicing warnings:` line (BambuStudio.cpp:7126) is skippable; any other -100 stays a failure.
NO_CHECK_SKIPPED = (
    "G-code path conflict check (exit -101)",
    "support-necessity check (floating cantilevers and regions)",
    "exit -100 on empty-layer or overlap warnings",
)
_SLICING_WARNINGS = re.compile(r"found slicing warnings: (.+?)(?:, no_check=\d)?\s*$", re.M)
_ERROR_LINE = re.compile(r"\[error\]\s+(.+?)\s*$", re.M)


def _log_text(rr: RunResult) -> tuple[str, str | None]:
    """Studio's log of a pass and, when it cannot be read, why (never a silent empty log: the record says so)."""
    try:
        return Path(rr.log).read_text(encoding="utf-8", errors="replace"), None
    except OSError as e:
        return "", f"Studio's log {rr.log} could not be read ({e.strerror or e}): the pass is classified by its exit code"


def checked_outcome(rr: RunResult, plate: int, known: Collection[str] = ()) -> dict:
    """One checked pass: `ok`, `skippable` (-101, or -100 with Studio's slicing-warning line) or `fail`, with what the log
    says. A -101 pair comes from `studio.conflicts_in_log` (`known`: the CLI object names) and reads as `export_fails`
    does (`studio.conflict_phrase`). The record keeps the pass's own log and result.json paths."""
    text, unreadable = _log_text(rr)
    rec: dict = {
        "plate": plate,
        "exit": rr.code,
        "meaning": rr.meaning(),
        "log": str(rr.log),
        "result": str(Path(rr.log).parent / "result.json"),
    }
    if rr.code == 0 and rr.ok:
        rec.update(kind="ok", reason="passed the checks")
    elif rr.code == -101:
        pairs = conflicts_in_log(text, known)
        first = pairs[0] if pairs else None
        rec.update(
            kind="skippable",
            pair=list(first) if first is not None and first[1] is not None else None,
            reason="; ".join(conflict_phrase(a, b) for a, b in pairs) if pairs else NO_PAIR,
        )
    elif rr.code == -100 and _SLICING_WARNINGS.search(text):
        m = _SLICING_WARNINGS.search(text)
        rec.update(kind="skippable", pair=None, reason=f"empty layers or overlap: {m.group(1) if m else ''} (exit -100)")
    else:
        errs = _ERROR_LINE.findall(text)
        rec.update(kind="fail", reason=errs[-1] if errs else rr.meaning())
    if unreadable:
        rec["warning"] = unreadable
        rec["reason"] += f" (WARN: {unreadable})"
    return rec


def _moved(rr: RunResult, src: Path, dest: Path) -> RunResult:
    if src.exists():
        shutil.move(str(src), str(dest))
    return replace(rr, output=dest)


def _finite(v) -> float | None:
    try:
        f = float(str(v))
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def sliced_problems(project: Project, summary: dict, plates: Collection[int]) -> list[str]:
    """What a sliced export lacks, for `build --slice` and `shambu slice` alike: G-code at all, every requested plate
    id (`plates`, from the input, never from the export) present, and per requested plate its
    `Metadata/plate_N.gcode` entry and its slice results, and no G-code outside the printable area. [] when complete."""
    probs = []
    if not summary["sliced"]:
        probs.append("the file holds no G-code although a slice was requested")
    names = set(project.names())
    present = {pl["id"] for pl in summary["plates"]}
    probs += [f"plate {n} is missing from the export (the input has it)" for n in sorted(set(plates) - present)]
    for pl in summary["plates"]:
        if pl["id"] not in plates:
            continue
        if f"Metadata/plate_{pl['id']}.gcode" not in names:
            probs.append(f"plate {pl['id']} has no G-code entry (Metadata/plate_{pl['id']}.gcode)")
        if not pl.get("slice"):
            probs.append(f"plate {pl['id']} has no slice results")
        elif pl["slice"].get("outside") == "true":
            probs.append(f"plate {pl['id']}: the slicer reports G-code outside the printable area")
    return probs


def verify_tower(
    ps: Mapping, n_plates: int, wrote: Mapping[str, str], spots: Mapping[int, tuple[float, float]], who: str = "the tower plan"
) -> list[str]:
    """The strict tower verify, for `build` and `shambu slice`: every key in `wrote` is in the file with that finite
    number, and when any plate has a spot (`spots`: 0-based plate -> (x, y)) `wipe_tower_x/y` hold one value per plate
    with those values (Studio's `get_at` falls back to the FIRST value for a short vector, so a short vector misplaces
    later towers)."""
    probs: list[str] = []
    for k, want in wrote.items():
        got = ps.get(k)
        if k not in ps:
            probs.append(f"{k} is missing from the file, {who} wrote {want!r}")
        elif _finite(got) is None or _finite(got) != _finite(want):
            probs.append(f"{k} in file is {got!r}, {who} wrote {want!r}")
    if not spots:
        return probs
    for axis, j in (("x", 0), ("y", 1)):
        key = f"wipe_tower_{axis}"
        vec = ps.get(key)
        if not isinstance(vec, list) or len(vec) < n_plates:
            probs.append(f"{key} in file is {vec!r}: it needs one value per plate ({n_plates})")
            continue
        for i, xy in spots.items():
            got_v = _finite(vec[i])
            if got_v is None or abs(got_v - xy[j]) > 0.01:
                probs.append(f"plate {i + 1} {key} in file is {vec!r}, {who} put it at {xy[j]:g}")
    return probs


def two_pass_slice(
    studio: StudioCLI,
    make_cmd: Callable[[bool, int, Path], CliCommand],
    *,
    name: str,
    outputdir: Path,
    timeout: int,
    expect_output: Path,
    no_check: bool,
    plates: Sequence[int] = (0,),
    known: Collection[str] = (),
) -> tuple[RunResult, dict | None]:
    """Without `no_check`: one checked slice of every plate (or of `plates[0]`). With it: one checked pass per plate in
    `plates` (`(0,)` = all at once, for a one-plate project), each in its own folder under `<outputdir>/<name>.passes/`
    so no pass overwrites another's log or result.json. When several plates all pass, one combined checked pass slices
    them together, and it is classified like the others (the conflict check is not deterministic at a near-touching
    pair). A --no-check pass runs only when every failing pass failed in a skippable way (-101, or -100 with Studio's
    slicing-warning line). `make_cmd(no_check, plate, outdir)` builds a command that exports `expect_output.name` into
    `outdir`. Returns the run that produced `expect_output` and the `checks_skipped` record (None when every check
    passed)."""
    whole = plates[0] if len(plates) == 1 else 0
    if not no_check:
        return studio.run(
            make_cmd(False, whole, outputdir), name=name, outputdir=outputdir, timeout=timeout, expect_output=expect_output
        ), None
    passes = Path(outputdir) / f"{name}.passes"
    runs: list[tuple[RunResult, dict]] = []

    def checked(plate: int, folder: str) -> None:
        d = passes / folder
        rr = studio.run(
            make_cmd(False, plate, d), name=f"{name}-checked", outputdir=d, timeout=timeout, expect_output=d / expect_output.name
        )
        runs.append((rr, checked_outcome(rr, plate, known)))

    for pl in plates:
        checked(pl, "checked" if len(plates) == 1 else f"checked-plate-{pl}")
    if len(plates) > 1 and all(rec["kind"] == "ok" for _, rec in runs):
        checked(0, "checked-all")
    records = [rec for _, rec in runs]
    fails = [(rr, rec) for rr, rec in runs if rec["kind"] == "fail"]
    skippable = [rec for rec in records if rec["kind"] == "skippable"]
    if fails:
        rr, rec = fails[0]
        where = f"plate {rec['plate']}" if rec["plate"] else ("the plate" if len(plates) == 1 else "the combined pass")
        return rr, {
            "skipped": False,
            "plates": records,
            "report_line": f"slice: {where} failed the checked pass with exit {rec['exit']} ({rec['meaning']}: {rec['reason']}); "
            "--no-check does not bypass that, so no unchecked pass ran",
        }
    if not skippable:
        rr = runs[-1][0]
        return _moved(rr, Path(rr.log).parent / expect_output.name, expect_output), None
    d2 = passes / "nocheck"
    second = studio.run(
        make_cmd(True, whole, d2), name=f"{name}-nocheck", outputdir=d2, timeout=timeout, expect_output=d2 / expect_output.name
    )
    if second.ok:
        second = _moved(second, d2 / expect_output.name, expect_output)

    def label(r: dict) -> str:
        if r["plate"]:
            return f"plate {r['plate']}: "
        return "all plates together: " if len(plates) > 1 else ""

    found = "; ".join(label(r) + r["reason"] for r in skippable)
    look = "at that pair" if all(r.get("pair") for r in skippable) and len(skippable) == 1 else "at each of these"
    line = (
        f"slice: Studio's checks found {found}; exported with --no-check as the GUI would, with a serious warning. Check the "
        f"preview {look} before printing. The conflict check and the support-necessity check did not run on this G-code."
    )
    if not second.ok:
        line = f"slice: the checked pass found {found}, and the --no-check pass exited {second.code} ({second.meaning()}); nothing exported"
    first = skippable[0]
    return second, {
        "skipped": second.ok,
        "plates": records,
        "first_exit": first["exit"],
        "first_meaning": first["meaning"],
        "first_log": first["log"],
        "pair": first.get("pair"),
        "reason": found,
        "checks": list(NO_CHECK_SKIPPED),
        "second_exit": second.code,
        "second_log": str(second.log),
        "second_result": str(Path(second.log).parent / "result.json"),
        "report_line": line,
    }


class Pipeline:
    """Template Method: `build()` calls the steps in order. Override a step to change behaviour."""

    def __init__(
        self,
        studio: StudioCLI | None = None,
        resolver: P.PresetResolver | None = None,
        placer: L.Placer | None = None,
        checker: L.LimitChecker | None = None,
        listeners: list[Listener] | None = None,
        probe: SpreadProbe | None = None,
    ):
        self.studio = studio or StudioCLI()
        self.resolver = resolver or P.resolver()
        self.placer = placer
        self.checker = checker or L.LimitChecker()
        self.listeners = listeners or []
        self.probe = probe  # None: a StudioProbe under `probe_dir` (default <outdir>/.probe) when a plan measures
        self.probe_dir: Path | None = None  # a shared probe cache (sweep: the input spec's outdir, for every variant)
        self.checks_skipped: dict | None = None

    def emit(self, event: str, **data) -> None:
        for fn in self.listeners:
            fn(event, data)

    # ---------------------------------------------------------------- the template
    def build(
        self,
        spec: Spec,
        *,
        export: bool = True,
        timeout: int = 1200,
        keep_going: bool = False,
        do_slice: bool = False,
        filaments: FilamentResolution | None = None,
        probe_supports: bool = False,
        no_check: bool = False,
        no_probe: bool = False,
    ) -> BuildResult:
        """`filaments` is the spec reconciled with the printer (loaded.reconcile): its spec replaces `spec`, and its
        problems and warnings lead the plan's. Without it a `"loaded"` filament is a problem. `probe_supports`: slice
        each supported single-file part alone to measure its support spread (probe.py), in place of the estimate.
        `no_check` (with `do_slice`) slices again with --no-check when the checked slice exits -101 or -100
        (`two_pass_slice`). `no_probe` turns off the measuring fallback of `plan`."""
        if no_check and not do_slice:
            raise BuildError("--no-check needs --slice")
        self.checks_skipped = None
        if filaments is not None:
            spec = filaments.spec
        self.emit("plan", name=spec.name)
        plan = self.plan(spec, filaments, probe_supports=probe_supports, no_probe=no_probe)
        result = BuildResult(plan, plan.outdir / (f"{spec.name}.gcode.3mf" if do_slice else f"{spec.name}.3mf"), sliced=do_slice)
        result.inputs = self.write_inputs(plan)
        self.emit("planned", ok=plan.ok, problems=plan.problems)
        # --keep-going overrides advisory checks, never a placeholder filament or a part without a position
        if plan.exportable and (plan.ok or keep_going):
            if export:
                if no_check:  # a parameter, so an export() override sees it (or fails loudly when it cannot take it)
                    result.run = self.export(plan, result.inputs, result.project, timeout, do_slice, no_check=True)
                else:
                    result.run = self.export(plan, result.inputs, result.project, timeout, do_slice)
                result.checks_skipped = self.checks_skipped  # report.checks_skipped_lines: under the estimates
                if result.run.ok:
                    result.patches = self.patch(plan, result.project, do_slice)
                    result.verify_problems = self.verify(plan, result.project, do_slice)
                    result.summary = summarise(result.project, result.run)
        result.report = self.report(result)
        result.report_path = plan.outdir / f"{spec.name}.report.md"
        result.report_path.write_text(result.report, encoding="utf-8")
        self.emit("finished", ok=result.ok, report=str(result.report_path))
        return result

    # ---------------------------------------------------------------- steps
    def plan(
        self, spec: Spec, resolution: FilamentResolution | None = None, *, probe_supports: bool = False, no_probe: bool = False
    ) -> Plan:
        """Measure, place and check every plate. The support spread is an estimate unless `probe_supports` measures it.
        The estimate is advisory, so the plan does not fail closed on it: when packing with the estimates needs more
        plates than the spec declares, or the auto tower finds no spot clear of them, the plates concerned are planned
        again with the support probe (a cached solo slice per supported part) and say so in a `Spreads measured:` line.
        `no_probe` keeps the estimates' answer."""
        plan = self._plan_once(spec, resolution, None if probe_supports else set())
        if probe_supports or no_probe:
            return plan
        why = {i: w for i, p in enumerate(plan.plates, 1) if (w := self._estimate_failure(p))}
        if not why:
            return plan
        measured = self._plan_once(spec, resolution, set(why))
        for i, cause in why.items():
            plate = measured.plates[i - 1]
            plate.checks.insert(0, L.Check(True, self._measured_line(plate, cause, measured.warnings), "measured"))
        return measured

    @staticmethod
    def _estimate_failure(p: PlannedPlate) -> str | None:
        """Why the estimates failed this plate, or None: more plates than declared, a part left without a position, no
        tower spot clear of them, or an estimated spread past a nozzle's reach."""
        if not any(x.placed.spread.source == "estimate" for x in p.parts):
            return None
        if p.packed_plates > 1:
            return f"the estimated support spread needed {p.packed_plates} plates"
        lost = [x.key for x in p.parts if not x.placed.placed and x.placed.spread.source == "estimate"]
        if lost:
            return f"the estimated support spread left {', '.join(lost)} without a position"
        t = p.tower
        if t is not None and t.mode == "auto" and (t.problem or t.warning):
            return "the estimated support spread left no tower spot"
        if any(c.kind == L.REACH_ESTIMATE for c in p.checks):
            return "the estimated support spread crossed a nozzle's reach"
        return None

    @staticmethod
    def _measured_line(p: PlannedPlate, cause: str, warnings: list[str]) -> str:
        bits = []
        for x in p.parts:
            sp = x.placed.spread
            if sp.source == "probe":
                bits.append(f"{x.key}: " + "/".join(f"{v:.1f}" for v in sp.sides()))
            elif sp.source == "estimate":
                failed = any(f"({x.spec.key()}): support probe failed" in w for w in warnings)
                bits.append(f"{x.key}: estimate kept ({'the probe failed' if failed else 'not probed: several files'})")
        tail = (
            f"the plate still needs {p.packed_plates} plates with the measurements: the packer's answer (a heuristic, no "
            "rotation); place the parts with `at` or split the spec"
            if p.packed_plates > 1
            else "placement uses the measurements"
        )
        return f"Spreads measured: {cause}; a solo slice per supported part measured (mm on -x/+x/-y/+y) " + "; ".join(bits) + "; " + tail

    def _plan_once(self, spec: Spec, resolution: FilamentResolution | None, probe_plates: set[int] | None) -> Plan:
        """One planning pass. `probe_plates`: the plates whose supported parts the probe measures (None: every plate)."""
        outdir = spec.outdir.expanduser().resolve()
        workdir = outdir / "shambu"
        workdir.mkdir(parents=True, exist_ok=True)
        problems: list[str] = list(resolution.problems) if resolution else []
        warnings: list[str] = list(resolution.warnings) if resolution else []
        warnings += [w for w in spec.warnings if w not in warnings]  # unknown keys: `check` and `build` warn as `spec` does
        problems += spec.validate()
        machine = self.resolver.for_cli("machine", spec.printer)
        process = self.resolver.for_cli("process", spec.process.base, with_gui_derived(spec.process.set), spec.process.name)
        filaments: list[dict] = []
        for i, fspec in enumerate(spec.filaments, 1):
            if fspec.loaded:
                # Left to the printer but never resolved (no reconcile ran, or it found nothing to load): a placeholder
                # keeps the placement checks running; the problem keeps the plan from exporting.
                if resolution is None:
                    problems.append(
                        f'filament {i}: preset "loaded" was not resolved against the printer (`shambu check` and `build` read it)'
                    )
                filaments.append(
                    {"name": "loaded (unresolved)", "type": "filament", "from": "system", "filament_type": ["?"], "compatible_printers": []}
                )
            else:
                filaments.append(self.resolver.for_cli("filament", fspec.preset, fspec.set, fspec.name))
        for i, (f, fs) in enumerate(zip(filaments, spec.filaments, strict=True)):
            # Without a colour per filament the CLI leaves the flush matrix 1 x 1 and headless slicing segfaults
            # (FilamentGroup::calc_one_change_budget). The colour also shows in Studio.
            f["filament_colour"] = [fs.colour or DEFAULT_COLOURS[i % len(DEFAULT_COLOURS)]]
        problems += P.check_compatible(machine, process, filaments)
        facts = P.machine_facts(machine)
        n_ext = len(facts["extruders"])
        fmap = list(spec.filament_map) if spec.filament_map else [min(i, n_ext) for i in range(1, len(filaments) + 1)]
        if len(fmap) != len(filaments) or any(not 1 <= v <= n_ext for v in fmap):
            problems.append(f"filament_map {fmap} must give one extruder (1..{n_ext}) per filament ({len(filaments)})")
        known = (
            set(process)
            | set(machine)
            | set().union(*(set(f) for f in filaments))
            | {"extruder"}
            | set(A.PLATE_PARAMS_SAVED)
            | R.all_keys()
        )
        tpu = {i for i, f in enumerate(filaments, 1) if "TPU" in str(f.get("filament_type", ""))}
        ctx = L.Context(facts, {i + 1: fmap[i] for i in range(len(fmap))}, tpu, spec.aux_x_limit)
        bed = facts["bed"]
        # By layer: candidate points with the max-not-sum pair rule (layout.BottomLeftPacker). By object: rows in print
        # order (ShelfPacker below).
        placer = self.placer or L.BottomLeftPacker(
            (spec.layout.edge, bed["x"][1] - spec.layout.edge), (spec.layout.edge, bed["y"][1] - spec.layout.edge), spec.layout.gap
        )
        map_applies = bool(spec.filament_map) or len(filaments) > 1
        # The plate's print_sequence metadata beats the process key (reference 08 section 2), so the effective value
        # per plate is the plate's own, else the process preset's (a user may set it in process.set).
        proc_seq = str(process.get("print_sequence", "by layer"))
        sequences = [p.sequence(proc_seq) for p in spec.plates]
        if any(sq == "by object" for sq in sequences):
            # Studio refuses these two with print-by-object (Print.cpp validate, reference 08).
            if str(process.get("timelapse_type", "0")) == "1":
                problems.append("print_sequence 'by object' does not allow Smooth timelapse (timelapse_type 1)")
            if str(process.get("enable_wrapping_detection", "0")) == "1":
                problems.append("print_sequence 'by object' does not allow clumping detection (enable_wrapping_detection 1)")
        clr = facts["clearance"]
        skirt_extra = 0.0
        if _num(process.get("skirt_loops"), 0) > 0 and str(process.get("skirt_per_object", "1")) not in ("0", "false"):
            skirt_extra = _num(process.get("skirt_distance"), 2.0) + _num(process.get("skirt_loops"), 0) * _num(
                process.get("line_width"), 0.42
            )
        ctx = replace(ctx, skirt_extra=skirt_extra)

        alist = A.AssembleList()
        plates: list[PlannedPlate] = []
        requests: list[T.TowerRequest | None] = []  # one per plate; planned together after the loop (one size per project)
        used_keys: set[str] = set()
        k = 0  # assemble_index: unique across the project, as object names are global
        # Native orientation (pass 1) loads the same machine and process JSON as the export (pass 2), so Studio's
        # auto-orient uses the process's support_threshold_angle. write_inputs() writes the same files again later.
        orient_settings = (
            [P.write(machine, workdir / "machine.json"), P.write(process, workdir / "process.json")]
            if any(ps.orientation.auto for pl in spec.plates for ps in pl.parts)
            else None
        )
        probe: SpreadProbe | None = None
        if probe_plates is None or probe_plates:
            cache = self.probe_dir or outdir / ".probe"
            probe = self.probe or StudioProbe(self.studio, cache, machine, process, filaments, spec.bed_type, fmap)
        for pi, (pspec, seq) in enumerate(zip(spec.plates, sequences, strict=True), 1):
            on = probe if probe_plates is None or pi in probe_plates else None
            parts = self._measure(pspec.parts, pi, process, problems, len(filaments), used_keys, workdir, orient_settings, on, warnings)
            by_object = seq == "by object"
            if pspec.group_by_filament:
                if by_object:
                    parts, notes = self._group_by_filament(parts, spec.layout, bed, ctx, used_keys)
                    warnings += [f"plate {pi} '{pspec.name}': {n}" for n in notes]
                else:
                    problems.append(
                        f"plate {pi} '{pspec.name}': group_by_filament needs print_sequence 'by object'; the plate prints {seq}"
                    )
            sequential = by_object and len(parts) > 1  # Studio applies the by-object rules (and drops the tower) only then
            if by_object:
                # Studio prints a by-object plate in object order. Group by main filament (two single-filament groups
                # then need one nozzle change in total), shortest first within a group so an object over the rod height
                # prints after the shorter ones that could share its Y band; the packer closes the row after a tall one.
                parts.sort(key=lambda p: (min(p.placed.filaments), p.placed.info.size[2]))
                if sequential and len({f for p in parts for f in p.placed.filaments}) > 1:
                    warnings.append(
                        f"plate {pi} '{pspec.name}': print-by-object, so Studio builds no prime tower; the idle nozzle is not wiped between objects"
                    )
            multi = len({f for p in parts for f in p.placed.filaments}) > 1 and not sequential
            # Smooth timelapse needs a tower on a one-filament plate too (PartPlate.cpp:2222-2223)
            multi = multi or (T.smooth_timelapse(process) and not sequential)
            fixed = [p.placed for p in parts if p.placed.fixed]
            free = [p.placed for p in parts if not p.placed.fixed]
            # The tower footprint (fixed spot, or a provisional back-right corner for "auto") keeps packed parts out;
            # TowerClear checks the final footprint after every plate is planned.
            reserve, tower_base = self._tower_reserve(pspec, pi, multi, parts, bool(free), process, filaments, facts, spec, warnings)
            pctx = replace(ctx, by_object=sequential)
            obstacles = [reserve] if reserve else []
            placement_checks: list[L.Check] = []
            overflow = False
            if free:
                bounds = {p.key: L.reach_bounds(p, ctx) for p in free}  # per filament, each with its own growth
                x_min_of = {k: b[0] for k, b in bounds.items()}
                x_max_of = {k: b[1] for k, b in bounds.items()}
                plate_placer = placer
                if sequential and self.placer is None:
                    all_short = all(p.placed.info.size[2] < clr["nozzle_height"] for p in parts)
                    plate_placer = L.ShelfPacker(
                        (spec.layout.edge, bed["x"][1] - spec.layout.edge),
                        (spec.layout.edge, bed["y"][1] - spec.layout.edge),
                        max(spec.layout.gap, pctx.sequential_gap(all_short)),
                        keep_order=True,
                        solo_row_above=clr["height_to_rod"],
                    )
                packed = plate_placer.place(free, obstacles, x_min_of, fixed=fixed, x_max_of=x_max_of)
                placement_checks = self._placement_checks(packed, free, plate_placer)
                overflow = len(packed) > 1  # the PLACEMENT NOT DONE line says it (one line, not a Problem as well)
            for p in parts:
                p.place_members()
            requests.append(self._tower_request(pspec, pi, multi, parts, tower_base, facts, spec) if not overflow else None)
            checks = placement_checks + self.checker.check([p.placed for p in parts], pctx)  # TowerClear: after every plate
            checks += self._subpart_checks([p for p in parts if p.placed.placed], pctx)
            plate = A.Plate(
                name=pspec.name or f"plate {pi}",
                need_arrange=False,
                plate_params=self._plate_params(spec, fmap, map_applies, seq if (pspec.print_sequence or proc_seq != "by layer") else None),
            )
            groups: dict[int, list[PlannedPart]] = {}
            for p in parts:  # copies of one spec share an Item (count + pos list)
                groups.setdefault(id(p.spec), []).append(p)
            pinned: list[str] = []
            for group in groups.values():
                first = group[0]
                settings = dict(first.spec.set)
                if len(filaments) > 1 and L.needs_support({**process, **settings}):
                    # Studio prints "default" (0) support with whichever filament is loaded; on a two-filament plate that
                    # put a filament-1 part's support base on the right nozzle at x < 20.5 (exit -102, reference 08 3.3b).
                    for key in ("support_filament", "support_interface_filament"):
                        if str(settings.get(key, process.get(key, "0"))) in ("0", ""):
                            settings[key] = str(first.placed.filament)
                            pinned.append(f"{first.key}:{key}")
                # The CLI translates copy 1 by pos[0], then CLONES that translated object for copies 2..n and translates
                # each by its own pos[i] (reference 01 section 2.4, live-verified 2026-10-13): their pos is relative to
                # copy 1's world position, so pos[i] = T_i - T_0.
                first_t = group[0].placed.pos()
                positions = [first_t] + [
                    (p.placed.pos()[0] - first_t[0], p.placed.pos()[1] - first_t[1], p.placed.pos()[2] - first_t[2]) for p in group[1:]
                ]
                # A range without layer_height segfaults the slicer (layer_height_profile_from_ranges); the GUI always writes it.
                ranges = [
                    A.HeightRange(r.min_z, r.max_z, {"layer_height": str(process.get("layer_height", "0.2")), **r.set})
                    for r in first.spec.height_ranges
                ]
                if first.grouped:
                    # One merged object `assemble_k`; the merge keeps each volume's own translation (notes-research
                    # section A). Copies of one spec share an Item, positioned like copies above (pos[i] = T_i - T_0).
                    k += 1
                    by_spec: dict[int, list[PlannedPart]] = {}
                    for m in first.members:
                        by_spec.setdefault(id(m.spec), []).append(m)
                    for copies in by_spec.values():
                        t0 = copies[0].placed.pos()
                        pos = [t0] + [(c.placed.pos()[0] - t0[0], c.placed.pos()[1] - t0[1], c.placed.pos()[2] - t0[2]) for c in copies[1:]]
                        fp, _, sub = copies[0].files[0]
                        plate.items.append(A.Item(fp, first.placed.filament, len(copies), sub, [k] * len(copies), pos))
                    plate.assembled.append(A.Assembled(k, settings, ranges))
                    first.cli_name = f"assemble_{k}"
                elif first.assembled:
                    idx = list(range(k + 1, k + 1 + len(group)))
                    k += len(group)
                    for fp, fil, sub in first.files:
                        plate.items.append(A.Item(fp, fil, len(group), sub, idx, positions))
                    for i in idx:
                        plate.assembled.append(A.Assembled(i, settings, ranges))
                    for n, p in enumerate(group):
                        p.cli_name = f"assemble_{idx[n]}"
                else:
                    fp, fil, sub = first.files[0]
                    plate.items.append(A.Item(fp, fil, len(group), sub, 0, positions, settings, ranges))
                    for n, p in enumerate(group, 1):
                        p.cli_name = f"{fp.stem}_{n}"
            if pinned:
                warnings.append(
                    f"plate {pi} '{pspec.name}': support filament pinned to the part's own filament for "
                    + ", ".join(pinned)
                    + " (Studio's default 0 prints support with whichever filament is loaded; the right nozzle cannot reach x < 20.5)"
                )
            alist.plates.append(plate)
            plates.append(PlannedPlate(plate.name, parts, checks, seq, packed_plates=len(packed) if free else 1))
        self._plan_towers(plates, requests, spec)
        problems += self._tower_globals(plates, process)
        warnings += self._name_objects([x for p in plates for x in p.parts])
        problems += A.validate(alist.to_json(), known_keys=known, filament_count=len(filaments))
        plan = Plan(
            spec,
            outdir,
            workdir,
            machine,
            process,
            filaments,
            facts,
            fmap,
            plates,
            alist,
            problems,
            warnings,
            filament_resolution=resolution,
        )
        plan.bookkeeping = _bookkeeping(plan, self.resolver)  # the process is final: tower planning wrote its keys above
        return plan

    # ---------------------------------------------------------------- prime tower (tower.py)
    @staticmethod
    def _tower_reserve(
        pspec, pi: int, multi: bool, parts: list[PlannedPart], has_free: bool, process: dict, filaments: list[dict], facts: dict,
        spec: Spec, warnings: list[str],
    ) -> tuple[L.Rect | None, T.TowerSettings | None]:  # fmt: skip
        """The rect packed parts must avoid on a two-filament plate, and the plate's tower settings (None: no tower)."""
        if not multi:
            return None, None
        wt = pspec.wipe_tower
        if wt.mode == "off" or str(process.get("enable_prime_tower", "1")).lower() in ("0", "false"):
            warnings.append(T.off_warning(pi))
            return None, None
        n_fil = len({f for p in parts for f in p.placed.filaments})
        base = wt.settings(T.TowerSettings.from_config(process, filaments, n_fil, len(facts["extruders"])))
        height = max((p.placed.info.size[2] for p in parts), default=0.0)
        if wt.mode == "fixed" and wt.x is not None and wt.y is not None:
            return T.footprint(base, height, wt.x, wt.y).rect, base  # TowerClear reports a bad spot (once)
        if not has_free:
            return None, base
        area = T.shared_reach(facts["extruders"], facts["bed"], spec.aux_x_limit)
        w, d = base.size(height)
        b, c = base.brim_at(height), T.PART_CLEARANCE  # grown by the clearance the auto search keeps to parts
        return (area[2] - w - 2 * b - c, area[3] - d - 2 * b - c, area[2], area[3]), base

    @staticmethod
    def _tower_request(
        pspec, pi: int, multi: bool, parts: list[PlannedPart], base: T.TowerSettings | None, facts: dict, spec: Spec
    ) -> T.TowerRequest | None:
        """The plate's tower question after packing (None: the plate needs no tower). Its obstacle test is
        `tower.FootprintClearance` over the placed parts' silhouettes and spreads when they have outlines, else the
        box test."""
        if not multi:
            return None
        bed = facts["bed"]
        bed_r = (bed["x"][0], bed["y"][0], bed["x"][1], bed["y"][1])
        area = T.shared_reach(facts["extruders"], bed, spec.aux_x_limit)
        height = max((p.placed.info.size[2] for p in parts), default=0.0)
        on_bed = [p.placed for p in parts if p.placed.placed]
        obstacles = tuple((p.key, p.rect()) for p in on_bed)
        # The silhouettes when any part has one (a part known by its box only keeps the box test inside it)
        test = T.FootprintClearance.of(on_bed) if any(p.footprint is not None for p in on_bed) else None
        wt = pspec.wipe_tower
        if base is None:
            return T.TowerRequest(pi, "off", T.TowerSettings(), height, obstacles, area, bed_r)
        mode = "fixed" if wt.mode == "fixed" and wt.x is not None and wt.y is not None else "auto"
        return T.TowerRequest(pi, mode, base, height, obstacles, area, bed_r, wt.x, wt.y, clearance=test)

    @staticmethod
    def _plan_towers(plates: list[PlannedPlate], requests: list[T.TowerRequest | None], spec: Spec) -> None:
        """Every plate's tower with one project-wide size (tower.plan_project); each plate gets its TowerClear check."""
        asked = [r for r in requests if r is not None]
        if not asked:
            return
        # A tower size the user set in process.set stays as it is: no fallback to smaller widths
        fallback = not any(k in spec.process.set for k in ("prime_tower_width", "prime_tower_rib_wall", "prime_tower_brim_width"))
        by_plate = {t.plate: t for t in T.plan_project(asked, fallback=fallback)}
        for pi, plate in enumerate(plates, 1):
            plate.tower = by_plate.get(pi)
            if plate.tower is not None and plate.tower.mode != "off":
                plate.checks += list(T.TowerClear(plate.tower).check([], None))

    @staticmethod
    def _tower_globals(plates: list[PlannedPlate], process: dict) -> list[str]:
        """Width, rib wall, brim and enable_prime_tower are one value per project in Studio: write the chosen tower's
        values into the process (they reach process.json and the file), or report plates that disagree."""
        towers = [p.tower for p in plates if p.tower is not None]
        chosen = [(t.plate, t.footprint.settings) for t in towers if t.footprint is not None]
        sizes = {(st.width, st.rib_wall, st.brim) for _, st in chosen}
        problems = []
        if len(sizes) > 1:
            problems.append(
                "prime tower size is per project: the plates need different towers ("
                + "; ".join(f"plate {n}: {st.describe()}" for n, st in chosen)
                + "). Give every plate the same wipe_tower width, rib_wall and brim"
            )
        off = [t.plate for t in towers if t.mode == "off"]
        on = [t.plate for t in towers if t.mode != "off"]
        if off and on:
            problems.append(
                f"prime tower on/off is per project (enable_prime_tower): plates {off} set wipe_tower false, plates {on} need a tower"
            )
        if len(sizes) == 1:
            st = chosen[0][1]
            process["prime_tower_width"] = f"{st.width:g}"
            process["prime_tower_rib_wall"] = "1" if st.rib_wall else "0"
            process["prime_tower_brim_width"] = f"{st.brim:g}"
            process["enable_prime_tower"] = "1"
        elif off and not on:
            process["enable_prime_tower"] = "0"
        return problems

    def _measure(
        self,
        part_specs: Sequence[PartSpec],
        plate_no: int,
        process: dict,
        problems: list[str],
        n_filaments: int,
        used_keys: set[str],
        workdir: Path | None = None,
        orient_settings: list[Path] | None = None,
        probe: SpreadProbe | None = None,
        warnings: list[str] | None = None,
    ) -> list[PlannedPart]:
        out: list[PlannedPart] = []
        for si, ps in enumerate(part_specs, 1):
            where = f"plate {plate_no} part {si}"
            files = [(f.path.expanduser().resolve(), f.filament, f.subtype) for f in ps.files()]
            o = ps.orientation
            orient_res: OrientResult | None = None
            auto_face: tuple[float, float, float] | None = None
            if not o.is_identity and all(fp.is_file() for fp, _, _ in files):
                # The CLI cannot turn one object (reference 01), so the orientation is baked into copies of the STLs,
                # all in one shared frame so the sub-parts of a merged object keep their alignment.
                odir = (workdir or files[0][0].parent) / "oriented"
                srcs = [fp for fp, _, _ in files]
                try:
                    if o.auto and len(srcs) == 1:
                        # Pass 1: Studio's auto-orient on the bare STL (--convert-unit first), exported with the turn
                        # baked in. The export (pass 2) loads that file and never passes --orient.
                        orient_res = self.studio.orient(
                            srcs, odir / f"{srcs[0].stem}__auto-{M._orientation_hash(srcs, o)}", settings=orient_settings
                        )
                        turned = orient_res.files
                    elif o.auto:
                        # Studio turns every positional input on its own; a merged part must keep one frame, so it
                        # takes shambu's own best candidate over all its files.
                        facets = [f for p in srcs for f in M.read_stl(p)]
                        cands = M.candidates(facets)
                        auto_face = next((c for c in cands if c.metrics.stands), cands[0]).normal
                        turned = M.oriented_copies(srcs, M.Orientation(lay_on_face=auto_face), odir)
                    else:
                        turned = M.oriented_copies(srcs, o, odir)
                    files = [(t, fil, sub) for t, (_, fil, sub) in zip(turned, files, strict=True)]
                    stands = M.metrics([f for t in turned for f in M.read_stl(t)]).stands
                    if not stands:
                        problems.append(
                            f"{where} ({ps.key()}): the orientation leaves no flat face on the bed (the part would stand on an edge); "
                            + (
                                "Studio's auto-orient found nothing better: add a flat base in CAD or pick a lay_on_face from `shambu orient`"
                                if o.auto
                                else "pick a lay_on_face from `shambu orient`"
                            )
                        )
                except BambuError as e:
                    problems.append(f"{where}: cannot orient {ps.key()}: {e}")
            measured: dict[Path, MeshInfo | None] = {}
            for fp, _, _ in files:  # one measurement per path; success is tracked per file reference
                if fp in measured:
                    continue
                measured[fp] = None
                if not fp.is_file():
                    problems.append(f"{where}: {fp} does not exist")
                    continue
                try:
                    measured[fp] = self.studio.info(fp)
                except BambuError as e:
                    problems.append(str(e))
            infos = [measured[fp] for fp, _, _ in files]
            ok_infos = [i for i in infos if i is not None]
            if len(ok_infos) != len(files):
                continue
            try:
                eff = effective_filaments(ps, files, process)
            except BuildError as e:
                problems.append(f"{where} ({ps.key()}): {e}")
                continue
            file_fils = {f for _, f, _ in files}  # spec.validate already reports these when out of range
            bad = sorted(f for f in eff if not 1 <= f <= n_filaments and f not in file_fils)
            if bad:
                problems.append(
                    f"{where} ({ps.key()}): filament slot(s) {bad} from overrides do not exist; the spec loads filaments 1..{n_filaments}"
                )
            main = files[0][1] if not eff or files[0][1] in eff else min(eff)
            body = L.union_info(ok_infos)  # merged sub-parts share one translation: pack their union bbox
            eff_settings = {**process, **ps.set}
            margin, spread, mask = self._margins(ps, eff_settings, body, files, main, probe, warnings, where)
            footprint, why_box = self._footprint(files, body)
            if why_box and warnings is not None:
                warnings.append(f"{where} ({ps.key()}): the layout uses the bounding box, not the outline: {why_box}")
            try:
                iface_spread = ps.brim_margin if ps.brim_margin is not None else L.brim_margin(eff_settings) + L.INTERFACE_SPREAD
                iface = interface_uses(ps, process, iface_spread, files)
            except BuildError as e:
                problems.append(f"{where} ({ps.key()}): {e}")
                continue
            for copy in range(ps.count):
                key = _unique_key(ps.key(), used_keys)
                placed = L.Placed(
                    key,
                    body,
                    main,
                    brim=margin,
                    also_filaments=frozenset(eff - {main}),
                    spread=spread,
                    footprint=footprint,
                    support_mask=mask,
                    interface=iface,
                )
                if ps.at is not None and copy == 0:
                    placed.x0, placed.y0, placed.fixed = float(ps.at[0]), float(ps.at[1]), True
                out.append(PlannedPart(ps, key, files, ok_infos, placed, orient=orient_res, auto_face=auto_face))
        return out

    @staticmethod
    def _margins(
        ps: PartSpec,
        eff_settings: dict,
        body: MeshInfo,
        files: list[tuple[Path, int, str]],
        main: int,
        probe: SpreadProbe | None,
        warnings: list[str] | None,
        where: str,
    ) -> tuple[float, L.Spread, L.Footprint | None]:
        """Brim, support spread and (with a probe) the measured support mask of one part (reference 08 section 11).
        The spec's `brim_margin` replaces brim and support on every side. The spec's `support_spread` [mx, px, my, py]
        replaces the support part. A probe (a solo slice, single-file supported parts only) replaces the estimate and
        brings the mask the pair rule uses. Otherwise `layout.support_spread` estimates it."""
        if ps.brim_margin is not None:
            return 0.0, L.spec_spread(ps.brim_margin), None
        brim = L.brim_margin(eff_settings)
        if ps.support_spread is not None:
            mx, px, my, py = ps.support_spread
            return brim, L.Spread(mx, px, my, py, "spec", L.clipped_at_bed(eff_settings), "spec support_spread"), None
        measured: L.Spread | None = None
        mask: L.Footprint | None = None
        if probe is not None and L.needs_support(eff_settings) and len(files) == 1:
            try:
                tag = ps.orientation.tag() if not ps.orientation.is_identity else "as-is"
                res = probe.measure(ProbeRequest(ps.key(), files[0][0], body, main, dict(ps.set), tag))
                measured, mask = res.spread, res.mask
            except BambuError as e:
                if warnings is not None:
                    warnings.append(f"{where} ({ps.key()}): support probe failed ({e}); the estimate applies")
        return brim, L.support_spread(eff_settings, body.size[2], measured), mask

    @staticmethod
    def _footprint(files: list[tuple[Path, int, str]], body: MeshInfo) -> tuple[L.Footprint | None, str]:
        """The XY silhouette of the part's printing bodies in the frame the CLI loads (after the orientation), as a
        RasterFootprint, and "". None (the bounding box) plus the reason when an STL cannot be read or its box differs from Studio's by more than 0.5 mm (a unit conversion, a 3MF input): the box is then the
        safe outline, and the caller warns."""
        facets: list[M.Facet] = []
        for fp, _, sub in files:
            if sub != "normal_part":
                continue
            try:
                facets += M.read_stl(fp)
            except BambuError as e:
                return None, f"{fp.name} cannot be read ({e})"
        # an STL without facets (a test stand-in): Studio's --info box is all there is, no warning
        return L.outline_of(facets, (*body.min[:2], *body.max[:2]))

    @staticmethod
    def _placement_checks(packed: L.Placement, free: list[L.Placed], placer: L.Placer) -> list[L.Check]:
        """The PLACEMENT NOT DONE lines (fix 9 of the plate-14 post-mortem): one per part the placer could not place,
        with its arithmetic, and one naming which parts went to which plate when they need more than one. Parts past
        plate 1 are marked not placed, so no rule checks them at positions on a virtual plate."""
        out = []
        for key, why in packed.unplaced.items():
            out.append(
                L.Check(
                    False,
                    f"{why}. This is the packer's answer (a heuristic, no rotation): Studio's arrange or a hand layout can "
                    f"differ. No position was computed for {key}, so no overlap, bed-edge or reach check ran for it. "
                    f"Give {key} an `at` [x, y] (min corner), a `support_spread` or its own plate.",
                    "placement",
                )
            )
        if len(packed) > 1:
            for n, plate in enumerate(packed.plates[1:], 2):
                for p in plate:
                    p.placed, p.why_not = False, f"would need plate {n}"
            split = "; ".join(
                f"plate {n}: " + (", ".join(p.key for p in plate) or "the fixed parts only") for n, plate in enumerate(packed.plates, 1)
            )
            kind = "row packer, no rotation" if isinstance(placer, L.ShelfPacker) else "bottom-left packer, no rotation"
            out.append(
                L.Check(
                    False,
                    f"the {kind} put the parts on {len(packed)} plates with their support spreads ({split}). That is the "
                    f"packer's answer (a heuristic), not a proof: Studio's arrange or a hand layout (`at`) can fit more. "
                    f"Move the parts of plate 2 and later to another plate in the spec, or place them by hand.",
                    "placement",
                )
            )
        return out

    @staticmethod
    def _group_by_filament(
        parts: list[PlannedPart], layout: LayoutSpec, bed: dict, ctx: L.Context, used_keys: set[str]
    ) -> tuple[list[PlannedPart], list[str]]:
        """Merge single-file parts without `at` that share the main filament, the settings and the height ranges into
        one object per cluster (print-by-object then prints the cluster together, by layer, with no nozzle change).
        The members pack locally with the layout gap inside the area every extruder of the cluster reaches, minus the
        layout edges (and aux_x_limit for extruder 2), so the plate packer can place the cluster where those nozzles
        reach; the cluster is one `Placed` (union bbox from (0, 0, 0)). A cluster of one stays as it is. Returns the
        new part list and notes for the warnings."""
        clusters: dict[tuple, list[PlannedPart]] = {}
        order: list[PlannedPart | tuple] = []
        for p in parts:
            if p.assembled or p.spec.at is not None:
                order.append(p)
                continue
            ck = (
                p.placed.filament,
                p.placed.also_filaments,
                tuple(sorted(p.spec.set.items())),
                tuple((r.min_z, r.max_z, tuple(sorted(r.set.items()))) for r in p.spec.height_ranges),
            )
            if ck not in clusters:
                clusters[ck] = []
                order.append(ck)
            clusters[ck].append(p)
        per_fil = Counter(ck[0] for ck, ms in clusters.items() if len(ms) > 1)
        seen: Counter[int] = Counter()
        out: list[PlannedPart] = []
        notes: list[str] = []
        for o in order:
            if isinstance(o, PlannedPart):
                out.append(o)
                continue
            members = clusters[o]
            if len(members) == 1:
                out += members
                continue
            fil = members[0].placed.filament
            seen[fil] += 1
            base = f"group-f{fil}" if per_fil[fil] == 1 else f"group-f{fil}-{seen[fil]}"
            exts = [ctx.extruder_of(f) for f in sorted(members[0].placed.filaments)]
            x_lo = max([bed["x"][0] + layout.edge] + [L.extruder_x(e, ctx.aux_x_limit)[0] for e in exts])
            x_hi = min([bed["x"][1] - layout.edge] + [L.extruder_x(e, ctx.aux_x_limit)[1] for e in exts])
            y_lo = max([bed["y"][0] + layout.edge] + [e["y"][0] for e in exts])
            y_hi = min([bed["y"][1] - layout.edge] + [e["y"][1] for e in exts])
            local = L.ShelfPacker((0.0, x_hi - x_lo), (0.0, y_hi - y_lo), layout.gap)
            why = f"its {len(members)} parts do not fit together in the reach of extruder(s) {sorted({e['index'] for e in exts})}"
            got = local.place([m.placed for m in members])
            fits = len(got) == 1 and not got.unplaced
            if got.unplaced:
                why = next(iter(got.unplaced.values()))
            for m in members:  # a member the local packer left out is still a part to place on the plate
                m.placed.placed, m.placed.why_not = True, ""
            if fits:  # the cluster's rect grows by the largest member margin on every side: it must still fit
                gm = max(m.placed.keep_out for m in members)
                gw = max(m.placed.x1 for m in members) - min(m.placed.x0 for m in members) + 2 * gm
                gh = max(m.placed.y1 for m in members) - min(m.placed.y0 for m in members) + 2 * gm
                fits = gw <= x_hi - x_lo and gh <= y_hi - y_lo
            if not fits:
                notes.append(f"{base} not merged: {why}; the parts print as separate objects")
                out += members
                continue
            x0, y0 = min(m.placed.x0 for m in members), min(m.placed.y0 for m in members)
            x1, y1 = max(m.placed.x1 for m in members), max(m.placed.y1 for m in members)
            size = (x1 - x0, y1 - y0, max(m.placed.info.size[2] for m in members))
            info = MeshInfo(
                members[0].infos[0].path,
                size,
                (0.0, 0.0, 0.0),
                size,
                all(m.placed.info.manifold for m in members),
                sum(m.placed.info.facets for m in members),
                1,
                sum(m.placed.info.volume_mm3 for m in members),
            )
            key = _unique_key(base, used_keys)
            first = members[0]
            files = [(m.files[0][0], fil, m.files[0][2]) for m in members]
            gspec = PartSpec(
                None,
                tuple(FileRef(fp, f, sub) for fp, f, sub in files),
                key,
                fil,
                1,
                "normal_part",
                first.spec.recipes,
                dict(first.spec.set),
                first.spec.height_ranges,
            )
            iface: dict[int, tuple[float, str]] = {}
            for m in members:
                for slot, v in m.placed.interface.items():
                    if slot not in iface or v[0] > iface[slot][0]:
                        iface[slot] = v
            normal = {f for m in members for f in m.placed.filaments if f not in m.placed.interface}
            placed = L.Placed(
                key,
                info,
                fil,
                brim=max(m.placed.brim for m in members),
                also_filaments=frozenset().union(*(m.placed.also_filaments for m in members)) - {fil},
                interface={s: v for s, v in iface.items() if s not in normal},  # a slot any member prints with: full spread
                spread=L.group_spread([m.placed for m in members]),  # per side, past the group box
                # the members' own outlines, not the group's box: a part beside a concave member keeps its room
                footprint=L.union_footprint([m.placed.shape() for m in members], (x0, y0)),
                support_mask=L.group_mask([m.placed for m in members], (x0, y0)),
            )
            offsets = [(m.placed.x0 - x0, m.placed.y0 - y0) for m in members]
            out.append(PlannedPart(gspec, key, files, [m.infos[0] for m in members], placed, members=members, offsets=offsets))
        return out, notes

    @staticmethod
    def _subpart_checks(parts: list[PlannedPart], ctx: L.Context) -> list[L.Check]:
        """Per sub-part of a merged object: manifold, and the x reach of the sub-part's own filament. A filament group's
        members get the mesh checks (manifold, metres, bodies) by name; the group's own rect covers their reach."""
        out = []
        for p in parts:
            if p.grouped:
                out += list(L.MeshSane().check([m.placed for m in p.members], ctx))
                continue
            if not p.assembled:
                continue
            pos = p.placed.pos()
            for n, ((fp, fil, _), i) in enumerate(zip(p.files, p.infos, strict=True)):
                if not i.manifold:
                    out.append(L.Check(False, f"{p.key}: sub-part {fp.name} is not manifold"))
                if n == 0 or not ctx.known(fil):
                    continue
                e = ctx.extruder_of(fil)
                x0, x1 = i.min[0] + pos[0], i.max[0] + pos[0]
                lo, hi = L.extruder_x(e, ctx.aux_x_limit)
                if x0 < lo or x1 > hi:
                    out.append(
                        L.Check(
                            False,
                            f"{p.key}: sub-part {fp.name} on filament {fil} spans x {x0:.1f}..{x1:.1f}, outside extruder {e['index']} reach",
                        )
                    )
        return out

    @staticmethod
    def _name_objects(parts: list[PlannedPart]) -> list[str]:
        """The CLI names objects `<stem>_<n>` per Item and `assemble_<k>`; two spec entries for one stem both give
        `<stem>_1`. A rename by name cannot tell such objects apart, so they keep the CLI name (with a warning).
        An object whose target key is such an ambiguous name keeps its CLI name too."""
        counts = Counter(p.cli_name for p in parts)
        stay = {n for n, c in counts.items() if c > 1}
        warnings = []
        for name in sorted(stay):
            keys = [p.key for p in parts if p.cli_name == name]
            warnings.append(
                f"objects {keys} all get the CLI name '{name}' (same file stem); they keep that name in the project "
                f"because a rename by name cannot tell them apart. Give the files distinct names to name them."
            )
        for p in parts:
            if p.cli_name in stay:
                p.rename = False
            elif p.key in stay:
                p.rename = False
                warnings.append(f"'{p.key}' keeps the CLI name '{p.cli_name}': its name '{p.key}' is the CLI name of other objects")
        return warnings

    @staticmethod
    def _plate_params(spec: Spec, fmap: list[int], map_applies: bool, print_sequence: str | None = None) -> dict[str, str]:
        params = {"curr_bed_type": spec.bed_type}
        if print_sequence:  # written whenever the plate or the process says anything but the default
            params["print_sequence"] = print_sequence
        if map_applies:
            params.update({"filament_map_mode": "Manual", "filament_map": ",".join(str(v) for v in fmap)})
        return params

    def write_inputs(self, plan: Plan) -> dict[str, Path]:
        w = plan.workdir
        paths = {"machine": P.write(plan.machine, w / "machine.json"), "process": P.write(plan.process, w / "process.json")}
        first = plan.spec.plates[0] if plan.spec.plates else None
        if first and (first.pause_at_z or first.colour_changes):
            # --load-custom-gcodes reaches plate 1 only (CustomGCode::Info JSON); other plates are patched after export
            gcodes = [{"type": "PausePrint", "print_z": z, "color": "", "extruder": 1, "extra": ""} for z in first.pause_at_z]
            gcodes += [
                {"type": "ColorChange", "print_z": c.z, "color": c.colour, "extruder": c.filament, "extra": ""}
                for c in first.colour_changes
            ]
            paths["custom_gcodes"] = w / "custom_gcodes.json"
            paths["custom_gcodes"].write_text(
                json.dumps({"mode": "MultiExtruder" if len(plan.filaments) > 1 else "SingleExtruder", "gcodes": gcodes}, indent=1)
            )
        for i, f in enumerate(plan.filaments, 1):
            paths[f"filament{i}"] = P.write(f, w / f"filament-{i}.json")
        paths["assemble"] = plan.assemble.write(w / "assemble.json")
        paths["spec"] = Path(w / "spec.json")
        paths["spec"].write_text(json.dumps(plan.spec.to_json(), indent=1), encoding="utf-8")
        return paths

    def command(
        self,
        plan: Plan,
        paths: dict[str, Path],
        name: str,
        do_slice: bool = False,
        no_check: bool = False,
        plate: int = 0,
        outdir: Path | None = None,
    ) -> CliCommand:
        cmd = (
            CliCommand()
            .load_settings(paths["machine"], paths["process"])
            .load_filaments(*[paths[f"filament{i}"] for i in range(1, len(plan.filaments) + 1)])
            .load_assemble_list(paths["assemble"])
            .setting("curr_bed_type", plan.spec.bed_type)
            .setting("nozzle_volume_type", ["Standard"] * len(plan.facts["extruders"]))
            .setting("filament_colour", ";".join(f["filament_colour"][0] for f in plan.filaments))
            .flag("allow-rotations", False)
        )
        if plan.map_applies:
            cmd.setting("filament_map_mode", "Manual").setting("filament_map", plan.filament_map)
        if len(plan.filaments) > 1 or any(p.tower is not None and p.tower.footprint is not None for p in plan.plates):
            # (a one-filament plate gets a tower too with Smooth timelapse.) wipe_tower_x/y are per-plate vectors: a scalar moves the tower on plate 1 only, and every later plate keeps
            # the CLI default (15, 220), inside the right nozzle's dead strip (exit -102) or under a part (exit -101).
            # A plate with a planned tower gets its spot; any other plate keeps WIPE_TOWER_XY.
            spots = [p.tower.footprint if p.tower is not None else None for p in plan.plates]
            xs = [float(fp.x) if fp else WIPE_TOWER_XY[0] for fp in spots]
            ys = [float(fp.y) if fp else WIPE_TOWER_XY[1] for fp in spots]
            cmd.setting("wipe_tower_x", xs).setting("wipe_tower_y", ys)
        if do_slice:
            if "custom_gcodes" in paths:
                cmd.option("load-custom-gcodes", str(paths["custom_gcodes"]))
            cmd.slice(plate, no_check=no_check, max_seconds=600)
        return cmd.export_3mf(f"{name}.gcode.3mf" if do_slice else f"{name}.3mf", outdir or plan.outdir)

    def export(
        self, plan: Plan, paths: dict[str, Path], project: Path, timeout: int, do_slice: bool = False, no_check: bool = False
    ) -> RunResult:
        project.unlink(missing_ok=True)
        self.emit("export", project=str(project), slice=do_slice)
        t0 = time.time()
        if do_slice:
            rr, self.checks_skipped = two_pass_slice(
                self.studio,
                lambda nc, plate, d: self.command(plan, paths, plan.spec.name, do_slice, no_check=nc, plate=plate, outdir=d),
                name=plan.spec.name,
                outputdir=plan.outdir,
                timeout=timeout,
                expect_output=project,
                no_check=no_check,
                plates=tuple(range(1, len(plan.plates) + 1)) if len(plan.plates) > 1 else (0,),
                known={x.cli_name for x in plan.parts if x.cli_name},
            )
        else:
            rr = self.studio.run(
                self.command(plan, paths, plan.spec.name, do_slice),
                name=plan.spec.name,
                outputdir=plan.outdir,
                timeout=timeout,
                expect_output=project,
            )
        self.emit("exported", ok=rr.ok, seconds=round(time.time() - t0, 1), summary=rr.summary())
        return rr

    def patch_set(self, plan: Plan, sliced: bool = False) -> PatchSet:
        """Sliced: only patches that keep the G-code valid (names, preset bookkeeping). Unsliced: everything the CLI left
        wrong. Both: every preset slot named by its system base with its diff list (`SetPresetBookkeeping`), also a slot
        with no keys, so a user-named preset still gets the system id Studio can find."""
        ps = PatchSet()
        if not sliced:
            ps.add(SetBedTypeEverywhere(plan.spec.bed_type))
            ps.add(SetFilamentColours(tuple(f["filament_colour"][0] for f in plan.filaments)))
            # A CLI export without --slice leaves extruder_nozzle_stats [] and every later CLI slice exits -100. The
            # preset-aware variant repair runs first, so the map patch finds the blocks consistent.
            ps.add(RepairNozzleStats(tuple(tuple(f.get("filament_extruder_variant") or ()) for f in plan.filaments)))
            if plan.map_applies:
                ps.add(SetFilamentMap(tuple(plan.filament_map)))
        for old, new in _rename_steps([(p.cli_name, p.key) for p in plan.parts if p.rename and p.cli_name and p.cli_name != p.key]):
            ps.add(RenameObject(old, new))
        for i, pspec in enumerate(plan.spec.plates, 1):
            if sliced and i == 1:
                continue  # plate 1 got them through --load-custom-gcodes before slicing
            for z in pspec.pause_at_z:
                ps.add(AddCustomGcode(i, z, "pause"))
            for cc in pspec.colour_changes:
                ps.add(AddCustomGcode(i, cc.z, "color_change", extruder=cc.filament, colour=cc.colour))
        for slot, (_, base, keys) in enumerate(plan.bookkeeping or _bookkeeping(plan, self.resolver)):
            ps.add(SetPresetBookkeeping(slot, base, tuple(keys)))
        return ps

    def patch(self, plan: Plan, project: Path, sliced: bool = False) -> list[str]:
        ps = self.patch_set(plan, sliced)
        if Project(str(project)).n_filaments() != len(plan.filaments):
            # The slot numbers follow the plan's filament count; verify() reports the count that differs.
            ps.patches = [x for x in ps.patches if not isinstance(x, SetPresetBookkeeping)]
        done = ps.apply(str(project))
        self.emit("patched", patches=done)
        return done

    def verify(self, plan: Plan, project: Path, sliced: bool = False) -> list[str]:
        probs = []
        pr = Project(str(project))
        s = pr.summary()
        ps = pr.project_settings
        if sliced:
            probs += sliced_problems(pr, s, range(1, len(plan.plates) + 1))
        cols = [f["filament_colour"][0].upper() for f in plan.filaments]
        if [c.upper() for c in ps.get("filament_colour", [])] != cols:
            probs.append(f"filament colours in file {ps.get('filament_colour')} differ from {cols}")
        book = plan.bookkeeping or _bookkeeping(plan, self.resolver)
        names = [base for kind, base, _ in book if kind == "filament"]
        if list(ps.get("filament_settings_id", [])) != names:
            probs.append(f"filament presets in file are {ps.get('filament_settings_id')}, expected the system bases {names}")
        want_map = [str(v) for v in plan.filament_map]
        if plan.map_applies:
            if [str(v) for v in ps.get("filament_map", [])] != want_map:
                probs.append(f"filament_map in file is {ps.get('filament_map')}, expected {plan.filament_map}")
            if ps.get("filament_map_mode") != "Manual":
                probs.append(f"filament_map_mode in file is {ps.get('filament_map_mode')!r}, expected 'Manual'")
        if s["bed_type"] != plan.spec.bed_type:
            probs.append(f"bed type in file is {s['bed_type']}")
        for pl in s["plates"]:
            bt = pl.get("bed_type")
            if (bt is None and not sliced) or (bt is not None and bt != plan.spec.bed_type):
                probs.append(f"plate {pl['id']} bed type is {bt!r}, expected {plan.spec.bed_type!r}")
            if plan.map_applies and pl.get("filament_maps") is not None and pl["filament_maps"].split() != want_map:
                probs.append(f"plate {pl['id']} filament_maps is {pl['filament_maps']!r}, expected {' '.join(want_map)!r}")
            if plan.map_applies and pl.get("filament_map_mode") not in (None, "Manual"):
                probs.append(f"plate {pl['id']} filament_map_mode is {pl['filament_map_mode']!r}, expected 'Manual'")
            want_seq = next((p.print_sequence for i, p in enumerate(plan.plates, 1) if i == pl["id"]), "by layer")
            got_seq = pl.get("print_sequence") or str(ps.get("print_sequence") or "by layer")
            if want_seq != got_seq:
                probs.append(f"plate {pl['id']} print_sequence is {got_seq!r}, expected {want_seq!r}")
        probs += [f"not re-sliceable by the CLI: {x}" for x in pr.reslice_problems(len(plan.facts["extruders"]))]
        probs += self._verify_tower(plan, ps)
        probs += self._verify_process_bookkeeping(plan, pr, book)
        if len(s["filaments"]) != len(plan.filaments):
            probs.append(f"{len(s['filaments'])} filaments in file, expected {len(plan.filaments)}")
        if len(s["plates"]) != len(plan.spec.plates):
            probs.append(f"{len(s['plates'])} plates in file, expected {len(plan.spec.plates)}")
        want = sum(p.count for pl in plan.spec.plates for p in pl.parts)  # the spec, not a plan that may have dropped parts
        want -= sum(len(x.members) - 1 for x in plan.parts if x.grouped)  # a filament group is one object
        if len(s["objects"]) != want:
            probs.append(f"{len(s['objects'])} objects in file, expected {want}")
        have = Counter(o["name"] for o in s["objects"])
        for name, n in Counter(x.file_name for x in plan.parts).items():
            if have[name] < n:
                probs.append(f"object '{name}' not found in the file" + (f" {n} times" if n > 1 else "") + f" (names: {sorted(have)})")
        try:
            self.studio.info(project)  # the CLI loads the patched file: a cheap structural validation
        except BambuError as e:
            probs.append(f"Bambu Studio cannot load the patched project: {e}")
        for k in ("machine_start_gcode", "machine_end_gcode", "change_filament_gcode"):
            got, exp = str(ps.get(k, "")), str(plan.machine.get(k, ""))
            if got != exp:
                probs.append(f"{k} in file ({len(got)} chars) differs from the resolved machine preset ({len(exp)} chars)")
        for k, v in plan.spec.process.set.items():
            v = plan.process.get(k, v) if k in TOWER_KEYS else v  # tower planning may override the spec value
            if k not in ps:
                probs.append(f"process setting {k} is missing from the file, spec says {v!r}")
            elif str(ps[k]) != str(v):
                probs.append(f"process setting {k} is {ps[k]!r} in the file, spec says {v!r}")
        self.emit("verified", problems=probs)
        return probs

    @staticmethod
    def _verify_process_bookkeeping(plan: Plan, pr: Project, book: list[tuple[str, str, list[str]]]) -> list[str]:
        """The process slot as Studio reads it on open (spec 2026-10-29 section 1): `print_settings_id` = the base,
        `inherits_group[0]` = "", every key of the plan's list in `different_settings_to_system[0]` with the plan's
        value in the file. Keys the CLI listed on its own are allowed; the tower keys are `_verify_tower`'s."""
        _, base, keys = book[0]
        slot = pr.preset_bookkeeping()[0]
        ps = pr.project_settings
        probs = []
        if slot["id"] != base:
            probs.append(f"process in file is {slot['id']}, expected the system base {base} + {len(keys)} overrides ({';'.join(keys)})")
        if slot["inherits"] != "":
            probs.append(f"inherits_group[0] in file is {slot['inherits']!r}, expected '' (process {base} is a system preset)")
        missing = [k for k in keys if k not in slot["keys"]]
        if missing:
            probs.append(
                f"different_settings_to_system[0] lacks {';'.join(missing)}: Studio resets them to {base} on open "
                f"(file lists {';'.join(slot['keys']) or 'nothing'}, expected {';'.join(keys)})"
            )
        for k in keys:
            if k in TOWER_KEYS:
                continue
            if k not in ps:
                probs.append(f"process setting {k} is listed for {base} but missing from the file, plan says {plan.process.get(k)!r}")
            elif _as_text(ps[k]) != _as_text(plan.process.get(k, "")):
                probs.append(f"process setting {k} is {ps[k]!r} in the file, plan says {plan.process.get(k)!r} (listed for {base})")
        return probs

    @staticmethod
    def _verify_tower(plan: Plan, ps: dict) -> list[str]:
        """The strict tower verify of a build (`verify_tower`): the keys tower planning wrote and each planned spot."""
        if not any(p.tower is not None for p in plan.plates):
            return []
        wrote = {k: plan.process[k] for k in TOWER_KEYS if k in plan.process}
        spots = {i: (p.tower.footprint.x, p.tower.footprint.y) for i, p in enumerate(plan.plates) if p.tower and p.tower.footprint}
        return verify_tower(ps, len(plan.plates), wrote, spots, "the tower plan")

    def report(self, r: BuildResult) -> str:
        from .report import render

        return render(r)


def load_spec(path: str | Path) -> Spec:
    try:
        return Spec.from_json(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as e:
        raise BuildError(f"cannot read spec {path}: {e}") from e
