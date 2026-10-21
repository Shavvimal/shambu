"""spec.py and recipes.py: parsing, recipe merge order, validation, the fluent builder."""

import pytest

from shambu import recipes as R
from shambu.errors import BuildError
from shambu.spec import HeightRangeSpec, PartSpec, Spec, SpecBuilder, WipeTowerSpec
from shambu.tower import TowerSettings


def spec_json(tmp_path, **over) -> dict:
    d = {
        "name": "plate-a",
        "outdir": str(tmp_path),
        "printer": "P",
        "process": {"base": "B", "name": "N", "recipes": ["petg-functional"], "set": {"wall_loops": "8"}},
        "filaments": [{"preset": "F1", "colour": "#000000"}, {"preset": "F2", "colour": "#FFFFFF"}],
        "plates": [
            {
                "name": "p",
                "parts": [
                    {
                        "path": "/abs/a.stl",
                        "filament": 1,
                        "count": 2,
                        "recipes": ["tall-outer-brim"],
                        "set": {"brim_width": 8},
                        "height_ranges": [{"min_z": 0, "max_z": 0.6, "set": {"extruder": 2}}],
                        "at": [10, 10],
                    },
                    {"name": "label", "parts": [{"path": "/abs/b.stl"}, {"path": "/abs/c.stl", "filament": 2}]},
                ],
                "pause_at_z": [4.2],
                "colour_changes": [{"z": 10.2, "filament": 2}],
            }
        ],
    }
    d.update(over)
    return d


def test_process_recipe_then_set(tmp_path):
    s = Spec.from_json(spec_json(tmp_path))
    assert s.process.set["wall_loops"] == "8"  # explicit set wins
    assert s.process.set["sparse_infill_density"] == "40%"  # recipe value kept
    assert s.process.recipes == ("petg-functional",)


def test_part_recipe_then_set_and_strings(tmp_path):
    part = Spec.from_json(spec_json(tmp_path)).plates[0].parts[0]
    assert part.set["brim_type"] == R.recipe_settings()["tall-outer-brim"]["brim_type"]  # from the recipe
    assert part.set["brim_width"] == "8"  # set wins, as a string
    assert part.height_ranges == (HeightRangeSpec(0.0, 0.6, {"extruder": "2"}),)
    assert (part.count, part.at) == (2, (10.0, 10.0))


def test_multipart_and_plate_extras(tmp_path):
    pl = Spec.from_json(spec_json(tmp_path)).plates[0]
    label = pl.parts[1]
    assert [(f.path.name, f.filament) for f in label.files()] == [("b.stl", 1), ("c.stl", 2)]
    assert label.key() == "label"
    assert pl.pause_at_z == (4.2,)
    assert pl.colour_changes[0].filament == 2


def test_object_recipe_adds_height_ranges(tmp_path):
    d = spec_json(tmp_path)
    d["plates"][0]["parts"][1]["recipes"] = ["two-colour-first-layers"]
    label = Spec.from_json(d).plates[0].parts[1]
    assert label.height_ranges == (HeightRangeSpec(0.0, 0.6, {"extruder": "2"}),)


def test_unknown_process_recipe_raises(tmp_path):
    d = spec_json(tmp_path)
    d["process"]["recipes"] = ["nope"]
    with pytest.raises(BuildError, match="unknown recipe"):
        Spec.from_json(d)


def test_unknown_part_recipe_raises(tmp_path):
    d = spec_json(tmp_path)
    d["plates"][0]["parts"][0]["recipes"] = ["nope"]
    with pytest.raises(BuildError, match="unknown recipe"):
        Spec.from_json(d)


def test_missing_top_level_key(tmp_path):
    d = spec_json(tmp_path)
    del d["printer"]
    with pytest.raises(BuildError, match="lacks 'printer'"):
        Spec.from_json(d)


def test_valid_spec_has_no_errors(tmp_path):
    assert Spec.from_json(spec_json(tmp_path)).validate() == []


def test_json_round_trip(tmp_path):
    s = Spec.from_json(spec_json(tmp_path))
    assert Spec.from_json(s.to_json()) == s


