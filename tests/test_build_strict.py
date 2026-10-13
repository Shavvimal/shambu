"""Planning, naming and verify defects from the adversarial review: one block per defect, each test failed on the old pipeline."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from conftest import MACHINE, PROCESS, PROCESS_NAME, FakeStudio, make_3mf, mesh
from shambu import assemble as A
from shambu import layout as L
from shambu.build import Pipeline, _rename_steps, effective_filaments
from shambu.patches import RenameObject, SetFilamentMap
from shambu.spec import Spec
from shambu.studio import MeshInfo
from shambu.threemf import Project

TWO = [{"preset": "Test PLA", "colour": "#000000"}, {"preset": "Test PLA", "colour": "#FFFFFF"}]
CHANGE_FILAMENT = {"change_filament_gcode": "M600"}


@dataclass
class MeshStudio(FakeStudio):
    """FakeStudio with a full MeshInfo per file name (offset, manifold), for multipart geometry."""

    meshes: dict[str, MeshInfo] = field(default_factory=dict)

    def info(self, path) -> MeshInfo:
        path = Path(path)
        if path.name in self.meshes:
            self.calls.append(("info", path))
            return self.meshes[path.name]
        return super().info(path)


def spec(tmp_path, plates, filaments=TWO, **over) -> Spec:
    d = {
        "name": "strict",
        "outdir": str(tmp_path / "out"),
        "printer": MACHINE,
        "process": {"base": PROCESS, "name": PROCESS_NAME, "set": {"wall_loops": "2"}},
        "filaments": filaments,
        "bed_type": "Textured PEI Plate",
        "plates": plates,
    }
    d.update(over)
    return Spec.from_json(d)


def plan_of(tmp_path, resolver, plates, studio=None, **over):
    return Pipeline(studio or FakeStudio(), resolver).plan(spec(tmp_path, plates, **over))


def parts_of(plan) -> list:
    return [x for p in plan.plates for x in p.parts]


def fails(plan) -> list[str]:
    return [c.text for p in plan.plates for c in p.checks if not c.ok]


# ---------------------------------------------------------------------------- 1. effective filaments
def test_effective_filaments_resolves_every_override(stls):
    from shambu.spec import PartSpec

    ps = PartSpec.from_json(
        {
            "path": str(stls["body"]),
            "filament": 1,
            "set": {"extruder": "2", "support_interface_filament": "3", "support_filament": "0"},
            "height_ranges": [{"min_z": 0, "max_z": 1, "set": {"extruder": "4"}}],
        }
    )
    assert effective_filaments(ps) == {2, 3, 4}  # 1 is replaced by set.extruder; 0 means "default"
    multi = PartSpec.from_json(
        {
            "name": "m",
            "parts": [
                {"path": str(stls["base"])},
                {"path": str(stls["text"]), "filament": 2},
                {"path": str(stls["body"]), "filament": 3, "subtype": "negative_part"},
            ],
        }
    )
    assert effective_filaments(multi) == {1, 2}  # a negative part prints nothing


def test_set_extruder_override_is_checked_against_its_reach(tmp_path, stls, resolver):
    plan = plan_of(
        tmp_path,
        resolver,
        [{"name": "p", "parts": [{"path": str(stls["body"]), "filament": 1, "set": {"extruder": "2"}, "at": [5, 30], "brim_margin": 0}]}],
    )
    assert any("extruder 2" in t and "reaches x 20.5" in t for t in fails(plan))


def test_support_filament_override_is_checked_against_its_reach(tmp_path, stls, resolver):
    plan = plan_of(
        tmp_path,
        resolver,
        [
            {
                "name": "p",
                "parts": [{"path": str(stls["body"]), "filament": 1, "set": {"support_filament": "2"}, "at": [5, 30], "brim_margin": 0}],
            }
        ],
    )
    assert any("uses filament 2" in t for t in fails(plan))


def test_unknown_override_slot_is_a_problem(tmp_path, stls, resolver):
    plan = plan_of(tmp_path, resolver, [{"name": "p", "parts": [{"path": str(stls["body"]), "set": {"extruder": "99"}}]}])
    assert any("[99]" in p for p in plan.problems)
    assert not plan.ok


def test_tpu_through_set_extruder_on_aux_fails(tmp_path, stls, resolver):
    tpu = [{"preset": "Test PLA", "colour": "#000000"}, {"preset": "Test TPU", "colour": "#FFFFFF"}]
    plan = plan_of(tmp_path, resolver, [{"name": "p", "parts": [{"path": str(stls["body"]), "set": {"extruder": "2"}}]}], filaments=tpu)
    assert any("TPU" in t for t in fails(plan))


def test_override_filament_makes_the_plate_multi_and_reserves_the_tower(tmp_path, stls, resolver):
    plan = plan_of(
        tmp_path,
        resolver,
        [{"name": "p", "parts": [{"path": str(stls["body"]), "set": {"support_filament": "2"}, "at": [210, 210], "brim_margin": 0}]}],
    )
    assert any("prime-tower reserve" in t for t in fails(plan))


# ---------------------------------------------------------------------------- 2. multipart geometry
def test_multipart_uses_union_bbox_height_and_manifold(tmp_path, stls, resolver):
    studio = MeshStudio(
        meshes={"base.stl": mesh("base.stl", (20, 20, 10)), "text.stl": mesh("text.stl", (10, 10, 330), (30, 0, 0), manifold=False)}
    )
    plan = plan_of(
        tmp_path,
        resolver,
        [{"name": "p", "parts": [{"name": "label", "parts": [{"path": str(stls["base"])}, {"path": str(stls["text"])}]}]}],
        studio,
    )
    label = parts_of(plan)[0]
    assert (label.placed.w, label.placed.info.size[2]) == (40, 330)
    t = fails(plan)
    assert any("330.0 mm tall" in x for x in t)
    assert any("label is not manifold" in x for x in t)
    assert any("sub-part text.stl is not manifold" in x for x in t)


# ---------------------------------------------------------------------------- 3. repeated STL
def test_object_referencing_one_stl_twice_is_kept(tmp_path, stls, resolver):
    plan = plan_of(
        tmp_path,
        resolver,
        [
            {
                "name": "p",
                "parts": [
                    {"name": "pair", "parts": [{"path": str(stls["base"])}, {"path": str(stls["base"]), "subtype": "modifier_part"}]}
                ],
            }
        ],
    )
    assert [p.key for p in parts_of(plan)] == ["pair"]
    assert len(parts_of(plan)[0].infos) == 2
    objs = plan.assemble.to_json()["plates"][0]["objects"]
    assert len(objs) == 2


# ---------------------------------------------------------------------------- 4. single-filament explicit map
def test_single_filament_explicit_map_is_emitted_patched_and_verified(tmp_path, stls, resolver):
    one = [{"preset": "Test PLA", "colour": "#000000"}]
    fixture = make_3mf(
        tmp_path / "one.3mf", object_names=("body_1", "text_1"), filament_settings_id=["Test PLA"], filament_type=["PLA"], **CHANGE_FILAMENT
    )
    studio = FakeStudio(fixture)
    sp = spec(
        tmp_path, [{"name": "p", "parts": [{"path": str(stls["body"])}, {"path": str(stls["text"])}]}], filaments=one, filament_map=[2]
    )
    r = Pipeline(studio, resolver).build(sp)
    args = next(a for kind, a in studio.calls if kind == "run")
    assert "--filament-map=2" in args and "--filament-map-mode=Manual" in args
    assert any(isinstance(x, SetFilamentMap) for x in Pipeline(studio, resolver).patch_set(r.plan).patches)
    assert r.plan.assemble.plates[0].plate_params["filament_map"] == "2"
    assert Project(str(r.project)).summary()["filament_map"] == ["2"]
    assert r.verify_problems == []


# ---------------------------------------------------------------------------- 5. strict verify
def two_part_plates(stls):
    return [
        {
            "name": "p",
            "parts": [
                {"path": str(stls["body"]), "name": "body"},
                {"name": "label", "parts": [{"path": str(stls["base"])}, {"path": str(stls["text"]), "filament": 2}]},
            ],
        }
    ]


def build_with(tmp_path, stls, resolver, fixture, plates=None, pipeline_cls=Pipeline, **over):
    return pipeline_cls(FakeStudio(fixture), resolver).build(spec(tmp_path, plates or two_part_plates(stls), **over), keep_going=True)


def test_verify_requires_every_process_override(tmp_path, stls, resolver):
    fx = make_3mf(tmp_path / "f.3mf", object_names=("body_1", "assemble_1"), **CHANGE_FILAMENT)
    r = build_with(
        tmp_path,
        stls,
        resolver,
        fx,
        process={"base": PROCESS, "name": PROCESS_NAME, "set": {"wall_loops": "2", "sparse_infill_density": "20%"}},
    )
    assert any("sparse_infill_density is missing" in p for p in r.verify_problems)


def test_verify_compares_machine_gcode_with_the_preset(tmp_path, stls, resolver):
    fx = make_3mf(tmp_path / "f.3mf", object_names=("body_1", "assemble_1"), machine_start_gcode="X" * 1500, **CHANGE_FILAMENT)
    r = build_with(tmp_path, stls, resolver, fx)
    assert any(p.startswith("machine_start_gcode") for p in r.verify_problems)


def test_verify_compares_filament_presets(tmp_path, stls, resolver):
    fx = make_3mf(tmp_path / "f.3mf", object_names=("body_1", "assemble_1"), filament_settings_id=["Other", "Test PLA"], **CHANGE_FILAMENT)
    r = build_with(tmp_path, stls, resolver, fx)
    assert any("filament presets in file" in p for p in r.verify_problems)


def test_verify_requires_plate_bed_type(tmp_path, stls, resolver):
    class NoPatch(Pipeline):
        def patch(self, plan, project, sliced=False):
            return []

    fx = make_3mf(tmp_path / "f.3mf", object_names=("body", "label"), **CHANGE_FILAMENT)
    r = build_with(tmp_path, stls, resolver, fx, pipeline_cls=NoPatch)
    assert any("plate 1 bed type is None" in p for p in r.verify_problems)


def test_verify_compares_plate_filament_maps(tmp_path, stls, resolver):
    fx = make_3mf(tmp_path / "f.3mf", object_names=("body_1", "assemble_1"), **CHANGE_FILAMENT)
    pr = Project(str(fx))
    pr.set_filament_map([1, 1], plate=1)
    pr.save(str(fx))
    r = build_with(tmp_path, stls, resolver, fx)
    assert any("plate 1 filament_maps" in p for p in r.verify_problems)


def test_verify_counts_objects_from_the_spec(tmp_path, stls, resolver):
    fx = make_3mf(tmp_path / "f.3mf", object_names=("body_1", "assemble_1"), **CHANGE_FILAMENT)
    plates = two_part_plates(stls)
    plates[0]["parts"].append({"path": str(tmp_path / "missing.stl")})
    r = build_with(tmp_path, stls, resolver, fx, plates=plates)
    assert "2 objects in file, expected 3" in r.verify_problems


# ---------------------------------------------------------------------------- 6. unique names
def test_assemble_index_continues_across_plates(tmp_path, stls, resolver):
    label = {"name": "label", "parts": [{"path": str(stls["base"])}, {"path": str(stls["text"]), "filament": 2}]}
    plan = plan_of(tmp_path, resolver, [{"name": "a", "parts": [label]}, {"name": "b", "parts": [label]}])
    assert [p.cli_name for p in parts_of(plan)] == ["assemble_1", "assemble_2"]
    assert [p.key for p in parts_of(plan)] == ["label", "label #2"]
    idx = [o.get("assemble_index") for pl in plan.assemble.to_json()["plates"] for o in pl["objects"]]
    assert idx == [[1], [1], [2], [2]]


def test_same_stem_twice_keeps_cli_name_and_warns(tmp_path, stls, resolver):
    plan = plan_of(tmp_path, resolver, [{"name": "p", "parts": [{"path": str(stls["body"])}, {"path": str(stls["body"])}]}])
    assert [p.key for p in parts_of(plan)] == ["body", "body #2"]
    assert [p.file_name for p in parts_of(plan)] == ["body_1", "body_1"]
    assert any("'body_1'" in w for w in plan.warnings)
    assert not any(isinstance(x, RenameObject) for x in Pipeline(FakeStudio(), resolver).patch_set(plan).patches)


def test_rename_steps_order_and_cycle():
    assert _rename_steps([("a_1", "b_1"), ("b_1", "c")]) == [("b_1", "c"), ("a_1", "b_1")]
    steps = _rename_steps([("x", "y"), ("y", "x")])
    names = {"x": "X", "y": "Y"}
    for old, new in steps:
        assert new not in names
        names[new] = names.pop(old)
    assert names == {"y": "X", "x": "Y"}


# ---------------------------------------------------------------------------- 7. bed margin and reserve
FACTS = {
    "bed": {"x": (0.0, 256.0), "y": (0.0, 256.0)},
    "z_max": 325.0,
    "extruders": [{"index": 1, "x": (0.0, 256.0), "y": (0.0, 256.0), "z_max": 320.0, "type": "Direct Drive", "nozzle": "0.4"}],
}


def test_inside_bed_counts_the_brim_margin():
    p = L.Placed("a", mesh("a.stl"), 1, 1.0, 30.0, margin=3.0)
    assert any("leaves the bed" in c.text for c in L.LimitChecker().check([p], L.Context(FACTS)) if not c.ok)


def test_reserve_rule_flags_a_fixed_part_in_the_tower_corner():
    p = L.Placed("a", mesh("a.stl"), 1, 210.0, 210.0, fixed=True)
    ctx = L.Context(FACTS, reserve=(203.0, 203.0, 256.0, 256.0))
    assert any("prime-tower reserve" in c.text for c in L.LimitChecker().check([p], ctx) if not c.ok)
    assert all(c.ok for c in L.LimitChecker().check([p], L.Context(FACTS)))


# ---------------------------------------------------------------------------- 8. assemble.validate
def test_validate_accepts_per_copy_assemble_index(stls):
    al = A.AssembleList(
        [
            A.Plate(
                "p",
                [A.Item(stls["base"], 1, 2, assemble_index=[1, 2], pos=[(0, 0, 0), (30, 0, 0)])],
                assembled=[A.Assembled(1), A.Assembled(2)],
            )
        ]
    )
    assert A.validate(al.to_json()) == []


def test_validate_checks_assemble_index_length(stls):
    data = A.AssembleList([A.Plate("p", [A.Item(stls["base"], 1, 2, assemble_index=[1, 2, 3])])]).to_json()
    assert any("assemble_index has 3 entries" in e for e in A.validate(data))


# ---------------------------------------------------------------------------- 9. Placer gets x_min_of
def test_any_placer_receives_the_reach_limits(tmp_path, stls, resolver):
    seen = {}

    class Recording:
        def place(self, parts, obstacles=(), x_min_of=None):
            seen.update(x_min_of or {})
            return L.ShelfPacker().place(parts, obstacles, x_min_of)

    sp = spec(tmp_path, two_part_plates(stls))
    Pipeline(FakeStudio(), resolver, placer=Recording()).plan(sp)
    assert seen == {"body": 0.0, "label": 20.5}
    assert not hasattr(L.ShelfPacker(), "x_min_of")


def test_report_lists_effective_filaments(tmp_path, stls, resolver):
    r = Pipeline(FakeStudio(), resolver).build(
        spec(tmp_path, [{"name": "p", "parts": [{"path": str(stls["body"]), "set": {"support_filament": "2"}}]}]), export=False
    )
    assert "prints with filament(s) 1, 2" in r.report
    json.dumps(r.to_json())
