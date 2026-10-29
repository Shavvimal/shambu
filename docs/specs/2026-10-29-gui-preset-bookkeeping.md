# Feature spec: a shambu project must open in Bambu Studio with the process it was sliced with (2026-10-29)

Status: IN PROGRESS for 0.6.0; section 9 records the Codex design review and what it changed. Severity: high (every build with a recipe or a `set` key loses those values the
moment the GUI opens it; the GUI then re-slices on the stock preset and the user prints something shambu never
checked). Owner's requirement, verbatim: "I WANT to see it in the Bambu studio. I don't trust it yet to just send,
wait 9 hours to realise Shambu was wrong. I need it to work well with the GUI so I can do my final pass check." And:
"the slice we use and the GUI SHOULD match up. there is no reason for them not to, so keep GUI validation as part of
the workflow". Sending the file to the printer without Studio is not a fix.

Written from the post-mortem `docs/errors/2026-10-29-gui-open-drops-process.md`, a read of the Bambu Studio source at
the installed tag `v02.08.02.61` (commit `926a7192`), the three evidence files in the openarm plates folder, and the
shambu tree at `5efeabd`. Studio paths below are relative to `src/` of that tag. Every Studio rule the code will
encode cites its line here so the reference files can carry it.

## 1. What Studio does with a 3MF's process (confirmed from source)

1. `Plater::priv::load_files` (`slic3r/GUI/Plater.cpp:8398`, `:8799`) reads the archive, loads any project-embedded
   presets, then calls `PresetBundle::load_config_model` → `load_config_file_config(name, is_external=true, ...)`
   (`libslic3r/PresetBundle.cpp:3729`).
2. `load_config_file_config` pads `inherits_group` and `different_settings_to_system` to `num_filaments + 2` entries
   (`:3775-3776`). Index layout: `[0]` process, `[1..N]` filaments, `[N+1]` printer (`:3866-3867`, `:3885`, `:3985`).
3. For the process slot it splits `different_settings_to_system[0]` on `;`, adds the four ignore keys (`inherits`,
   `print_settings_id`, `filament_settings_id`, `printer_settings_id`, `:3250-3253`) and calls
   `PresetCollection::load_external_preset(..., different_keys, LoadAndSelect::Always)` (`:3866-3879`). The guard
   `if (!has_different_settings_to_system)` is commented out (`:3870`), so the set is never empty.
4. `load_external_preset` (`libslic3r/Preset.cpp:2214`) builds `cfg` from the full inline config, looks up
   `print_settings_id` by name, then (`:2258-2269`):
   ```
   if (!inherits.empty() && (different_settings_list.size() > 0)) {
       iter = find_preset_internal(inherits);
       if (found) cfg.update_non_diff_values_to_base_config(iter->config, keys, different_settings_list, ...);
   } else if (found && it->is_system && (different_settings_list.size() > 0)) {
       cfg.update_non_diff_values_to_base_config(it->config, keys, different_settings_list, ...);
   }
   ```
   `update_non_diff_values_to_base_config` (`libslic3r/PrintConfig.cpp:9218`) sets EVERY key not named in the list
   back to the base preset's value. Only the listed keys keep their inline values.
5. Then: if the named preset exists and `profile_print_params_same`, it is selected clean (`:2292`). Else if the name
   is unknown and `inherits` names a known preset with the same params, that preset is selected clean (`:2307-2323`).
   Else if a preset was found but differs, it is selected and `cfg` applied on top, so it shows as modified
   (`:2330-2345`). Else a new project-embedded preset `"<name>(<file>.3mf)"` is created from the inline config
   (`:2364-2418`); it has no parent, so a GUI save of it writes an empty diff list and every reopen adds a suffix.
