# Bambu Studio 02.08.02.61 CLI: source-verified reference

> **Provenance and licence notice.** Bambu Studio is free software under the AGPL-3.0
> (https://github.com/bambulab/BambuStudio). The findings in this document come from reading that source at tag
> `v02.08.02.61`, inspecting the shipped macOS binary (symbols, strings, crash reports, a debugger session on a local
> re-signed copy) and running experiments with its command line. Short excerpts are quoted for interoperability only.
> No Bambu Studio code is included in shambu, and shambu is not affiliated with or endorsed by Bambu Lab.
> See `DISCLAIMER.md` at the repository root.

Source: `https://github.com/bambulab/BambuStudio`, tag `v02.08.02.61`, commit
`926a7192574bcb9b3a732e1ec59a46d79cb45466` (exact match to the installed
`/Applications/BambuStudio.app`, CFBundleShortVersionString `02.08.02.61`).
Clone: `$S/BambuStudio` (sparse). All `file:line` citations are relative to
`$S/BambuStudio/`. `BS.cpp` below means `src/BambuStudio.cpp`.

Nothing here was executed; every claim comes from reading the source. Items
marked **UNVERIFIED** are inferences that need a run to confirm.

---

## 0. Ten findings that matter most

1. **Action order on the command line is execution order.** `--slice`, `--no-check`,
   `--mtcpp`, `--mstpp`, `--export-slicedata`, `--load-slicedata`, `--export-settings`
   are all *actions*, processed one by one in argv order (`BS.cpp:6330`). `--slice`
   slices inline when it is reached. So `--slice 0 --no-check` slices **with** checks;
   `--no-check --slice 0` slices without. `--export-3mf` only sets a flag, so its
   position does not matter.
2. **Boolean options never consume the next token.** `--min-save 1` makes `1` an
   *input file*. Use the bare flag (`--min-save`) or `--min-save=1` / `--min-save=0`
   (`src/libslic3r/Config.cpp:1729-1735`).
3. **`--export-3mf` path is concatenated with `--outputdir`**:
   `export_3mf_file = outfile_dir + "/" + export_3mf_file` (`BS.cpp:7503`). An absolute
   `--export-3mf` plus `--outputdir` gives `<outdir>//abs/path.3mf`; the zip writer
   cannot open `<that>.3mf.tmp` (`src/libslic3r/Format/bbs_3mf.cpp:6352-6354, 6457-6458`),
   so you get "Unable to open the file X.3mf.tmp" and return code -13.
   Pass a bare file name with `--outputdir`, or an absolute path without `--outputdir`.
4. **`--pipe` does nothing on macOS.** The whole progress manager is inside
   `#if defined(__linux__)` (`BS.cpp:251-444, 1763-1770`). On macOS the option is
   accepted (and counts as an action) but no pipe is opened and nothing is written.
5. **`--load-settings` / `--load-filaments` JSON must be fully flattened.** The CLI
   loads them with `ConfigBase::load_from_json`, which stores `inherits` as a plain
   string and never resolves it (`src/libslic3r/Config.cpp:848-930`); `include`
   (the X2D G-code templates) is not resolved either. The only parent-resolving
   code looks in `Resources/profiles/BBL/{machine,process,filament}_full/`
   (`BS.cpp:2343, 2523, 2721, 2762, 2832`), and **those directories do not exist in
   the macOS app bundle** (only `machine/`, `process/`, `filament/` exist).
6. **`curr_bed_type` defaults to "Cool Plate"** (`src/libslic3r/PrintConfig.cpp:1298-1313`),
   and the CLI never reads the machine model's `default_bed_type`. The X2D marks
   Cool Plate as unsupported (`resources/profiles/BBL/machine/Bambu Lab X2D.json`:
   `"not_support_bed_type": "Cool Plate"`). Always set it: either
   `--curr-bed-type "Textured PEI Plate"` or per plate in `plate_params`.
7. **Thumbnails are always rendered for STL/assemble input**, through GLFW +
   OpenGL off-screen framebuffers (`BS.cpp:6218-6327, 7513-7900`). No flag or
   environment variable disables the plate/no-light thumbnails. `--skip-useless-pick`
   only skips the top/pick pair, and only on plates with ≤1 or >64 objects
   (`BS.cpp:7873`). The 5-minute cost is **UNVERIFIED** as to which step causes it
   (see §3.3).
8. **`--min-save` strips the model**: it adds `SaveStrategy::SkipModel`
   (`BS.cpp:8487-8488`), and the exporter then omits all objects, parts and plate
   instance lists (`bbs_3mf.cpp:7382, 7414, 7442, 8196, 8447, 8494`). The result is a
   G-code-style 3MF that cannot be edited. Never use it for an editable project.
9. **Assemble-list `pos_x/pos_y/pos_z` is a translation added to the STL's own
   coordinates**, not a target for the bounding-box centre or origin (`BS.cpp:1251`,
   `src/libslic3r/Model.cpp:1439-1450, 3460-3476, 1886-1894`). With
   `need_arrange: false`, the plate origin is then added (`BS.cpp:5505`). Z is never
   dropped to the bed unless you pass `--ensure-on-bed`.
10. **`--orient 1` overwrites each object's config with the whole process preset**
    (`o->config.assign_config(load_process_config)`, `BS.cpp:5216`). This drops the
    per-object `extruder` and turns every process key into a per-object override.
    Do not use `--orient 1` with `--load-settings`; orient meshes yourself.

---

## 1. Command-line parsing

### 1.1 Parser (verbatim, `src/libslic3r/Config.cpp:1660-1760`, abridged)

`DynamicConfig::read_cli` (`src/libslic3r/Config.cpp:1660-1760`), paraphrased with three lines quoted:
```cpp
if (! parse_options || ! boost::starts_with(token, "-")) { extra->push_back(token); continue; }  // positional = input file
//BBS: don't use 'no-' for boolean options
if (value.empty() && optdef.type != coBool && optdef.type != coBools) { ... value = argv[++ i]; }
```
Behaviour: every option key is registered with `_` turned into `-`; a token without a leading dash is an input
file; `--` ends option parsing; `--key=value` splits on the first `=`; an unknown option prints "Invalid option" and
fails the whole parse; a non-bool option without `=value` consumes the next argv token; a bool option never does
(empty value = true); vector options append on repeat; string options are stored without unescaping; other types go
through `set_deserialize_nothrow` and fail with "Invalid value for option".

Rules that follow from this code:

| Rule | Consequence |
|---|---|
| CLI name = option key with `_` replaced by `-` (`Config.cpp:242-258`), unless `def->cli` is set | `--load-settings`, `--layer-height`. The underscore form (`--load_settings`) is **rejected** ("Invalid option"). `--help` also accepts `-h`. |
| `--key=value` and `--key value` both work for non-bool options | Use `--key=value` everywhere; it is unambiguous. |
| Bools take no separate value | `--flag` = true; `--flag=0` = false. `--flag 0` = true plus a stray input file `0`. |
| `--no-xxx` is not supported | Invalid option, parse fails. |
| Vector options append on repeat | `--load-filaments a.json --load-filaments b.json` = two entries. |
| Strings vectors split on `;` (`Config.cpp:150-215`); leading spaces/tabs stripped; a value starting with `"` is C-unescaped | Quote paths that contain `;`. |
| Int / float vectors split on `,` (`Config.cpp:1084-1090`); enum vectors also `,` | `--load-filament-ids=1,2,1`, `--nozzle-volume-type="Standard,High Flow"`. |
| Points: `0x0,256x0,...` | Same as preset JSON. |
| Unknown option or bad value | `setup()` fails: prints help, `run()` returns `CLI_INVALID_PARAMS` (-2) **without** writing result.json (`BS.cpp:1547-1556`). |

### 1.2 Which config def the parser uses

`m_config` is `DynamicPrintAndCLIConfig` (`src/BambuStudio.hpp:124`), whose def is the
union of `print_config_def` + the three CLI defs (`src/libslic3r/PrintConfig.hpp:1862-1872`).
So **every print/printer/filament setting is accepted as `--<key-with-dashes>=<value>`**,
except the keys marked `cli = ConfigOptionDef::nocli`. Those are rejected as "Invalid option":

`printhost_apikey, printhost_port, printhost_cafile, printhost_ssl_ignore_revoke,
upward_compatible_machine, compatible_printers_condition, compatible_prints,
compatible_prints_condition, compatible_machine_expression_group,
compatible_process_expression_group, different_settings_to_system,
print_compatible_printers, default_filament_profile, default_print_profile,
filament_settings_id, filament_ids, filament_vendor, inherits, inherits_group,
host_type, printer_model, printer_variant, print_settings_id, printer_settings_id,
extruder_variant_list, printer_extruder_id, printer_extruder_variant,
print_extruder_id, print_extruder_variant, filament_extruder_id,
filament_extruder_variant, filament_self_index, material_vendor, (SLA keys)`
(from `grep nocli src/libslic3r/PrintConfig.cpp`; 43 sites).

After parsing, `m_extra_config.apply(m_config, true)` keeps only the print keys
(`BS.cpp:1561`). They have the **highest priority**: applied over the merged presets
(`BS.cpp:4082`) and again over the plate config for each slice
(`BS.cpp:6896-6898`). A vector key given on the CLI replaces the whole vector, so
per-filament keys must list one value per filament.

`setup()` sorts each explicitly given option into `m_actions` (action defs) or
`m_transforms` (transform defs), in argv order (`BS.cpp:8338-8345`).
**If `m_actions` is empty (and `--downward-check` is not set), the GUI starts**
(`BS.cpp:1604-1652`). A run with only `--load-assemble-list` or only transforms
opens the GUI.

### 1.3 Option table (all CLI-specific options)

Definitions: `src/libslic3r/PrintConfig.cpp:9713-10208`. Type codes: Bool, Int, Float,
String, Strings (`;`-list), Ints (`,`-list). "A" = action, "T" = transform, "M" = misc.

| CLI | Kind | Type | Default | What the code does |
|---|---|---|---|---|
| `--export-3mf <file>` | A | String | `output.3mf` | Sets `export_to_3mf`; export runs **after** the action loop (`BS.cpp:6406-6409, 7473+`). Path joined to `--outputdir` (finding 3). Writes `<file>.tmp` then renames. |
| `--slice <n>` | A | Int | 0 | 0 = all plates, n = plate n. Slices inline at its argv position (`BS.cpp:6431`). For non-3MF input any n>0 is reset to 0 (`BS.cpp:2187-2191`). Invalid n → -2 (`BS.cpp:5231-5236`). |
| `--export-png <n>` | A | Int | -1 | Renders `plate_<n>_<camera_view>.png` (512×512) into outputdir or CWD. **Must be the only action**, else -2 (`BS.cpp:6410-6417, 8175-8212`). |
| `--export-settings <file>` | A | String | `output.json` | `m_print_config.save_to_json(file, "project_settings", "project", version)` at its argv position (`BS.cpp:6360-6364`). Not joined to outputdir. |
| `--export-slicedata <dir>` | A | String | `cached_data` | After each plate slice, `print->export_cached_data(dir/<plate>)` (`BS.cpp:7241-7254`). Must precede `--slice`. |
| `--load-slicedata <dir>` | A | Strings (sic) | `cached_data` | `print->load_cached_data(dir/<plate>)` then `process(nullptr,true)`; falls back to a normal slice on error (`BS.cpp:6339-6359, 7070-7088`). Must precede `--slice`. Not allowed with `--repetitions`. |
| `--export-stl` | A | Bool | false | Exports all objects as one STL (`BS.cpp:6381-6387`). |
| `--export-stls <dir>` | A | String | `stl_path` | One STL per object (`BS.cpp:6388-6395`). |
| `--help`, `-h` | A | Bool | false | Prints help, then **continues** the run (it is an action). |
| `--info` | A | Bool | false | `model.print_info()` (`BS.cpp:6365-6370`). |
| `--uptodate` | A | Bool | false | 3MF input only: refresh 3MF presets from system presets (`*_full` dirs, absent on macOS) or from `--uptodate-settings/--uptodate-filaments` (`BS.cpp:2616-2866`). |
| `--load-defaultfila` | A | Bool | false | Empty slots in `--load-filaments` (e.g. `a.json;;b.json`) are filled with the first loaded filament (`BS.cpp:2449-2488, 2546-2557`). |
| `--min-save` | A | Bool | false | Adds `SaveStrategy::SkipModel` (finding 8). |
| `--mtcpp <n>` | A | Int | 1000000 | Max triangles per plate; only active when given **before** `--slice`. Exceed → -59 (`BS.cpp:6377-6378, 6589-6596`). |
| `--mstpp <s>` | A | Int | 300 | Max slice seconds per plate; same ordering rule. Exceed → -58 (`BS.cpp:6379-6380, 7357-7367`). |
| `--no-check` | A | Bool | false | `print->set_no_check_flag(true)`: skips the G-code path conflict checker (`src/libslic3r/Print.cpp:2418-2433`) and the "support necessity" check (`src/libslic3r/PrintObject.cpp:870`); turns the layer-height validate error into a warning; does not fail on `SlicingEmptyGcodeLayers`/`SlicingGcodeOverlap` warnings (`BS.cpp:6979, 7120-7135`). Must precede `--slice`. |
| `--normative-check` | A | Bool | **true** | For 3MF input only: non-empty `post_process` → -19 (`BS.cpp:1950-1960`). Disable with `--normative-check=0`. |
| `--pipe <name>` | A | String | "" | Linux only (finding 4). On macOS: no effect, but it is an action. |
| `--arrange <n>` | T | Int | 0 | 0 → `need_arrange=false`; 1 → true; other → keep automatic (`BS.cpp:5060-5075`). Automatic = false for a Bambu 3MF, true for STL/OBJ input. **Ignored for assemble lists** (they use per-plate `need_arrange`). |
| `--orient <n>` | T | Int | 0 | 1 → orient every object with `orientation::orient()`, after replacing the object config with the process preset (finding 10, `BS.cpp:4982-5011, 5207-5223`). 0 → no orient. |
| `--ensure-on-bed` | T | Bool | false | After transforms/arrange: `o->ensure_on_bed()` for all objects (`BS.cpp:6182-6188`). STL/OBJ *positional* inputs are always put on the bed at load (`BS.cpp:2124`); assemble-list objects are not. |
| `--repetitions <n>` | T | Int | 1 | Duplicates the plate n-1 times with a binary search for the largest count that fits; needs `--slice <plate>` > 0 (`BS.cpp:4947-4968, 5232-5260, 5585-6165`). |
| `--assemble` | T | Bool | false | Merges all loaded objects into one object "Assembly" (`BS.cpp:4925-4946`). Not with `--clone-objects`. |
| `--convert-unit` | T | Bool | false | Meters/inches → mm if the model looks like it (`BS.cpp:4970-4981`). |
| `--rotate`, `--rotate-x`, `--rotate-y` | T | Float (deg) | 0 | `o->rotate(rad, axis)` on every object (`BS.cpp:5078-5092`). |
| `--scale <f>` | T | Float | 1 | `o->scale(f)`; f ≤ 0 → -2 (`BS.cpp:5093-5103`). |
| `--load-settings "<m.json>;<p.json>"` | M | Strings | [] | One machine and/or one process JSON (§5). |
| `--load-filaments "<f1>;<f2>;..."` | M | Strings | [] | Filament JSONs; position = filament index 1..N (§5). |
| `--load-filament-ids=1,2,...` | M | Ints | [] | Per positional input file: object `extruder`. Size must equal number of input files, needs `--load-filaments`, value ≤ number of filaments, not allowed with a 3MF (`BS.cpp:1825-1840, 1886-1892, 2080-2091, 2115-2120`). |
| `--clone-objects=3,1,...` | M | Ints | [] | Per input file: clone every object n times (≤512, names `<name>_1..n`). Same size/3MF restrictions (`BS.cpp:1810-1824, 2093-2113`). |
| `--skip-objects=3,5` | M | Ints | [] | 3MF instance `identify_id`s to mark unprintable (`BS.cpp:1785-1793, 6536-6567`). All skipped → -60. |
| `--load-assemble-list <json>` | M | String | "" | §2. Not allowed with positional inputs (`BS.cpp:1850-1855`). |
| `--load-custom-gcodes <json>` | M | String | "" | `CustomGCode::Info` JSON for plate `slice-1` (or 1). §4.4. |
| `--outputdir <dir>` | M | String | "" | Prefix for result.json, `plate_N.gcode`, the 3MF name, PNGs. **Never created** by the CLI. |
| `--datadir <dir>` | M | String | — | `set_data_dir(absolute(dir))` only (`BS.cpp:8364-8368`). CLI mode loads no presets from it; it only matters as a fallback temp dir when `$TMPDIR` has <2 GiB free (`src/libslic3r/Model.cpp:1100-1125`). |
| `--debug <0-5>` | M | Int | 1 | Boost.Log filter: 0 fatal … 3 info, 4 debug, 5 trace. No file sink in CLI mode, so logs go to the console (stderr). At 4, every progress step logs `default_status_callback: percent=…` (`BS.cpp:446-455`). |
| `--estimate-mode` | M | Bool | false | 3MF + machine switch only; auto-fills filaments from `filament_full/` (absent on macOS). |
| `--check-preset` | M | Bool | false | Each loaded filament's `compatible_printers` must contain the printer system name, else -5 (`BS.cpp:3007-3030`). |
| `--enable-timelapse` | M | Bool | false | Only affects smooth-timelapse wipe-tower decisions (`BS.cpp:4178-4181`). |
| `--allow-multicolor-oneplate` | M | Bool | **true** | `arrange_cfg.allow_multi_materials_on_same_plate` (`BS.cpp:5440, 5882`). |
| `--allow-rotations` | M | Bool | **true** | Arrange may rotate objects. Pass `--allow-rotations=0` to keep orientation. |
| `--avoid-extrusion-cali-region` | M | Bool | false | Arrange avoids the calibration region. |
| `--skip-modified-gcodes` | M | Bool | false | For 3MF + uptodate: drop customised G-code keys and `Custom` layer G-codes (`BS.cpp:4017-4035`). |
| `--skip-useless-pick` | M | Bool | false | No top/pick thumbnails on plates with ≤1 or >64 objects; also sets `label_object_enabled=false` on 1-object sliced plates (`BS.cpp:7485-7499, 7873-7880`). |
| `--makerlab-name`, `--makerlab-version` | M | String | "" | Written as `MakerLab` / `MakerLabVersion` metadata in `3D/3dmodel.model` (`bbs_3mf.cpp:7266-7272`). |
| `--metadata-name "a;b"`, `--metadata-value "x;y"` | M | Strings | [] | Must be the same length, else -2 (`BS.cpp:1684-1689`). Each pair becomes `<metadata name="a">x</metadata>` in `3dmodel.model` (`bbs_3mf.cpp:7283-7288`); the value is XML-escaped twice. |
| `--allow-newer-file` | M | Bool | false | Skip the 3MF version check (-24). |
| `--allow-mix-temp` | M | Bool | false | Disables the "filaments with very different temperatures" validation (-62). |
| `--camera-view <n>` | M | Int | 0 | Only for `--export-png`: 0 Iso, 1 Top_Front, 2 Left, 3 Right, 10-12 Iso_1..3. |
| `--uptodate-settings`, `--uptodate-filaments` | M | Strings | [] | Explicit system presets for `--uptodate`. |
| `--downward-check`, `--downward-settings` | M | Bool / Strings | false / [] | Checks which smaller printers the plates fit; results go into result.json `downward_compatible_machine`. `--downward-check` alone suppresses the GUI. |

Hidden/legacy keys handled in code but **not defined** (so rejected by the parser):
`copy`, `center`, `align_xy`, `cut*`, `split`, `repair`, `scale_to_fit`, `export_obj`,
`help_fff`, `help_sla`, `sw_renderer`.

---

## 2. Assemble list (`--load-assemble-list`)

### 2.1 Keys (`src/BambuStudio.hpp:22-41`)

The JSON keys are string constants in `src/BambuStudio.hpp:22-41`: `plates`, `plate_params`, `plate_name`,
`need_arrange`, `objects`, `subtype`, `path`, `count`, `filaments`, `pos_x`, `pos_y`, `pos_z`, `assemble_index`,
`print_params`, `assembled_params`, `min_z`, `max_z`, `height_ranges`, `range_params`.

### 2.2 Parser (verbatim core, `BS.cpp:753-912`)

Core of the parser (`BS.cpp:753-912`, paraphrased; four lines quoted for the shape):
```cpp
int plate_count = root_json[JSON_ASSEMPLE_PLATES].size();
if ((plate_count <= 0) || (plate_count > MAX_PLATE_COUNT)) return CLI_CONFIG_FILE_ERROR;   // MAX_PLATE_COUNT = 36
assemble_object.filaments = object_json.at(JSON_ASSEMPLE_OBJECT_FILAMENTS).get<std::vector<int>>();
} catch(std::exception &err) { ret = CLI_CONFIG_FILE_ERROR; }
```
What the rest of the function does: per plate it reads `plate_name`, `need_arrange` and an optional `plate_params`
string map; `objects` must be non-empty; per object `path`, `count` (> 0), optional `subtype`
(`ModelVolume::type_from_string`, default normal part), `filaments` (required, size 1 or `count`), and the optional
arrays `assemble_index`, `pos_x`, `pos_y`, `pos_z` (same size rule), `print_params` (string map) and
`height_ranges` (`min_z`, `max_z`, `range_params`). `assembled_params` entries carry a scalar `assemble_index` with
optional `print_params` and `height_ranges`. Any exception becomes `CLI_CONFIG_FILE_ERROR` (-5).

### 2.3 Schema with exact semantics

```jsonc
{
  "plates": [                                  // 1..36 plates; index = plate number - 1
    {
      "plate_name":   "string",                // REQUIRED (missing key = undefined behaviour in nlohmann const operator[])
      "need_arrange": false,                   // REQUIRED bool
      "plate_params": { "curr_bed_type": "Textured PEI Plate" },   // optional; ALL values must be JSON strings
      "objects": [                             // REQUIRED, ≥1 entry
        {
          "path": "/abs/part.stl",             // REQUIRED; must exist; only .stl (any subtype) or .obj (normal_part only), case-insensitive
          "count": 2,                          // REQUIRED int ≥1 (number of copies)
          "filaments": [1, 2],                 // REQUIRED (uses .at(), missing → -5); size 1 or count; 1-based filament index; must be non-empty
          "subtype": "normal_part",            // optional: normal_part | negative_part | modifier_part | support_enforcer | support_blocker; unknown string → normal_part
          "assemble_index": [0, 0],            // optional ints; size 1 or count; 0 = stand-alone object, k>0 = merge into object "assemble_k"
          "pos_x": [10, 60], "pos_y": [0, 0], "pos_z": [0, 0],   // optional float arrays; size 1 or count (see 2.4 trap)
          "print_params": { "wall_loops": "3" },                  // optional; per-object config; string values only
          "height_ranges": [ { "min_z": 0.0, "max_z": 5.0, "range_params": { "sparse_infill_density": "50%" } } ]
        }
      ],
      "assembled_params": [                    // optional; config for merged objects
        { "assemble_index": 1, "print_params": { ... }, "height_ranges": [ ... ] }
      ]
    }
  ]
}
```

Type traps: `print_params`, `plate_params`, `range_params` go through
`get<std::map<std::string,std::string>>()`; a JSON number or bool value throws, and the
whole file fails with -5. `pos_*`, `filaments`, `assemble_index` must be arrays, not
scalars. `min_z`/`max_z` are numbers.

### 2.4 How objects are built (`construct_assemble_list`, `BS.cpp:1052-1352`)

1. Each `path` is read into a temporary object named `<stem>_1`; copies are
   `<stem>_2..<stem>_count` (`BS.cpp:1110-1113, 1263-1264`).
2. Object `extruder` = `filaments[0]` (copies: `filaments[i]` if present, else `[0]`)
   (`BS.cpp:1187-1190, 1271-1281`). A coloured OBJ (vertex colours or MTL) instead gets
   per-face colour mapping, sets `filaments[0]=0`, and adds new filament colours; then
   `--load-filaments` is mandatory and `--filament-colour` must not be given
   (`BS.cpp:2561-2604`).
3. `print_params` → `object->config.set_deserialize(key, value)` (`BS.cpp:1199-1206`). It
   accepts **any** key in `print_config_def`; an unknown key throws
   `UnknownOptionException` → caught at `BS.cpp:2178` → -6. Keys applied *after* the
   `extruder` assignment, so `"extruder": "2"` in `print_params` overrides `filaments`.
   Only object/region keys have a slicing effect, but **every** key is written into
   `model_settings.config` as a per-object override.
4. `height_ranges` → `object->layer_config_ranges[{min_z,max_z}]`, only for
   `normal_part`, else -2 (`BS.cpp:1209-1226`).
5. Defaults: empty `pos_*` → `[0]`; empty `assemble_index` → `[0]`, which is only
   allowed for `normal_part` (non-part subtypes need `assemble_index ≥ 1`, else -2)
   (`BS.cpp:1228-1249`).
6. Position: `object->translate(pos_x[0], pos_y[0], pos_z[0])` (`BS.cpp:1251`).
   `ModelObject::translate` adds the vector to each volume offset
   (`Model.cpp:1886-1894`). The STL mesh was centred into the volume, but the volume
   offset keeps the original bounding-box centre (`Model.cpp:3460-3476`). So
   **world position = STL native coordinates + pos** (+ plate origin later). To put a
   part's bbox centre at (X, Y), use `pos = (X, Y) - stl_bbox_centre_xy`, and
   `pos_z = -stl_min_z` to sit it on the bed.
7. **Copy-index bug:** for copy `i`, the code picks `array_index = i` if
   `i < pos_x.size()`, else 0, and then uses that index for `pos_y` and `pos_z` too
   (`BS.cpp:1268-1270`). If `pos_x` has `count` entries but `pos_y` has 1, it reads
   `pos_y[i]` out of bounds. **Always give pos_x, pos_y, pos_z the same length.**
8. Merge (`merge_or_add_object`, `BS.cpp:914-946`):
   - `assemble_index == 0` → a stand-alone copy of the object (its config and height
     ranges kept).
   - `assemble_index == k > 0` → volumes are added to object `assemble_k`, with the
     volume type forced to `subtype`. For `normal_part` and `modifier_part` the source
     object's config (including `extruder` and `print_params`) is applied to the
     **volume** config. For negative/enforcer/blocker volumes it is not.
     The source object's `height_ranges` are **lost** (only volumes are copied). Use
     `assembled_params[k].height_ranges` for merged objects.
   - `assembled_params[k].print_params` → object config of `assemble_k`
     (`BS.cpp:1296-1336`).
9. `plate_params` → `PlateData.config.set_deserialize` (`BS.cpp:1079-1086`). At
   `partplate_list.load_from_3mf_structure` the whole plate config is applied to the
   plate (`src/slic3r/GUI/PartPlate.cpp`, `load_from_3mf_structure`). During slicing
   `new_print_config.apply(*part_plate->config())` applies **every** plate key
   (`BS.cpp:6897`), but the 3MF writer persists only `curr_bed_type`, `print_sequence`,
   `first_layer_print_sequence`, `other_layers_print_sequence(_nums)`, `spiral_mode`,
   `filament_map_mode`, `filament_map`, `filament_volume_map`
   (`bbs_3mf.cpp:8329-8410`). Other keys affect a CLI slice but vanish from the project.
10. `model.add_default_instances()` adds one instance per object at offset 0
    (`BS.cpp:2183`, `Model.cpp:787-794`).

### 2.5 Placement and arrange (`BS.cpp:5320-5535`)

- Plate count of the list must equal the generated plate count, else -2.
- Plates with `need_arrange: false` are locked first.
- `need_arrange: true`: only that plate's objects are arranged (rotation per
  `--allow-rotations`, multicolour per `--allow-multicolor-oneplate`); a wipe-tower
  keep-out is reserved when the plate uses >1 filament; any item that lands on another
  bed → -21 (`BS.cpp:5477-5485`). Arrange moves XY only.
