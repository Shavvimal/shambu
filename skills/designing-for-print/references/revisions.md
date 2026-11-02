# Revisions

## Names

Export each revision under a new name: `out/<part>@<rev>.stl`, `out/<part>@<rev>.stp`, and the record
`out/<part>@<rev>.cad.json` that `--promote` writes. Keep old revisions: `cad diff` and any plate that still
names them need them. `--promote` refuses an existing name, so a new revision always gets a new label.

## Write the expectation first

Before the change, write `expect.json` from the user's request, so the diff checks the request and not your memory
of it:

```json
{"changes": [
  {"feature": "h2", "diameter_mm": 4.2, "coupon": true},
  {"measure": "size_z", "delta_mm": 2}
]}
```

- `feature`: a spec hole name (the hole whose mouth lies on that feature's axis), or the id `cad diff` prints for a
  feature with no name (`hole d4.00 at (30.0, 12.0, 2.5)`). Optional `change` (`resized`, `moved`, `gone`, `new`),
  `diameter_mm` (the new diameter) and `coupon` (true or false; required when the feature carries a fit).
- `measure`: `size_x`, `size_y` or `size_z` with `delta_mm`.

## Run and read the diff

`shambu cad diff out/<part>@<old>.cad.json out/<part>@<new>.cad.json --spec cad.json --plate plate.json
--expect expect.json`. Records as inputs refuse a stale file (`cad.stale.*`).

- `measures`: old, new and delta for volume, area, box, centre of mass and inertia.
- `sections`: where the section area changed, as bands ("changed between z 18.0 and 24.0").
- `features`: same, resized, moved, gone, new. A through hole whose depth follows a thickness change stays same.
- `impact`: the plates that hold the changed STL (reslice them with `shambu build PLATE --slice`), and whether
  `shambu patch --replace-mesh` applies (`needs --allow-size-change` after a size change; orient again after a bed
  face change).
- `diff.coupon`: a changed fit; print a coupon before the part.
- `diff.unexpected`, `diff.expected_missing`, `diff.coupon_undecided`: the change does not match the request. Fix the
  model or the expectation, never both silently.

Not checked by `diff`: a rotation or move of the whole part (the exports must share one frame), changes thinner
than the section spacing (use `--sections N`, or `--surface` with the geom extra), and anything only the STEP holds.
