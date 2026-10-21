# 07 - Print recipes from the 3D printing notes (Bambu Studio 02.08.02.61, X2D 0.4)

> **Provenance and licence notice.** Bambu Studio is free software under the AGPL-3.0
> (https://github.com/bambulab/BambuStudio). The findings in this document come from reading that source at tag
> `v02.08.02.61`, inspecting the shipped macOS binary (symbols, strings, crash reports, a debugger session on a local
> re-signed copy) and running experiments with its command line. Short excerpts are quoted for interoperability only.
> No Bambu Studio code is included in shambu, and shambu is not affiliated with or endorsed by Bambu Lab.
> See `DISCLAIMER.md` at the repository root.

Sources: `the author's print notes` (cited as "notes"), the resolved presets
`x2d-presets-resolved.json` (process `0.20mm Standard @BBL X2D`, filament `Bambu PETG Basic @BBL X2D 0.4 nozzle`,
machine `Bambu Lab X2D 0.4 nozzle`), `src/libslic3r/PrintConfig.cpp` / `PrintConfig.hpp` / `Print.cpp` /
`Format/bbs_3mf.cpp` (cited as `PC:`, `PH:`, `P:`, `F:`), the shipped profiles in
`bin-re/dbg.app/Contents/Resources/profiles/BBL/`, and report 03 (3MF format). The Bambu wiki returned
HTTP 402 to WebFetch, so nothing here comes from the wiki. Every value below is a string, as in a preset.

## Contents

| Section | What it holds |
|---|---|
| [0. Scope rules a library needs before any recipe](#0-scope-rules-a-library-needs-before-any-recipe) | Where each key can live: process, object, height range, plate |
| [1. Tip table](#1-tip-table) | Every tip from the notes with its scope, exact keys and caveats |
| [2. Named recipes (paste into a Python dict)](#2-named-recipes-paste-into-a-python-dict) | The named recipes as Python dict entries |
| [3. Facts for the skill text, not for code](#3-facts-for-the-skill-text-not-for-code) | CAD and handling facts for the skill text, not for code |
| [4. Contradictions between the notes and the presets or machine profile](#4-contradictions-between-the-notes-and-the-presets-or-machine-profile) | Where the notes disagree with the presets or the machine profile |

## 0. Scope rules a library needs before any recipe

Where a key can live is set by its config class in `PH`:

| Class (PH line) | Can be set in | Examples |
|---|---|---|
| `PrintObjectConfig` (PH:946) | process, per-object (`model_settings.config` object metadata) | `brim_type`, `brim_width`, `brim_object_gap`, `elefant_foot_compensation`, `layer_height`, `raft_layers`, `seam_position`, `enable_support`, `support_type`, `support_style`, `support_filament`, `support_interface_filament`, `support_top_z_distance`, `support_on_build_plate_only`, `support_threshold_angle`, `support_interface_top_layers`, `xy_hole_compensation`, `xy_contour_compensation`, `enable_circle_compensation`, `wall_generator` |
| `PrintRegionConfig` (PH:1062) | process, per-object, height range, modifier part | `wall_loops`, `top_shell_layers`, `bottom_shell_layers`, `sparse_infill_density`, `sparse_infill_pattern`, `outer_wall_line_width`, `inner_wall_line_width`, `fuzzy_skin*`, `ironing_*`, `wall_filament`, `sparse_infill_filament` |
| `PrintConfig` (global, PH:1402) | process / project only (one value per plate) | `curr_bed_type`, `print_sequence`, `enable_prime_tower`, `timelapse_type`, `initial_layer_print_height`, `independent_support_layer_height` |

- A modifier part (`subtype="modifier_part"`) and a height range take region keys, plus `extruder` (filament slot) and,
  for a height range, `layer_height`. Report 03 section 2.6 has the `layer_config_ranges.xml` form, keyed by 1-based
  build order, not by object id.
- Keys missing from the resolved JSON take their `PrintConfig.cpp` default. Important ones: `brim_type` = `"auto_brim"`
  (PC:1702), `curr_bed_type` is per project (report 03 says the CLI writes `"Cool Plate"`), `timelapse_type` = `"0"`
  (Traditional), `wall_sequence` = `"inner wall/outer wall"`, `internal_solid_infill_pattern` = `"zig-zag"`,
  `support_interface_not_for_body` = `"1"`, `independent_support_layer_height` (global).
- The resolved preset still carries the legacy key `"wall_infill_order": "inner wall/outer wall/infill"`. The loader
  maps it to `wall_sequence` (PC:7324). A library must write `wall_sequence`, never `wall_infill_order`.
- "Rectilinear" is not a valid `sparse_infill_pattern` string. The key for rectilinear is `"zig-zag"` (PC:221).
  `"rectilinear"` is valid only in support enums.
- Filament slots and nozzles: `filament_printable` bit 0 = left, bit 1 = right (PC:2900). Index 0 of every per-extruder
  machine array is the left (main, Direct Drive) nozzle. Index 1 is the right (auxiliary, Bowden) nozzle.
  `filament_map` / plate `filament_maps` are 1-based: `"1"` = left, `"2"` = right.
- Prime tower rules (P:1472-1547): the "no prime tower with `print_sequence` `"by object"` and more than one extruder"
  check sits inside `#if 0` and never runs (corrected 2026-10-13, reference 08 section 2); Studio instead switches the
  tower off itself on a by-object plate with more than one object (`normalize_fdm_2`). With more than one object on the
  plate, every object must share `layer_height`, `initial_layer_print_height`, `raft_layers` and any variable-layer
  profile. With supports on and `independent_support_layer_height` off, the slicer rounds the support gap to a
  multiple of the layer height (no error).
- Line width validation (P:1575-1591): width must be > layer height and <= 2.5 x nozzle = `1.0` mm on a 0.4 nozzle.

## 1. Tip table

Scope words: **process** (whole plate), **per-object**, **height range**, **modifier** (modifier mesh), **plate**
(plate or project metadata), **filament**, **user-in-GUI** (a GUI tool or a physical step), **CAD not slicer**.

| # | Tip (short) | Notes line | Scope | Exact key(s) and value(s) | When to apply | Caveats |
|---|---|---|---|---|---|---|
| 1 | More walls, not more infill | notes | process or per-object | `"wall_loops": "6"` (gripper PETG), `"top_shell_layers": "5"`, `"bottom_shell_layers": "5"`. Preset: `wall_loops` `"2"`, top `"5"`, bottom `"3"` | Functional or loaded parts. Thin parts get most strength from walls | Six 0.45 mm loops fill any section under about 5.4 mm, so infill then does nothing |
| 2 | Infill 40 % gyroid for functional PETG | notes | process or per-object | `"sparse_infill_density": "40%"`, `"sparse_infill_pattern": "gyroid"` | gripper brackets, carriers, jaws | Preset is `"15%"` `"grid"` |
| 3 | 100 % only on parts the stress maths sized as solid | notes | per-object | `"sparse_infill_density": "100%"` | Pinion, racks, rails | At 100 % the slicer prints solid infill with `internal_solid_infill_pattern` (default `"zig-zag"` = rectilinear), so `sparse_infill_pattern` is ignored. "100 % on everything" was wrong and cost mass |
| 4 | 15-20 % infill is plenty, add walls instead | notes | process | `"sparse_infill_density": "15%"` (preset) to `"20%"`, `"wall_loops": "3"` or more | General PLA parts | Preset already gives 15 % grid, 2 walls |
| 5 | 5 % infill for decorative parts | notes | process or per-object | `"sparse_infill_density": "5%"`, optionally `"sparse_infill_pattern": "gyroid"` or `"lightning"` | Display parts with no load | Top skins can sag over 5 % infill. Keep `top_shell_layers` at `"5"` |
| 6 | Per-object settings on one shared plate | notes | per-object | any object or region key as object metadata, e.g. `<metadata key="sparse_infill_density" value="100%"/>` | Mixed plates: one process, overrides per part | Report 03: survives CLI load and `--export-3mf`. `layer_height` per object breaks the prime tower rule |
| 7 | Raise infill only around bolt holes | notes | modifier | a `modifier_part` volume (cylinder or box around the hole) with `"sparse_infill_density": "100%"` or `"wall_loops": "6"` | Bolt bosses on an otherwise light part | Needs a mesh. The headless CLI cannot add one. The library must write the volume into the 3MF itself (report 03: "not done"). Only region keys work on a modifier |
| 8 | Layer height is the main time lever | notes | process or per-object | `"layer_height"`: `"0.08"` to `"0.28"` (machine `min_layer_height` / `max_layer_height`). Shipped presets: 0.08 / 0.12 / 0.16 / 0.20 / 0.24 | Straight-sided parts: thick. Curved tops: thin or adaptive | Different `layer_height` on two objects of one plate fails the prime tower check |
| 9 | Adaptive (variable) layer height | notes | user-in-GUI, or per-object 3MF data | No process key: `adaptive_layer_height` is commented out (PC:3369, PH:1032). The GUI "Variable layer height" tool (Adaptive + Smooth) writes `Metadata/layer_heights_profile.txt`: `object_id=<1-based>|z0;h0;z1;h1;...` (F:7842-7860) | Curved tops (Pikachu: 1 h 8 min vs 3.5 h fine) | A library could compute the profile itself and write the file, but that is new code. With several objects and a prime tower, all must share one profile (P:1494-1547) |
| 10 | Line width wider than the nozzle | notes | process, per-object or modifier | `"outer_wall_line_width": "0.75"`, `"inner_wall_line_width": "0.75"` (preset 0.42 / 0.45). Allowed range > layer height, <= `"1.0"` | Fast boxes with 1.5 mm walls: 2 loops instead of 3, 40 to 32 min | Wide lines need more volumetric flow. PETG cap is `filament_max_volumetric_speed` 15 mm3/s (DD standard), so the slicer slows down. Not tested on the X2D |
| 11 | Bigger nozzle (0.6 / 0.8) | notes | machine (user hardware) | machine preset `"Bambu Lab X2D 0.6 nozzle"` / `0.8` | Older slow printers | Notes say little gain on fast printers like the X2D |
| 12 | Elephant foot | notes | per-object (slicer) + CAD | `"elefant_foot_compensation": "0.15"` (already the preset value; class default is 0) | Always on | Notes fix it in CAD (0.4 x 0.6 relief chamfer) on top of the preset 0.15. Where a relief is not allowed, deburr by hand. Do not raise the key for the limiter holes: it shrinks the whole first layer, not just the edge |
| 13 | Brim on tall or long-narrow parts | notes | per-object | `"brim_type": "outer_only"`, `"brim_width": "5"`, `"brim_object_gap": "0.1"` (preset width and gap). Or `"brim_type": "auto_brim"` | Blades on end, 216 mm rails, 112 mm jaws, carrier, bracket | Notes say "outer only, auto": these are two different enum values. See section 4 |
| 14 | No brim on flat coupons, small parts, big flat plates | notes | per-object | `"brim_type": "no_brim"` | Coupons, shims, the 210 x 96 x 3 tag bridge | The class default is `"auto_brim"`, which can add a brim to a small footprint. Write `"no_brim"` explicitly |
| 15 | Dog ears (brim ears) | notes | per-object + user-in-GUI | `"brim_type": "brim_ears"` (GUI label "Painted") plus ear points from the "Brim Ears" gizmo ("Auto-generate points", "Max angle", "Detection radius", "Head diameter") | Big flat parts where only corners lift | Without points the type does nothing (GUI warns). Points live in `Metadata/brim_ear_points.txt`: header `brim_points_format_version=1`, then `object_id=<1-based>|x y z head_radius volume_idx ...` (F:3110-3183, F:7946-7960). A library can write corner points, but the format is untested |
| 16 | Tree supports for almost everything, default settings | notes | per-object | `"enable_support": "1"`, `"support_type": "tree(auto)"` (preset), `"support_style": "default"` | Upright carrier and bracket | Set per object, not per plate (notes). Preset has `enable_support` `"0"` |
| 17 | Normal supports, snug, straight up from the plate | notes | per-object | `"enable_support": "1"`, `"support_type": "normal(auto)"`, `"support_style": "snug"`, `"support_on_build_plate_only": "1"` | A fully flat overhang with clear plate below it | `support_on_build_plate_only` drops all support that would stand on the part |
| 18 | Support threshold angle | notes | per-object | `"support_threshold_angle": "30"` (preset). Overhangs flatter than this angle from horizontal get support | Keep 30. 45 deg overhangs are safe (notes) | Raising it adds support on 45-60 deg faces that print fine |
| 19 | Supports only in a few places (paint) | notes (jaw finger block), notes | per-object + modifier or user-in-GUI | `"enable_support": "1"`, `"support_type": "tree(manual)"` or `"normal(manual)"`, plus `support_enforcer` volumes or painted triangles (`paint_supports`) | Jaw finger: one 10 mm block | Manual types make only enforced support. Enforcer volumes are a 3MF write (report 03: whole-triangle painting is feasible) |
| 20 | Interface layers and top Z distance (single material) | not in notes | per-object | `"support_interface_top_layers": "2"`, `"support_interface_bottom_layers": "2"`, `"support_top_z_distance": "0.2"`, `"support_bottom_z_distance": "0.2"` (all preset) | Default | Datum faces on support are rough. Plan to file them (notes) |
| 21 | Different-material support interface | notes | per-object (support keys) + plate (filament map) + process (prime tower) | `"enable_support": "1"`, `"support_type": "tree(auto)"`, `"support_filament": "0"` (Default = part filament; or the part's slot, e.g. `"1"`), `"support_interface_filament": "2"`, `"support_top_z_distance": "0"`, `"support_interface_not_for_body": "1"`, `"enable_prime_tower": "1"`; plate `filament_map_mode` `"Manual"`, `filament_maps` `"1 2"` | PETG part with PLA interface, or PLA part with PETG interface | Needs one material per nozzle. Interface nozzle (right) cannot reach X < 20.5 mm (section 4): every supported part must sit at x >= 20.5 plus its interface spread (brim margin plus 3 mm; measured 2.2 mm past the box, n=1; the seed build exited -102 at x 14.3). The plate counts as two filaments even though only the supports use filament 2, so Studio builds a prime tower sized by the tallest part: with the rib wall, 41 mm square plus an 8 mm brim for a 218 mm part (35 mm plus 2.6 mm for a 32 mm part). Give the plate a `wipe_tower` spot both extruders reach (x >= 20.5), or let `"auto"` find one; if no spot fits, use `rib_wall` false and a 22 mm width (06-slicer-rules.md section 10). Measured on plate 14 (n=1): the tower adds 27 min and 71 g over no tower (19.25 h against 18.8 h). Re-slicing an unsliced export with `--slice 1` exits -100 "No valid nozzle found" unless RepairNozzleStats ran (`shambu build` applies it; `shambu patch --repair-nozzle-stats` for an older file; 03-3mf-format.md section 8). Right nozzle speeds are lower (outer wall 50 vs 200 mm/s). Bed follows `bed_temperature_formula` `"by_highest_temp"`: PETG 70 deg C applies to a PLA part too. The GUI suggests more (section 2, recipe 8) |
| 22 | Prime tower | notes | process (global) | `"enable_prime_tower": "1"` (preset), `"prime_tower_width": "35"` | Any plate with two filaments | Forces equal layering on multi-object plates. On a by-object plate with more than one object Studio drops the tower itself (reference 08) |
| 23 | Fuzzy skin, outer wall only, tiny | notes | per-object, modifier, or paint | `"fuzzy_skin": "external"`, `"fuzzy_skin_thickness": "0.15"`, `"fuzzy_skin_point_distance": "0.4"`, `"fuzzy_skin_first_layer": "0"` | Decorative parts to hide layer lines | 0.15 / 0.4 are my starting values. The notes only say "tiny". Preset is 0.3 / 0.8. Not for functional parts (notes): it grows the outside by up to the thickness. Thickness must stay below the outer wall width |
| 24 | X-Y hole compensation | notes | per-object | `"xy_hole_compensation": "0.02"` (notes range 0.01-0.03; preset `"0"`) | Only a part whose holes are all vertical and none sets a shared dimension | It offsets holes in each layer's XY slice, so it only reaches vertical holes. Horizontal bridged bores (0.2-0.3 mm under) get nothing. Do NOT use on gripper parts: notes chose CAD clearance + coupons because the error depends on orientation and bore length |
| 25 | X-Y contour compensation | notes | per-object | `"xy_contour_compensation": "0"` (keep) | Never on current data | Outsides measured on size (18.01-18.06 for 18.00). Nothing to correct |
| 26 | Auto circle compensation | not in notes | per-object + filament | `"enable_circle_compensation": "1"`, `"circle_compensation_manual_offset": "0"` | Not for PETG Basic | Tooltip (PC:6066): only PLA Basic, PLA CF, PET CF, PETG CF, PETG HF. The PETG Basic profile does carry `hole_coef_*`, but the feature is unsupported for it |
| 27 | Seam position | not in notes | per-object | `"seam_position": "aligned"` (preset); `"back"` for show faces; `"nearest"`, `"random"` | Cosmetic parts: `"back"` | Seam can also be painted (`paint_seam`) |
| 28 | Ironing top faces | not in notes | per-object or modifier | `"ironing_type": "top"` (or `"topmost"`, `"solid"`), `"ironing_flow": "10%"`, `"ironing_spacing": "0.15"`, `"ironing_speed": "30"` (preset) | Flat visible tops (tag bridge face) | Adds time. Not for functional parts |
| 29 | Two colours without an AMS: pause and swap | notes | plate (custom G-code) | `Metadata/custom_gcode_per_layer.xml` `<layer top_z="Z" type="1" .../>` (PausePrint), or CLI `--load-custom-gcodes {"gcodes":[{"type":"PausePrint","print_z":Z,...}]}` | Single-nozzle colour swap | `top_z` = print_z of the first layer in the new colour. Check the layer in the preview. Pause G-code is `machine_pause_gcode` `"M400 U1"` |
| 30 | Two colours, one per nozzle: first N layers | notes | height range + plate | height range `min_z "0"`, `max_z "0.6"`, option `"extruder": "2"` (first 3 layers at 0.2); or tool change `type="2"` at `top_z "0.8"`; plate `filament_maps "1 2"` | Coloured base or background-print text | 0.6 mm = 0.2 first layer + 2 x 0.2. Recompute if `layer_height` changes. A prime tower is generated |
| 31 | Text: print the background, face down | notes | CAD or user-in-GUI + height range | letters cut through layer 1; height range `0`-`0.2` = filament A, rest = filament B (same as row 30 with N = 1) | Small text | Emboss/deboss text is a GUI tool (notes). Colour on the side costs one swap per layer (notes) |
| 32 | Split to objects | notes | user-in-GUI | GUI toolbar "Split to objects" / "Split to parts" | Files with several bodies stuck together | CLI `split` is rejected (report 01). A library can split meshes before loading (connected components) |
| 33 | Cut tool with connectors | notes | user-in-GUI or CAD | GUI Cut gizmo with plug/dowel connectors; result recorded in `Metadata/cut_information.xml` | Parts larger than 256 x 256 x 261 | CLI `cut*` is rejected (report 01). Better done in CAD with dowel holes (notes) |
| 34 | 0.1 mm layers for shims, printed alone | notes | per-object or process, plate alone | `"layer_height": "0.1"`; keep `"initial_layer_print_height": "0.2"`; `"brim_type": "no_brim"` | 0.5 and 1.0 mm spring shims | 0.5 mm = 0.2 + 3 x 0.1. Printing alone avoids the prime tower equal-layer rule. No 0.10 preset ships: 0.08 and 0.12 exist |
| 35 | Textured PEI plate and bed temperature | notes | plate + filament | `"curr_bed_type": "Textured PEI Plate"` (project or plate `bed_type`); PETG `"textured_plate_temp": ["70"]`, PLA `["55"]`, TPU `["35"]` | All gripper PETG parts | Project default from CLI is `"Cool Plate"`. Always set it. No glue needed for PLA on textured PEI |
| 36 | Drying PETG / TPU | notes | user (no slicer key acts) | Profile hints only: PETG `filament_dev_ams_drying_temperature` 65/55 deg C, 12 h; chamber drying bed 80 deg C, 12 h. TPU 65/75/45/45 deg C, 12-18 h | Before every PETG or TPU print | These keys drive AMS drying. With no AMS, they are reference numbers |
| 37 | TPU only on the main (left) nozzle | notes | filament + plate | TPU 95A profile `filament_printable ["1"]` (left only); plate `filament_maps` puts the TPU slot on `"1"`, mode `"Manual"` | Any TPU part | The slicer already flags TPU on the right as an error (`filament_extruder_compatibility ["8"]`) |
| 38 | Speed vs quality | notes | process preset choice | Choose `0.08mm High Quality` / `0.12mm High Quality` / `0.16mm High Quality` / `0.20mm Standard` / `0.24mm Standard @BBL X2D` | Gaps and under-fill at normal speeds = clog, not settings | Do not tune speeds per recipe. Right (Bowden) nozzle preset speeds are already lower |
| 39 | First layer | notes | process (global) + user | `"initial_layer_print_height": "0.2"`, `"initial_layer_line_width": "0.5"`, `"initial_layer_speed": "50"` (preset) | Keep | Adhesion is a plate step: wash with dish soap, hold by the edges, room at 20-25 deg C, glue as release layer for PETG on smooth PEI |
| 40 | Timelapse | not in notes | process (global) + send dialog | `"timelapse_type": "0"` (Traditional) or `"1"` (Smooth) | User's choice at send time | Smooth needs a prime tower and is not allowed with `"by object"`. Recording on/off is in the GUI send dialog |
| 41 | Print at 45 deg / load in the layer plane | notes | CAD not slicer (orientation) | none. Export STLs on their bed face (`*_print.stl`) | Every part | Do not use CLI `--orient 1`: it overwrites object configs (report 01) |
| 42 | Advanced mode on | notes | user-in-GUI | GUI toggle only | Always | Downloaded projects can carry advanced-only settings |
| 43 | Hotter is shinier (PLA 250) | notes | filament | `"nozzle_temperature": ["250"]` | Never on gripper parts (notes) | PLA range high is 240 (`fdm_filament_common`). 250 triggers a range warning |
| 44 | Use the right filament profile | notes | filament | pick `Bambu PLA Basic @BBL X2D 0.4 nozzle` / `Bambu PETG Basic @BBL X2D 0.4 nozzle` / `Bambu TPU 95A @BBL X2D 0.4 nozzle` | Always | Often the only setting to change |

## 2. Named recipes (paste into a Python dict)

Fields beyond `name`, `description`, `process_set`, `object_set`, `filament_hint`, `when`: `plate_set` (project or plate
metadata), `height_ranges`, `custom_gcode`, `filament_map`, `requires` (things the library cannot do alone).
`process_set` holds global and process keys. `object_set` holds per-object keys and is applied to the target objects.

```python
RECIPES = {
  "petg-functional": {
    "name": "petg-functional",
    "description": "gripper strength settings: PETG, 0.2 mm layers, 6 walls, 5 top and bottom shells, 40% gyroid, textured PEI.",
    "process_set": {
      "layer_height": "0.2",
      "wall_loops": "6",
      "top_shell_layers": "5",
      "bottom_shell_layers": "5",
      "sparse_infill_density": "40%",
      "sparse_infill_pattern": "gyroid",
      "xy_hole_compensation": "0",
      "xy_contour_compensation": "0",
      "curr_bed_type": "Textured PEI Plate"
    },
    "object_set": {"__solid_parts__": {"sparse_infill_density": "100%"}},
    "filament_hint": "Bambu PETG Basic @BBL X2D 0.4 nozzle (dry first)",
    "when": "Functional or loaded PETG parts. Put 100% only on parts the stress maths sized as solid (pinion, racks, rails)."
  },
  "pla-decorative-5pct": {
    "name": "pla-decorative-5pct",
    "description": "Light decorative PLA: 5% infill, 2 walls, 5 top shells.",
    "process_set": {
      "sparse_infill_density": "5%",
      "sparse_infill_pattern": "gyroid",
      "wall_loops": "2",
      "top_shell_layers": "5",
      "curr_bed_type": "Textured PEI Plate"
    },
    "filament_hint": "Bambu PLA Basic or PLA Matte @BBL X2D 0.4 nozzle (matte hides layer lines)",
    "when": "Display parts with no load. For curved tops, add a variable layer profile in the GUI."
  },
  "tall-narrow-brim": {
    "name": "tall-narrow-brim",
    "description": "Outer brim on tall or long and narrow parts.",
    "process_set": {},
    "object_set": {"brim_type": "outer_only", "brim_width": "5", "brim_object_gap": "0.1"},
    "when": "Parts standing on end, long rails, jaws, the upright carrier and bracket."
  },
  "flat-no-brim": {
    "name": "flat-no-brim",
    "description": "Explicit no brim, because the default brim_type is auto_brim.",
    "process_set": {},
    "object_set": {"brim_type": "no_brim"},
    "when": "Flat coupons, small parts, shims, large flat plates. Switch to brim ears only if a corner lifts."
  },
  "tree-supports": {
    "name": "tree-supports",
    "description": "Automatic tree supports with default settings, per object.",
    "process_set": {},
    "object_set": {
      "enable_support": "1",
      "support_type": "tree(auto)",
      "support_style": "default",
      "support_threshold_angle": "30"
    },
    "when": "Most parts that need support. Apply only to the objects that need it."
  },
  "snug-normal-supports": {
    "name": "snug-normal-supports",
    "description": "Normal supports, snug style, standing on the plate only.",
    "process_set": {},
    "object_set": {
      "enable_support": "1",
      "support_type": "normal(auto)",
      "support_style": "snug",
      "support_on_build_plate_only": "1"
    },
    "when": "A fully flat overhang with clear plate straight below it."
  },
  "dual-material-interface": {
    "name": "dual-material-interface",
    "description": "Tree supports in the part material, interface in the other material (PLA on PETG or PETG on PLA), top Z 0.",
    "process_set": {
      "enable_prime_tower": "1",
      "independent_support_layer_height": "0"
    },
    "object_set": {
      "enable_support": "1",
      "support_type": "tree(auto)",
      "support_filament": "0",
      "support_interface_filament": "2",
      "support_interface_not_for_body": "1",
      "support_top_z_distance": "0",
      "support_interface_spacing": "0",
      "support_interface_pattern": "rectilinear_interlaced"
    },
    "filament_map": {"mode": "Manual", "maps": "1 2"},
    "filament_hint": "slot 1 = part material on the left nozzle, slot 2 = the other material on the right nozzle",
    "requires": ["supported features at X >= 20.5 mm (right nozzle area)", "check the bed temperature: by_highest_temp"],
    "when": "Supported faces that must come off clean. Expect a prime tower and a slower print."
  },
  "fuzzy-skin-external": {
    "name": "fuzzy-skin-external",
    "description": "Fine fuzzy skin on the outer wall only to hide layer lines.",
    "process_set": {},
    "object_set": {
      "fuzzy_skin": "external",
      "fuzzy_skin_thickness": "0.15",
      "fuzzy_skin_point_distance": "0.4",
      "fuzzy_skin_first_layer": "0"
    },
    "when": "Decorative parts only. Never on mating or functional faces."
  },
  "shim-0.1mm": {
    "name": "shim-0.1mm",
    "description": "Thin shims at 0.1 mm layers, alone on the plate, no brim.",
    "process_set": {"layer_height": "0.1", "initial_layer_print_height": "0.2"},
    "object_set": {"brim_type": "no_brim"},
    "requires": ["alone on its plate (prime tower needs equal layer heights)"],
    "when": "0.5 and 1.0 mm spring shims and other parts thinner than about 1.5 mm."
  },
  "two-colour-first-layers": {
    "name": "two-colour-first-layers",
    "description": "First 3 layers (0.6 mm) in filament 2 on the right nozzle, rest in filament 1.",
    "process_set": {"enable_prime_tower": "1"},
    "height_ranges": [{"min_z": "0", "max_z": "0.6", "set": {"extruder": "2"}}],
    "filament_map": {"mode": "Manual", "maps": "1 2"},
    "alt_single_nozzle": {"custom_gcode": [{"type": "PausePrint", "print_z": "0.8"}]},
    "when": "A coloured base, or background-print text with letters cut through the first layers. Recompute max_z if layer_height changes."
  }
}
```

Notes on the recipes:
- `"__solid_parts__"` is a placeholder for the library's object selector.
- In `dual-material-interface`, the last three object keys and `independent_support_layer_height` come from the GUI's own
  suggestion text, found as strings in the binary ("0 top z distance, 0 interface spacing, 0 support/object xy distance,
  interlaced rectilinear pattern, disable independent support layer height ..."). That suggestion is for soluble and
  PLA-under-TPU supports. It also sets `support_object_xy_distance` to 0 and puts the interface material in the base too.
  The notes keep the base in the part material, so the recipe keeps `support_object_xy_distance` at the preset 0.35 and
  `support_filament` at `"0"`. Treat these three extra keys as unverified for PLA/PETG.
- `fuzzy-skin-external` values 0.15 / 0.4 are starting values, not from the notes.

## 3. Facts for the skill text, not for code

CAD rules (general, from the notes)
- Holes print undersize, and the error depends on orientation: vertical holes lose about 0.1 mm, bridged horizontal
  bores lose more, and long bridged bores lose the most. Put clearance in the CAD, qualify it with a coupon in the
  same orientation, and never fix a shared dimension by drilling the print.
- Heat-set insert holes need their own coupon per insert size and material; the printed hole, not the datasheet, is
  the number to trust.
- A 0.4 x 0.6 mm relief chamfer on every bed-side mating edge, a lead-in chamfer at every socket mouth, and a floor of
  at least 0.8 mm under any relief.
- Fillets at every section change; a thin neck is where printed parts break first.
- Bottom edges: chamfer, never a fillet. Teardrop horizontal holes. Sacrificial bridge layers over nut pockets.
  Overhangs 45 to 60 degrees at most. Sliding fits start at a 0.2 mm gap.
- Orientation marks on mirror-image parts. Export every STL already lying on its bed face.
- Do not copy injection-moulding ribs and pockets; solid blocky parts print stronger and faster.

Coupons and order
- Print a coupon before the part, in the same orientation as the feature, with pips to read each size.
- Order plates by dependency: coupons, then the parts their numbers set, then the large overnight parts.
- Test only the changed section of a large part.

Materials and drying
- Everything except PLA must be dried before printing. PETG survives outdoors and heat (about 70 C) and is UV stable.
- Abrasive filled filaments need hardened nozzles.

Machine and files
- External spool on the X2D: the upper buffer inlet feeds the left (main) hotend, the lower inlet the right one.
- USB export: File, Export, Export Plate Sliced File gives `.gcode.3mf`; a known bug doubles the extension. Cloud
  send needs the same account in Bambu Studio and on the printer.
- Check footprints, not part counts: two medium parts can be wider than the bed together.

Time and logging
- The slicer estimate wins over any planning rate. Weigh every part and log it.
- Colour on top costs one swap; colour on a side swaps every layer and costs far more.

Troubleshooting
- Failure at the start is adhesion. Failure at one height is a missing support. Failure at a random height is a part
  that shook loose or a corner that lifted.
- Gaps at normal speeds mean a clog: replace the nozzle or do a cold pull. A Benchy separates printer from settings.

## 4. Contradictions between the notes and the presets or machine profile

1. **Auxiliary nozzle reach is on the other side.** notes says the auxiliary nozzle "only reaches 235.5 mm in X", and notes
   says "keep supported features inside 235.5 mm in X". The machine profile gives
   `extruder_printable_area` = `["0x0,256x0,256x256,0x256", "20.5x0,256x0,256x256,20.5x256"]`. Index 1 is the right
   (auxiliary) nozzle, by `filament_printable` bit order and `extruder_type ["Direct Drive", "Bowden"]`. The width
   matches (256 - 20.5 = 235.5 mm), but the dead strip is X 0 to 20.5 mm on the left edge. "Inside 235.5 mm in X" read
   as X <= 235.5 is the wrong side. The rule for a library: anything the right nozzle prints must sit at X >= 20.5 mm.
   Also `extruder_printable_height` is `["261", "256"]`: the right nozzle stops at Z 256 mm.
2. **"Brim (outer only, auto)" (notes) names two enum values.** `brim_type` takes one of `auto_brim`, `brim_ears`,
   `outer_only`, `inner_only`, `outer_and_inner`, `no_brim`. "Auto" is `auto_brim`, which picks the width itself.
   `outer_only` uses `brim_width` (preset 5 mm). The recipe uses `outer_only` with 5 mm. Confirm which one the user meant.
3. **The brim default is not "none".** The notes treat "no brim" as the normal state. The class default is
   `"auto_brim"`, which can add a brim to small footprints. Flat parts need an explicit `"no_brim"`.
4. **Dog ears are not automatic.** notes describes dog ears as a brim type. In 02.08 `brim_ears` is labelled "Painted"
   and needs points placed in the Brim Ears gizmo (or "Auto-generate points"). Without points, no ears print.
5. **100 % "rectilinear" (notes).** The sparse pattern string for rectilinear is `"zig-zag"`. At 100 % the sparse
   pattern is not used at all: the slicer prints `internal_solid_infill_pattern` (default `"zig-zag"`). The result
   matches the notes, but a library that writes `"rectilinear"` to `sparse_infill_pattern` writes an invalid value.
6. **Preset vs gripper strength.** Preset: `wall_loops` 2, `bottom_shell_layers` 3, 15 % `grid`. gripper build: 6 walls, 5 bottom,
   40 % gyroid. `top_shell_layers` 5 already matches.
7. **Elephant foot is already compensated.** The preset has `elefant_foot_compensation` 0.15 (class default 0). The
   notes' 0.4 x 0.6 CAD relief stacks on top of it. Not wrong, but the measured first-layer data in the notes already
   includes the 0.15 mm.
8. **Hole compensation range vs reach.** notes suggests `xy_hole_compensation` 0.01-0.03 mm as the "better option". notes
   rejects it. The key only acts on holes in each layer's XY slice, so it cannot fix the 0.2-0.3 mm error on horizontal
   bores at all, and 0.01-0.03 mm is far below the measured 0.1 mm on vertical holes. The data supports notes.
9. **Auto circle compensation does not cover PETG Basic.** The tooltip limits it to PLA Basic, PLA CF, PET CF, PETG CF
   and PETG HF, so it is not an option for the gripper PETG parts.
10. **PLA at 250 deg C (notes).** The PLA profile prints at 220 deg C with a range high of 240 deg C. 250 is outside the
    profile range and triggers a warning.
11. **Mixed-material bed temperature.** notes does not mention the bed. `bed_temperature_formula` is
    `"by_highest_temp"`, so a PLA part with a PETG interface prints on a 70 deg C bed (PLA profile: 55 deg C).
12. **Support base material.** notes keeps the support base on the part material. The GUI's own suggestion for
    material-interface supports (soluble, PLA-under-TPU) is to use the interface material for the base too, with 0 xy
    distance. The notes' choice is consistent with `support_interface_not_for_body` = 1 (default), but differs from the
    GUI prompt the user will see.
13. **Default filament in the resolved set is PETG.** The machine's `default_filament_profile` is `Bambu PLA Basic`. The
    resolved JSON uses `Bambu PETG Basic`. Not a conflict with the notes (the gripper is PETG), but a library must not assume
    PLA temperatures from this file.
14. **Layer height for shims.** notes says 0.1 mm layers. No 0.10 mm X2D process ships (0.08 and 0.12 do). Setting
    `layer_height` 0.1 on the 0.20 Standard preset is valid (`min_layer_height` 0.08).
