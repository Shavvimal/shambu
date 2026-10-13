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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import assemble as A
from . import layout as L
from . import mesh as M
from . import presets as P
from . import recipes as R
from .errors import BambuError, BuildError
from .patches import AddCustomGcode, PatchSet, RenameObject, SetBedTypeEverywhere, SetFilamentColours, SetFilamentMap
from .spec import PartSpec, Spec
from .studio import CliCommand, MeshInfo, RunResult, StudioCLI
from .threemf import Project

log = logging.getLogger(__name__)
Listener = Callable[[str, dict], None]
DEFAULT_COLOURS = ("#1A1A1A", "#F0F0F0", "#00AE42", "#0A2989")  # used when the spec gives none; slicing needs one per filament
WIPE_TOWER_XY = (208.0, 208.0)  # the CLI default (15, 220) runs off the bed (return code -104)
WIPE_TOWER_RESERVE: L.Rect = (203.0, 203.0, 256.0, 256.0)  # keep parts out of this corner when a plate uses two filaments
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
PRINTING_SUBTYPES = ("normal_part", "modifier_part")  # sub-parts whose filament reaches the nozzle
FileRefs = Sequence[tuple[Path, int, str]]


def _slot(value: str, where: str) -> int:
    try:
        return int(str(value).strip())
    except ValueError:
        raise BuildError(f"{where} = {value!r} is not a filament slot number") from None


