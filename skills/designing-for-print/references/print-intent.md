# Print intent for a CAD part

Contents
1. The intent checklist (asked in this order)
2. Clearance holes: ISO 273
3. Inserts, press pins and slip fits
4. Hole allowance and orientation
5. Tolerances a printed part can hold
6. Coupons

## 1. The intent checklist (asked in this order)

Each item gets a value and one sentence of basis in `cad.json` `intent` (or a `fits` row). An item the brief does
not answer is asked once, in one batched question with a proposed default, or declared as an assumption in the
report.

1. Material and process: PETG, PLA, ASA or TPU; nozzle size. Gives the density for mass and decides the rest.
2. Function of every hole: clearance for a screw (ISO 273 series), heat-set insert, self-tapping into plastic,
   press pin, slip fit, or none. Each becomes a `fits` row with its `basis`.
3. Hole allowance, vertical and horizontal, in mm, added in CAD (`intent.hole_allowance_mm`).
4. Bed face and the load direction: which face goes on the bed (`intent.bed_face` and `stamp --bed-face`); layer
   lines must not carry a peel load. It decides which bores print horizontal.
5. Critical dimensions: one to five, each with a tolerance and why (`intent.critical`). Everything else gets one
   general tolerance stated as the printer's real accuracy (for example +/- 0.2 mm), never a machining class.
6. Mating pairs: each with its intended clearance or interference and the side that takes the allowance. They
   become `clearance` exemptions (with reasons) and `sweep` rules.
7. First layer and edges: a chamfer on bed-facing edges instead of a fillet; an elephant-foot allowance on mating
   bottom edges.
8. Coupons: which fits print as a coupon first, in the feature's own orientation.

## 2. Clearance holes: ISO 273

Built into shambu as `M<size>-clearance-<series>` (CAD diameter on the mesh, band +/- 0.02 mm):

| Thread | fine | medium | coarse |
|---|---|---|---|
| M2 | 2.2 | 2.4 | 2.6 |
| M2.5 | 2.7 | 2.9 | 3.1 |
| M3 | 3.2 | 3.4 | 3.6 |
| M4 | 4.3 | 4.5 | 4.8 |
| M5 | 5.3 | 5.5 | 5.8 |
| M6 | 6.4 | 6.6 | 7.0 |
| M8 | 8.4 | 9.0 | 10.0 |

Default to medium. If the model adds the print allowance to the hole, the mesh diameter is no longer the ISO
size: write your own `fits` row (`"hole_mm": 3.5, "basis": "ISO 273 medium + 0.1 vertical allowance"`); shambu
warns that it shadows the built-in name when you reuse one.

## 3. Inserts, press pins and slip fits

No such rows ship: their sizes depend on the part the user buys. Take them from the datasheet and write the
basis: `{"name": "insert-M3", "kind": "insert", "hole_mm": 4.0, "depth_min_mm": 6.0, "wall_min_mm": 1.6,
"basis": "insert datasheet: pocket 4.0, length 5.7"}`. Full-diameter depth is at least the insert length plus a
margin the user accepts; a relief bore below the pocket does not count as pocket depth (the gauge stops at the
step). With no datasheet, ask; if the user cannot say, declare the assumption in the report.

## 4. Hole allowance and orientation

Holes print undersize: about 0.1 mm for a vertical bore and 0.2 to 0.3 mm for a horizontal (bridged) one. The
allowance goes in the model, recorded in `intent.hole_allowance_mm`, because the shambu process for functional
PETG prints with `no-hole-compensation`. `features` and `fit` flag every bore that prints horizontal against the
bed face (`feature.horizontal_bore`, `fit.horizontal`).

## 5. Tolerances a printed part can hold

A 0.4 mm nozzle FDM part holds about +/- 0.2 mm in XY on a calibrated printer and one layer height in Z. Promise
tighter only on a critical dimension that a coupon has proved. The gates prove the CAD export; the printed size is
always "not checked".

## 6. Coupons

The first print of any new insert, press or slip fit is a coupon: the feature with a few millimetres of wall, in
the orientation the part prints it. A flat coupon cannot qualify a sideways bore. `fit.coupon` and `diff.coupon`
name the fits that need one.
