# shambu review rubric

Each item has a test a reviewer can run. An item passes only when its test passes. Run from
`~/.claude/skills/bambu-3mf/scripts/`. "rg" = ripgrep.

## Architecture

1. **Front-end boundary.** Only `shambu/cli.py` and `bambu.py` read argv, stdin or print to stdout.
   Fail if `rg -n "sys\.argv|input\(|print\(" shambu --glob '!cli.py'` returns a line, or a library module imports `shambu.cli`.
2. **Dependency injection.** `Pipeline`, `Bambu`, `PrinterService` receive their adapters (`StudioCLI`, `PresetResolver`, `Placer`, `LimitChecker`, `Discovery`) in `__init__`; tests construct them with fakes.
   Fail if `subprocess.` appears outside `studio.py`, `printer.py` (arp) and `facade.py` (open), or `paho` is imported outside `printer.py`.
3. **Constructors only store.** No `__init__` does I/O. (`StudioCLI.__post_init__` checks that the binary exists: that is a stat, documented.)
4. **Pure planning core.** `spec.py`, `assemble.py`, `layout.py`, `tower.py`, `edits.py`, `patches.py`, `recipes.py` import no `subprocess`, `socket`, `paho`, `zipfile`.
   Fail if `rg -n "import (subprocess|socket|zipfile)|paho" shambu/{spec,assemble,layout,tower,edits,patches,recipes}.py` matches.
5. **Pattern economy.** Every Protocol or Strategy has two implementations or a test fake: `PresetSource` (DirectorySource, ChainedSource), `Placer` (ShelfPacker, BottomLeftPacker + test fake), `Footprint` (RasterFootprint, BoxFootprint), `SpreadProbe` (StudioProbe + test fake), `Rule` (Clearance, InsideBed, ExtruderReach, TpuOnMain, MeshSane, SequentialClearance, tower.TowerClear), `tower.Clearance` (FootprintClearance, BoxClearance + test fake), `Patch` (twelve in `patches.py`, four in `edits.py`), `Discovery` (ArpProbe, SsdpListen), `Listener` (CLI logger + test recorder).
6. **Template Method is overridable.** A `Pipeline` subclass that overrides `export()` with a fake runs end to end in tests without Bambu Studio.

## Correctness

7. **CLI rules encoded, not remembered.** `CliCommand.args()` refuses a command with no action, orders `--no-check` before `--slice`, emits `--flag=0|1` for bools, joins `--export-3mf` to a bare name + `--outputdir`. Fail if a test for each rule is missing.
8. **Presets are complete.** `PresetResolver.for_cli("machine", "Bambu Lab X2D 0.4 nozzle")["machine_start_gcode"]` is longer than 10000 chars (the `include` templates are merged) and `check_compatible` passes for the X2D defaults.
9. **Assemble-list rules.** `assemble.validate()` flags: non-string `*_params` values, unknown keys, `pos_*` length mismatch, non-part subtype without `assemble_index`, height ranges on merged parts, > 36 plates, filament index > count.
10. **Placement is explicit and checked.** Every part has a computed bbox on the bed; `LimitChecker` flags overlap, bed exit, extruder 2 reach (x < 20.5 and the user's 235.5 limit) for the part's filament AND its height-range, sub-part and process support or interface filaments (one FAIL line per part and extruder), TPU off the main nozzle, non-manifold meshes.
11. **3MF integrity.** Each `Patch` declares `touches` (entry names or fnmatch patterns; none declared means `Metadata/*`), including the entries its slice invalidation removes or rewrites (`edits.SLICE_TOUCHES` plus `model_settings.config`). `Project.save()` compares every entry with the loaded bytes and raises, before writing, when an undeclared entry changed, was added or was removed, when a `3D/*` entry was added, removed or reordered, or when a `_rels` entry changed. Only the geometry edits in `edits.py` declare `3D/*` entries: the sub-model of the one object they name (resolved at apply time) and the item or component `transform` attributes of `3D/3dmodel.model`, rewritten by targeted text substitution. An edit verifies the temporary archive before it replaces the destination. Entry order, compression and dates stay; the output passes `zipfile.testzip()`; applying the same `PatchSet` twice gives the same summary and the same bytes. `layer_config_ranges.xml` ids are build-order indexes.
   Test: `tests/test_edits.py::test_save_rejects_undeclared_entry_change`, `test_undeclared_deletion_raises`, `test_edit_applied_twice_is_idempotent`.
12. **Verify after export.** `Pipeline.verify()` compares bed type, process name, filament and plate and object counts, start/end G-code length and every process override against the written file.
13. **Explicit failure.** Missing preset, binary, STL, or a crashed/timed-out CLI raises a `BambuError` subclass or yields a `RunResult` with a non-zero code and a meaning string; no `except` returns `None` silently. `ruff check --select BLE001,E722` is clean except seven documented `# noqa: BLE001`: two in the facade's doctor (the config and machine sections: one section's failure must not hide the others), two in the MQTT teardown (`loop_stop` and `disconnect` after a read), the injectable port probe in `ArpProbe` (a closed port is False; any other exception is logged and counted closed), the caller's `on_step` progress callback (logged, never fails the read), and the printer read's worker thread (`BaseException` caught to re-raise it in the caller's thread).

## Safety

14. **Secrets.** The LAN access code is read only by `conf.access_code()`, stored in `MqttReader.access_code` with `repr=False`, and never logged, printed, written or passed on a command line. Test: a sentinel code in a fake config never appears in CLI stdout/stderr.
15. **Read-only printer.** The only MQTT publish is the `pushall` request. Fail if `rg -n "publish\(" shambu` shows another topic or payload, or any code path sends `project_file`, `pause`, `stop`, `resume`, `print`.
16. **No GUI keystrokes.** No `osascript ... keystroke`. Opening a project uses `open -a BambuStudio`.

## Quality

17. **Types and lint.** `ruff check .` clean with the `pyproject.toml` rule set; `mypy shambu` reports no errors in `spec.py`, `assemble.py`, `layout.py`, `patches.py`, `presets.py`.
18. **Tests offline by default.** `python3 -m pytest -q` passes without the Studio binary, the network or paho (integration tests skip unless `SHAMBU_INTEGRATION=1`).
19. **CLI UX.** Every subcommand supports `--json` with exactly one JSON document on stdout and logs on stderr; exit codes 0 ok, 1 check failed, 2 usage, 3 environment error. `--help` works without paho.
20. **Docs.** Every module has a docstring that states what it handles and what it does not; `README.md` shows the three main flows (build a spec, patch a file, read the printer); the SKILL.md recipe points at `bambu.py`, not at hand-assembled flags.