- `need_arrange: false`: `mo->translate_instances(plate_origin)` (`BS.cpp:5505`), so
  `pos` is plate-local (plate 1 origin is (0,0)). If the plate uses >1 filament and the
  config has no `wipe_tower_x`, the tower is set to the default position
  (`BS.cpp:5510-5533`).
- Multiple plates work (up to 36), each with its own objects and settings.

---

## 3. Order of operations in `CLI::run` (`BS.cpp:1488-8268`)

1. `setup()`: data dir, parse argv, split actions/transforms (`BS.cpp:8270-8388`).
2. Temp dir = `wxFileName::GetTempDir()` (i.e. `$TMPDIR`) (`BS.cpp:1558-1559`).
3. No actions → start GUI (`BS.cpp:1604`). Else set log level (`--debug`).
4. Read CLI misc options; `--pipe` (Linux only); skip map (`BS.cpp:1680-1800`).
5. Validate `--clone-objects` / `--load-filament-ids` sizes (`BS.cpp:1810-1840`).
6. Load models: positional files (`BS.cpp:1856-2158`; a Bambu 3MF must be first) or the
   assemble list (`BS.cpp:2160-2185`).
7. `--load-custom-gcodes` parsed (`BS.cpp:2210-2240`).
8. `--load-settings` (machine/process) (`BS.cpp:2314-2401`), `--load-filaments`
   (`BS.cpp:2441-2558`), OBJ colours, `--uptodate` (`BS.cpp:2616-2910`).
