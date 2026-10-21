# 00. What the reverse engineering found (Studio 02.08.02.61)

The rules below are the reason `shambu` exists as a CLI instead of a page of `BambuStudio` flags. The code
encodes every one of them; this file is the short list, and the numbered references hold the evidence with
file:line citations. Read this before changing `studio.py`, `build.py`, `assemble.py`, `threemf.py`, `layout.py`,
`tower.py`, `edits.py` or `calibrate.py`.

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
10. The 0.4.0 rules, one paragraph each: clearance, spread estimate, probe, line allowance and pair rule, bed and
    reach, silhouettes, packer, prime tower, interface filament, nozzle stats, two-pass `--no-check`, project edits,
    the escape hatch, the time estimate, the watch log, calibration

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
- Exit -139 can also be a one-off crash: on the plate-14 layout with the prime tower at the front (128, 2), one
  of four identical slices crashed inside tree support generation and the three re-runs passed (2026-10-21).
  Rule: rerun a -139 once with the same command; a second -139 is reported with the log path, not worked round
  (reference 02 section 2.7).

**The prime tower position is a per-plate vector (0.2.1).** `wipe_tower_x` and `wipe_tower_y` hold one value per
plate. Passed as a scalar on the command line, the value lands on plate 1 only and every later plate keeps the CLI
default (15, 220), inside the right nozzle's dead strip (x < 20.5): plate 2 of an 8-plate two-filament project
failed with exit -102 and, with a part in that corner, exit -101 (organiser build, 2026-10-13). The CLI writes one
value per plate. A plate with a planned tower gets its own spot (section 10, rule 8); any other plate keeps
(208, 208).

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
| `03-3mf-format.md` | The 3MF entries, per-plate and per-object metadata, the 188 process keys, re-slicing a CLI export, the edit verbs | changing `threemf.py`, `patches.py`, `edits.py` |
| `04-app-printer-control.md` | MQTT, SSDP, FTPS, Authorization Control, the GUI's accessibility tree, the watch log | changing `printer.py`, `calibrate.py` |
| `05-bambu-cli-ideas.md` | Other tools' approaches (davglass, printago) | adding a command |
| `06-slicer-rules.md` | The print rules behind every recipe, the prime tower, Studio's estimate against the printer | choosing or adding a recipe, `tower.py` |
| `07-print-recipes.md` | Key scopes, defaults, the tip table | adding a recipe or a per-object key |
| `08-optimisation.md` | Orientation, print-by-object source rules, supports, time, finish, headless metrics, the GUI workflow, ClassCAD | changing `mesh.py`, `layout.py`, `sweep.py`, or explaining a choice |
| `09-user-notes.md` | The user's own finish and fit notes (personal tools, materials) | the hand-back for this user |

## 10. The 0.4.0 rules, one paragraph each (layout, tower, re-slicing, edits, time)

Evidence: the plate-14 post-mortem runs (X2D, PETG parts, PLA matte interface, 2026-10-20 to 2026-10-21), the seed
build, two watched prints, and the Studio source at tag v02.08.02.61. Details: reference 08 section 11 (layout),
06 sections 5, 10 and 11 (interface, tower, time), 03 section 8 and "Project edits", 04 Track 7 (watch log).

1. **Studio has no clearance rule between objects before slicing.** `validate()` tests hulls against the exclude
   area only, and a tower that is too close is a warning (Print.cpp:944-1050, 1003-1045). After slicing, Studio
   refuses two objects whose extrusion centrelines cross in one layer: exit -101, with the two CLI object names
   and no z in the log (GCode/ConflictChecker.cpp:16-17, 203-221, 289-314; BambuStudio.cpp:6971-6972). So the
   slice is the binding clash test, and `check` is a pre-flight. The old rule (0.25 x height, 5..25 mm, on all
   four sides, summed between neighbours, held off the bed edge, boxes only) refused a five-part plate that
   sliced and printed (18 h 0 min, 480.7 g).

