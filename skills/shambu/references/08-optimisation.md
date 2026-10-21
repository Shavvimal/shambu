# 08. Print optimisation: orientation, supports, time and finish (research of 2026-10-13)

What this is: the evidence behind `shambu orient`, `shambu sweep`, the `print_sequence` plate flag, the
`lay_on_face` / `rotate` part keys and the recipes marked "research 08". Seven research passes ran in parallel:
orientation, supports and overhangs, multi-filament time, surface quality, the Bambu Studio source at tag
`v02.08.02.61` plus live headless slices on this Mac, a community sweep (Bambu wiki, forum, release notes), and an
evaluation of ClassCAD as a mesh engine. Reddit returned HTTP 403 and no YouTube transcripts were obtained, so the
community material rests on forum.bambulab.com, wiki.bambulab.com (fetched with `curl -A Mozilla/5.0`; plain
fetches get HTTP 402) and the Studio source. Everything measured is a slicer estimate, not a stopwatch.

Target throughout: Bambu Lab X2D, 0.4 mm nozzle, filament 1 on the left direct-drive nozzle, filament 2 on the
right Bowden nozzle (x >= 20.5 mm, 256 mm tall), no AMS, Bambu Studio 02.08.02.61.

Section map: 1 orientation, 2 print-by-object (source rules), 3 supports and overhangs, 4 time, 5 surface and fit,
6 headless metrics, 7 Studio GUI workflow for the user, 8 ClassCAD, 9 corrections to older references, 10 sources.

---

## 1. Orientation

### 1.1 Rules, most valuable first

1. **Load first, supports second, finish third.** Layers are the weak direction. CNC Kitchen tensile coupons (PLA,
   0.2 mm): flat 63 MPa, 30 deg 44, 45 deg 40, 60 deg 36, upright 31 MPa. Upright is half of flat and the curve is
   S-shaped, so a small tilt gains little. Every angled print on supports in that test failed (warped off the
   supports). A hook or clip: the load must pull along the X/Y lines, never across layers.
2. **Largest flat face on the bed** unless rule 1 or a show face forbids it. This is what Studio's auto-orient and
   Lay on face optimise (bottom area, bottom hull area, overhang area).
3. **Overhangs: 45 deg from vertical prints clean on a 0.4 nozzle at 0.2 mm; 45 to 60 is "test it".** The geometry:
   a wall stepping out `d` per layer of height `h` leans `atan(d/h)`. Bambu's overhang-speed bands are per cent of
   line width unsupported (10/25/50/75/100 %), which at 0.2 mm layers and a 0.42 line is about 12/28/46/58/65 deg.
   An H2D printed five test angles up to 75 deg with straight undersides (one X post, n=1).
4. **Bambu's `support_threshold_angle` counts from HORIZONTAL, default 30.** So the default supports only faces
   within 30 deg of flat (overhangs steeper than 60 deg from vertical). The recipe `more-support` sets 45. The tool's
   own `--threshold` is from vertical (the 45 deg rule), so 45 in `shambu orient` equals 45 in Bambu's key by
   coincidence of the symmetric value only; 30 in Bambu equals 60 in the tool.
5. **Bridge instead of supporting** where the span is short: a measured plate bridged 10 to 40 mm cleanly in PLA,
   60 and 80 mm sagged. The Bambu preset sets `max_bridge_length` to 0, so tree support grows under every bridge
   (recipe `bridge-short-spans` sets 10). Sacrificial one-layer floors over counterbores
   (`counterbore_hole_bridging`) and chamfers over 20 deg replace supports in holes.
6. **Show face up or on a side, never on supports.** Supported faces are rough. A face on textured PEI gets the
   plate's matte texture; the smooth plate gives gloss. Text embossed on the bottom face prints face down with no
   overhang.
7. **Holes: axis vertical for diameter, horizontal holes go oval and droop at the crown** (teardrop or diamond in
   CAD). Vertical holes still print about 0.1 mm under, bridged horizontal bores 0.2 to 0.3 mm under.
8. **Pins, dowels, snap arms: lying down with a D-flat.** A standing pin shears along layers.
9. **Threads: bolts upright, nuts flat. Gears flat. Cylinders: axis vertical for roundness, horizontal (with a
   flat) for bending strength.**
10. **Minimise height when nothing else decides.** Every layer adds fixed cost (Z move, seam travel, retract, the
    minimum layer time). On a two-nozzle plate every layer that holds both filaments also adds a nozzle switch and
    a tower wipe, so a tall orientation multiplies that.
11. **Seam follows orientation**: corners and concave edges hide it, round sides cannot. Rotate about Z so a sharp
    corner or the hidden side faces +Y (`seam-back`), or use `scarf-seam` on cylinders.
12. **Tall thin parts**: lower them, widen the base (brim), or brace. No published height-to-base ratio exists.
13. **Fillets facing the bed are steep overhangs: chamfer bed-facing edges.** Elephant foot widens the first layers:
    chamfer mating edges on the bed side (0.4 x 0.6 mm on the gripper build).
14. **Split instead of compromising**: cut (planar or dovetail, with plug, dowel or snap connectors) so each piece
    prints flat and support-free, then glue. Studio's Cut tool is GUI only (section 7); shambu has no mesh boolean.
15. **Print at 45 deg only with a plan for support adhesion** (few custom support points, smaller XY gap, a cut flat
    corner with a brim). Tilting also removes stair-stepping on shallow slopes.

### 1.2 What `shambu orient` computes

