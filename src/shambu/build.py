"""From a plate spec to a checked Bambu Studio project (Template Method pipeline).

A spec is one JSON document the calling agent writes (see `spec.py`). The pipeline runs the fixed
sequence plan -> write inputs -> export -> patch -> verify -> report. Each step is a method, so a
subclass can replace one (a dry run skips export; a test injects a fake StudioCLI). Listeners
(Observer) get an event per step for progress output.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
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
)
from .spec import FileRef, LayoutSpec, PartSpec, Spec
from .studio import CliCommand, MeshInfo, OrientResult, RunResult, SlicedPlate, StudioCLI
from .threemf import Project

log = logging.getLogger(__name__)
Listener = Callable[[str, dict], None]
DEFAULT_COLOURS = ("#1A1A1A", "#F0F0F0", "#00AE42", "#0A2989")  # used when the spec gives none; slicing needs one per filament
WIPE_TOWER_XY = (208.0, 208.0)  # plates without a tower of their own: the CLI default (15, 220) runs off the bed (-104)
# Object/region keys that choose a filament slot. 0 means "the object's own filament". The CLI applies print_params
# after `filaments`, so `extruder` there overrides the file's filament (01-source-cli.md section 2.4 step 3).
FILAMENT_KEYS = (
    "extruder",
    "wall_filament",
    "sparse_infill_filament",
    "solid_infill_filament",
    "support_filament",
    "support_interface_filament",
)
PRINTING_SUBTYPES = ("normal_part", "modifier_part")
TOWER_KEYS = (
    "prime_tower_width",
    "prime_tower_rib_wall",
    "prime_tower_brim_width",
    "enable_prime_tower",
)  # set by tower planning  # sub-parts whose filament reaches the nozzle
UNRESOLVED_FILAMENT = "loaded (unresolved)"  # placeholder preset name for a "loaded" slot the printer did not fill
FileRefs = Sequence[tuple[Path, int, str]]


def _slot(value: str, where: str) -> int:
    try:
        return int(str(value).strip())
    except ValueError:
        raise BuildError(f"{where} = {value!r} is not a filament slot number") from None


SUPPORT_FILAMENT_KEYS = ("support_filament", "support_interface_filament")
WALL_INFILL_KEYS = ("wall_filament", "sparse_infill_filament", "solid_infill_filament")


def _support_on(settings: Mapping) -> bool:
    """Studio's "has support material" (support, enforced support layers or a raft): `tower.needs_support`."""
    return T.needs_support(settings)


def effective_filaments(part_spec: PartSpec, files: FileRefs | None = None, process: Mapping | None = None) -> set[int]:
    """Every filament slot an object prints with, after all overrides.

    A single file prints with its `filament` unless `set.extruder` replaces it. A merged object prints with
    the filaments of its normal and modifier sub-parts (plus `set.extruder`, kept as a conservative extra).
    Support, wall and infill filament keys in `set` and every height range's filament keys add their slot.
    With `process` (the resolved process: recipes and process.set included), its wall and infill filament keys
    add their slot for every part, and its support and support-interface filament keys for every part with
    `enable_support` on: the `dual-material-interface` recipe sets support_interface_filament 2 at process level,
    and a checker that missed it passed a plate that exited -102 (reference 06 section 5).
    Raises BuildError for a value that is not an integer. Slots are not range-checked here.
    """
    refs = list(files) if files is not None else [(f.path, f.filament, f.subtype) for f in part_spec.files()]
    ext = _slot(part_spec.set["extruder"], "set.extruder") if "extruder" in part_spec.set else 0
    out: set[int] = set()
    if len(refs) == 1:
        out.add(ext or refs[0][1])
    else:
        out |= {fil for _, fil, sub in refs if sub in PRINTING_SUBTYPES}
        if ext:
            out.add(ext)
    support = _support_on({**(process or {}), **part_spec.set})
    for k in FILAMENT_KEYS[1:]:
        if k in SUPPORT_FILAMENT_KEYS and not support:
            continue  # Studio prints no support (or raft) for this part: its support slots are unused
        if k in part_spec.set and (v := _slot(part_spec.set[k], f"set.{k}")):
            out.add(v)
    for r in part_spec.height_ranges:
        for k in FILAMENT_KEYS:
            if k in r.set and (v := _slot(r.set[k], f"height range {r.min_z}..{r.max_z} {k}")):
                out.add(v)
    if process is not None:
        keys = WALL_INFILL_KEYS + (SUPPORT_FILAMENT_KEYS if support else ())
        for k in keys:
            if k not in part_spec.set and k in process and (v := _slot(process[k], f"process {k}")):
                out.add(v)
    return out


