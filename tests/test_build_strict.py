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
            "set": {"extruder": "2", "enable_support": "1", "support_interface_filament": "3", "support_filament": "0"},
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
                "parts": [
                    {
                        "path": str(stls["body"]),
                        "filament": 1,
                        "set": {"enable_support": "1", "support_filament": "2"},
                        "at": [5, 30],
                        "brim_margin": 0,
                    }
                ],
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


def test_override_filament_makes_the_plate_multi_and_places_a_tower_clear_of_it(tmp_path, stls, resolver):
    plan = plan_of(
        tmp_path,
        resolver,
        [
            {
                "name": "p",
                "parts": [
                    {"path": str(stls["body"]), "set": {"enable_support": "1", "support_filament": "2"}, "at": [210, 210], "brim_margin": 0}
                ],
            }
        ],
    )
    tower = plan.plates[0].tower  # the plate is multi: an "auto" tower lands clear of the part (tower.py)
    assert tower is not None and tower.footprint is not None
    assert not L.overlaps(tower.footprint.rect, plan.plates[0].parts[0].placed.rect())


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


def test_inside_bed_counts_the_brim_margin_as_a_clipped_brim():
    """Studio clips a brim at the bed edge (Brim.cpp:1011-1138): a WARN, not a FAIL."""
    p = L.Placed("a", mesh("a.stl"), 1, 1.0, 30.0, brim=3.0)
    checks = L.LimitChecker().check([p], L.Context(FACTS))
    assert all(c.ok for c in checks)
    (w,) = [c for c in checks if c.kind == "brim"]
    assert w.warn and "the 3.0 mm brim reaches x -2.0..24.0" in w.text and "will be clipped at x 0.0..24.0" in w.text


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
        spec(tmp_path, [{"name": "p", "parts": [{"path": str(stls["body"]), "set": {"enable_support": "1", "support_filament": "2"}}]}]),
        export=False,
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
    assert bad[0].text.startswith("the packer could not place feeler: 300.0 mm deep + 2 x 0.0 mm spread = 300.0 mm")
    assert "No position was computed for feeler, so no overlap, bed-edge or reach check ran for it" in bad[0].text
    assert "leaves the bed" not in r.report
    assert "- PLACEMENT NOT DONE: the packer could not place feeler" in r.report
    assert "feeler                       filament 1  not placed  (50.0 x 300.0 x 64.0 mm; brim 5.2 mm)" in r.report


def test_overflow_line_names_the_split(tmp_path, stls, resolver):
    studio = FakeStudio(sizes={"body.stl": (200.0, 200.0, 10.0)})
    plan = Pipeline(studio, resolver).plan(boxes_spec(tmp_path, [{"path": str(stls["body"]), "name": "big", "count": 3}]))
    bad = [c for c in plan.plates[0].checks if not c.ok]
    assert len(bad) == 1 and bad[0].kind == "placement"
    assert "put the parts on 3 plates with their support spreads (plate 1: big; plate 2: big #2; plate 3: big #3)" in bad[0].text
    assert "the packer's answer (a heuristic), not a proof" in bad[0].text
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
    assert feeler.brim == 0.0 and feeler.spread.sides() == (5.2,) * 4 and feeler.spread.source == "spec"
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
        "- WARN margin rule: housing and shell are 10.0 mm apart; support reaches 26.0 mm (estimated); the slice decides",
        "- WARN margin rule: housing and mount are 10.0 mm apart; support reaches 26.0 mm (estimated); the slice decides",
        "- WARN margin rule: shell and mount are 10.0 mm apart; support reaches 10.6 mm (estimated); the slice decides",
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
M204 S6000
G1 X80 Y105 F60000
G1 Z.4
G1 Z.2
G1 E.8 F1800
; FEATURE: Support
; LINE_WIDTH: 0.5
G1 F3000
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
    from shambu.probe import parse_support

    got = parse_support(PROBE_GCODE.splitlines()).sides
    assert got == pytest.approx((20.0, 5.0, 0.0, 0.0), abs=0.05)  # the arc bulges to x 125; brim and purge line ignored
    assert parse_support(["G1 X0 Y0", "G1 X1 Y1 E1"]).sides is None  # no model extrusion inside an object block


