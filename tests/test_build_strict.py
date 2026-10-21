"""Planning, naming and verify defects from the adversarial review: one block per defect, each test failed on the old pipeline."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from conftest import MACHINE, PROCESS, PROCESS_NAME, FakeStudio, make_3mf, mesh
from shambu import assemble as A
from shambu import layout as L
from shambu.build import Pipeline, _rename_steps, effective_filaments
from shambu.patches import RenameObject, SetFilamentMap
from shambu.spec import Spec
from shambu.studio import MeshInfo, RunResult
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
        def place(self, parts, obstacles=(), x_min_of=None, *, fixed=(), x_max_of=None):
            seen.update(x_min_of or {})
            return L.ShelfPacker().place(parts, obstacles, x_min_of, fixed=fixed)

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


# ---------------------------------------------------------------------------- 11. prime tower on every plate
def test_wipe_tower_position_is_set_for_every_plate(tmp_path, stls, resolver):
    """wipe_tower_x/y are per-plate vectors in Studio: a scalar moved the tower on plate 1 only, and every later plate
    kept the CLI default (15, 220), inside the right nozzle's dead strip (measured: exit -102 on an 8-plate project)."""
    fixture = make_3mf(tmp_path / "two.3mf", object_names=("body_1", "text_1"), **CHANGE_FILAMENT)
    plates = [
        {"name": "p", "parts": [{"path": str(stls["body"]), "name": "body"}]},
        {"name": "q", "parts": [{"path": str(stls["text"]), "name": "text", "filament": 2}]},
    ]
    studio = FakeStudio(fixture)
    Pipeline(studio, resolver).build(spec(tmp_path, plates), keep_going=True)
    args = next(a for kind, a in studio.calls if kind == "run")
    assert "--wipe-tower-x=208.0,208.0" in args and "--wipe-tower-y=208.0,208.0" in args


# ---------------------------------------------------------------------------- 12. layout checker (post-mortem 55, reference 08 section 11)
ONE = [{"preset": "Test PLA", "colour": "#000000"}]


@dataclass
class LogStudio(FakeStudio):
    """FakeStudio whose run log holds Studio's own lines (a -101 conflict, a -21 arrange failure)."""

    log_text: str = ""

    def run(self, cmd, *, name="run", outputdir=None, timeout=1200, expect_output=None):
        rr = super().run(cmd, name=name, outputdir=outputdir, timeout=timeout, expect_output=expect_output)
        with open(rr.log, "a", encoding="utf-8") as f:
            f.write(self.log_text)
        return rr


def boxes_spec(tmp_path, parts, **over):
    return spec(tmp_path, [{"name": "p", "parts": parts}], filaments=ONE, **over)


def stl_named(tmp_path, name):
    p = tmp_path / "stl" / f"{name}.stl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"solid x\nendsolid x\n")
    return p


def test_unplaced_part_gets_one_line_and_no_bed_fail(tmp_path, stls, resolver):
    studio = FakeStudio(sizes={"body.stl": (50.0, 300.0, 64.0)})
    r = Pipeline(studio, resolver).build(boxes_spec(tmp_path, [{"path": str(stls["body"]), "name": "feeler"}]), export=False)
    checks = r.plan.plates[0].checks
    bad = [c for c in checks if not c.ok]
    assert [c.kind for c in bad] == ["placement"]
    assert bad[0].text.startswith("the packer could not place feeler: 300.0 mm deep + 2 x 5.2 mm margin = 310.4 mm")
    assert "No position was computed for feeler, so no overlap, bed-edge or reach check ran for it" in bad[0].text
    assert "leaves the bed" not in r.report
    assert "- PLACEMENT NOT DONE: the packer could not place feeler" in r.report
    assert "feeler                       filament 1  not placed  (50.0 x 300.0 x 64.0 mm; brim 5.2 mm)" in r.report