9. Process/printer compatibility check (§5.2) (`BS.cpp:2911-3005`), `--check-preset`.
10. Merge machine → process → filament settings into `m_print_config`
    (`BS.cpp:3131-3750`); flush volumes; merge all models into one (`BS.cpp:3940-3956`).
11. Apply custom G-codes to the model (`BS.cpp:4005-4015`).
12. Apply CLI print keys, normalise, `validate()` (fail → -18) (`BS.cpp:4082-4132`).
13. Build the plate list and bed shapes, multi-extruder areas (`BS.cpp:4213-4654`).
14. Downward check (`BS.cpp:4663-4908`).
15. Transforms in argv order (`BS.cpp:4923-5202`).
16. Orient (`--orient 1`) (`BS.cpp:5205-5223`).
17. Arrange: assemble per plate (`BS.cpp:5320-5537`) or global/plate arrange for other
    input (`BS.cpp:5538-6169`).
18. `--ensure-on-bed` (`BS.cpp:6182-6188`). `prepare_time` recorded.
19. Action loop in argv order (`BS.cpp:6330-7457`): slicing happens here.
20. Export 3MF (thumbnails → bboxes → `store_bbs_3mf`) (`BS.cpp:7473-8174`) or PNG export
    (`BS.cpp:8175-8213`).
