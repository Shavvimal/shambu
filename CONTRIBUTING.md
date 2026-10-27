# Contributing

Thanks for helping. shambu is small on purpose: one CLI, one Python package, two skills.

## Set up

```sh
git clone https://github.com/Shavvimal/shambu.git && cd shambu
make install          # uv sync --group dev
make check            # ruff, mypy, pytest
```

Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). The tests run offline and never call the Bambu Studio
binary or a printer; integration tests skip unless `SHAMBU_INTEGRATION=1` and a Bambu Studio install is present.

## Where things live

| Change | Where | Read first |
|---|---|---|
| A new print recipe | `src/shambu/recipes.py` | `skills/shambu/references/07-print-recipes.md` |
| A new 3MF patch | `src/shambu/patches.py` + `threemf.py` | `references/03-3mf-format.md` |
| A new limit rule or placer | `src/shambu/layout.py` | `references/06-slicer-rules.md`, `08-optimisation.md` section 11 and `00-findings.md` section 10 |
| CLI flag handling | `src/shambu/studio.py` (`CliCommand`) | `references/01-source-cli.md` section 1 |
| Printer fields | `src/shambu/printer.py` | `references/04-app-printer-control.md` |
| Printer profiles and the credentials file | `src/shambu/profiles.py`, `conf.py`, `vault.py` | `docs/specs/2026-10-27-printer-profiles.md` |
| Skill text | `skills/shambu/SKILL.md`, `skills/setup/SKILL.md` | the plugin docs |

Every rule the code encodes about Bambu Studio should cite the reference that proved it. The
review rubric in `REVIEW-RUBRIC.md` is what a pull request is judged against.

## Test on real CAD before a release

The unit tests use synthetic boxes. Real parts found every 0.2.0 bug: a link whose STL sits at z -38 printed its
second copy floating, a supported part next to a second-filament part had its support base printed by the right
nozzle, a carrier's chamfer band scored as the best orientation with nothing on the bed. None of those shapes is in
a unit test, and none would have been invented for one.

So before a release, and after any change to `mesh.py`, `layout.py`, `build.py`, `sweep.py` or a recipe, run the
CLI on actual STLs of several kinds, not one favourite part:

- functional robot parts exported in an assembly frame (gripper links, brackets, motor mounts, gears): off-origin
  STLs, copies, holes in every direction, tree supports;
- URDF or simulation meshes in metres (SO-101, OpenArm): the metres warning and `rotate --scale 1000`;
- Gridfinity bins, baseplates and labels: 45-degree feet and lips, thin two-colour plates, many small objects on
  one plate (print-by-object should lose there);
- organic forms (a character lamp, a logo duck, a funnel): dense meshes, no flat faces, supports everywhere,
  the orientation scorer's blind spots;
- an assembly STL with several bodies (`shambu split`) and a non-manifold mesh.

Keep such files in `data/` (gitignored; `data/README.md` says what to keep and why) and run the loop from that
README: `shambu info`, `shambu orient`, `shambu build --slice`, `shambu sweep ... --orientations ... --by-object`.
Read the sweep table and the Studio log of any variant that fails; the failing exit code and the G-code Studio
leaves in the variant folder say which nozzle printed what, where. A tool that only ever met cubes is not tested.

## Pull requests

- One topic per PR, squash-merged. Add or change a test for every behaviour change.
- Bump `__version__` in `src/shambu/__init__.py` and `version` in `.claude-plugin/plugin.json` together when
  anything under `src/`, `skills/` or `.claude-plugin/` changes (CI enforces the bump; a test enforces the match).
- Add a line under `## [Unreleased]` in `CHANGELOG.md`.
- Never commit a printer serial, access code, IP address or personal path. Run
  `rg -n '192\.168|access_code|/Users/'` before pushing.

## Printer safety

The printer code is read-only by design: subscribe plus one `pushall`. Pull requests that add print start, pause,
stop, upload or any other printer command will not be merged.
