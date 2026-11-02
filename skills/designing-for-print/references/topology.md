# Topology maps: `shambu topo`

`shambu topo` computes a stiffness map of a part: where material carries the load cases you give and where it
works little. It is a design aid. It is a map, not a proof: it never says a part is strong enough, and every result
carries findings and a not-checked list that say so.

## Contents

1. Install and verbs
2. When to use it in the design loop
3. The spec (`shambu.topo/1`)
4. Selections
5. The method
6. The run files and `run.json`
7. Tracing windows (`topo trace`)
8. Findings
9. Not checked
10. Runtime and pitch
11. Background reading

## 1. Install and verbs

| Verb | Needs | Does | Exit |
| --- | --- | --- | --- |
| `shambu topo check SPEC.json` | base install | Validates the spec: one error line per problem; the grid size against the cell cap | 0 valid, 1 errors |
| `shambu topo voxelise SPEC.json [--pitch MM] [--max-cells N]` | `topo` | Grid, cells, voxel against mesh volume, pieces, open-mesh columns, what each selection holds. No solve | 0, 1 a selection is empty with no fallback |
| `shambu topo solve SPEC.json --out DIR [--volfrac F]... [--pitch MM] [--maxit N] [--tol T] [--budget-min M] [--full-only] [--mesh auto\|cubes\|smooth\|none] [--max-cells N]` | `topo` (`smooth`: `topo-mesh`) | Full-density check, then one optimisation per volume fraction; writes the run files | 0 (WARN allowed), 1 a FAIL finding |
| `shambu topo validate [--size small\|paper]` | `topo` | The Michell cantilever: shape bands and a monotone objective | 0 pass, 1 fail |
| `shambu topo trace RUN.json --faces FACES.json [--rules conservative\|aggressive\|RULES.json] [--volfrac F]` | `topo-trace` | Window polygons per wall face, with clearances and stage areas | 0, 1 a window inside its keep-out |

Exit 2 is a usage error, a flag out of its range included (`--pitch 0`, `--maxit 0`, `--tol nan`); exit 3 an
environment error: a missing extra (`TopoDependencyError`, with the install line) or a file that does not read or
write. A spec, faces or rules file that breaks its schema is exit 1 with one line per error, and so is a name used
twice. `--json` prints one document, always strict JSON: a NaN or an infinity becomes null and a
`topo.non_finite` FAIL.

Size: a domain over 30000 cells is refused before anything is allocated (exit 1), with a memory estimate: a solve
measured about 0.18 MB a cell at 50000 cells on a solid block, so the default cap is about 5.5 GB. An STL's bounding
grid may hold 50 times the cap, the degrees of freedom 12 times and the filter weights 500 times. `--max-cells N`
raises every cap with it on a machine that has the memory; a coarser pitch is the other way out.

Install lines (quote them from the error; never run `pip install` for the user):

```
uv tool install --force 'shambu[topo] @ git+https://github.com/Shavvimal/shambu@v0.9.0'
uv tool install --force 'shambu[topo-trace] @ git+https://github.com/Shavvimal/shambu@v0.9.0'
uv tool install --force 'shambu[topo-mesh] @ git+https://github.com/Shavvimal/shambu@v0.9.0'
```

## 2. When to use it in the design loop

1. The part exists and passes `shambu cad gate`. Export its STL in the part's own frame.
2. Write the spec from the user's load cases. Every force needs a source (a weight, a motor torque, a measured
   pull). Ask once for any missing case; never invent a load.
3. `shambu topo check`, then `shambu topo voxelise`. Read the volume ratio and the selection counts before a solve.
4. `shambu topo solve ... --out topo/<part>@<rev>`. Start at a coarse pitch (3 to 4 cells across the thinnest wall
   is the floor) and one volume fraction.
5. Read the findings first, then the region table. A low region is a candidate, not a decision.
6. Change the CAD part by hand (a pocket, a window, a thinner rib) in ClassCAD. The map never edits the model.
7. Gate the new export with `shambu cad gate`, then run `shambu printability` and a test print. Any strength claim
   comes from a test or a stress analysis, never from the map.

## 3. The spec (`shambu.topo/1`)