Per candidate orientation (as is, six axis faces, the largest flat faces laid down, duplicates within 1 deg merged):
bed contact area (facets flat on z = min), overhang area (downward facets steeper than the threshold, off the bed),
its shadow (the same area projected onto the bed and summed: overlapping projections count twice and nothing is
occluded, so it is a proxy for the support footprint, not the footprint itself), the steepest overhang present,
height, centroid height and volume. Score = shadow - 0.25 x contact + 2 x height + 50 x (slenderness - 3 when above
3), lower is better; slenderness = height / sqrt(contact). The weights are the tool's own. The default threshold is 60
deg from vertical (Studio's own 30 from horizontal); a facet at exactly the threshold counts as printable. Bridges are
counted as overhang (Studio's auto-orient has the same blind spot, and its wiki says bridges are its main error
source), so a part with real bridges needs the sweep, not the score alone. Internal faces in a non-watertight STL
count as overhang: fix the mesh first.

Studio's auto-orient (`Orient.cpp`, a Tweaker-style method) for comparison: candidates are the current down
direction, the 10 largest facet-normal clusters, the 14 largest convex-hull normal clusters and 18 fixed axis and
45 deg directions. Cost = `RELATIVE_F * (overhang*TAR_C + TAR_D + TAR_LAF*area_laf) / (TAR_D + CONTOUR_F*contour +
BOTTOM_F*bottom + BOTTOM_HULL_F*bottom_hull) + 100*(bottom < 0.1 mm2)`, with the CLI weight set
RELATIVE_F 20, TAR_C 0.1, TAR_D 1, TAR_LAF 0.001, CONTOUR_F 0.5, BOTTOM_F 2.5, BOTTOM_HULL_F 0.1 (`OrientParamsArea`).
`area_laf` is the area of faces 1.4 to 14 deg off horizontal above the first layer (they print as stair steps). It
has no height, time, strength or bridge term. Lowest cost wins; a tie keeps +Z up.

### 1.3 Why the STL is rewritten

The CLI's `--rotate`, `--rotate-x`, `--rotate-y` act on every object at once and the assemble list carries no
transform (reference 01). A per-item `<build><item transform=...>` in the 3MF is honoured by the importer (live test:
a cube rotated 90 deg in the 3MF sliced at its new height), but the tool feeds the CLI STLs through the assemble
list, so it bakes the orientation into a copy of the STL (`<outdir>/shambu/oriented/<stem>__<tag>.stl`), dropped onto
z = 0 at x, y >= 0; the sub-parts of a merged object are turned in one shared frame so they keep their alignment,
and the file name carries a hash of the source path and the orientation so two parts with one stem, or rotations
of 1.2 and 12 degrees, never share a file. The object keeps the original stem as its name.

A part with `"orientation": "auto"` takes Studio's own auto-orient in two passes instead. Pass 1 is an orient-only
run on the bare STL: `--load-settings "<machine.json>;<process.json>" --convert-unit --orient=1 --export-stls DIR
--outputdir DIR <part>.stl`, in that argv order (transforms run in argv order, so the unit fix comes first). Studio
writes `DIR/obj_1_<stem>.stl` with the turn baked in and prints its candidate table on stdout; shambu keeps its own
`stands` check on that file. Pass 2 is the normal slicing run with the assemble list pointing at the exported file;
it never passes `--orient`, so the config replacement (reference 01, finding 10) cannot happen. Studio's printed
vector is the mesh direction that ends up pointing up, so the matching spec value is `lay_on_face = -best`. Live on
bracket, carrier, jaw_posy, tag_bridge and bottle-funnel (Studio 02.08.02.61): Studio's export and shambu's copy
with `lay_on_face = -best` gave the same contact, support shadow and height, and on all five shambu's own score
ranked that face first too. Pass 1 takes 0.07 to 0.13 s per part. A merged part (`parts: [...]`) keeps shambu's own
best candidate, because Studio turns each positional input on its own. `shambu orient --native` prints Studio's
table; `shambu sweep --orientations PART --source studio` takes its candidates (negated, filtered by `stands`, top N by
`unprintability`) and bakes each one with the code above.

### 1.4 Myths

- "45 deg is a hard limit": no, it depends on nozzle, layer height, cooling and material.
- "Tall prints are slow because Z is slow": no, per-layer overhead and minimum layer time are the cost.
- "Auto-orient finds the strongest orientation": no, it ignores load direction and time (source).

---

## 2. Print-by-object: the exact rules (Print.cpp `sequential_print_clearance_valid`, P:597-940, tag v02.08.02.61)

Live tests on this Mac confirmed every rule below (CLI exit -63 "object collision in sequential print" on failure).

- **When it applies**: `print_sequence == by object` and more than one object on the plate (PH:954). The plate's
  `<metadata key="print_sequence" value="by object"/>` in `Metadata/model_settings.config` beats the process key
  (BS:6897); the process default is `by layer`.
- **Pre-checks**: Smooth timelapse (`timelapse_type` 1) and clumping detection (`enable_wrapping_detection` 1) are
  refused with by object. X2D defaults are 0 and 0.
- **Horizontal rule**: each object's 2D convex hull grows by `0.5 * extruder_clearance_max_radius - 0.1` mm with
  round joins, and no two grown hulls may intersect: hull-to-hull gap >= `radius - 0.2` = **59.8 mm on the X2D**
  (59 mm fails, 60 passes). If every object is shorter than `nozzle_height` (4 mm) the gap drops to 3.8 mm. A
  per-object skirt adds its distance (X2D processes have `skirt_loops` 0). shambu checks bounding boxes, which
  contain the hulls, with a 60 mm gap: a safe over-estimate.
- **Print order** = the order of `<item>` elements in the 3MF build = the assemble-list order (the old sort by
  position is inside `#if 0`). Between objects the G-code changes tool, lifts to max z + z hop and travels.
- **Vertical rule**, for object k in print order: every object's bbox grows by `dist_to_rod / 2` per side (16.5 mm on
  the X2D), so two objects "share a Y band" when their raw Y gap is under `dist_to_rod` (33 mm). Limit =
  `height_to_lid` (133), or `printable_height` (261, 256 on the right nozzle) for the last object. If the grown Y
  range of any LATER object overlaps k's grown range, the limit is `height_to_rod` (32.8). X separation does not help.
  Tests: 40 mm cube first, 10 mm second, same Y: "t_1 is too tall". Tall one second: passes. Tall first with a
  40 mm Y gap: passes; 30 mm gap: fails.
- **X2D values** (`Bambu Lab X2D 0.4 nozzle.json`): `extruder_clearance_max_radius` 60, `height_to_rod` 32.8,
  `height_to_lid` 133, `dist_to_rod` 33 (inherited), `nozzle_height` 4, `machine_switch_extruder_time` 5 (statistics
  only), `best_object_pos` 0.3x0.5. Other machines: H2D 96 / 47.4 / 201, X1C and P1S 68 / 34 / 90, A1 73 / 25 / 256.
- **Prime tower**: the "no tower with by object and two extruders" check (P:1472) is inside `#if 0` and never runs.
  `normalize_fdm_2` switches the tower off itself when by object with more than one object. Live test: two objects,
  one per nozzle, `enable_prime_tower` 1: exit 0, no `; FEATURE: Prime tower`, objects printed one after another.
  Not tested: one by-object object that uses both nozzles (its per-object tool ordering might want a tower).
- **Arrange** in by-object mode uses max(user spacing, radius + 0.001) and sorts bed temperature desc, height asc,
  area desc; Studio still prints in object-list order, so a headless tool orders the objects itself.