def test_probe_parses_a_solo_slice_without_object_labels():
    """Studio writes no object labels when the plate holds one object (live, 02.08.02.61): every move after the first
    layer change counts, and the start G-code (`Custom`) never does."""
    from shambu.probe import parse_support

    solo = [
        line
        for line in PROBE_GCODE.splitlines()
        if not line.startswith(("; start printing object", "; stop printing object")) and line != "G1 X0 Y0 E5"
    ]
    solo.insert(solo.index("G1 X100 Y100 F3000"), "; CHANGE_LAYER")
    assert parse_support(solo).sides == pytest.approx((20.0, 5.0, 0.0, 0.0), abs=0.05)


@dataclass
class ProbeStudio(FakeStudio):
    """Writes a .gcode.3mf with the fixture G-code for every probe slice and counts them."""

    probes: int = 0
    ver: str = "02.08.02.61"
    gcode: str = PROBE_GCODE
    probe_mode: str = "ok"  # ok, fail (a non-zero slice), nogcode (an archive without G-code), badzip
    positions: list = field(default_factory=list)  # the pos of every probed object, from its assemble list

    def version(self) -> str:
        return self.ver

    def run(self, cmd, *, name="run", outputdir=None, timeout=1200, expect_output=None):
        if not name.startswith("probe"):
            return super().run(cmd, name=name, outputdir=outputdir, timeout=timeout, expect_output=expect_output)
        import zipfile

        self.probes += 1
        self.positions.append(json.loads((Path(outputdir) / "assemble.json").read_text())["plates"][0]["objects"][0]["pos_x"])
        log = Path(outputdir) / f"{name}.log"
        log.write_text("$ fake probe\n", encoding="utf-8")
        if self.probe_mode == "fail":
            return RunResult(["BambuStudio"], 156, -100, 0.1, log, {"return_code": -100}, expect_output)
        if self.probe_mode == "badzip":
            Path(expect_output).write_bytes(b"not a zip")
        else:
            with zipfile.ZipFile(expect_output, "w") as z:
                if self.probe_mode != "nogcode":
                    z.writestr("Metadata/plate_1.gcode", self.gcode)
                z.writestr("Metadata/model_settings.config", "<config/>")
        return RunResult(["BambuStudio"], 0, 0, 0.1, log, {"return_code": 0}, expect_output)


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


@dataclass
class StlStudio(FakeStudio):
    """FakeStudio whose --info measures the STL itself, so oriented copies get their real box."""

    def info(self, path) -> MeshInfo:
        from shambu import mesh as M

        path = Path(path)
        self.calls.append(("info", path))
        lo, hi = M.bbox(M.read_stl(path))
        return mesh(path, tuple(b - a for a, b in zip(lo, hi, strict=True)), lo)


def test_footprint_comes_from_the_oriented_stl(tmp_path, resolver):
    """The silhouette is taken from the oriented copy the CLI loads: a U open to +x, turned 90 degrees about z, opens
    to +y, and its 1600 mm2 outline (not its 4000 mm2 box) is what the layout sees."""
    from shambu import mesh as M
    from test_mesh import box

    u = [f for r in ((0, 0, 10, 100), (10, 0, 40, 10), (10, 90, 40, 100)) for f in box((r[0], r[1], 0), (r[2], r[3], 10))]
    path = M.write_stl(tmp_path / "stl" / "u.stl", u)
    plan = Pipeline(StlStudio(), resolver).plan(boxes_spec(tmp_path, [{"path": str(path), "rotate": [0, 0, 90]}]))
    x = plan.plates[0].parts[0]
    assert x.files[0][0].name != "u.stl"  # the oriented copy
    fp = x.placed.footprint
    assert isinstance(fp, L.RasterFootprint) and fp.area() == pytest.approx(1600.0)
    assert (x.placed.w, x.placed.h) == pytest.approx((100.0, 40.0))
    (lo_row, *_), top = fp.rows, fp.rows[-1]
    assert len(lo_row[2]) == 1 and len(top[2]) == 2  # the back now runs along the bottom, the two arms point to +y
    plan = Pipeline(FakeStudio(sizes={"u.stl": (55.0, 100.0, 10.0)}), resolver).plan(boxes_spec(tmp_path, [{"path": str(path)}]))
    assert plan.plates[0].parts[0].placed.footprint is None  # Studio's box differs from the STL's: keep the box
    assert any(
        "the layout uses the bounding box, not the outline: the mesh's box differs from Studio's by 15.0 mm" in w for w in plan.warnings
    )


