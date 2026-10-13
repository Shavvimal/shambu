---
name: shambu
description: Prepare a Bambu Studio project (.3mf) from STL files with custom print settings, optionally sliced headless with time and filament estimates, through the `shambu` CLI (install it once with /shambu:setup). Also inspects and patches existing projects and reads a Bambu Lab printer's progress over LAN (read-only). No CAD, no printer control, no sending. Use when asked to make a plate, a project, to slice something with given settings, to change a project file, or to check how far the print is.
allowed-tools: Bash(shambu:*), Read
---

# shambu: STLs in, a checked Bambu Studio project out

Scope: take finished STL files plus the settings the user wants, and produce a `.3mf` project (or a sliced
`.gcode.3mf`) that opens in Bambu Studio with the parts placed, the process set, the right filament on each
part and the textured PEI plate selected. The user opens it, checks it and prints it themselves. This skill
never designs parts and never sends a job. It reads the printer's progress over the LAN, nothing more.

User's setup: Bambu Lab X2D, 0.4 nozzle, two nozzles (filament 1 on the left direct-drive nozzle, filament 2
on the right Bowden nozzle), no AMS, 256 x 256 x 261 bed. Bambu Studio 02.08.02.61 on macOS. The user is new
to slicing: explain each step in the hand-back.

## The tool

Everything goes through the `shambu` CLI (Python 3.11+, stdlib plus `paho-mqtt` for the printer only; installed
by `/shambu:setup`). It encodes every CLI rule found by reverse engineering Bambu Studio 02.08.02.61 (its source
at tag `v02.08.02.61`, the shipped binary, crash reports and real project files), so never hand-assemble
`BambuStudio` flags. If `shambu` is not on PATH, tell the user to run `/shambu:setup`.

```
shambu doctor                       # install, profiles, GUI presets, machine reach, recipes
shambu info part.stl ...            # size, bbox, facets, manifold (Studio's --info)
shambu presets list filament --contains PETG
shambu recipes                      # named settings bundles validated on real prints
shambu spec                         # the spec JSON shape
shambu orient part.stl [--threshold 45] [--write DIR]   # score orientations: contact, support shadow, height
shambu rotate part.stl -o DIR --lay-on-face 0,0,1 [--rotate 0,0,45]   # write a turned STL copy
shambu split  part.stl -o DIR       # one STL per body (Studio's "Split to objects")
shambu check  plate.json            # presets, STL measurements, placement, limit checks; no export
shambu build  plate.json [--slice] [--open]   # export, patch, verify, report (+ headless slice)
shambu sweep  plate.json --orientations PART --by-object [--variants v.json]   # slice variants, compare
shambu inspect project.3mf          # plates, objects, parts, filaments, settings, pauses, estimates
shambu patch   project.3mf --bed-type "Textured PEI Plate" --map 1,2 --set "obj:enable_support=1" --pause 1:10.2 --print-sequence "1:by object" ...
shambu printer status [--ip <printer-ip>]     # read-only MQTT pushall
shambu printer watch --every 300              # until the printer leaves RUNNING/PAUSE
```

Add `--json` for machine-readable output (one JSON document; `printer watch --json` prints one document per poll,
JSON Lines). Exit codes: 0 ok, 1 a check or verification failed, 2 usage, 3 environment (binary, config,
printer). Module map and the rules the code encodes: the repository README. Review rubric: `REVIEW-RUBRIC.md`.

## The recipe

1. **Write the spec** (one JSON, see `bambu.py spec`). Name, outdir, printer `Bambu Lab X2D 0.4 nozzle`,
   process (`base` preset, a `name`, `recipes`, `set` overrides as strings), filaments (preset and colour per
   slot), `bed_type`, plates with parts. A part is a `path` with `filament`, `count`, `recipes`, `set`,
   `height_ranges`, optional `at` [x, y] for the bbox corner, optional `lay_on_face` [nx, ny, nz] and `rotate`
   [x, y, z] degrees (the tool writes a turned copy of the STL; the CLI cannot turn one object); or `parts` (several
   files merged into one object, e.g. a label body on filament 1 and its text on filament 2). Per plate:
   `pause_at_z`, `colour_changes`, `print_sequence` ("by layer" default, or "by object"). Use `aux_x_limit: 235.5`
   when the user's note about the auxiliary nozzle must hold.
