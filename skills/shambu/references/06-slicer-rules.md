# 06. Slicer rules from experience: the reasons behind the recipes

The named recipes (`shambu recipes`) carry the settings; this file carries the reasons and the numbers.
Sources: "gripper build" = a functional PETG gripper build on an X2D (2026-10); "63-tip video" = a 16-year
printer and designer's tips video (2026-10-12); "notes" = the author's own print notes (not shipped; a
summary is in `09-user-notes.md`); "research 08" = `08-optimisation.md`.

## Contents

1. Process presets and strength
2. Layer height and line width
3. First layer and adhesion
4. Supports
5. Different-material support interface
6. Colour, text and time
7. Filament and Studio setup
8. Machine limits the tool checks (X2D)
9. Out of scope for the tool
10. Prime tower on a two-filament plate

## 1. Process presets and strength

- `petg-functional` (gripper build): 0.2 mm layers, 6 walls, 5 top and 5 bottom shells, 40 % gyroid, elephant
  foot 0.15, no brim by default. `solid-part` (100 % infill) only on parts sized as solid in the stress maths:
  gear, pinion, racks, rails. "100 % on everything" was wrong and cost mass.
- `pla-light` (63-tip video): 3 walls, 15 % gyroid. `pla-decorative`: 2 walls, 5 %.
- Strength comes from walls, the 45-degree print, a stronger filament and fillets at every section change, not
  from infill: add wall loops before raising infill (gripper build, 63-tip video, notes). A hook breaks at its
  sharp root: a CAD fix.
- Local strength: a `modifier_part` sub-part raises infill only around bolt holes (63-tip video); it needs a
  modifier mesh from the CAD step.
- `no-hole-compensation`: clearance lives in the CAD and is qualified by coupons. Slicer compensation applies one
  offset to every hole, the measured error differs by orientation and bore length (vertical about 0.1 mm under,
  bridged horizontal 0.2 to 0.3 mm, more on long bores), and it cannot reach horizontal bores at all (notes,
  gripper build). `precise-fit` keeps the outer wall at its nominal width for fit-critical parts.

## 2. Layer height and line width

- Layer height is the biggest time lever (63-tip video): a straight-sided box 40 min coarse vs 3.5 h fine,
  nearly identical. Curved tops show the steps: variable layer height in the GUI (no process key exists; the GUI
  writes `Metadata/layer_heights_profile.txt`). `coarse-fast` = 0.28 mm.
- 0.16 mm only where a curved or visible surface needs it; on flat solid surfaces it looks worse and costs time
  (notes). Prefer `layer_height` 0.16 on the Standard base over the `High Quality` preset, which also slows the
  walls and widens the tower (19 vs 27 min on a 30 mm cube, research 08).
- `wide-line-two-loop-walls`: 0.75 mm line width prints a 1.5 mm wall in two loops, box 40 to 32 min (63-tip
  video). Top-surface line width is a separate key (`top_surface_line_width`, 0.42 on the X2D).
- `shim-0.1mm`: 0.5 and 1.0 mm spring shims alone at 0.1 mm layers (gripper build). Alone, because with a prime
  tower every object on a plate must share one layer height.

## 3. First layer and adhesion

- Elephant foot 0.15 is in the preset. Parts should also carry a 0.4 x 0.6 mm relief chamfer on every edge that
  mates on the bed side (gripper build). If they do not, say so: that is a CAD fix.
- `tall-outer-brim` (outer only, 5 mm) on anything tall, or long and narrow: blades on end, 216 mm rails, 112 mm
  jaws, carrier, bracket (gripper build). `flat-no-brim` on flat coupons and small parts; the default is
  `auto_brim`, so say it explicitly.
- A big flat plate (210 x 96 x 3): flat, no brim, unless a corner lifts. Then `dog-ears`: the user places the ear
  points with the Brim Ears gizmo in Studio (gripper build, 63-tip video).

## 4. Supports

- No supports on anything that lies flat (gripper build). Set supports per object (`tree-supports`,
  `snug-normal-supports`, `no-supports`), never for the whole plate.