What shambu does with this (`layout.SequentialClearance`, `ShelfPacker(keep_order, solo_row_above)`, `build.plan`):
orders objects by main filament then height (two groups of single-filament objects then need one nozzle change
in total; an object that uses two filaments itself still switches within its own layers), packs them in print
order with a 60 mm gap (4 mm when all are under 4 mm tall, plus a per-object skirt), closes a row after any object
over 32.8 mm so nothing later shares its Y band, writes the plate `print_sequence` (a `print_sequence` in the
process settings counts, the plate's own value wins), warns that a multi-object plate has no tower, and refuses
Smooth timelapse and clumping detection. With one object on the plate none of this applies, as in Studio.

**Merged groups (`group_by_filament: true`, by object only).** The CLI translates each assemble-list Item by its
own `pos`, then merges it into `assemble_k` with `add_volume`, which copies the volume's transformation
(BambuStudio.cpp 1251-1287, Model.cpp 1452-1467). So the volumes of one merged object keep their own positions,
and Studio prints that object by layer as one unit: every layer of the group holds all its members, then the next
object starts. shambu merges single-file parts with no `at` that share the main filament, the settings (`set`)
and the height ranges. It packs each cluster locally with the layout gap (5 mm plus brim margins), places the
cluster as one object 60 mm from the others, and writes one Item per member spec with `assemble_index` k and
`pos` = group start + member offset - STL min (copies relative to the first copy). A cluster of one stays a
plain object. Groups take the names `group-f<n>` (or `group-f<n>-<k>` when two clusters share a filament), and
the report lists the members. Live (2026-10-13, X2D, 2 x PLA): two filament-1 boxes and two filament-2 boxes
gave 2 objects, 1 tool change, and every box printed within 0.2 mm of its planned bbox.

---

## 3. Supports, overhangs and bridging

### 3.1 Per-nozzle arrays (the trap)

On the X2D every speed and acceleration key and `enable_overhang_speed` is a 6-slot array in the order Direct
Drive Standard / High Flow / E3D High Flow, then Bowden Standard / High Flow / E3D High Flow (`print_extruder_variant`,
`print_extruder_id` 1,1,1,2,2,2). Writing one scalar into `bridge_speed`, `support_speed`, `overhang_*_speed` or
`support_interface_speed` loses the other five values. No recipe writes these keys.

### 3.2 Bambu's own support recipe that the headless CLI never applies

`resources/profiles/BBL/filament/support_recommended_params.json` drives the X2D "recommended support parameters"
dialog in `Tab.cpp`; only the GUI runs it. For a PLA or PETG part with the other material (or "Support for
PLA/PETG") as interface, Bambu applies: `tree(auto)`, `support_style` `tree_hybrid`, threshold 35,
`support_on_build_plate_only` 0, `raft_first_layer_expansion` 5, `support_top_z_distance` 0,
`support_interface_pattern` `rectilinear_interlaced`, `support_interface_spacing` 0, `tree_support_branch_diameter` 3,
`tree_support_branch_diameter_angle` 7, `support_interface_speed` 50 (array), plus `independent_support_layer_height`
0 and `thick_bridges` 0. PVA under PLA also needs bottom Z 0 and XY 0; TPU on PLA needs XY 0. The recipe
`dual-material-interface` now carries these values (speed array excepted). Bambu's mutual-support page: PLA interface
at 230 C, PETG HF interface at 265 C capped at 10 mm3/s, interface only (a full base in the other material risks
detaching), bed 60 C; PETG part with a PLA interface instead of full PLA support was 1 h 20 min shorter on a 5 h
print, mostly from fewer nozzle switches.

### 3.3 Key facts (names verified in PrintConfig.cpp)

| Key | Bambu preset (X2D 0.20 Standard) | Use |
|---|---|---|
| `support_threshold_angle` | 30 (from horizontal) | 35 with a support material; 45 for more support (`more-support`) |
| `support_type` | `tree(auto)` | `normal(auto)` for big flat undersides (better flat finish); `*(manual)` = enforcers only |
| `support_style` | `default` (organic) | `tree_hybrid` with a dissimilar interface; `snug` for normal that must not spread |
| `support_top_z_distance` | 0.2 | PETG on PETG 0.25 to 0.3 (`petg-supports`, community); 0 with a dissimilar interface |
| `support_object_xy_distance` | 0.35 | 0.5 for PETG (less side scarring) |
| `support_critical_regions_only` | 0 | 1 = only sharp tails and cantilevers (`minimal-supports`) |
| `max_bridge_length` | **0** | 10 so short bridges print free under tree support (`bridge-short-spans`); normal support uses `bridge_no_support` |
| `counterbore_hole_bridging` | `none` | `partiallybridge` or `sacrificiallayer` (`counterbore-bridging`) |
| `support_interface_top_layers` / `_bottom_layers` | 2 / 2 | 3 top for a smoother underside |
| `tree_support_branch_diameter` / `_angle` | 2 / 5 | 3 / 7 for tall trees or a support material |
| `wall_sequence` | `inner wall/outer wall` | keep for overhangs: outer-first leaves the outer wall hanging |
| `overhang_1_4_speed` ... `overhang_totally_speed` | 0 / 50 / 30 / 10 / 10 (arrays) | 0 in a band = no slowdown; bands are % of line width, not degrees |
| `overhang_fan_threshold` (filament) | PLA 50 %, PETG 10 % | PETG forces 90 % fan above 10 %; PLA 100 % fan above 50 % |
| `overhang_threshold_participating_cooling` | | the GUI lists `100%`, the parser accepts only `95%` |
| `independent_support_layer_height` | 1 | tooltip: invalid with a prime tower; Bambu sets 0 with a support filament |
| `thick_bridges` | 0 | 1 only on long spans; the GUI turns it off with a 0-gap dissimilar interface |
| `bridge_flow` | 1.0 | the wiki's one bridge test found 1.4 to 1.6 best at 30 to 40 mm/s (contested) |

Keys that do not exist in Bambu Studio (OrcaSlicer only; writing them does nothing): `make_overhang_printable*`,
`extra_perimeters_on_overhangs`, `overhang_reverse`, `internal_bridge_flow`, `bridge_density`, `thick_internal_bridges`,
`tree_support_tip_diameter`, `dont_support_bridges`, `wall_direction`, `only_one_wall_top` (Bambu's key for one wall
on top surfaces is `top_one_wall_type`: `not apply`, `all top`, `topmost`), `hole_to_polyhole`, `resonance_avoidance`. `tree_support_with_infill` is on Bambu's obsolete list. Teardrop holes, chamfers and
"make overhang printable" cones belong in CAD. One X post measured the same part at 45 min with tree supports in
Bambu Studio and 19 min with Orca's cone conversion and no supports: the geometry change, not the slicer, saved it.

### 3.3b `support_filament` 0 on a two-filament plate (live finding, 2026-10-13)

`support_filament` and `support_interface_filament` default to 0, "default": Studio prints the support with
whichever filament is loaded, to save a change. On a by-layer plate with a filament-1 link (tree supports, x 2..74)
and a filament-2 part, the exported G-code printed seven "Support" moves at z 0.4 with T1 (the right nozzle) at
x < 20.5, and the CLI refused the plate: exit -102, "Found G-code in unprintable area of multi-extruder printers".
The same link without supports, or with `support_filament` 1 and `support_interface_filament` 1, slices. shambu
pins both keys to the part's own filament whenever a supported part sits on a plate with more than one filament and
the spec leaves them at 0; `dual-material-interface` sets them explicitly and is left alone.

### 3.4 Painted supports without the GUI

An enforcer or blocker volume is a `<part subtype="support_enforcer|support_blocker">` in `model_settings.config`
(the spec's `subtype` field already supports both). Painted triangles carry `paint_supports="4"` (enforcer) or `"8"`
(blocker) per whole triangle; the codes were derived from `TriangleSelector.cpp`, not from a GUI-saved 3MF, so test
before relying on them. Manual support types (`tree(manual)`, `normal(manual)`) build support only on enforcers.

### 3.5 Support ironing

Since 2.5.0 `enable_support_ironing` (needs interface spacing 0, not grid, Developer mode) irons the interface. One
X post: the ironed side's PETG-under-PLA supports fell off on their own and the underside came out smooth.

### 3.6 Tree support spreads past the part's box (live finding, 2026-10-13)

The planner used to measure only the model footprint. A gripper bracket (95 x 170 x 90 in its CAD frame, laid on its
-y cheek, tree supports) next to a nut holder 5 mm away passed `check` and failed in Studio with exit -101 (G-code
path conflicts). In the bracket-alone G-code, Support extrusions below z 10 reach x 110.1 and y 107.4 against an outer
wall at x 96.8, y 91.8: 13 to 15.6 mm past the box. With `layout.gap` 15 it still conflicted; with gap 20 it sliced
(8 h 25 min, 231.7 g). On a two-filament SO-101 plate the jaw on filament 2 (bbox from x 25.7) put T1 support at x 15.0,
inside the right nozzle's dead strip, and Studio refused it with -102. Rule now: a part with supports gets a margin of
0.25 x its height, clamped to 5..25 mm (the bracket, 170 mm tall on its cheek face, gets the 25 mm cap), and the
extruder-reach check uses the rect with the margin (`layout.support_margin`).

---

## 4. Time

### 4.1 The by-object hack, measured

| Plate (X2D, PLA + PLA, 0.20 Standard) | by layer | by object |
|---|---|---|
| 4 x 20 mm cubes, 2 per filament (research agent) | 1 h 13 m, 21.5 g, 100 nozzle changes, prime tower | 48 m, 14.6 g, 1 change, no tower |
| bracket 60x40x30 + 2 x 20 mm cubes on filament 2 (this PR's live sweep) | 1 h 24 m, 27.4 g, 7.2 g tower, 100 changes | 40 m, 16.4 g, 0 g tower, 1 change |
| 6 x 10 mm cubes, 3 per filament | 36 m (26.5 m with the tower off) | 29.5 m |
| 6 x 10 mm cubes, one filament | 14.5 m | 27.8 m |
| gripper: 2 links (PETG, tree supports) + tag bridge (PLA, 210 x 3 x 96 standing as exported) | 3 h 38, 72.4 g, 61 changes, 6.2 g tower | 3 h 13, 66.2 g, 1 change |
| the same plate with the tag bridge laid flat (its designer's bed face) | **2 h 27, 62.0 g, 14 changes, 1.6 g tower** | (by object not re-run) |
| 4 x 10x10x15 mm cubes, 2 per filament, merged into 2 groups (notes-research A) | 42 m 16 s, 75 changes | **21 m 04 s merged**; 29 m 05 s as 4 separate objects; 1 change each |
| 2 boxes (20x20x10, 15x25x12) on filament 1 + 2 x 18x18x8 on filament 2 (`group_by_filament`, live) | not run | **23 m 30 s grouped** (1410 s); 26 m 32 s separate (1592 s); 1 change each |
| OpenArm: J7_joint_B + camera_mount (PLA, f1), shaft_A (PETG, f2) | 1 h 24, 20.6 g, 70 changes, 7.2 g tower | **59 m, 13.3 g, 1 change** |
| SO-101 wrist: wrist_roll_follower, wrist_roll_pitch, moving_jaw (PLA, one filament, 20 to 65 mm tall, tree supports) | **3 h 23, 84.5 g** | 3 h 24, 84.4 g (1 min slower) |
| SO-101: 3 x motor_holder_wrist (28 mm, one filament) | 1 h 20 | **1 h 19** |
| Gridfinity: 6 labels, text part on filament 2 | **38 m (2299 s), 8.5 g, 3 changes, 0.3 g tower** | 40 m (2404 s, +105 s), 8.1 g, 23 changes, no tower |
| Gridfinity: 3 bins a01, a17, a21 (one filament) | **2 h 09 (7786 s), 76.4 g** | 2 h 11 (7863 s, +77 s) |
| organic: duck + bottle funnel (f1) + spout funnel (f2) | 2 h 40, 49.8 g, 273 changes, 18.9 g tower | **1 h 13, 30.9 g, 1 change** |

Why it works: by layer, two single-colour object groups cost about one nozzle change per layer (0.2 mm x 20 mm =
100 changes) and a prime tower wipe after each. The slicer charges about 14.5 s per X2D nozzle change (a forum test:
11.7 s on the H2D, 76 s for an AMS change on one nozzle, 95 s on an X1C), but the tower is the larger cost. Why it
fails on tiny parts: by object each object meets the filament's minimum layer time alone (Bambu PLA
`slow_down_layer_time` 4 s), so six 10 mm cubes spend 20 min waiting. The gripper rows show the other lever: orientation beat print sequence there, because the standing tag bridge
forced a filament-2 layer on every one of 480 layers and the flat one on 15. Rule for the skill: slice both ways
(`shambu sweep --by-object`) and keep the faster one. Expect by object to win when each object is single-colour with
real layer area, and by layer to win for many tiny parts. By layer with `enable_prime_tower` 0 is faster still on
tiny parts but the idle nozzle then oozes with no wipe (Bambu built ramming and pre-cooling because of that).
Merged same-filament groups fix most of the tiny-part loss: the members of a group share each layer, so they share
the minimum layer time (outer wall 278 s merged vs 421 s separate on the four cubes; this cause is inferred, not
measured). Each group was timed once.

The real-part rows (one slice each) agree: by object won by about 1 h 27 min on the duck and funnels and by 25 min on
the OpenArm plate, where each filament holds whole objects; it lost 1 min to 105 s on single-filament or tiny
two-colour plates (SO-101 wrist, Gridfinity bins and labels). On six Gridfinity bins a recipe beat both: `fast-infill`
4 h 39 against 5 h 21 for the baseline (`wide-line-two-loop-walls` 4 h 44, `coarse-fast` 4 h 58); by object did not
fit one plate.

### 4.2 The right nozzle is slow

The X2D `0.20mm Standard` preset gives the right (Bowden) nozzle outer wall 50 mm/s and 1000 mm/s2 against 200 mm/s
and 10000 mm/s2 on the left. The same 80 x 80 x 40 box: 1 h 08 on the left, 2 h 09 on the right (+88 %); a 30 mm cube
19 vs 32 min. Put the filament with the most volume on filament 1 (left); accents, labels and support interfaces go
on filament 2. The wiki marks PETG on the right hotend "not recommended" and every TPU unavailable there.

### 4.3 Ranked time levers (two-filament X2D plates unless noted)

1. High-volume filament on the left nozzle (30 to 50 % of the moved volume's time).
2. One filament per nozzle: no flush at all (flush only happens within one nozzle).
3. By object for single-colour objects with real layer area (20 to 40 %, section 4.1).
4. Objects ordered so one filament finishes before the other (shambu does this: one change in total).
5. Colour boundaries horizontal (Z bands), not vertical: the wiki's example cut changes by about 80 % and time by
   70 % by laying a part flat; text on the top of a box added 12 min, the same text on a side 68 min (63-tip video).
6. Colour changes near the bottom: the tower stops at the last change (tower 14.8 g to 9.1 g in the wiki example).
7. Fewer layers: 0.28 mm cut colour changes by 27 % in the wiki's example; `coarse-fast`.
8. `0.20mm Standard`, not `High Quality` (HQ: outer wall 60, accel 4000, gyroid, a 60 mm tower): 19 vs 27 min on a
   30 mm cube. X2D has no Strength or Fast preset.
9. Infill pattern on an 80 x 80 x 40 box: lightning 0:47, adaptive cubic 0:59, grid 1:09, zig-zag 1:09, crosshatch
   1:34, gyroid 1:43, honeycomb 2:20. `fast-infill` = adaptive cubic + `infill_combination` (another 8 %).
10. 3 top shells instead of 5 on hidden tops (3 %). Arachne vs classic and `reduce_crossing_wall` changed nothing.
11. Fill the plate with copies: by layer the change count per layer does not grow with more same-colour objects.
12. Pause and swap by hand instead of a second filament for a single Z-band colour (`pause_at_z`).

---

## 5. Surface and fit

- X2D profile defaults differ from the code defaults: `wall_generator` classic, `top_surface_pattern` monotonicline,
  `elefant_foot_compensation` 0.15, `enable_arc_fitting` 1, `z_direction_outwall_speed_continuous` 1,
  `enable_prime_tower` 1. Keep them.
- Scope: `outer_wall_acceleration`, `print_in_clockwise`, `z_direction_outwall_speed_continuous`, `enable_arc_fitting`,
  `resolution`, `filament_shrink` and `wipe` are plate-global. Seam, wall order, compensation and wall generator are
  object keys. Ironing, top pattern and fuzzy skin are region keys (modifiers and height ranges too).
- Seams: `aligned` on cornered parts, `back` or painted on organic parts, scarf on cylinders. Bambu's X2D filament
  profiles set scarf to `none`; `override_filament_scarf_seam_setting` 1 + `seam_slope_type` external enables it
  from the process (`scarf-seam`). Keep `seam_slope_conditional` 1. Scarf reduces the seam, never removes it; users
  report it shows more on silk.
- `precise_outer_wall` is a Developer-mode key and Studio ignores it unless `wall_sequence` is inner/outer
  (`PerimeterGenerator.cpp`, a warning in `Print.cpp`). `precise-fit` sets both.
- `enable_circle_compensation` (2.0.0) only for PLA Basic, PLA CF, PET CF, PETG CF, PETG HF, full circles under
  50 mm in the XY plane; it forces 200 mm/s and a scarf seam on those circles and zeroes the manual XY compensation.
  The sign of `circle_compensation_manual_offset` is contested (wiki: positive looser; tooltip: positive tighter).
- Classic walls = one loop per layer = one seam = smoother walls; Arachne for small text and thin ribs. Fuzzy skin
  `extrusion` and `combined` modes need Arachne. `show-walls` = classic, 3 loops, inner-outer-inner.
- Iron only the face that matters (`iron-top` = `ironing_type` topmost); `top` irons every stair step of a slope.
- Pillowing: `top_shell_thickness` >= 1.0 mm and `internal_bridge_support_thickness` 0.8 before touching flow.
- `filament_shrink` is contested (wiki: outer XY only; tooltip: scales XY, which includes holes): cube test first.
- Developer-mode keys (Ctrl+P in the GUI) are ordinary keys in the 3MF; shambu can write them.
- Dual-nozzle by-layer prints: keep the prime tower (idle-nozzle ooze blobs knock towers over when it is gone).

---

## 6. Headless metrics (what the tool reads after `--slice`)

- `Metadata/slice_info.config` per plate: `prediction` (seconds, total including prepare), `weight` (all filaments),
  `support_used` (true/false only), `outside`, `filament_maps`, per used filament `used_m`, `used_g`, `group_id`
  (0 = left nozzle), `total_load_time`. No support grams, no change count.
- G-code header (`Metadata/plate_N.gcode`, between `; HEADER_BLOCK_START/END`): `; model printing time: ...; total
  estimated time: ...`, `; total layer number:` (summed over objects by object), `; total filament length [mm] :`,
  `; total filament volume [cm^3] :` (the label is wrong, the value is mm3), `; total filament weight [g] :` (includes
  flush and tower), `; filament_density:`, `; filament_diameter:`, `; max_z_height:`, `; filament: 1,2`. No tool-change
  or flush line.
- `result.json` (CLI only): `total_used_g - main_used_g` per filament = flush plus tower waste; `filament_change_times`
  counts flushes, not nozzle switches (0 for a by-object nozzle change).
- G-code body: `; FEATURE: <name>` tags every block (Outer wall, Inner wall, Sparse infill, Support, Support
  interface, Support transition, Prime tower, Bridge, ...), `; FLUSH_START` / `; FLUSH_END` bracket a flush,
  `; WIPE_TOWER_START/END` the tower, extrusion is relative (M83), a bare `T<n>` line (n < 100; `T255` and `T65535`
  are "no tool") is a filament change and the start G-code emits none (filament 1 is loaded implicitly). G2/G3 arcs
  extrude too (arc fitting is on). `threemf.Project.feature_usage()` sums signed E per feature and tool and converts
  with the header density and diameter: 12.38 g model against Studio's 12.39 g and 16.38 g = 12.39 + 3.99 g tower on
  the live plates. Summing only positive E over-counts by the deretractions (18.4 g instead of 16.4).
- A `--slice` of a two-filament plate takes about 1 s on this Mac; a five-variant sweep ran in 3.2 s.

---

## 7. Studio GUI workflow for the user (the hand-back text, checked against 2.8 source and wiki)

Three rules to explain: largest flat face down (adhesion, no warping, no supports); keep overhangs under about 45
deg from vertical and point steep features up (Bambu supports only what is within `support_threshold_angle` of
flat, default 30, so the slicer is more permissive than the rule); align loads with the X/Y lines and put detailed
faces on the sides, not on supports.

| Tool | Key (macOS) | What it does | Pitfalls |
|---|---|---|---|
| Lay on face | F | White planes appear on the model; click one to put that face on the bed. Modes: Convex hull (default) or Triangular facet | Convex-hull mode can leave a part slightly tilted; single object only |
| Rotate | R | Rings per axis plus numeric boxes. Snap: inner band 45 deg, fine ticks 5 deg | Numeric rotation is in world axes |
| Auto orient | Shift+R | Studio's cost function (section 1.2) | Ignores strength and time, mis-reads bridges, has flipped parts the wrong way (issue #12204) |
| Cut | C | Planar or dovetail, any angle; connectors Plug, Dowel, Snap with depth ratio and tolerance; then Keep orientation, Place on cut or Flip | Dovetail can leave non-manifold edges |
| Mirror | right-click, Mirror, along X / Y / Z | | |
| Show Overhang | View, Show Overhang (Cmd L) | Highlights overhang faces in Prepare | Bridges still show |
| Supports painting | L | Enforce or block per facet | L in Preview toggles single-layer view instead |
| Seam painting | P | | |
| Arrange | A, Shift+A (selected plate) | Spacing, Auto rotate, multi-material per plate; by object uses max(spacing, 60 mm) | Never changes Z orientation |
| Bottom view | Cmd 2 | The "look from below" check | |
| Plate settings | right-click the plate | Bed type, print sequence (by layer / by object), spiral vase, first-layer filament order | |

Checking an orientation in Preview (Slice Plate, top right):

1. The colour-scheme dropdown has Summary, Line Type, Layer Height, Line Width, Speed, Fan Speed, AUX Fan Speed,
   Temperature, Flow, Tool, Filament and Layer Time. There is no "Overhang speed" scheme in 2.8: use **Line Type**
   and read the Overhang wall, Bridge, Support and Support interface rows (time, per cent, filament per role), or
   **Speed** to see where the printer slows.
2. The filament table under the legend splits each filament into Model, Support, Flushed, Tower and Total: the direct
   number for what an orientation costs in support.
3. Toggle Supports in the left Process panel and re-slice to see where Studio builds support; or paint enforcers
   and blockers with L.
4. Big red patches hanging in the air in Line Type (Overhang wall) mean: back to Prepare, F, try another face.

Headless, the same loop is `shambu orient part.stl` then `shambu sweep plate.json --orientations part --by-object`.

---

## 8. ClassCAD (classcad.ch) as a mesh engine: evaluated, not adopted

ClassCAD (awv informatik, the kernel behind buerli.io) is a B-rep (NURBS) kernel. The MCP `load` on engine 21.2.0
refused a 12-facet STL with error 1013 "Possible values are: [OFB, STP, IWP]"; `part.importFeature` with format
STL reported success and created 0 solids. STL is export only (the site: imported "as a mesh, for display"). On
STEP it can rotate, slice at a plane, subtract dowel holes, report volume and centroid and export STL; it has no
bed-contact or overhang area. Access is JS/TS in a browser or Node WASM engine, a native worker on port 9094, or
the MCP (`npx @classcad/mcp`, 0.2.2 installed, 0.4.0 on npm a week later); the Python client is marked
experimental. It needs a free account once per machine plus an engine key, then runs offline for 14 days. The Free
plan is non-commercial and exports STL and GLB only; STEP export needs Solo or higher; prices are not published on
the plans page. Verdict: no STL import, Node plus sign-in, a non-commercial free plan and a fast-moving API make it
unsuitable as a shambu dependency. Rotation, metrics and body splitting are 300 lines of stdlib Python (`mesh.py`).
Cut-and-glue stays a GUI step (Studio's Cut tool with dowels) or, later, an optional `trimesh` + `manifold3d`
extra; a STEP-only ClassCAD path (Claude runs the ClassCAD skill before shambu) is possible for CAD-sourced parts.
Not verified: whether a native `classcad-cli` worker build imports STL as a solid, and the commercial prices.

---

## 9. Corrections to older references

- Reference 07 lines 41 and 74 said a prime tower is not allowed with by object and two extruders. The check is
  compiled out and Studio drops the tower itself (section 2). Both lines are corrected.
- Reference 07 said the support gap must be a multiple of the layer height. No error enforces it; with
  `independent_support_layer_height` 0 the slicer rounds the gap (`Slicing.cpp:120-124`).
- The shipped SKILL.md said "the tool never rotates parts". It now does, through an oriented STL copy.
- `shambu check` reported "every reach and height limit holds" on a by-object plate the slicer then rejected with
  exit -63; `SequentialClearance` closes that gap.
- Copies of a part (`count` > 1) were placed at the first copy's position plus their own: Studio clones the
  translated first copy before translating the others (reference 01 section 2.4). Found with the gripper link
  (STL at z -38): copy 2 floated at z 38 and sat off the bed. Fixed in `build.py`.
- Real-part findings in one place: the copy bug, the support-filament pin (3.3b), zero-contact orientation
  candidates (1.2), meshes in metres (`shambu rotate --scale`). The manual loop lives in `data/README.md`.
- Support spread: tree support reached 15.6 mm past a gripper bracket's box (Studio -101 at gap 15, sliced at 20).
  The layout now keeps 0.25 x height, 5..25 mm, round a supported part (3.6).
- Metres: `check` passed an OpenArm J7_joint_B scaled to metres (0.0 x 0.0 x 0.0 mm, OK) and Studio failed with -100.
  A mesh under 1 mm now fails `check`; the SO-101 URDF meshes are the real case.
- Export failure: on the gripper p5-bracket plate (-101), OpenArm G3 (-63) and SO-101 plate-arms the report said
  "already sliced" and pointed at a missing `.gcode.3mf`. It now says "export failed" with Studio's error.
- T lines: Gridfinity labels with a height range on filament 2 reported 0 filament changes; the start G-code's
  indented `T1 H-1` was skipped. The first T line now sets the loaded filament.
- Threshold: `orient` used 45 deg from vertical and counted Gridfinity's exact-45 foot and lip by float noise. The
  default is now 60, Studio's own 30 from horizontal, and an exact-threshold facet is printable.
- Slenderness: a 6 mm guide rod (organic gripper set) ranked standing 204 mm tall with 27.6 mm2 of contact. The
  score now adds 50 per unit of height / sqrt(contact) above 3 (1.2).
- Cavities: `split` wrote an SO-101 moving_jaw's inward shell (signed volume -76.5 mm3) as a second body. Inward
  shells are now dropped and counted as cavities.

---

## 10. Sources

Studio source, tag v02.08.02.61 (https://github.com/bambulab/BambuStudio): `src/libslic3r/Print.cpp` (validate,
sequential_print_clearance_valid, has_wipe_tower), `PrintConfig.cpp` / `.hpp` (keys, enums, normalize_fdm_2,
is_sequential_print), `Arrange.cpp`, `Orient.cpp` / `Orient.hpp`, `GCode.cpp`, `GCodeProcessor.cpp`,
`Format/bbs_3mf.cpp`, `PerimeterGenerator.cpp`, `src/slic3r/GUI/Tab.cpp`, `ConfigManipulation.cpp`,
`KBShortcutsDialog.cpp`, `GCodeRenderer/BaseRenderer.cpp`, `TriangleSelector.cpp`, `src/BambuStudio.cpp`.
Local profiles: `/Applications/BambuStudio.app/Contents/Resources/profiles/BBL/` (machine, process, filament,
`filament/support_recommended_params.json`).

Bambu wiki (wiki.bambulab.com/en/...): software/bambu-studio/auto-orientation, cut-tool, seam, support,
support-painting, slow-down-for-overhang, auto-cooling, parameter/bridge, parameter/prime-tower,
parameter/quality-advance-settings, sequent-print, manual/dual-nozzles-slicing-filament-grouping,
reduce-wasting-during-filament-change, wall-generator, adaptive-layer-height, brim-ears, xy-hole-contour-compensation,
manual/auto-circle-contour-compensation, 3d-scene-operations, auto-arranging, plates_management, release notes 1.7
to 2.8.2; filament-acc/filament/h2d-pla-and-petg-mutual-support, filament-acc/filament/print-quality/overhang,
filament-acc/acc/plates, knowledge-sharing/3d-prints-shrinkage.

Others: https://cnckitchen.com/blog/stop-printing-flat-the-45-secret-for-stronger-parts,
https://help.prusa3d.com/en/article/modeling-with-3d-printing-in-mind_164135,
https://omni3d.com/how-to-calculate-maximum-overhang-angle, https://hackaday.com/tag/sacrificial-bridge,
https://forum.bambulab.com/t/3-ways-to-fix-filament-sagging-in-supportless-holes/192129,
https://forum.bambulab.com/t/tall-prints-a-community-guide/50321,
https://forum.bambulab.com/t/problems-with-supports-on-petg/30267,
https://forum.bambulab.com/t/petg-support-sticks-to-much-advice-on-settings/12033,
https://forum.bambulab.com/t/scarf-joints-doesnt-seam-to-always-work/157900,
https://forum.bambulab.com/t/outer-wall-acceleration/192135,
https://forum.bambulab.com/t/feature-request-iron-lowest-surface-base/121295,
https://forum.bambulab.com/t/h2d-dual-nozzle-reduce-or-remove-prime-tower-with-similar-filaments/162234,
Bambu forum threads 157310 (nozzle change timing), 131032 (by-object height myth), 99336 (clearance editing),
https://printago.io/slicer-cli/orient, https://github.com/bambulab/BambuStudio/issues/12204, /6328, /9104,
https://www.orcaslicer.com/wiki (precision, overhang, ironing, fuzzy skin pages, for the shared key semantics),
X posts by @thetechbff (overhang angles, bridge spans, PETG interface, support ironing, orientation and strength,
Orca cone conversion; animations from real toolpaths, n=1 each), https://classcad.ch, https://buerli.io,
https://www.npmjs.com/package/@classcad/mcp.

---

## 11. Layout checker: support spread, the pair rule, footprints and the packer (plate-14 post-mortem, 2026-10-21)

Section 3.6's rule (`0.25 x height`, 5..25 mm, all four sides, summed between two parts, held off the bed edge) refused
a five-part plate that printed (18 h 0 min, 480.7 g). The rule is replaced. Code: `layout.support_spread`,
`layout.pair_check` / `Clearance`, `InsideBed`, `ExtruderReach`, `mesh.silhouette`, `layout.RasterFootprint`,
`layout.BottomLeftPacker`, `probe.py`.

### 11.1 What Studio enforces by layer (source, tag v02.08.02.61)

| Fact | Where |
|---|---|
| No object-to-object clearance before slicing: `validate()` checks hulls against the exclude area only; the tower is a warning | `Print.cpp:944-1050`, `1036-1039`, `1353-1359` |
| Exit -101: centrelines of two objects cross in one layer (1 mm buckets, > 0.01 mm); no clearance distance; support-on-support ignored | `GCode/ConflictChecker.cpp:16-17, 203-221, 289-314` |
| Brims and skirts are not in the conflict test; brims of different objects are clipped against each other | `ConflictChecker.cpp:163-200`, `Brim.cpp:258-365` |
| `--no-check` switches the conflict test off; the log names the two objects (CLI names), no z | `Print.cpp:2418-2435`, `BambuStudio.cpp:6971-6972` |
| Tree support grows per object, against its own outlines and the bed border only: it does not avoid a neighbour and never leaves the bed | `Support/TreeSupport3D.cpp:5232-5246`, `TreeModelVolumes.cpp:128-133`, `TreeSupport.cpp:607-629, 2507-2522` |
| Tree first-layer brim (auto) is 1..10 mm from the branch's second moment and height | `TreeSupport.cpp:2336, 2476-2484`, `ExPolygon.cpp:416-430` |
| Studio's own arrange keeps 24 mm between objects when any part has tree supports (12 per item), and exits -21 when they do not fit | `ModelArrange.cpp:158-177`, `Arrange.cpp:84-136`, `BambuStudio.cpp:5477-5485` |

### 11.2 Measured spread (G-code of the printed plate, centrelines past the part's own model extrusions)

| Part | Height | -x | +x | -y | +y |
|---|---|---|---|---|---|
| housing (on its side) | 218 | **20.9** (z 0..5; 14.5 at z 20..25) | 0.3 | 0.3 | 2.9 |
| camera mount | 32 | inside | inside | inside | inside |
| camera shell | 48 | 2.5 | inside | inside | 2.9 |
| feeler | 64 | 0.2 | 0.6 | 4.7 | 3.4 |
| cradle | 29 | 7.1 | 7.1 | inside | inside |

With section 3.6's bracket (170 mm tall, 15.6 mm) this gives the estimate: tree `min(22, max(8, 0.10 x height))`
(20.9 / 218 = 0.096, 15.6 / 170 = 0.092; the cap is 20.9 plus one line), normal support 5 mm (11.4). The mesh does
not tell the side: the housing's -y and +y overhangs had 0.3 and 2.9 mm, its -x overhang 20.9 mm. So the estimate
goes on all four sides, and `--probe-supports` measures per side (11.6).

Footprints on the printed plate: the feeler's wide part exists only in two end blocks, and the cradle sits between
them. Box gap -3.7 mm (overlap), convex-hull gap 0.0, real gap 4.95 mm (brim to support), model to model about
19.8 mm. Housing / shell: box gap 0.8 mm (the old "0.6 mm" refusal), hull gap 21.1 mm.

### 11.3 The rules now

- Pair (`Clearance`): footprint gap g (silhouette) and its direction. g < 1.0 mm (two lines) is a FAIL body ("footprints
  touch" or "intersect by w x d mm"). Otherwise the need is max(a's spread toward b, b's spread toward a) + 1.0: the max,
  not the sum, because support-on-support crossings do not fail Studio. Short against an estimate: WARN "the slice
  decides". Short against a probe or a spec value: FAIL margin. Brims are not in the pair rule.
- Bed (`InsideBed`): body, brim and normal-support spread inside the bed; tree spread is not counted (clipped).
- Reach (`ExtruderReach`): only the sides where the extruder's reach is narrower than the bed (the right nozzle's
  x < 20.5, `aux_x_limit`); there brim and the full spread count, trees too (section 3.3b, exit -102). No reach line
  when the reach is the whole bed: it repeated the bed line on every spec of the post-mortem.
- Spec: `brim_margin` replaces brim and support on every side (FAIL-level, counted at the bed edge); `support_spread`
  `[mx, px, my, py]` replaces the support part per side.

### 11.4 Live clash test (30 mm cube next to an L with a 15 mm cantilever at z 20..30, no brim)

| Support on the L | gap 10 | gap 2 | gap 0 | gap -2 |
|---|---|---|---|---|
| none | 0 | 0 | 0 | -101 |
| tree(auto) | 0 | 0 | -101 | -101 |
| normal(auto) | 0 | -101 | -101 | -101 |

Normal support spread 4.4 mm at z 0..5 and 2.5 mm above; tree 1.5 mm. The tree support box was identical at gap 10 and
gap 2: it ignores the neighbour. Two unsupported models may touch (walls 0.42 mm apart sliced). `--no-check` on the
tree gap-0 project exits 0 with crossing paths: a `--no-check` slice is not a clash test, so `build --slice` keeps the
check, and on exit -101 the report names the two spec keys from the log line.

### 11.5 Packer

The five parts (124.2 x 135 x 218, 40.6 x 58.8 x 32.1, 58.6 x 60.7 x 47.8, 50 x 226.4 x 64, 22.1 x 99 x 28.9): the row
packer needs 2 plates even at zero margin (the 226 mm feeler opens a row, the bed above the housing stays empty).
`BottomLeftPacker` (candidate points at placed parts' edges plus spread + 1 + gap, lowest top edge then lowest right
edge, first fit, 0 degrees only) fits them on 1 plate with the estimates and with the measured spreads (unit tests).
Studio's arrange on the same five parts exits -21 with and without rotations. The row packer stays for print-by-object
plates and filament groups. A part that fits nowhere is reported once ("PLACEMENT NOT DONE", with the arithmetic) and
is never checked at a position nobody chose.

### 11.6 Support probe (`check|build --probe-supports`)

A solo `--slice` of each supported single-file part in its final orientation, `--no-check`, no prime tower, cached as
`<outdir>/.probe/<mesh sha256>-<orientation>-<support keys hash>.json`. A one-object plate has no object labels in
its G-code (live): the parser then reads every move after the first `; CHANGE_LAYER`, without `Custom` moves. Live on
the plate-14 spec with the PLA-interface process (`raft_first_layer_expansion` 5, `tree_hybrid`; measured once, so not
the printed file's settings): housing -x 13.8, +x 4.4, -y 0.9, +y 1.3; mount +x 4.6; shell -x 10.6, +y 8.1; feeler 7.0 /
7.0 / 5.0 / 5.0; cradle -x 9.1, +x 7.7, -y 2.1. Five probes plus the check took 12.5 s; the cached rerun 0.4 s.

### 11.7 Silhouette cost (Python 3.14, Apple silicon, 0.5 mm cell)

Housing STL, 11,084 facets: 0.015 s. Feeler, 1,660 facets: 0.016 s. Cradle, 2,508 facets: 0.006 s. The 0.5 mm cell
stays; a 1.0 mm cell saves nothing that matters. The raster is computed after the orientation is baked into the STL,
never rotated, so a part turned 45 degrees keeps its real outline (a 50 x 226 bar is 11,300 mm2, not 195 x 195).

### 11.8 Open

- Normal support at the bed edge: no bed clip found in `SupportMaterial.cpp` / `SupportCommon.cpp`, not tested live;
  the bed rule counts it.
- Fade with z: every neighbour on plate 14 stands on the bed, so the z 0..5 band decides. An overhanging neighbour
  (the feeler's end blocks at z 36..64 over the cradle) is where a z-aware check would matter.
- Collinear support overlap still conflicts (`ConflictChecker.cpp:295-296`); not reproduced.

### 11.9 Review corrections (Codex review against the source, 2026-10-21)

- The 1.0 mm pair allowance (`layout.LINE`) is shambu's policy, not a Studio limit. Studio refuses lines of two objects
  that overlap collinearly by more than 0.01 mm, whatever their roles, or cross more than 0.01 mm from an endpoint;
  only support-on-support crossings get a 100 mm threshold (`ConflictChecker.cpp:291-310`).
- The conflict check reads the first instance of each PrintObject (`ConflictChecker.cpp:226, 244-245`), and Studio
  groups a model object's instances that differ only by an XY shift into one PrintObject (`PrintApply.cpp:140-160`).
  shambu's `count` copies are separate model objects: a two-copy export holds two `<object>` entries and two build
  items, each with `instance_id` 0 (live, 2026-10-21). So every copy is clash-tested, and the -101 log names copy 2 by
  its own CLI name (`<stem>_2`). GUI instances (copies made in Studio) are not: there only the first copy is tested.
- "Has support" is `enable_support || enforce_support_layers > 0 || raft_layers > 0` (`Print.hpp:492-494`):
  `layout.needs_support`. A raft-only part gets a 5 mm spread (or `raft_first_layer_expansion`) and its support
  filaments are pinned on a two-filament plate.
- A brim that leaves the bed or a nozzle's reach is clipped (`Brim.cpp:1011-1045, 1079-1095, 1129-1138`): WARN brim.
  A model or support that crosses stays a FAIL. The packer keeps the whole brim on the bed when it can.
- Four spreads cannot see support inside a notch. A probe now keeps the support centreline cells (0.5 mm) in the
  part's footprint frame, and the pair rule tests that mask against the other part's model. The probe slices at the
  bed centre (Studio clips trees at the border, `TreeSupport.cpp:626-629`), in `<outdir>/.probe/<cache key>/` with its
  own log. The cache key holds the Studio version. Live rerun on the plate-14 spec: same spreads as 11.6, no WARN and
  no FAIL with the masks, 14.7 s for five fresh probes.
- `--keep-going` never exports a plate with an unplaced part, as it never exports a placeholder filament.
- Fixed parts (`at`) and filament groups keep their outlines (the group is the union of its members' outlines).
