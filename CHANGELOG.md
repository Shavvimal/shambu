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

### Changed
- The skill explains orientation, supports and time hacks and runs the sweep loop to pick a configuration.
- Reference 07: the prime-tower-with-by-object rule is compiled out in Studio (corrected).

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
