"""build.Pipeline end to end with FakeStudio: plan -> inputs -> export -> patch -> verify -> report."""

import json

import pytest

from conftest import MACHINE, PROCESS, PROCESS_NAME, FakeStudio, make_3mf, write_cli_tower
from shambu.build import Pipeline
from shambu.spec import Spec
from shambu.studio import RunResult
from shambu.threemf import Project

CHANGE_FILAMENT = {"change_filament_gcode": "M600"}  # the resolved fixture machine preset carries this


@pytest.fixture
def cli_export(tmp_path):
    """What the CLI would write for the spec below: CLI object names, not the spec keys."""
    return make_3mf(tmp_path / "cli-export.3mf", object_names=("body_1", "assemble_1"), **CHANGE_FILAMENT)


def make_spec(tmp_path, stls, **over) -> Spec:
    d = {
        "name": "plate-t",
        "outdir": str(tmp_path / "out"),
        "printer": MACHINE,
        "process": {"base": PROCESS, "name": PROCESS_NAME, "set": {"wall_loops": "2"}},
        "filaments": [{"preset": "Test PLA", "colour": "#000000"}, {"preset": "Test PLA", "colour": "#FFFFFF"}],
        "bed_type": "Textured PEI Plate",
        "plates": [
            {
                "name": "p",
                "parts": [
                    {"path": str(stls["body"]), "name": "body"},
                    {"name": "label", "parts": [{"path": str(stls["base"])}, {"path": str(stls["text"]), "filament": 2}]},
                ],
                "pause_at_z": [4.2],
            }
        ],
    }
    d.update(over)
    return Spec.from_json(d)


def pipeline(resolver, studio, recorder=None, **kw):
    return Pipeline(studio, resolver, listeners=[recorder] if recorder else [], **kw)


# ---------------------------------------------------------------------------- happy path
def test_end_to_end_ok(tmp_path, stls, resolver, cli_export, recorder):
    r = pipeline(resolver, FakeStudio(cli_export), recorder).build(make_spec(tmp_path, stls))
    assert r.plan.problems == []
    assert r.verify_problems == []
    assert r.ok


def test_end_to_end_events_in_order(tmp_path, stls, resolver, cli_export, recorder):
    pipeline(resolver, FakeStudio(cli_export), recorder).build(make_spec(tmp_path, stls))
    assert recorder.names() == ["plan", "planned", "export", "exported", "patched", "verified", "finished"]


def test_end_to_end_patches_land_in_file(tmp_path, stls, resolver, cli_export):
    r = pipeline(resolver, FakeStudio(cli_export)).build(make_spec(tmp_path, stls))
    s = Project(str(r.project)).summary()
    assert s["bed_type"] == "Textured PEI Plate"
    assert [f["colour"] for f in s["filaments"]] == ["#000000", "#FFFFFF"]
    assert s["filament_map"] == ["1", "2"]
    assert sorted(o["name"] for o in s["objects"]) == ["body", "label"]
    assert [(c["type"], c["top_z"]) for c in s["custom_gcode"]] == [("pause", 4.2)]


def test_end_to_end_writes_inputs_and_report(tmp_path, stls, resolver, cli_export):
    r = pipeline(resolver, FakeStudio(cli_export)).build(make_spec(tmp_path, stls))
    for k in ("machine", "process", "filament1", "filament2", "assemble", "spec"):
        assert r.inputs[k].is_file(), k
    machine = json.loads(r.inputs["machine"].read_text())
    assert len(machine["machine_start_gcode"]) > 1000  # include template merged
    assert r.report_path.read_text().startswith("# plate-t")
    assert "written and verified" in r.report
    json.dumps(r.to_json())  # serialisable


def test_assemble_list_positions_and_merge(tmp_path, stls, resolver, cli_export):
    r = pipeline(resolver, FakeStudio(cli_export)).build(make_spec(tmp_path, stls), export=False)
    objs = json.loads(r.inputs["assemble"].read_text())["plates"][0]["objects"]
    assert [o.get("assemble_index") for o in objs] == [None, [1], [1]]
    assert all(len(o["pos_x"]) == 1 for o in objs)


def test_label_with_filament_2_subpart_is_pushed_into_nozzle_2_reach(tmp_path, stls, resolver, cli_export):
    plan = pipeline(resolver, FakeStudio(cli_export)).plan(make_spec(tmp_path, stls))
    label = next(p for p in plan.plates[0].parts if p.key == "label")
    assert label.placed.x0 - label.placed.brim >= 20.5


def test_command_has_loads_and_export(tmp_path, stls, resolver, cli_export):
    studio = FakeStudio(cli_export)
    pipeline(resolver, studio).build(make_spec(tmp_path, stls))
    args = next(a for kind, a in studio.calls if kind == "run")
    assert "--load-assemble-list" in args and "--load-settings" in args and "--export-3mf" in args
    assert args[args.index("--export-3mf") + 1] == "plate-t.3mf"
    assert "--allow-rotations=0" in args