6. On save, `PresetBundle::full_fff_config` (`PresetBundle.cpp:3304-3313`, `:3523-3545`) writes
   `print_settings_id` = the selected preset's name, `inherits_group[0]` = its `inherits` (`""` for a system preset),
   and `different_settings_to_system[0]` = the keys that differ from the parent, sorted, `;`-joined
   (`dirty_options_without_option_list`, `Preset.cpp:3162-3186`; `escape_strings_cstyle`, `Config.cpp:89`). The GUI
   couples `skeleton_infill_density` and `skin_infill_density` to `sparse_infill_density` when the user edits it
   (`slic3r/GUI/Tab.cpp:2105-2111`).
7. The embedded G-code of a `.gcode.3mf` is loaded as a slice result only when the model has NO objects
   (`Plater.cpp:9578-9581`: `if (model.objects.empty()) partplate_list.load_gcode_files();`). Studio's own
   `.gcode.3mf` export skips the geometry (`SaveStrategy::SkipModel`, `:22612`). A project with objects always opens
   unsliced, whatever the config says. There is no config-equality check.
8. Project-embedded presets: `Metadata/process_settings_<n>.config` (`libslic3r/Format/bbs_3mf.cpp:186-189`,
   `bbs_3mf.hpp:32-34`) are read when present (`:2039-2041`, `:2805-2898`), need `print_settings_id` and an
   `inherits` that names an installed preset (`Preset.cpp:1671-1752`, else "can not find parent for config" and the
   file is skipped), and are selected through the same step 4 reset, so they still need the diff list.
9. `filament_map_mode`: the plate's metadata wins over the project value (`PartPlate.cpp:300-311`); a new plate starts
   as `Auto For Flush` (`PartPlate.cpp:172`) and takes the plate metadata when present (`bbs_3mf.cpp:4657-4676`).
   Short `filament_map`, `filament_nozzle_map`, `filament_volume_map` vectors are padded, never reset
   (`Plater.cpp:~8935-8948`); `extruder_ams_count` is parsed and erased (`PresetBundle.cpp:3827-3831`).

## 2. What shambu writes today, and why Studio drops it

- `presets.PresetResolver.for_cli` (`src/shambu/presets.py:124-147`): any override or `name` makes the process a user
  preset: `name` = `spec.process.name` or `"<base> (modified)"`, `from: User`, `inherits: <base>`,
  `print_settings_id` = that name. A process recipe is an override (`spec.py:410-415`), so every build with a recipe
  takes this path. Filaments with a `set` or a `name` do the same.
- The Studio CLI writes `Metadata/project_settings.config` from those inputs (`build.py:1488-1551`); it carries the
  resolved values, `print_settings_id` = the user name, `inherits_group[0]` = the base, and
  `different_settings_to_system = ["", ..., ""]` (the CLI has no parent preset to diff against). Nothing in `src/`
  writes the list for a build; only `edits.AddFilament` resizes it (`edits.py:1692-1695`, `1803`).
- Step 4 above then resets every process key to the base. Step 5 selects the base clean. The user sees
  `0.20mm Standard @BBL X2D` with 2 walls and 15 % grid, and the Slice button lit (step 7, unavoidable).
- Per-object settings in `Metadata/model_settings.config` never pass through presets, so they survive. That is why the
  per-object recipes (`flat-no-brim`, `tall-outer-brim`, `snug-normal-supports`, `petg-supports`) reached the GUI.
- The export check (`build.py:1615-1616`) requires `print_settings_id` == the composed name and the report claims
  "verified: process name and every override" (`report.py:247`). Both describe the file, not what the GUI will do.

Evidence (openarm `plates/`): the untouched 19:22 export has `print_settings_id "PETG sweeper 4 walls 25 pc gyroid"`,
`inherits_group ["0.20mm Standard @BBL X2D","","",""]`, `different_settings_to_system ["","","",""]`, `wall_loops 4`;
Studio's 20:50 save has `0.20mm Standard @BBL X2D`, no list, `wall_loops 2`; Studio's 21:07 save after the owner's
manual edit has the list `bottom_shell_layers;skeleton_infill_density;skin_infill_density;sparse_infill_density;sparse_infill_pattern;wall_loops`
and `wall_loops 4`.

