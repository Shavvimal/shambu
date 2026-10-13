# shambu

[![ci](https://github.com/Shavvimal/shambu/actions/workflows/ci.yml/badge.svg)](https://github.com/Shavvimal/shambu/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/Shavvimal/shambu?sort=semver)](https://github.com/Shavvimal/shambu/releases)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![python](https://img.shields.io/badge/python-3.11%2B-blue)

STLs in, a checked Bambu Studio project out. `shambu` drives Bambu Studio's own command line to turn a short
JSON spec into a `.3mf` project (or a sliced `.gcode.3mf` with time and filament estimates) with the parts placed,
the process settings applied, the right filament on each part and the plate type set. It also inspects and
patches existing projects, and reads a Bambu Lab printer's status over the LAN (read-only). It ships as a CLI and
as a Claude Code plugin, so an agent can prepare plates while you keep the Print button.

Built by reverse engineering Bambu Studio 02.08.02.61: its source at tag `v02.08.02.61`, the shipped macOS binary,
its crash reports and real project files. The findings live in [`skills/shambu/references/`](skills/shambu/references/)
and every rule the code enforces cites one. See [`DISCLAIMER.md`](DISCLAIMER.md).

## How it works

```
spec.json  ->  check  ->  build  ->  patch  ->  verify  ->  report.md (+ project.3mf)
   presets flattened   STLs measured   Studio CLI export   bed/colours/map   re-read + --info
   (inherits+include)  parts packed    (0.3 s, or 1 s      pauses, names     estimates when
                       limits checked   with --slice)                        sliced
```

- **Presets** are resolved from Bambu Studio's own profile files, including the `include` templates the CLI
  itself ignores (otherwise a project gets the generic start G-code instead of the printer's).
- **Placement** is computed, never left to the slicer's arrange: every part gets a stated bed rectangle, brim
  margins, and a check against the bed, both nozzles' reach and height, TPU on the main nozzle, and the prime tower.
- **Export** runs the Studio binary headless with the flags that are known to work, in a private data and AppKit
  home so it can never hang on a hidden "reopen windows?" alert.
- **Patch** fixes what the CLI leaves wrong in the 3MF (bed type per plate, all filament colours, the nozzle map
  with its extruder variant, object names, pauses and colour changes).
- **Verify** re-reads the file, compares it with the spec, and loads it through Studio's `--info`.
- **Report** is the hand-back: placements with numbers, checks, patches, estimates, the user's next steps.

## Requirements

- macOS with [Bambu Studio](https://bambulab.com/en/download/studio) installed (02.08.x tested; the CLI lives in the app bundle).
- [uv](https://docs.astral.sh/uv/) (Python 3.11+ is fetched by uv if needed).
- For the plugin: [Claude Code](https://code.claude.com/).
- A Bambu Lab printer for the status commands (tested on an X2D; the printer must be on the same LAN and selected
  once in Bambu Studio's Device tab so its LAN access code is stored).

## Installation

As a Claude Code plugin:

```
/plugin marketplace add Shavvimal/shambu
/plugin install shambu@shambu
/reload-plugins
/shambu:setup
```

`/shambu:setup` installs the CLI (`uv tool install "shambu @ git+https://github.com/Shavvimal/shambu"`) and runs
`shambu doctor`. From then on, ask Claude to "make a plate from these STLs with PETG functional settings" and the
`/shambu` skill does the rest.

CLI only:

```
uv tool install "shambu @ git+https://github.com/Shavvimal/shambu"
shambu doctor
# or without installing:
uvx --from "git+https://github.com/Shavvimal/shambu" shambu doctor
```

## Usage

**1. Build a project from STLs.** Write a spec, check it, build it.

```
shambu spec                      # prints the spec shape
shambu recipes                   # named settings bundles: petg-functional, tree-supports, tall-outer-brim, ...
shambu check  plate.json         # resolve presets, measure STLs, place parts, check limits
shambu build  plate.json         # + export with Studio's CLI, patch, verify, write the report
shambu build  plate.json --slice # also slice headless: <name>.gcode.3mf with time and filament estimates
```

A spec names the printer preset, a process preset with recipes and overrides, the filaments with colours, and the
plates with their parts (a file with filament, count, recipes, settings, height ranges, optional fixed position; or
several files merged into one object). `build` writes `<outdir>/<name>.3mf`, `<name>.report.md`, `<name>.log` and
the CLI inputs under `<outdir>/shambu/`.

**2. Find the best orientation and print sequence.** Score orientations without slicing, then let the slicer judge.

```
shambu orient bracket.stl                       # bed contact, support shadow, height per candidate face
shambu sweep plate.json --orientations bracket --by-object   # slices each variant: time, grams, support, waste, swaps
shambu rotate part.stl -o out --lay-on-face 0,0,1          # write a turned copy; shambu split a.stl -o out
```

A part takes `"lay_on_face": [nx, ny, nz]` and `"rotate": [x, y, z]`; a plate takes `"print_sequence": "by object"`
(one object after another: on a two-filament plate that drops the prime tower and the per-layer nozzle changes,
40 min instead of 1 h 24 on the measured plate). The sweep writes each variant to `<outdir>/sweep/<name>/`.

**3. Inspect or patch an existing project.**

```
shambu inspect project.3mf
shambu patch project.3mf --bed-type "Textured PEI Plate" --colours "#1A1A1A,#F0F0F0" --map 1,2 \
    --set "carrier_1:enable_support=1" --pause 1:10.2 --rename-plate "1:carrier D.6" --print-sequence "1:by object"
```

**4. Read the printer (read-only).**

```
shambu printer status [--ip <printer-ip>]
shambu printer watch --every 300          # until the printer leaves RUNNING/PAUSE
```

Add `--json` to any command for machine-readable output (`printer watch --json` prints one document per poll).
Exit codes: 0 ok, 1 a check or verification failed, 2 usage, 3 environment (binary, config, printer).

## Safety

- The printer code subscribes to the status topic and publishes exactly one `pushall` request. It never starts,
  pauses, stops or changes a print and never uploads a file. Pull requests that add printer control are not merged.
- The LAN access code is read from Bambu Studio's configuration, kept in memory, never printed or logged, and sent
  only over TLS verified against Bambu's certificate authority (`--insecure` is an explicit opt-out).
- Projects are handed back for the user to open, check and print. The tool never presses Print.

## Rules the code encodes (so nobody has to remember them)

- The CLI never resolves `inherits` or `include`; a half-resolved machine preset gives a project with a generic
  577-character start G-code. `PresetResolver` merges both (the X2D template is 14848 characters).
- Any action flag makes the run headless; no action opens the GUI. `CliCommand.args()` refuses that.
- Bool flags take no separate value (`--min-save 1` makes `1` an input file). The builder writes `--flag=0|1`.
  `--min-save` strips all meshes; `--orient 1` overwrites object settings; `--pipe` does nothing on macOS.
- `--export-3mf` wants a bare name plus an existing `--outputdir`. The CLI defaults the bed to Cool Plate (which the
  X2D does not support) and writes one colour and one map entry for two filaments. The pipeline patches the 3MF.
- Headless slicing crashes without a colour per filament, without `layer_height` in every height range, and with
  the default prime tower (it sits off the bed). The pipeline sets all three; a plate slices in about one second.
- After any Studio crash, macOS shows a modal alert inside the CLI process that blocks it for ever (the fabled
  "five-minute export"). Every call runs with `CFFIXED_USER_HOME` pointed at a private empty directory.
- Assemble-list `pos_*` are translations added to the STL's own coordinates, and `pos_x`, `pos_y`, `pos_z` must
  have the same length (an indexing bug). A merged part loses its height ranges (use `assembled_params`).
- Extruder 2 (right, Bowden) reaches only x >= 20.5 mm and z <= 256 mm. Anything on filament 2, including a height
  range or a sub-part, must sit inside that. A filament moved to nozzle 2 also needs `filament_extruder_variant`.
- `layer_config_ranges.xml` keys objects by build order, not id. Thumbnails are optional.
- Print by object (Studio `sequential_print_clearance_valid`): objects need a 59.8 mm hull gap on the X2D (3.8 mm
  when all are under 4 mm tall), print in object-list order, and an object over 32.8 mm must not be followed by one
  in its Y band (33 mm); nothing but the last may pass 133 mm. Studio drops the prime tower itself. The layout
  packs in print order with a 60 mm gap and closes the row after a tall object.
- The CLI's `--rotate*` flags turn every object; a per-part orientation is baked into an STL copy instead.
- X2D speed keys are six-slot per-nozzle arrays; no recipe writes a scalar into them.

## Repository layout

| Path | What |
|---|---|
| `src/shambu/` | The package: `presets` (chain of profile sources, flattening), `studio` (binary adapter, `CliCommand` builder), `assemble` (assemble-list model and validation), `layout` (placer and limit rules), `mesh` (STL read/write, orientation scoring), `threemf` + `patches` (3MF read/patch commands, G-code feature usage), `spec` (typed spec, builder), `recipes`, `build` (template-method pipeline), `sweep` (variant builds and the comparison table), `report`, `printer` (discovery, MQTT reader, decoder), `conf`, `facade`, `cli` |
| `tests/` | Offline pytest suite with fakes for the binary, the profile tree and the MQTT transport |
| `skills/shambu/` | The Claude Code skill and the reverse-engineering references |
| `skills/setup/` | `/shambu:setup`: installs the CLI, runs `shambu doctor` |
| `.claude-plugin/` | Plugin manifest and single-plugin marketplace |
| `REVIEW-RUBRIC.md` | The checklist pull requests are reviewed against |
| `DISCLAIMER.md` | Responsible use, reverse engineering, third-party licences |

## Developing

```
make install     # uv sync --group dev
make check       # ruff, mypy, pytest
claude plugin validate --strict .
claude --plugin-dir .            # try the skills from this checkout
uv tool install --reinstall .    # install the CLI from this checkout
```

Bump `__version__` in `src/shambu/__init__.py` and `version` in `.claude-plugin/plugin.json` together whenever
anything under `src/`, `skills/` or `.claude-plugin/` changes; CI enforces the bump and the release workflow tags
`v<version>` on `main`.

## Licence

MIT. shambu contains no Bambu Studio code; it drives a locally installed copy under Bambu Lab's own terms and is not
affiliated with Bambu Lab. See `DISCLAIMER.md`.