2. **Support spread is an estimate unless measured.** Tree support grows per object against its own outlines and
   the bed border only: it does not avoid a neighbour (Support/TreeSupport3D.cpp:5232-5246,
   TreeModelVolumes.cpp:128-133). Measured on the printed plate: 20.9 mm on one side of a 218 mm housing (z 0..5),
   0 to 7.1 mm everywhere else; 15.6 mm under a 170 mm bracket. The estimate is tree
   `min(26, max(8, 0.22 x height))`, normal 5 mm, raft 5 mm (or `raft_first_layer_expansion`), on all four sides,
   because the mesh does not tell the side. "Has support" is Studio's predicate: `enable_support`,
   `enforce_support_layers > 0` or `raft_layers > 0` (Print.hpp:492-494; `layout.needs_support`). The spec
   overrides it per side (`support_spread`) or with `brim_margin` (brim and support together).

3. **`--probe-supports` measures the spread.** A solo `--slice --no-check` of each supported single-file part in
   its final orientation, at the bed centre because Studio clips trees at the border (TreeSupport.cpp:626-629),
   with no prime tower. It keeps the support centreline cells (0.5 mm) as a mask, so a notch is seen. The cache
   key holds the resolved machine, filaments, process, slot, map, bed type and Studio version, under
   `<outdir>/.probe/`. Live on the plate-14 spec: five fresh probes 12.5 to 14.7 s, a cached rerun 0.4 s, and no
   WARN or FAIL with the masks.

4. **The pair rule takes the larger spread, not the sum, plus a 1 mm line allowance.** A footprint gap under
   1.0 mm (`layout.LINE`, about two line widths) is `FAIL body overlap`. The 1.0 mm is shambu's policy: Studio
   refuses collinear overlap above 0.01 mm whatever the roles, and only support-on-support crossings get a
   100 mm threshold (ConflictChecker.cpp:291-310). Otherwise each part's outline is grown by its spreads and tested
   against the whole neighbour. Short against an estimate: `WARN margin rule` ("the slice decides"). Short
   against a probe or a spec value: `FAIL margin rule`. Brims leave the pair rule: Studio clips the brims of
   different objects against each other (ConflictChecker.cpp:163-200, Brim.cpp:258-365).

5. **The bed and the reach.** Body and normal-support spread stay inside the bed (FAIL); tree spread does not
   count, because Studio clips it to the bed (TreeSupport.cpp:607-629, 2507-2522); a raft keeps its margin
   (TreeSupport.cpp:1591-1617). A brim past the bed edge or a nozzle's reach is `WARN brim`: Studio clips it
   (Brim.cpp:1011-1045, 1079-1095, 1129-1138). The right nozzle's reach (x >= 20.5, and `aux_x_limit`) counts the
   full spread, trees too: Studio refuses an extruder's path outside its area with -102
   (GCodeProcessor.cpp:1887-1905, BambuStudio.cpp:7202-7205).

6. **Footprints are silhouettes.** The mesh's XY silhouette after orientation, rasterised at 0.5 mm (0.015 s on an
   11k-facet part), never rotated after the fact: a 50 x 226 bar turned 45 degrees is 11,300 mm2, not 195 x 195. A
   convex hull would refuse the printed plate: the feeler's hull covers the cradle that sits between its end
   blocks (box gap -3.7 mm, hull 0.0, real 4.95 mm). A mesh that does not rasterise falls back to its box with a
   WARN.

7. **The packer is bottom-left, 0 degrees only, and says when it gives up.** `BottomLeftPacker` tries candidate
   points at placed outlines' edges and internal corners, lowest top edge first. It fits the five post-mortem
   parts on one plate (the row packer needed two). A part that fits nowhere gets one `PLACEMENT NOT DONE` line
   with the arithmetic, is never checked at a position nobody chose, and never exports, even with
   `--keep-going`. shambu never runs Studio's arrange, which keeps 24 mm between objects when any part has tree
   supports and exits -21 when they do not fit (ModelArrange.cpp:158-177, Arrange.cpp:84-136,
   BambuStudio.cpp:5477-5485): it did on these five parts, with and without rotations. `count` copies are separate
   model objects, so each one is clash-tested (ConflictChecker.cpp:226, 244-245; PrintApply.cpp:140-160).

