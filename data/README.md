# data/: real CAD parts for testing (gitignored)

Synthetic boxes pass the unit tests; real parts find the bugs. Everything in this folder except this README is
ignored by git. Copy STLs here from your CAD exports (the gripper parts from the openarm build, a funnel, a URDF
mesh in metres) and run the manual loop below before a release. Nothing here is committed, so sizes and
licences do not matter.

Keep variety, not quantity: robot parts in an assembly frame, URDF meshes in metres, Gridfinity bins and labels,
organic forms (lamps, figures, funnels), an assembly with several bodies, a non-manifold mesh. Each family breaks a
different assumption. Suggested contents (what found the 0.2.0 bugs, see
`skills/shambu/references/08-optimisation.md` section 9):

- a part whose STL does not start at the origin and prints in two or more copies (`link.stl`, z -38 in its CAD
  frame): found the copy-position bug;
- a tall thin part on the second filament (`tag_bridge.stl`, 210 x 3 x 96 standing) next to a supported part on
  the first filament: found the support-filament pin;
- a part with a band of chamfers (`carrier.stl`): found the zero-contact orientation candidates;
- a mesh in metres (`so101_moving_jaw_metres.stl`): the metres warning and `rotate --scale 1000`; its inward
  cavity shell (-76.5 mm3) found the `split` cavity bug; a metres copy in a spec found the `check` gap (Studio -100);
- a big part with tree supports next to a small one (gripper `bracket.stl` 5 mm from a nut holder): found the
  support-spread margin (Studio -101 at gap 15, sliced at 20);
- Gridfinity bins and two-colour labels: the exact-45-degree foot and lip found the threshold float noise, the
  label height range on filament 2 found the filament-change count, and a multi-object 3MF found `info` keeping
  only the last object;
- organic forms (a lamp with no flat base, a duck, two funnels): found the slenderness gap (a 6 mm rod ranked
  standing), the empty orientation sweep, and the by-object win (1 h 13 against 2 h 40, 273 swaps);
- an assembly with several bodies (an OpenArm gripper with motors, 88 bodies): found the missing multi-body note
  and the `split` count that disagrees with Studio's parts.

The loop (each command takes up to about 1.6 s per part, a 520k-facet `orient` about 13 s, a sweep 1 to 7 s per
variant):

```
shambu info data/*.stl                       # size, bbox, manifold; a bbox under 1 mm is metres
shambu orient data/link.stl --top 8          # compare with the designer's intended bed face
shambu sweep plate.json --orientations link --by-object   # the slicer judges: time, grams, support, waste, swaps
```

Write the spec with absolute paths into this folder (`data/*.json` is ignored too). Compare the orient table
with the part's print notes: the tool optimises supports and contact, the designer may have optimised for load
direction, which is the documented priority order in the skill.