2. **`check`** it. The tool resolves the presets (`inherits` and `include`, so the X2D start G-code is the
   real 14848-character one), measures every STL, packs the parts (shelf packing, 2 mm edge, 5 mm gap, brim
   margins, parts on filament 2 pushed to x >= 20.5, the prime-tower corner kept free), and checks overlap,
   bed, both nozzles' reach and height, TPU on the main nozzle, manifold meshes, and on a by-object plate the
   head clearance and gantry height rules. Fix the spec until every check reads OK.
3. **`build`** it. Unsliced: `<name>.3mf` in about 0.3 s. With `--slice`: `<name>.gcode.3mf` in about 1 s
   with G-code, time and filament estimates in the report. The tool passes the colours, the bed type, the
   nozzle map and the prime-tower position on the command line, adds `layer_height` to every height range,
   then patches what the CLI leaves wrong (bed type per plate, all filament colours, the map with its
   `filament_extruder_variant`, object names, pauses) and verifies the file by re-reading it and loading it
   through Studio's `--info`.
4. **Hand back** `<outdir>/<name>.report.md`: it is the report (placements with numbers, checks, patches,
   estimates, the user's steps). `--open` opens the project in Studio (a second Studio process when the GUI
   is already running; its window is untouched).

Change an existing project with `patch` instead of rebuilding when only settings move: bed type, colours,
filament to nozzle map, per-object settings (`--set "name:key=value"`), object or part filament
(`--extruder "name[/part]:2"`), height ranges (`--range "name:0-0.6:extruder=2"`), pauses, colour changes,
custom G-code, plate and object names. Patching a sliced file drops its G-code (names excepted); rebuild
with `--slice` or let the user re-slice.

## Slicer rules from experience

Named recipes (`bambu.py recipes`) carry the settings; the reasons stay here. Sources: "gripper build" = the
user's gripper build on this X2D (PETG). "63-tip video" = a 16-year printer and designer's tips video
(2026-10-12). "notes" = `the author's print notes`.

Process presets (one per project, then per-object overrides):
- `petg-functional` (gripper build): 0.2 mm layers, 6 walls, 5 top and 5 bottom shells, 40 % gyroid, elephant
  foot 0.15, no brim by default. `solid-part` (100 % infill) only on parts sized as solid in the stress
  maths: gear, pinion, racks, rails. "100 % on everything" was wrong and cost mass.
- `pla-light` (63-tip video): 3 walls, 15 % gyroid. `pla-decorative`: 2 walls, 5 %.
- Strength comes from walls, not infill: add wall loops before raising infill (gripper build, 63-tip video).
- Local strength: a `modifier_part` sub-part raises infill only around bolt holes (63-tip video); it needs a
  modifier mesh from the CAD step.
- `no-hole-compensation`: clearance lives in the CAD and is qualified by coupons. Slicer compensation applies
  one offset to every hole, the measured error differs by orientation and bore length, and it cannot reach
  horizontal bores at all (notes, gripper build).

Layer height and line width:
- Layer height is the biggest time lever (63-tip video): straight-sided box 40 min coarse vs 3.5 h fine,
  nearly identical. Curved tops show the steps: use the GUI's variable layer height there (no process key
  exists; the GUI writes `Metadata/layer_heights_profile.txt`).
- `wide-line-two-loop-walls`: 0.75 mm line width prints a 1.5 mm wall in two loops, box 40 to 32 min
  (63-tip video).
