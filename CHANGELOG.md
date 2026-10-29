# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] - 2026-10-29

A shambu project now opens in Bambu Studio with the process it was sliced with. On 2026-10-29 the owner opened a
shambu project in Studio for the final pass, the process came back as the stock `0.20mm Standard @BBL X2D`, and the
GUI re-sliced at 2 walls and 15 % grid instead of the spec's 4 walls and 25 % gyroid. shambu 0.5.1 exported the
process as a user preset Studio did not know, with an empty `different_settings_to_system`, and Studio resets every
key that list does not name (post-mortem `docs/errors/2026-10-29-gui-open-drops-process.md`, spec
`docs/specs/2026-10-29-gui-preset-bookkeeping.md`).

### Fixed
- The preset bookkeeping in every export: `print_settings_id` (and each `filament_settings_id`, the
  `printer_settings_id`) names the base system preset, and `different_settings_to_system[slot]` lists the overrides:
  sorted(the explicit overrides together with every key that differs from the resolved base), merged with any key the
  CLI listed. Both the sliced and the unsliced export carry it, and `slice --settings-from` writes the process and
  printer bookkeeping too. The GUI shows `<base> (modified)` with the values in place, and a GUI save keeps them (read
  from the Studio source at v02.08.02.61; the live open check is spec section 6). The spec's `process.name` stays the report label.
- Include order in preset resolution: the leaf's own keys now win over its `include` template, as in Studio
  (`PresetBundle.cpp:5084-5086`, `:5109`, `:5142-5144`, `:5158`). Three X2D filament values were the template's before:
  `filament_cooling_before_tower`, `retraction_distances_when_ec`, `slow_down_min_speed`.
- The hand-back text: open with `shambu open`, confirm the process names `<base>` and check the override values in
  its tab (Studio marks the base modified only when an override differs from it, `Preset.cpp:2292-2305`), confirm
  each filament on its planned nozzle, Slice plate (Studio opens a project with objects unsliced), and the GUI
  estimate must land within 1 min and 1 g of shambu's. Otherwise the user saves the project in Studio under a new
  name and runs `shambu verify` on the save: no Print until it exits 0, and a FAIL the user wants goes in the spec and
  a rebuild. Print only when the estimate matched or verify on the saved copy exited 0. "Re-slice in Studio after any
  change" is gone.

### Added
- `shambu verify FILE --against SPEC [--json]`: compares a GUI-saved project with its spec: the spec's process keys
  (recipes included), the preset ids and lists, filament colours and set keys, map and mode at project and plate
  level, bed type, print sequence, and each object's spec-set settings, filament and position (`at`); one FAIL, WARN or
  INFO line per difference. Exit 0 when no line is FAIL, 1 on any FAIL line or a spec that does not load (WARN-only
  GUI edits exit 0), 3 when the project file does not read. A filament 2..N saved by the GUI as `<base>(<file>.3mf)`
  passes when its values match. A spec on an installed user preset expects the exporter's bookkeeping: the system
  base as the id and the user preset's overrides in the list and the file. Any `inherits_group` entry that names
  another preset than the slot's system base is a FAIL, for every slot (`Preset.cpp:2258-2264`). "25%" equals "25"
  only for the percent-only keys; for a FloatOrPercent key such as `max_travel_detour_distance` it is a difference.
- `shambu patch FILE --process-set KEY=VALUE` and `--process-unset KEY`: change a process key and keep the override
  list right.
- The `two-filament-plate` recipe (`[process]`: `enable_prime_tower 1`, `independent_support_layer_height 0`), paired
  with `dual-material-interface` on the parts.
- `shambu inspect` prints `process <id> (+N overrides: ...)`.
- `shambu open` prints "WARN: Bambu Studio was already running: check that the front window shows <file name>":
  where the project opens depends on Studio's `single_instance` setting (`GUI_App.cpp:7711-7722`).
- A WARN in `check` and `build` when a `[process]` recipe sits on a part or an `[object]` recipe in
  `process.recipes`. The keys still apply.
- The derived infill keys: when the spec or a recipe sets `sparse_infill_density`, `skeleton_infill_density` and
  `skin_infill_density` follow it, as the GUI does (`Tab.cpp:2105-2111`).