def test_overflow_line_names_the_split(tmp_path, stls, resolver):
    studio = FakeStudio(sizes={"body.stl": (200.0, 200.0, 10.0)})
    plan = Pipeline(studio, resolver).plan(boxes_spec(tmp_path, [{"path": str(stls["body"]), "name": "big", "count": 3}]))
    bad = [c for c in plan.plates[0].checks if not c.ok]
    assert len(bad) == 1 and bad[0].kind == "placement"
    assert "the parts need 3 plates with their support spreads (plate 1: big; plate 2: big #2; plate 3: big #3)" in bad[0].text
    lines = L.describe([x.placed for x in plan.plates[0].parts])
    assert "not placed (would need plate 2)" in lines[1] and "not placed (would need plate 3)" in lines[2]


def test_exit_101_names_spec_keys(tmp_path, stls, resolver):
    parts = [
        {"path": str(stls["body"]), "name": "cube", "at": [10, 10]},
        {"path": str(stls["base"]), "name": "ell", "at": [40, 10], "recipes": ["tree-supports"]},
    ]
    studio = LogStudio(
        code=-101, log_text="[error] gcode path conflicts found between body_1 and base_1\nplate 1: found slicing result conflict!\n"
    )
    r = Pipeline(studio, resolver).build(boxes_spec(tmp_path, parts), do_slice=True)
    assert r.export_fails() == [
        "Studio found crossing G-code paths between cube and ell (exit -101). Move them apart "
        "(ell's support reaches 8.0 mm on -x, estimated) and build again."
    ]
    assert "- FAIL: Studio found crossing G-code paths between cube and ell (exit -101)" in r.report
    assert r.export_fails()[0] in r.to_json()["problems"]


def test_exit_21_explains_studio_arrange_spacing(tmp_path, stls, resolver):
    studio = LogStudio(code=-21, log_text="run:arrange failed: ap.name camera_mount_print_1 ap.bed_idx 1, plate index 0\n")
    r = Pipeline(studio, resolver).build(boxes_spec(tmp_path, [{"path": str(stls["body"])}]), do_slice=True)
    assert r.export_fails() == [
        "Studio's arrange could not fit the parts on plate 1 (it keeps 24 mm between objects when any part has tree supports)"
    ]


def test_clash_slice_never_passes_no_check(tmp_path, stls, resolver):
    studio = FakeStudio()
    Pipeline(studio, resolver).build(boxes_spec(tmp_path, [{"path": str(stls["body"])}]), do_slice=True)
    args = next(a for kind, a in studio.calls if kind == "run")
    assert any(a.split("=")[0] == "--slice" for a in args) and not any(a.startswith("--no-check") for a in args)


def test_brim_margin_override_becomes_a_spec_spread(tmp_path, stls, resolver):
    parts = [
        {"path": str(stls["body"]), "name": "feeler", "brim_margin": 5.2, "at": [10, 10]},
        {"path": str(stls["base"]), "name": "cradle", "at": [34, 10]},  # 4 mm from the feeler
    ]
    plan = Pipeline(FakeStudio(), resolver).plan(boxes_spec(tmp_path, parts))
    feeler = plan.plates[0].parts[0].placed
    assert feeler.margin == 0.0 and feeler.spread.sides() == (5.2,) * 4 and feeler.spread.source == "spec"
    (c,) = [c for c in plan.plates[0].checks if c.kind == "margin"]
    assert not c.ok and "feeler's support reaches 5.2 mm on +x (set in the spec)" in c.text


def test_support_spread_override_is_per_side(tmp_path, stls, resolver):
    parts = [
        {"path": str(stls["body"]), "name": "housing", "at": [10, 10], "recipes": ["tree-supports"], "support_spread": [21, 0.5, 0.5, 3]},
        {"path": str(stls["base"]), "name": "shell", "at": [34, 10]},  # 4 mm on the housing's +x side: 0.5 + 1 is enough
    ]
    plan = Pipeline(FakeStudio(), resolver).plan(boxes_spec(tmp_path, parts))
    sp = plan.plates[0].parts[0].placed.spread
    assert sp.sides() == (21.0, 0.5, 0.5, 3.0) and sp.source == "spec" and sp.tree
    assert all(c.ok and not c.warn for c in plan.plates[0].checks)


