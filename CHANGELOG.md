# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-10-13

### Added
- `shambu orient part.stl`: scores orientations (bed contact, overhang shadow, height) and writes the best as a new
  STL; `shambu rotate` and `shambu split` (lay a face on the bed or rotate by Euler angles; split multi-body STLs).
  Pure stdlib mesh code in `mesh.py`.
- Spec part keys `lay_on_face` and `rotate`: the pipeline bakes the orientation into an STL copy under
  `<outdir>/shambu/oriented/` (the Studio CLI cannot turn one object).
- Spec plate key `print_sequence: "by object"` with the exact Studio rules (60 mm head clearance, the 32.8 mm rod
  height when a later object shares the Y band, 133 mm lid, print order = object order, no prime tower, no Smooth
  timelapse or clumping detection); objects are ordered by filament then height. `shambu patch --print-sequence`.
- `shambu sweep spec.json [--orientations PART] [--by-object] [--variants file.json]`: builds and slices variants of
  one spec and tabulates time, grams, support grams, waste (tower + flush) and filament changes.
- Filament by feature from the sliced G-code (`Project.feature_usage`): model, support, prime tower, flush grams and
  the number of filament changes, in reports, `inspect --json` and the sweep table.
- Recipes from the research: `bridge-short-spans`, `minimal-supports`, `petg-supports`, `counterbore-bridging`,
  `more-support`, `scarf-seam`, `seam-back`, `iron-top`, `precise-fit`, `show-walls`, `fast-infill`;
  `dual-material-interface` now carries Bambu's own X2D support values.
- Reference `08-optimisation.md`: orientation, supports, time and finish research with sources, the by-object
  source rules, headless metrics, the Studio GUI workflow and the ClassCAD evaluation.
- Spec plate key `group_by_filament: true` (print-by-object only): single-file parts with one filament, the same
  settings and height ranges and no `at` merge into one object `group-f<n>`, packed close inside and 60 mm from
  the others, so the members print together layer by layer and share the minimum layer time (four cubes: 21 min
  merged, 29 min separate, 42 min by layer). The report and `--json` list the members; verify counts a group as one object.
- Recipes `fine-0.16` (0.16 mm layers on the 0.20 Standard speeds) and `top-concentric`. A 30 mm cube: 1062 s
  model time at 0.16 against 852 s at 0.20, and 1666 s with the shipped 0.16mm High Quality preset.

### Fixed
- Copies of a part (`count` 2 or more) landed at the first copy's position plus their own, because Studio's CLI
  clones the translated first copy before translating the others. A link at z -38 in its CAD frame printed its
  second copy floating at z 38 and off the bed. Copies now get positions relative to the first copy
  (reference 01 section 2.4, live-verified).

- On a plate with two filaments, a supported part's `support_filament` and `support_interface_filament` are
  pinned to the part's own filament when the spec leaves them at Studio's "default" (0): Studio otherwise prints
  the support base with whichever filament is loaded, and the right nozzle cannot reach x < 20.5 (exit -102,
  found with a real gripper link next to a second-filament part).
- Orientations that leave (almost) nothing flat on the bed sort last and are marked "NO FLAT CONTACT"; the sweep
  skips them. `shambu orient` warns when a mesh is in metres; `shambu rotate --scale 1000` converts it.
- The layout keeps a support margin round a part with supports: 0.25 x its height, clamped to 5..25 mm. The
  extruder-reach check uses the margin rect. A bracket's tree support reached 15.6 mm past its box (Studio -101).
- A mesh whose whole bbox is under 1 mm fails `check` as a mesh in metres (Studio failed it with -100).
- `check` notes an STL with more than one body (an assembly prints as one object).
- An overflowing plate gives one FAIL line ("the parts need N plates") and no overlap lines for parts on the
  virtual second plate. No OK line follows a FAIL for the same rule.
- An orientation (`lay_on_face`, `rotate`) that leaves no flat contact on the bed is a problem, not a silent build.
- A failed export reports "export failed" with Studio's error string, `sliced` false in `--json`, and no "open the
  project" step for a file that does not exist.
- Studio's slicer warnings (`warning_message`, for example "floating cantilever") appear in the report and `--json`.
- The filament-change count takes the first T line as the loaded filament (the indented `T0 H-1` in the start
  G-code) and counts only later changes. A plate whose first layers use filament 2 reported 0 changes.
- Default overhang threshold 60 degrees from vertical, which is Studio's own 30 from horizontal; a facet at exactly
  the threshold is printable (Gridfinity's 45-degree foot counted by float noise).
- `orient` adds a slenderness penalty, so a 6 mm rod 204 mm tall no longer wins standing.
- `split` drops inward shells and counts them as cavities (an SO-101 jaw had a -76.5 mm3 shell written as a body).
- `orientation_variants` raises when no candidate stands, instead of an empty sweep with no message.
- Sweep time columns round to the minute (2299 s and 2404 s print 0h38 and 0h40, +0h02).
- `--json` works after the subcommand too, as the README says.
- `info` continues past a bad file (error entry, exit 1) and warns on a mesh in metres; `orient --json` carries the
  metres warning.
- `--threshold` defaults to 60 in `orient` and `sweep`.
- `rotate --scale` alone drops the scaled copy onto z = 0, as the help says.
- `split` reports the cavities it dropped.
- Text `inspect` prints the project and per-plate print sequence.
- A bad spec (unknown recipe, bad JSON) in `check` or `build` gives one error document and exit 1, not 3. The
  message names `shambu recipes`, not `bambu recipes`.
- `info` on a multi-object 3MF sums facets and parts and takes the union bbox; it reported only the last object.

### Changed
- The skill explains orientation, supports and time hacks and runs the sweep loop to pick a configuration.
- `data/` is gitignored for real CAD parts; `data/README.md` and CONTRIBUTING describe the manual real-CAD loop.
- Reference 07: the prime-tower-with-by-object rule is compiled out in Studio (corrected).
- SKILL.md follows the official skill best practices: a description with trigger words and a not-for clause,
  `argument-hint`, narrow `allowed-tools`, a checklist, a minimal spec, a hand-back template, a decision table and a
  references table. The evidence moved to references 00, 06 and 09.
- The setup skill has narrow `allowed-tools`.
- Evals live under `skills/*/evals/`.
- The long references have a Contents table.

### Known limitations
- `luffy-v1.3mf`, a file written by Bambu Studio 02.03.01.00, crashes Studio's own `BambuStudio --info` (exit 139;
  shambu reports exit -11). A shambu-built 3MF of the same 520k-facet mesh loads, so it is not shambu's fault.


## [0.1.1] - 2026-10-13

### Fixed
- Formatting of one comment so `ruff format --check` passes in CI.

## [0.1.0] - 2026-10-13

### Added
- `shambu` CLI: `doctor`, `info`, `presets`, `recipes`, `spec`, `check`, `build [--slice]`, `inspect`, `patch`, `open`,
  `printer status|watch|discover|raw`.
- Spec-driven pipeline: preset flattening (`inherits` + `include`), STL measurement, shelf packing with nozzle-reach
  rules, assemble-list generation, Bambu Studio CLI export, 3MF patching, verification and a hand-back report.
- Headless slicing with time and filament estimates (filament colours, height-range layer heights and an in-bed prime
  tower are set so the slicer no longer crashes).
- Read-only printer status over LAN MQTT (one `pushall`), with TLS verification against Bambu's CA.
- Claude Code plugin with the `/shambu` skill and `/shambu:setup`.
- Reverse-engineering references for Bambu Studio 02.08.02.61 (CLI source, binary and crash analysis, 3MF format,
  app and printer control, print recipes).