```json
{
  "schema": "shambu.topo/1",
  "units": "mm-N-MPa",
  "domain": {"stl": "out/bracket@B.stl", "pitch": 2.0, "subsamples": 3},
  "material": {"E": 2100, "nu": 0.38, "density_g_cm3": 1.27, "fill": 0.7},
  "simp": {"penal": 3, "emin_rel": 1e-9, "rmin": null, "rmin_cells": 2.5, "move": 0.2, "eta": 0.5,
           "volfrac": [0.5, 0.7], "maxit": 80, "tol": 0.01, "budget_min": 40, "min_member_mm": 4},
  "supports": [{"name": "wall plate", "nodes": {"box": {"x": [-0.1, 0.1]}}, "dofs": "xyz"}],
  "passive": [{"name": "bolt boss", "cells": {"cyl": {"axis": "z", "c": [12, 8], "r": 5, "range": [0, 10]}}}],
  "cases": [
    {"name": "hook load", "weight": null,
     "loads": [{"cells": {"ball": {"c": [60, 8, 4], "r": 3}}, "grow_if_empty": 1.0,
                "force": [0, 0, -20], "moment": [0, 0, 0], "about": "centroid"}]},
    {"name": "own weight 3 g", "body_force": {"accel_g": [0, 0, -3], "scale_with_density": true}}
  ],
  "weights": "normalised",
  "regions": [{"name": "web", "cells": {"box": {"x": [20, 50], "z": [2, 8]}}}]
}
```

| Key | Meaning | Default |
| --- | --- | --- |
| `domain.stl` / `domain.box` | a closed STL (path relative to the spec), or `{"min", "max"}` for a solid block; exactly one | - |
| `domain.pitch` | cell size, mm | required |
| `domain.subsamples` | samples per cell edge for the voxeliser (1 to 7) | 3 |
| `material.E`, `nu` | modulus (MPa) and Poisson's ratio. The defaults are a generic PETG order of magnitude; state your own | 2100, 0.38 |
| `material.density_g_cm3`, `fill` | for self-weight only: density times the share of plastic | 1.27, 1.0 |
| `simp.penal` | SIMP penalty p | 3 |
| `simp.rmin` / `rmin_cells` | filter radius in mm, or in cells when `rmin` is null | null / 2.5 |
| `simp.volfrac` | design volume fractions, each in (0, 1) | [0.5] |
| `simp.move`, `eta` | update move limit and damping exponent | 0.2, 0.5 |
| `simp.maxit`, `tol`, `budget_min` | iteration limit, change tolerance, time budget per run | 80, 0.01, 40 |
| `simp.min_member_mm` | the thinnest member your printer makes as a member, for `topo.filter_small` | null |
| `supports[].dofs` | the fixed directions, any of `xyz` | `xyz` |
| `passive` | cells kept at density 1 (bosses, faces, insert pockets) | none |
| `cases[].loads[]` | `cells` (the nodes of the cells) or `nodes`; `force`, `moment` (N, N mm); `about`: `"centroid"` or a point where the force acts | - |
| `cases[].body_force` | acceleration in g; `scale_with_density` makes the load follow the material | none |
| `weights` | `normalised`: each case weighs `(1/n) / c_k(full)`; `raw`: the case `weight` values (default 1) | `normalised` |
| `regions` | named cell sets for the region table and `topo.unloaded_region` | none |

## 4. Selections

One primitive or a combination. Coordinates are in the STL's frame; an axis left out of a box is unbounded.

| Form | Example |
| --- | --- |
| box | `{"box": {"x": [0, 10], "abs_y": [21, 38]}}` (`abs_*` bounds the absolute value: two mirrored boxes) |
| ball | `{"ball": {"c": [x, y, z], "r": 3}}` |
| cylinder | `{"cyl": {"axis": "z", "c": [a, b], "r": 4.5, "range": [lo, hi]}}` (`c` in the other two axes, x, y, z order) |
| annulus | `{"annulus": {"axis": "z", "c": [a, b], "r_in": 3, "r_out": 6, "range": [lo, hi]}}` |
| half-space | `{"half": {"p": [x, y, z], "n": [nx, ny, nz]}}` (the side the normal points to) |
| combine | `{"union": [...]}`, `{"intersect": [...]}`, `{"minus": [a, b]}` |

`grow_if_empty` on a load grows every primitive by that many mm when the selection holds nothing; the run then
carries `topo.load_fallback`. Without it an empty load is a spec error (exit 1).

## 5. The method