## 3. Two more defects found on the way

1. **Include precedence in `resolve()`** (`presets.py:109-122`): the `include` templates are merged AFTER the preset's
   own keys, so the template wins. Studio's resolved `Generic PETG @BBL X2D 0.4 nozzle` has
   `filament_cooling_before_tower 10`, `retraction_distances_when_ec [3,3,3,4,4,4]`, `slow_down_min_speed 20` (the
   leaf's values); shambu feeds the CLI `0`, `10`, `10` (the template's). The headless slice therefore runs with
   three filament values the GUI never uses. Studio's order (`PresetBundle::load_vendor_configs_from_json`,
   `PresetBundle.cpp:5060-5158`): the flattened parent (`:5108`), then the include's keys that differ from the
   collection default (`include_config.diff(*include_default_config)`, `apply_only`, `:5141-5143`), then the leaf's
   own keys (`config.apply(config_src)`, `:5158`). Section 5.8 fixes the order.
2. **`skeleton_infill_density` / `skin_infill_density`** stay at the base value when a recipe sets
   `sparse_infill_density`; the GUI sets all three together (step 6). Harmless for gyroid, but the file differs from
   what the GUI would save, so the verifier would flag it. shambu mirrors the GUI coupling (section 5 item 2).

## 4. Required behaviour

1. A project built by shambu, opened in Studio 02.08, shows the process preset shambu built from, marked modified,
   with every spec and recipe value in place (wall loops, infill, shells, brim, supports, interface filament). A GUI
   save keeps them. `shambu inspect` on that save shows the same values and the same diff list.
2. The same for the printer and for filament slot 1. Studio loads filament slots 2..N with
   `LoadAndSelect::OnlyIfModified`/`Never` (`PresetBundle.cpp:4003-4013`), so a modified secondary filament comes back
   from a GUI save as a project-embedded copy named `<base>(<file>.3mf)` with `inherits_group[i]` = base and the values
   kept (`Preset.cpp:2332`, `:2354-2365`, `:2401-2439`). The export still writes the base id; the verifier accepts the
   embedded form for slots 2..N and compares the values.
3. The user's GUI slice of the opened project gives the same grams and minutes as shambu's report (the 29 October
   runs matched to 0.03 g and 1 minute once the settings agreed), so the GUI final pass checks shambu's numbers
   instead of replacing them.
4. The hand-back says what the GUI will show: the base preset name with "(modified)", the plate unsliced (Studio does
   not load the embedded G-code of a project with objects), and that a GUI slice must land on shambu's estimate.
5. A GUI-saved project can be checked against its spec (`shambu verify FILE --against SPEC`), and a process key can be
   patched in place (`shambu patch FILE --process-set KEY=VALUE`) with the bookkeeping kept right.
6. The dual-material interface is set per object, where the GUI keeps it and where Studio's own suggestion dialog
   writes it; the plate-wide parts stay in the process.

## 5. The changes

### 5.1 Export the process (and filaments, printer) as the base preset plus its diff list (F1a)

- The list for a slot is `sorted(explicit overrides ∪ diff_keys)`. Explicit overrides: every key of `spec.process.set`
  (recipes included), the tower keys tower planning wrote, and the derived infill keys (5.2); for a filament, its
  `set` keys; the machine has none. An equal key on the list is harmless (Studio drops it on save,
  `PresetBundle.cpp:3306-3313`); a missing one is reset. The export merges its list with any keys the CLI already
  put in the slot (the CLI can list a tower key it changed, `BambuStudio.cpp:3388`, `:4182-4208`).
- `presets.py`: new `PresetResolver.diff_keys(kind, base, composed) -> list[str]`: the sorted keys of `composed` whose
  value differs from `resolve(kind, base)` or that the resolved base does not define, minus the bookkeeping keys
  (`name`, `type`, `from`, `inherits`, `instantiation`, `setting_id`, `is_custom_defined`, the three `*_settings_id`,
  `compatible_printers`, `compatible_printers_condition`, `description`, `version`, `filament_colour` for a filament,
  `include`). Values compare as the strings Studio stores; a list compares element-wise. Pure, no I/O beyond the
  resolver's cache.
