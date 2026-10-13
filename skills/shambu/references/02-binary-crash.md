# 02 - Bambu Studio 02.08.02.61 binary: headless crash, hidden surface, time sink

> **Provenance and licence notice.** Bambu Studio is free software under the AGPL-3.0
> (https://github.com/bambulab/BambuStudio). The findings in this document come from reading that source at tag
> `v02.08.02.61`, inspecting the shipped macOS binary (symbols, strings, crash reports, a debugger session on a local
> re-signed copy) and running experiments with its command line. Short excerpts are quoted for interoperability only.
> No Bambu Studio code is included in shambu, and shambu is not affiliated with or endorsed by Bambu Lab.
> See `DISCLAIMER.md` at the repository root.

Target: `/Applications/BambuStudio.app` (02.08.02.61, universal x86_64 + arm64, 277 MB main binary, not stripped: `nm` lists 412,747 symbols, so every crash frame has a C++ name).
Workspace: `$S = <scratch>/bin-re`.
All CLI runs used `--datadir $S/datadir`. Logs are in `$S/logs/`, outputs in `$S/out/<experiment>/`, the run index in `$S/logs/results.jsonl`.

## Verdict in five lines

1. Headless slicing works on this Mac. The exit 139 is not GL, Metal, AppKit or a missing NSApplication. It is a null-pointer read in the filament-grouping code: the flush matrix is never sized for 2 filaments when the project has no filament colours.
2. Fix: give each filament a colour (`--filament-colour "#000000;#0000FF"` or `"filament_colour": ["#000000"]` in each filament JSON). Then the user's fit-test plate slices headless in 0.6 s.
3. A second, independent crash: a `height_ranges` entry without `layer_height` in `range_params` segfaults in `layer_height_profile_from_ranges`. Add `"layer_height": "0.2"` to every range.
4. The "5.5 minute" export is not compute. `export_time` in `result.json` is in milliseconds (export takes 0.2 s). The real wall-clock sink is a modal AppKit alert ("reopen windows?") that blocks the CLI forever after recent crashes. `CFFIXED_USER_HOME=<private dir>` removes it.
5. The user's `resolve_presets.py` ignores the `include` key, so the X2D machine preset gets a generic start/end G-code and the filament preset gets 1 extruder variant instead of 6. Fix the resolver before any sliced file goes to the printer.

## 1. Headless slice crash

### 1.1 The three crash reports (2026-10-12 19:12:34, 19:12:44, 19:13:05)

All three are identical:

| Field | Value |
|---|---|
| Exception | `EXC_BAD_ACCESS` / `SIGSEGV`, `KERN_INVALID_ADDRESS at 0x0000000000000000` |
| ESR | Data Abort, byte read, translation fault (a read through a null pointer) |
| Faulting thread | 0, `bambustu_main`, `com.apple.main-thread` (so not AppKit off the main thread) |
| Parent | `zsh` / exited process (CLI run, no GUI) |
| Threads | 20 to 21 |

Faulting thread, symbolicated by the report itself (binary has symbols):

```
0  Slic3r::FilamentGroup::calc_one_change_budget(std::vector<unsigned> const&) const + 228
1  Slic3r::FilamentGroup::calc_group_by_enum(int, std::vector<unsigned> const&, std::unordered_map<int, std::vector<int>> const&, int*) + 96
2  Slic3r::FilamentGroup::calc_min_flush_group(int*) + 1612
3  Slic3r::FilamentGroup::calc_filament_group_for_flush(int*) + 68
4  Slic3r::ToolOrdering::get_recommended_filament_maps(Slic3r::Print*, ..., Slic3r::FilamentMapMode, ...) + 1996
5  Slic3r::ToolOrdering::reorder_extruders_for_minimum_flush_volume(bool) + 432
6  Slic3r::ToolOrdering::sort_and_build_data(Slic3r::Print const&, unsigned, bool) + 68
7  Slic3r::Print::_make_wipe_tower() + 120
8  Slic3r::Print::process(...) + 6936
9  Slic3r::CLI::run(int, char**) + 221136
10 main + 192
11 dyld start + 6992
```

No GL, wx, AppKit or Metal frame is on the stack. "Detect overhangs for auto-lift" is only the last progress message before `_make_wipe_tower`: at `--debug 5` it is `default_status_callback: percent=71, ... message=Detect overhangs for auto-lift`, and the filament-map and tool-ordering step follows it (`e21`). It is not the cause.

### 1.2 Root cause (confirmed live under lldb)

`lldb` cannot attach to the shipped app (hardened runtime, no `get-task-allow`). I copied the bundle to `$S/dbg.app` and ad-hoc re-signed only the copy with `get-task-allow`. The original app is untouched. The crash reproduced at the same PC (`0x100cb8704`).

Faulting instruction: `ldr s9, [x24, x23, lsl #2]` with `x24 = 0` (row pointer) and `x23 = 0`.

What the registers and memory show:

- `this->flush_matrix` holds 2 matrices (one per X2D nozzle), as expected.
- Matrix 0, row 0 holds exactly 1 float. Row 1 has a null data pointer (empty vector).
- The used-filament list is `{0, 1}`. The loop reads `matrix[f1][f0]`, so it reads row 1 of a 1 x 1 matrix and dereferences null.

Why the matrix is 1 x 1: the log of every crashing run shows `no filament colors found in projects`. Without project filament colours the CLI skips the code that logs `flush_volumes_matrix before computing` / `after computed`. The project keeps the default single-extruder 4 x 4 matrix (16 values, see `--export-settings` below) and `filament_colour` stays at 1 entry for 2 filaments. The per-nozzle matrices built from that are short, and `calc_one_change_budget` has no bounds check.

When a colour is supplied, the log shows the matrix recomputed for 2 nozzles x 2 x 2 filaments:

```
flush_volumes_matrix before computing: 0,280,280,280,280,0,280,280,280,280,0,280,280,280,280,0
flush_volumes_matrix after computed: 0,90,900,0,0,90,900,0
```

Trigger conditions, all confirmed:

- 2 or more filaments used on a plate (1 filament never crashes, even with 2 loaded: `e02`).
- No colour for the filaments (the user loads the same `filament-pla.json` twice, and that file has no `filament_colour`).
- `--slice`. Plain `--export-3mf` never enters `Print::process`.

Changing `filament_map_mode` to Manual or turning off the prime tower in the process JSON has no effect, because the CLI keeps project keys from the process file at defaults (`e05_*`, log still says `filament map mode is 0`). `--filament-map-mode Manual --filament-map 1,2` on the command line does take effect (log: `mode is 2`), but then it fails later at `could not found extruder_type Bowden ... filament_index 2` and also segfaults (`e07`).

### 1.3 Second crash: height ranges without a layer height

The user's assemble list puts `"range_params": {"extruder": "2"}` on a height range. With the colour fix in place, the fit-test plate then crashes here:

```
0 Slic3r::layer_height_profile_from_ranges(Slic3r::SlicingParameters const&, std::map<std::pair<double,double>, ...>) 
1 Slic3r::PrintObject::update_layer_height_profile(Slic3r::ModelObject const&, ...)
2 Slic3r::Print::validate(...)
4 Slic3r::CLI::run(int, char**) + 217676
```

The function reads the `layer_height` option of each range without a null check. The GUI always writes `layer_height` into a range, and the assemble list does not. Adding `"layer_height": "0.2"` to every `range_params` fixes it (`f05` onward). Another session also hit this crash at 11:33 today (crash reports `BambuStudio-2026-10-13-113331.ips` and `-113348.ips`, same frames).

### 1.4 Third blocker on the real plate: wipe tower outside the bed

With both fixes, the fit-test plate stops with return code -104 (exit 152): "Found G-code outside of the printable area". The log shows the default wipe tower at `wipe_x 15, wipe_y 220`, bbox max y `256.936` on a 256 mm bed. Also, nozzle 2 can only reach x from 20.5 (`extruder_printable_area`). `--no-check` does not bypass it. `--wipe-tower-x 200 --wipe-tower-y 180` slices clean in 0.6 s: 17.73 g filament 1, 1.53 g filament 2, 2692 s estimate, `filament_maps="1 2"`, 0 filament changes (`f10`).

### 1.5 Experiment table

`BS` = `/Applications/BambuStudio.app/Contents/MacOS/BambuStudio --datadir $S/datadir`. `M;P` = `--load-settings "$S/machine.json;$S/process.json"` (user's files, read-only copies). `2F` = the user's PLA file loaded twice. `2C` = `--load-filament-ids 1,2 cube.stl cube2.stl`. Time is wall clock. Exit `-11` = SIGSEGV (shell shows 139). Runs from `v01` onward set `CFFIXED_USER_HOME=$S/home` (section 3).

| # | Command (after `BS M;P`) | Exit | Time | Result |
|---|---|---|---|---|
| e01 | 1 filament, cube, `--slice 0 --export-3mf` | 0 | 0.3 s | Sliced, 4.14 g, 922 s estimate |
| e02 | `2F`, 1 cube, `--slice 0` | 0 | 0.3 s | Works: only filament 1 used |
| e03 | `2F 2C --slice 0` | -11 | 0.1 s | **Reproduces user crash** (same frames, `ips` 11:23:30) |
| e04 | 1 filament, cube, `--export-3mf` only | 0 | 0.3 s | `export_time: 202` (ms) |
| e05 | `2F 2C` + process JSON with `filament_map_mode: Manual` / 8-value flush matrix / prime tower off | -11 | 0.1 s | Project keys in process JSON ignored |
| e06 | e03 under lldb, original app | - | - | Attach denied (hardened runtime) |
| e06b | e03 under lldb, re-signed copy | stop | - | Live bt + registers (section 1.2) |
| e07 | `2F 2C --filament-map-mode Manual --filament-map 1,2` | -11 | 0.1 s | Mode applied, then "could not found extruder_type Bowden", crash |
| e09 | `2F 2C`, filament resolved with `include` | -11 | 0.1 s | Preset fix alone does not help |
| e10 | `2F 2C`, machine + filament resolved with `include` | -11 | 0.1 s | Same |
| e11 | 1 filament, include-resolved presets, `--slice 0` | killed | 624 s | **Hung** at `glfwInit Success.`: modal AppKit alert (section 3) |
| e12 | plain export, `--outputdir` missing | 243 | <1 s | -13 "Failed exporting 3mf files." (output dir must exist) |
| e13a-c | crash, then 2 plain exports | 139, 0, killed | -, 1 s, 120 s | Second export after a crash hung at the alert |
| e14 x3 | plain export, real home | killed | 60 s each | Every run hung at the alert |
| e15 | `-ApplePersistenceIgnoreState YES` | 254 | 0 s | CLI rejects it ("Invalid option") |
| e16 | plain export, `CFFIXED_USER_HOME=$S/home` | 0 | <1 s | **Alert gone** |
| e17 | `2F 2C --filament-colour "#FFFFFF;#000000" --slice 0` | 0 | 0.4 s | **Works.** 50 T0 + 50 T1, flush matrix computed |
| v01 | e03 + `--no-check` | -11 | 0.1 s | Crash (in `Print::process`, before checks) |
| v02 | e03 + `--skip-useless-pick 1` | 254 | 0 s | "1" read as a model file: -2 Invalid parameters |
| v03 | e03 + `--skip-useless-pick` (bare) | -11 | 0.1 s | Crash |
| v04 | e03 + `--min-save 1` | 254 | 0 s | "1" read as a model file |
| v05 | e03 with `--export-slicedata` only | -11 | 0.1 s | Crash |
| v06 | e03 exporting `out.gcode.3mf` | -11 | 0.1 s | Crash |
| v07 | include-resolved presets, no colour | -11 | 0.1 s | Crash |
| v08 | include-resolved presets + `--filament-colour` | 0 | 0.4 s | Works |
| v09 | `--filament-colour "#FFFFFF;#FFFFFF"` (same colour) | 0 | 0.4 s | Works: any colour list fixes it |
| v10 | 1 filament export after many crashes, fixed home | 0 | 0.3 s | No alert |
| f01 | user fit-test assemble list, export only | 0 | 0.4 s | Works |
| f02 | fit-test, `--slice 0`, no colour | -11 | 0.1 s | Flush-matrix crash |
| f03 | fit-test, `--slice 0`, colour flag | -11 | 0.1 s | Height-range crash (section 1.3) |
| f05 | fit-test + `layer_height` in ranges + colour flag | 152 | 0.3 s | -104 G-code outside printable area (wipe tower) |
| f06 | same, no colour | -11 | 0.1 s | Flush-matrix crash |
| e19 | `2C`, colour inside each filament JSON | 0 | 0.4 s | Works, no CLI flag needed |
| f07/f08/f09 | fit-test, colour in JSON (f08 `--no-check`, f09 include-resolved presets) | 152 | 0.3-0.4 s | -104 in all three |
| f10 | f07 + `--wipe-tower-x 200 --wipe-tower-y 180` | 0 | 0.6 s | **Full headless slice of the user's plate**, `fit.gcode.3mf` |
| e20 | export + `--curr-bed-type "Textured PEI Plate"` | 0 | 0.3 s | `curr_bed_type` set in `project_settings.config` |

## 2. Hidden surface

### 2.1 Options not in `--help`

Literal `--` strings in the arm64 slice that `--help` does not list: `--gcodeviewer`, `--single-instance`, `--no-single-instance`, `--scale-to-fit`, `--use-firmware-retraction`. The CLI parser rejects the first four with exit 254 ("Invalid option"). They belong to the GUI launcher or to PrusaSlicer code that is dead here.

The bigger hidden surface: **every print/project config key is a CLI flag**, with `_` written as `-`. Help line 1 says so ("setting values from the command line (highest priority)"). Verified:

| Flag | Effect |
|---|---|
| `--filament-colour "#000000;#0000FF"` | Sets project filament colours. Fixes the 2-filament crash |
| `--filament-map-mode Manual`, `--filament-map 1,2` | Applied (log `filament map mode is 2`), but crashes with the user's presets |
| `--wipe-tower-x 200 --wipe-tower-y 180` | Moves the prime tower |
| `--curr-bed-type "Textured PEI Plate"` | Sets the bed type. Replaces `set_bed_type.py` |
| `--use-firmware-retraction` | Boolean config key (a value after it is read as a file) |

Process-JSON overrides of project-level keys (`filament_map_mode`, `flush_volumes_matrix`, `enable_prime_tower`) had no visible effect in `e05`. Use the flag form for project keys.

Help says "option" for these, but they are bare booleans. A value after them is read as a model file (exit 253 "input files not found" or 254 "Invalid parameters"):
`--skip-useless-pick`, `--min-save`, `--allow-mix-temp`, `--load-defaultfila`.
`--mstpp 60` and `--mtcpp 100000` take a value.

CLI option keys found next to each other in the binary: `outputdir, load_settings, uptodate_settings, uptodate_filaments, downward_settings, load_filaments, skip_objects, downward_check, metadata_name, metadata_value, normative_check, uptodate, load_defaultfila, min_save, enable_timelapse, allow_rotations, skip_modified_gcodes, skip_useless_pick, allow_newer_file, allow_mix_temp, estimate_mode, check_preset, camera_view, avoid_extrusion_cali_region, pipe, makerlab_name, makerlab_version, load_custom_gcodes, load_assemble_list, allow_multicolor_oneplate, load_filament_ids, clone_objects, load_slicedata, export_settings, export_stls, export_3mf, export_png, no_check, export_slicedata`. All are in `--help`.

Validation messages worth knowing (from the strings):

- `load_assemble_list should not be used with input model files to load and should not be sued with transforms`
- `loaded_filament_ids size %1% should be the same with input files size %2%`
- `can not load 3mf when set loaded_filament_ids or clone_objects`
- `should not set export_png with other actions together.`
- `%1%: estimate_mode requires filament_map_mode to be AutoForFlush if explicitly set`

### 2.2 Environment variables

I listed every call site of `_getenv` in the arm64 code with its string argument (objdump). Bambu's own code reads only two:

- `HOME` in `Slic3r::PathSanitizer::init_usrname_range()` (scrubs the user name from logged paths)
- `SHELL` in `Slic3r::run_post_process_scripts` (post-processing is refused under the CLI anyway: code -19)

`SLIC3R_PP_HOST` and `SLIC3R_PP_OUTPUT_NAME` are set for post-processing scripts, not read.

The rest belong to bundled libraries: `TMPDIR/TMP/TEMP/TEMPDIR`, `LANG/LC_ALL/LC_CTYPE`, `LC_PATH` (wx), `TBB_VERSION`, `TBB_MALLOC_*`, `LIBIGL_NUM_THREADS`, `OPENCV_*`, `MMGT_*` (OpenCascade), `BLOSC_*`, `OPENVDB_DISABLE_DELAYED_LOAD`, `FREETYPE_PROPERTIES`, `JPEGMEM`, `LIBTIFF_*`, `EXPAT_ENTROPY_DEBUG`, `APP_SANDBOX_CONTAINER_ID`, and curl/OpenSSL (`NO_PROXY`, `SSLKEYLOGFILE`, `CURL_SSL_BACKEND`, `SSL_CERT_*`, `OPENSSL_*`).

There is **no** `BAMBU_*`, `BBL_*`, `WX*`, `MESA`/`LIBGL_ALWAYS_SOFTWARE`, `QT_*` or `*_DEBUG` switch. Nothing forces software GL or turns off thumbnails. None is needed: the GL path is hardware (below).

The one environment variable that matters is a macOS one: `CFFIXED_USER_HOME` (section 3).

### 2.3 Graphics and frameworks

`otool -L` (arm64): OpenGL.framework, AppKit, Cocoa, QuartzCore, WebKit, AVFoundation/AVKit, CoreMedia, VideoToolbox, ModelIO, Security, Carbon, IOKit, DiskArbitration, libc++, libz, libbz2, liblzma, libiconv. **No Metal.framework link.** wxWidgets, GLFW, OpenCV, OpenVDB, OCCT, TBB and curl/OpenSSL are linked statically (paths in strings: `.../dep_wxWidgets-prefix/...`).

Headless thumbnails use GLFW (`glfwInit`, `glfwCreateWindow`, `_glfwPlatformCreateWindow` → `[NSApplication run]`). The log reports `got opengl version 4.1 Metal - 90.5, ... vendor Apple, graphics card model Apple M5 Max` and `FrameBuffer normal`. This is Apple's hardware GL-on-Metal layer, not software rendering. Thumbnail rendering takes about 2 ms per image.

Code signing: hardened runtime, team `T3UBR9Y3B2`, entitlements `cs.allow-unsigned-executable-memory` and `cs.disable-library-validation`. No `get-task-allow`, so lldb needs a re-signed copy.

### 2.4 Info.plist

- Bundle id `com.bambulab.bambu-studio`, version `02.08.02.61`.
- URL types: one, `CFBundleURLName "bambustudioopen url"`, scheme `bambustudioopen`.
- Document types (all role Viewer, rank Alternate): stl, obj, amf, 3mf, gcode.
- No `LSEnvironment`. `NSAppTransportSecurity.NSAllowsArbitraryLoads = true`.

### 2.5 URL schemes

`bambustudioopen://` (registered, handled in `GUI_App::MacOpenURL`, from the disassembly):

1. Take the text after `bambustudioopen://` (split on `//`), then `Http::url_decode` it. The whole remainder is the download URL. There are no named parameters.
2. Trusted when it starts with `http(s)://makerworld` or `http(s)://public-cdn.bblmw.com`, or contains `amazonaws.com` or `aliyuncs.com`. Else the app shows "This file is not from a trusted site, do you want to open it anyway?".
3. `GUI_App::sanitize_download_url`, then `Plater::request_model_download` downloads and opens the model or project.

Example form: `bambustudioopen://https%3A%2F%2Fmakerworld.bblmw.com%2F...%2Fmodel.3mf`.

`bambustudio://open` (not in Info.plist, so macOS does not route it): `GUI_App::post_init` checks whether an input file argument starts with this prefix. It then URL-decodes it, runs `split_str`, and applies the same trusted-host checks and download. I did not confirm the parameter name. OrcaSlicer uses `?file=<url>`, and a `"file_"` literal sits nearby. Treat `bambustudio://open?file=` as unverified. Neither scheme can set print parameters. Both open the GUI, so a headless wrapper cannot use them.

### 2.6 The `--pipe` progress protocol

`--pipe <name>` is accepted and the log prints `Will use pipe <name>`. A FIFO at that path received **0 bytes** during a full 2-filament slice (`e18`). The binary has no JSON progress keys (`plate_percent`, `total_percent` and similar are absent). On macOS the status callback is `default_status_callback`, which only logs:

```
default_status_callback: percent=%1%, warning_step=%2%, message=%3%, message_type=%4%
```

For progress, parse those log lines (they appear at `--debug 4` and above). `result.json` is the end-of-run record.

### 2.7 Return codes

The process exit status is `return_code & 0xFF` (for example -2 → 254, -13 → 243, -104 → 152). `result.json` always holds the signed value and `error_string`. The table comes from the static initialiser at `0x1000d9218` (the `mov w8,#-N` before each string). Codes I saw at runtime are marked ✔.

| Code | Exit | Message |
|---|---|---|
| 0 ✔ | 0 | Success. |
| -1 | 255 | Failed setting up server environment. |
| -2 ✔ | 254 | Invalid parameters to the slicer. (also any "Invalid option") |
| -3 ✔ | 253 | The input files to the slicer are not found. |
| -4 | 252 | File list order to the slicer is invalid (3mf must be first). |
| -5 | 251 | The input preset file is invalid and can not be parsed. |
| -6 | 250 | The input model file to the slicer can not be parsed. |
| -7 | 249 | Unsupported printer technology (not FDM). |
| -8 | 248 | Unsupported CLI instruction. |
| -9 | 247 | Failed copying objects. |
| -10 | 246 | Failed scaling an object to fit the plate. |
| -11 | 245 | Failed exporting STL files. |
| -12 | 244 | Failed exporting OBJ files. |
| -13 ✔ | 243 | Failed exporting 3mf files. (output dir missing) |
| -14 | 242 | Out of memory during slicing. |
| -15 | 241 | The selected printer is not supported. |
| -16 | 240 | The selected printer is not compatible with the 3mf. |
| -17 | 239 | The selected printer is not compatible with the process preset in the 3mf. |
| -18 | 238 | Invalid parameter value(s) included in the 3mf file. |
| -19 | 237 | post_process is not supported under CLI. |
| -20 | 236 | Printer bed smaller than the bed size used in the print profile. |
| -21 | 235 | An error occurred when auto-arranging object(s). |
| -22 | 234 | An error occurred when auto-orienting object(s). |
| -23 | 233 | You cannot change the Printable Area, Printable Height, and Exclude Area. |
| -24 | 232 | Unsupported 3MF version. |
| -25 | 231 | Unsupported (experimental) features in this 3MF. |
| -50 | 206 | One of the plates is empty or has no object fully inside it. |
| -51 | 205 | Incorrect slicing parameters in the 3mf. |
| -52 | 204 | Some objects are located over the boundary of the heated bed. |
| -53 to -57 | 203-199 | Slice-cache errors (create dir, export, not found, parse, import). |
| -58 | 198 | Slicing time of a plate exceeds the limit (`--mstpp`). |
| -59 | 197 | Triangle count of a plate exceeds the limit (`--mtcpp`). |
| -60 | 196 | No printable objects to slice after skipping. |
| -61 | 195 | Filaments are not compatible with the plate type. |
| -62 | 194 | Filament temperature difference too large (see `--allow-mix-temp`). |
| -63 | 193 | Object conflicts in print-by-object mode. |
| -64 | 192 | Object conflicts were detected. |
| -65 | 191 | Parameters incompatible with Spiral Vase mode. |
| -66 | 190 | Some filaments cannot be mapped to correct extruders (multi-extruder). |
| -67 | 189 | Not support printing 2 or more TPU filaments. |
| -68 | 188 | Some filaments cannot be printed on the extruder mapped to. |
| -100 | 156 | Failed slicing the model. |
| -101 | 155 | G-code conflicts detected after slicing (move the wipe tower). |
| -102 | 154 | G-code in unprintable area of multi-extruder printers. |
| -103 | 153 | Some filament unprintable at first layer on this plate type. |
| -104 ✔ | 152 | G-code outside of the printable area (support, wipe tower, brim, skirt). |
| -105 | 151 | G-code in the wrapping detect area. |
| signal | 139 / -11 | SIGSEGV (sections 1.2, 1.3). `result.json` is NOT written. |

`result.json` keys: `error_string, export_time, layer_height, plate_index, prepare_time, return_code, sparse_infill_density, wall_loops`. After a slice it also has `sliced_plates[]` with `feature_type_times, filament_change_times, filaments[] {filament_id, id, main_used_g, total_used_g}, generate_support_material_time, id, infill_time, layer_filament_change, main_predication, make_perimeters_time, obj_cached_cnt, objects[] {bbox {depth,height,width,x,y,z}, id, name, triangle_count}, sliced_time, sliced_time_with_cache, total_predication, triangle_count, warning_message`. Times are in **milliseconds** except `*_predication` (seconds). On an early error `prepare_time` is garbage (`4451762828`), so do not read it.

### 2.8 Assemble list keys

The loader (`load_assemble_plate_list`, `construct_assemble_list`) knows exactly the keys the skill already lists: `plates, plate_name, need_arrange, plate_params, objects, path, count, filaments, subtype, assemble_index, pos_x, pos_y, pos_z, print_params, height_ranges, min_z, max_z, range_params, assembled_params`. I found no other keys. Facts from the neighbouring messages:

- `filaments`, `assemble_index`, `pos_x/y/z`: each list must have length 1 or `count` ("count %2% not equal to clone count %3%, also not equal to 1").
- `path` can be STL or **OBJ**. OBJ colours with an `.mtl` are converted into filaments ("convert_obj_cluster_colors", "mtl lost material").
- "only normal part can have height ranges", "only normal part can be used as individual object". Subtype names in the binary: `normal_part, negative_part, modifier_part, support_enforcer, support_blocker`. The JSON probably takes the enum integer: the log prints `subtype %4%` as a number. Unverified.
- `range_params` must contain `layer_height`, or slicing segfaults (section 1.3).
- `plate_params`, `print_params` and `assembled_params` are logged as "key %2%, value %3%" pairs (string key/value config).

## 3. Time sink

### 3.1 What I measured

User's fit-test assemble list (4 STLs, 2 filaments), 3 runs each, `CFFIXED_USER_HOME=$S/home`:

| Variant | Exit | Wall time (3 runs) | `export_time` (ms) | Notes |
|---|---|---|---|---|
| T01 plain `--export-3mf` | 0 | 0.3 / 0.3 / 0.3 s | 210 / 198 / 200 | 90,737-byte 3MF |
| T02 `--skip-useless-pick` | 0 | 0.3 / 0.3 / 0.3 s | 197 / 198 / 200 | No change without `--slice` (pick/top PNGs still written) |
| T03 `--min-save` | 0 | 0.2 / 0.2 / 0.3 s | 195 / 193 / 200 | **Drops `3D/Objects/*.model`: the 3MF has no meshes. Do not use** |
| T04 both | 0 | 0.3 s | 210-219 | Same as T03 |
| T05 `--debug 0` | 0 | 0.3 s | 207-228 | No gain |
| T06 `--export-png 0` (no 3MF) | 0 | 0.2-0.3 s | 187-203 | Writes `plate_1_0.png` only |
| T07 `--debug 5` | 0 | 0.3 s | 206-221 | No cost |
| f10 full `--slice 0` + `.gcode.3mf` | 0 | 0.6 s | - | Slice itself 0.24 s |

The 0.3 s breaks down from the log time stamps: about 200 ms is GLFW and GL context set-up (`opengl version 3.3.7` to `init_opengl_and_colors finished`), and 5 thumbnails take about 2 ms each.

### 3.2 Where the 5.5 minutes go

1. **`export_time` is milliseconds.** The user's `result.json` says `"export_time": 230`, and my runs say 187 to 235 for the same kind of export. 230 to 345 means 0.23 to 0.35 s, not 230 to 345 s.
2. **The wall-clock wait is a modal alert.** After a BambuStudio crash, macOS writes `~/Library/Application Support/CrashReporter/BambuStudio_<uuid>.plist`. The next launches then go through `glfwCreateWindow` → `[NSApplication run]` → `_handleAEOpenEvent` → `NSPersistentUIRestorer promptToIgnorePersistentStateWithCrashHistory` → `[NSAlert runModal]`. That is the "The application quit unexpectedly while reopening windows" dialog. `sample` showed the main thread parked in `_DPSNextEvent` at 0 % CPU (`$S/logs/e11_sample.txt`). It is not CPU-bound and not software GL. The CLI stays blocked until someone clicks the dialog or kills the process. The log stops at `glfwInit Success.`
   - Reproduced: e11 (624 s, killed), e13c, and e14 x3 (each killed at 60 s). Each one came right after crash runs. The first launch right after a crash sometimes passes (e13b) because ReportCrash writes the history asynchronously.
   - Other sessions on this Mac crashed BambuStudio 18 times between 11:23 and 11:34 today. That keeps the history fresh, so any CLI run can hang.
   - `make_plate.py` starts several exports in parallel with `--slice` crashes in the mix. That matches "about 10 minutes even without slicing".
3. **Fix, measured:** `CFFIXED_USER_HOME=<private empty dir>` gives AppKit an empty preferences/crash-history root. The same plain export then finished in under 1 s, and every later run did too (e16, v10, T01 to T07). The time saved is the whole wait: from 60 to 624+ s (unbounded) down to 0.3 s per export. The variable does not touch the real `~/Library`. Bambu data stays under `--datadir`.

`-ApplePersistenceIgnoreState YES` on argv cannot work: the CLI rejects it (exit 254). `defaults write com.bambulab.bambu-studio ApplePersistenceIgnoreState -bool YES` would probably work too, but it changes the user's real preferences, so I did not test it.

**Fastest working combination:** `CFFIXED_USER_HOME=$PRIVATE_HOME BS --datadir D --load-settings "machine;process" --load-filaments "f1;f2" --load-assemble-list A.json --curr-bed-type "Textured PEI Plate" --export-3mf X.3mf --outputdir OUT` gives 0.3 s. Add `--slice 0` plus the colour, range and wipe-tower fixes and it gives 0.6 s with G-code. No other flag saves measurable time.

## 4. `--info` and `--export-settings`

### 4.1 `--info` output

`BS --datadir D --info file.stl [file2.stl ...]` exits 0 with no GL and no `--outputdir`. Line 1 of stdout/stderr is a `[trace]` log line. Then, per file:

```
[cube.stl]
size_x = 20.000000
size_y = 20.000000
size_z = 20.000000
min_x = 0.000000
min_y = 0.000000
min_z = 0.000000
max_x = 20.000000
max_y = 20.000000
max_z = 20.000000
number_of_facets = 12
manifold = yes
number_of_parts =  1
volume = 8000.000488
```

Parser rules:

- Section header `[<basename>]`, one per input file, in input order.
- Each line is `key = value` with exactly `" = "`. **`number_of_parts =  1` has two spaces**, so strip the value.
- Floats have 6 decimals. Units are mm and mm³.
- Always present: `size_x size_y size_z min_x min_y min_z max_x max_y max_z number_of_facets manifold number_of_parts volume`.
- Conditional keys (printed only when non-zero, from the strings): `non_manifold_edges, non_manifold_vertices, open_edges, degenerate_facets, edges_fixed, facets_removed, facets_reversed, backwards_edges`. Their position is between `manifold` and `number_of_parts`.
- **`manifold` is not reliable.** A cube with one facet removed printed `manifold = yes` together with `open_edges = 3` (and volume 6666.67). Treat the file as closed only when `open_edges`, `non_manifold_edges` and `non_manifold_vertices` are all absent or 0.

Logs: `$S/logs/info_cube.txt`, `info_open.txt`, `info_two.txt`.

### 4.2 `--export-settings`

`BS --datadir D --load-settings "M;P" --load-filaments "F;F" --export-settings settings.json --outputdir OUT` exits 0, with or without a model (both outputs are byte-identical). It also writes `result.json` in `--outputdir`. The file is one flat JSON object, 578 keys, `"name": "project_settings"`, `"from": "project"`, `"version": "02.08.02.61"`. It is the merged project config, the same as `Metadata/project_settings.config` in a 3MF, **before** the model-dependent steps. It shows the crash precondition: `filament_colour` has 1 entry for 2 filaments, `flush_volumes_matrix` has the 16-value 4 x 4 default, `filament_map` is `["1"]`, and `curr_bed_type` is `"Cool Plate"`. Preset metadata keys (`type`, `instantiation`, `setting_id`, `include`, `description`, `compatible_printers`, `filament_id`) are dropped. Saved at `$S/settings.json`.

## 5. Preset resolver defect (found on the way)

The X2D system presets use `"include": [...]` templates as well as `inherits`. `<project>/plates/resolve_presets.py` follows `inherits` only. Compared with an include-aware resolve (`$S/resolve.py`, merge order parent → includes → own keys):

- `machine.json`: `machine_start_gcode`, `machine_end_gcode`, `layer_change_gcode`, `time_lapse_gcode` and `change_filament_gcode` come from the generic parent, not the X2D templates. The start G-code in the user's file is a generic `G28 / G29 / M109 S205 / purge line at X10`. It is not the X2D sequence. **A G-code sliced from these presets must not go to the printer.** GUI slicing of the exported 3MF probably carries the same embedded G-code. I did not verify that.
- `filament.json`: 34 keys keep the single-variant base value instead of 6 per-variant values, including `filament_extruder_variant` (`["Direct Drive Standard"]` instead of DD and Bowden x Standard/High Flow/E3D), retraction, z-hop, wipe, ramming and overhang speeds. This causes the `could not found extruder_type Bowden` error in Manual map mode.

## Recommendations for the wrapper

1. **Always set `CFFIXED_USER_HOME`** to a private, persistent, empty directory (for example `<scratch>/bambu-home`) for every CLI call. Also keep `--datadir`. This removes the modal crash-history alert, which is the real multi-minute "export time". Also set a hard timeout (60 s for export, 300 s for slice). Treat a timeout as "alert or hang", never as "still working".
2. **Give each filament a colour.** Write `"filament_colour": ["#RRGGBB"]` into each resolved filament JSON (the user's real colours), or pass `--filament-colour "c1;c2"`. Without it, any 2-filament `--slice` segfaults.
3. **Put `"layer_height": "<process layer height>"` in every `height_ranges[].range_params`.**
4. **Place the prime tower** with `--wipe-tower-x/--wipe-tower-y` inside both nozzles' area: x from 20.5 to (256 - tower width - brim), y + about 37 ≤ 256. `200,180` worked, and so might anything in x 25 to 195 that keeps clear of parts. If parts are on filament 2, keep the tower x ≤ 235.5 - 37.
5. **Fix `resolve_presets.py`** to merge `include` templates (`$S/resolve.py` is a 20-line reference). Re-generate `config/*.json` before slicing anything for the printer.
6. Flags: use `--skip-useless-pick`, `--allow-mix-temp` and `--load-defaultfila` **bare**. Never use `--min-save` for a project the user opens (the meshes are dropped). Use `--curr-bed-type "Textured PEI Plate"` instead of patching the zip. Create `--outputdir` first (else -13). Do not pass `-Apple*` or other argv-domain flags (exit 254).
7. Read the outcome from `result.json` (`return_code`, `error_string`, and `sliced_plates[].warning_message`), not from stdout. Map the exit status with the table in 2.7. Exit 139 means a crash, and `result.json` is missing or stale. Delete `result.json` before each run so a stale one is never read.
8. Progress: `--pipe` does nothing on macOS. Tail the log for `percent=` lines (`--debug 4`) if progress is needed.
9. Parse `--info` with the rules in 4.1. Use `open_edges` and the other conditional keys, not `manifold`.
10. Run CLI jobs one at a time. With the alert removed, an export is 0.3 s and a slice 0.6 s, so parallel runs gain nothing. They only add crash history when a run fails.

Artefacts: `$S/resolve.py`, `$S/run.py`, `$S/ex.sh`, `$S/lbt.sh` (lldb backtrace on the re-signed copy), `$S/errcodes.tsv`, `$S/strings_arm64.txt`, `$S/settings.json`, `$S/out/f10_fit_wipetower_moved/fit.gcode.3mf` (headless-sliced user plate, generic start G-code, **do not print**). `$S/dbg.app` (674 MB re-signed copy) can be deleted.
