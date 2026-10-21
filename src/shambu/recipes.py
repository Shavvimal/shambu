"""Named print recipes: settings bundles real prints have validated.

A recipe is data (frozen dataclass), applied by name from a spec (`"recipes": [...]`) at process
or per-object scope. Sources: "gripper build" = a functional PETG gripper build on an X2D;
"63-tip video" = a 16-year printer/designer's tips (2026-10-12); "notes" = other items in
the author's print notes; "research 08" = the 2026-10-13 optimisation research in
`skills/shambu/references/08-optimisation.md` (Bambu wiki, Studio source, headless measurements).
Values are strings in Bambu preset form.

Never put a scalar into a per-nozzle array key on the X2D (`*_speed`, `*_acceleration`,
`enable_overhang_speed`): the profile holds six values (three per nozzle) and a scalar wipes
the other five (research 08, section 3).
"""

from __future__ import annotations

from dataclasses import dataclass, field

SCOPES = ("process", "object")


@dataclass(frozen=True)
class Recipe:
    name: str
    scope: str  # process | object (an object recipe also works at process scope)
    description: str
    set: dict[str, str] = field(default_factory=dict)
    when: str = ""
    source: str = ""
    caveat: str = ""
    height_ranges: tuple[dict, ...] = ()  # object scope only: [{"min_z", "max_z", "set"}] added to the part


