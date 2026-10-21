# Bambu Studio .3mf project format (02.08.02.61, X2D)

> **Provenance and licence notice.** Bambu Studio is free software under the AGPL-3.0
> (https://github.com/bambulab/BambuStudio). The findings in this document come from reading that source at tag
> `v02.08.02.61`, inspecting the shipped macOS binary (symbols, strings, crash reports, a debugger session on a local
> re-signed copy) and running experiments with its command line. Short excerpts are quoted for interoperability only.
> No Bambu Studio code is included in shambu, and shambu is not affiliated with or endorsed by Bambu Lab.
> See `DISCLAIMER.md` at the repository root.

Scope: what a stdlib Python library can read, patch and generate inside a Bambu Studio project
so that an agent can set what the CLI cannot, or skip the CLI. Evidence comes from 8 real files,
21 CLI runs, and `src/libslic3r/Format/bbs_3mf.cpp` from BambuStudio master (commit
`47a13eeb`, 2026-10-08; the installed app is 02.08.02.61, so line numbers are approximate for
that build). Line refs: **F** = `bbs_3mf.cpp`, **CLI** = `src/BambuStudio.cpp`, **PC** =
`libslic3r/PrintConfig.cpp`.

Work dir: `$S = <scratch>/3mf-re`.
Prototype: `$S/proto/bambu3mf.py` (read + patch), `$S/proto/gen.py` (generate from STL),
`$S/proto/test_roundtrip.py` (patch test). No file outside `$S` was changed.

## Contents

| Section | What it holds |
|---|---|
| [0. Headline findings](#0-headline-findings) | The main findings about the project files that were examined |
| [1. File inventory](#1-file-inventory) | The input files, their source and the program that made them |
| [2. Schemas](#2-schemas) | The XML and config schema of each file in the project zip |
| [3. CLI fit test vs its assemble list](#3-cli-fit-test-vs-its-assemble-list) | How a CLI assemble list maps to the saved project files |
| [4. What the GUI stores that the CLI does not](#4-what-the-gui-stores-that-the-cli-does-not) | Exact forms of GUI-only data: per-part settings, painting, brim ears |
| [5. Prototype and patch survival](#5-prototype-and-patch-survival) | The read-and-patch prototype, and which patches survive a Studio reload |
| [6. Generating a project from STLs with no CLI](#6-generating-a-project-from-stls-with-no-cli) | Writing a project from STLs without the CLI |
| [7. Gaps and next steps](#7-gaps-and-next-steps) | Open questions and the tests that would close them |
| [Appendix: `project_settings.config` keys by source](#appendix-project_settingsconfig-keys-by-source) | Each `project_settings.config` key, grouped by preset source |

## 0. Headline findings

1. **All 7 "GUI-saved" inputs are CLI exports.** Each has a CLI log with the same timestamp as
   the 3MF (`plate-10 .../bambu/export.log` 02:31:19 = 3MF mtime 02:31:19), an empty
   `<assemble>`, no G-code and no paint data. The user opened them in the GUI (they are in
   `BambuStudio.conf` recent files) but did not save. So no real GUI-saved sample exists. The
   GUI-only data in section 4 comes from the writer code, not from a sample. To close this gap,
   save one plate from the GUI after painting supports and slicing it.
2. **The fit test is not patched to Textured PEI.** `fit-test.3mf` holds
   `"curr_bed_type": "Cool Plate"` (as do A, B, C). Its `project_settings.config` ends with a
   newline, which the CLI's writer does not add, so a script did rewrite it at some point.
3. **The CLI export is fast; only slicing is slow or broken.** `--export-3mf` on a 3MF takes
   about 0.06 s. Headless `--slice 1` on this Mac exits with no G-code: rc=0 with no output for
   the fit test, and "No valid nozzle found. Please check nozzle count." for plate 10. So the
   library cannot get slice verification from the CLI on this machine.
4. **Every patch the library makes survives a CLI load and re-export** (table in section 5).
5. **`layer_config_ranges.xml` keys objects by build order, not by id** (section 2.6). This is
   the main trap for a hand-written patch.
6. **A filament mapped to nozzle 2 also needs `filament_extruder_variant` updated.** Without it,
   slicing logs `could not found extruder_type Bowden, nozzle_volume_type Standard,
   filament_index 2` and segfaults (rc=139). The prototype now writes it.
7. **Generating a project from STLs with no CLI works,** if `project_settings.config` is a
   complete config from a template. With only the five keys the loader needs, the CLI loads the
   file but silently fills the factory defaults (wall_loops 2, nozzle 200 C, one extruder) under
   the original preset names. Verdict: **partially feasible** (section 6).

## 1. File inventory

Inputs (copies in `$S/in`):

| Copy | Source | Producer |
|---|---|---|
| `cli-fit.3mf` | `<project>/plates/fit-test.3mf` | CLI `--load-assemble-list` |
| `cli-A/B/C.3mf` | `<project>/plates/plate-{A,B,C}.3mf` | CLI, 4/3/5 plates |
| `gui-p10.3mf` | `.../<project>/plates/plate-10-root-coupons/plate-10-root-coupons-D6.3mf` | CLI (see 0.1) |
| `gui-p8.3mf` | `.../plate-8-carrier/plate-8-carrier-D5.3mf` | CLI |
| `gui-p8-plus.3mf` | `.../plate-8-carrier/bambu/stale/plate-8-carrier-D5-plus-root-coupon.3mf` | CLI, with `print_params` |
| `other-thirdparty.3mf` | a third-party export (GLB source) | third-party writer that claims `BambuStudio-02.03.01.00`, `3mfVersion 2`, source `model.glb` |

No `.3mf` exists in `~/Downloads`.

Entry inventory (N = plate or object number). Zip: CLI files use miniz, flag bits 0x808
(UTF-8 + data descriptor), PNGs stored, everything else deflated.

| Entry | Format | Compression | Size (bytes) | Present in |
|---|---|---|---|---|
| `[Content_Types].xml` | XML | deflate | 323-432 | all |
| `Metadata/plate_N.png` | PNG | stored | 3989-18801 | all 7 Bambu files |
| `Metadata/plate_N_small.png` | PNG | stored | 1957-5413 | all 7 |
| `Metadata/plate_no_light_N.png` | PNG | stored | 2748-9191 | all 7 |
| `Metadata/top_N.png`, `pick_N.png` | PNG | stored | 1429-10222 | all 7 |
| `3D/3dmodel.model` | XML | deflate | 1113-22569 | all |
| `3D/_rels/3dmodel.model.rels` | XML | deflate | 272-5884 | all |
| `3D/Objects/object_N.model` | XML | deflate | 30926-40296787 | all |
| `Metadata/layer_config_ranges.xml` | XML | deflate | 187-2759 | plates A/B/C/fit only |
| `Metadata/project_settings.config` | JSON | deflate | 1491-43492 | all |
| `Metadata/model_settings.config` | XML | deflate | 1175-62747 | all |
| `Metadata/cut_information.xml` | XML | deflate | 139-3533 | all 7 |
| `Metadata/slice_info.config` | XML | deflate | 205 | all 7 (header only) |
| `Metadata/filament_sequence.json` | JSON | deflate | 72-356 | all 7 |
| `_rels/.rels` | XML | deflate | 263-673 | all |

Absent from every input, but written by Bambu when the condition applies (F writer):
`Metadata/plate_N.gcode` + `plate_N.gcode.md5` (sliced), `Metadata/plate_N.json` (sliced; "pattern
bbox"), `Metadata/custom_gcode_per_layer.xml` (pauses or colour changes exist),
`Metadata/brim_ear_points.txt` (brim ears), `Auxiliaries/*`, and embedded
presets (`_add_project_embedded_presets_to_archive`, user presets).

**CLI vs "GUI" set difference: none.** All 7 Bambu files have the same entry set except
`layer_config_ranges.xml`, which exists only when a height range exists. The third-party file
has only `[Content_Types].xml`, rels, the model files and both settings files (36 keys).

Entry order in a CLI export: `[Content_Types].xml`, PNGs, `3D/3dmodel.model`, model rels,
object meshes, `layer_config_ranges.xml`, `project_settings.config`, `model_settings.config`,
`cut_information.xml`, `slice_info.config`, `filament_sequence.json`, `_rels/.rels`.
**Order matters once:** the loader applies `slice_info.config` and `filament_sequence.json` only
to plates that `model_settings.config` already created (F:3497, F:4810). Keep
`model_settings.config` first of the three. `bambu3mf.save()` checks this.

## 2. Schemas

### 2.1 `[Content_Types].xml`, `_rels/.rels`, `3D/_rels/3dmodel.model.rels`

```xml
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="png" ContentType="image/png"/>
 <Default Extension="gcode" ContentType="text/x.gcode"/>
</Types>
```
```xml
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
 <Relationship Target="/Metadata/plate_1.png" Id="rel-2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"/>
 <Relationship Target="/Metadata/plate_1.png" Id="rel-4" Type="http://schemas.bambulab.com/package/2021/cover-thumbnail-middle"/>
<Relationship Target="/Metadata/plate_1_small.png" Id="rel-5" Type="http://schemas.bambulab.com/package/2021/cover-thumbnail-small"/>
</Relationships>
```
`3dmodel.model.rels` lists one `Relationship` per `3D/Objects/object_N.model`.

Loader rules (F:1916-1930, F:5322, F:5383):
- `_rels/.rels` is **required**. The first relationship whose Type ends in `3dmodel` is the start part.
- `3D/_rels/3dmodel.model.rels` is optional, but without it no sub-model loads, and every
  `<component p:path>` then fails with "sub_objects can not be found".
- `[Content_Types].xml` is never read (writer only, F:7075).

### 2.2 `3D/3dmodel.model` (root model)

```xml
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
  xmlns:BambuStudio="http://schemas.bambulab.com/package/2021"
  xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">
 <metadata name="Application">BambuStudio-02.08.02.61</metadata>
 <metadata name="BambuStudio:3mfVersion">1</metadata>
 <metadata name="Copyright"></metadata> ... CreationDate, Description, Designer, DesignerCover,
   DesignerUserId, License, ModificationDate, Origin, ProfileCover, ProfileDescription, ProfileTitle, Title
 <resources>
  <object id="7" p:UUID="00000003-61cb-4c03-9d28-80fed5dfa1dc" type="model">
   <components>
    <component p:path="/3D/Objects/object_3.model" objectid="5" p:UUID="00030000-b206-40ff-9872-83e8017abed1" transform="1 0 0 0 1 0 0 0 1 132 7.5 0.600000024"/>
    <component p:path="/3D/Objects/object_3.model" objectid="6" p:UUID="00030001-b206-40ff-9872-83e8017abed1" transform="1 0 0 0 1 0 0 0 1 132 7.54381108 0.900000036"/>
   </components>
  </object>
 </resources>
 <build p:UUID="2c7c17d8-22b5-4d84-8835-1976022ea369">
  <item objectid="7" p:UUID="00000007-b1ec-4553-aec9-835e5b724bb4" transform="1 0 0 0 1 0 0 0 1 0 0 0" printable="1"/>
 </build>
</model>
```
- **The `Application` metadata is the project switch.** It must start with `BambuStudio-`
  (F:4315-4331). Without it the loader sets `dont_load_config` and **skips**
  `project_settings.config`, `custom_gcode_per_layer.xml`, `slice_info.config`,
  `filament_sequence.json`, the PNGs and the G-code (F:2039-2050, F:2101-2175).
- `BambuStudio:3mfVersion` is written as 1 and not checked (the check is commented out, F:4319).
- After a CLI re-export, the root also gets `Thumbnail_Middle` / `Thumbnail_Small` metadata.
- Transform: 12 numbers, column-major 3x4 (r00 r10 r20 r01 r11 r21 r02 r12 r22 tx ty tz).
- Ids: each part gets a mesh object id in the sub-file (1, 3, 5, 6) and each printable object
  gets a root id (2, 4, 7). Root objects hold only `<components>`.
- `p:UUID` patterns are deterministic counters (`0000000N-61cb-...`, `000N0000-b206-...`,
  `0000000N-b1ec-...`). The loader does not need them to be unique across files.

### 2.3 `3D/Objects/object_N.model` (meshes)

One file per root object. It holds one `<object>` per part, with the mesh **re-centred on its
bounding-box centre** (`add_volume(mesh, MODEL_PART, true)`, Model.cpp:3461). The build is empty.
```xml
<model ...same namespaces...>
 <metadata name="BambuStudio:3mfVersion">1</metadata>
 <resources>
  <object id="5" p:UUID="00030000-81cb-4c03-9d28-80fed5dfa1dc" type="model">
   <mesh><vertices><vertex x="-17.7777843" y="-5.41573477" z="-0.600000024"/>...</vertices>
         <triangles><triangle v1="0" v2="1" v3="2"/>...</triangles></mesh>
  </object>
  <object id="6" ...>...</object>
 </resources>
 <build/>
</model>
```
**Paint attributes on `<triangle>`** (F:316-321, read F:4174-4179): `paint_supports`,
`paint_seam`, `paint_color` (multi-material painting, the old `mmu_segmentation`), and
`paint_fuzzy_skin`; also `face_property`. None of the inputs holds any. Encoding: an uppercase
hex string of the `TriangleSelector` bitstream for that triangle. The decoder reads the string
from the **last** character to the first and takes each nibble least-significant bit first
(Model.cpp:4652-4664). A non-hex character drops the triangle's paint. From memory (not checked,
`TriangleSelector.cpp` was not in the checkout): 2 bits give the split count; a leaf state is
2 bits, or `11` + 4 bits for state >= 3. Under that layout, a whole triangle painted
"enforcer"/filament 1 is `4`, "blocker"/filament 2 is `8`, filament 3 is `0C`. Supports use
state 1 = enforcer, 2 = blocker. The root model also gets `BambuStudio:FdmSupportsPaintingVersion`,
`SeamPaintingVersion`, `MmPaintingVersion` = 1 when paint exists (seen in the third-party file).
Painting is feasible for whole triangles (for example, enforce supports on every triangle facing
down within a z band) without the sub-triangle tree.

### 2.4 `Metadata/model_settings.config` (XML)

Real example (fit test, abridged):
```xml
<config>
  <object id="7">                                   <!-- = root object id in 3dmodel.model -->
    <metadata key="name" value="assemble_1"/>
    <metadata face_count="3976"/>
    <part id="5" subtype="normal_part" uuid="81d97e24-a4e0-4acd-a9aa-45f427c676e6">
      <metadata key="name" value="a01_1"/>
      <metadata key="matrix" value="1 0 0 132 0 1 0 7.5 0 0 1 0.60000002384185791 0 0 0 1"/>
      <metadata key="source_file" value="a01.stl"/>
      <metadata key="source_object_id" value="2"/>
      <metadata key="source_volume_id" value="0"/>
      <metadata key="source_offset_x" value="0"/>
      <metadata key="source_offset_y" value="0"/>
      <metadata key="source_offset_z" value="0.60000002384185791"/>
      <metadata key="extruder" value="1"/>
      <mesh_stat face_count="2224" edges_fixed="0" degenerate_facets="0" facets_removed="0" facets_reversed="0" backwards_edges="0"/>
    </part>
    <part id="6" ...> ... <metadata key="extruder" value="2"/> ... </part>
  </object>
  <plate>
    <metadata key="plater_id" value="1"/>
    <metadata key="plater_name" value="fit test"/>
    <metadata key="locked" value="false"/>
    <metadata key="filament_map_mode" value="Auto For Flush"/>
    <metadata key="gcode_file" value=""/>
    <metadata key="thumbnail_file" value="Metadata/plate_1.png"/>
    <metadata key="thumbnail_no_light_file" value="Metadata/plate_no_light_1.png"/>
    <metadata key="top_file" value="Metadata/top_1.png"/>
    <metadata key="pick_file" value="Metadata/pick_1.png"/>
    <model_instance>
      <metadata key="object_id" value="7"/>
      <metadata key="instance_id" value="0"/>
      <metadata key="identify_id" value="72"/>
    </model_instance>
  </plate>
  <assemble>
  </assemble>
</config>
```

| Element / key | Meaning (reader ref) |
|---|---|
| `object@id` | root object id in `3dmodel.model` (F:2268) |
| object `name`, `module` | name; anything else → `ModelObject::config.set_deserialize(key, value)` = **per-object print setting** (F:2272-2280) |
| object `extruder` | filament slot (1-based) for the object |
| `part@id`, `@subtype` (`normal_part`, `negative_volume`, `modifier_part`, `support_enforcer`, `support_blocker`), `@uuid` | volume; legacy `volume firstid/lastid` also read |
| part `name`, `volume_type`/`part_type`, `source_file`, `source_object_id`, `source_volume_id`, `source_offset_x/y/z`, `source_in_inches`, `source_in_meters`, `assembly_src_guid`, `matrix`, `mesh_shared` | handled keys (F:5560-5590); `matrix` only sets `source.transform` and `source_offset_*` only `source.mesh_offset` (for reload-from-disk). **Neither places the part.** |
| part `extruder` and any other key | → volume config = **per-part setting**. A part `extruder` is dropped when the object has one volume (F:2458). |
| `mesh_stat` | repair stats, informational |
| `text_info`, `BambuStudioShape` | text / emboss parameters (GUI) |
| `plate` keys `plater_id`, `plater_name`, `locked` | plate identity |
| plate `bed_type` | **per-plate `curr_bed_type`** (F:4727). Values: `Default Plate` (= follow project), `Cool Plate`, `Engineering Plate`, `High Temp Plate`, `Textured PEI Plate`, `Supertack Plate` |
| plate `print_sequence`, `first_layer_print_sequence`, `other_layers_print_sequence`, `other_layers_print_sequence_nums`, `spiral_mode` | per-plate overrides |
| plate `filament_map_mode` | per-plate; `Auto` (legacy) is ignored |
| plate `filament_maps` | space-separated 1-based nozzle per filament, e.g. `"1 2"`; values < 1 become 1 (F:4768) |
| plate `filament_volume_maps`, `enable_filament_dynamic_map`, `has_filament_switcher`, `limit_filament_maps` | dual-nozzle extras |
| plate `gcode_file`, `prediction`, `weight`, `outside`, `support_used`, `label_object_enabled`, `support_material_on_wipe_tower`, `default_ams_type`, `printer_model_id`, `nozzle_volume_type`, `nozzle_diameters` | slice results |
| plate `thumbnail_file`, `thumbnail_no_light_file`, `top_file`, `pick_file`, `pattern_bbox_file` | asset paths |
| `model_instance` `object_id`, `instance_id`, `identify_id` | which object instance sits on the plate; `identify_id` is the label id used for skip-object |
| `assemble` / `assemble_item` (`object_id`, `instance_id`, `transform`, `offset`, `volume_id`) | assembly view; empty in all inputs |

Per-object settings written by the CLI from `print_params` (real, `gui-p8-plus` and plate A):
```xml
<object id="4">
  <metadata key="name" value="coupon_root_print_1"/>
  <metadata key="enable_support" value="0"/>
  <metadata key="extruder" value="1"/>
```
```xml
<object id="44">
  <metadata key="name" value="baseplate-6x6_1"/>
  <metadata key="brim_type" value="outer_only"/>
  <metadata key="brim_width" value="1.5"/>
```
The GUI writes exactly the same shape for its object-list overrides: same key names as the
process JSON, string values in preset form (`"25%"`, `"1"`, `"outer_only"`).

### 2.5 `Metadata/project_settings.config` (JSON)

One flat JSON object, 578 keys for the X2D (4-space indent, no trailing newline when the CLI
writes it). It is the **fully resolved** printer + process + filaments config. Vectors come in
fixed shapes:

| Vector length | Keys (count) | Indexed by |
|---|---|---|
| scalar | 308 | — |
| 1 | 30 (`filament_colour`, `filament_map`, `filament_nozzle_map`, `wipe_tower_x/y`, ...) | filament slot, but the CLI writes only 1 entry (bug, see below). `wipe_tower_x/y` are indexed by plate, not slot: pass one value per plate (0.2.1, reference 00) |
| 2 | 147 (`filament_settings_id`, `filament_type`, `nozzle_temperature`, `extruder_type`, `nozzle_diameter`, ...) | filament slot **or** extruder (both are 2 here) |
| 4 | `different_settings_to_system`, `inherits_group`, `printable_area` | [process, filament 1, filament 2, printer] |
| 6 | 65 (`outer_wall_speed`, `retraction_length`, `print_extruder_variant`, ...) | extruder variant (`print_extruder_variant` lists the 6) |
| 12 | `machine_max_*` | 2 extruders x 6 |
| 16 / 8 | `flush_volumes_matrix` / `flush_volumes_vector` | flush tables |

Keys that matter for this task (fit test values):

| Key | Value | Notes |
|---|---|---|
| `curr_bed_type` | `"Cool Plate"` | project bed; plate `bed_type` overrides it. Enum strings PC:454-461 |
| `printer_settings_id` | `"Bambu Lab X2D 0.4 nozzle"` | required by the CLI |
| `print_settings_id` | `"Gridfinity PLA 0.20 @BBL X2D"` | required by the CLI |
| `filament_settings_id` | `["Generic PLA @BBL X2D 0.4 nozzle", ×2]` | required; its length is the filament count |
| `filament_colour` | `["#00AE42"]` | **only 1 entry for 2 filaments** (CLI writes the default) |
| `filament_type`, `filament_ids`, `filament_vendor` | `["PLA","PLA"]`, `["GFL99",...]` | per filament |
| `filament_map` | `["1"]` | 1-based extruder per filament; also 1 entry only |
| `filament_map_mode` | `"Auto For Flush"` | `Auto For Flush`, `Auto For Match`, `Manual`, `Nozzle Manual`, `Auto For Quality` (PC:532-538) |
| `filament_nozzle_map`, `filament_volume_map`, `filament_map_2` | `["1"]` | dual-nozzle internals |
| `filament_extruder_variant` | `["Direct Drive Standard", "Direct Drive Standard"]` | **must match** `extruder_type[map-1] + " " + nozzle_volume_type[map-1]` for each filament |
| `extruder_type` | `["Direct Drive", "Bowden"]` | X2D: extruder 1 direct drive, extruder 2 Bowden |
| `nozzle_volume_type`, `default_nozzle_volume_type` | `["Standard","Standard"]` | `Standard`, `High Flow`, `TPU High Flow`, `Hybrid`, `E3D High Flow` |
| `nozzle_diameter` | `["0.4","0.4"]` | required; its length is the extruder count |
| `printer_extruder_variant`, `print_extruder_variant` | 6 variants: `Direct Drive Standard/High Flow/E3D High Flow`, `Bowden Standard/High Flow/E3D High Flow` | |
| `printer_extruder_id`, `print_extruder_id` | `["1","1","1","2","2","2"]` | variant → extruder |
| `extruder_variant_list` | 2 comma lists | |
| `physical_extruder_map` | `["1","0"]` | logical → physical nozzle |
| `master_extruder_id` | `"1"` | |
| `extruder_printable_area` | `["0x0,256x0,256x256,0x256", "20.5x0,256x0,256x256,20.5x256"]` | extruder 2 cannot reach x < 20.5 |
| `extruder_printable_height` | `["261","256"]` | |
| `inherits_group`, `different_settings_to_system` | 4 × `""` | user-preset bookkeeping |
| `from`, `name`, `version` | `"project"`, `"project_settings"`, `"02.08.02.61"` | |
| `machine_pause_gcode` | `"M400 U1"` | used for a pause entry |

The full key lists by source (process 188, machine 107, filament 128, defaults or derived 155),
classified against the fit test's own preset JSONs, are in the appendix.

### 2.6 `Metadata/layer_config_ranges.xml` (height ranges)

```xml
<?xml version="1.0" encoding="utf-8"?>
<objects>
 <object id="2">
  <range min_z="21" max_z="25.600000381469727">
   <option opt_key="extruder">2</option>
  </range>
 </object>
</objects>
```
**`object@id` is the 1-based position of the object in build order** (the order of first
appearance in `<build><item>`), not the `3dmodel.model` id. Writer F:8006-8034 uses
`object_cnt++` over `model.objects`. Reader F:2276: "m_layer_config_ranges are indexed by a 1
based model object index". In the fit test, the range for the bin (root id 4, 2nd build item)
sits under `id="2"`. In `cli-C`, model_settings lists objects in a different order from the
build, so a library must compute the index from `<build>`. Any config key can be an `option`
(`extruder`, `sparse_infill_density`, `wall_loops`, `layer_height`, ...).
`cut_information.xml` uses the same 1-based index.

### 2.7 `Metadata/custom_gcode_per_layer.xml` (pause, colour change, custom G-code)

Absent from every input. The prototype wrote this, and the CLI re-exported it unchanged in
meaning (CLI writer output, verbatim):
```xml
<?xml version="1.0" encoding="utf-8"?>
<custom_gcodes_per_layer>
<plate>
<plate_info id="1"/>
<layer top_z="4.2000000000000002" type="0" extruder="2" color="#FF0000" extra="" gcode=""/>
<layer top_z="6" type="4" extruder="1" color="" extra="M117 hello" gcode="M117 hello"/>
<layer top_z="10.199999999999999" type="1" extruder="1" color="" extra="" gcode="M400 U1"/>
<mode value="MultiExtruder"/>
</plate>
</custom_gcodes_per_layer>
```
- Root `custom_gcodes_per_layer`, then one `<plate>` per plate: `<plate_info id=N>` (1-based),
  `<layer>` entries, then `<mode value=...>` (`SingleExtruder`, `MultiAsSingle`, `MultiExtruder`).
- `type` (CustomGCode.hpp:14-22): 0 ColorChange, 1 PausePrint, 2 ToolChange, 3 Template,
  4 Custom.
- `top_z` = print_z of the layer the action belongs to. `extruder` = filament slot (1-based).
  `color` = new colour for type 0. `extra` = G-code text for type 4, message for type 1.
- `gcode` is informational: the writer fills it (`machine_pause_gcode` for pause,
  `tool_change`, else `extra`), and the reader ignores it when `type` exists (F:3581-3664).
  Without `type`, the reader maps legacy `gcode` values `M600`, `M601`, `tool_change`.
- Without any `<plate>`, entries go to plate 1.
- Only loaded when `Application` starts with `BambuStudio-` (F:2126).

The CLI has `--load-custom-gcodes file.json` (CLI:2264-2290), which **replaces** the custom
G-code of one plate (the `--slice` plate, else plate 1). Its format is
`{"mode":"MultiExtruder","gcodes":[{"type":"PausePrint","print_z":10.2,"color":"","extruder":1,"extra":""}]}`.
The library covers every plate in one pass.

### 2.8 `Metadata/slice_info.config`

Unsliced (all inputs):
```xml
<config>
  <header>
    <header_item key="X-BBL-Client-Type" value="slicer"/>
    <header_item key="X-BBL-Client-Version" value="02.08.02.61"/>
  </header>
</config>
```
When sliced, the writer (F:8766+) adds per plate:
`<plate><metadata key="index" value="1"/>`, `printer_model_id`, `nozzle_diameters`,
`timelapse_type`, `prediction` (s), `weight` (g), `outside`, `support_used`,
`label_object_enabled`, `filament_maps`, `limit_filament_maps`, `extruder_type`,
`nozzle_volume_type`, then `<object identify_id= name= skipped=>`,
`<filament id= tray_info_idx= type= color= used_m= used_g= .../>`, `<warning .../>`,
`<layer_filament_lists>` and similar. On load the reader only fills fields of plates that
`model_settings` created (F:4810), and ignores `<object>` (F:4573).

### 2.9 `Metadata/filament_sequence.json`, `cut_information.xml`, `plate_N.json`, G-code

```json
{"plate_1":{"nozzle_sequence":[],"optimal_assignment":[],"sequence":[]}}
```
```xml
<objects>
 <object id="1">
  <cut_id id="0" check_sum="1" connectors_cnt="0"/>
 </object>
</objects>
```
- `filament_sequence.json`: one key per plate; empty unless the GUI set a filament order.
- `cut_information.xml`: one `<object>` per object (1-based build index); `id="0"` means "never cut".
- `plate_N.json` ("pattern bbox"), `plate_N.gcode` and `.gcode.md5` (uppercase hex MD5) exist only
  after slicing. The loader copies them to a temp dir and parses none of them; `.md5` matches no
  branch (F:2164-2175).

### 2.10 Thumbnails

Optional. The loader only copies PNGs (F:2164-2175). A missing or empty thumbnail is
regenerated by the CLI on export (CLI:7651-7658). Proof: `gen-full.3mf` from `gen.py` has no
PNG and no thumbnail relationships. The CLI loaded it and wrote all five PNGs on re-export. The
GUI renders its own plate view, so a missing PNG only affects the file-browser preview and the
printer screen (for sliced files sent to the printer).

## 3. CLI fit test vs its assemble list

| Assemble list | `model_settings.config` | `3dmodel.model` | `layer_config_ranges.xml` |
|---|---|---|---|
| `baseplate-1x1.stl`, `pos 41.5, 23, 0`, `filaments [1]` | object 2 `name baseplate-1x1_1`, `extruder 1`; part `source_offset_z 2.5`, `matrix ... 41.5 ... 23 ... 2.5` | component transform `... 41.5 23 2.5`, item identity | — |
| `bins/a01.stl`, `pos 88.25, 22.75`, `height_ranges [{21, 25.6, extruder "2"}]` | object 4 `a01_1`, `extruder 1` | component `... 88.25 22.75 12.6999998` | `<object id="2"><range min_z="21" max_z="25.600000381469727"><option opt_key="extruder">2</option>` |
| `labels/a01.stl` + `labels/a01-text.stl`, both `assemble_index [1]`, `filaments [1]` / `[2]` | object 7 `assemble_1` (no object extruder); parts `a01_1` `extruder 1` and `a01-text_1` `extruder 2` | two components in one sub-file `object_3.model` | — |
| `plate_name "fit test"`, `need_arrange false` | plate `plater_name "fit test"` | — | — |
| `print_params` (plate 8 + coupon) | object `<metadata key="enable_support" value="0"/>` | — | — |

**`pos_x`/`pos_y` = where the STL's own origin lands, not the bbox centre.** CLI:1289 calls
`object->translate(pos_x, pos_y, pos_z)` after the mesh was re-centred on its bbox, so the part
translation = pos + STL bbox centre. Tested with an off-centre cube (bbox x 100-110, y 0-10,
z 3-13) at pos (50, 60): component transform `155 65 8`, `source_offset 105 5 8`. The tray
STLs are centred in XY, so for them origin and centre are the same point. `pos_z 0` does not drop
an object to the bed in the export (the cube kept z 3-13). Bambu drops it on load.

Transform layout in a CLI export: the placement lives in the **component** transform and the
build item is identity. After one CLI load-and-export, single-part objects move the placement to
the **build item** and the component becomes identity (F:2411-2429 moves the volume offset into
the instance). Both layouts load the same.

CLI quirk: a load-and-export doubles the part `matrix` of assembled objects (`132 → 264`,
`7.5 → 15`). It is harmless because `matrix` only feeds reload-from-disk, but a library should
not trust `matrix` for placement. It should read the component and item transforms.

## 4. What the GUI stores that the CLI does not

No GUI-saved sample exists (see 0.1). From the writer and reader code, these are the exact forms:

| GUI action | Where | Exact form | Library status |
|---|---|---|---|
| Per-object support / brim / infill / walls | `model_settings.config` `<object>` | `<metadata key="enable_support" value="1"/>`, `<metadata key="brim_type" value="outer_only"/>`, `<metadata key="sparse_infill_density" value="25%"/>`, `<metadata key="wall_loops" value="4"/>` | done, survives CLI |
| Per-part setting (modifier) | `<part>` metadata | `<metadata key="sparse_infill_density" value="50%"/>` under a `subtype="modifier_part"` part | not done (needs a modifier mesh) |
| Height range modifiers | `layer_config_ranges.xml` | section 2.6 | done, survives CLI |
| Plate bed type | plate metadata | `<metadata key="bed_type" value="Textured PEI Plate"/>` after `locked` | done, survives CLI |
| Plate filament grouping (dual nozzle) | plate metadata | `<metadata key="filament_map_mode" value="Manual"/>` `<metadata key="filament_maps" value="1 2"/>` | done, survives CLI |
| Plate print sequence | plate metadata | `<metadata key="print_sequence" value="by object"/>` | trivial to add |
| Pause / colour change / custom G-code | `custom_gcode_per_layer.xml` | section 2.7 | done, survives CLI |
| Support / seam / colour painting | `3D/Objects/*.model` `<triangle paint_supports="4"/>` | section 2.3 | not done; whole-triangle painting is feasible |
| Brim ears | `Metadata/brim_ear_points.txt` (F:181, keyed by 1-based object index) | format not studied | not done |
| Filament colours for all slots | `project_settings.config` | `"filament_colour": ["#000000", "#1E90FF"]` | done (CLI writes only 1 entry) |
| Sliced output | `Metadata/plate_N.gcode`, `.gcode.md5`, `plate_N.json`, `slice_info.config` `<plate>`, plate `gcode_file`/`prediction`/`weight` | section 2.8 | library **removes** these on any patch (stale) |
| Embedded user presets | `Metadata/*.config` presets (`_add_project_embedded_presets_to_archive`) | not studied | not needed |

## 5. Prototype and patch survival

`$S/proto/bambu3mf.py` (stdlib: `zipfile`, `json`, `xml.etree`; about 520 lines).

- `Project(path).summary()` returns: application, sliced flag, printer, process, bed type,
  filaments (slot, preset, type, colour), filament map and mode, extruders, plates (id, name,
  bed type override, map override, objects), objects (root id, build index, name, extruder,
  per-object settings, parts with extruder and settings, height ranges), custom G-code.
  Verified on all 8 inputs plus `pos.3mf` (`$S/proto/summary-*.json`). Run it as
  `python3 -I bambu3mf.py FILE.3mf`.
- Patches: `set_bed_type(name, plate=None)`, `set_filament_colours([...])`,
  `set_filament_map([1, 2], plate=None, mode='Manual')`, `set_object_setting(obj, key, value)`,
  `clear_object_setting`, `set_extruder(obj, n, part_name=None)`,
  `set_height_range(obj, min_z, max_z, {opts})`, `add_custom_gcode(plate, top_z, kind, ...)`,
  `rename_plate`, `rename_object`, `invalidate_slices()`.
- `save(path)` keeps entry order, compression type and date of every entry. New entries are
  deflated (PNG stored). It inserts `layer_config_ranges.xml` before `project_settings.config` and
  `custom_gcode_per_layer.xml` before `model_settings.config`. It never rewrites
  `3dmodel.model` or meshes.

Verification (`test_roundtrip.py`): 13 patches on the fit test → save → `python -m zipfile -t`
passes → re-read: 13/13 PASS. Entry order unchanged. Only one entry added
(`custom_gcode_per_layer.xml`). Smoke test (bed, object setting, pause, plate rename) passed on
plates A, C and plate 8 + coupon. A synthetic sliced file (G-code, md5, json, `<plate>` in
slice_info, `gcode_file`, `prediction`) came back unsliced after a patch.

CLI checks on `$S/proto/patched.3mf`: `--info` rc=0 (3 objects). `--export-3mf re.3mf` rc=0 in
0.06 s.

| Patch | Written to | Survives CLI load + `--export-3mf` | Survives with `--load-settings machine;process --load-filaments` |
|---|---|---|---|
| Project bed type `Textured PEI Plate` | `project_settings` `curr_bed_type` | yes | yes (not a key in these preset files) |
| Plate bed type `Supertack Plate` | plate `bed_type` | yes | yes |
| Filament colours (2 entries) | `filament_colour` | yes | yes |
| Filament map `[1, 2]`, mode `Manual` | `filament_map`, `filament_map_mode`, `filament_extruder_variant` | yes | yes |
| Plate filament map `1 2`, `Manual` | plate `filament_map_mode`, `filament_maps` | yes | yes |
| Per-object `enable_support`, `sparse_infill_density`, `brim_type`, `wall_loops` | object metadata | yes (key order changes) | yes |
| Object extruder 2 | object `extruder` | yes | yes |
| Part extruder | part `extruder` | yes | yes |
| Replace height range, add a 2-option range | `layer_config_ranges.xml` | yes (re-sorted by index, floats re-printed) | yes |
| Pause, colour change, custom G-code | `custom_gcode_per_layer.xml` | yes (`top_z` printed as `10.199999999999999`) | yes |
| Plate rename | plate `plater_name` | yes | yes |
| (CLI rewrite, not a patch) | assembled object gets `extruder 1`; assembled part `matrix` doubled; placement moves from component to item for single-part objects; dates; `Thumbnail_*` metadata added | — | — |

Keys present in the `--load-settings` files overwrite the project values, unless they are listed
in `different_settings_to_system` (CLI:3263-3305). For example, a project-level
`sparse_infill_density` patch is lost if the process file sets it. Per-object, per-plate and
per-layer data are never touched by `--load-settings`.

Slicing could not confirm that the slicer honours the patches. Headless `--slice 1` fails on
this Mac even for unpatched files (fit test: rc=0, no output; plate 10: "No valid nozzle found").
With `filament_map [1,2]` but the old `filament_extruder_variant`, it segfaults after
`could not found extruder_type Bowden, nozzle_volume_type Standard, filament_index 2, extruder
index 2` (PC:9143). With the variant fixed, that error disappears. **To do: open
`$S/proto/patched.3mf` in the GUI once, slice plate 1, and check that the pause, the colour
change, the supertack bed and nozzle 2 appear in the preview.**

## 6. Generating a project from STLs with no CLI

`$S/proto/gen.py` writes `[Content_Types].xml`, `_rels/.rels`, `3D/3dmodel.model`, model rels,
one `3D/Objects/object_N.model` per object (re-centred meshes, parts as components),
`model_settings.config` (objects, parts, extruders, per-object settings, one plate) and
`project_settings.config` from a template. No thumbnails, no `slice_info`, no `cut_information`,
no `filament_sequence`.

Test: base plate, bin and a two-part label on filaments 1 and 2, settings from the fit test's
`project_settings` with Textured PEI and two colours.

| Variant | `--info` | `--export-3mf` | Result |
|---|---|---|---|
| full template settings | rc=0 | rc=0 | Same objects, parts, extruders, placement as the CLI's own export. `project_settings` unchanged (0 keys differ). CLI added all PNGs, `cut_information`, `slice_info`, `filament_sequence`. |
| 5 required keys only | rc=0 | rc=0 | **Silently wrong**: 559 keys, 303 differ. `wall_loops 2`, `sparse_infill_density 20%`, `nozzle_temperature ["200"]`, `extruder_type ["Direct Drive"]` (factory defaults) under the name `Gridfinity PLA 0.20 @BBL X2D`. |
| 4 keys (third-party file has 36 keys, no `printable_height`) | rc=139 | — | segfault |

Strictly required, from the code and a key bisection (55 CLI runs, `bisect_keys.py`):
1. `_rels/.rels` pointing at `/3D/3dmodel.model` (F:1916).
2. `3D/_rels/3dmodel.model.rels` listing every sub-model the components name (F:5383).
3. `<metadata name="Application">BambuStudio-...` in the root model, or no config loads at all (F:2039).
4. In `project_settings.config`: `printer_settings_id`, `print_settings_id`,
   `filament_settings_id`, `nozzle_diameter` (CLI:2010-2014 dereference them) and
   `printable_height` (CLI:2095). Without them the CLI segfaults.
5. `model_settings.config` with `<object>` names and a `<plate>` holding `<model_instance>`
   entries. Without it, no plates and no per-object config are created (F:2130).

What the loader does **not** do: fill missing keys from the named presets. `load_from_json`
sets only the keys present (F:2858-2881). The CLI then fills the rest from compiled defaults.
The GUI's `PresetBundle` path was not in the checkout, so GUI behaviour for a partial config is
unverified.

Building a full `project_settings` from the resolved preset JSONs without the CLI was tested
(`merge_test.py`: PLA template + PETG process/filament JSON vs the CLI's plate 10 output).
26 keys still differ:
- Filament per-variant vectors (6 entries in the preset) collapse to one value per filament,
  chosen by `filament_extruder_variant`. Some lists have 4 entries (`filament_dev_ams_drying_*`).
- Value normalisation: `"100"` → `"100%"`, `"1.0"` → `"1"`, `"0.3x0.5"` → `"0.3,0.5"`,
  `"45.0"` → `"45%"`.
- `filament_ids` comes from `filament_id`; `printer_settings_id` comes from the machine name.
- Per-filament keys whose preset value is `nil` keep a value from the base.

**Verdict: partially feasible. Geometry and project structure: feasible and proven.
`project_settings` from scratch: not worth it.** Reasons:
- The mesh, component, plate and per-object layers are small, documented above, and the CLI
  loads the result exactly like its own export.
- A complete, correct `project_settings` needs Bambu's preset resolution: inheritance, variant
  collapse, value normalisation, defaults for 155 keys that no preset file holds. A partial
  config fails silently with wrong temperatures and walls, which is the worst failure mode.
- The CLI is not the slow part: `--export-3mf` takes about 0.1 s. Only slicing is slow, and it
  is broken headless on this Mac anyway.

Recommended design for the library:
1. Once per (printer, process, filament set), make a template project with the CLI (or reuse
   any project with that combination) and cache its `project_settings.config`.
2. Generate geometry and per-object data with `gen.py`, or let the CLI do the assembly as now.
3. Patch with `bambu3mf.py` for what the CLI cannot set: all filament colours, manual nozzle map
   (with the variant fix), per-plate bed type, pauses and colour changes on any plate, per-part
   extruders after assembly, extra height-range options, renames.
4. Optionally run `--export-3mf` once as a validator (it normalises the file and adds thumbnails).

## 7. Gaps and next steps

- No real GUI-saved or sliced project was available. One GUI save with painted supports, a
  modifier and a slice would confirm sections 2.3, 2.8 and 4.
- Paint bit layout is from memory (`TriangleSelector.cpp` not in the sparse checkout).
- When a filament moves to nozzle 2 (Bowden), its per-variant values (temperatures, retraction)
  stay the direct-drive values. For PLA and PETG on the X2D they are the same in the presets
  seen here, but the library should re-resolve them from the filament preset.
- `mode` in `custom_gcode_per_layer.xml` is set to `MultiExtruder` for more than one filament.
  This matches the CLI round trip but the slicer's use of it is unverified.

## Appendix: `project_settings.config` keys by source

Classified against `<project>/plates/config/{process-gridfinity,filament-pla,machine}.json`
(first match wins in that order); "other" keys come from compiled defaults or are derived.

**process: 188 keys.** `avoid_crossing_wall_includes_support`, `bottom_color_penetration_layers`, `bottom_shell_layers`, `bottom_shell_thickness`, `bottom_surface_density`, `bottom_surface_pattern`, `bridge_flow`, `bridge_no_support`, `bridge_speed`, `brim_object_gap`, `brim_type`, `brim_width`, `circle_compensation_manual_offset`, `compatible_printers_condition`, `default_acceleration`, `detect_floating_vertical_shell`, `detect_overhang_wall`, `detect_thin_wall`, `draft_shield`, `elefant_foot_compensation`, `enable_arc_fitting`, `enable_circle_compensation`, `enable_height_slowdown`, `enable_overhang_speed`, `enable_prime_tower`, `enable_support`, `enable_support_ironing`, `enable_tower_interface_features`, `enable_wrapping_detection`, `filename_format`, `fill_multiline`, `from`, `fuzzy_skin`, `fuzzy_skin_first_layer`, `fuzzy_skin_mode`, `fuzzy_skin_noise_type`, `fuzzy_skin_octaves`, `fuzzy_skin_persistence`, `fuzzy_skin_point_distance`, `fuzzy_skin_scale`, `fuzzy_skin_thickness`, `gap_infill_speed`, `infill_combination`, `infill_direction`, `infill_instead_top_bottom_surfaces`, `infill_lock_depth`, `infill_rotate_step`, `infill_shift_step`, `infill_wall_overlap`, `initial_layer_acceleration`, `initial_layer_infill_speed`, `initial_layer_line_width`, `initial_layer_print_height`, `initial_layer_speed`, `initial_layer_travel_acceleration`, `inner_wall_acceleration`, `inner_wall_line_width`, `inner_wall_speed`, `interface_shells`, `internal_bridge_support_thickness`, `internal_solid_infill_line_width`, `internal_solid_infill_speed`, `ironing_flow`, `ironing_inset`, `ironing_spacing`, `ironing_speed`, `ironing_type`, `layer_height`, `line_width`, `locked_skeleton_infill_pattern`, `locked_skin_infill_pattern`, `max_bridge_length`, `max_travel_detour_distance`, `minimum_sparse_infill_area`, `monotonic_travel_into_wall`, `name`, `outer_wall_acceleration`, `outer_wall_line_width`, `outer_wall_speed`, `overhang_1_4_speed`, `overhang_2_4_speed`, `overhang_3_4_speed`, `overhang_4_4_speed`, `overhang_totally_speed`, `override_filament_scarf_seam_setting`, `pre_start_fan_time`, `prime_tower_brim_width`, `prime_tower_enable_framework`, `prime_tower_flat_ironing`, `prime_tower_lift_height`, `prime_tower_lift_speed`, `prime_tower_max_speed`, `prime_tower_width`, `print_extruder_id`, `print_extruder_variant`, `print_sequence`, `print_settings_id`, `raft_layers`, `reduce_crossing_wall`, `reduce_infill_retraction_mode`, `resolution`, `scarf_angle_threshold`, `seam_placement_away_from_overhangs`, `seam_position`, `seam_slope_gap`, `seam_slope_min_length`, `seam_slope_start_height`, `seam_slope_type`, `skeleton_infill_density`, `skeleton_infill_line_width`, `skin_infill_density`, `skin_infill_depth`, `skin_infill_line_width`, `skirt_distance`, `skirt_height`, `skirt_loops`, `skirt_per_object`, `slowdown_end_acc`, `slowdown_end_height`, `slowdown_end_speed`, `slowdown_start_acc`, `slowdown_start_height`, `slowdown_start_speed`, `small_perimeter_speed`, `small_perimeter_threshold`, `smooth_coefficient`, `solid_infill_filament`, `sparse_infill_acceleration`, `sparse_infill_density`, `sparse_infill_filament`, `sparse_infill_lattice_angle_1`, `sparse_infill_lattice_angle_2`, `sparse_infill_line_width`, `sparse_infill_pattern`, `sparse_infill_speed`, `spiral_mode`, `standby_temperature_delta`, `support_base_pattern`, `support_base_pattern_spacing`, `support_bottom_z_distance`, `support_expansion`, `support_filament`, `support_interface_bottom_layers`, `support_interface_filament`, `support_interface_loop_pattern`, `support_interface_pattern`, `support_interface_spacing`, `support_interface_speed`, `support_interface_top_layers`, `support_ironing_direction`, `support_ironing_flow`, `support_ironing_inset`, `support_ironing_pattern`, `support_ironing_spacing`, `support_ironing_speed`, `support_line_width`, `support_object_xy_distance`, `support_on_build_plate_only`, `support_speed`, `support_style`, `support_threshold_angle`, `support_top_z_distance`, `support_type`, `symmetric_infill_y_axis`, `top_color_penetration_layers`, `top_shell_layers`, `top_shell_thickness`, `top_solid_infill_flow_ratio`, `top_surface_acceleration`, `top_surface_density`, `top_surface_line_width`, `top_surface_pattern`, `top_surface_speed`, `travel_acceleration`, `travel_short_distance_acceleration`, `travel_speed`, `travel_speed_z`, `tree_support_branch_angle`, `tree_support_branch_diameter`, `tree_support_wall_count`, `vertical_shell_speed`, `wall_filament`, `wall_generator`, `wall_loops`, `wipe_tower_no_sparse_layers`, `xy_contour_compensation`, `xy_hole_compensation`, `z_direction_outwall_speed_continuous`

**machine: 107 keys.** `auxiliary_fan`, `bed_exclude_area`, `bed_temperature_formula`, `best_object_pos`, `change_filament_gcode`, `cooling_filter_enabled`, `default_filament_profile`, `default_nozzle_volume_type`, `default_print_profile`, `deretraction_speed`, `enable_long_retraction_when_cut`, `enable_pre_heating`, `extruder_clearance_dist_to_rod`, `extruder_clearance_height_to_lid`, `extruder_clearance_height_to_rod`, `extruder_clearance_max_radius`, `extruder_colour`, `extruder_max_nozzle_count`, `extruder_offset`, `extruder_printable_area`, `extruder_printable_height`, `extruder_type`, `extruder_variant_list`, `fan_direction`, `farthest_point_timelapse`, `gcode_flavor`, `grab_length`, `group_algo_with_time`, `head_wrap_detect_zone`, `hotend_cooling_rate`, `hotend_heating_rate`, `layer_change_gcode`, `long_retractions_when_cut`, `machine_bed_mass_Y`, `machine_end_gcode`, `machine_hotend_change_time`, `machine_load_filament_time`, `machine_max_acceleration_e`, `machine_max_acceleration_extruding`, `machine_max_acceleration_retracting`, `machine_max_acceleration_travel`, `machine_max_acceleration_x`, `machine_max_acceleration_y`, `machine_max_acceleration_z`, `machine_max_force_Y`, `machine_max_jerk_e`, `machine_max_jerk_x`, `machine_max_jerk_y`, `machine_max_jerk_z`, `machine_max_printed_mass`, `machine_max_speed_e`, `machine_max_speed_x`, `machine_max_speed_y`, `machine_max_speed_z`, `machine_min_extruding_rate`, `machine_min_travel_rate`, `machine_pause_gcode`, `machine_prepare_compensation_time`, `machine_start_gcode`, `machine_switch_extruder_time`, `machine_unload_filament_time`, `master_extruder_id`, `max_layer_height`, `min_layer_height`, `nozzle_diameter`, `nozzle_flush_dataset`, `nozzle_height`, `nozzle_type`, `nozzle_volume`, `physical_extruder_map`, `print_in_clockwise`, `printable_area`, `printable_height`, `printer_extruder_id`, `printer_extruder_variant`, `printer_model`, `printer_settings_id`, `printer_structure`, `printer_technology`, `printer_variant`, `retract_before_wipe`, `retract_length_toolchange`, `retract_lift_above`, `retract_lift_below`, `retract_restart_extra`, `retract_restart_extra_toolchange`, `retract_when_changing_layer`, `retraction_distances_when_cut`, `retraction_length`, `retraction_minimum_travel`, `retraction_speed`, `scan_first_layer`, `silent_mode`, `single_extruder_multi_material`, `support_air_filtration`, `support_chamber_temp_control`, `support_cooling_filter`, `support_fast_purge_mode`, `support_object_skip_flush`, `time_lapse_gcode`, `upward_compatible_machine`, `wipe`, `wipe_distance`, `wrapping_detection_gcode`, `wrapping_exclude_area`, `z_hop`, `z_hop_types`

**filament: 128 keys.** `activate_air_filtration`, `additional_cooling_fan_speed`, `additional_fan_full_speed_layer`, `chamber_temperatures`, `circle_compensation_speed`, `close_additional_fan_first_x_layers`, `close_fan_the_first_x_layers`, `complete_print_exhaust_fan_speed`, `cool_plate_temp`, `cool_plate_temp_initial_layer`, `cooling_perimeter_transition_distance`, `cooling_slowdown_logic`, `counter_coef_1`, `counter_coef_2`, `counter_coef_3`, `counter_limit_max`, `counter_limit_min`, `diameter_limit`, `during_print_exhaust_fan_speed`, `eng_plate_temp`, `eng_plate_temp_initial_layer`, `fan_cooling_layer_time`, `fan_max_speed`, `fan_min_speed`, `filament_adaptive_volumetric_speed`, `filament_adhesiveness_category`, `filament_bridge_speed`, `filament_change_length`, `filament_cooling_before_tower`, `filament_cost`, `filament_density`, `filament_deretraction_speed`, `filament_dev_ams_drying_ams_limitations`, `filament_dev_ams_drying_heat_distortion_temperature`, `filament_dev_ams_drying_temperature`, `filament_dev_ams_drying_time`, `filament_dev_chamber_drying_bed_temperature`, `filament_dev_chamber_drying_time`, `filament_dev_drying_cooling_temperature`, `filament_dev_drying_softening_temperature`, `filament_diameter`, `filament_enable_overhang_speed`, `filament_end_gcode`, `filament_extruder_compatibility`, `filament_extruder_variant`, `filament_flow_ratio`, `filament_flush_temp`, `filament_flush_temp_fast`, `filament_flush_volumetric_speed`, `filament_is_support`, `filament_long_retractions_when_cut`, `filament_max_volumetric_speed`, `filament_metal_stickiness`, `filament_minimal_purge_on_wipe_tower`, `filament_overhang_1_4_speed`, `filament_overhang_2_4_speed`, `filament_overhang_3_4_speed`, `filament_overhang_4_4_speed`, `filament_overhang_totally_speed`, `filament_pre_cooling_temperature`, `filament_pre_cooling_temperature_nc`, `filament_preheat_temperature_delta`, `filament_prime_volume`, `filament_prime_volume_nc`, `filament_printable`, `filament_ramming_travel_time`, `filament_ramming_travel_time_nc`, `filament_ramming_volumetric_speed`, `filament_ramming_volumetric_speed_nc`, `filament_retract_before_wipe`, `filament_retract_length_nc`, `filament_retract_restart_extra`, `filament_retract_when_changing_layer`, `filament_retraction_distances_when_cut`, `filament_retraction_length`, `filament_retraction_minimum_travel`, `filament_retraction_speed`, `filament_scarf_gap`, `filament_scarf_height`, `filament_scarf_length`, `filament_scarf_seam_type`, `filament_settings_id`, `filament_shrink`, `filament_soluble`, `filament_start_gcode`, `filament_tower_interface_pre_extrusion_dist`, `filament_tower_interface_pre_extrusion_length`, `filament_tower_interface_print_temp`, `filament_tower_interface_purge_volume`, `filament_tower_ironing_area`, `filament_type`, `filament_velocity_adaptation_factor`, `filament_vendor`, `filament_wipe`, `filament_wipe_distance`, `filament_z_hop`, `filament_z_hop_types`, `first_x_layer_fan_speed`, `full_fan_speed_layer`, `hole_coef_1`, `hole_coef_2`, `hole_coef_3`, `hole_limit_max`, `hole_limit_min`, `hot_plate_temp`, `hot_plate_temp_initial_layer`, `impact_strength_z`, `long_retractions_when_ec`, `no_slow_down_for_cooling_on_outwalls`, `nozzle_temperature`, `nozzle_temperature_initial_layer`, `nozzle_temperature_range_high`, `nozzle_temperature_range_low`, `overhang_fan_speed`, `overhang_fan_threshold`, `override_process_overhang_speed`, `reduce_fan_stop_start_freq`, `required_nozzle_HRC`, `retraction_distances_when_ec`, `slow_down_for_layer_cooling`, `slow_down_layer_time`, `slow_down_min_speed`, `supertack_plate_temp`, `supertack_plate_temp_initial_layer`, `temperature_vitrification`, `textured_plate_temp`, `textured_plate_temp_initial_layer`, `volumetric_speed_coefficients`

**other (defaults or derived): 155 keys.** `accel_to_decel_enable`, `accel_to_decel_factor`, `alternate_extra_wall`, `ams_filament_load_time_ams`, `ams_filament_load_time_ams_lite`, `ams_filament_load_time_n3f_s`, `ams_filament_unload_time_ams`, `ams_filament_unload_time_ams_lite`, `ams_filament_unload_time_n3f_s`, `apply_scarf_seam_on_circles`, `bed_custom_model`, `bed_custom_texture`, `bed_heat_soak_area`, `before_layer_change_gcode`, `bridge_angle`, `counterbore_hole_bridging`, `curr_bed_type`, `default_ams_type`, `default_filament_colour`, `default_jerk`, `detect_narrow_internal_solid_infill`, `different_settings_to_system`, `embedding_wall_into_infill`, `enable_filament_dynamic_map`, `enable_mixed_color_sublayer`, `enable_order_independent_overlap_carving`, `enable_overhang_bridge_fan`, `enable_pressure_advance`, `enforce_support_layers`, `ensure_vertical_shell_thickness`, `exclude_object`, `extruder_ams_count`, `extruder_nozzle_stats`, `filament_change_length_nc`, `filament_colour`, `filament_ids`, `filament_is_mixed`, `filament_map`, `filament_map_2`, `filament_map_mode`, `filament_mixed_components`, `filament_mixed_gradient`, `filament_mixed_gradient_curve`, `filament_mixed_gradient_per_part`, `filament_mixed_gradient_range`, `filament_mixed_sublayer_ratios`, `filament_notes`, `filament_nozzle_map`, `filament_self_index`, `filament_volume_map`, `filter_out_gap_fill`, `first_layer_print_sequence`, `first_x_layer_part_fan_speed`, `flush_into_infill`, `flush_into_objects`, `flush_into_support`, `flush_multiplier`, `flush_multiplier_fast`, `flush_volumes_matrix`, `flush_volumes_vector`, `gcode_add_line_number`, `has_filament_switcher`, `has_scarf_joint_seam`, `independent_support_layer_height`, `infill_jerk`, `inherits_group`, `initial_layer_flow_ratio`, `initial_layer_jerk`, `inner_wall_jerk`, `interlocking_beam`, `interlocking_beam_layer_count`, `interlocking_beam_width`, `interlocking_boundary_avoidance`, `interlocking_depth`, `interlocking_orientation`, `internal_solid_infill_pattern`, `ironing_direction`, `ironing_fan_speed`, `ironing_pattern`, `is_infill_first`, `min_bead_width`, `min_feature_size`, `mmu_segmented_region_interlocking_depth`, `mmu_segmented_region_max_width`, `nozzle_volume_type`, `only_one_wall_first_layer`, `ooze_prevention`, `other_layers_print_sequence`, `other_layers_print_sequence_nums`, `outer_wall_jerk`, `overhang_threshold_participating_cooling`, `post_process`, `precise_outer_wall`, `precise_z_height`, `pressure_advance`, `prime_tower_extra_rib_length`, `prime_tower_fillet_wall`, `prime_tower_infill_gap`, `prime_tower_rib_wall`, `prime_tower_rib_width`, `prime_tower_skip_points`, `prime_volume_mode`, `print_compatible_printers`, `print_flow_ratio`, `printer_notes`, `printing_by_object_gcode`, `process_notes`, `raft_contact_distance`, `raft_expansion`, `raft_first_layer_density`, `raft_first_layer_expansion`, `role_base_wipe_speed`, `seam_gap`, `seam_slope_conditional`, `seam_slope_entire_loop`, `seam_slope_inner_walls`, `seam_slope_steps`, `slice_closing_radius`, `slicing_mode`, `smooth_speed_discontinuity_area`, `sparse_infill_anchor`, `sparse_infill_anchor_max`, `spiral_mode_max_xy_smoothing`, `spiral_mode_smooth`, `start_end_points`, `support_angle`, `support_bottom_interface_spacing`, `support_critical_regions_only`, `support_interface_not_for_body`, `support_object_first_layer_gap`, `support_remove_small_overhang`, `template_custom_gcode`, `thick_bridges`, `thumbnail_size`, `timelapse_type`, `top_area_threshold`, `top_one_wall_type`, `top_surface_jerk`, `top_z_overrides_xy_distance`, `travel_jerk`, `tree_support_branch_diameter_angle`, `tree_support_branch_distance`, `use_firmware_retraction`, `use_relative_e_distances`, `version`, `wall_distribution_count`, `wall_sequence`, `wall_transition_angle`, `wall_transition_filter_deviation`, `wall_transition_length`, `wipe_speed`, `wipe_tower_rotation_angle`, `wipe_tower_x`, `wipe_tower_y`, `wrapping_detection_layers`



## Project edits: replace a mesh, add a filament, move an object or the tower (shambu 0.3.x, `edits.py`)

Verified against Studio 02.08.02.61 (source at tag v02.08.02.61) and live CLI runs on copies of real projects.

**Placement.** An object's world position is `<build><item transform>` composed with `<component transform>`
(bbs_3mf.cpp:4396, 4439). The 12 numbers are in column order `r00 r10 r20 r01 r11 r21 r02 r12 r22 tx ty tz`, in mm.
A GUI save puts the placement on the item (component identity); a shambu build puts it on the component (item
identity); a CLI re-export folds a one-part object into the item. The part `matrix`, `source_offset_*`,
`<assemble_item>` and `plate_N.json` are not authoritative: moving an object changes the item translation only.
Plates after the first sit at world offsets `(col * W * 1.2, -row * D * 1.2)`, `cols = compute_colum_count(n)`
(PartPlate.cpp:4536-4548, PartPlate.hpp:38-50); `--move X,Y@PLATE` gives plate-local min-corner coordinates.

**Meshes.** Each sub-mesh is written re-centred on its bbox centre (bbs_3mf.cpp:5366); `source_offset_*` of the part is
that centre in the STL's own frame. The GUI's "replace with STL" keeps the volume transform and shifts it by
`R * (c_new - c_old)` so the two STL frames coincide (Plater.cpp:11302-11411), then calls `ensure_on_bed` only when the
object is not sinking (Plater.cpp:11375, 11397). `face_count` and `mesh_stat face_count` are written, never read
(bbs_3mf.cpp:8218, 8306). Studio shares one mesh resource between identical volumes (bbs_3mf.cpp:7353-7367; loaded at
5271-5313): two components (of two objects, or of one object) name the same `p:path` and `objectid`, or a part carries the
`mesh_shared` metadata the reader follows (bbs_3mf.cpp:5282-5313), and `--replace-mesh` refuses such an
object. A raw STL and a shambu-oriented copy of it are in different frames (the oriented copy sits on its min corner):
with `--anchor frame` the raw STL turned by `--rotate-mesh 0,0,90` moves the housing 91.74 mm, so the patch refuses
it and names `--anchor centre`.

**Filaments.** `--load-filaments` on a 3MF replaces slots and keeps the project's filament count
(BambuStudio.cpp:2036). An export loaded with N+1 filaments writes the per-filament keys in slot order but the loaded
preset's variant block FIRST and `filament_self_index` all `"1"` (live: `nozzle_temperature` 220 x 6 then 255 x 6 for
a PETG slot 1 and a PLA slot 2). So `--add-filament` reads the new slot's normalised values from a scratch export
loaded with the new preset ALONE (slot 1 there), the flush matrix from a second scratch export with every slot and
colour, and appends slot N+1 itself. The per-variant keys are Studio's `filament_options_with_variant`
(PrintConfig.cpp:7464); a block's length comes from its preset's `filament_extruder_variant`. A one-entry vector means
"every slot uses entry 0" (Config.hpp:681-685) and is written out before the append. The verify step slices plate 1:
the variant lookup (`could not found extruder_type`, PrintConfig.cpp:8935) runs when a slice applies the config, not on
a plain export.

**Prime tower.** `wipe_tower_x/y` are per-plate vectors of the tower's MIN corner, plate-local (BambuStudio.cpp:4481-4486,
Print.cpp:2423). The CLI estimates the footprint as `x - brim .. x + width + brim`; `--tower` checks
`x - 8 .. x + max(prime_tower_width, 35) + 8` against the bed and x against extruder 2's reach when a filament maps to it.

**`--no-check`.** It skips the conflict check (-101), the support-necessity check and the -100 exit on empty-layer or
overlap warnings; -102..-105 and validate errors still exit (BambuStudio.cpp:6192, 6418, 6974, 7185-7212). Studio exits
on the first conflicting plate (BambuStudio.cpp:7105-7109), and -100 is also the generic exception exit
(BambuStudio.cpp:7413-7418): only a -100 whose log has `found slicing warnings:` is skippable. A checked slice of a BBL
3MF without `--load-filaments` resets `filament_prime_volume` to 45, a `--no-check` one does not
(BambuStudio.cpp:6886-6894); `shambu slice` pins the project's values in every pass. Each pass writes its log and
`result.json` into its own folder `<name>.slice-<random>/<name>.passes/{checked[-plate-N],checked-all,nocheck}/` next to
the output (a fresh folder per run, never an existing one); the export replaces the output only after it reads back, and
an output that is the input is refused.

**The touches contract.** Every patch declares the entries it changes, including what `invalidate_slices` removes
(`Metadata/plate_N.gcode`, `.md5`, `plate_N.json`, the `slice_info.config` plates) and rewrites
(`model_settings.config`). `Project.save()` refuses an undeclared change, addition or deletion, a `3D/*` entry added,
removed or reordered, and any `_rels` change.
