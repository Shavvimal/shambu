"""Studio runs behind the project edits: the scratch exports that normalise a new filament slot, the verify slice
after an added filament, `shambu slice` (checked passes and the --no-check pass) and the `shambu studio --` escape
hatch with its run folder. `facade.Bambu` exposes each as a method; `cli.py` only parses argv and prints.

Design: plain functions over an injected `StudioCLI` (or a test fake with the same `run()`), a `PresetResolver` and
paths. Every Studio call goes through `StudioCLI.run` (rubric 2). `shambu slice` shares the build's pieces: the
checked and --no-check passes (`build.two_pass_slice`, whose -101 pair comes from `studio.conflicts_in_log`), the
RepairNozzleStats path for an unsliced CLI export, the read-back (`summarise`) and the strict tower verify
(`build.verify_tower`); its "Checks skipped" lines are `report.checks_skipped_lines`. Every file this module writes is
a scratch file in a temporary or run folder, or the requested output, which is replaced only after the run succeeded
and the export verified.

Not here: building a project from a spec (`build.py`), the edits themselves (`edits.py`), the archive writer
(`threemf.py`).
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import presets as P
from .build import DEFAULT_COLOURS, _estimate, summarise, two_pass_slice, verify_tower
from .edits import AddFilament, EditError, EditSet, FilamentSlot, SetTower, check_preset, slot_from_export, slot_from_preset, slot_presets
from .errors import BambuError, StudioError
from .patches import PatchSet, RepairNozzleStats
from .spec import Spec
from .studio import CliCommand, RunResult, StudioCLI
from .threemf import Project

FILAMENT_ARG = re.compile(r"(?P<preset>.+?)(?::(?P<colour>#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?))?(?:@(?P<ext>\d+))?")
VARIANT_MISS = "could not found extruder_type"


# ---------------------------------------------------------------------------- add a filament
def add_filament_patch(
    studio: StudioCLI | None, resolver: P.PresetResolver, file: str, arg: str, allow_duplicate: bool = False
) -> AddFilament:
    """`PRESET[:#RRGGBB][@EXTRUDER]` as an AddFilament. With Studio: two scratch exports of a copy of `file`, one loaded
    with the new preset alone (its normalised values, slot 1 there) and one with every slot (only its flush matrix is
    read). Without Studio: the resolved preset JSON, which AddFilament reports with a WARN. ValueError for a malformed
    argument (usage); EditError for an unknown or incompatible preset, a bad colour or extruder."""
    m = FILAMENT_ARG.fullmatch(arg.strip())
    if m is None:
        raise ValueError(f"--add-filament {arg!r}: use PRESET[:#RRGGBB][@EXTRUDER]")
    try:
        preset = resolver.for_cli("filament", m["preset"].strip())
    except BambuError as e:
        raise EditError(f"--add-filament: {e}") from e
    preset_colour = preset.get("filament_colour") or preset.get("default_filament_colour") or [""]
    colour = (m["colour"] or (preset_colour[0] if isinstance(preset_colour, list) else str(preset_colour)) or "").upper()
    extruder = int(m["ext"] or 1)
    ps = Project(file).project_settings
    warn = ""
    if not colour:  # BBL system presets carry no filament_colour: the next default the project's slots do not use
        used = {str(c).upper() for c in ps.get("filament_colour") or []}
        colour = next((c for c in DEFAULT_COLOURS if c.upper() not in used), DEFAULT_COLOURS[len(used) % len(DEFAULT_COLOURS)])
        warn = f"WARN: colour not in preset {m['preset'].strip()}; using {colour} (pass :#RRGGBB to choose)"
    check_preset(ps, preset, colour, extruder)
    if studio is None:
        return AddFilament(_noted(slot_from_preset(ps, preset), warn), colour, extruder, allow_duplicate)
    with tempfile.TemporaryDirectory(prefix="shambu-add-filament-") as td:
        work = Path(td)
        src = work / "in.3mf"
        shutil.copyfile(file, src)
        new_json = P.write({**preset, "filament_colour": [colour]}, work / "filament-new.json")

        def export(name: str, paths: list[Path], cols: list[str]) -> dict:
            outdir = work / name
            outdir.mkdir()
            cmd = CliCommand(inputs=[str(src)]).load_filaments(*paths).setting("filament_colour", ";".join(cols))
            rr = studio.run(
                cmd.export_3mf("scratch.3mf", outdir), name=name, outputdir=outdir, timeout=300, expect_output=outdir / "scratch.3mf"
            )
            if not rr.ok:
                raise StudioError(f"the scratch export ({name}) for the new filament failed: {rr.summary()}")
            return Project(str(outdir / "scratch.3mf")).project_settings

        # 1. the new preset alone: Studio's normalised values, as slot 1 of the scratch copy (an export loaded with
        # N+1 filaments writes the loaded preset's variant block first, so it cannot give them)
        normalised = export("add-filament-normalise", [new_json], [colour])
        # 2. slots 1..N from the project's own settings (their presets may be custom or gone) plus the new one, with every
        # colour: only its flush matrix is read. Every JSON carries a filament_colour, else the CLI's vector-size check
        # fails (exit -5).
        old = slot_presets(ps, preset)
        paths = [P.write(f, work / f"filament-{i}.json") for i, f in enumerate(old, 1)] + [new_json]
        flush = export("add-filament-flush", paths, [f["filament_colour"][0].upper() for f in old] + [colour])
    return AddFilament(_noted(slot_from_export(ps, normalised, preset, flush), warn), colour, extruder, allow_duplicate)


def _noted(slot: FilamentSlot, warn: str) -> FilamentSlot:
    return dataclasses.replace(slot, notes=(warn, *slot.notes)) if warn else slot


def verify_filaments(studio: StudioCLI, path: str) -> str:
    """The verify slice: slice plate 1 of the patched archive and look for Studio's variant-lookup miss
    (PrintConfig.cpp:8935), which runs when a slice applies the config, not on a plain export. Only exit 0 with a
    readable log passes; a crash, a timeout, a failed slice or a missing log raises EditError."""
    outdir = Path(tempfile.mkdtemp(prefix="shambu-verify-"))  # kept when the check fails: the error names its log
    cmd = CliCommand(inputs=[str(Path(path).resolve())]).slice(1, no_check=False).export_3mf("verify.gcode.3mf", outdir)
    rr = studio.run(cmd, name="add-filament-verify", outputdir=outdir, timeout=1200)
    try:
        text = Path(rr.log).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise EditError(f"verify slice: Studio's log {rr.log} is unreadable ({e}); the filament was not added") from e
    if rr.code != 0:
        raise EditError(f"verify slice: Studio exited {rr.code} ({rr.meaning()}); the filament was not added; log {rr.log}")
    if not text.strip():
        raise EditError(f"verify slice: Studio wrote an empty log {rr.log}; the filament was not added")
    if VARIANT_MISS in text:
        line = next(x for x in text.splitlines() if VARIANT_MISS in x)
        raise EditError(f"verify slice: Studio cannot find a filament's extruder variant: {line.strip()}; log {rr.log}")
    shutil.rmtree(outdir, ignore_errors=True)
    return f"verify slice: Studio sliced plate 1 (exit 0, success); no '{VARIANT_MISS}' in its log"


def apply_edits(studio: StudioCLI | None, path: str, edits: EditSet, out: str | None = None, verify: bool = True) -> list[str]:
    """Apply an EditSet. With an AddFilament, `verify` and Studio present, the verify slice runs on the temporary archive
    before it replaces the destination, so a failure leaves the destination as it was."""
    if verify and studio is not None and any(isinstance(x, AddFilament) for x in edits.patches):
        lines: list[str] = []
        done = edits.apply(path, out, check=lambda tmp: lines.append(verify_filaments(studio, tmp)))
        edits.notes += lines
        return done
    return edits.apply(path, out)


# ---------------------------------------------------------------------------- shambu slice
@dataclass
class SliceResult:
    file: Path
    output: Path
    run: RunResult
    checks_skipped: dict | None
    estimates: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    verify_problems: list[str] = field(default_factory=list)  # the read-back of the export (re-slice and tower)

    @property
    def ok(self) -> bool:
        return self.run.ok and not self.verify_problems

    def to_json(self) -> dict:
        return {
            "file": str(self.file),
            "output": str(self.output) if self.ok else None,
            "ok": self.ok,
            "cli": self.run.summary(),
            "checks_skipped": self.checks_skipped,
            "estimates": self.estimates,
            "notes": self.notes,
            "verify_problems": self.verify_problems,
        }


def slice_output(src: Path, out: str | None) -> Path:
    if out:
        return Path(out).resolve()
    stem = src.name.removesuffix(".3mf").removesuffix(".gcode")
    dest = src.with_name(f"{stem}.gcode.3mf")
    return dest if dest != src else src.with_name(f"{stem}.resliced.gcode.3mf")


def slice_project(
    studio: StudioCLI,
    resolver: P.PresetResolver,
    file: str,
    out: str | None = None,
    *,
    plate: int = 0,
    no_check: bool = False,
    tower: tuple[float, float, int | None] | None = None,
    settings_from: Spec | None = None,
    timeout: int = 1200,
) -> SliceResult:
    """Slice a saved project into `out`. The input is never written: a tower position goes into a scratch copy, and
    Studio exports to a unique temporary name next to `out`, which replaces `out` only after the run succeeded.
    ValueError when `out` resolves to the input itself."""
    src = Path(file).resolve()
    project = Project(str(src))  # a readable Bambu project, or ProjectError before Studio runs
    dest = slice_output(src, out)
    if dest.resolve() == src or (dest.exists() and os.path.samefile(dest, src)):
        raise ValueError(f"the output {dest} is the input file: give another -o")
    dest.parent.mkdir(parents=True, exist_ok=True)
    n_plates = len(project.model_settings.findall("plate"))
    # A checked slice of a BBL 3MF without --load-filaments resets filament_prime_volume to 45, a --no-check one does
    # not (BambuStudio.cpp:6886-6894): pin the project's own values in every pass so the passes slice the same config.
    raw_prime = project.project_settings.get("filament_prime_volume")
    prime = [str(v) for v in raw_prime] if isinstance(raw_prime, list) and raw_prime else None
    notes: list[str] = []
    stem = dest.name.removesuffix(".3mf").removesuffix(".gcode")
    # every pass writes its log, result.json and export into a fresh run folder next to the output (kept as the record;
    # never an existing folder, never removed); the export there replaces the output only after it reads back
    work = Path(tempfile.mkdtemp(prefix=f"{stem}.slice-", dir=dest.parent))
    tmp_dest = work / f"{stem}.gcode.3mf"
    inp = src
    loads: list[Path] = []
    if tower is not None:
        inp = work / "input.3mf"
        es = EditSet([SetTower(*tower)])
        es.apply(str(src), str(inp))
        notes += es.notes
    n_ext = len(P.machine_facts(project.project_settings)["extruders"])
    why = Project(str(inp)).reslice_problems(n_ext)
    if why:  # an unsliced CLI export: every CLI slice of it exits -100 until the nozzle stats are repaired
        rep = work / "repaired.3mf"
        notes += [f"repaired before slicing ({x})" for x in why] + PatchSet([RepairNozzleStats()]).apply(str(inp), str(rep))
        if inp != src:
            inp.unlink(missing_ok=True)
        inp = rep
    if settings_from is not None:
        machine = resolver.for_cli("machine", settings_from.printer)
        process = resolver.for_cli("process", settings_from.process.base, settings_from.process.set, settings_from.process.name)
        loads = [P.write(machine, work / "machine.json"), P.write(process, work / "process.json")]
        notes.append(f"settings: machine {machine['name']}, process {process['name']}; the project's filaments kept")

    def make(nc: bool, pl: int, outdir: Path) -> CliCommand:
        cmd = CliCommand(inputs=[str(inp)])
        if loads:
            cmd.load_settings(*loads)
        if prime:
            cmd.setting("filament_prime_volume", prime)
        cmd.slice(pl, no_check=nc)
        return cmd.export_3mf(tmp_dest.name, outdir)

    plates = (plate,) if plate else (tuple(range(1, n_plates + 1)) if n_plates > 1 else (0,))
    rr, skipped = two_pass_slice(
        studio,
        make,
        name=stem,
        outputdir=work,
        timeout=timeout,
        expect_output=tmp_dest,
        no_check=no_check,
        plates=plates,
        known=set(project.object_names()),
    )
    if inp != src:
        inp.unlink(missing_ok=True)
    estimates: list[dict] = []
    if rr.ok:
        try:  # read the export back before it replaces anything
            s = summarise(tmp_dest, rr)
        except BambuError as e:
            raise EditError(f"Studio exited 0 but its export {tmp_dest} does not read back ({e}); {dest} is unchanged") from e
        by_id = {sp.id: sp for sp in rr.plates()}
        estimates = [_estimate(pl, by_id.get(pl["id"])) for pl in s["plates"] if pl.get("slice")]
        back = Project(str(tmp_dest))
        problems = [f"not re-sliceable by the CLI: {x}" for x in back.reslice_problems(n_ext)]
        spots = {i: (tower[0], tower[1]) for i in range(n_plates) if tower[2] in (None, i + 1)} if tower else {}
        problems += verify_tower(back.project_settings, n_plates, {}, spots, "--tower")
        if problems:  # the export stays in the run folder: it is not the slice the command asked for
            res = SliceResult(src, dest, rr, skipped, estimates=estimates, notes=notes, verify_problems=problems)
            notes.append(f"logs, Studio's result.json and the unverified export: {work}")
            return res
        os.replace(tmp_dest, dest)
        rr = RunResult(rr.cmd, rr.status, rr.code, rr.seconds, rr.log, rr.result, dest)
    res = SliceResult(src, dest, rr, skipped, estimates=estimates, notes=notes)
    if prime:
        notes.append(f"filament_prime_volume pinned to the project's {prime} in every pass (Studio resets it to 45 on a checked slice)")
    notes.append(f"logs and Studio's result.json: {work}")
    return res


# ---------------------------------------------------------------------------- shambu studio --
def studio_raw(studio: StudioCLI, raw: list[str], cwd: Path, out_dir: Path, timeout: int = 1200) -> dict:
    """Run a raw Studio argument list under `CliCommand.parse`'s rules (StudioError when refused) and keep the run
    folder `<out_dir>/studio-runs/<UTC stamp>/`: command.json, the log, result.json, exit.txt."""
    from . import __version__  # at call time: shambu/__init__ imports the facade, which imports this module

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = Path(out_dir).resolve() / "studio-runs" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = CliCommand.parse(raw, cwd, scratch=run_dir)  # an assemble list is rewritten into the run folder
    cmd.option("outputdir", str(run_dir))
    rr = studio.run(cmd, name="studio", outputdir=run_dir, timeout=timeout)
    (run_dir / "command.json").write_text(
        json.dumps({"argv": rr.cmd, "raw": raw, "cwd": str(cwd), "shambu": __version__, "studio": studio.version()}, indent=1),
        encoding="utf-8",
    )
    if not (run_dir / "result.json").exists():
        (run_dir / "result.json").write_text(
            json.dumps({"return_code": rr.code, "written_by": "shambu (Studio wrote no result.json)"}, indent=1), encoding="utf-8"
        )
    (run_dir / "exit.txt").write_text(f"exit {rr.code} ({rr.meaning()})\n", encoding="utf-8")
    return {"exit": rr.code, "meaning": rr.meaning(), "run_dir": str(run_dir), "log": str(rr.log), "argv": rr.cmd}