- `build.py`: the `Plan` carries, per slot, the base system name and the diff list (`plan.bookkeeping`:
  `[(base_name, diff_keys), ...]` in the Studio index layout). Tower planning edits the process after `for_cli`
  (`build.py:1086-1093`), so the diff is computed from the final `plan.process` just before `write_inputs`.
- `threemf.py`: new `Project.set_preset_bookkeeping(slot: int, base: str, diff: Sequence[str])` writes
  `print_settings_id` / `filament_settings_id[i]` / `printer_settings_id` = `base`, `inherits_group[slot] = ""`,
  `different_settings_to_system[slot] = ";".join(sorted(diff))` (both vectors created or resized to N+2 with `""`),
  and marks `project_settings.config` dirty WITHOUT invalidating the slice (a new `_touch_ps(invalidate=False)`;
  these keys do not change the G-code). `patches.py`: `SetPresetBookkeeping(slot, base, diff)` wrapping it.
- `build.py patch_set`: add one `SetPresetBookkeeping` per slot that has a diff, in BOTH the sliced and the unsliced
  branch. `runs.slice_project` with `--settings-from` applies the same for its machine and process.
- Why a patch and not a CLI input: the CLI copies `different_settings_to_system[0]` from the input process JSON only
  when the input model is not a Bambu 3MF (`BambuStudio.cpp:2382-2386`, `:3352-3360`), so `slice --settings-from`
  (whose input is a 3MF) could never use it, and the CLI writes `print_settings_id` from the JSON's name. One
  post-export patch serves every path. The CLI itself never resets values (`:3135-3137`, `update_all` true), so the
  headless slice always had the spec's values; only the GUI open lost them.
- The composed name stays in the input `process.json` the CLI reads (the CLI needs a name), in the report line
  "process `<name>` (from `<base>`)", in `result.json`, and in the plate's `plater_name`-adjacent metadata is NOT
  touched. The 3MF's preset id is the base name. Rationale: Studio resolves by name against the installed library
  (step 5); a name that exists only in the file either resets or forks a `(file.3mf)` preset with no parent.
- `build.py verify`: the process check becomes: `print_settings_id` == base name, `inherits_group[0] == ""`,
  `different_settings_to_system[0]` == the plan's list, and every key in the list has the plan's value in the file.
  The filament check compares `filament_settings_id` with the base names. The message "process in file is X,
  expected Y" names the base and the list.
- `report.py:247` "verified" line: "process `<base>` + <n> overrides (<list>)".
- Spec `process.name` keeps its meaning as the readable label in the report and the recipe list; the skill text says
  it is not the GUI's preset name.

### 5.2 Mirror the GUI's derived keys

When the composed process sets `sparse_infill_density` and the spec or recipe does not set `skeleton_infill_density`
or `skin_infill_density`, set both to the same value (Tab.cpp:2105-2111). Same for `sparse_infill_line_width` →
`skin_infill_line_width`, `skeleton_infill_line_width` (`:2113-2117`). Done in `build.py` where the process is
composed, so the diff list includes them, as the GUI's does.

### 5.3 Object-scoped interface recipe (F2)

- `recipes.py`: `dual-material-interface` keeps its name but becomes `[object]` scope with the keys Studio's own
  suggestion writes per object (evidence, the 20:50 save of `cap_print`): `enable_support 1`,
  `support_type tree(auto)`, `support_style tree_hybrid`, `support_threshold_angle 35`,
  `support_on_build_plate_only 0`, `support_interface_filament 2`, `support_filament 0`,
  `support_interface_not_for_body 1`, `support_top_z_distance 0`, `support_interface_spacing 0`,
  `support_interface_pattern rectilinear_interlaced`, `tree_support_branch_diameter 3`,
  `tree_support_branch_diameter_angle 7`, `raft_first_layer_expansion 5`, `thick_bridges 0` (15 keys).
  `support_interface_speed` stays out (per-nozzle array, caveat at `recipes.py:172`).