def effective_filaments(part_spec: PartSpec, files: FileRefs | None = None) -> set[int]:
    """Every filament slot an object prints with, after all overrides.

    A single file prints with its `filament` unless `set.extruder` replaces it. A merged object prints with
    the filaments of its normal and modifier sub-parts (plus `set.extruder`, kept as a conservative extra).
    Support, wall and infill filament keys in `set` and every height range's filament keys add their slot.
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
    for k in FILAMENT_KEYS[1:]:
        if k in part_spec.set and (v := _slot(part_spec.set[k], f"set.{k}")):
            out.add(v)
    for r in part_spec.height_ranges:
        for k in FILAMENT_KEYS:
            if k in r.set and (v := _slot(r.set[k], f"height range {r.min_z}..{r.max_z} {k}")):
                out.add(v)
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

    @property
    def assembled(self) -> bool:
        return len(self.files) > 1

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

    @property
    def ok(self) -> bool:
        return not self.problems and all(c.ok for p in self.plates for c in p.checks)

    @property
    def map_applies(self) -> bool:
        """The filament -> extruder map is written (CLI, patch, verify) when the spec gives one or there are 2+ filaments."""
        return bool(self.spec.filament_map) or len(self.filaments) > 1

    @property
    def parts(self) -> list[PlannedPart]:
        return [x for p in self.plates for x in p.parts]


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

    def to_json(self) -> dict:
        return {
            "name": self.plan.spec.name,
            "project": str(self.project),
            "ok": self.ok,
            "plan_ok": self.plan.ok,
            "sliced": self.sliced,
            "estimates": [{"plate": p["id"], **p["slice"]} for p in (self.summary or {}).get("plates", []) if p.get("slice")],
            "problems": self.plan.problems,
            "warnings": self.plan.warnings,
            "plates": [
                {
                    "name": p.name,
                    "print_sequence": p.print_sequence,
                    "layout": L.describe([x.placed for x in p.parts]),
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
        self, spec: Spec, *, export: bool = True, timeout: int = 1200, keep_going: bool = False, do_slice: bool = False
    ) -> BuildResult:
        self.emit("plan", name=spec.name)
        plan = self.plan(spec)
        result = BuildResult(plan, plan.outdir / (f"{spec.name}.gcode.3mf" if do_slice else f"{spec.name}.3mf"), sliced=do_slice)
        result.inputs = self.write_inputs(plan)
        self.emit("planned", ok=plan.ok, problems=plan.problems)
        if plan.ok or keep_going:
            if export:
                result.run = self.export(plan, result.inputs, result.project, timeout, do_slice)
                if result.run.ok:
                    result.patches = self.patch(plan, result.project, do_slice)
                    result.verify_problems = self.verify(plan, result.project, do_slice)
                    result.summary = Project(str(result.project)).summary()
        result.report = self.report(result)
        result.report_path = plan.outdir / f"{spec.name}.report.md"
        result.report_path.write_text(result.report, encoding="utf-8")
        self.emit("finished", ok=result.ok, report=str(result.report_path))
        return result

    # ---------------------------------------------------------------- steps
    def plan(self, spec: Spec) -> Plan:
        outdir = spec.outdir.expanduser().resolve()
        workdir = outdir / "shambu"
        workdir.mkdir(parents=True, exist_ok=True)
        problems: list[str] = list(spec.validate())
        warnings: list[str] = []
        machine = self.resolver.for_cli("machine", spec.printer)
        process = self.resolver.for_cli("process", spec.process.base, spec.process.set, spec.process.name)
        filaments = [self.resolver.for_cli("filament", f.preset, f.set, f.name) for f in spec.filaments]
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
        used_keys: set[str] = set()
        k = 0  # assemble_index: unique across the project, as object names are global
        for pi, (pspec, seq) in enumerate(zip(spec.plates, sequences, strict=True), 1):
            parts = self._measure(pspec.parts, pi, process, problems, len(filaments), used_keys, workdir)
            by_object = seq == "by object"
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
            pctx = replace(ctx, reserve=WIPE_TOWER_RESERVE if multi else None, by_object=sequential)
            fixed = [p for p in parts if p.placed.fixed]
            free = [p.placed for p in parts if not p.placed.fixed]
            obstacles = [p.rect() for p in (x.placed for x in fixed)] + ([WIPE_TOWER_RESERVE] if multi else [])
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
                if len(packed) > 1:
                    problems.append(
                        f"plate {pi} '{pspec.name}': the parts need {len(packed)} plates; move some to another plate in the spec"
                    )
            checks = self.checker.check([p.placed for p in parts], pctx)
            checks += self._subpart_checks(parts, pctx)
            plate = A.Plate(
                name=pspec.name or f"plate {pi}",
                need_arrange=False,
                plate_params=self._plate_params(spec, fmap, map_applies, seq if (pspec.print_sequence or proc_seq != "by layer") else None),
            )
            groups: dict[int, list[PlannedPart]] = {}
            for p in parts:  # copies of one spec share an Item (count + pos list)
                groups.setdefault(id(p.spec), []).append(p)
            for group in groups.values():
                first = group[0]
                positions = [p.placed.pos() for p in group]
                # A range without layer_height segfaults the slicer (layer_height_profile_from_ranges); the GUI always writes it.
                ranges = [
                    A.HeightRange(r.min_z, r.max_z, {"layer_height": str(process.get("layer_height", "0.2")), **r.set})
                    for r in first.spec.height_ranges
                ]
                if first.assembled:
                    idx = list(range(k + 1, k + 1 + len(group)))
                    k += len(group)
                    for fp, fil, sub in first.files:
                        plate.items.append(A.Item(fp, fil, len(group), sub, idx, positions))
                    for i in idx:
                        plate.assembled.append(A.Assembled(i, first.spec.set, ranges))
                    for n, p in enumerate(group):
                        p.cli_name = f"assemble_{idx[n]}"
                else:
                    fp, fil, sub = first.files[0]
                    plate.items.append(A.Item(fp, fil, len(group), sub, 0, positions, first.spec.set, ranges))
                    for n, p in enumerate(group, 1):
                        p.cli_name = f"{fp.stem}_{n}"
            alist.plates.append(plate)
            plates.append(PlannedPlate(plate.name, parts, checks, seq))
        warnings += self._name_objects([x for p in plates for x in p.parts])
        problems += A.validate(alist.to_json(), known_keys=known, filament_count=len(filaments))
        return Plan(spec, outdir, workdir, machine, process, filaments, facts, fmap, plates, alist, problems, warnings)

    def _measure(
        self,
        part_specs: Sequence[PartSpec],
        plate_no: int,
        process: dict,
        problems: list[str],
        n_filaments: int,
        used_keys: set[str],
        workdir: Path | None = None,
    ) -> list[PlannedPart]:
        out: list[PlannedPart] = []
        for si, ps in enumerate(part_specs, 1):
            where = f"plate {plate_no} part {si}"
            files = [(f.path.expanduser().resolve(), f.filament, f.subtype) for f in ps.files()]
            if not ps.orientation.is_identity and all(fp.is_file() for fp, _, _ in files):
                # The CLI cannot turn one object (reference 01), so the orientation is baked into copies of the STLs,
                # all in one shared frame so the sub-parts of a merged object keep their alignment.
                try:
                    turned = M.oriented_copies([fp for fp, _, _ in files], ps.orientation, (workdir or files[0][0].parent) / "oriented")
                    files = [(t, fil, sub) for t, (_, fil, sub) in zip(turned, files, strict=True)]
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
                eff = effective_filaments(ps, files)
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
            margin = ps.brim_margin if ps.brim_margin is not None else L.brim_margin({**process, **ps.set})
            for copy in range(ps.count):
                key = _unique_key(ps.key(), used_keys)
                placed = L.Placed(key, body, main, margin=margin, also_filaments=frozenset(eff - {main}))
                if ps.at is not None and copy == 0:
                    placed.x0, placed.y0, placed.fixed = float(ps.at[0]), float(ps.at[1]), True
                out.append(PlannedPart(ps, key, files, ok_infos, placed))
        return out

    @staticmethod
    def _subpart_checks(parts: list[PlannedPart], ctx: L.Context) -> list[L.Check]:
        """Per sub-part of a merged object: manifold, and the x reach of the sub-part's own filament."""
        out = []
        for p in parts:
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
            cmd.setting("wipe_tower_x", WIPE_TOWER_XY[0]).setting("wipe_tower_y", WIPE_TOWER_XY[1])
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
        if s["process"] != plan.process["name"]:
            probs.append(f"process in file is {s['process']}, expected {plan.process['name']}")
        if len(s["filaments"]) != len(plan.filaments):
            probs.append(f"{len(s['filaments'])} filaments in file, expected {len(plan.filaments)}")
        if len(s["plates"]) != len(plan.spec.plates):
            probs.append(f"{len(s['plates'])} plates in file, expected {len(plan.spec.plates)}")
        want = sum(p.count for pl in plan.spec.plates for p in pl.parts)  # the spec, not a plan that may have dropped parts
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
            if k not in ps:
                probs.append(f"process setting {k} is missing from the file, spec says {v!r}")
            elif str(ps[k]) != str(v):
                probs.append(f"process setting {k} is {ps[k]!r} in the file, spec says {v!r}")
        self.emit("verified", problems=probs)
        return probs

    def report(self, r: BuildResult) -> str:
        from .report import render

        return render(r)


def load_spec(path: str | Path) -> Spec:
    try:
        return Spec.from_json(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as e:
        raise BuildError(f"cannot read spec {path}: {e}") from e