def test_json_round_trip_with_recipe_ranges(tmp_path):
    d = spec_json(tmp_path)
    d["plates"][0]["parts"][1]["recipes"] = ["two-colour-first-layers"]
    s = Spec.from_json(d)
    assert Spec.from_json(s.to_json()) == s


@pytest.mark.parametrize(
    ("over", "text"),
    [
        ({"name": "a/b"}, "bare file stem"),
        ({"bed_type": "Glass"}, "bed_type 'Glass'"),
        ({"filaments": []}, "at least one filament"),
        ({"plates": []}, "at least one plate"),
    ],
)
def test_validate_top_level(tmp_path, over, text):
    assert any(text in e for e in Spec.from_json(spec_json(tmp_path, **over)).validate())


@pytest.mark.parametrize(
    ("part", "text"),
    [
        ({"path": "/a.stl", "filament": 3}, "filament 3 outside 1..2"),
        ({"path": "/a.stl", "count": 0}, "count must be >= 1"),
        ({"path": "/a.stl", "subtype": "glue"}, "subtype 'glue'"),
        ({"path": "/a.stl", "height_ranges": [{"min_z": 1, "max_z": 1}]}, "is empty"),
        ({"name": "nothing"}, "needs 'path' or 'parts'"),
    ],
)
def test_validate_parts(tmp_path, part, text):
    d = spec_json(tmp_path)
    d["plates"][0]["parts"] = [part]
    assert any(text in e for e in Spec.from_json(d).validate())


def test_validate_empty_plate_and_colour_change(tmp_path):
    d = spec_json(tmp_path)
    d["plates"] = [{"name": "e", "parts": [], "colour_changes": [{"z": 1, "filament": 5}]}]
    errs = Spec.from_json(d).validate()
    assert any("no parts" in e for e in errs)
    assert any("colour change to filament 5" in e for e in errs)


# ---------------------------------------------------------------------------- builder
def test_builder_builds_valid_spec(tmp_path):
    s = (
        SpecBuilder("x", tmp_path, "P")
        .process("B", "N", wall_loops=4)
        .filament("F1", "#000000")
        .filament("F2")
        .bed("Cool Plate")
        .filament_map(1, 2)
        .aux_x_limit(235.5)
        .layout(gap=3)
        .plate(
            "p",
            SpecBuilder.part("/a.stl", count=2, sparse_infill_density="20%"),
            SpecBuilder.multipart("label", ("/b.stl", 1), ("/c.stl", 2)),
            pause_at_z=(2.0,),
        )
        .build()
    )
    assert (s.bed_type, s.filament_map, s.aux_x_limit, s.layout.gap) == ("Cool Plate", (1, 2), 235.5, 3)
    assert s.process.set == {"wall_loops": "4"}
    assert s.plates[0].parts[0].set == {"sparse_infill_density": "20%"}
    assert s.plates[0].parts[1].filament == 1


def test_builder_recipe_merges(tmp_path):
    s = SpecBuilder("x", tmp_path).recipe("pla-light").filament("F").plate("p", SpecBuilder.part("/a.stl")).build()
    assert s.process.recipes == ("pla-light",)
    assert s.process.set["sparse_infill_density"] == "15%"


def test_builder_unknown_recipe(tmp_path):
    with pytest.raises(BuildError):
        SpecBuilder("x", tmp_path).recipe("nope")


def test_builder_build_validates(tmp_path):
    with pytest.raises(BuildError, match="at least one filament"):
        SpecBuilder("x", tmp_path).plate("p", SpecBuilder.part("/a.stl")).build()


def test_builder_explicit_settings_beat_recipe(tmp_path):
    s = (
        SpecBuilder("x", tmp_path)
        .process("B", wall_loops="8")
        .recipe("petg-functional")
        .filament("F")
        .plate("p", SpecBuilder.part("/a.stl"))
        .build()
    )
    assert s.process.set["wall_loops"] == "8"


def test_part_key_falls_back_to_stem():
    assert PartSpec.from_json({"path": "/x/jaw.stl"}).key() == "jaw"


