# Post-mortem: shambu projects lose their process when Bambu Studio opens them (2026-10-29)

Scope: every use of shambu 0.5.1 (installed CLI, `uv tool`, git tag v0.5.1, commit 5efeabd; plugin skill 0.5.1) on the
sweeper v1 plates in Claude Code session `8d0c87de` on 29 October 2026, 17:13 to 21:11 local time, against Bambu Studio
02.08.02.61 on macOS and the X2D printer `20P…277`. Written for the agent that fixes shambu. Every claim carries a tag:
[V] read from a file named here, [S] seen on a screenshot kept here, [E] estimate or hypothesis, [Calc] arithmetic.

Shav's requirement, verbatim: "I WANT to see it in the Bambu studio. I don't trust it yet to just send, wait 9 hours to
realise Shambu was wrong. I need it to work well with the GUI so I can do my final pass check." So the fix target is:
**a shambu project opened in the Studio GUI must show the process, filaments, supports and sliced result shambu built,
and survive the user's final-pass edits without silently reverting.** "Send the file to the printer without Studio" is
NOT an acceptable workaround; the assistant proposed it twice this session and Shav rejected it.

Evidence folder: `content/drafts/openarm/cad/sweeper-v1/plates/` (this file's parent), screenshots in
`postmortem/screenshots/` (file names below are the session image numbers). Session transcript: Claude Code session
`8d0c87de-7d07-454b-8e00-594d68ea9d24` (`cairn` export of this session when synced).

## 0. Verdict

1. **Root cause, one mechanism.** shambu writes the plate's process as a *named user preset* ("PETG sweeper 4 walls 25 pc
   gyroid, PLA interface", `from: User`, `inherits: 0.20mm Standard @BBL X2D`) and leaves the project's preset bookkeeping
   empty: `different_settings_to_system = ["", "", "", ""]`. Studio's GUI does not have a preset of that name installed, so
   on open it replaces the process with the installed default (`default_print_profile` = `0.20mm Standard @BBL X2D`) and,
   because the bookkeeping says nothing differs from system, it discards every inline override: wall loops, infill, bottom
   shells, the support interface filament, the support style, the filament map mode. The per-object settings in
   `Metadata/model_settings.config` are NOT touched, which is why the user's per-object edits survived while the global
   ones vanished. [V: section 3 config diffs; E for the exact Studio code path, which nobody has read]
2. **Every visible symptom follows from that one loss.** The "only PETG on the plate, auxiliary nozzle empty" grouping
   dialog (filament 2 was still in the project; it was simply no longer used once `support_interface_filament` went back
   to 0). The re-slice at 157 g / 5 h 02 and 162.5 g / 5 h 22 instead of 212 to 216.8 g / 6 h 30 to 6 h 36 (2 walls and
   15 % grid instead of 4 walls and 25 % gyroid). The "Slice plate" button lit on a file that was exported sliced. The
   filament map mode falling from `Manual` to `Auto For Flush`. [V, S]
3. **shambu's own docs recorded the behaviour on 16 October and did not fix it.** `docs/errors/2026-10-16-loaded-filament-not-used.md`
   and the skill reference 00-findings.md line 130: "Studio offered a swap on open, and the swap reset the process to the
   stock preset without a word (5 h 08 against 6 h 34 for the same part)". The skill then added a rule about filaments, and
   the process reset itself stayed. [V]
4. **Three secondary faults** made the recovery slower: the dual-material interface recipe is process-scoped, so it dies with
   the process (the GUI writes the same keys per object, where they survive); `shambu patch --set` cannot change a process
   key, only object keys; and the hand-back text promises "open, confirm, Print" without saying that any change in the GUI
   re-slices on the stock preset. [V]
5. **Two assistant errors, not shambu's.** Combining `snug-normal-supports` with the PLA interface instead of Bambu's own
   tree-hybrid set (cosmetic), and recommending the SD-card upload as the fix (rejected by the user). Recorded in section 6.

## 1. Timeline

Local time (BST), from file mtimes (`ls -laT`), screenshot mtimes and the session. Shambu commands are quoted as run.

| Time | Who | What | Evidence |
| --- | --- | --- | --- |
| 17:13 | shambu | `shambu build plate.json --slice` (all PETG, `petg-supports`): exit 0, 5 h 41 min, 194.5 g. `shambu open` | `sweeper-v1-left.report.md` (first version, overwritten at 19:22), `shambu/` dir mtime 17:13:32 |
| 17:16 | Shav | moves parts in Studio; warning "object body_print has floating cantilever" | screenshot `7.png` (a real geometry fault, fixed in CAD rev S1.1f; not a shambu fault) |
| 17:17 | Shav | saves the project in Studio over the shambu file: 153 KB, unsliced | `shambu inspect` at 17:2x printed `process 0.20mm Standard @BBL X2D; map ['1','2'] (Auto For Flush)`; the positions were his |
| 17:26 | assistant | puts Shav's six positions into `plate.json` as `at`; `shambu check` all OK | `plate.json` mtime 17:26:15 |
| 19:22 | shambu | rebuild on CAD S1.1g with `snug-normal-supports` + `petg-supports` on cap and vane: 6 h 30 min, 212.3 g, support 3.9 g | `sweeper-v1-left.gcode.3mf` 4.9 MB 19:22:02, `sweeper-v1-left.report.md`, `sweeper-v1-left.log` |
| 20:39 | shambu | `shambu build plate-pla-interface.json --slice` (process recipes + `dual-material-interface`, cap and vane `snug-normal-supports`, tower auto): exit 0, 6 h 36 min, 216.1 g, PLA 1.2 g, 6 changes, tower 39 x 28.4 at (81, 195) | `plate-pla-interface.json` 20:39:39, `sweeper-v1-left-pla-interface.report.md` 20:39:47, `.log` 20:39:46, `plate_1.gcode` 21.4 MB 20:39:45, `result.json`, `shambu/process.json`, `shambu/assemble.json` |
| 20:39 | shambu | `shambu open sweeper-v1-left-pla-interface.gcode.3mf` | session |
| 20:42 | Shav | opens the Filament grouping dialog, chooses Custom; Studio re-slices: 157.28 g, 5 h 02, main nozzle PETG only, auxiliary empty | `8.png` |
| 20:44 | Shav | the grouping dialog shows Main Extruder: 1 PETG, Auxiliary: empty; "Slice plate" button active | `9.png` |
| 20:49 | Shav | per the assistant's GUI steps, sets the cap's and vane's support interface filament to 2 (PLA); Studio offers Bambu's PLA-on-PETG set; Yes | `10.png` |
| 20:50 | Shav | saves the project in Studio over the shambu file: 154 934 bytes, unsliced | `sweeper-v1-left-pla-interface.gcode.3mf` mtime 20:50:11; copied unchanged to `sweeper-v1-left-pla-interface-gui-saved.3mf` at 20:52 |
| 20:51 | Shav | slices in the GUI: 162.53 g, 5 h 22, PETG main, PLA auxiliary, tower present | `11.png` |
| 20:52 | shambu | `shambu slice sweeper-v1-left-pla-interface.gcode.3mf --settings-from plate-pla-interface.json`: exit 0, 6 h 36 min, 216.8 g, "the project's filaments kept", wrote `...resliced.gcode.3mf` | `sweeper-v1-left-pla-interface.resliced.slice-ntg9j2hq/` (log, result.json, plate_1.gcode 21.5 MB, process.json, machine.json; mtimes 20:52:52 to 20:52:54) |
| 20:53 | Shav | the earlier window still in front: 162.53 g | `12.png` |
| 20:54 | Shav | the resliced file opened (window title `...resliced.gcode`): Studio shows 162.53 g, 5 h 22, "Slice plate" greyed (sliced), i.e. it re-sliced on load with the stock process | `14.png` |
| 21:00 | Shav | sets Wall loops 4, infill 25 % gyroid, bottom shells 5 by hand, slices: 216.77 g, 6 h 37 | `15.png` |
| 21:07 | Shav | saves over the resliced file: 155 080 bytes, unsliced | `sweeper-v1-left-pla-interface.resliced.gcode.3mf` mtime 21:07:51 |
| 21:11 | Shav | preview check, prints | `16.png` |

Net cost: about 2 hours of Shav's evening, three re-slices, two assistant explanations that were wrong, and the print
started 4 hours after the first plate was ready.

## 2. What shambu wrote (the export) and what Studio kept (the save)

Three project files, all in the plates folder. Only `sweeper-v1-left.gcode.3mf` (19:22, all PETG) is an untouched shambu
export: it still carries `Metadata/plate_1.gcode` and `plate_1.gcode.md5`. The two others were saved by Studio over
shambu's files and have no G-code entry (Studio drops it on save when the plate is invalid). [V: zip listings]

```
sweeper-v1-left.gcode.3mf                         shambu export, sliced   Metadata: plate_1.gcode, plate_1.gcode.md5, project_settings.config, model_settings.config, slice_info.config (full), ...
sweeper-v1-left-pla-interface-gui-saved.3mf       Studio save 20:50       Metadata: no gcode; slice_info.config holds only the header
sweeper-v1-left-pla-interface.resliced.gcode.3mf  Studio save 21:07       same shape as above, plus different_settings_to_system filled by the GUI
```

### 2.1 `Metadata/project_settings.config`, the keys that decide it

Read with `python3 -I -c "import zipfile,json; ..."` on each file (the exact script is in the session; it reads the
JSON and prints the keys below).

| Key | shambu export (19:22) | Studio save 20:50 | Studio save 21:07 (after Shav's manual walls) |
| --- | --- | --- | --- |
| `print_settings_id` | `PETG sweeper 4 walls 25 pc gyroid` | `0.20mm Standard @BBL X2D` | `0.20mm Standard @BBL X2D` |
| `different_settings_to_system` | `["", "", "", ""]` | `null` (key absent) | `["bottom_shell_layers;skeleton_infill_density;skin_infill_density;sparse_infill_density;sparse_infill_pattern;wall_loops", "", "", ""]` |
| `inherits_group` | `["0.20mm Standard @BBL X2D", "", "", ""]` | `null` | `null` |
| `wall_loops` | `4` | `2` | `4` |
| `sparse_infill_density` | `25%` | `15%` | `25%` |
| `sparse_infill_pattern` | `gyroid` | `grid` | `gyroid` |
| `bottom_shell_layers` | `5` | `3` | `5` |
| `brim_type` | `no_brim` | `auto_brim` | `auto_brim` |
| `support_interface_filament` | `0` here; `2` in the 20:39 export per `shambu/process.json` | `0` | `0` |
| `filament_map_mode` | `Manual` | `Auto For Flush` | `Auto For Flush` |
| `filament_map` | `["1", "2"]` | `["1", "2"]` | `["1", "2"]` |
| `filament_settings_id` | both filaments | both filaments | both filaments |
| `filament_nozzle_map` | `["1"]` (one entry for two filaments) | `["1", "0"]` | `["1", "0"]` |
| `filament_volume_map` | `["0"]` (one entry) | `["0", "0"]` | `["0", "0"]` |
| `extruder_ams_count` | `[]` | `["1#0|4#0", "1#0|4#0"]` | same |
| `filament_map_2` | `["1"]` (shambu-only key) | absent | absent |
| `wipe_tower_x`, `wipe_tower_y` | `["208"]`, `["208"]` (all-PETG file, no tower) | `["81"]`, `["195"]` | same |

Read it as a fingerprint: **Studio keeps modifications of a known preset only when `different_settings_to_system[0]`
names the modified keys**. Shav's manual edit produced exactly that list; shambu's export produced an empty string and
a preset name Studio has never heard of. Whether Studio first fails the name lookup and then trusts the empty
bookkeeping, or applies the bookkeeping regardless of the name, is the one thing to establish by experiment (section 7).

The 28 keys that differ between the shambu export and the 20:50 save, for completeness: `bottom_shell_layers`,
`brim_type`, `default_filament_colour`, `enable_overhang_bridge_fan`, `enable_pressure_advance`, `extruder_ams_count`,
`filament_change_length_nc`, `filament_cooling_before_tower`, `filament_dev_ams_drying_ams_limitations`,
`filament_dev_ams_drying_temperature`, `filament_dev_ams_drying_time`, `filament_flush_temp_fast`, `filament_map_mode`,
`filament_nozzle_map`, `filament_volume_map`, `first_x_layer_part_fan_speed`, `ironing_fan_speed`,
`overhang_threshold_participating_cooling`, `pre_start_fan_time`, `pressure_advance`, `print_settings_id`,
`retraction_distances_when_ec`, `slow_down_min_speed`, `sparse_infill_density`, `sparse_infill_pattern`, `wall_loops`,
`wipe_tower_x`, `wipe_tower_y`. Keys only in the shambu export: `compatible_printers_condition`,
`different_settings_to_system`, `filament_map_2`, `inherits_group`. Keys only in the Studio save: `filament_colour_type`,
`filament_multi_colour`, `host_type`, `printhost_authorization_type`, `printhost_ssl_ignore_revoke`. [V]

### 2.2 How shambu composed the process (why the name is unknown to the GUI)

`plates/shambu/process.json` is the preset shambu fed to the Studio CLI (`--load-settings machine.json;process.json`,
command line quoted in `sweeper-v1-left-pla-interface.log` line 1). Its head: [V]

```
name                        = "PETG sweeper 4 walls 25 pc gyroid, PLA interface"
inherits                    = "0.20mm Standard @BBL X2D"
from                        = "User"
type                        = "process"
print_settings_id           = "PETG sweeper 4 walls 25 pc gyroid, PLA interface"
is_custom_defined           = "0"
different_settings_to_system = null        (not set by shambu)
wall_loops 4, sparse_infill_density 25%, sparse_infill_pattern gyroid,
support_interface_filament 2, enable_support 1, support_type tree(auto), support_style tree_hybrid,
support_on_build_plate_only 0, support_top_z_distance 0, support_interface_spacing 0, support_interface_pattern rectilinear_interlaced
(204 keys)
```

The code that does it: `~/Code/shambu/src/shambu/presets.py` lines 128 to 146 (commit 5efeabd): "Any override or a new
name turns the preset into `from: User` with `inherits` = the system" preset; `d["name"] = new_name or f"{name} (modified)"`,
`d["from"] = "User"`, `d["inherits"] = name`, `d["is_custom_defined"] = "0"`, and `d[ID_KEY[kind]] = d["name"]`. Nothing in
`src/shambu/` writes `different_settings_to_system` for a build (the only references are in `edits.py` lines 90, 1692 and
1803, where `patch` preserves or resizes the array). So the CLI export carries the resolved values and a user-preset name
that exists only inside the 3MF. The GUI's library (`~/Library/Application Support/BambuStudio/user/<id>/process/`) has
no such preset. [V: grep of `src/shambu`, `presets.py`]

Note the all-PETG export (19:22) carries `inherits_group[0] = "0.20mm Standard @BBL X2D"`, so Studio did know the base
preset; it still dropped the values. [V]

### 2.3 What survived: the per-object settings

`Metadata/model_settings.config` in the 20:50 save, object `cap_print`: `enable_support 1`, `support_interface_filament 2`,
`support_interface_pattern rectilinear_interlaced`, `support_interface_spacing 0`, `support_interface_speed 50,50,50,50`,
`support_on_build_plate_only 1`, `support_style tree_hybrid`, `support_threshold_angle 35`, `support_top_z_distance 0`,
`support_type normal(auto)`, `tree_support_branch_diameter 3`, `tree_support_branch_diameter_angle 7`, `raft_first_layer_expansion 5`,
`brim_type outer_only`, `brim_width 5`, `brim_object_gap 0.1`, `extruder 1`. The object positions are Shav's from 17:17
(body min corner 7.882, 7.528; cap 148.656, 11.236; vane 96.453, 81.722; gates 118.895/131.11, 15.579/132.403,
189.145/137.688), unchanged through every save. [V: `shambu inspect` outputs in the session; the config]

shambu writes its per-object settings through `shambu/assemble.json` (`print_params` per object: `brim_type`,
`enable_support`, `support_type`, `support_style`, `support_on_build_plate_only`, `support_filament`, ...), and those
reached the GUI intact. **Object scope survives a GUI open; process scope does not.** That asymmetry is the lever for
the fix.

## 3. The faults, numbered

### F1. Process overrides are written where the GUI discards them (root cause)

- Symptom: open a shambu project in Studio, the process preset reads `0.20mm Standard @BBL X2D`, and wall loops, infill,
  bottom shells, brim type, support interface filament, support style and the filament map mode are the preset's, not
  the spec's. The sliced result is invalidated and the next slice runs at 2 walls / 15 % grid. [S 8, 9, 14; V 2.1]
- Where: `presets.py` 128 to 146 (the user-preset naming), and the absence of any writer for
  `different_settings_to_system` / a GUI-installable preset.
- Known since: `docs/errors/2026-10-16-loaded-filament-not-used.md` ("the swap reset the process to the stock preset
  without a word"), restated in the skill's `references/00-findings.md` line 130 and in the skill text's Never list
  ("Reuse a spec's filaments from an earlier day: Studio's swap on open resets the process to the stock preset"). The
  fix taken then was on the filament side only.

### F2. The dual-material interface lives in the process, so it dies with it

- `dual-material-interface` is a process recipe (`shambu recipes` prints `[process]`). Its keys
  (`support_interface_filament 2`, `support_top_z_distance 0`, `support_interface_spacing 0`,
  `support_interface_pattern rectilinear_interlaced`, `support_style tree_hybrid`, `support_threshold_angle 35`,
  `raft_first_layer_expansion 5`, `enable_support 1`, `support_on_build_plate_only 0`) are what Studio's own
  "Suggestion" dialog applies PER OBJECT when the user picks a different interface filament on an object [S 10], and those
  per-object keys survived the save [V 2.3]. shambu's process-level placement was the one that vanished.
- Consequence seen: the grouping dialog listed only PETG, because no object used filament 2 any more [S 8, 9].

### F3. No verb patches a process key

- `shambu patch --set` takes `OBJECT:KEY=VALUE` only (`shambu patch --help`); `--set wall_loops=4` fails with
  "bad patch option: not enough values to unpack" [V, session]. The only route was `shambu slice FILE --settings-from
  SPEC`, which re-applies the spec's process and wrote `...resliced.gcode.3mf` at 216.8 g; Studio then discarded that
  process again on open (F1) [S 14].

### F4. The hand-back promises what the GUI does not do

- `report.py` lines 284 to 303: "Open the project (`open -a BambuStudio ...`), confirm the plate type ... and the filament
  colours ... The plate is already sliced ... then Print. Re-slice in Studio after any change." On this machine the
  project opened unsliced or re-sliced on load, and "re-slice after any change" is exactly what reverted it. The
  hand-back template in the skill (section "Hand-back") carries the same text. The assistant repeated it twice.

### F5. Dual-nozzle vectors are written one entry short

- The shambu export carries `filament_nozzle_map ["1"]`, `filament_volume_map ["0"]`, `extruder_ams_count []` and a
  shambu-only `filament_map_2 ["1"]` for a two-filament project; Studio rewrites them to two entries on save [V 2.1].
  Reference `03-3mf-format.md` line 316 documents the one-entry CLI bug for `filament_colour` and `filament_map`; shambu
  fixed `filament_map` (two entries, `build.py` 1463 and 1509) and `filament_colour`, not these three. Whether a short
  vector contributes to the GUI's reset is untested [E]; it is cheap to fix and removes one variable.

### F6. `filament_map_mode` Manual does not survive the GUI

- shambu writes `Manual` and checks for it after export (`build.py` 1597 to 1608 fail a non-Manual mode). Studio's save
  shows `Auto For Flush` [V 2.1]. The checker is therefore testing something the GUI will undo; if the mode matters for
  the nozzle assignment, the GUI must be told through a route it keeps (the plate's `filament_maps` metadata,
  `threemf.py` 721 to 726 writes it) or the check should not claim it.

### F7. `shambu open` leaves the old window in front

- With a project already open, `shambu open` (`open -a BambuStudio FILE`) opened the new file in a second window behind the
  first; the user read the old window's 162.53 g as the new file's result [S 12]. Minor, but it cost one round.

## 4. What the assistant got wrong (so the fixing agent does not inherit it)

1. Chose `snug-normal-supports` on the plate only for the cap and the vane, with the PLA interface, instead of Bambu's
   tree-hybrid set that the GUI suggests [S 10]. The slice worked; the setting is not the tuned one.
2. Told the user twice to bypass Studio (Device tab upload, or "print the embedded G-code without touching anything").
   Rejected: the GUI final pass is a requirement. The fix must make the GUI path work.
3. Explained the empty auxiliary nozzle as "Studio dropped filament 2 on load". Wrong: filament 2 was in the project all
   along (`filament_settings_id` has two entries in every save); it was unused once the interface setting reverted.
4. Did not warn, before the first open, that any GUI change would re-slice on the stock preset, although the skill text
   carried the warning.

## 5. What worked, so it is not "fixed" by accident

- `shambu check`: packing, bed and reach checks, the overlap rule, Shav's positions as `at`, the support spread
  warnings ("the slice decides") all behaved; the warnings were right (the slice found no clash). [V reports]
- `shambu build --slice`: four exports, four verified reports, exit 0 each time; the shambu slice numbers matched the GUI
  slice to the gram once the GUI had the same settings (216.8 against 216.77 g; 6 h 36 against 6 h 37). [V, S 15]
- `shambu slice --settings-from`: re-applied the spec process onto a GUI-saved layout and kept the GUI's per-object edits
  and filaments. [V resliced report]
- `shambu inspect`: every diagnosis in this note came from it or from reading the 3MF it describes.
- The per-object recipes (`flat-no-brim`, `tall-outer-brim`, `no-supports`, `snug-normal-supports`) reached the GUI intact.

## 6. Fixes to investigate, with acceptance tests

The agent cannot drive the Studio GUI. Each fix therefore ends in a file-level assertion that Shav confirms once by opening
the file, after which `shambu inspect` on his save proves it.

F1a. **Write the process as a modified system preset, not a new user preset.** In the exported `project_settings.config`
set `print_settings_id` to the base system preset (`0.20mm Standard @BBL X2D`), keep `inherits_group[0]` = base, and fill
`different_settings_to_system[0]` with the `;`-joined list of every process key that differs from the base preset (compute
the diff against the resolved base; Studio's own list for Shav's edit was
`bottom_shell_layers;skeleton_infill_density;skin_infill_density;sparse_infill_density;sparse_infill_pattern;wall_loops`,
note the two derived infill keys it adds). Do the same for `[1]`, `[2]` (filaments) and `[3]` (printer) when a recipe
or the spec overrides them. Keep the readable recipe name elsewhere (a plate name or a comment key), not as the preset
id. Acceptance: open in Studio, the process shows `0.20mm Standard @BBL X2D (modified)` or equivalent, `wall_loops` reads
4 in the GUI and in the saved file, and the stored slice is NOT invalidated on open ("Slice plate" greyed, the shambu
grams shown). Then a GUI save re-read by `shambu inspect` shows the same `print_settings_id` and the list.

F1b. **Alternative or belt-and-braces: install the preset.** Write the composed process as a real user preset JSON into
`~/Library/Application Support/BambuStudio/user/<preset_folder>/process/<name>.json` (`doctor` already reads
`preset_folder` 2379583394) with `inherits` and the diff, so the GUI finds the name. Risk: Studio rebuilds that folder from
its cloud account; test whether a local file survives a Studio restart and a sync. Prefer F1a if it passes alone.

F2. **Make the interface recipe object-scoped.** Provide `dual-material-interface` (or a sibling `pla-interface`) as an
`[object]` recipe that writes the keys the GUI writes per object (section 2.3 list, including
`support_interface_speed 50,50,50,50`, `support_threshold_angle 35`, `raft_first_layer_expansion 5`,
`support_style tree_hybrid`), and keep only the plate-wide parts (prime tower, flush, `filament_map`) at process level.
Acceptance: after a GUI open and save, `shambu inspect` shows `support_interface_filament 2` on the supported objects
and the grouping dialog lists PLA on the auxiliary nozzle without any user action.

F3. **Add a process-level patch.** `shambu patch FILE --process-set KEY=VALUE` (and `--process-unset`), writing the value
into `project_settings.config` AND updating `different_settings_to_system[0]`. Acceptance: patch a GUI-saved file's
`wall_loops` to 4, open it in Studio, see 4.

F4. **Rewrite the hand-back and the skill.** The "Owner steps" and the hand-back `Next:` line must say what the GUI will do
on open under the current writer (until F1a lands): that the process preset shows the base name, which values to confirm
in the GUI (wall loops, infill, bottom shells, interface filament per object) before slicing or printing, and that any
change re-slices. After F1a: say the process shows "(modified)" and list the keys. Remove "Re-slice in Studio after any
change" as a bare instruction. Delete the SD-card bypass from any guidance.

F5. **Write full-length dual-nozzle vectors.** `filament_nozzle_map`, `filament_volume_map`, `extruder_ams_count` with one
entry per filament, matching what Studio writes (`["1","0"]`, `["0","0"]`, `["1#0|4#0","1#0|4#0"]` for this machine);
drop or document `filament_map_2`. Acceptance: a GUI save changes none of them.

F6. **Decide what `filament_map_mode` means.** Either write `Manual` only when the GUI keeps it (test), or stop failing the
export check on it and set the plate's `filament_maps` instead. Acceptance: `shambu inspect` on a GUI save shows the
mode shambu intended, or the check no longer claims it.

F7. **`shambu open` focus.** Use `open -a BambuStudio --new` or raise the window with AppleScript after opening, or print
"Studio already has a project open; the new one is behind it" when a BambuStudio process exists (`doctor` already reads
`running_pids`). Acceptance: the opened file's name is in the front window's title.

F8. **A GUI round-trip verifier.** `shambu verify FILE.3mf --against plate.json` that reads a GUI-saved file and reports
every process, filament, object and position value that differs from the spec, so Shav's final pass has a check behind
it. This is the tool-side half of "I need it to work well with the GUI": the user edits, shambu tells him what changed.

## 7. The experiments, in order (each about five minutes, one Studio open per row)

1. Take `sweeper-v1-left.gcode.3mf` (shambu export, sliced). Edit `project_settings.config` ONLY to set
   `print_settings_id = "0.20mm Standard @BBL X2D"` and `different_settings_to_system[0] = "wall_loops;sparse_infill_density;sparse_infill_pattern;bottom_shell_layers;brim_type"`
   (through `shambu patch` once F3 exists, or a scratch script for the experiment). Open in Studio. Does the GUI show
   4 walls? Is the slice kept? Save, `shambu inspect`. This isolates F1a.
2. Same file, unmodified, but with the preset name installed as a user preset JSON (F1b). Open. Same questions.
3. Same file with F5's vectors fixed and nothing else. Open. Did anything change? (Expect no.)
4. A two-filament build with the interface keys per object (F2) and the process at base + diff (F1a). Open; grouping
   dialog must show PLA on the auxiliary with no user action; save; `shambu inspect` must show the interface keys on
   the objects and `wall_loops 4`.

Shav opens the files; the agent prepares them and reads the saves. Do not ask Shav to "just send it to the printer".

## 8. Evidence index

Paths relative to `content/drafts/openarm/cad/sweeper-v1/plates/` unless absolute.

| What | Path |
| --- | --- |
| Spec, all-PETG, Shav's positions | `plate.json` (17:26) |
| Spec, PLA interface | `plate-pla-interface.json` (20:39) |
| shambu export, all PETG, sliced, untouched | `sweeper-v1-left.gcode.3mf` (19:22, 4.9 MB), `sweeper-v1-left.report.md`, `sweeper-v1-left.log` |
| shambu's composed presets for the 20:39 build | `shambu/process.json`, `shambu/machine.json`, `shambu/filament-1.json`, `shambu/filament-2.json`, `shambu/assemble.json`, `shambu/spec.json` |
| shambu's 20:39 report and log (the export itself was overwritten at 20:50) | `sweeper-v1-left-pla-interface.report.md`, `sweeper-v1-left-pla-interface.log` (line 1 is the Studio CLI command), `result.json`, `plate_1.gcode` (21.4 MB, not in git) |
| Studio's save of that project at 20:50 (unsliced, process reset, per-object interface kept) | `sweeper-v1-left-pla-interface-gui-saved.3mf` (identical bytes to `sweeper-v1-left-pla-interface.gcode.3mf`) |
| shambu's `slice --settings-from` run (216.8 g) | `sweeper-v1-left-pla-interface.resliced.slice-ntg9j2hq/` (log, `result.json`, `plate_1.gcode` 21.5 MB, `process.json`, `machine.json`); the 3MF it wrote was overwritten by Studio at 21:07 |
| Studio's save at 21:07 after Shav's manual process edits (the `different_settings_to_system` fingerprint) | `sweeper-v1-left-pla-interface.resliced.gcode.3mf` |
| Screenshots | `postmortem/screenshots/7.png` (17:16 cantilever), `8.png` (20:42, 157 g PETG only), `9.png` (20:44 grouping dialog), `10.png` (20:49 Bambu suggestion), `11.png` (20:51, 162.53 g with PLA), `12.png` (20:53 old window), `14.png` (20:54 resliced file shows 162.53 g), `15.png` (21:00, 216.77 g after manual walls), `16.png` (21:11 preview) |
| shambu source | `~/Code/shambu` at 5efeabd: `src/shambu/presets.py` 128 to 146, `build.py` 1463, 1509, 1597 to 1608, `threemf.py` 576 to 726, `edits.py` 84 to 91, 1692, 1803, `report.py` 284 to 303 |
| shambu's prior record of the behaviour | `~/Code/shambu/docs/errors/2026-10-16-loaded-filament-not-used.md`; skill `references/00-findings.md` lines 128 to 136; `references/03-3mf-format.md` lines 295 to 340 (the vector table, `different_settings_to_system` "user-preset bookkeeping") and section 8 (re-slicing a CLI export) |
| The earlier post-mortem (format and the "Shav places by hand, Studio's default process 2 walls 15 %" precedent at 20:57 on 20 Oct) | `content/drafts/openarm/g2-redesign/55-shambu-post-mortem.md` |
| Session | Claude Code `8d0c87de-7d07-454b-8e00-594d68ea9d24`, 29 Oct 2026 |

## 9. One-line summary for the changelog

shambu 0.5.1 exports the process as an unknown user preset with empty `different_settings_to_system`; Bambu Studio
02.08 replaces it with the stock preset on open and drops every override, so a GUI final pass re-slices at 2 walls and
15 %. Per-object settings survive. Fix by exporting the process as the base preset plus its diff list, moving the
interface recipe to object scope, adding a process-level patch, and rewriting the hand-back.

## 10. Corrections from the fixing session (2026-10-29)

The fixing session read the Bambu Studio source at tag `v02.08.02.61` and the shambu tree. Each item corrects or
settles a claim above; the rules with their line cites are in `skills/shambu/references/03-3mf-format.md` section 9
and `docs/specs/2026-10-29-gui-preset-bookkeeping.md`.

1. **Section 2.3, the per-object interface keys.** They came from the owner's GUI suggestion at 20:49 (screenshot
   `10.png`), not from shambu. shambu wrote `support_style snug` (`snug-normal-supports`) and no interface keys per
   object; its interface keys were in the process.
2. **Section 2.2, the preset name.** The name comes from the spec's `process.name` (`spec.py`, passed to
   `presets.PresetResolver.for_cli`), not from a recipe.
3. **Who writes `project_settings.config`.** The Studio CLI writes it from shambu's preset inputs; shambu
   post-processes it (0.6.0 adds the preset bookkeeping there as a patch).
4. **The headless slice was right.** The CLI never resets values (`BambuStudio.cpp:3135-3137`, `update_all` true), so
   shambu's 216 g / 6 h 36 slice had the spec's values. Only the GUI open lost them (`Preset.cpp:2258-2269`).
5. **"Slice plate" lit is not a shambu fault.** Studio loads the embedded G-code only when the model has no objects
   (`Plater.cpp:9578-9581`), so a project with objects always opens unsliced. The GUI slice is the check.
6. **F5.** Short `filament_map`, `filament_nozzle_map` and `filament_volume_map` vectors are padded on load, never
   reset (`Plater.cpp:~8935-8948`). F5 did not contribute to the loss.
7. **F6.** The plate metadata already carries `filament_map_mode` Manual, and the plate value wins
   (`PartPlate.cpp:300-311`). The global reset to Auto For Flush comes from a `printer_model` change on open
   (`Plater.cpp:24529-24533`); nothing in a file prevents it.
8. **A defect the post-mortem missed.** `presets.resolve()` merged `include` templates after the preset's own keys, so
   the template won. Three X2D filament values (`filament_cooling_before_tower`, `retraction_distances_when_ec`,
   `slow_down_min_speed`) differed from Studio's. Found and fixed in 0.6.0 (Studio's order:
   `PresetBundle.cpp:5108`, `:5141-5143`, `:5158`).
9. **Section 2.1, the 28-key list.** It compares two different projects (the 19:22 all-PETG export and the 20:50 save
   of the PLA-interface build), so most of its keys are not GUI drops. The process keys the GUI dropped are the six
   named in the evidence (`print_settings_id`, `wall_loops`, `sparse_infill_density`, `sparse_infill_pattern`,
   `bottom_shell_layers`, `brim_type`) plus `filament_map_mode`.
