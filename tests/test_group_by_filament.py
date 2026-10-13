"""group_by_filament: same-filament parts on a print-by-object plate merge into one object. Offline."""

import json

import pytest

from conftest import MACHINE, PROCESS, PROCESS_NAME, FakeStudio, make_3mf
from shambu.build import Pipeline
from shambu.spec import PlateSpec, Spec, SpecBuilder

SIZES = {"body.stl": (20.0, 20.0, 10.0), "base.stl": (30.0, 20.0, 12.0), "text.stl": (20.0, 20.0, 8.0)}
LOS = {"base.stl": (5.0, 7.0, 2.0)}  # an STL that does not start at the origin


def spec_dict(tmp_path, stls, parts=None, **plate_extra):
    plate = {"name": "p", "print_sequence": "by object", "group_by_filament": True, **plate_extra}
    plate["parts"] = parts or [
        {"path": str(stls["body"]), "filament": 1},
        {"path": str(stls["base"]), "filament": 1, "name": "base"},
        {"path": str(stls["text"]), "filament": 2, "name": "text"},
    ]
    return {
        "name": "g",
        "outdir": str(tmp_path / "out"),
        "printer": MACHINE,
        "process": {"base": PROCESS, "name": PROCESS_NAME},
        "filaments": [{"preset": "Test PLA", "colour": "#000000"}, {"preset": "Test PLA", "colour": "#FFFFFF"}],
        "bed_type": "Textured PEI Plate",
        "plates": [plate],
    }


def plan_of(tmp_path, stls, resolver, d=None):
    studio = FakeStudio(sizes=SIZES, los=LOS)
    return Pipeline(studio, resolver).plan(Spec.from_json(d or spec_dict(tmp_path, stls)))


def fails(plan):
    return [c.text for p in plan.plates for c in p.checks if not c.ok]


# ---------------------------------------------------------------------------- spec
def test_spec_round_trip_and_builder(tmp_path, stls):
    s = Spec.from_json(spec_dict(tmp_path, stls))
    assert s.plates[0].group_by_filament
    assert s.to_json()["plates"][0]["group_by_filament"] is True
    assert "group_by_filament" not in PlateSpec("x").to_json()
    b = (
        SpecBuilder("b", tmp_path)
        .filament("Test PLA")
        .plate("p", SpecBuilder.part(stls["body"]), print_sequence="by object", group_by_filament=True)
    )
    assert b.build().plates[0].group_by_filament


def test_by_layer_with_grouping_is_a_spec_error(tmp_path, stls):
    errs = Spec.from_json(spec_dict(tmp_path, stls, print_sequence="by layer")).validate()
    assert any("group_by_filament needs print_sequence 'by object'" in e for e in errs)


def test_grouping_on_a_by_layer_process_is_a_plan_problem(tmp_path, stls, resolver):
    d = spec_dict(tmp_path, stls)
    del d["plates"][0]["print_sequence"]  # the process preset prints by layer
    plan = plan_of(tmp_path, stls, resolver, d)
    assert any("group_by_filament needs print_sequence 'by object'" in p for p in plan.problems)
    assert len(plan.parts) == 3


# ---------------------------------------------------------------------------- plan and assemble list
def test_two_filament_one_parts_become_one_object(tmp_path, stls, resolver):
    plan = plan_of(tmp_path, stls, resolver)
    assert plan.ok, (plan.problems, fails(plan))
    parts = plan.plates[0].parts
    assert [x.key for x in parts] == ["group-f1", "text"]
    g, text = parts
    assert g.grouped and [m.key for m in g.members] == ["body", "base"]
    assert g.cli_name == "assemble_1" and g.placed.filament == 1
    assert len(g.files) == 2  # every member file, so the sub-part checks see them

    plate = plan.assemble.to_json()["plates"][0]
    assert [a["assemble_index"] for a in plate["assembled_params"]] == [1]
    merged = [o for o in plate["objects"] if o.get("assemble_index")]
    assert [o["assemble_index"] for o in merged] == [[1], [1]]
    assert all(o["filaments"] == [1] for o in merged)
    single = [o for o in plate["objects"] if not o.get("assemble_index")]
    assert len(single) == 1 and single[0]["filaments"] == [2]

    # positions: each member's own translation = bed position of its bbox minus its STL min
    by_name = {m.key: m for m in g.members}
    for o in merged:
        m = by_name["base" if o["path"].endswith("base.stl") else "body"]
        lo = LOS.get(m.files[0][0].name, (0.0, 0.0, 0.0))
        assert o["pos_x"] == [pytest.approx(m.placed.x0 - lo[0], abs=1e-3)]
        assert o["pos_y"] == [pytest.approx(m.placed.y0 - lo[1], abs=1e-3)]
        assert o["pos_z"] == [pytest.approx(-lo[2], abs=1e-3)]
    body, base = g.members
    # the members sit inside the group's bbox, with at least the layout gap between them
    for m in g.members:
        assert g.placed.x0 - 1e-6 <= m.placed.x0 and m.placed.x1 <= g.placed.x1 + 1e-6
        assert g.placed.y0 - 1e-6 <= m.placed.y0 and m.placed.y1 <= g.placed.y1 + 1e-6
    assert (
        max(
            base.placed.x0 - body.placed.x1,
            body.placed.x0 - base.placed.x1,
            base.placed.y0 - body.placed.y1,
            body.placed.y0 - base.placed.y1,
        )
        >= 5
    )
    # 60 mm print-by-object clearance between the group and the other object
    assert max(text.placed.x0 - g.placed.x1, text.placed.y0 - g.placed.y1) >= 60
    assert g.placed.info.size[2] == 12.0