- The plate-wide part moves to a new `[process]` recipe `two-filament-plate` (`enable_prime_tower 1`,
  `independent_support_layer_height 0`: both are global `PrintConfig` keys, `PrintConfig.hpp:1558`, so a per-object
  copy does nothing) that the spec's `process.recipes` lists; the skill text pairs them.
- `build.py:943-950` auto-pin: a part with `support_interface_filament` set by a recipe keeps it (today the pin only
  fires on `"0"`, so no change in logic, but a test proves it).
- Scope is enforced softly: a `[process]` recipe listed on a part, or an `[object]` recipe listed on the process, is a
  WARN line in `check` and `build` (today nothing says anything, `spec.py:191-195`, `:410-413`).

### 5.4 Process-level patch (F3)

`shambu patch FILE --process-set KEY=VALUE [--process-set ...]` and `--process-unset KEY`. `SetProcessSetting` /
`UnsetProcessSetting` in `patches.py` over `Project.set_process_setting(key, value)` / `clear_process_setting(key)`:
writes the value into `project_settings.config`, then recomputes `different_settings_to_system[0]` as the current list
plus the key (set) or minus the key (unset, with the value restored from the resolved base preset named by
`print_settings_id`; when that preset is not installed, refuse with "process preset X is not in the Studio library;
cannot restore KEY"). Invalidates the slice (the value changes the G-code). `--process-set` on a file whose
`print_settings_id` is not a system preset (a user name with `inherits_group[0]` set) updates the list the same way
against the `inherits_group[0]` parent. Exit 1 when the key is unknown to the resolved process preset.

### 5.5 Hand-back and skill text (F4)

- `report.py:284-303` "Owner steps in Bambu Studio" becomes, in order: (1) open with `shambu open FILE`; (2) the
  process combo shows `<base> (modified)`; open its tab and confirm `<the diff keys with values>`; (3) confirm the
  plate type and the filament colours and, for two filaments, that the grouping shows filament 2 on the auxiliary
  nozzle; (4) Slice plate: Studio does not load shambu's embedded G-code for a project with objects, so the plate
  opens unsliced; the GUI estimate must match `<h> h <min> min, <g> g` from this report within a minute and a gram,
  otherwise stop, save the project in Studio under a new name and run `shambu verify <saved>.3mf --against SPEC`
  (what Studio holds in memory is not on disk until saved; verifying the export proves nothing about the GUI state);
  (5) Preview checks as today; (6) Print only when the estimate matched or verify exits 0. The line
  "Re-slice in Studio after any change" goes. No SD-card or Device-tab bypass anywhere.
- `skills/shambu/SKILL.md`: the hand-back template's `Process:` line names the base and the override count; the
  `Next:` line carries steps 2 and 4; the Never list gets "Tell the user to send the file to the printer without
  opening it in Studio: the GUI final pass is the requirement"; the 2026-10-16 Never line about the swap is rewritten
  to the mechanism (a preset Studio cannot find by name is reset to its base unless the diff list names the keys).
- `references/03-3mf-format.md` gets a section "Preset bookkeeping Studio honours" with the rules of section 1 and
  the line cites; `references/00-findings.md` records the 29 October finding and corrects the 16 October note.

### 5.6 GUI round-trip verifier (F8)

`shambu verify FILE.3mf --against SPEC.json [--json]`: builds the plan's presets from the spec (no Studio run), reads
the file, and reports per line: process preset id and diff list (expected vs file), every spec and recipe process
key (value expected vs in file), each filament preset id and colour, `filament_map` and mode at project and plate
level, bed type, print sequence, every object's per-object settings against its part's recipes and `set`, and each
object's position against `at` when the spec gives one (tolerance 0.05 mm). Exit 0 when no FAIL line, 1 on any FAIL or a spec that does not
load (as every other verb through `load_spec_or_error`), 3 on a project file that does not read. A key the spec does not set but the GUI changed is a WARN with its file value and does not change the
exit code: the exit code answers "is the spec honoured", the WARN lines answer "what did the user change" (decided in
review, 2026-10-29). It reuses `Pipeline.plan()` up to the presets and the existing
`Project.summary()`; no new parsing. Positions use the existing `summary()["objects"][i]["position"]`.

### 5.7 `shambu open` focus (F7)

`facade.open_in_studio`: after `open -a BambuStudio FILE`, when `StudioCLI.running_pids()` was non-empty before the
call, print one WARN line: "Bambu Studio was already running: the project opened in a new window behind the
current one". No AppleScript keystrokes (rubric item 16). `open -g`/`--new` are not used: Studio is single-instance
(`single_instance` in its config) and a second instance is refused.

### 5.8 Include precedence (section 3 item 1)

`resolve()`: flatten the parent chain, merge the `include` templates on top of it, then the preset's own keys last,
so the leaf wins over the template and the template over the ancestors (`PresetBundle.cpp:5108`, `:5141-5143`,
`:5158`). Known approximation, recorded in the docstring: Studio applies only the template keys that differ from the
collection default; shambu has no default table and applies every template key. The three X2D filament values above
come out right either way. A test on the synthetic library pins the order (leaf > include > parent).

### Not done, with reasons

- **F1b, install the preset into the user library.** Studio syncs that folder with the cloud account
  (`sync_user_preset = true`); a file written there can be removed or uploaded. F1a alone is enough once the diff
  list is right; keep F1b out.
- **The named preset in the GUI (embedded `Metadata/process_settings_1.config`).** Works on paper (section 1 step 8)
  and the experiment file `exp2` tests it, but it adds a second entry Studio rewrites on save and a `(file.3mf)` fork
  risk if `inherits` is ever missing. Decide after the experiment; not in 0.6.0 unless `exp2` opens clean.
- **F5, full-length dual-nozzle vectors.** Studio pads them on load and never resets anything because of them
  (section 1 step 9). Nothing to fix; the reference notes it.
- **F6, `filament_map_mode` Manual.** shambu already writes it where Studio reads it: in each `<plate>` of
  `model_settings.config` (`build.py:1463` plate params; the 19:22 export has it) and at project level. The plate
  value wins (`PartPlate.cpp:300-311`) and survives the grouping popup (`FilamentGroupPopup.cpp:429` always offers
  Manual). The global value is reset to Auto For Flush by a `printer_model` change on open (`Plater.cpp:24529-24533`,
  when the GUI held another printer or the printer sync reselects it); nothing in a file prevents that. The plate
  reset seen on 29 October is explained by the source only through the grouping dialog (Cancel, a greyed OK on the
  Custom page, or an Auto page choice: `FilamentMapDialog.cpp:208-226`, `:288-293`, `:345-350`). The
  `prefered_filament_map_mode` preference is read only by the popup (`:315`) and never writes a project. So: no
  writer change; the verifier (5.6) reports project and plate mode so a change is visible; the hand-back tells the
  user to leave the grouping dialog alone unless it shows the wrong nozzle.
- **Keeping the sliced state on open.** Impossible for a project with objects (section 1 step 7). The hand-back says
  so and makes the GUI slice the check.

## 6. Experiments for the owner (five minutes, one Studio open each)

Files in `/private/tmp/claude-501/-Users-shav-Code-shambu/7e472d4d-d270-4fc4-b452-1e466b3a9e19/scratchpad/experiments/`,
built by `make_experiments.py` from the untouched 19:22 export; only `project_settings.config` changed (exp2 adds one
entry). Diff list computed: `bottom_shell_layers;brim_type;sparse_infill_density;sparse_infill_pattern;wall_loops`.

1. `exp1-base-plus-diff.gcode.3mf`: expect the process combo `0.20mm Standard @BBL X2D (modified)`, wall loops 4,
   25 % gyroid, 5 bottom shells, brim none, plate unsliced, and a GUI slice near 6 h 30 min / 212 g. Save, then
   `shambu inspect` must show the same id and list.
2. `exp2-embedded-named-preset.gcode.3mf`: expect the combo to read `PETG sweeper 4 walls 25 pc gyroid` with the same
   values. Save, reopen once more: the name must not grow a `(…3mf)` suffix.
3. After the implementation: `shambu build plate-pla-interface.json --slice` on the sweeper, open, no grouping
   dialog action, confirm PLA on the auxiliary nozzle and 4 walls, save, `shambu verify <save> --against plate-pla-interface.json`
   exits 0, GUI slice within 1 min / 1 g of the report.

## 7. Acceptance tests (offline, in `tests/`)

- `test_presets.py`: `diff_keys` on the synthetic library: an override, a key absent from the base, an unchanged key,
  a list value; the bookkeeping keys never appear. `for_cli` with the include-order fix: the leaf wins over the template.
- `test_threemf_patches.py`: `SetPresetBookkeeping` on the fixture 3MF: creates the two vectors at N+2, writes the
  slot, keeps the G-code entries of a sliced fixture (no invalidation), idempotent, `save()` passes the touches check.
- `test_build_strict.py`: a fake export with the user name in `print_settings_id` is patched to the base + list and
  `verify()` passes; a fake export missing one list key fails with the key named.
- `test_cli.py`: `patch --process-set wall_loops=4` on the fixture: value and list updated, slice invalidated;
  `--process-unset` restores the base value; `verify --against` exit codes 0 and 1 with one line per difference.
- `test_pipeline.py`: the hand-back text contains "(modified)", the diff keys, "opens unsliced", and never
  "Re-slice in Studio after any change", "SD card" or "Device tab".
- `test_spec.py`: the scope WARN for a mis-scoped recipe; `dual-material-interface` is `[object]`.

## 8. Release

0.6.0 (a behaviour change in every export). Bump `src/shambu/__init__.py`, `.claude-plugin/plugin.json`,
`skills/shambu/SKILL.md` `--expect-version`, `skills/setup/SKILL.md`, `README.md`; CHANGELOG entry under 0.6.0 with
the one-line summary from the post-mortem section 9 and the include-order fix.

## 9. Design review (Codex gpt-6-astra high, thread 01a189a0, 2026-10-29) and the decisions

Verdict was "reject" on three blockers. Each finding, and what changed:

1. **Blocker, diff baseline (F).** shambu's resolved base can differ from Studio's effective base (the include
   diff-against-default filter, a different vendor bundle version), so a key equal to shambu's base but not Studio's
   would be left off the list and reset. Accepted: the list is explicit overrides ∪ computed diff (5.1), merged with the
   CLI's own entries. Dismissed the stronger "list every key" option: it would carry a wrong shambu base value into the
   GUI and hide it; the intent list lets the GUI reset such a key and the verifier (5.6) then shows the value difference.
2. **Blocker, secondary filaments (C).** Slots 2..N are loaded `OnlyIfModified`/`Never` and become embedded copies.
   Accepted: requirement 4.2 and the verifier (5.6) now describe and accept that form; a test covers two slots on one
   base. The export keeps writing the base id, which is what the export check sees.
3. **Blocker, verifier gaps (G/I).** `summary()` has no position field (`edits.object_positions()` through the facade
   does), `Pipeline.plan()` runs Studio, and ids/colours alone miss dropped filament or printer overrides. Accepted: the
   verifier composes presets from the spec without planning, reuses the position helper, compares values for every
   preset kind, keeps the explicit-override value check independent of the list, normalises empty vectors and compares
   key sets.
4. **Should, process patch (G).** Append-only bookkeeping would list a key set back to its base value. Accepted: the
   patch recomputes the slot canonically when the base resolves, appends only when it does not; the resolver stays in
   the CLI, the patch receives values (rubric 4).
5. **Should, every slot (I).** Accepted: every slot is normalised, including empty diffs (a `name`-only filament). The
   "CLI never resets values" claim is narrowed: it copies every input key (`update_all`), but may change and list the
   tower key; hence the merge in 5.1.
6. **Should, mechanism qualifications (A, B).** Accepted: `inherits_group[0] = ""`; listed variant-dependent vectors are
   remapped, not kept verbatim (`PrintConfig.cpp:9273-9287`); a known `inherits` parent can also be selected modified.
7. **Should, derived keys (E).** Skin/skeleton densities change fill only for the locked-zag pattern
   (`Fill.cpp:307-329`). Accepted: couple only on an explicit `sparse_infill_density` override, keep explicit derived
   values, share the helper with `slice --settings-from`, document locked-zag.
8. **Nit, G-code kept (D).** Agreed: the MD5 covers the G-code bytes only (`bbs_3mf.cpp:6776-6800`); the bookkeeping
   patch declares `Metadata/project_settings.config` as its only touch and a test checks the G-code bytes are unchanged.
9. **Should, rubric (G).** Accepted: the open warning is returned by the facade and printed by the CLI; exit codes as
   rubric 19.
10. **Nit, deferrals (H).** Agreed; the embedded named process preset stays an experiment (section 6).

## 10. Code review round 1 (Codex per worktree, rubric reviewers), decisions

- **Bookkeeping worktree**, four blockers accepted: `_relist_process` keeps listed keys and adds a newly set key absent
  from the base; `_process_base` takes `inherits_group[0]` before `print_settings_id` (Studio's reset target,
  `Preset.cpp:2258-2269`); `slice --settings-from` unions the input file's listed keys (the CLI keeps their values but
  clears the list, `BambuStudio.cpp:3352-3360`); the filament count follows `filament_colour`
  (`PresetBundle.cpp:3751-3755`) and a disagreement with `filament_settings_id` refuses the patch. Also: the export's
  original preset ids are checked against the composed names before the bookkeeping patch rewrites them;
  `--process-set` refuses filament and printer keys; `PatchSet.apply` forwards the union of the patches' touches.
