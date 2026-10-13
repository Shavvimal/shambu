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
- Time: the slicer estimate wins (`--slice` gives it). Real plates ran 86 g in 3 h 11 min and 242 g in 9 h 31.

## 9. Out of scope for the tool

Fillets, teardrop holes, nut slots, inserts, design advice (the CAD step) and polishing (the user's hands). The
skill still names them in the hand-back when they decide the result: a bed-side fillet that should be a
chamfer, a bore that wants a coupon, a show face that wants matte filament.