RECIPES: tuple[Recipe, ...] = (
    Recipe(
        "petg-functional",
        "process",
        "Load-bearing PETG parts: strength from walls, 40 % gyroid.",
        {
            "wall_loops": "6",
            "top_shell_layers": "5",
            "bottom_shell_layers": "5",
            "sparse_infill_density": "40%",
            "sparse_infill_pattern": "gyroid",
            "elefant_foot_compensation": "0.15",
            "brim_type": "no_brim",
        },
        "Functional PETG parts (brackets, carriers, jaws). Raise walls before infill.",
        "gripper build",
        "100 % infill only on parts sized as solid in the stress maths (gears, racks, rails): use `solid-part` per object.",
    ),
    Recipe(
        "pla-light",
        "process",
        "Everyday PLA: 3 walls, 15 % gyroid.",
        {
            "wall_loops": "3",
            "top_shell_layers": "4",
            "bottom_shell_layers": "3",
            "sparse_infill_density": "15%",
            "sparse_infill_pattern": "gyroid",
            "elefant_foot_compensation": "0.15",
        },
        "Most PLA parts. 15 to 20 % is usually plenty.",
        "63-tip video",
    ),
    Recipe(
        "pla-decorative",
        "process",
        "Decorative PLA: 2 walls, 5 % infill.",
        {"wall_loops": "2", "sparse_infill_density": "5%", "sparse_infill_pattern": "gyroid"},
        "Parts that carry no load.",
        "63-tip video",
    ),
    Recipe(
        "solid-part",
        "object",
        "100 % infill for one object (solid zig-zag; the sparse pattern is then ignored).",
        {"sparse_infill_density": "100%"},
        "Gears, pinions, racks, rails: parts the stress maths sized as solid.",
        "gripper build",
    ),
    Recipe(
        "tall-outer-brim",
        "object",
        "Outer-only 5 mm brim for tall or long, narrow parts.",
        {"brim_type": "outer_only", "brim_width": "5", "brim_object_gap": "0.1"},
        "Blades on end, long rails, jaws, carrier, bracket.",
        "gripper build",
        'The notes say "outer only, auto"; outer_only 5 mm is the explicit form, auto_brim lets Studio size it.',
    ),
    Recipe(
        "flat-no-brim",
        "object",
        "No brim on flat coupons and small parts (the default auto_brim may still add one).",
        {"brim_type": "no_brim"},
        "Flat-lying parts with a large bed contact area.",
        "gripper build",
    ),
    Recipe(
        "dog-ears",
        "object",
        "Brim ears (small discs at corners) instead of a full brim on big flat plates.",
        {"brim_type": "brim_ears"},
        "A corner of a large flat plate lifts.",
        "63-tip video / gripper build",
        "Does nothing until ear points exist: the user places them with the Brim Ears gizmo (Auto-generate points) in Studio.",
    ),
    Recipe(
        "tree-supports",
        "object",
        "Tree supports, auto placement, default settings.",
        {"enable_support": "1", "support_type": "tree(auto)", "support_style": "default", "support_threshold_angle": "30"},
        "Overhangs on upright parts. Default choice when supports are needed.",
        "63-tip video",
    ),
    Recipe(
        "snug-normal-supports",
        "object",
        "Normal supports in snug style, build plate only.",
        {"enable_support": "1", "support_type": "normal(auto)", "support_style": "snug", "support_on_build_plate_only": "1"},
        "A fully flat area supported straight up from the plate.",
        "63-tip video",
    ),
    Recipe(
        "no-supports",
        "object",
        "Supports off for one object.",
        {"enable_support": "0"},
        "Flat coupons beside a supported part.",
        "gripper build",
    ),
    Recipe(
        "dual-material-interface",
        "process",
        "Support interface in the other material on nozzle 2: Bambu's own X2D values for PLA/PETG pairs.",
        {
            "enable_support": "1",
            "support_type": "tree(auto)",
            "support_style": "tree_hybrid",
            "support_threshold_angle": "35",
            "support_on_build_plate_only": "0",
            "support_interface_filament": "2",
            "support_filament": "0",
            "support_interface_not_for_body": "1",
            "support_top_z_distance": "0",
            "support_interface_spacing": "0",
            "support_interface_pattern": "rectilinear_interlaced",
            "tree_support_branch_diameter": "3",
            "tree_support_branch_diameter_angle": "7",
            "raft_first_layer_expansion": "5",
            "independent_support_layer_height": "0",
            "thick_bridges": "0",
            "enable_prime_tower": "1",
        },
        "PETG part with PLA interface or PLA part with PETG interface; they barely bond. The plate counts as two filaments even "
        "though only the supports use filament 2, so Studio builds a prime tower sized by the tallest part (with the rib wall: "
        "41 mm square plus an 8 mm brim for a 218 mm part, 35 mm plus 2.6 mm for a 32 mm part). Give the plate a wipe_tower "
        "spot both extruders reach (x >= 20.5), or let "
        '"auto" find one; if no spot fits, use rib_wall false and a 22 mm width.',
        "notes / research 08 (Studio's support_recommended_params.json, which the headless CLI never applies); "
        "plate 14 and seed runs 2026-10-21 (reference 06, prime tower; reference 03, re-slicing a CLI export)",
        "Filament 2 must be the other material on nozzle 2 (filament_map 1,2). Every supported part must sit at x >= 20.5 plus "
        "its interface spread (brim margin plus 3 mm), because extruder 2 (right nozzle) prints the interface (2.2 mm past "
        "the part box measured, n=1; "
        "the seed build exited -102 at x 14.3). Measured on plate 14 (n=1): the tower adds 27 min and 71 g over no tower "
        "(19.25 h against 18.8 h). An unsliced export without the RepairNozzleStats patch exits -100 when re-sliced with "
        '`--slice 1` ("No valid nozzle found"): `shambu build` applies it, `shambu patch --repair-nozzle-stats` fixes an older file. '
        "Bed temperature follows the hotter filament (PETG 70 C under a PLA part). Bambu prints PLA interface at 230 C and PETG HF "
        "interface at 265 C with a 10 mm3/s cap; the slicer's own table also sets support_interface_speed 50, a per-nozzle array left alone here.",
    ),
    Recipe(
        "fuzzy-skin",
        "object",
        "Fuzzy skin on outer walls, tiny thickness and point distance.",
        {"fuzzy_skin": "external", "fuzzy_skin_thickness": "0.15", "fuzzy_skin_point_distance": "0.4", "fuzzy_skin_first_layer": "0"},
        "Warm matte texture without sanding. Decorative parts only, never on mating faces.",
        "notes",
        '0.15 / 0.4 are starting values; the notes only say "tiny".',
    ),
    Recipe(
        "shim-0.1mm",
        "process",
        "0.1 mm layers for thin shims and rings.",
        {"layer_height": "0.1", "initial_layer_print_height": "0.2", "brim_type": "no_brim"},
        "0.5 and 1.0 mm spring shims; print them alone.",
        "gripper build",
        "Alone on the plate: with a prime tower every object must share one layer height.",
    ),
    Recipe(
        "wide-line-two-loop-walls",
        "process",
        "0.75 mm line width so a 1.5 mm wall prints as two loops.",
        {"outer_wall_line_width": "0.75", "inner_wall_line_width": "0.75", "wall_loops": "2"},
        "Thin-walled boxes with 1.5 mm walls on a 0.4 nozzle; a 40 min box drops to 32 min.",
        "63-tip video",
    ),
    Recipe(
        "coarse-fast",
        "process",
        "0.28 mm layers for straight-sided parts where the finish does not matter.",
        {"layer_height": "0.28"},
        "Straight-sided boxes: 40 min coarse vs 3.5 h fine, nearly identical.",
        "63-tip video",
        "Curved tops show the steps; use the GUI's adaptive layer height there (no process key exists in 02.08; the GUI writes Metadata/layer_heights_profile.txt).",
    ),
    Recipe(
        "two-colour-first-layers",
        "object",
        "First three layers (0.6 mm) in filament 2 on nozzle 2, the rest in filament 1.",
        {},
        "A coloured base, or background-printed text with the letters cut through the first layers.",
        "notes",
        "Needs two filaments with filament_map 1,2 and the part inside nozzle 2's reach (x >= 20.5). Recompute max_z if the layer height is not 0.2. "
        "Single-nozzle alternative: a pause at z 0.8 and a manual swap.",
        height_ranges=({"min_z": 0.0, "max_z": 0.6, "set": {"extruder": "2"}},),
    ),
    Recipe(
        "bridge-short-spans",
        "object",
        "Let bridges up to 10 mm print in the air instead of growing tree supports under them.",
        {"max_bridge_length": "10", "bridge_no_support": "1"},
        "Any supported part with short flat spans (slots, counterbores, box lids). The Bambu preset sets max_bridge_length 0, so "
        "tree support goes under every bridge.",
        "research 08",
        "PLA bridges 10 to 40 mm cleanly on a measured plate; PETG less. Over 40 mm keep the support.",
    ),
    Recipe(
        "minimal-supports",
        "object",
        "Tree supports only under sharp tails and cantilevers (critical regions).",
        {"enable_support": "1", "support_type": "tree(auto)", "support_critical_regions_only": "1", "max_bridge_length": "10"},
        "The floor of an orientation sweep: what still needs support when everything printable is left alone.",
        "research 08",
        "Flat ceilings over the model are left to sag. Compare the sweep's support grams against `tree-supports`.",
    ),
    Recipe(
        "petg-supports",
        "object",
        "Same-material PETG supports that come off: wider top gap and XY gap.",
        {"enable_support": "1", "support_type": "tree(auto)", "support_top_z_distance": "0.25", "support_object_xy_distance": "0.5"},
        "PETG part supported in PETG. PETG welds to itself at the 0.2 mm default gap.",
        "research 08 (forum reports, medium confidence)",
        "0.25 to 0.3 mm top gap; a 0.2 mm layer rounds it to 0.2 or 0.4 unless independent_support_layer_height stays on (it does by default).",
    ),
    Recipe(
        "counterbore-bridging",
        "object",
        "Print screw counterbores without support: one sacrificial bridged layer to drill out.",
        {"counterbore_hole_bridging": "sacrificiallayer"},
        "Counterbored holes that print with the bore vertical and the counterbore underneath.",
        "research 08",
        "`partiallybridge` bridges only the ring; `sacrificiallayer` closes the hole with one layer. Both are object scope.",
    ),
    Recipe(
        "more-support",
        "object",
        "Support threshold 45 degrees from horizontal: supports under anything flatter than 45.",
        {"support_threshold_angle": "45"},
        "A part whose 45 to 60 degree faces drooped on the first print.",
        "research 08",
        "Bambu's support_threshold_angle counts from HORIZONTAL (default 30 = only faces within 30 degrees of flat get support). "
        "The 45-degree rule of thumb is from vertical, so 45 here is more support than the default, not less.",
    ),
    Recipe(
        "scarf-seam",
        "object",
        "Scarf joint seams on outer walls, conditional (loops with a corner keep a hidden normal seam).",
        {"override_filament_scarf_seam_setting": "1", "seam_slope_type": "external", "seam_slope_conditional": "1"},
        "Cylinders, domes and smooth curves where no corner can hide the seam.",
        "research 08",
        "Reduces the seam, never removes it. Users report it shows more on silk filaments. Bambu's X2D filament profiles set scarf to none, "
        "so the override is needed.",
    ),
    Recipe(
        "seam-back",
        "object",
        "Seam at the back (+Y) of the part.",
        {"seam_position": "back"},
        "A show part whose back faces away: rotate the part so the hidden side faces +Y.",
        "research 08",
    ),
    Recipe(
        "iron-top",
        "object",
        "Iron the topmost flat surface only.",
        {"ironing_type": "topmost", "ironing_flow": "10%", "ironing_spacing": "0.1"},
        "One flat top face that shows (lids, plaques, name plates). Adds time on that face only.",
        "research 08",
        "`top` would iron every stair step on a sloped part. For a lower face, use a height range with ironing_type=top instead.",
    ),
    Recipe(
        "precise-fit",
        "object",
        "Precise outer wall with inner-then-outer order, elephant foot 0.15.",
        {"precise_outer_wall": "1", "wall_sequence": "inner wall/outer wall", "elefant_foot_compensation": "0.15"},
        "Fit-critical parts: the outer wall keeps its nominal width instead of overlapping the inner one.",
        "research 08",
        "Studio silently ignores precise_outer_wall with any other wall order. Holes still print undersize: qualify with a coupon.",
    ),
    Recipe(
        "show-walls",
        "object",
        "Three walls printed inner, outer, inner: the cleanest outer skin on parts without overhangs.",
        {"wall_loops": "3", "wall_sequence": "inner-outer-inner wall", "wall_generator": "classic"},
        "Cosmetic parts with vertical walls. Classic walls give one loop per layer, so one seam.",
        "research 08",
        "Not for overhang-heavy parts: inner/outer order prints overhangs better. Arachne is better for small text and thin ribs.",
    ),
    Recipe(
        "fast-infill",
        "process",
        "Adaptive cubic infill with sparse layers combined: the fast infill that is still strong.",
        {"sparse_infill_pattern": "adaptivecubic", "infill_combination": "1"},
        "Any solid-looking part where time matters. Measured on an 80 mm box: grid 1 h 09, adaptive cubic 0 h 59, gyroid 1 h 43; "
        "combination saved another 8 %.",
        "research 08 (headless estimates)",
        "Lightning (0 h 47) is faster still but only holds up top skins. Gyroid is the slowest common pattern; keep it only where "
        "isotropic strength matters.",
    ),
    Recipe(
        "fine-0.16",
        "process",
        "0.16 mm layers on the Standard base: finer curves without the High Quality preset's slow walls.",
        {"layer_height": "0.16"},
        "Curved or visible surfaces that show 0.2 mm steps. Measured on a 30 mm cube: 1062 s against 852 s at 0.20; the "
        "0.16mm High Quality preset took 1666 s for the same layers.",
        "research 08 / notes",
        "Flat solid tops look worse at 0.16 and print slower: keep 0.20 there. Every object on a plate with a prime tower shares "
        "one layer height; a `height_ranges` entry can switch to 0.28 above the detail instead (13 min 07 against 14 min 41 on a cube).",
    ),
    Recipe(
        "top-concentric",
        "object",
        "Concentric top-surface lines for round tops.",
        {"top_surface_pattern": "concentric"},
        "A round or ring-shaped show face whose monotonic lines show their direction.",
        "notes / research 08",
        "Region key, so it works on a modifier or a height range too. Flat rectangular tops keep the X2D default `monotonicline`. "
        "One wall on top surfaces is already Studio's default (`top_one_wall_type` all top); `topmost` limits it to the top face.",
    ),
    Recipe(
        "no-hole-compensation",
        "process",
        "Hole and contour compensation off: clearance lives in the CAD, qualified by coupons.",
        {"xy_hole_compensation": "0", "xy_contour_compensation": "0"},
        "Every functional plate. Slicer compensation applies one offset to every hole, and the measured error differs by orientation and bore length.",
        "gripper build / notes",
    ),
)


def all_keys() -> set[str]:
    """Every setting key a recipe writes: these are vetted names, so the plan's unknown-key check accepts them even
    when the resolved preset does not list them (the shipped X2D presets omit several valid keys)."""
    return {k for r in RECIPES for k in r.set} | {k for r in RECIPES for h in r.height_ranges for k in h["set"]}


def by_name() -> dict[str, Recipe]:
    return {r.name: r for r in RECIPES}


def recipe_settings() -> dict[str, dict[str, str]]:
    return {r.name: dict(r.set) for r in RECIPES}


def describe(scope: str | None = None) -> list[str]:
    out = []
    for r in RECIPES:
        if scope and r.scope != scope:
            continue
        out.append(f"{r.name} [{r.scope}]: {r.description} When: {r.when} ({r.source})" + (f" Caveat: {r.caveat}" if r.caveat else ""))
    return out