def test_project_checks_still_pass_with_a_filament_two_group(tmp_path, stls, resolver):
    d = spec_dict(
        tmp_path,
        stls,
        parts=[
            {"path": str(stls["body"]), "filament": 2, "count": 2},
            {"path": str(stls["text"]), "filament": 1, "name": "text"},
        ],
    )
    plan = plan_of(tmp_path, stls, resolver, d)
    assert plan.ok, (plan.problems, fails(plan))
    texts = [c.text for c in plan.plates[0].checks if c.ok]
    assert any(t.startswith("print-by-object: 2 object(s)") for t in texts)
    g = next(x for x in plan.parts if x.grouped)
    assert g.key == "group-f2" and g.placed.x0 >= 20.5  # the right nozzle reaches x >= 20.5
    # copies of one spec share one Item: count 2, the second position relative to the first copy
    item = next(o for o in plan.assemble.to_json()["plates"][0]["objects"] if o.get("assemble_index"))
    assert item["count"] == 2 and item["assemble_index"] == [1, 1]
    a, b = g.members
    assert item["pos_x"][1] == pytest.approx(b.placed.x0 - a.placed.x0, abs=1e-3)
    assert item["pos_y"][1] == pytest.approx(b.placed.y0 - a.placed.y0, abs=1e-3)
    assert item["pos_z"][1] == pytest.approx(0.0)


def test_different_settings_do_not_merge(tmp_path, stls, resolver):
    d = spec_dict(tmp_path, stls)
    d["plates"][0]["parts"][1]["set"] = {"wall_loops": "4"}
    plan = plan_of(tmp_path, stls, resolver, d)
    assert not any(x.grouped for x in plan.parts) and len(plan.parts) == 3


def test_parts_with_at_do_not_merge(tmp_path, stls, resolver):
    d = spec_dict(tmp_path, stls)
    d["plates"][0]["parts"][1]["at"] = [150, 150]
    plan = plan_of(tmp_path, stls, resolver, d)
    assert not any(x.grouped for x in plan.parts) and len(plan.parts) == 3


def test_two_groups_on_one_filament_get_numbered_names(tmp_path, stls, resolver):
    d = spec_dict(
        tmp_path,
        stls,
        parts=[
            {"path": str(stls["body"]), "filament": 1, "count": 2},
            {"path": str(stls["text"]), "filament": 1, "count": 2, "name": "text", "set": {"wall_loops": "4"}},
        ],
    )
    plan = plan_of(tmp_path, stls, resolver, d)
    assert sorted(x.key for x in plan.parts) == ["group-f1-1", "group-f1-2"]
    plate = plan.assemble.to_json()["plates"][0]
    assert sorted(a["assemble_index"] for a in plate["assembled_params"]) == [1, 2]
    walls = {a["assemble_index"]: a.get("print_params", {}).get("wall_loops") for a in plate["assembled_params"]}
    text_group = next(x for x in plan.parts if x.members[0].key.startswith("text"))
    assert walls[int(text_group.cli_name.split("_")[1])] == "4"


# ---------------------------------------------------------------------------- build, verify, report
def test_build_verifies_two_objects_and_reports_members(tmp_path, stls, resolver):
    fixture = make_3mf(
        tmp_path / "cli.3mf", object_names=("assemble_1", "text_1"), change_filament_gcode="M600", print_sequence="by object"
    )
    r = Pipeline(FakeStudio(fixture, sizes=SIZES, los=LOS), resolver).build(Spec.from_json(spec_dict(tmp_path, stls)))
    assert r.plan.ok
    assert not any("objects in file" in p for p in r.verify_problems or []), r.verify_problems
    assert r.verify_problems == []
    assert "group-f1 is one object of 2 parts on filament 1" in r.report
    assert "    - body: filament 1, 20.0 x 20.0 x 10.0 mm, at x " in r.report
    assert "    - base: filament 1, 30.0 x 20.0 x 12.0 mm" in r.report
    j = json.loads(json.dumps(r.to_json()))
    groups = j["plates"][0]["groups"]
    assert [g["name"] for g in groups] == ["group-f1"]
    assert [m["name"] for m in groups[0]["members"]] == ["body", "base"]
    assert len(j["plates"][0]["layout"]) == 2


def test_verify_counts_a_group_as_one_object(tmp_path, stls, resolver):
    two = make_3mf(tmp_path / "cli.3mf", object_names=("group-f1", "text"), change_filament_gcode="M600", print_sequence="by object")
    pipe = Pipeline(FakeStudio(sizes=SIZES, los=LOS), resolver)
    grouped = pipe.plan(Spec.from_json(spec_dict(tmp_path, stls)))
    assert not any("objects in file" in p for p in pipe.verify(grouped, two))
    d = spec_dict(tmp_path, stls)
    d["plates"][0]["group_by_filament"] = False  # three objects expected, the file holds two
    assert "2 objects in file, expected 3" in pipe.verify(pipe.plan(Spec.from_json(d)), two)