21. `glfwTerminate`, `export_time` recorded, result.json written with code 0
    (`BS.cpp:8229-8262`).

### 3.1 Where thumbnails are rendered

- Function: `GLCanvas3D::render_thumbnail_framebuffer`
  (`src/slic3r/GUI/GLCanvas3D.cpp:7672-7759`), which calls
  `_render_thumbnail_internal` (`GLCanvas3D.cpp:12378+`). It uses an FBO (4× MSAA when
  available, FXAA post-pass if enabled), then `read_pixel`.
- GL context: `init_opengl_and_colors` (`BS.cpp:6218-6327`): `glfwInit`, an invisible
  640×480 GLFW window; on macOS a Core profile + forward-compatible context
  (`BS.cpp:6249-6252`); version hints come from `glfwGetVersion` (the GLFW library
  version, not the GL version). Then `OpenGLManager::init_gl` (GLEW) and the
  `thumbnail` shader. Every object volume of every instance is uploaded to a
  `GLVolumeCollection`.
- When it runs:
  - During `--slice`, only for non-Bambu printers (`printer_model` not starting with
    "Bambu Lab") to embed G-code thumbnails (`BS.cpp:7174-7181`). The X2D is a BBL
    printer, so not here.
  - At `--export-3mf`, whenever a plate has no thumbnail file. For STL/OBJ/assemble
    input this is always true. Per plate it renders `plate_N.png` (512²),
    `plate_no_light_N.png` (512²), `top_N.png` and `pick_N.png` (512², skipped by
    `--skip-useless-pick` on ≤1 or >64 objects) (`BS.cpp:7680-7900`).
  - `--export-png`.
