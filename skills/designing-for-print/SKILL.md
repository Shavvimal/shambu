---
name: designing-for-print
description: Builds, checks and revises 3D-printable parts in ClassCAD through the ClassCAD MCP, and gates each exported STL with the shambu cad commands before it reaches a plate. It plans from a parameter table and a print-intent block (material, inserts, hole allowance, bed face, critical tolerances), builds with parametric recipes, proves volume and features with numbers, exports STL and STEP, and keeps a file only when its gates pass: one closed solid, volume and size against expectation, hole and insert-pocket fits, part-to-part clearance, insertion and rotation sweeps, and a stale-export guard. Between revisions it diffs the old and new exports and names what changed, which plate to reslice and which fit needs a coupon print. Use when the user wants a part designed or changed for 3D printing in ClassCAD, asks whether a CAD export is fit to print, wants fit, clearance or clash checks on exported parts, or asks what changed between two revisions. Not for slicing or plates (the shambu skill) or machined parts.
argument-hint: "[part description, drawing path or cad.json] [print notes]"
allowed-tools: Bash(shambu *), Read, Write, mcp__plugin_classcad_classcad__*
---

# designing-for-print: a part is done when its export passes `shambu cad gate`

A part is done when its exported file passes `shambu cad gate`, not when the script says so. The ClassCAD MCP
builds; `shambu cad` judges the files it exports. Request: $ARGUMENTS

Load the classcad skill's rules for the MCP itself (sign-in, `run_script`, one bulk `docs` call, recipes,
verification). This skill adds the print intent, the gates and revisions.

## Standing rules

1. Before the first `shambu cad` command of a task: `shambu --version` (it must print: shambu 0.8.0). Another
   version or "command not found": stop and give the fix (`/shambu:setup update`). `clearance`, `sweep` and
   `diff --surface` need the geom extra. On exit 3 with `GeomMissing`, quote the install line from the error and
   stop that check; never run `pip install` yourself.
2. Every number in the model comes from the parameter table or the intent block in `cad.json`. For dimensioned
   input use ClassCAD's constrained-sketching recipe, never a hard-coded coordinate layout.
3. Judge the exported file, not the script. A script's own expected volume is a claim; `shambu cad check` is the
   judge. Quote finding codes and numbers as printed; never paraphrase a number.
4. A gate `pass` proves only the rules that ran. Report the `not checked` list every time. An item that was not
   checked is "not checked", never "passed". If your notes name an open gap, report it.
5. Never overwrite a good export with an unchecked one. Export to `out/.candidate/`, gate, then
   `shambu cad check ... --promote out/<part>@<rev>.stl`. `--promote` writes only a new file: give each revision a
   new name.
6. Never `clear` a working model to try something: `checkpoint`, try, `restore`.
7. Re-stamp after every re-export. A `cad.stale.*` finding means the file and its record disagree: export again,
   stamp again, gate again. Never say "it passed earlier".

## Workflow (copy this checklist into the reply)

- [ ] 1. Plan. Write `cad.json` before the first `run_script`: the parameter table (name, value, unit, source) as
      your notes, the `intent` block and the `fits` rows (`references/print-intent.md`). Ask once, in one batched
      question with a proposed default, for any intent item the brief does not give, or record it as an assumption.
- [ ] 2. Fetch the ClassCAD docs once: the build recipe chosen by input type, always `recipes/verification`, and
      every method the build needs, in one `docs` call.
- [ ] 3. Build in stages: body, then holes and pockets, then fillets and chamfers. End each stage with mass
      properties against the table's own arithmetic. Check the volume drops after every cut (the engine reports
      success for a cut that did nothing). `checkpoint` the first valid body.
