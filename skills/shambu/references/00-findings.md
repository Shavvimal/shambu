# 00. What the reverse engineering found (Studio 02.08.02.61, 2026-10-13)

The rules below are the reason `shambu` exists as a CLI instead of a page of `BambuStudio` flags. The code
encodes every one of them; this file is the short list, and the numbered references hold the evidence with
file:line citations. Read this before changing `studio.py`, `build.py`, `assemble.py` or `threemf.py`.

## Contents

1. The "five-minute export"
2. Headless slicing crashes and their causes
3. Presets
4. CLI flags
5. The assemble list
6. The 3MF
7. The Studio GUI
8. The printer (read-only)
9. Where the evidence lives

## 1. The "five-minute export"

It was not compute: `export_time` in `result.json` is milliseconds (an export takes 0.3 s). After any Studio
crash, macOS shows a modal "reopen windows?" alert inside the CLI process, which then blocks for ever at 0 % CPU.
The tool sets `CFFIXED_USER_HOME` to a private empty directory on every call, which removes the alert without
touching the real `~/Library` (reference 02).

## 2. Headless slicing crashes

- Exit 139 in `FilamentGroup::calc_one_change_budget`: with two filaments and no filament colours the flush
  matrix stays 1 x 1. A colour per filament fixes it.
- A height range without `layer_height` crashes `layer_height_profile_from_ranges`. The tool adds it.
- The default prime tower at (15, 220) runs off the bed (return code -104). The tool positions it.
- Exit -102 "G-code in unprintable area of multi-extruder printers": `support_filament` 0 lets Studio print a
  part's support with whichever filament is loaded; on a two-filament plate the right nozzle printed a filament-1
  part's support base at x < 20.5. The tool pins the support filaments (reference 08 section 3.3b).
- Exit -63 "collision in sequential print": the print-by-object clearance and height rules (reference 08
  section 2). The tool checks them before export.

**The prime tower position is a per-plate vector (0.2.1).** `wipe_tower_x` and `wipe_tower_y` hold one value per plate. Passed as a scalar on the command line, the value lands on plate 1 only and every later plate keeps the CLI default (15, 220), which sits inside the right nozzle's dead strip (x < 20.5): plate 2 of an 8-plate two-filament project failed with exit -102 "G-code in unprintable area" and, with a part in that corner, exit -101 "G-code path conflicts" (organiser build, 2026-10-13). The CLI now repeats the value once per plate, so the tower sits at (208, 208) everywhere.

## 3. Presets

The CLI never resolves `inherits` or `include`. A half-resolved machine preset gives a project with the generic
577-character start G-code instead of the X2D's 14848-character template: never print G-code from such a file.
`PresetResolver` merges both (reference 01).

## 4. CLI flags

Bool flags take no value (`--min-save 1` makes `1` an input file). `--min-save` strips all meshes. `--orient 1`
overwrites every object's settings with the process preset, but only when `--load-settings` loaded one and only
objects that carry per-object keys lose anything: an orient-only run on positional STLs (shambu's pass 1 for
`"orientation": "auto"`) is safe, and the slicing run with the assemble list never passes `--orient`. Studio's
printed best vector ends up pointing up, so `lay_on_face = -best` (live on five parts). `--rotate*` turns every object at once. `--pipe` does
nothing on macOS. Actions run in argv order (`--no-check` before `--slice`). `--export-3mf` wants a bare name plus
an existing `--outputdir` (reference 01 section 1).

## 5. The assemble list

`pos_*` are translations added to the STL's own coordinates for the first copy; copies 2..n are cloned from the
translated first copy, so their `pos` is relative to it (live-verified 2026-10-13: a link at z -38 printed its
second copy at z 38). Keep `pos_x`, `pos_y`, `pos_z` the same length (an indexing bug). `*_params` values must
be strings. A merged part loses its height ranges (use `assembled_params`). Non-part subtypes need
`assemble_index >= 1`. The CLI defaults the bed to Cool Plate, which the X2D does not support, and writes one
colour and one map entry for two filaments (reference 01 section 2).

## 6. The 3MF

`layer_config_ranges.xml` keys objects by build order, not id. A filament mapped to nozzle 2 also needs
`filament_extruder_variant` = "Bowden Standard" or slicing crashes. Thumbnails are optional. Every patch the tool
makes survives a CLI load and re-export. The plate's `print_sequence` metadata beats the process key
(reference 03, reference 08 section 2).

`result.json`, which a slicing run writes into `--outputdir` next to the 3MF, carries per plate
(`sliced_plates[]`) the seconds per feature in `feature_type_times` (every extrusion role plus `Travel`),
`total_predication`, `main_predication` (total minus prepare, prime tower and flush), `filament_change_times`, and
per filament `total_used_g` and `main_used_g`. `main_used_g` is model plus support, so `total_used_g - main_used_g` is
the flush and prime-tower waste; nothing splits support from the model, which is why the G-code parser stays for
support grams (BS.cpp:500-600, 7265-7330 at v02.08.02.61; PLAN-native-cli section 2.5). `filament_change_times`
counts filament loads, not nozzle switches: it is 0 on a two-nozzle X2D plate (0 against 41 tool switches in the
G-code, live), so the swap count comes from the parser's T lines and shambu keeps Studio's value only as
`studio_filament_change_times`. The slicing path is not the only one that writes the file, and each run
overwrites it: an orient-only run writes a 204-byte `result.json` with `return_code` 0, `error_string` "Success."
and no `sliced_plates` (live). The presence of `sliced_plates`, not of the file, shows that a slice happened.
`inspect` on a project built elsewhere has no run, so it falls back to the parser.

