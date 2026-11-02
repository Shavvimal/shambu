# Contributing

Thanks for helping. shambu is small on purpose: one CLI, one Python package, three skills (`shambu`, `setup`,
`designing-for-print`).

## Set up

```sh
git clone https://github.com/Shavvimal/shambu.git && cd shambu
make install          # uv sync --group dev
make check            # ruff, mypy, pytest
```

Python 3.11 or newer (CI runs 3.11, 3.12 and 3.13) and [uv](https://docs.astral.sh/uv/). `make install` installs
no optional extra, so the tests that need numpy, scipy, manifold3d or shapely skip. Run
`uv sync --group dev --all-extras` to run them too. The tests run offline and never call the Bambu Studio
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
| A printability check | `src/shambu/printability.py` (a `PrintCheck`), `meshgeom.py`, `findings.py` | `references/08-optimisation.md` section 12 |
| A file integrity check | `src/shambu/integrity.py` | `references/03-3mf-format.md` section 2.9 |
| A CAD gate or finding code | `src/shambu/cad/` (pure modules; Booleans and distances in `cad/geom/`, the geom extra) | `skills/designing-for-print/references/gates.md` |
| A topology solver step or a `topo.*` code | `src/shambu/topo/` (`spec.py` and `result.py` stdlib; the rest behind the topo extras) | `skills/designing-for-print/references/topology.md` |
| Skill text | `skills/shambu/SKILL.md`, `skills/setup/SKILL.md`, `skills/designing-for-print/SKILL.md` | the plugin docs |

Every rule the code encodes about Bambu Studio should cite the reference that proved it. A finding names its
evidence level (`measured`, `observed`, `external`, `heuristic`; reference 08 section 12.1): only a measured finding
can FAIL, and an external one stays INFO until a reference records the X2D file it was seen in. The
review rubric in `REVIEW-RUBRIC.md` is what a pull request is judged against.

## Test on real CAD before a release

The unit tests use synthetic boxes. Real parts found every 0.2.0 bug: a link whose STL sits at z -38 printed its
second copy floating, a supported part next to a second-filament part had its support base printed by the right
nozzle, a carrier's chamfer band scored as the best orientation with nothing on the bed. None of those shapes is in
a unit test, and none would have been invented for one.

So before a release, and after any change to `mesh.py`, `layout.py`, `build.py`, `sweep.py` or a recipe, run the
CLI on actual STLs of several kinds, not one favourite part:

- functional parts exported in an assembly frame (links, brackets, motor mounts, gears): off-origin
  STLs, copies, holes in every direction, tree supports;
- URDF or simulation meshes in metres (SO-101, or any robot-arm URDF): the metres warning and `rotate --scale 1000`;
- Gridfinity bins, baseplates and labels: 45-degree feet and lips, thin two-colour plates, many small objects on
  one plate (print-by-object should lose there);
- organic forms (a character lamp, a logo duck, a funnel): dense meshes, no flat faces, supports everywhere,
  the orientation scorer's blind spots;
- an assembly STL with several bodies (`shambu split`) and a non-manifold mesh.

After a change under `src/shambu/topo/`, run `shambu topo validate --size paper` and one `shambu topo solve` on a
real part spec with the topo extras installed, and read the findings, not only the exit code.

Keep such files in `data/` (gitignored; `data/README.md` says what to keep and why) and run the loop from that
README: `shambu info`, `shambu orient`, `shambu printability` in the pose `orient` picks, `shambu build --slice`,
`shambu cad gate` on a spec for a real CAD export with holes and a two-part assembly (the geom extra installed),
`shambu sweep ... --orientations ... --by-object`. After a change to `printability.py` or `meshgeom.py`, update the
timings and findings table in reference 08 section 12.5.
Read the sweep table and the Studio log of any variant that fails; the failing exit code and the G-code Studio
leaves in the variant folder say which nozzle printed what, where. A tool that only ever met cubes is not tested.

## Pull requests

- One topic per PR, squash-merged. Add or change a test for every behaviour change.
- Bump `__version__` in `src/shambu/__init__.py` and `version` in `.claude-plugin/plugin.json` together when
  anything under `src/`, `skills/` or `.claude-plugin/` changes (CI enforces the bump; a test enforces the match).
- Add a line under `## [Unreleased]` in `CHANGELOG.md`.
- Never commit a printer serial, access code, IP address or personal path. Run
  `rg -n '192\.168|access_code|/Users/'` before pushing.
- `tests/test_public_hygiene.py` fails on a home path, a 192.168 address, a Bambu preset folder id, a full session UUID,
  a short id after "session" or "thread", a printer serial (full or masked), a path into a personal notes or site
  repository, or a private-context phrase (`GENERIC_TERMS` in the test) in any tracked file. Fixture serials are
  invented and hold a word (`00X0TEST0000999`).
- Employer, client and private project names, part names and account names go in the untracked file
  `~/.config/shambu/private-terms` (or `$XDG_CONFIG_HOME/shambu/private-terms`), one per line, never in a tracked
  file. Only a local run reads it: CI has no such file, and the private-term test skips with a reason that says the
  names were not checked. Run `make check` locally with the file in place before every push.

## Printer safety

The printer code is read-only by design: subscribe plus one `pushall`. Pull requests that add print start, pause,
stop, upload or any other printer command will not be merged.