### Changed
- `dual-material-interface` is per object (`[object]`), where Studio's own suggestion dialog writes its interface
  keys; the GUI keeps per-object settings across an open. `enable_prime_tower` moved to `two-filament-plate`.

## [0.5.1] - 2026-10-27

### Changed
- setup skill, step 5: updating the plugin is one command, `/plugin marketplace update shambu`: per the Claude Code
  docs it refreshes the marketplace and updates the plugins installed from it, and closing the `/plugin` panel runs
  `/reload-plugins`. The two documented fallbacks stay: `/reload-plugins --force` when the reload is left pending,
  `/plugin update shambu@shambu` when the update reports a plugin it left for you. The README says the same.

## [0.5.0] - 2026-10-27

Printer profiles for LAN-only mode and cloud mode. On 2026-10-27 the X2D went to LAN-only mode and regenerated its
LAN access code, Studio's config still held the old code, and shambu had nowhere to keep an IP or a code, so every
read failed until Studio's config was edited by hand. shambu now keeps its own profiles and codes, and both modes use
the same local read.

### Added
- Printer profiles: `printers.json` in shambu's config dir holds one entry per printer (name, serial, last IP, mode,
  TLS flag, the dates added and last verified) and the default profile. It never holds an access code.
- `shambu printer login` in three forms: `--ip IP [--serial SERIAL]` (a new printer), `--printer NAME [--ip IP]` (a
  known profile, for a new code or a new address) and `--from-studio` (the serial and the code of Studio's selected
  printer). It reads the printer once with the code and stores the profile and the code only after that read
  succeeds. The code comes from `--access-code-file`, `--access-code-stdin` or a hidden prompt on a terminal.
  `--name`, `--default` and `--mode` set the profile's name, the default and the mode.
- `shambu printer list`: one line per profile (name, masked serial, IP, mode, last verified time, `(default)`); exit 1
  when there is none. `shambu printer forget --printer NAME [--keep-code]` removes a profile and, unless
  `--keep-code`, its stored code.
- `--printer NAME`, `--serial`, `--access-code-file FILE` and `--access-code-stdin` on `doctor`, `check`, `build`,
  `sweep` and every `printer` action. The two code flags read the first line and keep the code for this run only.
- `--mode lan|cloud` on `login`: the mode the user set on the touchscreen (shambu does not detect it; a profile
  without it says `unknown`). The mode picks the next step that a refused login names.
- A subnet pass in discovery: after the ARP table, the other hosts of the default-route network (/24 or narrower) on
  TCP 8883. It runs in `printer discover` (always), in `login` when it has no IP (`--from-studio` without `--ip`), and
  on a profile read only when the profile has no IP, which `login` never leaves. A read through a profile goes to the
  profile's stored IP and does not search.
- `login --insecure` stores the TLS-off choice on the profile, and every later read through that profile skips the
  check; a `login --printer NAME` refresh without `--insecure` clears it. For a new printer, `login --insecure` needs
  `--serial`.
- `login --ip` without `--serial` takes the serial from the CN of the printer's certificate (one TLS handshake, no
  MQTT, no credential).
- Identity-checked discovery: when the serial came from a flag or a profile and TLS is verified, each discovery hit
  must present a certificate that names that serial; a foreign host on 8883 is skipped. A serial from Studio's config
  gets the 0.4.0 behaviour: a TCP connect only.

