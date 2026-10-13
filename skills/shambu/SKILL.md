---
name: shambu
description: Builds Bambu Studio projects (.3mf) from STL files through the shambu CLI. It places parts, sets the process, filaments, brims, supports and bed type, picks a print orientation, prints by object when that is faster, and slices headless for time and filament estimates. It also compares orientation and print-sequence variants, inspects and patches existing .3mf or .gcode.3mf files, and reads a Bambu Lab printer's progress over LAN (read-only). Use when the user wants a plate or project from STLs, asks how to orient or support a part, wants a print time or filament estimate, wants to change settings in a 3MF, or asks how far the print is, even if Bambu Studio is not named. Not for designing parts (use a CAD skill), mesh repair, or starting, pausing or sending prints.
argument-hint: "[stl files or project.3mf] [print notes]"
allowed-tools: Bash(shambu *), Read
---

# shambu: STLs in, a checked Bambu Studio project out

Produce a `.3mf` (or a sliced `.gcode.3mf`) that the user opens, checks and prints. Never design parts, never
send a job. Request: $ARGUMENTS

## Before you start

1. Run `shambu doctor --json`. It gives the printer preset, both nozzles' reach and height, the bed, the GUI's
   current presets and bed type, and the recipe list. Use those values, not assumptions. If `shambu` is missing,
   tell the user to run `/shambu:setup` and stop.
2. One name per nozzle: left nozzle = filament 1 (main, direct drive); right nozzle = filament 2 (auxiliary,
   Bowden; on the X2D it reaches x 20.5 to 256 only, up to 256 mm, and prints no TPU).
3. Everything goes through the CLI. Never hand-assemble `BambuStudio` flags: the CLI encodes every rule found by
   reverse engineering Studio 02.08.02.61 (`references/00-findings.md`).

## Workflow (copy this checklist into the reply)

- [ ] 1. Spec: write `plate.json` (example below; the full shape: `shambu spec`; the settings bundles: `shambu recipes`).
- [ ] 2. Orient: `shambu orient part.stl` for every part that does not already lie on an obvious flat face; put
      the chosen `lay_on_face` in the spec. A mesh under 1 mm is in metres: `shambu rotate part.stl -o DIR --scale 1000`.
- [ ] 3. Check: `shambu check plate.json` until every line reads OK (presets, measurements, placement, reach,
      by-object rules).
- [ ] 4. Compare when the answer is not obvious: `shambu sweep plate.json --orientations PART --by-object`
      (and `--variants v.json` for settings). The slicer's table decides: time, grams, support, waste, swaps.
- [ ] 5. Build: `shambu build plate.json --slice`. Exit 1 means a check, the export or a verification failed:
      read the report, fix the spec, rebuild. Exit 3 means the environment (binary, config, a missing file).
- [ ] 6. Hand back with the template below, including both standing warnings.

Change an existing project with `shambu patch` instead of rebuilding when only settings move (bed type,
colours, nozzle map, per-object settings, filament per object or part, height ranges, pauses, colour changes,
plate print sequence, names). Patching a sliced file drops its G-code: rebuild with `--slice` or let the user
re-slice. `patch` changes settings only; it does not re-check spacing, so a by-object plate patched from a
by-layer layout still needs 60 mm between objects.

## Minimal spec

```json
{
  "name": "jaws", "outdir": "/abs/dir", "printer": "Bambu Lab X2D 0.4 nozzle",
  "process": {"base": "0.20mm Standard @BBL X2D", "name": "PETG functional", "recipes": ["petg-functional", "no-hole-compensation"]},
  "filaments": [{"preset": "Bambu PETG Basic @BBL X2D 0.4 nozzle", "colour": "#000000"},
                {"preset": "Bambu PLA Basic @BBL X2D 0.4 nozzle", "colour": "#FFFFFF"}],
  "bed_type": "Textured PEI Plate",
  "plates": [{"name": "p", "print_sequence": "by object", "group_by_filament": true,
              "parts": [{"path": "/abs/jaw.stl", "count": 2, "lay_on_face": [0, 1, 0], "recipes": ["tree-supports"]},
                        {"path": "/abs/label.stl", "filament": 2, "recipes": ["flat-no-brim"]}]}]
}
```