- **Verify worktree**, six blockers accepted: the auto-pin of `support_filament`/`support_interface_filament`
  (build.py:943-950) is mirrored in the expected object settings; the object's filament is compared; a missing object
  is FAIL unless its plate groups by filament and a `group-f<n>` object exists; plate membership is checked for every
  object; embedded filament copies still get their list and values checked; a plate without `filament_map_mode`
  metadata is Auto For Flush in Studio (`PartPlate.cpp:172`) and fails a Manual expectation, while an empty plate map
  falls back to the project map (`PartPlate.cpp:289`); the hand-back makes the user save the GUI project before
  verifying it. Should-fixes accepted: filament base values for list classification; tower keys INFO when the spec has
  plate-level tower overrides; variant blocks by `filament_self_index`; project options (`s_project_options`) are not
  "GUI edits"; a bad spec is exit 1; malformed metadata becomes `ProjectError`; the open WARN asks the user to check the
  front window instead of asserting a window order (`GUI_App.cpp:7711-7722` makes it conditional); a swallowed
  `PresetError` becomes a WARN line.
- **Recipes and docs worktree**: the scope WARN says a `[process]` recipe's global keys are ignored per object
  (`PrintConfig.hpp:1517`, `:1558`), the skill and README make the user save before verifying, "the CLI never resets"
  is restricted to shambu's new-process path (`BambuStudio.cpp:3388`), the dropped-key correction names the
  `support_interface_filament` loss, the verifier's CHANGELOG line is narrowed to what its tests show, and four cites
  were tightened (`:3985`, `:5084-5086` + `:5109`, `:5142-5144`, `Plater.cpp:8944-8958`).
- Dismissed: "GUI edits exit 1" (kept WARN, exit 0: the exit code answers whether the spec is honoured).