- **Domain.** Rays run along +z through a `subsamples x subsamples` lattice per cell; sorted hits pair into inside
  intervals by parity; a cell is kept when at least half its samples lie inside. Ray origins sit a small fixed
  offset off round numbers. The largest face-connected piece is kept. The domain is the part as it is, so the map
  can only remove material.
- **Elements.** One linear 8-node brick per cell, 2 x 2 x 2 Gauss points, isotropic linear elasticity. A cell of
  side h and modulus E has stiffness `E h k0`, with `k0` the unit cube at E = 1.
- **Interpolation (SIMP).** `E(rho) = Emin + rho^p (E0 - Emin)`, `Emin = emin_rel E0`.
- **Objective.** `J = sum_k w_k c_k`, `c_k = F_k^T U_k` (compliance, N mm). One sparse LU factorisation per
  iteration solves every case.
- **Sensitivity.** `dc_k/drho_e = -p rho_e^(p-1) (E0 - Emin) u_e^T k0 u_e + 2 u_e^T f_b` (the second term only for
  self-weight that scales with density). A positive value is clipped to -1e-30 because the update needs negative
  ones; the count is `topo.sens_clipped`.
- **Filter.** `rho_e = sum_j H_ej x_j / sum_j H_ej`, `H_ej = max(0, rmin - |x_e - x_j|)`; the gradient goes back
  through the same weights. Passive cells read 1.
- **Update (optimality criteria).** `x_new = clip(x (-dJ/dx / (lambda dV/dx))^eta, x - move, x + move, 0, 1)`,
  lambda by bisection so the mean physical density of the design cells equals the volume fraction. The upper bound
  starts at the largest sensitivity ratio and widens until the volume falls to the target, so the load scale does
  not matter. Stop when no design variable changes by more than `tol`, at `maxit`, or when the next iteration and
  the final analysis would pass the budget. A run is `converged` only when its volume is also within 0.005 of the
  target; else `topo.volume_missed` says by how much, and whether the passive regions put the target out of reach.
- **Loads.** A force and a moment spread over their nodes as a rigid insert would: `f_i = F/n + w x r_i` with
  `A w = M`, `A = sum(|r|^2 I - r r^T)`. A force acting at a point (`about`) moves to the centroid as a force and a
  couple. Supports clamp the selected directions of every selected node.
- **Checks.** Before any factorisation the supports must hold all six rigid-body modes (three translations, three
  rotations); a mode left free is `topo.under_constrained` (FAIL, no run), and so is a negative or non-finite
  full-density compliance. At full density, reactions must balance loads and moments to 1e-6 relative, and fixed
  displacements must read 0 (`topo.equilibrium`, FAIL). After each run every passive region must read 1
  (`topo.passive_not_one`, FAIL). These two FAILs mean a bug, not a design result. A case whose loads all sit on
  fixed nodes has zero compliance and is a spec error under either weighting, and so are passive regions that cover
  the whole domain.

## 6. The run files and `run.json`

`solve --out DIR` writes:

| File | Content |
| --- | --- |
| `run.json` | the document `--json` prints (`shambu.topo.run/1`) |
| `density_<tag>.npy` | float32 densities, shape `grid.shape`, indexed x, y, z; NaN outside the domain; cell centre at `origin + pitch * (index + 0.5)` |
| `passive.npy` | int16: 0 for a design or outside cell, i + 1 for the i-th entry of `passive` |
| `regions.md` | the region table per volume fraction, and the passive means |
| `iso_<tag>.stl` | the 0.5 iso-surface: cell faces (`cubes`) or marching cubes (`smooth`, `topo-mesh`) |

The tag is `v` and the volume fraction's decimal digits, at least two: `v50` for 0.5, `v05` for 0.05, `v401` for
0.401. Two fractions never share a file; a fraction given twice is an error.

`run.json` keys: `grid`, `domain` (cells, voxel and mesh volume, ratio, pieces, dropped, odd columns, open edges,
inverted, surface share),
`fe` (elements, nodes, DOF, free, fixed), `settings`, `cases`, `passive`, `full_density` (per case: compliance,
load, reaction, residuals, `ok`), `runs` (per volume fraction: iterations, `converged`, `budget_stop`,
`final_change`, `objective_drift_last20`, `objective` history, `compliance`, `ratio` to full density,
`design_volume_fraction`, `clipped_sensitivities`, `passive_means`, `regions`, `low_clusters`,
`skin_solid_next_to_passive`, `files`, `seconds`, `timings`, `peak_rss_mb`), `versions`, `findings`,
`not_checked`, `counts`.