- If GL init fails (`glfwInit` false or window NULL), `opengl_valid=false`, all
  renders are skipped, and the export still succeeds without previews
  (`BS.cpp:6230-6265, 7680-7682`).
- No CLI option, environment variable or code path disables the plate thumbnails. No
  `getenv` call exists in `BS.cpp` apart from `DISPLAY` (commented out) and `APPIMAGE`.

### 3.2 Unsliced export is valid

If no `--slice` action is given, `sliced_plate == -1` and the 3MF is exported as a
project without G-code (`BS.cpp:7478-7483`); `SaveStrategy::WithGcode` is set, but no
plate has a G-code file. This skips `Print::process` entirely.

### 3.3 The ~5 minute export (**UNVERIFIED**)

The source shows 4 renders of 512×512 per plate and one GL init per process. That
work is small for a hardware context. Candidates for the delay, in order of how to test:

1. GL context creation on macOS from a non-bundle process (GLFW creating an
   `NSApplication`) or a software fallback. Test: run with `--debug=3` and time-stamp
   stderr. Look at the gap between `glfwInit Success.`, `glewInit Sucess.`,
   `plate 1's thumbnail, need to regenerate` and `finished rendering`.
2. The model backup: every `add_volume` calls `Slic3r::save_object_mesh`
   (`Model.cpp:1449, 1465`), which writes meshes into `$TMPDIR/bamboo_model/...`.
3. The per-object model files written to the backup dir, then zipped
   (`bbs_3mf.cpp:6380-6390`).

`result.json` separates `prepare_time` (load → arrange) from `export_time`
(thumbnails + 3MF write). This tells you which half is slow.

---

## 4. Slicing path (`--slice`)

### 4.1 Per plate (`BS.cpp:6431-7420`)

1. `BuildVolume` of the plate; `model.update_print_volume_state`. No object inside →
   -50 (`BS.cpp:6522-6529`).
2. When `--slice n>0` or the first pass of `--slice 0` (`pre_check`): skip handling,
   partly-outside → -52, triangle limit → -59, TPU count → -67, multi-extruder
   reachability and filament map checks → -66 / -68 / -2 (`BS.cpp:6530-6878`).
3. 3MF input without `--load-filaments` and without `--no-check`: every
   `filament_prime_volume` forced to 45 (`BS.cpp:6886-6894`).
4. `new_print_config = m_print_config + plate config + CLI keys` (`BS.cpp:6896-6898`).
   `layer_height`, `wall_loops`, `sparse_infill_density` are copied to result.json from
   the **global** config (`BS.cpp:6899-6904`).
5. Multi-extruder auto map: the CLI invents AMS info (`extruder_ams_count = "1#0|4#1"`,
   4 fake slots per extruder with the project colours/types) (`BS.cpp:6905-6945`).
6. Default `filament_map` = all 1 if shorter than filament count (`BS.cpp:6956-6968`).
7. `print->apply`, `set_no_check_flag`, `validate()`; error mapping: bed/filament
   → -61, temperatures → -62, by-object collision → -63, layer collision → -64,
   other → -51 (`BS.cpp:6970-7012`).
8. With `--slice 0` and more than one plate: first loop validates all plates only;
   second loop slices (`BS.cpp:7020-7021, 7422-7425`).
9. `print->process()`; then conflict result → -101; critical slicing warnings
   (`EmptyGcodeLayers`, `GcodeOverlap`) → -100 unless `--no-check`
   (`BS.cpp:7089-7140`).
10. G-code to `<outputdir>/plate_N.gcode` or a temp file (`BS.cpp:7165-7172`);
    `export_gcode`; G-code checks → -104 / -105 / -102; first-layer filament → -103
    (`BS.cpp:7184-7210`).
11. Any exception during process/export → -100 (`BS.cpp:7409-7415`).
12. With `--slice 0`, Print and G-code move buffers are freed after each plate
    (`BS.cpp:7398-7406`).

### 4.2 "Detect overhangs for auto-lift"

`PrintObject::detect_overhangs_for_lift()` sets status 71 with this text
(`src/libslic3r/PrintObject.cpp:822-830`). It runs after perimeters, infill, ironing and
supports (`src/libslic3r/Print.cpp:2143, 2175`). The next steps have **no** status
message until "Generating skirt & brim" (70):
`detect_extruder_geometric_unprintables`, `calc_estimated_filament_print_time`, then
`_make_wipe_tower()` or `ToolOrdering` (multi-extruder filament grouping)
(`Print.cpp:2190-2232`), then `validate_compacted_wipe_tower_clearance`, skirt/brim, and
finally the conflict checker (`Print.cpp:2418-2433`). A crash "right after" the auto-lift
message is therefore most plausibly in tool ordering / filament grouping / wipe tower
for the two-extruder X2D (**UNVERIFIED**).

### 4.3 Flags that might avoid a headless crash (**UNVERIFIED**)

- Do not slice at all (no `--slice`). The user slices in the GUI.
- If slicing is needed: put `--no-check` **before** `--slice` (skips the conflict
  checker and support-necessity check).
- Pin the filament grouping: `--filament-map-mode=Manual --filament-map=1,1,...`
  (one entry per filament, 1 or 2), so the auto grouping code is not used.
- For a single filament, `--enable-prime-tower=0` removes the wipe-tower step.
- GL is only touched during slicing for non-BBL printers, so GL is not the cause on
  X2D slicing.

### 4.4 `--load-custom-gcodes` JSON (`src/libslic3r/CustomGCode.hpp:47-108`)

```json
{ "mode": "MultiExtruder",
  "gcodes": [ { "type": "PausePrint", "print_z": 12.4, "color": "", "extruder": 1, "extra": "Insert magnets" } ] }
```

`type` ∈ ColorChange | PausePrint | ToolChange | Template | Custom (unknown → Unknown).
`print_z`, `color`, `extruder` are required (`.at()`); `extra` optional. Stored for plate
`slice-1` (plate 1 when `--slice 0` or no slice) (`BS.cpp:2212-2228`). One plate only.

---

## 5. Presets

### 5.1 `--load-settings` / `--load-filaments` file requirements (`BS.cpp:2243-2312`)