def test_verify_flags_wrong_process_name(tmp_path, stls, resolver):
    wrong = make_3mf(tmp_path / "wrong.3mf", object_names=("body_1", "assemble_1"), print_settings_id="Other", **CHANGE_FILAMENT)
    r = pipeline(resolver, FakeStudio(wrong)).build(make_spec(tmp_path, stls))
    assert any("process in file is Other" in p for p in r.verify_problems)
    assert not r.ok
    assert "verify problems below" in r.report


def test_template_method_export_override(tmp_path, stls, resolver, cli_export):
    class NoStudio(Pipeline):
        def export(self, plan, paths, project, timeout, do_slice=False):
            project.write_bytes(cli_export.read_bytes())
            write_cli_tower(project, self.command(plan, paths, plan.spec.name, do_slice).args())  # what the CLI writes
            return RunResult(["fake"], 0, 0, 0.0, project.with_suffix(".log"), None, project)

    r = NoStudio(FakeStudio(), resolver).build(make_spec(tmp_path, stls))
    assert r.ok


# ---------------------------------------------------------------------------- failure paths
def test_failing_cli_stops_before_patch(tmp_path, stls, resolver, recorder):
    r = pipeline(resolver, FakeStudio(code=-17), recorder).build(make_spec(tmp_path, stls))
    assert r.run.code == -17
    assert not r.ok
    assert r.patches == [] and r.verify_problems is None
    assert "patched" not in recorder.names()
    assert "export FAILED" in r.report
    assert "compatible_printers" in r.report


def test_plan_not_ok_skips_export(tmp_path, stls, resolver):
    studio = FakeStudio()
    r = pipeline(resolver, studio).build(make_spec(tmp_path, stls, process={"base": "0.20mm Wrong"}))
    assert not r.plan.ok
    assert any("compatible_printers" in p for p in r.plan.problems)
    assert r.run is None
    assert [k for k, _ in studio.calls if k == "run"] == []
    assert "not exported" in r.report and "## Problems" in r.report


def test_failed_checks_report_says_nothing_was_exported(tmp_path, stls, resolver):
    """Two parts at one position overlap: the plan fails, the build stops before the export, and the report sends the
    user back to the spec, not to a project that may not exist or may be stale."""
    parts = [{"path": str(stls["body"]), "name": "a", "at": [20, 20]}, {"path": str(stls["base"]), "name": "b", "at": [20, 20]}]
    studio = FakeStudio()
    r = pipeline(resolver, studio).build(make_spec(tmp_path, stls, plates=[{"name": "p", "parts": parts}]), export=True)
    assert not r.plan.ok and r.run is None
    assert [k for k, _ in studio.calls if k == "run"] == []
    assert "FAIL body overlap: a (x 20.0..40.0, y 20.0..40.0) and b (x 20.0..40.0, y 20.0..40.0) intersect by 20.0 x 20.0 mm" in r.report
    assert "## Next" in r.report and "nothing was exported" in r.report and "Fix the spec and build again" in r.report
    assert "Owner steps" not in r.report and "Open the project" not in r.report


def test_check_only_report_says_nothing_was_exported(tmp_path, stls, resolver, cli_export):
    """`shambu check` (export=False) with a plan that passes: the Next section says nothing was exported and to run
    `shambu build`; no owner step tells the user to open a project this run never wrote."""
    r = pipeline(resolver, FakeStudio(cli_export)).build(make_spec(tmp_path, stls), export=False)
    assert r.plan.ok and r.run is None
    assert "## Next" in r.report and "nothing was exported" in r.report and "`shambu build`" in r.report
    assert "Owner steps" not in r.report and "Open the project" not in r.report


def test_keep_going_exports_anyway(tmp_path, stls, resolver, cli_export):
    r = pipeline(resolver, FakeStudio(cli_export)).build(make_spec(tmp_path, stls, process={"base": "0.20mm Wrong"}), keep_going=True)
    assert r.run is not None and not r.ok


def test_missing_stl_is_a_problem(tmp_path, stls, resolver):
    stls["body"].unlink()
    plan = pipeline(resolver, FakeStudio()).plan(make_spec(tmp_path, stls))
    assert any("does not exist" in p for p in plan.problems)


def test_overfull_plate_is_a_problem(tmp_path, stls, resolver):
    studio = FakeStudio(sizes={"body.stl": (200.0, 200.0, 10.0)})
    spec = make_spec(tmp_path, stls)
    spec = Spec.from_json({**spec.to_json(), "plates": [{"name": "p", "parts": [{"path": str(stls["body"]), "count": 2}]}]})
    plan = pipeline(resolver, studio).plan(spec)
    assert any("the packer put the parts on 2 plates" in p for p in plan.problems)