- `shim-0.1mm`: 0.5 and 1.0 mm spring shims alone at 0.1 mm layers (gripper build). Alone, because with a prime
  tower every object on a plate must share one layer height.

First layer and adhesion:
- Elephant foot 0.15 is in the preset. Parts should also carry a 0.4 x 0.6 mm relief chamfer on every edge
  that mates on the bed side (gripper build). If they do not, say so: that is a CAD fix.
- `tall-outer-brim` (outer only, 5 mm) on anything tall, or long and narrow: blades on end, 216 mm rails,
  112 mm jaws, carrier, bracket (gripper build). `flat-no-brim` on flat coupons and small parts; the default is
  `auto_brim`, so say it explicitly (gripper build).
- Big flat plate (210 x 96 x 3): flat, no brim, unless a corner lifts. Then `dog-ears`: the user places the
  ear points with the Brim Ears gizmo in Studio (gripper build, 63-tip video).

Supports:
- No supports on anything that lies flat (gripper build). Set supports per object (`tree-supports`,
  `snug-normal-supports`, `no-supports`), never for the whole plate.
- Tree supports by default; normal snug supports only for a flat area supported straight up from the plate
  (63-tip video). A few spots only: the user paints enforcers and blockers in Studio.
- Printers bridge further than expected, PLA best (10 to 40 mm clean on a measured plate). The Bambu preset puts
  tree support under every bridge (`max_bridge_length` 0): `bridge-short-spans` frees spans up to 10 mm.
  `minimal-supports` keeps only sharp tails and cantilevers; `counterbore-bridging` closes screw counterbores
  with one sacrificial layer; `petg-supports` widens the gaps so PETG-on-PETG comes off; `more-support` raises
  Bambu's threshold (it counts from horizontal: default 30 = support only under faces within 30 deg of flat).
- Never write a scalar into an X2D speed key (`*_speed`, `*_acceleration`, `enable_overhang_speed`): they are
  six-slot per-nozzle arrays and a scalar wipes the other five (reference 08, section 3).

Different-material support interface (`dual-material-interface`, notes):
- PETG part: PETG tree supports, PLA interface. PLA part: PLA supports, PETG interface. They barely bond.
- Part material on filament 1 (left nozzle), interface material on filament 2 (right nozzle), map 1,2.
  Supported features must sit at x >= 20.5 (the right nozzle's reach). Expect a prime tower and a slower
  print. The bed follows the hotter filament (PETG 70 C under a PLA part).

Colour, text and time:
- Colour position drives time (63-tip video): text on top of a box added 12 min (one swap plus purge tower).
  The same text on a side added 68 min (a swap every layer).
- Split the model so small parts keep one colour; assign the other colour to one back part.
- Cleanest small text: print face down as the background, letters as gaps; first layer one colour, second
  the other. `two-colour-first-layers` does the first three layers on filament 2.
- No second filament loaded: `pause_at_z`, the user swaps by hand and resumes (63-tip video).

Filament and Studio setup:
- Pick the real filament's profile, not Generic, when one exists (`presets list filament`). Same-brand
  profiles are tuned (63-tip video).
- Everything except PLA must be dried before printing. The report says so for PETG and TPU.
- TPU 95A prints only through the main (left) nozzle. The checker enforces it.
- "Split to objects" in Studio separates an STL with several bodies. A part bigger than the bed: Cut tool
  with connectors in Studio, or split in CAD.

Out of scope here: fillets, teardrop holes, nut slots, inserts, polishing, design advice. They belong to the
CAD step.

## Optimising a print: orientation, supports, time (reference 08)

The slicer is the only honest judge of time and filament, and a headless slice takes about a second, so the
skill iterates instead of guessing: score, slice the candidates, read the table, pick, hand back the numbers.

The loop:

1. `shambu orient part.stl` for every part whose orientation is not obvious. It lists the as-is orientation, the
   six axis faces and the largest flat faces laid down, with bed contact, support shadow (the overhang area
   projected onto the bed and summed; stacked overhangs count twice), the steepest overhang, and height. The threshold is 45 deg from vertical (the rule of thumb; Bambu's own
   `support_threshold_angle` counts from horizontal and defaults to 30). Internal faces of a non-watertight STL
   count as overhang: `shambu info` says whether the mesh is manifold.
2. Put the chosen face in the spec (`"lay_on_face": [nx, ny, nz]` copied from the table's lay_on_face column,
   or from `--json`) or let the sweep try the best few: `shambu sweep plate.json --orientations <part key> --top 3`.
3. On a plate with two filaments and several objects, add `--by-object`. The table shows time, grams, support
   grams, waste (prime tower plus flush) and filament changes per variant, fastest first.
4. Pick by the user's priority in this order: load direction (layers are the weak direction: upright PLA keeps
   about half the flat strength), then supports and finish, then time. Report the table rows that mattered.