# ---------------------------------------------------------------------------- recipes
def test_recipes_have_unique_names_and_string_values():
    names = [r.name for r in R.RECIPES]
    assert len(names) == len(set(names))
    assert all(isinstance(v, str) for r in R.RECIPES for v in r.set.values())
    assert all(r.scope in R.SCOPES for r in R.RECIPES)


def test_recipes_describe_filters_scope():
    assert len(R.describe("object")) == sum(r.scope == "object" for r in R.RECIPES)
    assert R.recipe_settings()["solid-part"]["sparse_infill_density"] == "100%"


# ---------------------------------------------------------------------------- wipe_tower and unknown keys
X2D_FACTS = {
    "bed": {"x": (0.0, 256.0), "y": (0.0, 256.0)},
    "extruders": [{"index": 1, "x": (0.0, 256.0), "y": (0.0, 256.0)}, {"index": 2, "x": (20.5, 256.0), "y": (0.0, 256.0)}],
}


def test_wipe_tower_parses_a_spot_auto_and_false(tmp_path):
    d = spec_json(tmp_path)
    d["plates"][0]["wipe_tower"] = {"x": 172, "y": 150, "width": 22, "rib_wall": False, "brim": 2}
    wt = Spec.from_json(d).plates[0].wipe_tower
    assert (wt.mode, wt.x, wt.y, wt.width, wt.rib_wall, wt.brim) == ("fixed", 172.0, 150.0, 22.0, False, 2.0)
    assert Spec.from_json(Spec.from_json(d).to_json()).plates[0].wipe_tower == wt  # round trip
    d["plates"][0]["wipe_tower"] = "auto"
    assert Spec.from_json(d).plates[0].wipe_tower.mode == "auto"
    d["plates"][0]["wipe_tower"] = False
    assert Spec.from_json(d).plates[0].wipe_tower.mode == "off"
    del d["plates"][0]["wipe_tower"]
    assert Spec.from_json(d).plates[0].wipe_tower.mode == "auto"  # the default


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ({"x": "172", "y": 150}, "plate 1 wipe_tower x must be a number"),
        ({"y": 150}, "plate 1 wipe_tower needs 'x'"),
        ({"x": 172, "y": 150, "width": 70}, "width 70 is outside 10..60"),
        ({"x": 172, "y": 150, "rib_wall": "no"}, "rib_wall must be true or false"),
        ("left", 'plate 1 wipe_tower must be "auto", false'),
    ],
)
def test_wipe_tower_rejects_bad_values(tmp_path, value, match):
    d = spec_json(tmp_path)
    d["plates"][0]["wipe_tower"] = value
    with pytest.raises(BuildError, match=match):
        Spec.from_json(d)


def test_wipe_tower_spot_outside_the_shared_reach_or_bed_raises_naming_the_plate():
    base = TowerSettings()
    WipeTowerSpec("fixed", 172, 150, 22, False, 2).check_spot(2, X2D_FACTS, base, 218)  # inside: no error
    with pytest.raises(BuildError, match=r"plate 2 wipe_tower at \(10, 150\).*share only x 20.5..256"):
        WipeTowerSpec("fixed", 10, 150, 22, False, 2).check_spot(2, X2D_FACTS, base, 218)
    with pytest.raises(BuildError, match=r"plate 3 wipe_tower at \(208, 208\).*leaves the bed 256 x 256"):
        WipeTowerSpec("fixed", 208, 208).check_spot(3, X2D_FACTS, base, 218)  # 41.4 rib wall + 8 mm brim


def test_unknown_keys_are_warnings_not_errors(tmp_path):
    d = spec_json(tmp_path)
    d["plates"][0]["parts"][0]["brim_margn"] = 3
    d["plates"][0]["wipe_tower"] = {"x": 172, "y": 150, "widht": 22}
    d["colour"] = "#000000"
    s = Spec.from_json(d)
    assert s.validate() == []
    assert any(w.startswith("plate 1 part 1: unknown key 'brim_margn' ignored") for w in s.warnings)
    assert any(w.startswith("plate 1 wipe_tower: unknown key 'widht' ignored") for w in s.warnings)
    assert any(w.startswith("spec: unknown key 'colour' ignored") for w in s.warnings)
    assert Spec.from_json(spec_json(tmp_path)).warnings == ()