def test_a_failed_probe_keeps_the_estimate_with_a_warning(tmp_path, stls, resolver):
    from shambu.errors import StudioError
    from shambu.probe import ProbeResult

    class Fixed:  # a SpreadProbe fake: the first part measures, the second fails
        def measure(self, req):
            if req.key == "bad":
                raise StudioError("no G-code")
            return ProbeResult(L.Spread(3.0, 0.0, 0.0, 1.0, "probe", True))

    parts = [
        {"path": str(stls["body"]), "name": "good", "recipes": ["tree-supports"]},
        {"path": str(stls["base"]), "name": "bad", "recipes": ["tree-supports"]},
    ]
    plan = Pipeline(FakeStudio(), resolver, probe=Fixed()).plan(boxes_spec(tmp_path, parts), probe_supports=True)
    good, bad = (x.placed.spread for x in plan.plates[0].parts)
    assert good.sides() == (3.0, 0.0, 0.0, 1.0) and bad.source == "estimate"
    assert any("(bad): support probe failed (no G-code); the estimate applies" in w for w in plan.warnings)


# ---------------------------------------------------------------------------- 14. review fixes (Codex, 2026-10-21)
def test_keep_going_never_exports_an_unplaced_part(tmp_path, stls, resolver):
    """--keep-going overrides advisory checks, never a part without a position: its pos would be one nobody chose."""
    studio = FakeStudio(sizes={"body.stl": (200.0, 200.0, 10.0)})
    r = Pipeline(studio, resolver).build(boxes_spec(tmp_path, [{"path": str(stls["body"]), "name": "big", "count": 3}]), keep_going=True)
    assert r.plan.unplaced == ["big #2", "big #3"] and not r.plan.exportable
    assert [k for k, _ in studio.calls if k == "run"] == []
    assert r.run is None and "`--keep-going` never exports a plate with parts that have no position (big #2, big #3)" in r.report


def test_keep_going_still_exports_past_an_advisory_fail(tmp_path, stls, resolver):
    parts = [{"path": str(stls["body"]), "name": "a", "at": [20, 20]}, {"path": str(stls["base"]), "name": "b", "at": [20, 20]}]
    studio = FakeStudio()
    Pipeline(studio, resolver).build(boxes_spec(tmp_path, parts), keep_going=True)
    assert [k for k, _ in studio.calls if k == "run"]  # placed but overlapping: the user asked to export anyway


def test_exit_101_names_the_second_copy(tmp_path, stls, resolver):
    """shambu's `count` copies are separate model objects (live: two <object> entries and two build items in the
    exported 3MF), so Studio's conflict check, which reads the first instance of each PrintObject
    (ConflictChecker.cpp:244-245), sees every copy, and the log names copy 2 by its own CLI name."""
    parts = [
        {"path": str(stls["body"]), "name": "cube", "count": 2},
        {"path": str(stls["base"]), "name": "ell", "at": [200, 10]},
    ]
    studio = LogStudio(code=-101, log_text="[error] gcode path conflicts found between body_2 and base_1\n")
    r = Pipeline(studio, resolver).build(boxes_spec(tmp_path, parts), do_slice=True)
    assert [p.cli_name for p in r.plan.parts] == ["body_1", "body_2", "base_1"]
    assert r.export_fails() == ["Studio found crossing G-code paths between cube #2 and ell (exit -101). Move them apart and build again."]


def test_raft_only_part_gets_a_spread_and_pins_its_support_filaments(tmp_path, stls, resolver):
    parts = [{"path": str(stls["body"]), "name": "raft", "set": {"raft_layers": "3"}}, {"path": str(stls["base"]), "filament": 2}]
    plan = Pipeline(FakeStudio(), resolver).plan(spec(tmp_path, [{"name": "p", "parts": parts}]))
    raft = plan.plates[0].parts[0]
    assert raft.placed.spread.most() == 5.0 and raft.placed.spread.source == "estimate"
    assert plan.assemble.plates[0].items[0].print_params["support_interface_filament"] == "1"
    assert any("raft:support_interface_filament" in w for w in plan.warnings)


