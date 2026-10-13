"""assemble.py: the builder emits a valid list, and validate() flags each rule in the module docstring."""

import copy
import json

import pytest

from shambu import assemble as A
from shambu.errors import AssembleError


@pytest.fixture
def good(stls) -> dict:
    al = A.AssembleList(
        [
            A.Plate(
                "p1",
                [
                    A.Item(
                        stls["body"],
                        1,
                        1,
                        pos=[(1.0, 2.0, 0.0)],
                        print_params={"wall_loops": "3"},
                        height_ranges=[A.HeightRange(0, 0.6, {"extruder": "2"})],
                    ),
                    A.Item(stls["base"], 1, 1, assemble_index=1, pos=[(0, 0, 0)]),
                    A.Item(stls["text"], 2, 1, "modifier_part", 1, [(0, 0, 0)]),
                ],
                plate_params={"curr_bed_type": "Textured PEI Plate"},
                assembled=[A.Assembled(1, {"wall_loops": "4"})],
            )
        ]
    )
    return al.to_json()


def obj(data, i=0):
    return data["plates"][0]["objects"][i]


def errs(data, **kw):
    return A.validate(data, **kw)


def has(data, text, **kw):
    return any(text in e for e in A.validate(data, **kw))


def test_builder_output_is_valid(good):
    assert errs(good, known_keys={"wall_loops", "extruder", "curr_bed_type"}, filament_count=2) == []


def test_builder_writes_strings(good, tmp_path):
    p = A.AssembleList([]).write(tmp_path / "a.json")
    assert json.loads(p.read_text()) == {"plates": []}
    assert obj(good)["height_ranges"][0]["range_params"] == {"extruder": "2"}


def test_item_pos_length_mismatch_raises(stls):
    with pytest.raises(AssembleError):
        A.Item(stls["body"], count=3, pos=[(0, 0, 0), (1, 1, 1)]).to_json()


def test_empty_plates():
    assert errs({"plates": []}) == ["'plates' must be a non-empty list"]


def test_too_many_plates(good):
    good["plates"] = good["plates"] * 37
    assert has(good, "at most 36")


def test_missing_plate_keys(good):
    del good["plates"][0]["plate_name"]
    assert has(good, "missing 'plate_name'")


def test_need_arrange_must_be_bool(good):
    good["plates"][0]["need_arrange"] = "false"
    assert has(good, "need_arrange must be true or false")


def test_plate_param_not_saved(good):
    good["plates"][0]["plate_params"]["wall_loops"] = "3"
    assert has(good, "not saved in the 3MF")


def test_plate_without_objects(good):
    good["plates"][0]["objects"] = []
    assert has(good, "no objects")


def test_file_not_found(good, tmp_path):
    obj(good)["path"] = str(tmp_path / "missing.stl")
    assert has(good, "file not found")


def test_relative_path(good, stls, monkeypatch):
    monkeypatch.chdir(stls["body"].parent)
    obj(good)["path"] = "body.stl"
    assert has(good, "path must be absolute")


def test_unknown_subtype(good):
    obj(good, 2)["subtype"] = "glue_part"
    assert has(good, "subtype 'glue_part' unknown")


def test_only_stl_and_obj(good, tmp_path):
    p = tmp_path / "x.step"
    p.write_text("x")
    obj(good)["path"] = str(p)
    assert has(good, "only .stl and .obj")


def test_obj_only_normal_part(good, tmp_path):
    p = tmp_path / "x.obj"
    p.write_text("x")
    obj(good, 2)["path"] = str(p)
    assert has(good, ".obj only as normal_part")


def test_count_at_least_one(good):
    obj(good)["count"] = 0
    assert has(good, "count must be an integer >= 1")


def test_filaments_required(good):
    del obj(good)["filaments"]
    assert has(good, "'filaments' must be a non-empty list")


def test_filaments_length_vs_count(good):
    obj(good)["filaments"] = [1, 2]
    assert has(good, "filaments has 2 entries")


def test_filament_index_over_count(good):
    obj(good)["filaments"] = [3]
    assert has(good, "filament 3 outside 1..2", filament_count=2)


def test_non_part_subtype_needs_assemble_index(good):
    del obj(good, 2)["assemble_index"]
    assert has(good, "modifier_part needs assemble_index >= 1")


def test_pos_lengths_differ(good):
    obj(good)["pos_y"] = [1.0, 2.0]
    assert has(good, "lengths differ")


def test_pos_partial(good):
    del obj(good)["pos_z"]
    assert has(good, "give pos_x, pos_y and pos_z together")


def test_pos_length_vs_count(good):
    o = obj(good)
    o["pos_x"], o["pos_y"], o["pos_z"] = [0, 1], [0, 1], [0, 1]
    assert has(good, "pos_x has 2 entries, needs 1 or count=1")


def test_non_string_param_values(good):
    obj(good)["print_params"]["wall_loops"] = 3
    assert has(good, "must be a JSON string, got int")


def test_unknown_param_key(good):
    assert has(good, "unknown setting key 'wall_loops'", known_keys={"extruder"})


def test_height_ranges_on_non_normal_part(good):
    obj(good, 2)["height_ranges"] = [{"min_z": 0, "max_z": 1, "range_params": {}}]
    assert has(good, "height_ranges only on normal_part")


def test_height_ranges_on_merged_part(good):
    obj(good, 1)["height_ranges"] = [{"min_z": 0, "max_z": 1, "range_params": {}}]
    assert has(good, "height_ranges on a merged part are lost")


def test_height_range_numbers(good):
    obj(good)["height_ranges"][0]["min_z"] = "0"
    assert has(good, "min_z/max_z must be numbers")


def test_assembled_params_index_scalar(good):
    good["plates"][0]["assembled_params"][0]["assemble_index"] = [1]
    assert has(good, "must be a scalar integer")


def test_assembled_params_unused_index(good):
    good["plates"][0]["assembled_params"][0]["assemble_index"] = 2
    assert has(good, "no object uses assemble_index 2")


def test_fixture_is_not_mutated_between_rules(good):
    before = copy.deepcopy(good)
    A.validate(good)
    assert good == before


def test_non_integer_assemble_index_is_reported_not_raised(good):
    obj(good, 2)["assemble_index"] = ["1"]
    assert has(good, "assemble_index")