Read it in this order: findings, `full_density[].ok`, `runs[].converged`, `ratio`, then the regions. Under fixed
loads a `ratio` of 1.46 means 46 % more deflection under that case at that volume, not 46 % more stress. With
self-weight that scales with density the lighter part also carries less load, so the ratio is a compliance ratio
only, not a deflection ratio.

## 7. Tracing windows (`topo trace`)

`FACES.json` (`shambu.topo.faces/1`):

```json
{
  "schema": "shambu.topo.faces/1",
  "build_axis": "z",
  "keepouts": [{"name": "screw head", "c": [12, 8, 12], "r": 4}],
  "faces": [{"name": "side wall", "axis": "y", "slab": [0, 2], "u": [5, 75], "v": [0, 30],
             "open_edges": ["v_hi"], "keepouts": []}]
}
```

`axis` is the wall normal; `slab` the wall's extent along it; `u` and `v` the face rectangle in the other two axes
(x, y, z order); `open_edges` the face edges that meet air. A window polygon is in those two axes, world mm.

Rules (`--rules`):

| Rule | conservative | aggressive |
| --- | --- | --- |
| `threshold` (slab-mean density) | 0.5 | 0.5 |
| `rib_mm` | 5 | 4 |
| `rim_mm` | 5 | 3 |
| `corner_r_mm` | 2.5 | 2 |
| `clip_r_mm` | none | 3 |
| `clearance_mm` | 3 | 2 |
| `margin_mm` | 0.5 | 0.5 |
| `min_area_mm2`, `min_width_mm`, `max_vertices` | 80, 6, 60 | 40, 5, 60 |
| `apex_mm`, `roof_deg`, `flat_report_deg` | 1, 45, 43 | 1, 45, 43 |
| `other_grow` | rib | rim |

A rules file (`shambu.topo.rules/1`) names a `base` preset and overrides any key. Pick the map's run with
`--volfrac` (or `keep_volfrac` in the file). Ranges: `threshold` and `keep_volfrac` in (0, 1), `raster_mm` above 0,
`roof_deg` in (0, 90], `flat_report_deg` in [0, 90], `max_vertices` at least 3, `rib_passes` at least 1, every
other length at least 0. A face raster over 4 million cells is an error: raise `raster_mm`.

A window never holds an island: where low density rings solid material, a strip `rib_mm` wide through the island,
along the build axis, splits the window and keeps the island joined to the wall. Passive cells up to the clearance
reach past the face rectangle are kept clear too. When run.json lists passive regions, `passive.npy` must sit
next to it (exit 3 otherwise).

Per window the output gives `poly`, `area_mm2`, `removed_mm3`, `mean_density`, `roof_flat_mm`, `clearance` (mm
and the nearest passive region or keep-out), `n_vertices` and `simplify_tol`; per face, `stage_areas_mm2` (after
each step) and the dropped windows with the reason. Writing the windows into a CAD script stays with the user.

## 8. Findings

Every solve, validate and trace result carries `topo.map_not_proof`, `topo.stiffness_not_strength` and
`topo.pitch_dependent`. `check` and `voxelise` produce no map, so they carry no standing findings and no
not-checked list. Only the five FAIL codes change the exit code.

