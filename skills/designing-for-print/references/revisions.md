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
  {"feature": "h2", "change": "resized", "diameter_mm": 4.2, "coupon": true},
  {"measure": "size_z", "delta_mm": 2},
  {"measure": "volume_mm3", "delta_mm3": 1550}
]}
```

- `feature`: a spec hole name (the hole whose mouth lies on that feature's axis), or the id `cad diff` prints for a
  feature with no name (`hole d4.00 at (30.0, 12.0, 2.5)`). `change` is required: `resized`, `moved`, `gone` or
  `new`. Optional: `diameter_mm` (the new diameter), `ends` (`through`, `blind` or `closed`) and `coupon` (true or
  false; required when the feature carries a fit).
- `measure`: `size_x`, `size_y` or `size_z` with `delta_mm`, or `volume_mm3` with `delta_mm3`. The delta is
  required.
- Any other key, a missing `change` or delta, or a feature listed twice is an expectation error (exit 1): an entry
  that does not parse would excuse changes it does not name.
- Each entry excuses only the change it names. A volume change needs a `volume_mm3` entry, or listed feature changes
  whose cylinders (diameter x depth) account for it; a size change always needs a `volume_mm3` entry as well. A
  section band needs a `volume_mm3` entry, a listed size change, or a listed feature change inside the band. An
  internal change (a cavity, a rib) that nothing lists is `diff.unexpected`.

## Run and read the diff

`shambu cad diff out/<part>@<old>.cad.json out/<part>@<new>.cad.json --spec cad.json --plate plate.json
--expect expect.json`. A record as an input must still name its own STL (`cad.stale.stl_sha` when the file was
replaced). Its build id and STEP are not rechecked: the script and parameters move on with each revision.

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