def test_support_probe_cache_misses_when_the_studio_version_changes(tmp_path, resolver):
    from shambu import mesh as M
    from test_mesh import box

    a = M.write_stl(tmp_path / "stl" / "a.stl", box((0, 0, 0), (20, 20, 10)))
    parts = [{"path": str(a), "name": "a", "recipes": ["tree-supports"]}]
    studio = ProbeStudio()
    Pipeline(studio, resolver).plan(boxes_spec(tmp_path, parts), probe_supports=True)
    Pipeline(studio, resolver).plan(boxes_spec(tmp_path, parts), probe_supports=True)
    assert studio.probes == 1
    studio.ver = "02.09.00.10"
    Pipeline(studio, resolver).plan(boxes_spec(tmp_path, parts), probe_supports=True)
    assert studio.probes == 2 and len(list((tmp_path / "out" / ".probe").glob("*.json"))) == 2


def test_support_probe_slices_at_the_bed_centre_in_its_own_directory(tmp_path, resolver):
    """Studio clips tree support at the bed border (TreeSupport.cpp:626-629): a probe at the edge would under-measure.
    Each probe slices in <outdir>/.probe/<cache key>/ with its own log, never in the export directory."""
    from shambu import mesh as M
    from test_mesh import box

    a = M.write_stl(tmp_path / "stl" / "a.stl", box((0, 0, 0), (20, 20, 10)))
    b = M.write_stl(tmp_path / "stl" / "b.stl", box((0, 0, 0), (40, 20, 10)))
    parts = [
        {"path": str(a), "name": "a", "recipes": ["tree-supports"], "at": [2, 2]},
        {"path": str(b), "name": "b", "recipes": ["tree-supports"]},
    ]
    studio = ProbeStudio(sizes={"b.stl": (40.0, 20.0, 10.0)})
    Pipeline(studio, resolver).plan(boxes_spec(tmp_path, parts), probe_supports=True)
    assert studio.positions == [[118.0], [108.0]]  # 128 - w / 2: the centre, whatever the spec's `at`
    logs = sorted((tmp_path / "out" / ".probe").glob("*/*.log"))
    assert len(logs) == 2 and logs[0].parent != logs[1].parent
    assert not list((tmp_path / "out").glob("probe*.log"))


def test_support_probe_keeps_the_mask_in_the_footprint_frame(tmp_path, resolver):
    """The fixture's part lands at x 118..138 (a 20 mm box at the centre); its support runs from x 80, so the mask
    starts 38 mm left of the footprint frame, and it survives the cache."""
    from shambu import mesh as M
    from test_mesh import box

    a = M.write_stl(tmp_path / "stl" / "a.stl", box((0, 0, 0), (20, 20, 10)))
    parts = [{"path": str(a), "name": "a", "recipes": ["tree-supports"]}]
    for _ in range(2):
        plan = Pipeline(ProbeStudio(), resolver).plan(boxes_spec(tmp_path, parts), probe_supports=True)
        mask = plan.plates[0].parts[0].placed.support_mask
        assert mask is not None and mask.bbox()[0] == pytest.approx(80.0 - 118.0)


# ---------------------------------------------------------------------------- 15. second review (Codex and rubric, 2026-10-21)
def test_probe_ignores_an_unretraction_at_the_travel_destination():
    """In the fixture, the outer wall ends, the head travels to the support start (80, 105) and unretracts there with
    `G1 E.8` before the `; FEATURE: Support` comment. Counted as extrusion, that point joined the outer wall's box and
    the -x spread read 0 instead of 20 (on the saved cradle probe: 9.1 instead of 14.9 mm)."""
    from shambu.probe import parse_support

    got = parse_support(PROBE_GCODE.splitlines())
    assert got.model_box == pytest.approx((100.0, 100.0, 120.0, 120.0))
    assert got.sides == pytest.approx((20.0, 5.0, 0.0, 0.0), abs=0.05)


def test_probe_counts_a_full_circle_arc():
    from shambu.probe import parse_support

    lines = ["; CHANGE_LAYER", "G1 X0 Y0", "; FEATURE: Outer wall", "G1 X10 Y0 E1", "; FEATURE: Support", "G1 X20 Y0", "G2 X20 Y0 I5 J0 E1"]
    assert parse_support(lines).sides == pytest.approx((0.0, 20.0, 5.0, 5.0), abs=0.1)  # the circle reaches x 30, y -5..5


