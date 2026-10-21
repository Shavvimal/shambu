---
name: shambu
description: Builds Bambu Studio projects (.3mf) from STL files through the shambu CLI. It reads which spools the printer holds and uses them as the project's filaments, places parts on their outlines (never rotated) or at the corner the spec gives, sets the process, brims, supports, prime tower and bed type, picks a print orientation, prints by object when that is faster, and slices headless for time and filament estimates. It also compares orientation and print-sequence variants, inspects, patches and edits existing .3mf or .gcode.3mf files (settings, mesh, filaments, object and tower positions), and reads a Bambu Lab printer's progress over LAN (read-only). Use when the user wants a plate or project from STLs, asks how to orient or support a part, wants a print time or filament estimate, wants to change a 3MF, or asks how far the print is, even if Bambu Studio is not named. Not for designing or editing CAD parts (use a CAD skill), mesh repair, printer hardware calibration, or starting, pausing or sending prints.
argument-hint: "[stl files or project.3mf] [print notes]"
allowed-tools: Bash(shambu *), Read
---

# shambu: STLs in, a checked Bambu Studio project out

Produce a `.3mf` (or a sliced `.gcode.3mf`) that the user opens, checks and prints. Never design parts, never
send a job. Request: $ARGUMENTS

## Before you start (standing rules)

1. Before the first shambu command of a task (before the spec is written): `shambu --version` (it must print:
   shambu 0.4.0), then `shambu doctor --json --expect-version 0.4.0`. Exit 3 with `"shambu": {"match": false}`
   (`MISMATCH` and a `Fix:` line) means another version is on PATH. A usage error on `--expect-version` means one
   older than 0.4.0. Either way: stop, give both versions and the fix (`/shambu:setup update`). Never use
   `uv run --project`, a checkout or another binary. No `timeout` wrapper (macOS has none), no `2>/dev/null`.
   Missing: `/shambu:setup`. doctor cannot find the printer's IP: `shambu printer discover`.
2. doctor gives the printer preset, both nozzles' reach and height, the bed, the GUI presets, the recipes and,
   under `printer`, the spools loaded now. Use those values, not a spec from an earlier day.
3. Write every filament as `{"preset": "loaded"}` unless the user names a spool. A pinned preset that is not the
   spool in its nozzle is a FAIL. `--allow-filament-mismatch` only when the user says the spool will change. The
   printer not read (`Filaments: WARNING printer not read`): the filaments are UNVERIFIED. The hand-back says so,
   and `Next:` starts "confirm the spools by eye". `--no-printer` only when the user says it is off.
4. Left nozzle = extruder 1 = filament 1 (main, direct drive); right nozzle = extruder 2 = filament 2 (Bowden;
   its x reach is narrower, from doctor; no TPU). `filament_map` and `@EXTRUDER` name the extruder: 1 left, 2 right.
5. Everything goes through the shambu CLI. Never run `BambuStudio` yourself and never unzip a 3MF to edit its XML
   (the rules: `references/00-findings.md`). The two sanctioned exits: `shambu slice FILE.3mf`, and, only after
   the user says yes, the logged `shambu studio -- ARGS`. When no verb does the work, say "shambu has no verb for
   <work>" and offer the Studio GUI steps or that one `shambu studio` call.
6. State a cause only when a check line or the Studio log states it (Hand-back, Cause). Never say "needs N
   plates" or "must print alone" from `check`: a plate count comes from a slice.

## Workflow (copy this checklist into the reply)

- [ ] 1. Spec: write `plate.json` (example below; `shambu spec` shows the full shape, `shambu recipes` the
      bundles). `shambu spec plate.json` warns about each unknown key (a typo is dropped).