- [ ] 4. Verify in the session: one current solid, non-null mass properties, bounds, the key holes by geometry
      probes, renders last (and the classcad skill's mirror check for any image input). Snippets:
      `references/classcad-scripts.md`.
- [ ] 5. Save the final script to `parts/<part>.js` and the parameters to `params.json` with `Write`. These two
      files are the build id's inputs.
- [ ] 6. Export candidates to `out/.candidate/`: the STL (with the faceting you will stamp) and a STEP.
- [ ] 7. Re-import the saved STEP in the session (checkpoint, import, measure, restore) and keep its volume.
- [ ] 8. Stamp: `shambu cad stamp out/.candidate/<part>.stl --part <part> --rev <rev> --input parts/<part>.js
      --input params.json --cad-volume V --cad-cog X,Y,Z [--cad-area A] --step out/.candidate/<part>.stp
      --step-volume V2 --faceting-tol T --angle-tol D --bed-face NX,NY,NZ`.
- [ ] 9. Gate: `shambu cad gate cad.json`. Exit 1: fix in ClassCAD (back to step 3), export, stamp and gate again.
      Exit 3: the environment; quote the error. Read `references/gates.md` for a code you do not know.
- [ ] 10. Keep only on a pass: `shambu cad check out/.candidate/<part>.stl --record out/.candidate/<part>.cad.json
      --promote out/<part>@<rev>.stl`. Save the STEP under the same revision name. Give the user the script path.
- [ ] 11. Revision (when the part existed): write `expect.json` from the user's request before the change, then
      `shambu cad diff out/<part>@<old>.cad.json out/<part>@<new>.cad.json --spec cad.json --plate plate.json
      --expect expect.json` (`references/revisions.md`).
- [ ] 12. Print hand-off: `shambu printability out/<part>@<rev>.stl --lay-on-face=<bed face>`, then the shambu skill
      builds the plate. Pass the record's `bed_face` as the spec part's `lay_on_face`.
- [ ] 13. Report with the template below.

## Report (a gate: every build, check or revision reply uses it)

```
Part: <name> rev <rev>   Files: <stl path> (gate <pass|fail|incomplete|stale>), <step path> [<STEP export as saved>]
Build id: <id>   CAD volume <v> mm3, STL <v> mm3, STEP re-import <v> mm3
Intent: <material>, bed face <n>, allowance v <a> / h <a> mm   Assumed (not confirmed): <items or none>
Findings (by part, then rule):
  <part>  <SEVERITY> <code>  <message as printed>  [<evidence>]  Fix: <fix text>
Fidelity (against the user's numbers, separate from the gates): <each critical dimension: measured how, value>
Revision (when diffed): <changes as printed>; reslice: <plates>; coupons: <fits>; unexpected: <none | codes>
Not checked: <the gate's not_checked list, plus anything this build did not verify>
Next: <one action: fix X in ClassCAD | shambu printability ... | build the plate with the shambu skill>
```

Separate parts never share a line. Gate findings, fidelity to the user's drawing and printability stay separate
blocks. There is no overall score and no "ready to print".

Standing warnings for every report with a fit:
- The first print of any new fit is a coupon, in the feature's own orientation (`fit.coupon`).
- Holes print undersize: about 0.1 mm vertical, 0.2 to 0.3 mm horizontal. The allowance lives in the model
  (`intent.hole_allowance_mm`); the shambu process for functional PETG uses `no-hole-compensation`.

## Gotchas

- The MCP `save` tool takes only a format and a path. Finer STL faceting needs `run_script` with the
  `common.save` STL options; that path is unverified on a remote worker. If it throws, use the MCP `save` and stamp
  `--faceting-tol 0.1 --angle-tol 6` (the `common.save` defaults; that the MCP `save` uses them is unverified).
- Analytic STEP options (`stp.version`, `stp.analytic`) are documented but unverified: one run threw on any `stp`
  option on a worker; untested on the local engine. Probe once per engine; on a throw, save plain STEP and say so
  in the report's Files line.
- A STEP re-import of garbage reports success with no solid. Confirm the import has a solid child before you
  measure it. A file path must be reachable by the engine process (a remote worker cannot read your disk).
- Mass properties reject feature ids and crash on an empty part: pass the part or solid id. The method doc lists
  only `volume` and `cog` in the result: pass `--cad-area` only when a call you ran reported an area.
- The engine reports success for a cut or a pattern that did nothing: assert the volume change after each one.
- The graphic mesh is coarse (about 0.1 model units): probe the B-rep for small features, never the render.
- `graphicBounds` is approximate and ignores instance transforms; `shambu cad check` reads the exact STL box.
- In-session interference: the `solid.intersection` doc says a disjoint pair destroys the target. This skill has
  not run it in a part session (unverified), so `checkpoint` first and `restore` after. Prefer
  `shambu cad clearance` on the exports.
- A `pass` with a rule `not-checked` (skipped, or geom missing) proves nothing about that rule. Say which.
- `cad.incomplete.*` is not a pass, and only some causes are inputs. Fix the spec or the files for
  `selector_empty`, `mesh_missing`, `unreadable`, `no_record` and `no_holes`. Fix the model for `no_volume` and
  `wall_unmeasured`: the files are fresh, so a re-export alone changes nothing. Table: `references/gates.md`.

## Commands

```
shambu cad stamp PART.stl --part NAME [--rev LABEL] [--input FILE]... [--cad-volume MM3] [--cad-area MM2]
       [--cad-cog X,Y,Z] [--step FILE] [--step-volume MM3] [--faceting-tol MM] [--angle-tol DEG] [--bed-face NX,NY,NZ] [-o REC]
shambu cad check PART.stl [--record REC] [--expect-volume MM3 --volume-tol MM3] [--expect-size X,Y,Z --size-tol MM]
       [--density G_CM3] [--promote NEW.stl] [--spec cad.json --part NAME]
shambu cad features PART.stl [--axis-tol DEG] [--min-diameter MM] [--spec cad.json --part NAME]
shambu cad fit PART.stl --hole NAME:FIT:X,Y,Z:AX,AY,AZ ... [--fits fits.json] [--spec cad.json --part NAME]
shambu cad clearance --spec cad.json [--rule ID]... [--pose NAME]          # geom extra
shambu cad sweep --spec cad.json [--rule ID]... [--no-cull]                # geom extra
shambu cad diff OLD NEW [--spec cad.json] [--plate plate.json] [--expect expect.json] [--sections N] [--surface]
shambu cad gate cad.json [--only ID]... [--skip ID]...
```

`--json` gives one JSON document. Exit 0 pass (WARN and INFO allowed), 1 fail, incomplete or stale (or an invalid
spec), 2 usage, 3 environment (a file that does not read, a target that cannot be written, the geom extra missing).

## References (read when the row says so)

| File | What | Read when |
|---|---|---|
| [gates.md](references/gates.md) | Every verb, the spec schema, every finding code with its fix, gate words, exit codes | writing `cad.json`, or a code needs explaining |
| [classcad-scripts.md](references/classcad-scripts.md) | The `run_script` steps for verify, export, STEP re-import and in-session overlap, with what is unverified | steps 4, 6 and 7 |
| [print-intent.md](references/print-intent.md) | The intent checklist, the ISO 273 table, inserts, allowances, coupons, tolerances | step 1, or the user asks about fits |
| [revisions.md](references/revisions.md) | Revision names, `expect.json`, reading `cad diff`, reslice and coupon decisions | step 11 |