### Changed
- Error text: a missing code is `no access code for serial <masked>: run shambu printer login, or open the printer
  once in Bambu Studio's Device tab`. A refused login keeps `refused the MQTT login`, then names the code's source and
  the profile, then the next step for the profile's mode (LAN-only: read the code off the touchscreen and run `shambu
  printer login`; cloud: open Studio once, then `login --from-studio`; unknown: both). When the printer's address
  changes, a profile read fails with `cannot connect ... The profile X expected this address (last login ...); run:
  shambu printer login --printer X --ip <new ip>, or shambu printer discover`. With no serial from any source the
  error is `no printer selected: run shambu printer login, or select it in Bambu Studio (Device tab)`, plus `has Bambu
  Studio run on this Mac?` when Studio's config is absent. The `no host on this LAN` error now lists why: the hosts
  checked that are not the printer, the candidates not checked before the timeout and the passes not run.
- A read through a profile goes to the profile's stored IP. When the printer's address changes (a new DHCP lease),
  the read fails with the error above and names the profile's last login; `printer discover` finds the new address
  and `login --printer NAME --ip <ip>` records it. A read never rewrites the profile.
- The watch log's `login` and `setup` categories name `shambu printer login` beside Studio.
- `doctor` prints one `printer profile ...` line in its config section when a profile is selected (name, masked
  serial, mode, last login as `HH:MM UTC`, TLS flag, whether `credentials.json` holds a code for the serial as `code:
  stored|none|unreadable`, profile count, config dir). Doctor's `access code stored` field still describes Studio's config only.
  A damaged `printers.json` is an error in that line, and the rest of the report still runs; the other commands that
  read profiles exit 3 and name the file.
- The UTC label: doctor's profile line (`last login HH:MM UTC`), the `login` hand-back (`read at HH:MM UTC`) and
  `printer list` (`verified HH:MM UTC`) carry it. Doctor's `loaded on the printer (read HH:MM ...)` line is unchanged
  from 0.4.0 and shows local time.
- `main` scrubs every known access code and every raw serial from an error text before it prints it; a masked serial
  stays.
- The config dir is `$SHAMBU_CONFIG_DIR`, else `$XDG_CONFIG_HOME/shambu`, else `~/.config/shambu`. A relative or
  empty value counts as unset, so no file lands in a checkout.
- `--printer` now shadows the argparse prefixes `--printer-timeout` and `--printer-preset`: spell both in full.

### Security
- The access code is written only to `credentials.json` (mode 0600) in the config dir. It never goes to
  `printers.json`, to Studio's config, to argv or to any output.
- Every code the process read, from any source, is a runtime secret: every CLI document and line, usage message and
  error text (the watch header and log, doctor and build warnings, `--json` output) is scrubbed of it. Doctor's
  printer error is redacted of IPs as before.
- shambu refuses a symlink, or anything that is not a regular file, at the paths of `printers.json` and
  `credentials.json`; `credentials.json` must also belong to the user and carry no group or other permission bit.
- Trust boundary: a first-time `login --ip` without `--serial` binds the profile to whatever Bambu-signed certificate
  answers at that IP. A chain-valid certificate proves a Bambu device, not the intended one; `--serial` from the
  touchscreen is the stronger binding.
- Discovery sends nothing but a TCP connect to a candidate host, plus a TLS handshake (the identity check) only when
  the serial came from a flag or a profile and TLS is verified; a serial from Studio's config gets a TCP connect only.
  The subnet pass scans no network wider than /24 and runs only where the Added list says (SECURITY.md has the
  contract).

## [0.4.0] - 2026-10-21

Post-mortem fixes for the G2 rev E plates (2026-10-20 to 10-21): a checker that refused a plate Studio printed, an
uncalibrated time estimate, no verb to edit a project, a two-filament slice that never ran, three versions of one tool.

### Changed
- Layout: the support margin is a per-side `Spread` (tree estimate `min(26, max(8, 0.22 x height))` mm, normal 5 mm,
  measured 20.9 mm on the housing's one side; `--probe-supports` measures each supported part with a solo slice and
  keeps a support mask, cached under `<outdir>/.probe/`). A pair needs the larger spread of the two plus a 1 mm line
  allowance, not the sum. Footprints are raster silhouettes of the oriented mesh (0.5 mm), not bounding boxes.
  `BottomLeftPacker` places by-layer plates (fewest plates, pocket candidates); `ShelfPacker` stays for
  print-by-object. Known limit: the five plate-14 parts without `at` pack on two plates even with measured spreads;
  the printed one-plate layout needs `at` for the feeler and the cradle (Studio's own arrange exits -21 on them). Brims are clipped by Studio and no longer fail a pair, the bed edge or the reach (`WARN brim`).
  Raft and enforced layers count as support (`needs_support`).
- Report: every check line carries a kind: `FAIL body overlap`, `WARN margin rule` (shambu's estimate, the slice
  decides), `WARN brim`, `PLACEMENT NOT DONE` (the packer gave up; no part is printed at the origin), a plate-split
  line naming which parts went where. An unplaced part never reaches Studio, even with `--keep-going`. Studio exit
  -101 names the two objects by spec key; -21 means Studio's own arrange could not fit the parts.
- Two filaments: `effective_filaments` counts a process-level `support_interface_filament` / `support_filament`, so the
  right nozzle's reach (x >= 20.5 plus the interface spread) is checked and the tower reserve applies. `ExtruderReach`
  is the one reach rule. Every unsliced export is re-sliceable: `RepairNozzleStats` fills an empty
  `extruder_nozzle_stats` (the cause of Studio exit -100 "No valid nozzle found" on every re-slice of a shambu
  export), `set_filament_map` keeps `filament_extruder_variant` in blocks, and verify refuses a file that is not
  re-sliceable. `RunResult.summary()` shows the stderr line behind exit -100.
- Prime tower: `wipe_tower` per plate (`"auto"`, a spot `{x, y, width, rib_wall, brim}`, or `false`) replaces the fixed
  (208, 208). `tower.py` ports Studio's size estimate (depth from the tallest part: 41.4 mm square plus 8 mm brim at
  218 mm; narrower is deeper) and places the tower on the silhouettes inside both nozzles' reach, one size per
  project, falling back to no rib wall and widths 35..20; `TowerClear` FAILs name the blocking part. The seed
  dual-material build that exited -102 now gets a pre-slice FAIL.
- Time: every estimate line reads "Studio estimate H h M min"; a measured range or a calibrated time appears only from
  `shambu calibrate` data with its sample count. The two measured prints disagree (0.75 and 1.05 x Studio), so there
  is no correction factor yet.
- doctor: a `shambu` section first (version from `__version__`, install kind from `direct_url.json`, commit),
  `--expect-version X.Y.Z` (exit 3 on a mismatch with a Fix line; the skills run `shambu --version` first),
  `--printer-timeout` (default 15 s), per-step timings; the Studio plist and profile errors no longer abort the
  report. Printer discovery is bounded: `arp -an` with a 2 s timeout, parallel port probes scaled to the host count,
  a 3 s MQTT connect, the whole read on a daemon thread with per-read state; failures are logged, never swallowed.
- Privacy: `printer status --json`, `printer filaments --json`, `printer raw` and every watch error show no IP, no
  serial and no wifi field; the watch log and the calibration file never hold an identifier.
- Spec: `at` is documented as the min corner; `brim_margin` replaces both computed margins; new per-part
  `support_spread: [mx, px, my, py]`; unknown keys are warned about in `spec`, `check` and `build`.
- REVIEW-RUBRIC: items 4, 5, 10, 11 and 13 rewritten for the new modules and the `touches` contract.
- Final review round: a filament group's spread is the per-side maximum of its members and keeps their probe masks;
  `patch` classifies filaments as `check` does (height-range wall, infill, support and interface keys; support slots
  only with support on; an interface-only slot grows by the interface spread); the packer bounds each filament by its
  own growth (an interface-only nozzle no longer refuses a plate the checker accepts); `patch --tower` and
  `slice --tower` size the tower with `tower.py` (Smooth timelapse included) and test it against the plate's outlines;
  `--add-filament` repairs `extruder_nozzle_stats []` before the new slot; `slice` refuses an export without G-code;
  printer job and file names, watch headers and `printer raw` drop the serial and access code; a refused patch is
  exit 1 and a file that cannot be written exit 3 with one JSON document; packer results read as the packer's answer,
  not a proof; two meshes of one name no longer overwrite each other under `sweep --write-spec`.
- Empirical replay (2026-10-21): the tree spread estimate is `min(26, max(8, 0.22 x height))` and is swept from the
  bounding box (a packed plate-14b and plate-11 sliced -101 at the old estimate swept from the outline); `slice` names
  the -101 pair; `--add-filament` tests the project's tower spot; `inspect` prints each object's min corner and size;
  `calibrate` needs 20 layers and 30 minutes for a rate (`span_too_short`) and names a pre-0.4.0 log; a fixed part
  off the bed says `at` is the min corner; a two-filament estimate shows the tower minutes and the measured options.
  The tower is sized per plate from the filaments its objects use; per-volume and height-range filament keys follow
  Studio's scope; the watch header is scrubbed as a structure; `slice` refuses an export missing a requested plate.
- The spread estimate is advisory: when packing with it splits a plate or leaves no tower spot, `check`, `build` and
  `sweep` measure that plate's supported parts with the cached solo-slice probe, place again with the measurements
  and report a `Spreads measured:` line; `--no-probe` keeps the estimate's answer.

### Added
- `shambu patch --replace-mesh OBJECT[/PART]=STL` (anchors frame|centre|min, `--rotate-mesh`, `--lay-on-face`, a no-op
  guard on canonical triangles, a size guard with the 90-degree hint, shared-mesh and `mesh_shared` refusal, paint and
  height-range checks, verify before the atomic replace), `--add-filament "PRESET[:#RRGGBB][@EXTRUDER]"` (append-only,
  slots 1..N byte-identical, the new slot normalised by a scratch Studio export, explicit vector schema, verified by a
  slice; a colour default with a WARN when the preset has none), `--move OBJECT=X,Y[@PLATE]` (min corner, `+DX,+DY`
  relative, resolved before applying, verified on reopen, silhouette clearance FAIL/WARN lines), `--tower X,Y[@PLATE]`,
  `--repair-nozzle-stats`, `--dry-run`. Each patch declares `touches` and `Project.save()` raises on an undeclared
  changed, added or removed entry.
- `shambu slice FILE.3mf [--plate N] [--no-check] [--tower X,Y[@P]] [--settings-from SPEC] [-o OUT]`: slice a saved
  project through shambu's rules (nozzle stats repaired, prime volumes pinned, a fresh run folder, replace only after
  the export reads back). `build --slice --no-check` and `slice --no-check` run a checked pass per plate first and
  reslice without checks only after a -101 or a warning -100, with `Checks skipped:` lines in the report and
  `checks_skipped` in result.json.
- `shambu studio [--out DIR] -- ARGS`: the logged escape hatch; the args pass through `CliCommand.parse` (no action,
  flag order, path containment, refused flags), the command, log and exit meaning land in a run folder.
- `shambu sweep --write-spec OUT.json [--pick NAME]`: writes the winning variant as a full spec with a `_sweep`
  provenance key (resolved spools, Studio version, command), meshes copied beside it.
- `shambu printer watch --log FILE --gcode3mf PROJECT [--plate N] [--wait-for-start] [--max-errors N] [--max-polls N]`:
  timestamped polls (`read_at` UTC, speed level and percent, stages, task id), 60 s polling at the start and the end,
  the final state recorded, errors as fixed-text categories. `shambu calibrate --log FILE... [--add] [--show]`: fits
  a layer-11-and-up rate and a startup offset per printer model and material, flags paused, failed, non-Standard,
  disagreeing and unlogged prints, stores a fingerprinted print in `~/Library/Application Support/shambu/calibration.json`
  under a file lock, and offers a point estimate only from three qualifying prints within 0.15.
- `Status` fields `read_at`, `speed_level`, `speed_percent`, `stages`, `task_id`; `ends_at` is a dated UTC time.
- `shambu inspect` prints each filament's extruder and nozzle stat and a "not re-sliceable" line when it applies.
- Skills: `doctor --expect-version 0.4.0` after `shambu --version`; the setup skill installs the matching tag and
  refreshes the marketplace before `/plugin update`; a hand-back gate with a "Cause: not established" rule; eight
  evals from the post-mortem; `docs/errors/2026-10-21-three-plates-and-uncalibrated-estimates.md`.

### Fixed
- `patch --map` collapsed `filament_extruder_variant` to one entry per filament, which made Studio log
  "could not found extruder_type ... filament_index 2" and slice filament 2 with filament 1's flow ratio.
- `printer status --json` printed the printer's IP address.
- `doctor --json` could not be wrapped in `timeout` on macOS (there is none); the skill says so.

## [0.3.0] - 2026-10-16

### Added
- `shambu printer filaments [--json]`: the spool in each nozzle (1 = left, 2 = right) and in each slot, with material,
  colour, Bambu preset id and the installed preset name it resolves to, from the same single read-only `pushall` as
  `printer status`. Live X2D capture under `tests/fixtures/`: the external spools are `vir_slot` ids 254 (left) and
  255 (right); each extruder's `snow` names the tray it holds.
- `shambu check` and `shambu build` read the printer before resolving a spec. A filament written as
  `{"preset": "loaded"}` (or with `preset` omitted) takes the spool in that nozzle, with its colour; a nozzle with
  nothing loaded, or one that names a slot the report does not list, is a problem that names the fitted spool, never
  a guess. A pinned preset that differs from the spool, or cannot be compared with it, is a FAIL (exit 1) naming
  both, unless `--allow-filament-mismatch`, which the report records (the override waives the preset check, not the
  colour rule). A pinned colour gives way to the spool colour unless the spec has a `colour_note`; a colour that is
  not `#RRGGBB` is a spec problem. The nozzle a filament is compared against follows `filament_map` and the machine
  preset's extruder count, never the report. The report's `Filaments:` line and each filament line state the source
  (the printer at HH:MM, the spec, or the GUI); the sweep table and JSON carry the same line and the fallback WARNING.
  A printer error in the report has its IP address redacted.
