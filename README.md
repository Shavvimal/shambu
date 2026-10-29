# shambu

[![ci](https://github.com/Shavvimal/shambu/actions/workflows/ci.yml/badge.svg)](https://github.com/Shavvimal/shambu/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/Shavvimal/shambu?sort=semver)](https://github.com/Shavvimal/shambu/releases)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![python](https://img.shields.io/badge/python-3.11%2B-blue)

**Why this exists.** Bambu Studio is a one-plate-at-a-time tool: you arrange, pick a profile, slice, read the
estimate, and try again. Every variant costs a human a few minutes, so nobody explores more than a handful. shambu
turns that loop into a command an agent can run many times over, in parallel: the same parts in six orientations,
by object and by layer, with and without a prime tower, on one plate or two, each slice read back as grams and
minutes (`shambu sweep` does exactly this). Because it ships as a Claude Code skill, several agents can prepare
several plates at once, each with its own placement and reach checks, and iterate on the result in a way a person
in the GUI cannot. Studio stays the place where you look at the plate and press Print; the search for the best plate
happens before that, at machine speed, with every rule the slicer enforces checked first.

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
- **Placement** is computed, never left to the slicer's arrange: parts are packed on their mesh outlines (never
  rotated) or put at the min corner the spec gives, then checked against the bed, both nozzles' reach and height,
  TPU on the main nozzle, each neighbour's support spread and the prime tower. The slice is the final clash test.
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
- A Bambu Lab printer for the status commands (tested on an X2D, in LAN-only and in cloud mode; the printer must be
  on the same LAN). shambu needs its LAN access code once: `shambu printer login` stores a profile and the code, or
  select the printer once in Bambu Studio's Device tab so Studio stores the code, as before.

## Installation

As a Claude Code plugin:

```
/plugin marketplace add Shavvimal/shambu
/plugin install shambu@shambu
/reload-plugins
/shambu:setup
```

To update later: `/plugin marketplace update shambu` (it updates the installed plugin and reloads when the panel
closes), then `/shambu:setup update`.

`/shambu:setup` installs the CLI (`uv tool install "shambu @ git+https://github.com/Shavvimal/shambu@v0.6.0"`) and runs
`shambu doctor`. From then on, ask Claude to "make a plate from these STLs with PETG functional settings" and the
`/shambu` skill does the rest.

CLI only:

```
uv tool install "shambu @ git+https://github.com/Shavvimal/shambu@v0.6.0"
shambu doctor --expect-version 0.6.0
# or without installing:
uvx --from "git+https://github.com/Shavvimal/shambu@v0.6.0" shambu doctor --expect-version 0.6.0
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

A spec names the printer preset, a process preset with recipes and overrides, the filaments (`"loaded"` takes the
spool in that nozzle) and the plates with their parts (a file with filament, count, recipes, settings, height
ranges, and an optional `at`: the min corner of its box on the bed; or several files merged into one object). Each
`check` line names its kind: `FAIL body overlap`, `FAIL margin rule`, `WARN margin rule` (an estimated support
spread: the slice decides), `WARN brim` or `PLACEMENT NOT DONE`. `--probe-supports` measures each supported part's
spread with a cached solo slice. A two-filament plate gets a prime tower that `"wipe_tower": "auto"` sizes and
places. `build --slice` is the final test: Studio's exit -101 names the two objects that clash. `build` writes
`<outdir>/<name>.3mf`, `<name>.report.md`, `<name>.log` and the CLI inputs under `<outdir>/shambu/`.

**2. Find the best orientation and print sequence.** Score orientations without slicing, then let the slicer judge.

```
shambu orient bracket.stl                       # bed contact, support shadow, height per candidate face
shambu sweep plate.json --orientations bracket --by-object   # slices each variant: time, grams, support, waste, swaps
shambu sweep plate.json --orientations bracket --write-spec plate-best.json   # writes the fastest variant as a spec
shambu rotate part.stl -o out --lay-on-face 0,0,1          # write a turned copy; shambu split a.stl -o out
```

A part takes `"lay_on_face": [nx, ny, nz]` and `"rotate": [x, y, z]`; a plate takes `"print_sequence": "by object"`
(one object after another: on a two-filament plate that drops the prime tower and the per-layer nozzle changes,
40 min instead of 1 h 24 on the measured plate). The sweep writes each variant to `<outdir>/sweep/<name>/`.
`--write-spec OUT.json` writes the fastest variant that passed (or `--pick NAME`) as a full spec with the original
`outdir` and a `_sweep` key that records the variant and its Studio estimate; build that file.

**3. Inspect, patch, edit or re-slice an existing project.**

```
shambu inspect project.3mf                           # process <base> (+N overrides: ...), filaments, objects
shambu verify project.3mf --against plate.json       # a GUI save against its spec: exit 0 no FAIL, 1 any FAIL or a bad spec, 3 the file does not read
shambu patch project.3mf --process-set wall_loops=4 --process-set sparse_infill_density=25%   # process keys, override list kept
shambu patch project.3mf --process-unset wall_loops  # back to the base preset's value
shambu patch project.3mf --bed-type "Textured PEI Plate" --colours "#1A1A1A,#F0F0F0" --map 1,2 \
    --set "carrier_1:enable_support=1" --pause 1:10.2 --rename-plate "1:carrier D.6" --print-sequence "1:by object"
shambu patch project.3mf --replace-mesh housing=housing-v2.stl --dry-run   # swap a mesh, keep name, settings, place
shambu patch project.3mf --add-filament "Bambu PLA Matte @BBL X2D 0.4 nozzle:#FCECD6@2"
shambu patch project.3mf --move housing=40,30        # the object's min corner, plate-local mm
shambu patch project.3mf --tower 172,150@1          # the prime tower's min corner on plate 1
shambu slice project.3mf [--no-check]                                    # headless slice of a saved project
```

`patch` changes the project in place (or to `-o`) and declares every entry it touches. The edit verbs check what
they change: `--replace-mesh` refuses a size change, a shared or painted mesh; `--add-filament` appends a slot and
verifies it with a slice (a preset with no colour takes the next unused default colour, with a warning); `--move`
and `--tower` report the same pair rule as `check`. `shambu slice --no-check` adds an unchecked pass only after a
-101 or a warning -100, as the GUI would, and the report lists what it skipped. `shambu studio -- ARGS` is the
logged last resort for anything else.

**Opening a project in Studio.** `shambu open project.3mf` (or `build --open`) hands the project to the GUI for the
final pass. The process combo shows the base preset, for example `0.20mm Standard @BBL X2D`, with every spec and
recipe value in place. Studio marks it `(modified)` only when an override differs from the base, so check the
values in the process tab, not the suffix: shambu names the base preset in the project and lists the
overrides in `different_settings_to_system`, the list Studio keeps on open. The spec's `process.name` is a label for
the report, not the GUI's preset name. The plate opens unsliced: Studio never loads the embedded G-code of a project
with objects. Press Slice plate. The GUI estimate must match the report within a minute and a gram. If it does not,
save the project in Studio under a new name (File > Save project as), run `shambu verify <saved>.3mf --against
plate.json` and read its FAIL lines: the export on disk does not change with what Studio holds in memory. Do not
Print until verify exits 0. If a FAIL is a change you want, put it in the spec and rebuild. Print only when the GUI
estimate matched, or verify on the saved copy exited 0. When Studio is already running, `open` prints "WARN: Bambu
Studio was already running: check that the front window shows <file name>": where the project opens depends on
Studio's `single_instance` setting (`GUI_App.cpp:7711-7722`).

**4. Read the printer (read-only) and measure prints.**

```
shambu printer login --ip <printer-ip> --serial <serial> --mode lan   # prompts for the LAN access code (not shown)
shambu printer login --printer x2d --ip <new-ip>     # a known profile: a new code or a new address
shambu printer login --from-studio --name x2d        # the serial and the code of Studio's selected printer
shambu printer list                                  # the profiles: name, masked serial, IP, mode, last verified
shambu printer filaments [--printer x2d]             # the spool in each nozzle and slot, with the preset it resolves to
shambu printer status [--ip <printer-ip>]
shambu printer watch --every 300          # until the printer leaves RUNNING/PAUSE
shambu printer watch --wait-for-start --log print.jsonl --gcode3mf plate.gcode.3mf   # log one print
shambu calibrate --log print.jsonl --add  # store it; `calibrate --show` prints the fit per printer and material
```

`login` reads the printer once with the code and stores the profile and the code only when that read succeeds.
`--access-code-stdin` or `--access-code-file FILE` give the code without a prompt; on any other command they give it
for that run only. Profiles live in `printers.json` and codes in `credentials.json` (mode 0600) in shambu's config
dir: `$SHAMBU_CONFIG_DIR`, else `$XDG_CONFIG_HOME/shambu`, else `~/.config/shambu`. A command that reads the printer
takes the code from a one-shot flag, then from the stored code of the profile (`--printer NAME`, else the default
profile), then from Bambu Studio's config. Both printer modes use the same local read; the mode only says where a new
code comes from when the printer refuses the old one (the touchscreen in LAN-only mode, Studio in cloud mode).
`shambu printer forget --printer NAME` removes a profile and its code (`--keep-code` keeps the code); when the
forgotten profile was the default, the first remaining profile becomes the default.

A read through a profile goes to the IP the profile stores and does not search; `login` always stores the IP it read
from. The subnet pass (the default-route /24 after the ARP table) runs in `printer discover` (always), in `login`
when it has no IP (`--from-studio` without `--ip`), and on a profile read only when the profile has no IP. When the
printer's address changes (a new DHCP lease), a profile read fails with `cannot connect ... The profile x2d expected
this address (last login ...); run: shambu printer login --printer x2d --ip <new ip>, or shambu printer discover`.
Run `shambu printer discover`, then `shambu printer login --printer x2d --ip <ip>` to record the new address.

Every time in a report is "Studio estimate", Studio's own prediction. The watch log stamps each poll and never
holds the IP or the serial. A report adds the measured range of stored prints, and a calibrated time only when
three qualifying prints of that printer model and material agree.

`check` and `build` read the same report before resolving a spec: a filament written as `{"preset": "loaded"}` takes
the spool in that nozzle with its colour, and a pinned preset that is not loaded fails the check (exit 1) unless
`--allow-filament-mismatch`. With the printer unreachable the report starts with a WARNING and the GUI's last-used
presets stand in.

Add `--json` to any command for machine-readable output (`printer watch --json` prints one document per poll).
Exit codes: 0 ok, 1 a check or verification failed or a patch was refused, 2 usage, 3 environment (binary, config, printer, a file that cannot be written), 130 interrupted.

## Safety

- The printer code subscribes to the status topic and publishes exactly one `pushall` request. It never starts,
  pauses, stops or changes a print and never uploads a file. Pull requests that add printer control are not merged.
- The LAN access code comes from a one-shot flag, the credentials file (mode 0600) or Bambu Studio's configuration.
  It is never printed, logged or passed on a command line, and it is sent only over TLS verified against Bambu's
  certificate authority (`--insecure` is an explicit opt-out). `login --insecure` stores that choice on the profile,
  and every later read through the profile skips the check; a `login --printer NAME` refresh without `--insecure`
  clears it.
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
- Assemble-list `pos_*` are translations added to the STL's own coordinates for the first copy; copies 2..n are
  cloned from the translated first copy, so their `pos` is relative to it. `pos_x`, `pos_y`, `pos_z` must have the
  same length (an indexing bug). A merged part loses its height ranges (use `assembled_params`).
- Extruder 2 (right, Bowden) reaches only x >= 20.5 mm and z <= 256 mm. Anything on filament 2, including a height
  range or a sub-part, must sit inside that. A filament moved to nozzle 2 also needs `filament_extruder_variant`.
- `layer_config_ranges.xml` keys objects by build order, not id. Thumbnails are optional.
- Print by object (Studio `sequential_print_clearance_valid`): objects need a 59.8 mm hull gap on the X2D (3.8 mm
  when all are under 4 mm tall), print in object-list order, and an object over 32.8 mm must not be followed by one
  in its Y band (33 mm); nothing but the last may pass 133 mm. Studio drops the prime tower itself. The layout
  packs in print order with a 60 mm gap and closes the row after a tall object.
- The CLI's `--rotate*` flags turn every object; a per-part orientation is baked into an STL copy instead.
- X2D speed keys are six-slot per-nozzle arrays; no recipe writes a scalar into them.
- `support_filament` 0 ("default") lets Studio print a part's support with whichever filament is loaded. On a
  two-filament plate that put a filament-1 part's support base on the right nozzle at x < 20.5 (exit -102). The
  planner pins `support_filament` and `support_interface_filament` to the part's own filament when they are unset.
- Tree support spreads past a part's box and does not avoid a neighbour: 20.9 mm on one side of a 218 mm housing,
  15.6 mm under a 170 mm bracket. The checker estimates `min(26, max(8, 0.22 x height))` per side (normal support
  5 mm), keeps the larger of two facing spreads plus 1 mm between parts, and leaves the verdict to the slice
  (exit -101); `--probe-supports` measures it.
- An unsliced CLI export writes `extruder_nozzle_stats: []`, and a later CLI slice of it exits -100 "No valid
  nozzle found". Every export gets the repaired key, and verify refuses a file without it.
- A mesh whose whole bbox is under 1 mm is in metres (URDF exports); Studio fails it with -100. `check` refuses it,
  and `shambu rotate --scale 1000` writes a mm copy.

## Repository layout

| Path | What |
|---|---|
| `src/shambu/` | The package: `presets` (chain of profile sources, flattening), `studio` (binary adapter, `CliCommand` builder), `assemble` (assemble-list model and validation), `layout` (placer and limit rules), `mesh` (STL read/write, orientation scoring), `threemf` + `patches` (3MF read/patch commands, G-code feature usage), `spec` (typed spec, builder), `recipes`, `build` (template-method pipeline), `sweep` (variant builds and the comparison table), `report`, `printer` (discovery, MQTT reader, decoder), `conf` (Studio's config, the config dir), `profiles` (printer profiles), `vault` (the credentials file), `facade`, `cli` |
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
uv tool install --reinstall .    # development only: a snapshot that does not follow the plugin; users install the tag (/shambu:setup)
```

Before a release, run the real-CAD loop in `data/README.md` (the folder is gitignored; copy your own STLs in).
Bump `__version__` in `src/shambu/__init__.py` and `version` in `.claude-plugin/plugin.json` together whenever
anything under `src/`, `skills/` or `.claude-plugin/` changes; CI enforces the bump and the release workflow tags
`v<version>` on `main`.

## Licence

MIT. shambu contains no Bambu Studio code; it drives a locally installed copy under Bambu Lab's own terms and is not
affiliated with Bambu Lab. See `DISCLAIMER.md`.