def test_keep_going_never_exports_a_part_pushed_off_by_a_fixed_part(tmp_path, stls, resolver):
    """A fixed 252 x 252 part fills plate 1. The free part goes to plate 2; plate 1 keeps its index although it holds
    no free part, so the free part is unplaced and --keep-going does not export the overlap."""
    studio = FakeStudio(sizes={"body.stl": (252.0, 252.0, 10.0), "base.stl": (20.0, 20.0, 10.0)})
    parts = [{"path": str(stls["body"]), "name": "big", "at": [2, 2]}, {"path": str(stls["base"]), "name": "small"}]
    r = Pipeline(studio, resolver).build(boxes_spec(tmp_path, parts), keep_going=True)
    assert r.plan.unplaced == ["small"]
    assert any("plate 1: the fixed parts only; plate 2: small" in c.text for c in r.plan.plates[0].checks)
    assert [k for k, _ in studio.calls if k == "run"] == []


def test_probe_cache_key_covers_every_resolved_preset(tmp_path):
    from shambu.probe import ProbeRequest, StudioProbe

    stl = tmp_path / "a.stl"
    stl.write_bytes(b"solid a\nendsolid a\n")
    base = dict(
        studio=FakeStudio(),
        cache=tmp_path,
        machine={"name": "M", "nozzle_diameter": ["0.4"]},
        process={"wall_loops": "2", "xy_contour_compensation": "0"},
        filaments=[{"name": "F", "filament_shrink": ["100%"]}],
    )
    req = ProbeRequest("a", stl, mesh(stl), 1)
    key = StudioProbe(**base).cache_path(req)
    assert StudioProbe(**{**base, "filaments": [{"name": "F", "filament_shrink": ["99.5%"]}]}).cache_path(req) != key
    assert StudioProbe(**{**base, "machine": {"name": "M", "nozzle_diameter": ["0.6"]}}).cache_path(req) != key
    assert StudioProbe(**{**base, "process": {"wall_loops": "2", "xy_contour_compensation": "0.1"}}).cache_path(req) != key
    assert StudioProbe(**base).cache_path(req) == key


def test_a_raft_keeps_its_bed_margin_on_every_source(tmp_path, resolver):
    """Studio does not clip raft paths with the tree code (TreeSupport.cpp:1591-1617): a raft under tree support
    keeps its spread at the bed edge whether the spread is estimated, given in the spec, or probed."""
    from shambu import mesh as M
    from test_mesh import box

    raft = {"enable_support": "1", "support_type": "tree(auto)", "raft_layers": "2"}
    assert not L.clipped_at_bed(raft) and L.clipped_at_bed({"enable_support": "1"})
    est = L.support_spread(raft, 100.0)
    assert est.source == "estimate" and not est.tree
    a = M.write_stl(tmp_path / "stl" / "a.stl", box((0, 0, 0), (20, 20, 10)))
    spec_part = {"path": str(a), "name": "s", "set": raft, "support_spread": [3, 3, 3, 3]}
    plan = Pipeline(FakeStudio(), resolver).plan(boxes_spec(tmp_path, [spec_part]))
    sp = plan.plates[0].parts[0].placed
    assert sp.spread.source == "spec" and not sp.spread.tree and sp.bed_margin(with_brim=False) == (3.0, 3.0, 3.0, 3.0)
    probed = {"path": str(a), "name": "p", "set": raft}
    plan = Pipeline(ProbeStudio(), resolver).plan(boxes_spec(tmp_path, [probed]), probe_supports=True)
    pp = plan.plates[0].parts[0].placed
    assert pp.spread.source == "probe" and not pp.spread.tree and pp.bed_margin(with_brim=False)[0] == pytest.approx(20.0, abs=0.05)


