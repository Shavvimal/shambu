# ClassCAD session steps for printable parts

Contents
1. How to read this file
2. Verify in the session (step 4)
3. Export candidates (step 6)
4. Re-import the STEP and measure it (step 7)
5. A quick in-session overlap look
6. What stays unverified

## 1. How to read this file

Each step names the ClassCAD method doc it depends on. Fetch those docs in the one bulk `docs` call of workflow
step 2 and write the script from the docs, not from memory: argument shapes change between releases. The outlines
below say what to do and what to check; they are not copy-paste code. Every row marked "unverified" was read in
the docs and not run for this skill.

MCP tools used: `run_script`, `docs`, `checkpoint`, `restore`, `save`, `snapshot`, `view`. Never `clear` a working
model.

## 2. Verify in the session (step 4)

| Check | How | Doc |
|---|---|---|
| One current solid | in `api.tree()`, count the `CC_Solid` nodes not consumed by a later feature; exactly one per part | `DATA` |
| Mass properties | `part.calculateMassProperties` on the part or solid id (never a feature id, never an empty part); keep the volume and the centre of mass (`cog` is an `{x, y, z}` object) for `shambu cad stamp`. Its doc lists no area in the result: give `--cad-area` only from a call that reported one | `v1.part.calculateMassProperties` |
| The table's arithmetic | after each cut, the volume must drop by about the cut's own volume; a no-op cut reports success too | `recipes/verification` |
| Bounds | `api.inspect.graphicBounds` on current solids only; approximate, ignores instance transforms (the STL check is exact) | `recipes/verification` |
| Key holes | probe the B-rep with `getGeometryIds` at the hole's expected radius; never measure the render (its mesh is coarse) | `recipes/verification` |
| Renders | `snapshot` last, after the numbers agree | MCP tool |

## 3. Export candidates (step 6)

Export to `out/.candidate/` so a failing file never replaces the last good one.

- STL, default: the MCP `save` tool with the STL format and a path. The `common.save` STL defaults are 0.1 mm and
  6 degrees; that the MCP `save` uses them is unverified. Stamp `--faceting-tol 0.1 --angle-tol 6`.
- STL, finer facets: `run_script` with `common.save` and its STL options (a faceting tolerance and an angle
  tolerance, plus a file). Unverified on a remote worker. On any throw, fall back to the MCP `save` and stamp its
  defaults. Doc: `v1.common.save`.
- STEP: the MCP `save` tool with the STP format. Analytic AP242 options (`stp.version`, `stp.analytic`) are
  documented but unverified; one earlier run threw on any `stp` option on a worker, and the local engine is
  untested. Probe once per engine: on success check the result's own success flag (not its level number); on a
  throw save plain STEP and write "STEP as saved (plain)" in the report.

## 4. Re-import the STEP and measure it (step 7)

1. `checkpoint`.
2. Import the saved STEP into the working part (`part.importFeature` with the file). The file must be readable by
   the engine process: a local engine reads your disk, a remote worker does not. Unverified for a remote worker.
3. Confirm the import created a `CC_Solid` child. A garbage file imports as a silent success with no solid.
4. `calculateMassProperties` on that solid id; keep the volume for `--step-volume`.
5. `restore` to the checkpoint.

Doc: `v1.part.importFeature`, `v1.part.calculateMassProperties`, `DATA`.

## 5. A quick in-session overlap look

For a fast look while modelling two parts of one session: `checkpoint`, copy both solids, intersect the copies
with `solid.intersection` keeping the tools, measure the result, then `restore`. A disjoint pair destroys the
target (an engine error, per the doc), which is why the checkpoint comes first. Unverified in a part session. Doc:
`v1.solid.intersection`. The gate that counts is `shambu cad clearance` on the exported STLs.

## 6. What stays unverified

| Behaviour | Status |
|---|---|
| STL faceting options through `common.save` on a remote worker | unverified |
| Analytic STEP options on any engine | unverified (one worker run threw) |
| STEP re-import by file path on a remote worker | unverified |
| In-session `solid.intersection` on part copies, and the disjoint pair destroying the target | from the doc, not run here |
| The MCP `save` tool's STL faceting (assumed the `common.save` defaults, 0.1 mm and 6 degrees) | unverified |
| A surface area from `calculateMassProperties` (its doc lists `volume` and `cog` only) | not in the doc |
| The tree contract for "current solid" (`CC_Solid`, consumed flag) | from the `DATA` doc, not run here |

When a run confirms or refutes one of these, say so in the report and keep the fallback.