| Code | Severity, evidence | When | What to do |
| --- | --- | --- | --- |
| `topo.map_not_proof` | WARN heuristic | always | Report it. Strength comes from a test or a stress analysis |
| `topo.stiffness_not_strength` | WARN heuristic | always | Quote ratios as stiffness loss, never as stress |
| `topo.pitch_dependent` | INFO heuristic | always | Compare runs at one pitch only |
| `topo.rigid_load_patch` | INFO heuristic | any load | A soft contact concentrates more than the rigid spread |
| `topo.clamped_support` | INFO heuristic | a support fixes x, y and z | Results next to it are optimistic |
| `topo.not_converged` | WARN measured | change still above `tol` at `maxit` | Read the drift; raise `--maxit` or accept a rough map |
| `topo.budget_stop` | WARN measured | the time budget stopped the run | A partial run: coarser pitch or a larger budget |
| `topo.volume_missed` | WARN measured | design volume more than 0.005 from the target | The map answers another volume; with passive regions, read `min_volume` |
| `topo.voxel_volume` | WARN measured | voxel / mesh volume outside 0.98 to 1.02 | Finer pitch; thin walls are lost |
| `topo.odd_parity` | WARN measured | ray columns with an odd hit count | The mesh is open: re-export it closed |
| `topo.open_mesh` | WARN measured | welded edges used by one facet or by more than two | The mesh is open or non-manifold: re-export it closed |
| `topo.inverted_mesh` | WARN measured | the signed mesh volume is negative | Facets wind inward: fix the export before other tools read it |
| `topo.dropped_pieces` | WARN measured | cells outside the largest piece | A loose body or a gap thinner than a cell |
| `topo.thin_walls` | WARN heuristic | over half the design cells within 3 cells of the surface | Finer pitch; the map has no room |
| `topo.filter_small` | WARN heuristic | rmin under 1.5 x pitch, or under `min_member_mm` | Raise `rmin` |
| `topo.unloaded_region` | WARN measured | a region with under 0.1 % of the strain energy in every case | Low because idle, not spare: add the case that loads it |
| `topo.inert_case` | WARN measured | a case's compliance ratio within 1 % of 1 | It loads only passive material; it shapes nothing |
| `topo.case_dominates` | WARN measured | `raw` weights and one case over 80 % of the objective | Use `"weights": "normalised"` |
| `topo.sens_clipped` | WARN measured | positive sensitivities in the last iteration | Self-weight: the update ignored where mass hurts |
| `topo.load_fallback` | WARN measured | a load selection grew | Check the load sits where you meant |
| `topo.equilibrium` | FAIL measured | reactions do not balance loads at full density | A model or solver bug: stop, report |
| `topo.passive_not_one` | FAIL measured | a passive region below density 1 | A solver bug: stop, report |
| `topo.under_constrained` | FAIL measured | the supports leave a rigid-body mode free, or the stiffness is singular | Add supports that hold every direction and rotation |
| `topo.non_finite` | FAIL measured | a NaN or an infinity in the result (now null) | No number to act on: report it with the spec |
| `topo.trace_rules_dominate` | WARN heuristic | the rules removed over half the candidate area | The rules, not the map, set the windows |
| `topo.roof_bridge` | WARN measured | a window roof flatter than 43 degrees beyond the apex | Check the bridge in the slicer |
| `topo.clearance` | FAIL measured | a window at or under `clearance_mm` from a keep-out | The trace broke its own rule: report it |

## 9. Not checked

Every result lists these, and a reply must repeat them:

- strength: stress, yield and fracture (the objective is stiffness);
- buckling, large deflection, fatigue and creep (the model is linear and static);
- print anisotropy: layer adhesion, walls and infill (the material is solid and isotropic);
- the real joints: bolts, contact and preload (supports are clamps, loads rigid patches);
- the redesigned part (the map is not the part);
- printability of the result (`shambu printability` on the new export).

## 10. Runtime and pitch

The factorisation dominates each iteration and grows about with the square of the DOF count. As a guide on a
laptop-class machine: about 10 k elements solve in about 0.5 s per iteration, about 37 k elements in about 4 s, and
about 80 k elements in about 15 s. Halving the pitch multiplies the cells by 8. Start coarse, read the findings,
refine only the run you will act on. `solve` records `seconds`, per-phase `timings` and `peak_rss_mb` per run.

`shambu topo validate` runs the 24 x 1 x 8 Michell cantilever in about a second; `--size paper` runs 60 x 4 x 20
cells. Both check the top and bottom chords at the clamp (above 0.6), the middle of the clamp (below 0.1) and a
monotone objective after iteration 5.

## 11. Background reading

Michell (1904) for the cantilever shape; Bendsoe and Kikuchi (1988) and Bendsoe (1989) for density methods; Zhou
and Rozvany (1991) and Bendsoe and Sigmund (1999) for SIMP; Sigmund (2001) and Andreassen et al. (2011) for the
compact 2D codes; Liu and Tovar (2014) for the 3D form; Bruns and Tortorelli (2001) and Bourdin (2001) for the
density filter; Bruyneel and Duysinx (2005) for self-weight. Not used here: level sets, BESO, stress constraints and
overhang filters inside the solver (the print rule lives in `trace`).
