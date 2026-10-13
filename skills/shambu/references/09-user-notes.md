# 09. The user's own finish and fit notes (personal tools and materials)

These are the owner's notes, kept out of the decision layer because they name personal tools and one printer.
Use them in the hand-back for this user; for anyone else, the slicer-side version lives in `08-optimisation.md`
section 5 and the recipes.

## Finish

- Hide layer lines: matte PLA scatters light so lines fade; `fuzzy-skin` with a tiny thickness and point
  distance adds a warm texture without sanding (decorative parts only, never on mating faces).
- Glossy: hotter prints shinier (PLA towards 250 C); then a fine automotive polishing compound (Menzerna 400)
  gives a glossy finish on PLA. For smoothness, an epoxy resin coat; the user also owns a Fanttik F2 for
  smoothing. Post-processing steps, not slicer settings.
- Top surface looking rough: lower the layer height for that part, concentric top-surface lines on round tops,
  one wall on the topmost surface only (`top_one_wall_type` topmost), iron one flat show face (`iron-top`),
  and check the top-surface line width (0.42 default).
- 0.16 mm layers for things that need it; not always better: flat solid lines look worse at 0.16 and a higher
  layer height is quicker.

## Fits and strength

- Holes print undersize because plastic shrinks as it cools and shrinkage scales the outside; design the
  clearance in, loose enough to fit after shrink, and qualify it with a coupon in the same orientation. X/Y hole
  compensation 0.01 to 0.03 mm is the slicer-side alternative; the gripper build chose CAD clearance.
- Heat-set inserts for screws (M3 x 4 x 4 in a 3.8 mm hole in PETG, M4 x 6 x 6 in 5.8, M2 x 4 x 3 in 2.9; iron
  at 245 C, 3 s, lift straight up). When there is no insert, a hexagon pocket for the nut.
- Fins on an outside edge absorb tolerance in a press fit (they push back when they need to).
- Strong parts: more walls, print at 45 degrees where the load wants it, a stronger filament, fillets at every
  section change. A hook breaks at the sharp root: fix it in CAD.
- Brim to stop a tall part falling over. Design the wall thickness.

## Workflow

- Several objects in two filaments: switch the plate to print by object and arrange with room between
  objects; far fewer swaps, a lot faster. Group the objects that share a filament next to each other; merge
  same-colour parts into one object so the group prints as one unit, and other parts can still share the plate.
- Always check the sliced preview before printing.
- Order plates by dependency: coupons first, then the parts whose numbers they set, then the big overnight
  parts. Weigh every part when it comes off and write it in the log.
