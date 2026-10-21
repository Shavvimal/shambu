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

## 10. Studio's time estimate versus the printer

Source: research C (2026-10-21), from two watched X2D prints, Studio's `GCodeProcessor.cpp` (v02.08.02.61) and five
outside reports. This section replaces the "slicer estimate wins" line in section 8. Show the Studio estimate. Show a
measured rate or a calibrated time only from `shambu calibrate` data, with n.

**The two measured prints disagree.** Both prints used Generic PETG, a 0.4 HS01 nozzle and the X2D 0.4 profile.

| Print | Span | Studio | Real | Rate | Resolution |
| --- | --- | --- | --- | --- | --- |
| Bracket D.7, 2026-10-16 | layers 11 to 620 | 281 min | 211 min | 0.751 | about ±0.01 (±1 min per end) |
| Bracket D.7, whole print | start to end (both rebuilt) | 410 min (6 h 50) | about 5 h 0 to 5 h 30 | about 0.75 to 0.80 (whole-print ratio) | ±15 min: neither the start nor the end was logged |
| E7a cradle, 2026-10-21 | layers 135 to 182 | 57.5 min | 60.6 min | 1.054 | ±0.03 (one layer is about 1.5 min of Studio time) |
| E7a cradle, send to layer 135 | upper bound | 289.3 min | at most 298.4 min | at most 1.031 | upper bound only |

So no single rate exists for this printer yet (n=2, 0.75 to 1.05). The speed level was logged on neither print.

**What `shambu calibrate` fits.** Each print has two terms, fitted apart:

- **Rate.** Real minutes per Studio minute between the first and the last poll at layer 11 and up. The first-layer
  slow-down does not bias it.
- **Startup offset.** When the watch saw the start, the real minutes from the first RUNNING poll that the rate does
  not explain (the intercept).

A whole-print prediction is `offset + rate x Studio minutes`. A single ratio applied to Studio's total would
count the startup as if it scaled with the print. The whole-print ratio (real start to end over Studio's total) is
stored only to describe the print. The report shows the range of rates ("measured prints ran at A to B x Studio
(n=N)"). It shows "calibrated about H h M min (n=N)" only when at least 3 prints of that printer model and material
qualify, and their rates span less than 0.15. A print qualifies when all of these hold:

- it finished (FINISH), and the log shows its start (a PREPARE poll, or a first RUNNING poll at layer 0);
- every poll was at Standard speed and 100 %;
- every layer poll has the printer's remaining time, and it stayed within 5 % of Studio's M73 R;
- it was never paused.

A paused print has its paused time removed and is flagged. A log whose clock goes back, whose `task_id` changes or
whose layer goes back is refused.

**Corrections to the D.7 post-mortem.**

- D.7 was PETG, not PLA (`filament_type = PETG` in its G-code; the report said "filament 1 PETG 179.6 g").
- HS01 is a standard-flow hardened-steel nozzle, not high flow: Studio reads character 2 of the type as the flow
  (`S` standard, `H`/`E` high flow) and characters 3 to 4 as the material (`01` hardened steel)
  (`DevNozzleSystem.cpp` :637-670). The "Standard" preset was correct.
- 0.75 is the mid-print rate (layers 11 to 620). The whole print is about 0.75 to 0.80, because the startup took
  longer than Studio allowed.
- The D.7 times are local time (BST), not UTC: the old `Status.ends_at()` used the local clock.
- The printer's countdown on D.7 ran at 0.74 to 0.81 x Studio's M73 `R`. On E7a it equalled M73 `R`. The speed level
  was not logged on D.7. A level other than Standard is the most likely cause. The watch log now records it.

**What Studio counts as 0 s.** Studio simulates the G-code's moves against the machine limits and adds a few fixed
times.

- Heat waits (`M109`, `M190`) count 0 s.
- The calibration commands have no handler, so they count 0 s too: `M983` dynamic flow, `M970`/`M974` vibration
  compensation, `G383`/`G3811` foreign-object and underside detection, plate detection, nozzle wipe and Z-offset
  calibration.
- `G29` (bed levelling) counts a fixed 260 s (`machine_prepare_compensation_time`), and only inside an `M622 J1`
  block. `M191 S` above 40 adds 720 s. `G4`, `M400 S/P` and `SYNC T` add their stated time.
- Studio counts both branches of an `M622 J1`/`J2` flag block, whichever the printer runs.
- No code path scales the result per printer.

The startup is therefore the part Studio cannot model, and the startup offset carries it.

**Outside reports.**

1. No fixed ratio; errors go both ways. BambuStudio #11150 (P2S, 2026-06): the estimate grew by 58 % on a long
   print; Bambu staff said their reprint matched the estimate and that the AMS tube length affects it.
   https://github.com/bambulab/BambuStudio/issues/11150
2. X2D profile slow-downs inflate the X2D estimate. BambuStudio #10590 (X2D, 2026-05): with "slow down for
   overhangs", `z_direction_outwall_speed_continuous = 1` and `outer_wall_speed = 200,50`, the X2D slices much
   slower than an A1 Mini. D.7 and E7a both carry these settings. https://github.com/bambulab/BambuStudio/issues/10590
3. Long multi-colour prints overrun by about 20 % (H2D, X1C). Users name fixed filament-change times, PTFE length
   and purge. https://forum.bambulab.com/t/actual-print-time-is-about-20-longer-than-estimated/172472
4. Single-colour long prints can finish early (P1S, three 19 h prints about 1 h early, about 5.5 %).
   https://forum.bambulab.com/t/how-come-prints-finish-much-earlier-than-bs-estimated/124230
5. The speed level and firmware acceleration change the live remaining time.
   https://forum.bambulab.com/t/bambu-lab-acceleration-overridden/133523 and
   https://forum.bambulab.com/t/change-speed-modes-midprint-automatically/59467

No Bambu statement gives a ratio. The community sees about -5 % to +20 %, mostly from multi-colour work and profile
slow-downs. D.7's 0.75 is an outlier, and a non-Standard speed level explains it better than any slicer cause.

**How to add a measured print.**

1. Export the plate as a `.gcode.3mf`.
2. Run `shambu printer watch --wait-for-start --log print.jsonl --gcode3mf plate.gcode.3mf` before or right after
   sending the print, and leave it until it logs FINISH.
3. Run `shambu calibrate --log print.jsonl --add`. The calibration file stores a fingerprint of the session (its
   header and first and last poll), not the file's name, so one session imported twice is one print.
4. Run `shambu calibrate --show` to print the stored prints and the fit per printer model and material.