Orientation rules (details and sources in reference 08, section 1):
- Largest flat face on the bed unless the load or a show face forbids it. Loads pull along X/Y lines, never
  across layers; a hook or a clip lies down. Pins and snap arms lie down with a D-flat; bolts stand, nuts lie;
  gears lie; cylinders stand for roundness.
- Steep features point up. 45 deg from vertical is safe at 0.2 mm on the 0.4 nozzle, 45 to 60 is "test it".
  Fillets facing the bed are overhangs: chamfer them in CAD. Horizontal holes droop and go oval: teardrop them.
- Show faces up or on a side, never on supports. A face on the textured plate gets its matte texture; text on the
  bottom prints face down with no overhang.
- Height costs time on every layer, and on a two-nozzle plate every layer with both filaments costs a nozzle
  switch and a tower wipe. Colour boundaries go horizontal (Z bands): text on top of a box added 12 min, on a side
  68 min.
- A part with no good face: cut it (Studio's Cut tool, C, with dowel connectors) and print both halves flat.
  shambu has no mesh boolean; say so and hand the cut to the user.
- Studio's auto-orient (Shift+R) scores overhang against bottom area only: no strength, no time, and bridges read
  as overhang. The tool's own score has the same bridge blind spot, which is why the sweep slices the candidates.

Print by object (`print_sequence: "by object"` on the plate; exact source rules in reference 08, section 2):
- What it does: each object prints start to finish before the next. With two filaments on two nozzles that
  removes the prime tower and the one-nozzle-change-per-layer cost: the measured bracket-plus-cubes plate went
  from 1 h 24 (7.2 g tower, 100 changes) to 40 min (1 change). The research agent's four cubes: 1 h 13 to 48 min.
- When it loses: many tiny parts. Each object meets the filament's minimum layer time alone (PLA 4 s per layer),
  so six 10 mm cubes took 28 min by object against 14.5 by layer. Always slice both (`--by-object`) and keep the
  faster one.
- Rules the tool enforces on the X2D (only when the plate holds more than one object, as in Studio): 60 mm
  between object footprints (4 mm when every object is under 4 mm tall; more with a per-object skirt); objects
  print in list order, which the tool sets to main filament then height, so two groups of single-filament objects
  need one nozzle change in total (an object that itself uses two filaments, through sub-parts, height ranges
  or a support interface, still changes within itself); an object over 32.8 mm must not be followed by one within
  33 mm in Y (the gantry rods), so the packer gives it the rest of its row; nothing but the last object may pass
  133 mm; Smooth timelapse and clumping detection are refused. Studio drops the prime tower itself, so the idle
  nozzle is not wiped between objects. A `print_sequence` in the process settings counts too; the plate's own
  value wins.
- Studio GUI equivalent for the user: right-click the plate, Plate Settings, Print sequence, By object; then
  Arrange (A) spaces the objects at 60 mm and the object list order is the print order.

Other time levers worth a sweep variant (reference 08, section 4): the right (Bowden) nozzle runs the outer wall at
50 mm/s against 200 on the left, so the filament with the most volume goes on filament 1 (an 80 mm box: 1 h 08 left,
2 h 09 right); `0.20mm Standard`, not High Quality (19 vs 27 min on a cube); `fast-infill` (adaptive cubic plus
combined infill: grid 1 h 09, adaptive cubic 0 h 59, gyroid 1 h 43 on an 80 mm box); `coarse-fast` 0.28 mm layers
(27 % fewer colour changes in Bambu's example); colour changes near the bottom so the tower stops early.

Finish (reference 08, section 5): `scarf-seam` on cylinders and `seam-back` on cornered parts (rotate the hidden
side to +Y); `show-walls` (classic, three loops, inner-outer-inner) on cosmetic parts without overhangs;
`iron-top` on one flat show face; `precise-fit` for fit-critical parts (precise outer wall works only with
inner-then-outer order). Keep the X2D defaults (classic walls, monotonic-line tops, arc fitting, continuous
outer-wall speed in Z). `enable_circle_compensation` only helps PLA Basic, PLA CF, PET CF, PETG CF and PETG HF.

What to tell the user to do in Studio (checked against the 2.8 source; reference 08, section 7): Lay on face is F
(click a white plane), Rotate is R (snaps at 45 deg on the inner ring, 5 deg on the ticks), Cut is C, Auto orient
is Shift+R, Show Overhang is Cmd L, Supports painting is L, Seam painting is P, bottom view is Cmd 2. After Slice
Plate, the Preview colour scheme has no "Overhang speed" entry in 2.8: use Line Type and read the Overhang wall,
Bridge and Support rows, or Speed; the filament table under the legend splits each filament into Model, Support,
Flushed, Tower and Total. Toggle Supports in the Process panel and re-slice to see where support goes.

## Machine limits the tool checks

- Bed 256 x 256, 261 tall. Check footprints, not counts: two 200 g parts do not share the bed (227 + 95).
- Right (auxiliary, Bowden) nozzle = extruder 2: reaches x 20.5 to 256 only and z up to 256. Anything on
  filament 2, including a height range or a sub-part, must sit inside that. The user's note said "inside
  235.5 mm in X": the width matches (256 - 20.5), the dead strip is on the left edge, not the right. Pass
  `aux_x_limit: 235.5` to apply the user's reading as well.
- External spool: upper buffer inlet feeds the left (main) hotend, lower inlet feeds the right.
- USB export: File, Export, Export Plate Sliced File gives `.gcode.3mf`; a known bug doubles the extension.
  Cloud send needs the same account in Studio and on the printer.
- Time: the slicer estimate wins (`--slice` gives it). Real plates ran 86 g in 3 h 11 min and 242 g in
  9 h 31 min.

## Feedback to the calling agent

Hand back the report file's content plus plain statements with numbers. Before building, the `check` output
answers: every STL exists and is manifold; each footprint and the layout fit; every STL lies on its bed face
(an STL in an assembly frame gets `lay_on_face` or `rotate` in the spec after `shambu orient`, never a guess);
filament 2 parts
and their x extents; TPU on the main nozzle; brims and supports per part with the recipe used; the process
matches the material.

After building: project path, sliced or not, estimate per plate when sliced (with filament by feature: model,
support, tower, flush grams and the number of filament changes), per plate the objects with filament, material
and orientation, the print sequence, the process name and the key settings, the patches made, the sweep table
when one ran, and what the user still sets by hand in Studio (painted enforcers, brim ear points, variable layer height, anything the spec could not
express). Then: open, check, slice (if unsliced), print.

Standing warnings (include every time):
- The first print of any new fit is a coupon or one part, in the same orientation as the feature. A flat
  coupon cannot qualify a sideways bore.
- Holes print undersize: about 0.1 mm vertical, 0.2 to 0.3 mm horizontal (bridged), more on long bridged
  bores. Flag every bore that prints horizontal.

## Reading the printer (read-only)

`shambu printer status` reads `BambuStudio.conf` for the selected serial and its LAN access code,
finds the printer by probing the ARP hosts on TCP 8883 (or `--ip`), connects with TLS (Bambu's bundled CA,
then unverified as a fallback), subscribes to `device/<serial>/report`, publishes one `pushall`, and prints
state, percent, layer, remaining time and end time, both nozzles (id 1 = main left, id 0 = auxiliary right),
bed and chamber, HMS codes with the offline text from Studio's `hms_en_20P.json`. `watch` polls until the
state leaves RUNNING/PAUSE (the state after a finished print is FINISH). The access code never leaves memory.
Nothing but `pushall` is ever published; starting, pausing or stopping stays with the user.

Pitfalls: Studio holds UDP 2021 (SSDP) while it runs, so discovery probes 8883 instead (`printer discover
--ssdp 10` listens with SO_REUSEPORT when needed). Newer firmware may need LAN-only mode for third-party
MQTT; status pushes are exempt from Bambu's Authorization Control, so no Developer Mode is needed.

## What the reverse engineering found (2026-10-13, Studio 02.08.02.61)

- The "5 minute export" was not compute: `export_time` in `result.json` is milliseconds (an export takes
  0.3 s). After any Studio crash, macOS shows a modal "reopen windows?" alert inside the CLI process, which
  then blocks for ever at 0 % CPU. The tool sets `CFFIXED_USER_HOME` to a private empty directory on every
  call, which removes the alert without touching the real `~/Library`.
- Headless `--slice` crashed (exit 139) in `FilamentGroup::calc_one_change_budget`: with two filaments and no
  filament colours the flush matrix stays 1 x 1. A colour per filament fixes it. A height range without
  `layer_height` crashes `layer_height_profile_from_ranges`. The default prime tower at (15, 220) runs off the
  bed (return code -104). The tool handles all three; the user's fit-test plate slices in 0.6 s.
- The CLI never resolves `inherits` or `include`. The old `resolve_presets.py` produced a machine preset with
  the generic 577-character start G-code instead of the X2D's template: never print G-code from those files.
- Bool flags take no value (`--min-save 1` makes `1` an input file). `--min-save` strips all meshes. `--orient 1`
  overwrites every object's settings with the process preset. `--pipe` does nothing on macOS. Actions run in
  argv order (`--no-check` before `--slice`). `--export-3mf` wants a bare name plus an existing `--outputdir`.
- Assemble list: `pos_*` are translations added to the STL's own coordinates (not a centre); keep `pos_x`,
  `pos_y`, `pos_z` the same length (an indexing bug); `*_params` values must be strings; a merged part loses
  its height ranges (use `assembled_params`); non-part subtypes need `assemble_index >= 1`; the CLI defaults
  the bed to Cool Plate, which the X2D does not support, and writes one colour and one map entry for two
  filaments.
- 3MF: `layer_config_ranges.xml` keys objects by build order, not id. A filament mapped to nozzle 2 also needs
  `filament_extruder_variant` = "Bowden Standard" or slicing crashes. Thumbnails are optional. Every patch the
  tool makes survives a CLI load and re-export.
- Studio GUI: Accessibility exposes the menus (export is `File > Export > Export plate sliced file`, ⌘G) but
  not the toolbar; `⇧⌘G` is Print plate, so the tool never sends keystrokes. The only safe GUI action is
  `open -a BambuStudio file.3mf`.
- Reports with the evidence, file:line citations and experiment tables sit beside this file:
  `references/01-source-cli.md`, `02-binary-crash.md`, `03-3mf-format.md`, `04-app-printer-control.md`,
  `05-bambu-cli-ideas.md`, `07-print-recipes.md`, `08-optimisation.md` (orientation, print-by-object source rules,
  supports, time, finish, headless metrics, the Studio GUI workflow, the ClassCAD evaluation; 2026-10-13). Read the
  relevant one before changing the library.