def test_conflict_names_with_spaces_and_shared_cli_names(tmp_path, stls, resolver):
    from shambu.studio import conflicts_in_log

    known = {"camera mount_1", "base_1", "a and b_1"}
    assert conflicts_in_log("gcode path conflicts found between camera mount_1 and base_1\n", known) == [("camera mount_1", "base_1")]
    assert conflicts_in_log("gcode path conflicts found between a and b_1 and base_1", known) == [("a and b_1", "base_1")]
    assert conflicts_in_log("gcode path conflicts found between x and y and z", set()) == [("x and y and z", None)]
    d1, d2 = tmp_path / "d1", tmp_path / "d2"
    d1.mkdir()
    d2.mkdir()
    (d1 / "part.stl").write_bytes(b"solid x\nendsolid x\n")
    (d2 / "part.stl").write_bytes(b"solid x\nendsolid x\n")
    parts = [
        {"path": str(d1 / "part.stl"), "name": "left", "at": [10, 10]},
        {"path": str(d2 / "part.stl"), "name": "right", "at": [60, 10]},
        {"path": str(stls["base"]), "name": "ell", "at": [120, 10]},
    ]
    studio = LogStudio(code=-101, log_text="[error] gcode path conflicts found between part_1 and base_1\n")
    r = Pipeline(studio, resolver).build(boxes_spec(tmp_path, parts), do_slice=True)
    (line,) = r.export_fails()
    assert "between part_1 (one of left, right: they share that CLI name, so the log cannot tell which) and ell" in line


def test_unreadable_slice_log_is_reported_as_such(tmp_path, stls, resolver):
    @dataclass
    class NoLog(FakeStudio):
        def run(self, cmd, **kw):
            rr = super().run(cmd, **kw)
            rr.log.unlink()
            return rr

    r = Pipeline(NoLog(code=-101), resolver).build(boxes_spec(tmp_path, [{"path": str(stls["body"])}]), do_slice=True)
    (line,) = r.export_fails()
    assert "its log" in line and "could not be read" in line and "the conflicting objects are unknown" in line


def test_unplaced_part_report_has_no_fit_summary(tmp_path, stls, resolver):
    studio = FakeStudio(sizes={"body.stl": (50.0, 300.0, 64.0)})
    r = Pipeline(studio, resolver).build(boxes_spec(tmp_path, [{"path": str(stls["body"]), "name": "feeler"}]), export=False)
    lines = [ln for ln in r.report.splitlines() if ln.startswith("- ")]
    assert any(ln.startswith("- PLACEMENT NOT DONE:") for ln in lines)
    assert not any("part(s) fit" in ln for ln in lines)
    j = r.to_json()["plates"][0]
    assert j["check_kinds"] == ["placement"] and j["check_levels"] == ["fail"]


def test_unreadable_stl_falls_back_to_the_box_with_a_warning(tmp_path, resolver):
    bad = tmp_path / "stl" / "bad.stl"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"\x00" * 200)  # neither a binary nor an ASCII STL
    plan = Pipeline(FakeStudio(), resolver).plan(boxes_spec(tmp_path, [{"path": str(bad), "name": "bad"}]))
    assert plan.plates[0].parts[0].placed.footprint is None
    assert any("(bad): the layout uses the bounding box, not the outline: bad.stl cannot be read" in w for w in plan.warnings)


@pytest.mark.parametrize(
    ("mode", "gcode", "why"),
    [
        ("fail", PROBE_GCODE, "did not slice: exit -100"),
        ("nogcode", PROBE_GCODE, "wrote no G-code"),
        ("badzip", PROBE_GCODE, "cannot read"),
        ("ok", "; CHANGE_LAYER\n; FEATURE: Support\nG1 X0 Y0\nG1 X10 Y0 E1\n", "holds no model extrusion"),
    ],
)
def test_probe_failures_keep_the_estimate_with_a_warning(tmp_path, resolver, mode, gcode, why):
    from shambu import mesh as M
    from test_mesh import box

    a = M.write_stl(tmp_path / "stl" / "a.stl", box((0, 0, 0), (20, 20, 10)))
    studio = ProbeStudio(probe_mode=mode, gcode=gcode)
    plan = Pipeline(studio, resolver).plan(
        boxes_spec(tmp_path, [{"path": str(a), "name": "a", "recipes": ["tree-supports"]}]), probe_supports=True
    )
    assert plan.plates[0].parts[0].placed.spread.source == "estimate"
    assert any("(a): support probe failed" in w and why in w for w in plan.warnings), plan.warnings


