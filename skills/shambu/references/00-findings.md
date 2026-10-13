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

## 3. Presets

The CLI never resolves `inherits` or `include`. A half-resolved machine preset gives a project with the generic
577-character start G-code instead of the X2D's 14848-character template: never print G-code from such a file.
`PresetResolver` merges both (reference 01).

## 4. CLI flags

Bool flags take no value (`--min-save 1` makes `1` an input file). `--min-save` strips all meshes. `--orient 1`
overwrites every object's settings with the process preset. `--rotate*` turns every object at once. `--pipe` does
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

## 7. The Studio GUI

Accessibility exposes the menus (export is `File > Export > Export plate sliced file`, Cmd G) but not the
toolbar; Shift Cmd G is Print plate, so the tool never sends keystrokes. The only safe GUI action is
`open -a BambuStudio file.3mf` (reference 04).

## 8. The printer (read-only)

`shambu printer status` reads `BambuStudio.conf` for the selected serial and its LAN access code, finds the
printer by probing the ARP hosts on TCP 8883 (or `--ip`), connects with TLS (Bambu's bundled CA, then unverified
as a fallback), subscribes to `device/<serial>/report`, publishes one `pushall`, and prints state, percent,
layer, remaining time and end time, both nozzles (id 1 = main left, id 0 = auxiliary right), bed and chamber,
HMS codes with the offline text from Studio's `hms_en_20P.json`. `watch` polls until the state leaves
RUNNING/PAUSE (the state after a finished print is FINISH). The access code never leaves memory. Nothing but
`pushall` is ever published; starting, pausing or stopping stays with the user.

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