## 7. The Studio GUI

Accessibility exposes the menus (export is `File > Export > Export plate sliced file`, Cmd G) but not the
toolbar; Shift Cmd G is Print plate, so the tool never sends keystrokes. The only safe GUI action is
`open -a BambuStudio file.3mf` (reference 04).

## 8. The printer (read-only)

`shambu printer status` reads `BambuStudio.conf` for the selected serial and its LAN access code, finds the
printer by probing the ARP hosts on TCP 8883 (or `--ip`), connects with TLS verified against Bambu's bundled CA
and the serial (no unverified fallback; `--insecure` turns verification off on request), subscribes to
`device/<serial>/report`, publishes one `pushall`, and prints state, percent, layer, remaining time and end time,
both nozzles (id 1 = main left, id 0 = auxiliary right), bed and chamber, HMS codes with the offline text from
Studio's `hms_en_20P.json`. `watch` polls until the state leaves RUNNING/PAUSE (the state after a finished print
is FINISH). The access code never leaves memory. Nothing but `pushall` is ever published; starting, pausing or
stopping stays with the user.

Loaded filament (`shambu printer filaments`, and what `doctor`, `check` and `build` read; live X2D capture
2026-10-16 in `tests/fixtures/x2d_pushall_two_external_spools.json`): the same `pushall` carries the spools.
The X2D reports its two external spools as `print.vir_slot[]` with ids "254" and "255" and no `vt_tray`; each
extruder's `snow` in `print.device.extruder.info[]` packs the tray it holds as (ams id << 8) | slot, 0xFFFF for
none, and the capture showed the left extruder (id 1) at 0xFE00 = 254 and the right one (id 0) at 0xFF00 = 255.
So 254 is the left external spool and 255 the right one. AMS slots are `print.ams.ams[].tray[]`. Every tray
carries `tray_type` (material), `tray_color` (RRGGBBAA), `tray_info_idx` (Bambu's filament id: GFG99 Generic
PETG, GFG00 Bambu PETG Basic, GFA00 Bambu PLA Basic), `tray_sub_brands`, `remain` and `k`. The installed
filament presets carry the same id as `filament_id` (inherited from the `@base` preset), so a tray resolves to
the exact preset name for the machine preset in use (GFG99 + `Bambu Lab X2D 0.4 nozzle` -> `Generic PETG @BBL
X2D 0.4 nozzle`); the Generic spool chosen by hand in Studio did carry its id. A tray without an id falls back
to the Generic preset of its material. Single-nozzle firmware reports one external spool as `vt_tray` and the
loaded slot as `ams.tray_now` (254 = external, 255 = none, else ams * 4 + slot).

Why this matters (`docs/errors/2026-10-16-loaded-filament-not-used.md`): a spec written for one spool passed
every check after the spool changed, Studio offered a swap on open, and the swap reset the process to the stock
preset without a word (5 h 08 against 6 h 34 for the same part). `check` and `build` now read the trays before
resolving the spec: `"preset": "loaded"` takes the spool (an unloaded nozzle is a problem that names the fitted
spool, never a guess), a pinned preset that differs or cannot be compared is a FAIL unless
`--allow-filament-mismatch` (which keeps the colour rule), a pinned colour gives way to the spool colour unless the
spec has a `colour_note`, the nozzle compared follows `filament_map` and the machine preset's extruder count, and
with the printer unreachable the GUI presets stand in after a WARNING on the first lines of the report, with the
project's filaments marked unverified.

Pitfalls: Studio holds UDP 2021 (SSDP) while it runs, so discovery probes 8883 instead (`printer discover
--ssdp 10` listens with SO_REUSEPORT when needed). Newer firmware may need LAN-only mode for third-party MQTT;
status pushes are exempt from Bambu's Authorization Control, so no Developer Mode is needed (reference 04).

## 9. Where the evidence lives

| File | What | Read when |
|---|---|---|
| `01-source-cli.md` | The CLI source at tag v02.08.02.61: flags, the assemble list, exit codes | changing `studio.py`, `assemble.py`, `build.py` |
| `02-binary-crash.md` | The shipped binary, crash reports, the AppKit alert, option keys | a headless run hangs or crashes |
| `03-3mf-format.md` | The 3MF entries, per-plate and per-object metadata, the 188 process keys | changing `threemf.py`, `patches.py` |
| `04-app-printer-control.md` | MQTT, SSDP, FTPS, Authorization Control, the GUI's accessibility tree | changing `printer.py` |
| `05-bambu-cli-ideas.md` | Other tools' approaches (davglass, printago) | adding a command |
| `06-slicer-rules.md` | The print rules behind every recipe, with sources and numbers | choosing or adding a recipe |
| `07-print-recipes.md` | Key scopes, defaults, the tip table | adding a recipe or a per-object key |
| `08-optimisation.md` | Orientation, print-by-object source rules, supports, time, finish, headless metrics, the GUI workflow, ClassCAD | changing `mesh.py`, `layout.py`, `sweep.py`, or explaining a choice |
| `09-user-notes.md` | The user's own finish and fit notes (personal tools, materials) | the hand-back for this user |
