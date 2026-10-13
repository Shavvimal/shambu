"""build.Pipeline end to end with FakeStudio: plan -> inputs -> export -> patch -> verify -> report."""

import json

import pytest

from conftest import MACHINE, PROCESS, PROCESS_NAME, FakeStudio, make_3mf
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
    assert label.placed.x0 - label.placed.margin >= 20.5


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
    assert any("need 2 plates" in p for p in plan.problems)


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