def test_unreadable_probe_cache_slices_again(tmp_path, resolver, caplog):
    from shambu import mesh as M
    from test_mesh import box

    a = M.write_stl(tmp_path / "stl" / "a.stl", box((0, 0, 0), (20, 20, 10)))
    parts = [{"path": str(a), "name": "a", "recipes": ["tree-supports"]}]
    studio = ProbeStudio()
    Pipeline(studio, resolver).plan(boxes_spec(tmp_path, parts), probe_supports=True)
    (entry,) = (tmp_path / "out" / ".probe").glob("*.json")
    entry.write_text("{not json", encoding="utf-8")
    plan = Pipeline(studio, resolver).plan(boxes_spec(tmp_path, parts), probe_supports=True)
    assert studio.probes == 2 and plan.plates[0].parts[0].placed.spread.source == "probe"
    assert "unreadable probe cache" in caplog.text


def test_probe_io_error_becomes_a_warning_not_a_traceback(tmp_path, resolver):
    from shambu import mesh as M
    from test_mesh import box

    a = M.write_stl(tmp_path / "stl" / "a.stl", box((0, 0, 0), (20, 20, 10)))
    out = tmp_path / "out"
    out.mkdir()
    (out / ".probe").write_text("a file where the cache directory should be", encoding="utf-8")
    plan = Pipeline(ProbeStudio(), resolver).plan(
        boxes_spec(tmp_path, [{"path": str(a), "name": "a", "recipes": ["tree-supports"]}]), probe_supports=True
    )
    assert any("(a): support probe failed (the support probe of a:" in w for w in plan.warnings)


# ---------------------------------------------------------------------------- the measuring fallback (estimates are advisory)
class _Measured:
    """A SpreadProbe fake: 1 mm on every side, or a failure for the parts named in `fail`."""

    def __init__(self, fail=()):
        self.fail, self.seen = set(fail), []

    def measure(self, req):
        from shambu.errors import StudioError
        from shambu.probe import ProbeResult

        self.seen.append(req.key)
        if req.key in self.fail:
            raise StudioError("no G-code")
        return ProbeResult(L.Spread(1.0, 1.0, 1.0, 1.0, "probe", True))


def _four_tall(tmp_path):
    """Four 115 x 115 x 200 parts with tree supports: the 26 mm estimate puts one per plate, 1 mm fits all four."""
    names = ["a", "b", "c", "d"]
    studio = FakeStudio(sizes={f"{n}.stl": (115.0, 115.0, 200.0) for n in names})
    parts = [{"path": str(stl_named(tmp_path, n)), "name": n, "recipes": ["tree-supports"]} for n in names]
    return studio, boxes_spec(tmp_path, parts)


def test_estimates_that_split_the_plate_are_measured_before_the_answer(tmp_path, resolver):
    studio, sp = _four_tall(tmp_path)
    probe = _Measured()
    plan = Pipeline(studio, resolver, probe=probe).plan(sp)
    assert sorted(probe.seen) == ["a", "b", "c", "d"] and plan.ok, plan.problems
    p = plan.plates[0]
    assert p.packed_plates == 1 and all(x.placed.spread.source == "probe" for x in p.parts)
    (line,) = [c.text for c in p.checks if c.kind == "measured"]
    assert line.startswith("Spreads measured: the estimated support spread needed 4 plates; a solo slice per supported part measured")
    assert "a: 1.0/1.0/1.0/1.0" in line and line.endswith("placement uses the measurements")
    from shambu.report import check_line

    assert check_line(next(c for c in p.checks if c.kind == "measured")).startswith("Spreads measured: ")


def test_no_probe_keeps_the_estimates_answer(tmp_path, resolver):
    studio, sp = _four_tall(tmp_path)
    probe = _Measured()
    plan = Pipeline(studio, resolver, probe=probe).plan(sp, no_probe=True)
    assert probe.seen == [] and plan.plates[0].packed_plates == 4 and not plan.ok
    assert any("the packer put the parts on 4 plates (a heuristic" in x for x in plan.problems)


def test_a_failing_probe_keeps_that_parts_estimate_and_says_so(tmp_path, resolver):
    studio, sp = _four_tall(tmp_path)
    plan = Pipeline(studio, resolver, probe=_Measured(fail={"d"})).plan(sp)
    p = plan.plates[0]
    (line,) = [c.text for c in p.checks if c.kind == "measured"]
    assert "d: estimate kept (the probe failed)" in line
    assert next(x for x in p.parts if x.key == "d").placed.spread.source == "estimate"
    assert any("(d): support probe failed (no G-code); the estimate applies" in w for w in plan.warnings)