def test_bad_filament_map_is_a_problem(tmp_path, stls, resolver):
    plan = pipeline(resolver, FakeStudio()).plan(make_spec(tmp_path, stls, filament_map=[1, 3]))
    assert any("filament_map" in p for p in plan.problems)


TPU_FILAMENTS = [{"preset": "Test PLA", "colour": "#000000"}, {"preset": "Test TPU", "colour": "#FFFFFF"}]


def test_tpu_on_aux_nozzle_fails_check(tmp_path, stls, resolver):
    spec = make_spec(tmp_path, stls, filaments=TPU_FILAMENTS, plates=[{"name": "p", "parts": [{"path": str(stls["body"]), "filament": 2}]}])
    plan = pipeline(resolver, FakeStudio()).plan(spec)
    assert any("TPU" in c.text for p in plan.plates for c in p.checks if not c.ok)


def test_tpu_subpart_on_aux_nozzle_fails_check(tmp_path, stls, resolver):
    plan = pipeline(resolver, FakeStudio()).plan(make_spec(tmp_path, stls, filaments=TPU_FILAMENTS))
    assert any("TPU" in c.text for p in plan.plates for c in p.checks if not c.ok)


# ---------------------------------------------------------------------------- batch 1 (real-STL test findings)
def one_part_spec(tmp_path, path, **part):
    return Spec.from_json(
        {
            "name": "one",
            "outdir": str(tmp_path / "out"),
            "printer": MACHINE,
            "process": {"base": PROCESS, "name": PROCESS_NAME},
            "filaments": [{"preset": "Test PLA"}],
            "plates": [{"name": "p", "parts": [{"path": str(path), **part}]}],
        }
    )


def test_overflow_gives_one_fail_line(tmp_path, stls, resolver):
    studio = FakeStudio(sizes={"body.stl": (200.0, 200.0, 10.0)})
    plan = pipeline(resolver, studio).plan(one_part_spec(tmp_path, stls["body"], count=3))
    fails = [c.text for c in plan.plates[0].checks if not c.ok]
    assert len(fails) == 1 and "put the parts on 3 plates" in fails[0]  # no repeated overlap and reach failures
    assert sum("put the parts on 3 plates" in p for p in plan.problems) == 1


def test_orientation_without_flat_contact_is_a_problem(tmp_path, resolver):
    from shambu import mesh as M
    from test_mesh import box

    stl = M.write_stl(tmp_path / "block.stl", box((0, 0, 0), (30, 20, 10)))
    plan = pipeline(resolver, FakeStudio()).plan(one_part_spec(tmp_path, stl, lay_on_face=[1, 1, 0]))  # stands on an edge
    assert any("leaves no flat face on the bed" in p and "block" in p for p in plan.problems)
    plan = pipeline(resolver, FakeStudio()).plan(one_part_spec(tmp_path, stl, lay_on_face=[1, 0, 0]))
    assert not any("no flat face" in p for p in plan.problems)


def test_to_json_after_a_failed_export(tmp_path, stls, resolver):
    studio = FakeStudio(code=-101, result={"error_string": "G-code path conflicts", "warning_message": "floating cantilever"})
    r = pipeline(resolver, studio).build(make_spec(tmp_path, stls), do_slice=True)
    j = r.to_json()
    assert j["ok"] is False and j["project"] is None and j["sliced"] is False
    assert any(
        p.startswith("export failed: exit -101 (G-code path conflicts)") and "Studio: G-code path conflicts" in p for p in j["problems"]
    )
    assert "floating cantilever" in j["warnings"]
    assert "## Next" in r.report and "nothing to open" in r.report and "slicer warning: floating cantilever" in r.report
    json.dumps(j)


def test_failed_export_without_result_json_keeps_the_old_shape(tmp_path, stls, resolver):
    r = pipeline(resolver, FakeStudio(code=-17)).build(make_spec(tmp_path, stls))
    assert r.run.result is None and r.slicer_warnings() == []
    assert r.export_problem().startswith("export failed: exit -17") and "Studio:" not in r.export_problem()


def test_supported_part_gets_the_support_spread(tmp_path, stls, resolver):
    studio = FakeStudio(sizes={"body.stl": (20.0, 20.0, 40.0)})
    plan = pipeline(resolver, studio).plan(one_part_spec(tmp_path, stls["body"], recipes=["tree-supports"]))
    p = plan.plates[0].parts[0].placed
    assert p.brim == pytest.approx(5.2)  # the brim, kept apart from the support spread
    assert p.spread.sides() == pytest.approx((8.0,) * 4) and p.spread.source == "estimate" and p.spread.tree  # 0.10 x 40 < 8 floor
    plan = pipeline(resolver, studio).plan(one_part_spec(tmp_path, stls["body"]))
    p = plan.plates[0].parts[0].placed
    assert p.brim == pytest.approx(5.2) and p.spread.source == "none"  # no supports: the brim only