A part is a `path` with `filament`, `count`, `recipes`, `set` (keys as strings), `height_ranges`, `at` [x, y],
`lay_on_face` [nx, ny, nz] and `rotate` [x, y, z] degrees; or `parts` (several files merged into one object,
turned together). Per plate: `pause_at_z`, `colour_changes`, `print_sequence`, `group_by_filament` (by object:
same-filament parts with the same settings merge into one object, packed close inside, 60 mm between groups).
`aux_x_limit: 235.5` applies the user's stricter reading of the right nozzle.

## Hand-back (use this shape every time)

```
Project: <path>  (sliced | unsliced | export FAILED: <Studio exit and reason>)   Printer: <preset>   Process: <name> (<base>, recipes ...)
Plate <n>: <time> h, <g> g (model <g>, support <g>, tower <g>, flush <g>, <k> filament changes)  print sequence: by layer | by object
  <object>  filament <n> (<material>)  <w> x <d> x <h> mm at x.., y..  face <normal> | as exported  recipes ...
Checks: all OK | FAIL: ...        Patches: ...        Slicer warnings: ...
Sweep (when run): the table rows that decided the choice, and why.
Still by hand in Studio: painted supports, brim ear points, variable layer height, <anything the spec cannot say>.
Next: open the project, confirm the plate type and colours, check Preview (Line Type: Overhang wall, Bridge, Support rows; the first layer), then Print.
Standing warnings:
- The first print of any new fit is a coupon or one part, in the same orientation as the feature. A flat coupon cannot qualify a sideways bore.
- Holes print undersize: about 0.1 mm vertical, 0.2 to 0.3 mm horizontal (bridged), more on long bridged bores. Every bore that prints horizontal is flagged above.
```

Write it as plain statements with numbers, in the order above. The report file `<outdir>/<name>.report.md`
holds the same facts; quote it, do not paraphrase numbers.

## Commands

```
shambu doctor | info FILE... | presets list filament --contains PETG | recipes | spec
shambu orient PART.stl [--threshold 60] [--top 12] [--write DIR]      # score orientations: contact, support shadow, height
shambu rotate PART.stl -o DIR [--lay-on-face nx,ny,nz] [--rotate x,y,z] [--scale 1000]
shambu split PART.stl -o DIR                                           # one STL per body
shambu check plate.json | build plate.json [--slice] [--open]
shambu sweep plate.json [--orientations PART]... [--by-object] [--variants v.json] [--no-slice]
shambu inspect project.3mf
shambu patch project.3mf --bed-type "Textured PEI Plate" --map 1,2 --set "obj:enable_support=1" --pause 1:10.2 --print-sequence "1:by object"
shambu printer status [--ip IP] | printer watch --every 300            # read-only
```

`--json` gives one JSON document (`printer watch --json`: one per poll). Exit codes: 0 ok, 1 a check, export
or verification failed, 2 usage, 3 environment.

## Decision rules