- Printer unreachable (or `--no-printer`): a WARNING on the first lines of the report, then the GUI's last-used
  presets from `BambuStudio.conf` stand in for `"loaded"` and pinned presets are kept and compared with the GUI.
- `shambu doctor` gains a `loaded on the printer` section next to the GUI presets, with a `MISMATCH` line per slot
  that differs; `--no-printer` and `--ip` on doctor, check, build and sweep.
- The skill runs `shambu doctor` before every build and writes filaments as `"loaded"` unless the owner names a
  spool; the hand-back carries the `Filaments:` line.

### Fixed
- A spec written for one spool passed every check after the owner changed spools, and the project opened in Studio
  with a filament that was not loaded; Studio's swap then reset the process preset without a word
  (`docs/errors/2026-10-16-loaded-filament-not-used.md`).

## [0.2.1] - 2026-10-13

### Fixed
- The prime tower position is a per-plate vector in Studio (`wipe_tower_x`/`wipe_tower_y`, one value per plate). The
  CLI passed one scalar, which moved the tower on plate 1 only; every later two-filament plate kept the CLI default
  (15, 220), inside the right nozzle's dead strip, and the slicer refused the project (exit -102, or -101 with a part
  in that corner). The build now repeats the position once per plate.

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
- Native orient and review fixes: `orient`/`normalise` refuse an input that is a file the run writes (an input
  named `obj_<i>_<stem>.stl` in the output directory was deleted) and report a bad output directory as one error
  (exit 3); `sweep --source studio` converts units in pass 1, checks contact on the mm mesh and points each variant
  at a mm copy; a tie in Studio's table goes to its `best:` row; `orient --native --pick N` writes row N; the
  G-code parse and the tower + flush grams are decided per plate, a project with two filaments loaded always
  parses, and absent result.json fields add no zero-valued keys; `check` says nothing was exported.
- `split` reports the cavities it dropped.
- Text `inspect` prints the project and per-plate print sequence.
- A bad spec (unknown recipe, bad JSON) in `check` or `build` gives one error document and exit 1, not 3. The
  message names `shambu recipes`, not `bambu recipes`.
- `info` on a multi-object 3MF sums facets and parts and takes the union bbox; it reported only the last object.
- Review fixes: same-stem members of a merged part get separate oriented copies, a filament group packs inside its nozzle's reach, a lone reversed-winding shell stays a body (flipped), `aux_x_limit` counts the brim margin, a short `at` or a structurally bad spec or variant gives exit 1 (the sweep records the variant as failed), failed plate checks show in the sweep problems, and a build stopped at the plan says nothing was exported.

### Changed
- Build estimates, reports and the sweep read Studio's `result.json`: seconds per feature (`feature_times_s`; sweep
  columns for support, prime tower, flush and travel), `main_prediction_s`, `studio_filament_change_times`, and per
  filament `main_g` and `waste_g` (flush + tower). The G-code parser now runs only for support grams, for the
  filament-change (tool-switch) count on a plate with more than one filament, for `inspect --json`, and when a run
  left no `result.json`.
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