def test_support_spread_must_be_four_non_negative_numbers(tmp_path, stls):
    from shambu.errors import BuildError
    from shambu.spec import PartSpec

    for bad in ([1, 2, 3], [1, 2, 3, -1], "1234", {"mx": 1}):
        with pytest.raises(BuildError, match="support_spread must be four non-negative numbers"):
            PartSpec.from_json({"path": str(stls["body"]), "support_spread": bad})
    ok = PartSpec.from_json({"path": str(stls["body"]), "support_spread": [1, 2, 3, 4]})
    assert ok.support_spread == (1.0, 2.0, 3.0, 4.0) and ok.to_json()["support_spread"] == [1.0, 2.0, 3.0, 4.0]


def test_post_mortem_plate_checks_clean_with_estimates(tmp_path, resolver, capsys, monkeypatch):
    """The post-mortem regression: three box parts, 10 mm apart as on the plate that printed, all with tree supports.
    0.3.0 gave 7 FAILs (summed 25 + 12 mm margins, tree spread held off the bed edge). Now: WARN lines at most, exit 0."""
    from shambu import cli
    from shambu.facade import Bambu

    sizes = {"housing.stl": (124.0, 135.0, 218.0), "shell.stl": (59.0, 61.0, 48.0), "mount.stl": (41.0, 59.0, 32.0)}
    studio = FakeStudio(sizes=sizes)
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=studio, resolver=resolver))
    parts = [
        {"path": str(stl_named(tmp_path, k)), "name": k, "at": at, "recipes": ["tree-supports"]}
        for k, at in (("housing", [10, 10]), ("shell", [144, 10]), ("mount", [144, 81]))
    ]
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(boxes_spec(tmp_path, parts).to_json()), encoding="utf-8")
    code = cli.main(["check", "--no-printer", str(p)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "FAIL" not in out and "PLACEMENT NOT DONE" not in out
    warns = [line for line in out.splitlines() if line.startswith("- WARN margin rule:")]
    assert warns == [
        "- WARN margin rule: housing and shell are 10.0 mm apart; support reaches 21.8 mm (estimated); the slice decides",
        "- WARN margin rule: housing and mount are 10.0 mm apart; support reaches 21.8 mm (estimated); the slice decides",
    ]


# ---------------------------------------------------------------------------- 13. support probe (a fixture G-code, no Studio)
PROBE_GCODE = """; HEADER_BLOCK_START
G1 X5 Y5 F3000
; FEATURE: Custom
G1 X60 Y5 E5
; start printing object, unique label id: 15
G1 X100 Y100 F3000
; FEATURE: Outer wall
G1 X120 Y100 E1
G1 X120 Y120 E1
G1 X100 Y120 E1
G1 X100 Y100 E1
; FEATURE: Support
G1 X80 Y105 F3000
G1 X95 Y105 E.5
G1 X121 Y110 E.5
G3 X121 Y118 I0 J4 E.2
; FEATURE: Brim
G1 X90 Y90 F3000
G1 X60 Y90 E1
; stop printing object, unique label id: 15
G1 X0 Y0 E5
"""


def test_probe_parses_support_extents_per_side():
    from shambu.probe import parse_support_spread

    got = parse_support_spread(PROBE_GCODE.splitlines())
    assert got == pytest.approx((20.0, 5.0, 0.0, 0.0), abs=0.05)  # the arc bulges to x 125; brim and purge line ignored
    assert parse_support_spread(["G1 X1 Y1 E1"]) is None  # no model extrusion inside an object block


def test_probe_parses_a_solo_slice_without_object_labels():
    """Studio writes no object labels when the plate holds one object (live, 02.08.02.61): every move after the first
    layer change counts, and the start G-code (`Custom`) never does."""
    from shambu.probe import parse_support_spread

    solo = [
        line
        for line in PROBE_GCODE.splitlines()
        if not line.startswith(("; start printing object", "; stop printing object")) and line != "G1 X0 Y0 E5"
    ]
    solo.insert(solo.index("G1 X100 Y100 F3000"), "; CHANGE_LAYER")
    assert parse_support_spread(solo) == pytest.approx((20.0, 5.0, 0.0, 0.0), abs=0.05)


@dataclass
class ProbeStudio(FakeStudio):
    """Writes a .gcode.3mf with the fixture G-code for every probe slice and counts them."""

    probes: int = 0

    def run(self, cmd, *, name="run", outputdir=None, timeout=1200, expect_output=None):
        if name != "probe":
            return super().run(cmd, name=name, outputdir=outputdir, timeout=timeout, expect_output=expect_output)
        import zipfile

        self.probes += 1
        with zipfile.ZipFile(expect_output, "w") as z:
            z.writestr("Metadata/plate_1.gcode", PROBE_GCODE)
        return RunResult(["BambuStudio"], 0, 0, 0.1, Path(outputdir) / "probe.log", {"return_code": 0}, expect_output)


def test_support_probe_is_cached_by_mesh_hash(tmp_path, resolver):
    from shambu import mesh as M
    from test_mesh import box

    a = M.write_stl(tmp_path / "stl" / "a.stl", box((0, 0, 0), (20, 20, 10)))
    b = M.write_stl(tmp_path / "stl" / "b.stl", box((5, 5, 0), (25, 25, 10)))  # another mesh, same box size
    parts = [
        {"path": str(a), "name": "a", "recipes": ["tree-supports"]},
        {"path": str(b), "name": "b", "recipes": ["tree-supports"]},
        {"path": str(a), "name": "plain"},  # no support: never probed
    ]
    studio = ProbeStudio(los={"b.stl": (5.0, 5.0, 0.0)})
    for _ in range(2):
        plan = Pipeline(studio, resolver).plan(boxes_spec(tmp_path, parts), probe_supports=True)
    assert studio.probes == 2  # one solo slice per supported mesh; the second plan reads <outdir>/.probe
    sp = plan.plates[0].parts[0].placed.spread
    assert sp.sides() == pytest.approx((20.0, 5.0, 0.0, 0.0), abs=0.05) and sp.source == "probe" and sp.tree
    assert len(list((tmp_path / "out" / ".probe").glob("*.json"))) == 2
    assert plan.plates[0].parts[2].placed.spread.source == "none"


def test_footprint_comes_from_the_oriented_stl(tmp_path, resolver):
    from shambu import mesh as M
    from test_mesh import box

    u = [f for r in ((0, 0, 10, 100), (10, 0, 40, 10), (10, 90, 40, 100)) for f in box((r[0], r[1], 0), (r[2], r[3], 10))]
    path = M.write_stl(tmp_path / "stl" / "u.stl", u)
    plan = Pipeline(FakeStudio(sizes={"u.stl": (40.0, 100.0, 10.0)}), resolver).plan(boxes_spec(tmp_path, [{"path": str(path)}]))
    fp = plan.plates[0].parts[0].placed.footprint
    assert isinstance(fp, L.RasterFootprint) and fp.area() == pytest.approx(1600.0)
    plan = Pipeline(FakeStudio(sizes={"u.stl": (55.0, 100.0, 10.0)}), resolver).plan(boxes_spec(tmp_path, [{"path": str(path)}]))
    assert plan.plates[0].parts[0].placed.footprint is None  # Studio's box differs from the STL's: keep the box


def test_a_failed_probe_keeps_the_estimate_with_a_warning(tmp_path, stls, resolver):
    from shambu.errors import StudioError

    class Fixed:  # a SpreadProbe fake: the first part measures, the second fails
        def measure(self, req):
            if req.key == "bad":
                raise StudioError("no G-code")
            return L.Spread(3.0, 0.0, 0.0, 1.0, "probe", True)

    parts = [
        {"path": str(stls["body"]), "name": "good", "recipes": ["tree-supports"]},
        {"path": str(stls["base"]), "name": "bad", "recipes": ["tree-supports"]},
    ]
    plan = Pipeline(FakeStudio(), resolver, probe=Fixed()).plan(boxes_spec(tmp_path, parts), probe_supports=True)
    good, bad = (x.placed.spread for x in plan.plates[0].parts)
    assert good.sides() == (3.0, 0.0, 0.0, 1.0) and bad.source == "estimate"
    assert any("(bad): support probe failed (no G-code); the estimate applies" in w for w in plan.warnings)