- Tree supports by default; normal snug supports only for a flat area supported straight up from the plate
  (63-tip video). A few spots only: the user paints enforcers and blockers in Studio.
- Printers bridge further than expected, PLA best (10 to 40 mm clean on a measured plate). The Bambu preset puts
  tree support under every bridge (`max_bridge_length` 0): `bridge-short-spans` frees spans up to 10 mm.
  `minimal-supports` keeps only sharp tails and cantilevers; `counterbore-bridging` closes screw counterbores
  with one sacrificial layer; `petg-supports` widens the gaps so PETG-on-PETG comes off; `more-support` raises
  Bambu's threshold (it counts from horizontal: default 30 = support only under faces within 30 deg of flat).
- Thin bridged ceilings in the first layers are the weak spot: inspect them before fitting (notes).

## 5. Different-material support interface (`dual-material-interface`, notes, research 08 section 3.2)

- PETG part: PETG tree supports, PLA interface. PLA part: PLA supports, PETG interface. They barely bond.
- Part material on filament 1 (left nozzle), interface material on filament 2 (right nozzle), map 1,2. Supported
  features must sit at x >= 20.5 (the right nozzle's reach). Expect a prime tower and a slower print. The bed
  follows the hotter filament (PETG 70 C under a PLA part). The recipe carries Bambu's own X2D values.
- Process-level support filaments (2026-10-21). The recipe sets `support_interface_filament 2` in the process, not
  on a part. So every part with `enable_support` on prints with filament 2 as well, and the plate counts as two
  filaments. `effective_filaments` (`build.py`) counts the process support, support-interface, wall and infill
  filament keys. Before that, shambu's seed build (camera mount + motor cradle) placed the cradle at x 9.2, reported
  "every reach limit holds", and Studio exited -102: the right nozzle printed interface at x 6.96..88.46 (585
  extrusions under x 20.5, the first at z 1.8, x 14.3). Studio makes the same mistake in its own count
  (`construct_assemble_list: Plate 1, used filaments 1`, BambuStudio.cpp:1338-1343).
- The interface spread: the interface prints past the part box, 2.2 mm on the cradle and -0.2 mm on the housing
  (measured once each). shambu takes the interface spread as the brim margin plus 3 mm (`layout.INTERFACE_SPREAD`)
  on all four sides, for a slot that prints only the support interface. `ExtruderReach` (`layout.py`), the one reach
  rule, holds that slot's extruder to it, and the packer keeps it inside that extruder's reach: one FAIL line per
  part and extruder. A slot the part also prints with (support, walls) gets the full support spread. "Has support"
  is Studio's own predicate (`layout.needs_support`: support, enforced layers or a raft).

## 6. Colour, text and time

- Colour position drives time (63-tip video): text on top of a box added 12 min (one swap plus purge tower). The
  same text on a side added 68 min (a swap every layer).
- Split the model so small parts keep one colour; assign the other colour to one back part.
- Cleanest small text: print face down as the background, letters as gaps; first layer one colour, second the
  other. `two-colour-first-layers` does the first three layers on filament 2.
- No second filament loaded: `pause_at_z`, the user swaps by hand and resumes (63-tip video).
- Several objects in two filaments: print by object (research 08 section 2 and 4). Group same-filament objects
  next to each other so one nozzle change serves the plate.

## 7. Filament and Studio setup

- Pick the real filament's profile, not Generic, when one exists (`presets list filament`). Same-brand profiles
  are tuned (63-tip video).
- Everything except PLA must be dried before printing. The report says so for PETG and TPU.
- TPU 95A prints only through the main (left) nozzle. The checker enforces it.
- "Split to objects" in Studio (or `shambu split`) separates an STL with several bodies. A part bigger than the
  bed: Cut tool with connectors in Studio, or split in CAD.

## 8. Machine limits the tool checks (X2D)

- Bed 256 x 256, 261 tall. Check footprints, not counts: two 200 g parts do not share the bed (227 + 95).
- Right (auxiliary, Bowden) nozzle = filament 2 by default: reaches x 20.5 to 256 only and z up to 256.
  Anything on filament 2, including a height range or a sub-part, must sit inside that. The user's note said
  "inside 235.5 mm in X": the width matches (256 - 20.5), the dead strip is on the left edge, not the right.
  Pass `aux_x_limit: 235.5` to apply the user's reading as well.
- The right nozzle runs the outer wall at 50 mm/s against 200 on the left: the filament with the most volume
  goes on filament 1 (an 80 mm box: 1 h 08 left, 2 h 09 right; research 08 section 4.2).
- X2D speed and acceleration keys are six-slot per-nozzle arrays; a scalar wipes the other five values
  (research 08 section 3.1).
- External spool: upper buffer inlet feeds the left (main) hotend, lower inlet feeds the right.
- USB export: File, Export, Export Plate Sliced File gives `.gcode.3mf`; a known bug doubles the extension.
  Cloud send needs the same account in Studio and on the printer.
- Time: show the Studio estimate (`--slice` gives it). Show a measured rate or a calibrated time only from `shambu calibrate` data, with n.

## 9. Out of scope for the tool

Fillets, teardrop holes, nut slots, inserts, design advice (the CAD step) and polishing (the user's hands). The
skill still names them in the hand-back when they decide the result: a bed-side fillet that should be a
chamfer, a bore that wants a coupon, a show face that wants matte filament.

## 10. Prime tower on a two-filament plate

Source: Bambu Studio v02.08.02.61 and the plate-14 runs of 2026-10-21 (X2D, PETG part, PLA matte interface).
`tower.py` ports the estimate; `build.py` keeps packed parts off a provisional footprint (a back-right corner
rect, grown by 3 mm), picks the spot after packing and writes it per plate.

Studio sizes the tower from the tallest object on the plate, not from the interface volume
(`PartPlate::estimate_wipe_tower_size`, PartPlate.cpp:2191-2265; WipeTower.cpp:1587-1630):

| Input | Rule |
| --- | --- |
| Minimum depth by height H | 5 mm at 5, 20 at 100, 40 at 250, 60 at 350, linear in between (35.73 mm at H 218) |
| Volume | `filament_prime_volume` (45) x filaments + one `filament_change_length` (10 mm of 1.75) per pair on two extruders: 114 mm3 |
| Rib wall on (default) | a square of side `rib_width/sqrt(2) + max(min_depth + extra_rib_length, sqrt(volume / layer_height x infill_gap))` |
| Rib wall off | `prime_tower_width` x `max(min_depth, volume / (layer_height x width) x infill_gap)` |
| Brim | `prime_tower_brim_width`; -1 (auto) = 8 mm for H >= 100, else H x 0.08 |
| Footprint | `x - brim .. x + w + brim`, `y - brim .. y + d + brim` (BambuStudio.cpp:4462-4488) |

| Tallest part | Rib wall | Size | With brim |
| --- | --- | --- | --- |
| 32 mm (seed) | on, auto brim 2.6 | 34.9 x 34.9 | 40.0 x 40.0 |
| 218 mm (plate 14) | on, auto brim 8 | 41.39 x 41.39 (Studio's log: width 41.3902, depth 41.3902) | 57.4 x 57.4 |
| 218 mm | off, width 22, brim 2 | 22 x 38.88 | 26 x 42.9 |

Measured on plate 14 (n=1): the tower at (208, 208) with the rib wall overlapped the feeler and left the bed
(`wipe bbox: min {200, 200} - max {257.39, 257.39}`), exit -101 "gcode path conflicts found between WipeTower and
feeler_module_print". A 22 mm tower without the rib wall, brim 2, at (172, 150) sliced: exit 0, 19.25 h, 586.5 g +
17.4 g, prime tower 185 min. The same layout with no tower: 18.8 h, 524.6 g + 8.5 g. So the tower adds 27 min and
71 g here. With no tower the idle nozzle is not wiped before the interface layers.

What shambu does:
- `wipe_tower` per plate: `"auto"` (the default when a plate prints two or more filaments by layer), a spot
  `{"x", "y", "width", "rib_wall", "brim"}`, or `false` (writes `enable_prime_tower 0`, with a WARN line).
- Width, rib wall and brim are process-global in Studio: two plates that ask for different sizes are a BuildError
  ("prime tower size is per project"). The chosen values go into the process JSON.
- `wipe_tower_x/y` are per-plate vectors: each plate gets its own spot, a plate without a tower keeps (208, 208).
- Auto placement: a 1 mm grid, the footprint inside the area extruders 1 and 2 (left and right nozzle) reach
  (x 20.5..256 on the X2D, and `aux_x_limit`) and the bed, clear of every part (`tower.FootprintClearance`, next
  item). Score: the clearance (capped at 10 mm),
  then high y (Studio's own default sits at y 250, PartPlate.cpp:69-70), then high x. If nothing fits, it tries the
  rib wall off at widths 35, 30, 25, 22 and 20 with a 2 mm brim. Then a FAIL names the best spot, the part that
  blocks it, and offers `"wipe_tower": false`. A size set in `process.set` is kept (no fallback).
- The model is the part silhouette, as in the pair rule (`layout.pair_check`): the tower rect keeps the 1 mm line
  allowance from each part's outline swept by its support spread per side, or from its probe mask. Brims do not
  count. A part known only by its box keeps the old test: its rect with brim and spread, plus 3 mm. Every size is
  tried with the estimated spreads first; when none fits, the outlines and the measured or spec spreads alone
  decide, and a tower inside an estimated spread gets a WARN line ("the slice decides"). A fixed `wipe_tower` spot
  gets the same: a FAIL only from an outline or a measured or spec spread, a WARN from an estimate.
- Why: by boxes, the 26.6 mm strip between the housing (x <= 168.5) and the feeler box (x >= 195.1) is too narrow
  for the T8 tower with 3 mm clearance, yet T8 sliced: the feeler is a 10 mm beam there (x 215..225), and the
  G-code tower reached x 195.8. T8 also sat 2.4 mm from the cradle box: inside its estimated 8 mm spread (measured:
  0 mm on +y) and, by the box, inside its 5.2 mm brim. Towers at (161, 226), (167, 216) and (167, 205) conflicted with the
  feeler or the mount although they were clear of the boxes: their support spread past the box.
- On the plate-14 parts the silhouettes also free the front: the housing's box starts at y 25.9, its outline right of
  x 95 starts at y 63.4. Auto then takes a 35 mm tower without rib wall at (132, 2) (estimates) or (128, 2) (probe),
  clear of every spread. Sliced with the probe (2026-10-21, n=1 per spot): (128, 2) exit 0, 24 h 13 min, 635.1 g;
  T8 at (172, 150) exit 0, 22 h 30 min, 603.1 g. The wider tower costs 1 h 43 min and 32 g here. One of four slices
  at (128, 2) crashed (-139, in tree support) and the three re-runs passed.
- One size for the project: the fallback runs over every plate together, and the first size every "auto" plate
  accepts wins (a narrower tower is deeper, so each step recomputes the footprint per plate). A fixed `wipe_tower`
  pins the size. If one plate blocks every size, that plate FAILs and the message names it.
- The estimate is the GUI's. Studio recomputes the real tower at slice time from the actual tool changes
  (WipeTower.cpp:4520-4585), so the sliced tower can be a little different.
- Smooth timelapse (`timelapse_type 1`) needs a tower with one filament too (PartPlate.cpp:2222-2223): shambu plans an
  "auto" tower on such a plate.
- "Has support" is Studio's own predicate, `layout.needs_support`: `enable_support`, `enforce_support_layers > 0`
  or `raft_layers > 0` (Print.hpp:492-494). A raft-only part prints its raft interface with the process interface
  filament too.
- The obstacle test of the placement is a parameter (`tower.Clearance`): `FootprintClearance` when the parts have
  outlines, `BoxClearance` when none has.
- Studio's own "Prime Tower is too close to others" is a warning, not a headless exit (Print.cpp:1003-1045). The
  binding check is the post-slice conflict test (-101).