- [ ] 2. Orient: `"orientation": "auto"` (Studio's auto-orient), or `shambu orient part.stl [--native]` and the
      chosen `lay_on_face`. A mesh under 1 mm is in metres: `shambu rotate part.stl -o DIR --convert-unit`.
- [ ] 3. Pre-flight: `shambu check plate.json`. The report prints these prefixes. Fix every `FAIL body overlap`
      and every plain `FAIL:` line (a spool, preset, file, mesh, the bed, reach, height or the prime tower).
      `FAIL margin rule`: a measured (probe) or spec spread reaches the other part. The measurement is real, so
      move the part, or lower the spec `support_spread`, then check again; it is never a plate count.
      `WARN margin rule`: an estimated spread; the slice decides. `WARN brim`: Studio will clip that brim.
      `WARN outline`: the box stands in for a mesh outline. `PLACEMENT NOT DONE`: the packer (a heuristic, no
      rotation) found no spot; quote it and give that part an `at`. `--probe-supports` measures each supported
      part's spread with a cached solo slice.
- [ ] 4. Compare when the answer is not obvious: `shambu sweep plate.json --orientations PART [--by-object]
      [--variants v.json] --write-spec plate-best.json [--pick NAME]`. Then build the written spec. Never copy a
      variant by hand. A further change (4 walls) goes in a `--variants` entry or the written spec's
      `process.set`, named in the hand-back. Strength and finish stay the user's call.
- [ ] 5. Build and slice: `shambu build plate.json --slice`: the final test. Exit 1: a check, the export or a
      verification failed; fix the spec, rebuild. Exit 3: the environment. On `export FAILED`, quote the exit
      and the log line (Decision rules). An unplaced part never exports, even with `--keep-going`; ask the user
      before `--keep-going` and list every FAIL it overrode.
- [ ] 6. Hand back with the template below. It is a gate.

Change an existing project with `shambu patch` (settings, bed, colours, map, pauses, names, print sequence, the
edits under Commands; `--dry-run` first). Patching a sliced file drops its G-code: `shambu slice` again. `patch`
does not re-check by-object spacing.

## Hand-back (a gate: no reply about a check, build, sweep, patch or slice goes out without it)

Use it for every result: a failure, a "does not fit" answer, a sweep, a project the user made and you patched.
The template comes first; an explanation follows it, never replaces it.

```
Project: <path>  (sliced | unsliced | export FAILED: <Studio exit and reason>)   Printer: <preset>   Process: <name> (<base>, recipes ...)
Filaments: from the printer at HH:MM: 1 = <preset> <colour> (<left/right external spool | AMS n tray m>), 2 = ...
           | WARNING printer not read (<reason>): from the GUI presets, UNVERIFIED; confirm the spools by eye before Print
Plate <n>: Studio estimate <h> h <m> min[<calibration text as printed>], <g> g (the grams the report prints: support <g>, prime tower + flush <g>, <k> filament changes)  print sequence: by layer | by object
  <object>  filament <n> (<material>)  <w> x <d> x <h> mm, min corner at x.., y..  face <normal> | as exported  recipes ...
  Prime tower: <x, y, width, rib wall> | none; two filaments: <tower min> min, <k> changes, and the two options the report names (width 22, false) with their measured times
Pre-flight (shambu check): all OK | <kind> <quoted line> ...        Patches: ...
Slice: Studio exit 0 | Studio exit <code>: "<quoted log line>" | not sliced (<why>)   Checks skipped: <quoted line> | none   Slicer warnings: ...
Cause (only when something failed): the check line or Studio log line that states it, quoted | "not established"
Sweep (when run): the table rows that decided the choice, and why.
Still by hand in Studio: painted supports, brim ear points, variable layer height, <anything the spec cannot say>.
Next: open the project (`shambu open project.3mf`), confirm the plate type and colours, check Preview (Line Type: Overhang wall, Bridge, Support rows; the first layer), then Print.
Standing warnings:
- The first print of any new fit is a coupon or one part, in the same orientation as the feature. A flat coupon cannot qualify a sideways bore.
- Holes print undersize: about 0.1 mm vertical, 0.2 to 0.3 mm horizontal (bridged), more on long bridged bores. Every bore that prints horizontal is flagged above.
```

Quote the report `<outdir>/<name>.report.md`; never paraphrase its numbers. Give only the grams it prints
(prime tower + flush is one number); model grams only when the report has them.

- Time: always "Studio estimate" with the number. A measured range ("measured prints ran at A to B x Studio
  (n=N)") or "calibrated about H h M min (n=N)" appears only when `shambu` prints it. Never apply a ratio
  yourself, never say "it will take".
- Cause: when the check and the slice disagree, or a slice fails and no line names why, write exactly: "The
  check said <quoted line>. The slicer said <exit code and quoted line>. The cause is not established." Then
  name the one test that would tell the candidates apart, and run it if it is cheap. A guess given as the reason
  is a fault, even when it sounds right.

## Minimal spec

```json
{
  "name": "jaws", "outdir": "/abs/dir", "printer": "Bambu Lab X2D 0.4 nozzle",
  "process": {"base": "0.20mm Standard @BBL X2D", "name": "PETG functional", "recipes": ["petg-functional", "no-hole-compensation"]},
  "filaments": [{"preset": "loaded"}, {"preset": "loaded"}],
  "bed_type": "Textured PEI Plate",
  "plates": [{"name": "p", "print_sequence": "by object", "group_by_filament": true,
              "parts": [{"path": "/abs/jaw.stl", "count": 2, "lay_on_face": [0, 1, 0], "recipes": ["tree-supports"]},
                        {"path": "/abs/label.stl", "filament": 2, "recipes": ["flat-no-brim"], "at": [150, 20]}]}]
}
```

A part takes `path`, `filament`, `count`, `recipes`, `set` (string values), `height_ranges`, `lay_on_face`
[nx, ny, nz] or `rotate` [x, y, z], or `"orientation": "auto"` (not with either); or `parts` (several files as
one object). `at` [x, y] is the bed position of the turned part's bounding-box MIN corner (front-left), not its
centre: to centre a part w x d on a bed W x D (doctor), write [(W - w) / 2, (D - d) / 2]. `brim_margin` (mm)
replaces the computed brim and spread on every side (FAIL-level); `support_spread` [-x, +x, -y, +y] (mm) sets the
spread per side. Per plate: `print_sequence`,
`group_by_filament`, `pause_at_z`, `colour_changes`, `wipe_tower` (`"auto"`, the default with two filaments;
`{"x", "y", "width", "rib_wall", "brim"}` with x, y the min corner; or `false`). A pinned filament `colour` gives
way to the spool's unless it has a `colour_note`. `aux_x_limit` (mm): a stricter right-nozzle x limit, when the
user sets one.

## Commands

```
shambu --version | doctor [--expect-version X.Y.Z] [--no-printer] [--ip IP] [--printer-timeout S]
shambu info FILE... | inspect project.3mf | presets list filament --contains PETG | recipes | spec [plate.json]
shambu orient PART.stl [--native] [--write DIR] | rotate PART.stl -o DIR [--lay-on-face nx,ny,nz] | split PART.stl -o DIR
shambu check plate.json | build plate.json [--slice] [--no-check] [--open] [--probe-supports]   # both read the spools first
       [--allow-filament-mismatch] [--no-printer] [--ip IP] [--keep-going: only with the user's yes]
shambu sweep plate.json [--orientations PART]... [--source own|studio] [--by-object] [--variants v.json] [--write-spec OUT.json [--pick NAME]]
shambu patch project.3mf --bed-type "Textured PEI Plate" --map 1,2 --set "obj:enable_support=1" --pause 1:10.2 --print-sequence "1:by object"
shambu patch project.3mf --replace-mesh OBJECT[/PART]=new.stl [--rotate-mesh X,Y,Z | --lay-on-face NX,NY,NZ] [--anchor frame|centre|min]
       --add-filament "PRESET[:#RRGGBB][@EXTRUDER]" | --move OBJECT=X,Y[@PLATE] (or +DX,+DY) [--strict] | --tower X,Y[@PLATE]
       --repair-nozzle-stats | --no-verify (skip the verify slice) | --dry-run | -o OUT.3mf
shambu open project.3mf                                                # opens it in Studio for the user
shambu slice FILE.3mf [--plate N] [--no-check] [--tower X,Y[@PLATE]] [--settings-from plate.json]
shambu studio [--out DIR] -- ARGS...                                   # logged raw Studio call: only with the user's yes
shambu printer discover | printer filaments | printer status | printer watch [--every 300] [--log FILE --gcode3mf F.gcode.3mf [--wait-for-start]]
shambu calibrate --log FILE --add | calibrate --show                    # store a watched print; show the fit
```

`--json` gives one JSON document (`printer watch --json`: one per poll). Exit codes: 0 ok, 1 a check, export or
verification failed or a patch was refused, 2 usage, 3 environment.

Edits: `--replace-mesh` keeps the name, settings and plate, and refuses a size change, a shared mesh or paint
(`--allow-size-change`, `--drop-paint`). `--add-filament` appends filament N+1 with the preset's colour, or the
next unused default colour and a WARN naming it, then a verify slice (`--no-verify` skips it, about 20 s). `--move` and `--tower` take the min corner and
report `check`'s pair rule. `slice --no-check` adds an unchecked pass per plate only after a -101 or a warning
-100, and lists what it skipped. `shambu studio` logs under `studio-runs/<UTC stamp>/`: quote the command, exit
and log path; its output is unverified until `shambu inspect` reads it.

## Decision rules

| Situation | Do |
|---|---|
| Functional PETG part | process `petg-functional` + `no-hole-compensation`; `solid-part` only on parts sized as solid |
| Part needs strength | more walls, lie the load in the layer plane, stronger filament, fillets in CAD; never more infill first |
| Tall, or long and narrow | `tall-outer-brim`; flat coupons, small parts `flat-no-brim`; a lifting corner: `dog-ears` (the user places the points) |
| Overhangs | `tree-supports` per object; `snug-normal-supports` for a flat area straight above the plate; `bridge-short-spans` (spans to 10 mm); `minimal-supports`, `counterbore-bridging`, `petg-supports`, `more-support` |
| Parts close together | `check` keeps the larger of two facing support spreads plus 1 mm, not their sum; a gap shorter than an estimated spread is a WARN. The slice decides: exit -101 is a real clash |
| PETG part with PLA loaded too (or the reverse) | `dual-material-interface`: the other material as interface only, map 1,2. The process interface makes the plate two-filament: every supported part sits at x >= 20.5 plus its interface spread |
| Two filaments by layer (prime tower) | `wipe_tower` "auto" finds a spot both nozzles reach or FAILs naming the blocker. Size: the tallest part's (41 mm plus an 8 mm brim at 218 mm) with the rib wall, then 35 mm without it, narrower only if nothing fits. 22 mm without the rib wall is faster: offer it (a spot with `width` 22, or `prime_tower_width` in `process.set`), never default to it: a tall narrow tower can topple |
| Two filaments, several single-colour objects | by object with `group_by_filament` (no tower); sweep `--by-object` to confirm; loses on many tiny parts; most volume on the left nozzle |
| Orientation unclear | `"orientation": "auto"`, or `sweep --orientations PART [--source studio]`; load direction first, then supports and finish, then time |
| Other needs (PLA, time, finish, colour, shims, thin walls) | the recipe whose "When" matches in `shambu recipes`; finish and fine layers per part only; colour in horizontal bands; one filament loaded: `pause_at_z`; TPU on filament 1 |
| Several bodies in one STL; bigger than the bed | `shambu split`; the user cuts it in Studio or CAD |
| Studio exit -101 | quote the two objects it names. Under a 1 mm gap neither a pass nor a -101 is proof (not repeatable). If the user accepts the GUI's warning: `--no-check`; the hand-back quotes `Checks skipped` |
| Exit -100, -102, -104, -21 | -100: quote the stderr line ("No valid nozzle found": `patch --repair-nozzle-stats`); -102: right-nozzle work at x < 20.5; -104: off the bed; -21 (only from `shambu studio` or a GUI file with arrange on): Studio's arrange could not fit the parts (24 mm apart with trees): give `at` |
| Exit -139 (crash) | run the same command once more; a second -139: report it with the log path, do not work round it |

Reasons and numbers: `references/06-slicer-rules.md`, `references/08-optimisation.md`.

By object, shambu keeps 60 mm between objects and closes the row after one over 32.8 mm (reference 08 section 2).
It never runs Studio's arrange and never rotates a part to pack it.

## Never

- Write a scalar into an X2D speed or acceleration key: they are six-slot per-nozzle arrays.
- Set supports, brims or infill for the whole plate when one part needs them: per object.
- Guess an orientation for a part exported in an assembly frame: `shambu orient`, then `lay_on_face`.
- Send keystrokes to Studio, press Print, or publish anything but one `pushall` to the printer.
- Reuse a spec's filaments from an earlier day: Studio's swap on open resets the process to the stock preset.

## Reading the printer

`printer filaments` and `status` send one `pushall`. `status`, `watch` and the log never show the IP; the masked
serial appears on stdout only. To measure a print: `printer watch --wait-for-start --log print.jsonl --gcode3mf
plate.gcode.3mf` until FINISH, then `calibrate --log print.jsonl --add`. A calibrated time needs three qualifying
prints of one printer model and material (`references/06-slicer-rules.md` section 11).

## References (read when the row says so)

| File | What | Read when |
|---|---|---|
| [00-findings.md](references/00-findings.md) | Every rule the CLI encodes, one paragraph each, with the Studio source line | something fails (section 10: the 0.4.0 rules), or before changing the library |
| [02-binary-crash.md](references/02-binary-crash.md) | Studio's exit codes and crashes | Studio exits non-zero |
| [03-3mf-format.md](references/03-3mf-format.md) | The 3MF entries, re-slicing a CLI export, the edit verbs | patching or editing a project |
| [04-app-printer-control.md](references/04-app-printer-control.md) | The printer report, the watch log | reading or logging a print |
| [06-slicer-rules.md](references/06-slicer-rules.md) | Reasons behind the recipes; X2D limits; the prime tower; Studio's estimate against the printer | choosing a recipe, a tower, explaining a time |
| [08-optimisation.md](references/08-optimisation.md) | Orientation, print by object, supports, the layout checker, headless metrics, the GUI workflow | optimising a plate, explaining a check line |
| [07-print-recipes.md](references/07-print-recipes.md) | Key scopes, defaults, the tip table | adding a recipe or a per-object key |
| [09-user-notes.md](references/09-user-notes.md) | The user's own finish and fit notes | the hand-back for this user |
| [01-source-cli.md](references/01-source-cli.md), [05-bambu-cli-ideas.md](references/05-bambu-cli-ideas.md) | CLI source, other tools | changing the code |