def interface_uses(part_spec: PartSpec, process: Mapping, key: str, brim_margin: float, own: int = 0) -> list[T.InterfaceUse]:
    """The support and support-interface slots a supported part prints with (part `set` first, then the process),
    for `tower.InterfaceReach`. A slot 0 (the part's own filament) and `own` (the part's main slot) are left out.
    Raises BuildError for a value that is not a slot number."""
    if not _support_on({**process, **part_spec.set}):
        return []
    out = []
    for k in SUPPORT_FILAMENT_KEYS:
        src = "part" if k in part_spec.set else "process"
        raw = part_spec.set.get(k, process.get(k, "0"))
        slot = _slot(str(raw).strip() or "0", f"{src} {k}")
        if slot and slot != own:
            out.append(T.InterfaceUse(key, k, slot, brim_margin + T.INTERFACE_SPREAD, src))
    return out


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
    filament_resolution: FilamentResolution | None = None  # how each filament was chosen: the printer, the GUI or the spec

    @property
    def ok(self) -> bool:
        return not self.problems and all(c.ok for p in self.plates for c in p.checks)

    @property
    def unresolved_filaments(self) -> list[int]:
        """Slots still at the `"loaded"` placeholder: nothing to export for them, whatever `--keep-going` says."""
        return [i for i, f in enumerate(self.filaments, 1) if f.get("name") == UNRESOLVED_FILAMENT]

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
        problems = list(self.plan.problems) + ([self.export_problem()] if self.export_problem() else [])
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
                }
                for p in self.plan.plates
            ],
            "cli": self.run.summary() if self.run else None,
            "patches": self.patches,
            "verify_problems": self.verify_problems,
            "inputs": {k: str(v) for k, v in self.inputs.items()},
            "report": str(self.report_path) if self.report_path else None,
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
    ):
        self.studio = studio or StudioCLI()
        self.resolver = resolver or P.resolver()
        self.placer = placer
        self.checker = checker or L.LimitChecker()
        self.listeners = listeners or []

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
    ) -> BuildResult:
        """`filaments` is the spec reconciled with the printer (loaded.reconcile): its spec replaces `spec`, and its
        problems and warnings lead the plan's. Without it a `"loaded"` filament is a problem."""
        if filaments is not None:
            spec = filaments.spec
        self.emit("plan", name=spec.name)
        plan = self.plan(spec, filaments)
        result = BuildResult(plan, plan.outdir / (f"{spec.name}.gcode.3mf" if do_slice else f"{spec.name}.3mf"), sliced=do_slice)
        result.inputs = self.write_inputs(plan)
        self.emit("planned", ok=plan.ok, problems=plan.problems)
        if plan.ok or (keep_going and not plan.unresolved_filaments):  # a placeholder filament can never export
            if export:
                result.run = self.export(plan, result.inputs, result.project, timeout, do_slice)
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
    def plan(self, spec: Spec, resolution: FilamentResolution | None = None) -> Plan:
        outdir = spec.outdir.expanduser().resolve()
        workdir = outdir / "shambu"
        workdir.mkdir(parents=True, exist_ok=True)
        problems: list[str] = list(resolution.problems) if resolution else []
        warnings: list[str] = list(resolution.warnings) if resolution else []
        warnings += [w for w in spec.warnings if w not in warnings]  # unknown keys: `check` and `build` warn as `spec` does
        problems += spec.validate()
        machine = self.resolver.for_cli("machine", spec.printer)
        process = self.resolver.for_cli("process", spec.process.base, spec.process.set, spec.process.name)
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
        placer = self.placer or L.ShelfPacker(
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
        for pi, (pspec, seq) in enumerate(zip(spec.plates, sequences, strict=True), 1):
            parts = self._measure(pspec.parts, pi, process, problems, len(filaments), used_keys, workdir, orient_settings)
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
            try:
                uses = tuple(
                    u
                    for p in parts
                    for u in interface_uses(p.spec, process, p.key, L.brim_margin({**process, **p.spec.set}), p.placed.filament)
                )
            except BuildError as e:
                problems.append(f"plate {pi} '{pspec.name}': {e}")
                uses = ()
            spread = {u.key: u.interface_spread for u in uses}
            for p in parts:  # the interface prints past the part box on every side: pack and check with that margin
                if p.key in spread:
                    p.placed.margin = max(p.placed.margin, spread[p.key])
            fixed = [p for p in parts if p.placed.fixed]
            free = [p.placed for p in parts if not p.placed.fixed]
            # The tower footprint (fixed spot, or a provisional back-right corner for "auto") keeps packed parts out;
            # TowerClear checks the final footprint after every plate is planned.
            reserve, tower_base = self._tower_reserve(
                pspec, pi, multi, parts, bool(free), process, filaments, facts, spec, problems, warnings
            )
            pctx = replace(ctx, by_object=sequential)
            obstacles = [p.rect() for p in (x.placed for x in fixed)] + ([reserve] if reserve else [])
            if free:
                x_min_of = {p.key: max(ctx.extruder_of(f)["x"][0] for f in p.filaments) for p in free}
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
                try:
                    packed = plate_placer.place(free, obstacles, x_min_of)
                except BambuError as e:
                    problems.append(f"plate {pi} '{pspec.name}': {e}")
                    packed = [free]
                overflow = len(packed) > 1
                if overflow:
                    problems.append(
                        f"plate {pi} '{pspec.name}': the parts need {len(packed)} plates; move some to another plate in the spec"
                    )
            else:
                overflow = False
            for p in parts:
                p.place_members()
            requests.append(self._tower_request(pspec, pi, multi, parts, tower_base, facts, spec) if not overflow else None)
            if overflow:  # the overflow parts sit on a second virtual plate: overlap and reach checks would only repeat the problem
                checks = [L.Check(False, f"the parts need {len(packed)} plates (see Problems); no further placement checks")]
            else:
                extra: tuple = (T.InterfaceReach(uses),) if uses else ()  # TowerClear runs after every plate is planned
                checker = (
                    replace(self.checker, rules=self.checker.rules + extra) if isinstance(self.checker, L.LimitChecker) else self.checker
                )
                checks = checker.check([p.placed for p in parts], pctx)
                checks += self._subpart_checks(parts, pctx)
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
                if len(filaments) > 1 and str(settings.get("enable_support", process.get("enable_support", "0"))) == "1":
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
            plates.append(PlannedPlate(plate.name, parts, checks, seq))
        self._plan_towers(plates, requests, spec)
        problems += self._tower_globals(plates, process)
        warnings += self._name_objects([x for p in plates for x in p.parts])
        problems += A.validate(alist.to_json(), known_keys=known, filament_count=len(filaments))
        return Plan(spec, outdir, workdir, machine, process, filaments, facts, fmap, plates, alist, problems, warnings, resolution)

    # ---------------------------------------------------------------- prime tower (tower.py)
    @staticmethod
    def _tower_reserve(
        pspec, pi: int, multi: bool, parts: list[PlannedPart], has_free: bool, process: dict, filaments: list[dict], facts: dict,
        spec: Spec, problems: list[str], warnings: list[str],
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
        """The plate's tower question after packing (None: the plate needs no tower)."""
        if not multi:
            return None
        bed = facts["bed"]
        bed_r = (bed["x"][0], bed["y"][0], bed["x"][1], bed["y"][1])
        area = T.shared_reach(facts["extruders"], bed, spec.aux_x_limit)
        height = max((p.placed.info.size[2] for p in parts), default=0.0)
        obstacles = tuple((p.key, p.placed.rect()) for p in parts)
        wt = pspec.wipe_tower
        if base is None:
            return T.TowerRequest(pi, "off", T.TowerSettings(), height, obstacles, area, bed_r)
        mode = "fixed" if wt.mode == "fixed" and wt.x is not None and wt.y is not None else "auto"
        return T.TowerRequest(pi, mode, base, height, obstacles, area, bed_r, wt.x, wt.y)

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
            margin = (
                ps.brim_margin
                if ps.brim_margin is not None
                else max(L.brim_margin(eff_settings), L.support_margin(eff_settings, body.size[2]))
            )
            for copy in range(ps.count):
                key = _unique_key(ps.key(), used_keys)
                placed = L.Placed(key, body, main, margin=margin, also_filaments=frozenset(eff - {main}))
                if ps.at is not None and copy == 0:
                    placed.x0, placed.y0, placed.fixed = float(ps.at[0]), float(ps.at[1]), True
                out.append(PlannedPart(ps, key, files, ok_infos, placed, orient=orient_res, auto_face=auto_face))
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
            x_lo = max([bed["x"][0] + layout.edge] + [e["x"][0] for e in exts])
            x_hi = min([bed["x"][1] - layout.edge] + [e["x"][1] for e in exts])
            if ctx.aux_x_limit is not None and any(e["index"] == 2 for e in exts):
                x_hi = min(x_hi, ctx.aux_x_limit)
            y_lo = max([bed["y"][0] + layout.edge] + [e["y"][0] for e in exts])
            y_hi = min([bed["y"][1] - layout.edge] + [e["y"][1] for e in exts])
            local = L.ShelfPacker((0.0, x_hi - x_lo), (0.0, y_hi - y_lo), layout.gap)
            why = f"its {len(members)} parts do not fit together in the reach of extruder(s) {sorted({e['index'] for e in exts})}"
            try:
                fits = len(local.place([m.placed for m in members])) == 1
            except BambuError as e:
                fits, why = False, str(e)
            if fits:  # the cluster's rect grows by the largest member margin on every side: it must still fit
                gm = max(m.placed.margin for m in members)
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
            placed = L.Placed(key, info, fil, margin=max(m.placed.margin for m in members), also_filaments=first.placed.also_filaments)
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
                if x0 < e["x"][0] or x1 > e["x"][1] or (e["index"] == 2 and ctx.aux_x_limit and x1 > ctx.aux_x_limit):
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

    def command(self, plan: Plan, paths: dict[str, Path], name: str, do_slice: bool = False) -> CliCommand:
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
        if len(plan.filaments) > 1:
            # wipe_tower_x/y are per-plate vectors: a scalar moves the tower on plate 1 only, and every later plate keeps
            # the CLI default (15, 220), inside the right nozzle's dead strip (exit -102) or under a part (exit -101).
            # A plate with a planned tower gets its spot; any other plate keeps WIPE_TOWER_XY.
            spots = [p.tower.footprint if p.tower is not None else None for p in plan.plates]
            xs = [float(fp.x) if fp else WIPE_TOWER_XY[0] for fp in spots]
            ys = [float(fp.y) if fp else WIPE_TOWER_XY[1] for fp in spots]
            cmd.setting("wipe_tower_x", xs).setting("wipe_tower_y", ys)
        if do_slice:
            if "custom_gcodes" in paths:
                cmd.option("load-custom-gcodes", str(paths["custom_gcodes"]))
            cmd.slice(0, no_check=False, max_seconds=600)
        return cmd.export_3mf(f"{name}.gcode.3mf" if do_slice else f"{name}.3mf", plan.outdir)

    def export(self, plan: Plan, paths: dict[str, Path], project: Path, timeout: int, do_slice: bool = False) -> RunResult:
        project.unlink(missing_ok=True)
        self.emit("export", project=str(project), slice=do_slice)
        t0 = time.time()
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
        """Sliced: only patches that keep the G-code valid (names). Unsliced: everything the CLI left wrong."""
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
        return ps

    def patch(self, plan: Plan, project: Path, sliced: bool = False) -> list[str]:
        done = self.patch_set(plan, sliced).apply(str(project))
        self.emit("patched", patches=done)
        return done

    def verify(self, plan: Plan, project: Path, sliced: bool = False) -> list[str]:
        probs = []
        pr = Project(str(project))
        s = pr.summary()
        ps = pr.project_settings
        if sliced and not s["sliced"]:
            probs.append("the file holds no G-code although --slice was requested")
        if sliced:
            for pl in s["plates"]:
                if not pl.get("slice"):
                    probs.append(f"plate {pl['id']} has no slice results")
                elif pl["slice"].get("outside") == "true":
                    probs.append(f"plate {pl['id']}: the slicer reports G-code outside the printable area")
        cols = [f["filament_colour"][0].upper() for f in plan.filaments]
        if [c.upper() for c in ps.get("filament_colour", [])] != cols:
            probs.append(f"filament colours in file {ps.get('filament_colour')} differ from {cols}")
        names = [f["name"] for f in plan.filaments]
        if list(ps.get("filament_settings_id", [])) != names:
            probs.append(f"filament presets in file are {ps.get('filament_settings_id')}, expected {names}")
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
        if s["process"] != plan.process["name"]:
            probs.append(f"process in file is {s['process']}, expected {plan.process['name']}")
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
    def _verify_tower(plan: Plan, ps: dict) -> list[str]:
        """The tower size and on/off planning wrote into the process, and each planned plate's spot, in the file. A key
        the file does not hold is not checked (every Studio export holds them; minimal test fixtures do not)."""
        probs: list[str] = []
        towers = [p.tower for p in plan.plates if p.tower is not None]
        if not towers:
            return probs
        for k in TOWER_KEYS:
            if k not in plan.process or k not in ps:
                continue
            got, want = ps.get(k), plan.process[k]
            try:
                same = got is not None and float(str(got)) == float(str(want))
            except ValueError:
                same = str(got) == str(want)
            if not same:
                probs.append(f"{k} in file is {got!r}, the tower plan wrote {want!r}")
        for axis in ("x", "y"):
            vec = ps.get(f"wipe_tower_{axis}")
            if vec is None:
                continue
            vals = vec if isinstance(vec, list) else [vec]
            for i, p in enumerate(plan.plates):
                fp = p.tower.footprint if p.tower is not None else None
                if fp is None:
                    continue
                want_v = fp.x if axis == "x" else fp.y
                try:
                    got_v = float(str(vals[i] if i < len(vals) else vals[-1]))
                except (TypeError, ValueError, IndexError):
                    got_v = float("nan")
                if abs(got_v - want_v) > 0.01:
                    probs.append(f"plate {i + 1} wipe_tower_{axis} in file is {vec!r}, the tower plan put it at {want_v:g}")
        return probs

    def report(self, r: BuildResult) -> str:
        from .report import render

        return render(r)


def load_spec(path: str | Path) -> Spec:
    try:
        return Spec.from_json(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as e:
        raise BuildError(f"cannot read spec {path}: {e}") from e
