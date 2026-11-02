# shambu cad: verbs, spec, codes

Contents
1. What the gates do and do not do
2. The spec file (`shambu.cad/1`)
3. The record (`shambu.cad.record/1`)
4. Verbs
5. Gate words and exit codes
6. Finding codes and fixes
7. Tolerances and where they come from

## 1. What the gates do and do not do

`shambu cad` reads exported files (STL, a 3MF object) and the records `stamp` writes. It never starts or drives a
CAD program. Every finding has a code, a severity (FAIL, WARN, INFO) and an evidence label (`measured` for numbers
taken from the files; `observed` for shambu's standing print warnings). Every result lists what it did not check.

Not checked by any gate: strength under load, the printed size of a fit (a mesh is the CAD size), overhangs and
walls (`shambu printability`), and anything a STEP B-rep carries that a mesh does not.

## 2. The spec file (`shambu.cad/1`)

Paths are relative to the spec file. Unknown keys are listed as `cad.spec.unknown_key` WARN, never dropped silently.

```json
{
 "schema": "shambu.cad/1",
 "provenance": {"inputs": ["params.json", "parts/*.js"], "rev": "B"},
 "intent": {"material": "PETG", "density_g_cm3": 1.27, "process": "FDM 0.4 mm nozzle",
            "hole_allowance_mm": {"vertical": 0.1, "horizontal": 0.25}, "bed_face": [0, 0, -1],
            "general_tolerance_mm": 0.2, "critical": [{"what": "bore 22", "tol_mm": [0, 0.1], "why": "bearing press"}]},
 "fits": [{"name": "insert-M3", "kind": "insert", "hole_mm": 4.0, "band_mm": [-0.02, 0.02],
           "depth_min_mm": 6.0, "wall_min_mm": 1.6, "basis": "insert datasheet: pocket 4.0, length 5"}],
 "parts": [
  {"name": "base", "mesh": "out/base@B.stl", "record": "out/base@B.cad.json", "role": "rigid", "group": "frame",
   "expected_volume_mm3": 41980, "volume_tol_mm3": 60, "expected_size_mm": [62, 40, 24], "size_tol_mm": 0.05,
   "expected_features": [{"kind": "hole", "diameter_mm": 4.0, "count": 4, "pcd_mm": 30, "angles_deg": [45, 135, 225, 315]}],
   "holes": [{"name": "h1", "fit": "insert-M3", "mouth": [5, 5, 24], "axis": [0, 0, -1]}]},
  {"name": "lid", "mesh": "out/lid@B.stl", "role": "moving", "group": "cover",
   "placement": {"origin": [0, 0, 24], "x": [1, 0, 0], "y": [0, 1, 0]}},
  {"name": "screw", "mesh": "ref/m3x8.stl", "role": "reference"}
 ],
 "zones": {"seat": {"z": [23.9, 24.1]}},
 "poses": {"open": {"lid": {"origin": [0, 20, 24], "x": [1, 0, 0], "y": [0, 0, 1]}}},
 "rules": [
  {"id": "solids", "type": "check", "parts": "role:!reference"},
  {"id": "inventory", "type": "features", "parts": "part:base"},
  {"id": "holes", "type": "fit", "parts": "part:base"},
  {"id": "clash", "type": "clearance", "a": "role:moving", "b": "role:rigid", "margin_mm": 0.3,
   "exempt": [{"a": "part:lid", "b": "part:base", "zone": "seat", "reason": "lid rests on the rim", "max_overlap_mm3": 0.5}]},
  {"id": "insert", "type": "sweep", "moving": "group:cover", "fixed": "role:rigid", "motion": "translate",
   "axis": [0, 0, -1], "from_mm": 30, "step_mm": 1, "resolution_mm": 0.02, "margin_mm": 0.2, "contact_zone": "seat"},
  {"id": "hinge", "type": "sweep", "moving": "part:lid", "fixed": "role:rigid", "motion": "rotate",
   "point": [0, 20, 24], "axis": [1, 0, 0], "from_deg": 0, "to_deg": 110, "step_deg": 1, "resolution_deg": 0.05,
   "margin_mm": 0.5, "first_contact_after_deg": 105}
 ]
}
```

Rules for the spec:

- Selectors pick parts: `part:NAME`, `role:ROLE`, `role:!ROLE`, `group:GROUP`, `all`. Never a file name, so a
  renamed or mirrored mesh is still found by its role.
- Roles: `rigid` (the default; `cad.default_role` INFO says so), `moving`, `breakaway`, `reference` (a bought part:
  never checked as a solid, always a clearance partner).
- A placement is `{origin, x, y}` with orthonormal x and y. A mesh with no placement is in the shared frame as
  exported. A pose replaces the placement of the parts it names (`--pose NAME` or the rule's `pose`).
- `expected_volume_mm3` needs `volume_tol_mm3`; `expected_size_mm` needs `size_tol_mm`. No hidden defaults.
- Every exemption needs a non-empty `reason`. A missing reason is a spec error (exit 1).
- A sweep translation starts `from_mm` back along the axis and ends seated (the placement). Its
  `first_contact_after_mm` counts travel from the start. A rotation starts at `from_deg` (the placement is the part
  at 0 degrees); `first_contact_after_deg` is an angle.
- No expressions and no loops. One translation or one rotation per sweep rule.

## 3. The record (`shambu.cad.record/1`)

`shambu cad stamp` writes `<part>.cad.json` beside the STL: the build id over the declared inputs (each input keeps
the label it was hashed under), the STL SHA-256 and the faceting used, the mesh facts, the numbers the CAD session
reported (`cad.volume_mm3`, `cad.area_mm2`, `cad.cog`), the STEP path, SHA-256 and re-import volume, and
`print.bed_face`. Paths are relative to the record's folder.

## 4. Verbs

| Verb | Does | Needs the geom extra |
|---|---|---|
| `stamp` | writes the record (the one verb that writes besides `--promote`) | no |
| `check` | provenance, solid, volume, size, mass; `--promote NEW.stl` copies a passing STL to a new file and writes a new record beside it | no |
| `features` | hole and boss inventory, patterns (pitch-circle diameter and angles), against `expected_features` | no |
| `fit` | gauges declared holes against the fit table (built-in ISO 273 rows plus the spec's or `--fits` rows) | no |
| `clearance` | pairwise overlap and exact minimum distance, exemptions with reasons and caps | yes |
| `sweep` | translation or rotation to first contact, bisected to the resolution | yes |
| `diff` | measures, section profile, feature changes, plates to reslice, coupons, expected changes | `--surface` only |
| `gate` | every rule of the spec in order, one exit code | for clearance and sweep rules |

`--no-cull` on `sweep` and `gate` evaluates every point at every step: the slow reference path. It gives the same
first contact and step count as the default.

## 5. Gate words and exit codes

The gate word is derived from the findings: `stale` if any `cad.stale.*`; else `fail` if any other FAIL; else
`incomplete` if any `cad.incomplete.*`; else `pass`. The last text line reads
`cad gate: pass (0 FAIL, 2 WARN, 1 INFO; 2 rules not checked)`.

| Exit | Meaning |
|---|---|
| 0 | pass (WARN and INFO allowed) |
| 1 | fail, incomplete or stale; or an invalid spec or expectation file |
| 2 | usage: a flag for another verb, a malformed `--hole`, the wrong number of files |
| 3 | environment: a file that does not read, a record or `--promote` target that cannot be written or already exists, `GeomMissing` |

## 6. Finding codes and fixes

| Code | Severity | Means | Do |
|---|---|---|---|
| `cad.stale.build_id` | FAIL | the inputs hash to another build than the record says | export, stamp and gate again |
| `cad.stale.stl_sha` | FAIL | the STL changed after it was stamped | stamp again (after checking why it changed) |
| `cad.stale.step_sha` | FAIL | the STEP is missing or changed | export the STEP again, re-import, stamp |
| `cad.record_path` | WARN | the record names another STL path; the hash decides | none, or stamp the file where it lives |
| `cad.incomplete.selector_empty` | FAIL | a selector matched no part | fix the selector or the roles and groups |
| `cad.incomplete.mesh_missing` | FAIL | a part's mesh is not there | export it, or fix the path |
| `cad.incomplete.unreadable` | FAIL | a mesh or record does not read | export again |
| `cad.incomplete.no_record` | FAIL | the spec declares a record that is missing | `shambu cad stamp` |
| `cad.incomplete.no_volume` | FAIL | a capped pair is not closed, so it has no overlap volume | re-export closed solids |
| `cad.spec.unknown_key` | WARN | a key the spec does not know (a typo?) | fix the key |
| `cad.default_role` | INFO | a part with no role is treated as rigid | name the role |
| `solid.open` | FAIL | open or non-manifold edges | fix the model (a failed Boolean, a surface body) |
| `solid.bodies` | FAIL | more than one outer shell | unite the bodies, or export each part |
| `solid.flipped` | WARN | facets wound inwards | export again |
| `solid.void` | INFO | an enclosed void | legal; mention it |
| `mesh.metres` | FAIL | the mesh is under 1 mm | export in millimetres |
| `volume.vs_cad` | FAIL | STL volume outside the faceting band of the CAD volume | export again; stamp |
| `volume.vs_step` | FAIL | STEP re-import volume outside 0.05 % of the CAD volume | export the STEP again |
| `volume.vs_expected` | FAIL | volume outside the spec's tolerance | compare model and parameter table |
| `volume.nonpositive` | FAIL | inside-out or open mesh | export a closed solid |
| `size.vs_expected` | FAIL | box outside the spec's size tolerance | check the outer dimensions |
| `mass.cog_vs_cad` | WARN | centre of mass beyond the faceting bound from the CAD's | export again; stamp |
| `feature.missing` | FAIL | fewer features than expected, or the pattern's angles differ (a mirror?) | check the cut, pattern count and handedness |
| `feature.extra` | WARN | more features than expected, or one not in the list | update the spec or the model |
| `feature.horizontal_bore` | INFO, observed | a hole prints horizontal | give it the horizontal allowance; coupon |
| `fit.not_found` | FAIL | no hole near the nominal size at the mouth | check the mouth point and axis |
| `fit.diameter` | FAIL | diameter outside the fit's band | set the hole size in the parameter table |
| `fit.depth` | FAIL | full-diameter depth under `depth_min_mm` | deepen the pocket or move the relief below it |
| `fit.wall` | FAIL | wall under `wall_min_mm` | thicken the boss or move the hole |
| `fit.offset` | FAIL | axis further from the declared one than allowed | move the hole, or fix the spec |
| `fit.shadowed` | WARN | a spec fit replaces a built-in name | rename it, or keep it on purpose |
| `fit.coupon` | INFO | a new insert, press or slip fit | print a coupon first |
| `fit.horizontal` | INFO, observed | a fit bore prints horizontal | check the allowance |
| `clash.overlap` | FAIL | overlap above the floor, no exemption | remove it, or exempt it with a reason and cap |
| `clash.over_cap` | FAIL | exempt pair over its cap | reduce the interference |
| `clash.exempt` | INFO | exempt pair within its cap (reason quoted) | none |
| `clash.margin` | FAIL | closer than the margin | open the gap |
| `clash.boolean_failed` | FAIL | the Boolean failed; never exempt | export closed solids |
| `sweep.early_contact` | FAIL | contact before the allowed travel | add clearance or move the stop |
| `sweep.margin`, `sweep.overlap` | FAIL | contact or overlap before the end of travel, or an end overlap over the cap | open the path |
| `sweep.first_contact` | INFO | where and with what the parts first meet | quote it with the resolution |
| `sweep.no_contact` | INFO | the moving parts met nothing | a seat with no stop is worth knowing |
| `diff.size`, `diff.volume`, `diff.section`, `diff.feature_*` | INFO | what changed | quote it |
| `diff.coupon` | WARN | a fit feature changed | print a coupon |
| `diff.coupon_undecided` | FAIL | a changed fit's expectation entry has no `coupon` decision | add `"coupon": true` or `false` |
| `diff.unexpected` | FAIL | a change the expectation does not list | revert it or list it |
| `diff.expected_missing` | FAIL | a listed change did not happen | make the change or remove the entry |
| `diff.reslice` | INFO | a plate holds the changed STL | `shambu build plate.json --slice` |

## 7. Tolerances and where they come from

- STL against CAD volume: max(A x t, 0.05 % of the CAD volume), with A the STL area and t the faceting tolerance
  (the record's, else the 0.1 mm export default, named in `expected_from`). Every STL vertex lies on the CAD
  surface and every facet within t of it. 0.05 % is the mass-property band of ClassCAD's verification recipe.
- CAD against STEP re-import: 0.05 % (both are B-rep integrals).
- Hole and boss diameters: a circle fit on the mesh vertices, which lie on the CAD surface: the CAD diameter to
  rounding. The built-in fit band is +/- 0.02 mm.
- Clearance distance: exact between the meshes (every vertex against the other's triangles, then edge against
  edge). Overlap floor: max(0.001 mm3, 1e-6 of the smaller part).
- Sweep: the first contact is bisected to `resolution_mm` or `resolution_deg`. A fixed part thinner than the arc's
  bulge between two straight step segments can be missed; use a smaller step for thin parts.