| Key | Required | Notes |
|---|---|---|
| `type` | yes | `machine`, `process` or `filament`. Anything else → -5. A `filament` file passed in `--load-settings` is silently ignored. |
| `from` | yes | `system`, `User` or `user`; missing or other → -5. |
| `name` | effectively yes | Becomes `printer_settings_id` / `print_settings_id` / `filament_settings_id`. If empty, the machine/process is treated as not loaded. |
| `inherits` | no | Stored as a string; **not resolved**. For `from=User`, it names the system parent used in compatibility checks. |
| `compatible_printers` (process) | yes in practice | Must contain the printer's system name (§5.2). |
| `filament_id` (filament) | recommended | Goes to `filament_ids` and result.json. |
| `printer_model` (machine) | recommended | Used to look up `model_id` in `machine_full/` (missing on macOS → `printer_model_id` stays empty). |
| G-code keys | recommended | `load_default_gcodes_to_config` only creates **empty** strings for missing keys (`BS.cpp:692-751`). An X2D machine JSON with `include` but without inlined `machine_start_gcode` etc. produces a project with **empty start/end G-code**. |

Other keys: all settings must be present (flattened), because the CLI starts from the
built-in defaults (`FullPrintConfig`) for any key not supplied.

Merge: machine keys copied over the defaults (`update_full_config`, `BS.cpp:3131-3196`),
skipping `compatible_prints, compatible_printers, model_id, inherits, dev_model_name,
name, from, type, version, setting_id, instantiation`. Then process keys, then each
filament's keys into slot `index` of each filament vector (`BS.cpp:3515-3750`).

### 5.2 Compatibility rule (`BS.cpp:2911-3005`)