8. **The prime tower is sized by the tallest part and placed where both nozzles reach.** Studio's estimate
   (PartPlate.cpp:2191-2265; WipeTower.cpp:1587-1630; footprint BambuStudio.cpp:4462-4488): 41.39 mm square plus an
   8 mm brim at 218 mm with the rib wall (Studio's own log), 34.9 mm plus 2.6 mm at 32 mm. The spec key
   `wipe_tower` per plate is `"auto"` (default with two or more filaments by layer), a spot
   `{x, y, width, rib_wall, brim}` (x, y = min corner), or `false` (WARN). Width, rib wall and brim are one value
   per project. `auto` searches a 1 mm grid inside both nozzles' reach, clear of every silhouette and support
   spread; it tries Studio's size (rib wall) first, then the rib wall off at 35, 30, 25, 22 and 20 mm with a 2 mm
   brim (`tower.candidates`), or FAILs naming the blocker. A narrower tower is faster: on plate 14 a 22 mm tower
   without the rib wall saved 1 h 43 min and 32 g against the 35 mm one (n=1). It stays an option
   (`wipe_tower.width` 22), not the default, because a 218 mm tower that narrow without a rib wall is a stability
   risk. A one-filament plate with Smooth timelapse needs a tower too (PartPlate.cpp:2222-2223).

9. **A process-level interface filament makes the plate two-filament.** `dual-material-interface` sets
   `support_interface_filament 2` in the process. Studio counts only part filaments in its own log
   (BambuStudio.cpp:1338-1343), and so did shambu: the seed build placed a cradle at x 9.2 and Studio exited -102
   (right-nozzle interface at x 14.3). `effective_filaments` counts the process keys, and `ExtruderReach` holds
   the interface extruder to x >= 20.5 plus the interface spread (brim margin plus 3 mm; measured 2.2 mm, n=1).

10. **Every export must be re-sliceable: the nozzle stats.** An export without `--slice` writes
    `extruder_nozzle_stats: []` (filled only in the slice loop, BambuStudio.cpp:6648-6686). A later CLI run takes
    the present key as authoritative, counts 0 nozzles and throws "No valid nozzle found. Please check nozzle
    count." (BambuStudio.cpp:4101-4156, ToolOrdering.cpp:1477-1481), mapped to -100 (exit 156) with the text on
    stderr only (BambuStudio.cpp:7413-7418). It was the post-mortem's exit 156: one empty key, not space and not
    the second filament (a one-filament plate of the same export failed the same way). `RepairNozzleStats`
    writes `Standard#1` per extruder and rebuilds `filament_extruder_variant` as one block per filament, paired
    with `filament_self_index` (PrintConfig.cpp:8022-8052; a miss logs `could not found extruder_type`,
    PrintConfig.cpp:8935). `build` applies it, `patch --repair-nozzle-stats` fixes an older file, and verify
    refuses a file that still has `[]` or `#0`.

11. **`--no-check` is a second pass, per plate, and only after a skippable failure.** `--no-check` skips the
    conflict check (-101), the support-necessity check and the -100 exit on empty-layer or overlap warnings;
    -102 to -105 and validate errors still exit (BambuStudio.cpp:6192, 6418, 6974, 7185-7212; Print.cpp:2418-2435).
    Studio stops at the first conflicting plate (BambuStudio.cpp:7105-7109), and -100 is also the generic
    exception exit, so only a -100 whose log has `found slicing warnings:` is skippable. `build --slice --no-check`
    and `shambu slice --no-check` slice checked first and add the unchecked pass only then; the report lists
    each `Checks skipped:` line. A `--no-check` slice is not a clash test: a tree gap-0 project exits 0 with
    crossing paths. At a 0.25 mm gap the same file and argv exited 0 in 2 of 4 checked slices, so near-touching
    parts prove nothing either way.

12. **Project edits keep everything else byte-identical.** An object's position is the build item transform
    composed with the component transform (bbs_3mf.cpp:4396, 4439); `--move X,Y[@PLATE]` sets the plate-local min
    corner (plate offsets PartPlate.cpp:4536-4548). `--replace-mesh` follows the GUI's "replace with STL": the two
    STL frames coincide (Plater.cpp:11302-11411), each sub-mesh is re-centred on its box (bbs_3mf.cpp:5366), and a
    mesh shared between volumes is refused (bbs_3mf.cpp:7353-7367). `--add-filament` appends slot N+1 from a
    scratch export loaded with the new preset alone, because `--load-filaments` keeps the project's count and
    writes the loaded block first (BambuStudio.cpp:2036; per-variant keys PrintConfig.cpp:7464; one-entry vectors
    Config.hpp:681-685); a preset with no colour takes the next unused default colour with a WARN that names it,
    and a verify slice of plate 1 runs. `--tower X,Y[@PLATE]` writes the per-plate min corner
    (BambuStudio.cpp:4481-4486). `--move`, `--tower` and `--replace-mesh` report `check`'s pair rule on
    silhouettes. Every patch declares the entries it touches, and `Project.save()` refuses any other change.
    Live: a replaced housing kept its min corner (44.211, 26.532); an added filament left slot 1 identical in all
    580 keys and sliced to the same 18 h 0 min, 480.7 g.

13. **The escape hatch is logged.** `shambu studio -- ARGS` runs one raw Studio command under the builder rules and
    writes `studio-runs/<UTC stamp>/` (command.json, the log, result.json, exit.txt). Its output is unverified
    until `shambu inspect` reads it. The skill runs it only after the user says yes.

14. **The time is Studio's prediction, uncalibrated.** `prediction` in `slice_info.config` is the G-code header
    total. Studio simulates moves against the machine limits and adds fixed times: `M109` and `M190` count 0 s
    (GCodeProcessor.cpp:5429-5432, 5478-5483), calibration commands have no handler and count 0 s
    (GCodeProcessor.cpp:1676-1736), and `G29` adds 260 s only inside an `M622 J1` block
    (GCodeProcessor.cpp:5220-5229). No code path scales the result per printer. Two measured prints disagree:
    0.75 to 0.80 x Studio (bracket D.7, PETG, standard-flow HS01 nozzle, speed level not logged) and 1.05 x (E7a
    cradle). So every hand-back says "Studio estimate".

15. **The watch log has its own clock and no identifiers.** The printer report carries no start time and no
    total (Track 7: `mc_remaining_time` DeviceManager.cpp:3081, `mc_percent` :3060, `layer_num` :3199, `spd_mag`
    :3304, `stg_cur` :3324). `printer watch --log FILE --gcode3mf PLATE.gcode.3mf [--wait-for-start]` stamps each
    poll (`read_at`), logs the speed level and stage, polls every 60 s near the start and the end, and writes a
    header with the file's name and its M73 track. The log never holds the IP, the serial or the access code;
    `status` shows only the masked serial, on stdout.

16. **`shambu calibrate` fits a rate and a startup offset.** The rate is real minutes per Studio minute from
    layer 11 on; the offset is the startup the rate does not explain. A print qualifies when it finished, the log
    saw its start, every poll was at Standard speed and 100 %, the printer's remaining time stayed within 5 % of
    M73 R, and it never paused. The hand-back shows "measured prints ran at A to B x Studio (n=N)", and
    "calibrated about H h M min (n=N)" only with 3 qualifying prints of one printer model and material whose rates
    span less than 0.15. `--add` stores a fingerprint of the session, so one log imported twice is one print.
