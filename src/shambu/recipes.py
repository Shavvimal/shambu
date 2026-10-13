"""Named print recipes: settings bundles real prints have validated.

A recipe is data (frozen dataclass), applied by name from a spec (`"recipes": [...]`) at process
or per-object scope. Sources: "gripper build" = a functional PETG gripper build on an X2D;
"63-tip video" = a 16-year printer/designer's tips (2026-10-12); "notes" = other items in
the author's print notes. Values are strings in Bambu preset form.
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
        "Support interface in the other material on nozzle 2, zero top gap.",
        {
            "enable_support": "1",
            "support_type": "tree(auto)",
            "support_interface_filament": "2",
            "support_filament": "0",
            "support_top_z_distance": "0",
            "support_interface_spacing": "0",
            "support_interface_pattern": "rectilinear_interlaced",
            "support_interface_top_layers": "3",
            "enable_prime_tower": "1",
        },
        "PETG part with PLA interface or PLA part with PETG interface; they barely bond. Expect a prime tower.",
        "notes",
        "Filament 2 must be the other material on nozzle 2 (filament_map 1,2); supported features must sit at x >= 20.5. "
        "Bed temperature follows the hotter filament (PETG 70 C under a PLA part). Interface spacing 0 and the interlaced pattern are Studio's own suggestion, unverified for PLA/PETG.",
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
        "no-hole-compensation",
        "process",
        "Hole and contour compensation off: clearance lives in the CAD, qualified by coupons.",
        {"xy_hole_compensation": "0", "xy_contour_compensation": "0"},
        "Every functional plate. Slicer compensation applies one offset to every hole, and the measured error differs by orientation and bore length.",
        "gripper build / notes",
    ),
)


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