- Machine + process loaded: OK only if process `compatible_printers` contains the
  printer system name (`name` if `from=system`, else the machine's `inherits`).
  Else -17.
- Only machine loaded (STL input): `current_print_compatible_printers` is empty, so
  -17. **Always load both machine and process for STL/assemble input.**
- Nothing loaded: compatible (built-in defaults; not a valid X2D project).

### 5.3 Filament order

`--load-filaments "f1;f2;f3"`: entry k is filament k (1-based) = the index used by
`filaments` in the assemble list, `--load-filament-ids`, `extruder` keys and
`filament_map` (`BS.cpp:2489-2557, 3548-3575`). Empty entries keep the slot (filled by
the first filament with `--load-defaultfila`). Filament count for STL/assemble input =
number of entries. The assemble-list path does **not** check that `filaments` values
are ≤ that count.

### 5.4 Bed type, filament map, nozzle volume (X2D/H2D)

- `curr_bed_type`: CLI key `--curr-bed-type="Textured PEI Plate"` (enum keys:
  `Cool Plate`, `Engineering Plate`, `High Temp Plate`, `Textured PEI Plate`,
  `Supertack Plate`; `Default Plate` = follow global) (`PrintConfig.cpp:437-444`).
  A `curr_bed_type` key inside the process JSON is also copied (no filter in
  `update_full_config`). Per plate: `plate_params.curr_bed_type`; it wins over global for
  the export bbox (`BS.cpp:8031-8044`), and the CLI key wins over it for slicing
  (`BS.cpp:6897-6898`). Validation: a filament whose bed temperature for that plate
  type is 0 → -61 (`Print.cpp:1669-1693`).
- `filament_map_mode`: `Auto For Flush` (default), `Auto For Match`, `Manual`,
  `Nozzle Manual`, `Auto For Quality` (`PrintConfig.cpp:515-521, 2440-2455`). Set via
  `--filament-map-mode=Manual` or `plate_params`.
- `filament_map`: Ints, one per filament, value = extruder 1 or 2. `-1` entries are
  replaced by the first valid value (`BS.cpp:6702-6742`). Values < 1 in a 3MF are
  clamped to 1 on load (`bbs_3mf.cpp:4666-4672`).
- `nozzle_volume_type`: enum vector, one per extruder: `Standard`, `High Flow`,
  `Hybrid`, `TPU High Flow`, `E3D High Flow` (`PrintConfig.cpp:500-506, 4852-4866`).
  Set via `--nozzle-volume-type="Standard,Standard"`. Missing for a multi-extruder
  printer → error log and Standard is used (`BS.cpp:3410-3436`).
- `extruder_nozzle_count` + `extruder_nozzle_volume_type` (H2C multi-nozzle) and
  `filament_nozzle_map` / `filament_volume_map` (for `Nozzle Manual`).
- `printer_extruder_variant`, `print_extruder_variant`, `filament_extruder_variant`
  are **nocli**; they come only from the preset JSONs. The variant string is
  `"<extruder_type> <nozzle_volume_type>"`, e.g. `Direct Drive Standard`,
  `Bowden High Flow`.

---

## 6. result.json and return codes

Writer: `record_exit_reson` (`BS.cpp:483-607`). File: `<outputdir>/result.json` or
`./result.json` (CWD). Overwritten (`trunc`) on each run; not written when `setup()`
fails or when the process crashes.

Keys: `plate_index` (int), `return_code` (int), `error_string` (string),
`prepare_time` (ms), `export_time` (ms), `layer_height`, `wall_loops`,
`sparse_infill_density` (global values, only set when sliced), optional
`downward_compatible_machine`, `upward_compatible_machine`,
`upward_compatibility_taint`, `sliced_count` (repetitions), and `sliced_plates[]`
with `id, sliced_time, sliced_time_with_cache, make_perimeters_time, infill_time,
generate_support_material_time, triangle_count, warning_message, total_predication (s),
main_predication, filament_change_times, layer_filament_change, obj_cached_cnt,
feature_type_times{role: s}, objects[{id,name,triangle_count,bbox{x,y,z,width,depth,height}}],
filaments[{id, filament_id, total_used_g, main_used_g}]`.

With `--no-check`, a validate error writes the real validator text into `error_string`
instead of the generic message (`BS.cpp:7004-7007`).

Process exit status = `run()` return value; on macOS/POSIX the shell sees it modulo 256
(-2 → 254, -17 → 239, -100 → 156).

Codes (`src/libslic3r/Utils.hpp:21-75`, messages `BS.cpp:122-176`):

| Code | Name | Meaning |
|---|---|---|
| 0 | CLI_SUCCESS | Success |
| -1 | CLI_ENVIRONMENT_ERROR | boost filesystem UTF-8 setup failed |
| -2 | CLI_INVALID_PARAMS | Bad CLI params / combos |
| -3 | CLI_FILE_NOTFOUND | Input, assemble-list or custom-gcode file missing |
| -4 | CLI_FILELIST_INVALID_ORDER | Bambu 3MF not first |
| -5 | CLI_CONFIG_FILE_ERROR | Preset/assemble/custom-gcode JSON invalid |
| -6 | CLI_DATA_FILE_ERROR | Model file unreadable (or unknown key in assemble params) |
| -7 | CLI_INVALID_PRINTER_TECH | Not FFF |
| -8 | CLI_UNSUPPORTED_OPERATION | Unimplemented transform/action |
| -9 | CLI_COPY_OBJECTS_ERROR | |
| -10 | CLI_SCALE_TO_FIT_ERROR | |
| -11 | CLI_EXPORT_STL_ERROR | |
| -12 | CLI_EXPORT_OBJ_ERROR | |
| -13 | CLI_EXPORT_3MF_ERROR | 3MF (or PNG) write failed |
| -14 | CLI_OUT_OF_MEMORY | |
| -15 | CLI_3MF_NOT_SUPPORT_MACHINE_CHANGE | |
| -16 | CLI_3MF_NEW_MACHINE_NOT_SUPPORTED | |
| -17 | CLI_PROCESS_NOT_COMPATIBLE | Process not compatible with printer |
| -18 | CLI_INVALID_VALUES_IN_3MF | `m_print_config.validate()` failed |
| -19 | CLI_POSTPROCESS_NOT_SUPPORTED | |
| -20 | CLI_PRINTABLE_SIZE_REDUCED | (defined, path commented out) |
| -21 | CLI_OBJECT_ARRANGE_FAILED | Arrange spilled to another plate |
| -22 | CLI_OBJECT_ORIENT_FAILED | |
| -23 | CLI_MODIFIED_PARAMS_TO_PRINTER | |
| -24 | CLI_FILE_VERSION_NOT_SUPPORTED | |
| -25 | CLI_3MF_FEATURE_NOT_SUPPORTED | |
| -50 | CLI_NO_SUITABLE_OBJECTS | Empty plate / nothing fully inside |
| -51 | CLI_VALIDATE_ERROR | Generic `Print::validate` error |
| -52 | CLI_OBJECTS_PARTLY_INSIDE | Object crosses bed boundary |
| -53..-57 | cache export/import errors | |
| -58 | CLI_SLICING_TIME_EXCEEDS_LIMIT | `--mstpp` |
| -59 | CLI_TRIANGLE_COUNT_EXCEEDS_LIMIT | `--mtcpp` |
| -60 | CLI_NO_SUITABLE_OBJECTS_AFTER_SKIP | |
| -61 | CLI_FILAMENT_NOT_MATCH_BED_TYPE | |
| -62 | CLI_FILAMENTS_DIFFERENT_TEMP | |
| -63 | CLI_OBJECT_COLLISION_IN_SEQ_PRINT | |
| -64 | CLI_OBJECT_COLLISION_IN_LAYER_PRINT | |
| -65 | CLI_SPIRAL_MODE_INVALID_PARAMS | |
| -66 | CLI_FILAMENT_CAN_NOT_MAP | Filament unreachable on its extruder |
| -67 | CLI_ONLY_ONE_TPU_SUPPORTED | |
| -68 | CLI_FILAMENTS_NOT_SUPPORTED_BY_EXTRUDER | `filament_printable` bit not set |
| -100 | CLI_SLICING_ERROR | Exception or critical warning |
| -101 | CLI_GCODE_PATH_CONFLICTS | |
| -102 | CLI_GCODE_PATH_IN_UNPRINTABLE_AREA | |
| -103 | CLI_FILAMENT_UNPRINTABLE_ON_FIRST_LAYER | |
| -104 | CLI_GCODE_PATH_OUTSIDE | |
| -105 | CLI_GCODE_IN_WRAPPING_DETECT_AREA | |

---

## 7. Bambu 3MF layout (writer `_save_model_to_file`, `bbs_3mf.cpp:6426-6905`)

| Path in zip | Format | Content |
|---|---|---|
| `[Content_Types].xml`, `_rels/.rels` | OPC XML | |
| `3D/3dmodel.model` | 3MF core XML | Build items, project metadata (`Application`, `Title`, `Designer`, `MakerLab`, CLI `--metadata-*`, …). With SplitModel (CLI always), objects live in `3D/Objects/object_N.model` and are referenced as components; rels in `3D/_rels/3dmodel.model.rels`. |
| `3D/Objects/object_N.model` | 3MF XML | Mesh. Per `<triangle>` optional attrs: `paint_supports`, `paint_seam`, `paint_color` (MMU segmentation), `paint_fuzzy_skin`, `face_property` (`bbs_3mf.cpp:316-321, 7735-7780`). Values are `TriangleSelector::get_triangle_as_string` hex bit-streams. |
| `Metadata/model_settings.config` | XML (§7.1) | Per-object / per-part config, plates, assemble info. |
| `Metadata/project_settings.config` | JSON | Full merged config (`save_to_json(..., "project_settings", "project", version)`), nozzle stats split for export (`bbs_3mf.cpp:8082-8091`). |
| `Metadata/slice_info.config` | XML | Header (`X-BBL-Client-Type`, `X-BBL-Client-Version`), per plate: `index, extruder_type, nozzle_volume_type, printer_model_id, nozzle_diameters, timelapse_type, prediction, weight, pause_count, first_layer_time, outside, support_used, label_object_enabled, support_material_on_wipe_tower, enable_filament_dynamic_map, has_filament_switcher, filament_maps`, objects, filaments (`id,type,color,used_m,used_g,…`), warnings, layer filament lists (`bbs_3mf.cpp:8619-8878`). |
| `Metadata/plate_N.png`, `plate_N_small.png`, `plate_no_light_N.png`, `top_N.png`, `pick_N.png` | PNG | Thumbnails (`bbs_3mf.hpp:25-31`). |
| `Metadata/plate_N.json` | JSON | First-layer bboxes (`PlateBBoxData`: `bbox_all`, `bbox_objects[]`, `bed_type`, `filament_ids`, `filament_colors`, `first_extruder`, `nozzle_diameter`, `is_seq_print`). |
| `Metadata/plate_N.gcode` + `.md5` | text | Only for sliced plates. |
| `Metadata/layer_heights_profile.txt` | text | Variable layer height. |
| `Metadata/layer_config_ranges.xml` | XML | `<objects><object id="k"><range min_z max_z><option opt_key="…">value</option>` (`bbs_3mf.cpp:7877-7934`); `id` = 1-based object order. |
| `Metadata/custom_gcode_per_layer.xml` | XML | `<custom_gcodes_per_layer><plate><plate_info id="N"/><layer top_z type extruder color extra gcode/>…<mode value="MultiExtruder"/></plate>` (`bbs_3mf.cpp:8943-8998`). `type`: 0 ColorChange, 1 PausePrint, 2 ToolChange, 3 Template, 4 Custom. |
| `Metadata/cut_information.xml` | XML | `<objects><object id><cut_id id check_sum connectors_cnt/><connectors><connector volume_id type radius height r_tolerance h_tolerance/>` (`bbs_3mf.cpp:8556-8617`). |
| `Metadata/brim_ear_points.txt` | text | |
| `Metadata/filament_sequence.json` | JSON | |
| `Metadata/assembly_tree.json`, `assembly_step.json`, `assembly_model.json` | JSON | Assembly view. |
| `Metadata/process_settings_N.config`, `filament_settings_N.config`, `machine_settings_N.config` | JSON | Embedded user presets (only for 3MF input with project presets). |
| `Auxiliaries/...` | any | Auxiliary files, cover thumbnails. |

### 7.1 `model_settings.config` (`bbs_3mf.cpp:8187-8554`)

```xml
<config>
  <object id="2">
    <metadata key="name" value="part_1"/>
    <metadata key="extruder" value="1"/>            <!-- every object config key, serialized -->
    <metadata face_count="1234"/>
    <part id="1" subtype="normal_part" uuid="…">     <!-- subtype: normal_part|negative_part|modifier_part|support_enforcer|support_blocker -->
      <metadata key="name" value="part_1"/>
      <metadata key="matrix" value="16 numbers"/>
      <metadata key="source_file" value="part.stl"/> <!-- + source_object_id, source_volume_id, source_offset_x/y/z -->
      <metadata key="extruder" value="2"/>           <!-- every volume config key -->
      <mesh_stat face_count="…" edges_fixed="…" degenerate_facets="…" facets_removed="…" facets_reversed="…" backwards_edges="…"/>
    </part>
  </object>
  <plate>
    <metadata key="plater_id" value="1"/>
    <metadata key="plater_name" value="…"/>
    <metadata key="locked" value="false"/>
    <metadata key="bed_type" value="Textured PEI Plate"/>        <!-- from plate config curr_bed_type -->
    <metadata key="print_sequence" value="…"/>
    <metadata key="first_layer_print_sequence" value="1 2"/>
    <metadata key="other_layers_print_sequence" value="…"/>
    <metadata key="other_layers_print_sequence_nums" value="…"/>
    <metadata key="spiral_mode" value="0"/>
    <metadata key="filament_map_mode" value="Manual"/>
    <metadata key="filament_maps" value="1 2"/>                  <!-- only when filament_map_mode is in plate config -->
    <metadata key="filament_volume_maps" value="0 0"/>
    <metadata key="gcode_file" value="Metadata/plate_1.gcode"/>
    <metadata key="thumbnail_file" value="Metadata/plate_1.png"/> <!-- + thumbnail_no_light_file, top_file, pick_file, pattern_bbox_file -->
    <model_instance>
      <metadata key="object_id" value="2"/><metadata key="instance_id" value="0"/><metadata key="identify_id" value="…"/>
    </model_instance>
  </plate>
  <assemble><assemble_item object_id="2" instance_id="0" transform="12 numbers" offset="x y z"/></assemble>
</config>
```

On load, `bed_type` becomes plate `curr_bed_type`; `filament_map_mode="Auto"`
(old files) is ignored (`bbs_3mf.cpp:4618-4680`).

---

## 8. X2D machine profile facts (`resources/profiles/BBL/machine/`)

| Fact | Value | Source |
|---|---|---|
| Model id | `N6` | `Bambu Lab X2D.json` |
| Variants | `Bambu Lab X2D 0.2/0.4/0.6/0.8 nozzle`; 0.2/0.6/0.8 inherit 0.4 and change only `nozzle_diameter`, `printer_variant` | |
| Inherits chain | `X2D 0.4 nozzle` → `fdm_bbl_3dp_002_common` → `fdm_machine_common` | |
| `printable_area` | `0x0, 256x0, 256x256, 0x256` | 0.4 nozzle |
| `printable_height` | 261 | |
| Extruders | 2 (`nozzle_diameter = ["0.4","0.4"]`) | |
| `extruder_type` | `["Direct Drive", "Bowden"]` | |
| `extruder_printable_area` | extruder 1: `0x0,256x0,256x256,0x256` (full bed); extruder 2: `20.5x0,256x0,256x256,20.5x256` (x ≥ 20.5 mm) | |
| `extruder_printable_height` | `["261", "256"]` | |
| Shared reach (both extruders) | x 20.5-256, y 0-256, z ≤ 256 | computed as in `BS.cpp:4245-4277` |
| `printer_extruder_variant` | `Direct Drive Standard/High Flow/E3D High Flow`, `Bowden Standard/High Flow/E3D High Flow` (`printer_extruder_id` 1,1,1,2,2,2) | |
| `extruder_max_nozzle_count` | `["1","1"]` | 002_common |
| `physical_extruder_map` | `["1","0"]` | 002_common |
| `default_nozzle_volume_type` | `["Standard","Standard"]` | 002_common |
| `printer_structure` | `corexy` | fdm_machine_common |
| `bed_exclude_area`, `wrapping_exclude_area`, `head_wrap_detect_zone` | empty | |
| `default_bed_type` | `Textured PEI Plate` | `Bambu Lab X2D.json` |
| `not_support_bed_type` | `Cool Plate` (the CLI default!) | `Bambu Lab X2D.json` |
| Defaults | process `0.20mm Standard @BBL X2D`; filament `Bambu PLA Basic @BBL X2D 0.4 nozzle` | |
| Clearances | `extruder_clearance_height_to_lid=133`, `..._to_rod=32.8`, `max_radius=60` | |
| G-code | via `include` of 5 template files (start, end, layer change, time-lapse, change filament) — not resolved by the CLI | |
| `best_object_pos` | `0.3x0.5` | 002_common |

---

## 9. Implications for a Python wrapper

### 9.1 Always pass

1. One action at least; the GUI starts otherwise. For a project: `--export-3mf=<name>.3mf`.
2. `--outputdir=<existing abs dir>` and a **bare file name** for `--export-3mf`.
   Create the directory first. Delete any old `result.json` there before the run.
3. `--load-settings="<machine.json>;<process.json>"`, both **flattened** (resolve
   `inherits` and `include` in Python from `Contents/Resources/profiles/BBL/`), with
   `type`, `name`, `from` (`system` for an unmodified system preset, else `User` plus
   `inherits` = system parent name), and the process `compatible_printers` containing
   the machine system name.
4. `--load-filaments="<f1.json>;<f2.json>"`, flattened, with `type`, `name`, `from`,
   `filament_id`.
5. `--curr-bed-type="Textured PEI Plate"` (or per plate in `plate_params`).
6. For X2D: `--nozzle-volume-type="Standard,Standard"`. If more than one filament:
   `--filament-map-mode=Manual --filament-map=<one 1|2 per filament>`, after checking
   reach (extruder 2 cannot print at x < 20.5 mm).
7. `--debug=3` (or 4 for percent lines), and capture stderr to a log file. Parse
   progress from the log; `--pipe` is dead on macOS.
8. `--allow-rotations=0` when objects must keep their orientation and arrange is on.

### 9.2 Avoid

- `--min-save` (strips the model).
- `--orient=1` with `--load-settings` (overwrites object configs).
- `--pipe` on macOS (no effect).
- `--export-png` together with any other action.
- Underscore option names, `--no-*` forms, and space-separated bool values.
- An absolute `--export-3mf` together with `--outputdir`.
- `--slice` unless G-code is required. If used: put `--no-check`, `--mtcpp`,
  `--mstpp`, `--export-slicedata` **before** `--slice`.
- `--uptodate`, `--estimate-mode`, `--downward-check` without `--downward-settings`
  (they need `*_full` directories that the macOS bundle does not ship).

### 9.3 Validate before calling the CLI

- Every path exists; assemble-list paths are `.stl` (or `.obj` for normal parts).
- Assemble JSON: `plate_name`, `need_arrange`, `objects`, `path`, `count ≥ 1`,
  non-empty `filaments` present; each array length is 1 or `count`;
  **`pos_x`, `pos_y`, `pos_z` have identical lengths**; all `*_params` values are
  strings; every `print_params`/`range_params`/`plate_params` key exists in
  `print_config_def` (take the key list from a `--export-settings` dump);
  non-part subtypes have `assemble_index ≥ 1`; `height_ranges` only on normal parts
  (or in `assembled_params` for merged objects); 1 ≤ plates ≤ 36.
- Filament indices (assemble `filaments`, `extruder`, `filament_map`) ≤ number of
  `--load-filaments` entries; per-filament CLI vectors have that exact length.
- Placement: compute `pos = target - stl_bbox_centre` in XY and `pos_z = -stl_min_z`
  from the STL itself; check that the footprint is inside 0-256 × 0-256 and below
  261 mm (256 mm and x ≥ 20.5 mm for anything printed by extruder 2).
- Exit status: map `256 - status` back to the negative code, and prefer
  `result.json` `return_code` when the file is fresh. A missing result.json means a
  parse failure (-2 from `setup`) or a crash.

### 9.4 Patch inside the 3MF afterwards (only if needed)

- `Metadata/model_settings.config`: add `<metadata key="bed_type">` per plate, plate
  names, `filament_map_mode`/`filament_maps`, per-part `extruder`, or remove unwanted
  per-object keys that `print_params` wrote.
- `Metadata/project_settings.config`: fix `curr_bed_type`, `filament_colour`,
  start/end G-code if the machine JSON lacked them, `printer_settings_id`,
  `print_settings_id`, `filament_settings_id`, `inherits_group`
  (index 0 = process, 1..N = filaments, N+1 = printer; empty string = system preset).
- `Metadata/custom_gcode_per_layer.xml`: pauses or colour changes for plates other than
  the first (the CLI can only target one plate).
- `Metadata/layer_config_ranges.xml`: height ranges for merged parts.
- Thumbnails: if GL fails, the 3MF has no `plate_N.png`; Bambu Studio regenerates
  previews on open (**UNVERIFIED**).
- Keep the zip entry names and the OPC rels intact; rewrite with the same file names.
