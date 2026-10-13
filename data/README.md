# data/: real CAD parts for testing (gitignored)

Synthetic boxes pass the unit tests; real parts find the bugs. Everything in this folder except this README is
ignored by git. Copy STLs here from your CAD exports (the gripper parts from the openarm build, a funnel, a URDF
mesh in metres) and run the manual loop below before a release. Nothing here is committed, so sizes and
licences do not matter.

Suggested contents (what found the 0.2.0 bugs, see `skills/shambu/references/08-optimisation.md` section 9):

- a part whose STL does not start at the origin and prints in two or more copies (`link.stl`, z -38 in its CAD
  frame): found the copy-position bug;
- a tall thin part on the second filament (`tag_bridge.stl`, 210 x 3 x 96 standing) next to a supported part on
  the first filament: found the support-filament pin;
- a part with a band of chamfers (`carrier.stl`): found the zero-contact orientation candidates;
- a mesh in metres (`so101_moving_jaw_metres.stl`): the metres warning and `rotate --scale 1000`.

The loop (each command takes well under a second per part, a sweep about a second per variant):

```
shambu info data/*.stl                       # size, bbox, manifold; a bbox under 1 mm is metres
shambu orient data/link.stl --top 8          # compare with the designer's intended bed face
shambu sweep plate.json --orientations link --by-object   # the slicer judges: time, grams, support, waste, swaps
```

Write the spec with absolute paths into this folder (`data/*.json` is ignored too). Compare the orient table
with the part's print notes: the tool optimises supports and contact, the designer may have optimised for load
direction, which is the documented priority order in the skill.