| Situation | Do |
|---|---|
| Functional PETG part | process `petg-functional` + `no-hole-compensation`; `solid-part` only on parts sized as solid |
| Everyday PLA, decorative PLA | `pla-light`; `pla-decorative` |
| Part needs strength | more walls, lie the load in the layer plane, stronger filament, fillets in CAD; never more infill first |
| Tall, or long and narrow | `tall-outer-brim`; flat coupons and small parts `flat-no-brim`; a corner lifts on a big flat plate: `dog-ears` (user places the points) |
| Overhangs | `tree-supports` per object, never for the whole plate; `snug-normal-supports` for a flat area straight above the plate; `bridge-short-spans` frees spans to 10 mm; `minimal-supports`, `counterbore-bridging`, `petg-supports`, `more-support` as named. Trees spread up to 25 mm past the footprint: the layout reserves that |
| PETG part with PLA loaded too (or the reverse) | `dual-material-interface`: the other material as interface only, map 1,2, supported features at x >= 20.5 |
| Two filaments, several single-colour objects | `print_sequence` by object with `group_by_filament`; sweep `--by-object` to confirm (measured 1 h 24 to 40 min; merged groups 21 min against 29 min as separate objects and 42 min by layer on four cubes); it loses on many tiny parts; biggest-volume filament on the left nozzle |
| Colour on a face | horizontal colour bands (Z) beat vertical ones (12 min vs 68 min for the same text); text face down as gaps in the first layers (`two-colour-first-layers`); no second filament loaded: `pause_at_z` |
| Orientation unclear | `shambu orient`, then `sweep --orientations`; priority: load direction, then supports and finish, then time; largest flat face down otherwise; show faces up or on a side; bed-facing fillets are a CAD fix (chamfer). The score misreads bridges and favours standing a thin part: the sweep decides |
| Time matters | `coarse-fast` (0.28), `fast-infill`, Standard not High Quality (1666 s vs 1062 s for the same 0.16 layers on a cube), fewer top shells on hidden tops, colour changes near the bottom; a `height_ranges` entry with a coarser `layer_height` above the detail |
| Top surface rough or a show face | `fine-0.16` only on the part that needs it (flat solid tops look worse and print slower); `top-concentric` on round tops; `iron-top` on one flat face; keep the X2D defaults (monotonic-line top, one wall on all top surfaces, 0.42 top line width) elsewhere |
| Finish matters | `scarf-seam` on cylinders, `seam-back` on cornered parts, `show-walls`, `precise-fit` for fits, `fuzzy-skin` to hide lines; matte filament, polish or resin after (`references/09-user-notes.md`) |
| Shims and rings | `shim-0.1mm`, alone on the plate |
| Thin walls | `wide-line-two-loop-walls` |
| TPU | filament 1 only (the checker enforces it); dry everything except PLA |
| Several bodies in one STL | `shambu split`, then one part each (inward shells are cavities, not bodies) |
| Part bigger than the bed, or no good face | the user cuts it in Studio (C, with dowels) or in CAD; the tool has no mesh boolean |

The reasons and numbers behind every row: `references/06-slicer-rules.md` and `references/08-optimisation.md`.

## Print by object, in one paragraph

Each object prints start to finish before the next. With one filament per nozzle that removes the prime tower
and the nozzle change on every layer. It loses on many tiny parts, where each object waits out the minimum
layer time alone; `group_by_filament` merges same-filament parts into one object so the group prints together.
The tool orders objects by filament then height, packs them 60 mm apart (4 mm when all are under 4 mm tall),
closes the row after any object over 32.8 mm (a later object in its Y band would hit the gantry rods), refuses
Smooth timelapse and clumping detection, and lets Studio drop the tower. Studio's own rules and the live tests:
`references/08-optimisation.md` section 2. GUI equivalent for the user: right-click the plate, Plate Settings,
Print sequence, By object; Arrange.

## Never

- Write a scalar into an X2D speed or acceleration key: they are six-slot per-nozzle arrays.
- Set supports, brims or infill for the whole plate when one part needs them: per object.
- Guess an orientation for a part exported in an assembly frame: `shambu orient`, then `lay_on_face`.
- Send keystrokes to Studio, press Print, or publish anything but one `pushall` to the printer.
- Trust the orientation score over the slicer: bridges count as overhang in the score; the sweep decides.

## Reading the printer

`shambu printer status` reads the selected printer's serial and LAN access code from Bambu Studio's own
configuration, finds the printer on the LAN, subscribes and sends one `pushall`, and prints state, progress,
layer, remaining time, nozzle, bed and chamber temperatures and HMS codes. `watch` polls until the print leaves
RUNNING/PAUSE. Nothing else is ever sent; the access code never leaves memory. Details and pitfalls:
`references/00-findings.md` section 8.

## References (read when the row says so)

| File | What | Read when |
|---|---|---|
| [00-findings.md](references/00-findings.md) | Every rule the CLI encodes, one paragraph each; the printer reader | something fails, or before changing the library |
| [06-slicer-rules.md](references/06-slicer-rules.md) | The reasons and numbers behind the recipes; X2D limits | choosing between recipes, explaining a setting |
| [08-optimisation.md](references/08-optimisation.md) | Orientation, print-by-object source rules, supports, time, finish, headless metrics, the Studio GUI workflow | optimising a plate, explaining the Studio steps |
| [07-print-recipes.md](references/07-print-recipes.md) | Key scopes, defaults, the tip table | adding a recipe or a per-object key |
| [09-user-notes.md](references/09-user-notes.md) | The user's own finish and fit notes | the hand-back for this user |
| 01 to 05 | CLI source, binary, 3MF format, printer protocol, other tools | changing the code |
