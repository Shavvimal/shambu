"""`shambu verify FILE --against SPEC`: the GUI round-trip verifier (spec 2026-10-29 section 5.6). Offline: the spec's
presets come from the synthetic library, the project is the geometry fixture, no STL exists and no Studio runs."""

import json

import pytest

from conftest import GEO_BODY_AT, GEO_LABEL_AT, MACHINE, PROCESS, FakeStudio, make_3mf, make_geo_3mf
from shambu import cli
from shambu.facade import Bambu

LIST = "skeleton_infill_density;skin_infill_density;sparse_infill_density;wall_loops"
GOOD = {
    "print_settings_id": PROCESS,
    "inherits_group": ["", "", "", ""],
    "different_settings_to_system": [LIST, "", "", ""],
    "wall_loops": "4",
    "sparse_infill_density": "25%",
    "skeleton_infill_density": "25%",
    "skin_infill_density": "25%",
    "change_filament_gcode": "M600",
    "filament_colour": ["#00AE42", "#FFFFFF"],
}


@pytest.fixture(autouse=True)
def fake_app(monkeypatch, resolver):
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=FakeStudio(), resolver=resolver))


def spec_file(tmp_path, *, body=None, label=None, filaments=None, **over):
    d = {
        "name": "v",
        "outdir": str(tmp_path / "out"),
        "printer": MACHINE,
        "process": {"base": PROCESS, "name": "Sweeper", "set": {"wall_loops": "4", "sparse_infill_density": "25%"}},
        "filaments": filaments or [{"preset": "Test PLA", "colour": "#00AE42"}, {"preset": "Test PLA", "colour": "#FFFFFF"}],
        "bed_type": "Cool Plate",
        "plates": [
            {
                "name": "p",
                "parts": [
                    {"path": "/nowhere/body.stl", "at": list(GEO_BODY_AT), "set": {"brim_type": "no_brim"}, **(body or {})},
                    {
                        "path": "/nowhere/label.stl",
                        "filament": 2,
                        "at": list(GEO_LABEL_AT),
                        "set": {"brim_type": "no_brim"},
                        **(label or {}),
                    },
                ],
            }
        ],
    }
    d.update(over)
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    return p


def project(tmp_path, **over):
    return make_geo_3mf(tmp_path / "geo.3mf", **{**GOOD, **over})


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def verify(capsys, f, spec):
    code, out, _ = run(capsys, "verify", str(f), "--against", str(spec))
    return code, out.strip().splitlines()


def test_matching_file_exits_0(capsys, tmp_path):
    code, lines = verify(capsys, project(tmp_path), spec_file(tmp_path))
    assert code == 0, lines
    assert not [x for x in lines if x.startswith(("FAIL", "WARN"))]
    assert lines[-1].startswith("verify: the file matches the spec")
    assert any(x.startswith("INFO process enable_prime_tower") and "tower key from planning" in x for x in lines)


def test_stock_process_without_a_list_names_every_reset_key(capsys, tmp_path):
    """What the GUI saved on 29 October: the base id, no list, the base values."""
    f = project(
        tmp_path,
        different_settings_to_system=["", "", "", ""],
        wall_loops="2",
        sparse_infill_density="15%",
        skeleton_infill_density="15%",
        skin_infill_density="15%",
    )
    code, lines = verify(capsys, f, spec_file(tmp_path))
    assert code == 1
    fails = [x for x in lines if x.startswith("FAIL")]
    for k, want, got in (("wall_loops", "4", "2"), ("sparse_infill_density", "25%", "15%"), ("skin_infill_density", "25%", "15%")):
        assert any(
            x.startswith(f"FAIL process {k}: expected {want}, got {got}") and "Studio reset it to the base on open" in x for x in fails
        )
    assert len(fails) == 4  # one line per key, not one for the value and one for the list
    assert lines[-1] == "verify: 4 differences, 0 GUI edits"


def test_user_preset_id_is_a_fail(capsys, tmp_path):
    code, lines = verify(capsys, project(tmp_path, print_settings_id="Sweeper"), spec_file(tmp_path))
    assert code == 1
    assert "FAIL process preset id: expected 0.20mm Test, got Sweeper (Studio selects the preset by this name)" in lines


def test_right_value_missing_from_the_list_fails_because_the_next_open_resets_it(capsys, tmp_path):
    f = project(tmp_path, different_settings_to_system=["skeleton_infill_density;skin_infill_density;sparse_infill_density", "", "", ""])
    code, lines = verify(capsys, f, spec_file(tmp_path))
    assert code == 1
    assert any(x.startswith("FAIL process list wall_loops: expected listed, got not listed") for x in lines)


def test_key_equal_to_the_base_dropped_on_save_is_info(capsys, tmp_path):
    spec = spec_file(tmp_path, process={"base": PROCESS, "set": {"wall_loops": "4", "sparse_infill_density": "25%", "brim_width": "5"}})
    code, lines = verify(capsys, project(tmp_path, brim_width="5"), spec)
    assert code == 0, lines
    assert any(x.startswith("INFO process list brim_width") and "dropped by Studio on save" in x for x in lines)


def test_gui_edit_is_a_warn_with_its_value(capsys, tmp_path):
    f = project(tmp_path, different_settings_to_system=[LIST + ";top_shell_layers", "", "", ""], top_shell_layers="7")
    code, lines = verify(capsys, f, spec_file(tmp_path))
    assert code == 0
    assert "WARN process top_shell_layers: got 7 (GUI edit: listed in the file, not set by the spec)" in lines
    assert lines[-1] == "verify: 0 differences, 1 GUI edits"


def test_inherits_group_naming_the_base_is_a_warn(capsys, tmp_path):
    code, lines = verify(capsys, project(tmp_path, inherits_group=[PROCESS, "", "", ""]), spec_file(tmp_path))
    assert code == 0 and any(x.startswith("WARN process inherits_group[0]") for x in lines)


def test_wrong_object_setting_is_a_fail(capsys, tmp_path):
    spec = spec_file(tmp_path, body={"set": {"brim_type": "outer_only"}})
    code, lines = verify(capsys, project(tmp_path), spec)
    assert code == 1
    assert "FAIL object body brim_type: expected outer_only, got no_brim" in lines


def test_object_key_the_spec_does_not_set_is_info(capsys, tmp_path):
    spec = spec_file(tmp_path, body={"set": {}})
    code, lines = verify(capsys, project(tmp_path), spec)
    assert code == 0
    assert "INFO object body settings: got brim_type=no_brim (added in the GUI or by the auto-pin)" in lines


def test_position_off_by_1_mm_is_a_fail(capsys, tmp_path):
    spec = spec_file(tmp_path, label={"at": [GEO_LABEL_AT[0] + 1, GEO_LABEL_AT[1]]})
    code, lines = verify(capsys, project(tmp_path), spec)
    assert code == 1
    assert any(x.startswith("FAIL object label position: expected plate 1 x 151.00 y 50.00, got plate 1 x 150.00 y 50.00") for x in lines)


def test_position_within_the_tolerance_passes(capsys, tmp_path):
    spec = spec_file(tmp_path, label={"at": [GEO_LABEL_AT[0] + 0.04, GEO_LABEL_AT[1]]})
    assert verify(capsys, project(tmp_path), spec)[0] == 0


def test_missing_object_is_a_fail_and_an_extra_one_info(capsys, tmp_path):
    spec = spec_file(tmp_path, label={"name": "tag"})
    code, lines = verify(capsys, project(tmp_path), spec)
    assert code == 1
    assert "FAIL object tag: expected present, got not found" in lines
    assert any(x.startswith("INFO object label: got in the file") for x in lines)


def test_secondary_filament_as_a_project_embedded_copy_is_accepted(capsys, tmp_path):
    """Two slots on one base with different overrides: after a GUI save slot 2 is `<base>(<file>.3mf)` with
    inherits_group[2] = base (PresetBundle.cpp:3931; Preset.cpp:2401-2439); its values are still compared."""
    fil = [
        {"preset": "Test PLA", "colour": "#00AE42", "set": {"filament_flow_ratio": "0.95"}},
        {"preset": "Test PLA", "colour": "#FFFFFF", "set": {"filament_flow_ratio": "0.98"}},
    ]
    over = {
        "filament_settings_id": ["Test PLA", "Test PLA(geo.3mf)"],
        "inherits_group": ["", "", "Test PLA", ""],
        "different_settings_to_system": [LIST, "filament_flow_ratio", "", ""],
        "filament_flow_ratio": ["0.95", "0.98"],
    }
    code, lines = verify(capsys, project(tmp_path, **over), spec_file(tmp_path, filaments=fil))
    assert code == 0, lines
    assert any(x.startswith("INFO filament 2 preset id") and "project-embedded copy of Test PLA" in x for x in lines)
    code, lines = verify(capsys, project(tmp_path, **{**over, "filament_flow_ratio": ["0.95", "0.95"]}), spec_file(tmp_path, filaments=fil))
    assert code == 1 and "FAIL filament 2 filament_flow_ratio: expected 0.98, got 0.95" in lines


def test_slot_1_must_carry_the_base_name(capsys, tmp_path):
    over = {"filament_settings_id": ["Test PLA(geo.3mf)", "Test PLA"], "inherits_group": ["", "Test PLA", "", ""]}
    code, lines = verify(capsys, project(tmp_path, **over), spec_file(tmp_path))
    assert code == 1 and any(x.startswith("FAIL filament 1 preset id: expected Test PLA, got Test PLA(geo.3mf)") for x in lines)


def test_loaded_filament_is_not_checked(capsys, tmp_path):
    fil = [{"preset": "loaded"}, {"preset": "Test PLA", "colour": "#FFFFFF"}]
    code, lines = verify(capsys, project(tmp_path), spec_file(tmp_path, filaments=fil))
    assert code == 0
    assert "INFO filament 1 preset id: got Test PLA (not checked (loaded from the printer))" in lines


def test_colour_map_and_plate_mode(capsys, tmp_path):
    f = project(tmp_path, filament_colour=["#00AE42", "#000000"], filament_map=["1", "1"], filament_map_mode="Auto For Flush")
    code, lines = verify(capsys, f, spec_file(tmp_path))
    assert code == 1
    assert "FAIL filament 2 colour: expected #FFFFFF, got #000000" in lines
    assert "FAIL filament_map: expected 1,2, got 1,1" in lines
    assert any(x.startswith("WARN filament_map_mode: expected Manual, got Auto For Flush") for x in lines)  # the plate's Manual wins


def test_filament_setting_and_machine_gcode(capsys, tmp_path):
    fil = [{"preset": "Test PLA", "colour": "#00AE42", "set": {"filament_flow_ratio": "0.95"}}, {"preset": "Test PLA", "colour": "#FFFFFF"}]
    f = project(tmp_path, change_filament_gcode="M600 ; edited", different_settings_to_system=[LIST, "filament_flow_ratio", "", ""])
    code, lines = verify(capsys, f, spec_file(tmp_path, filaments=fil))
    assert code == 1
    assert "FAIL filament 1 filament_flow_ratio: expected 0.95, got None" not in lines  # a missing key says it is missing
    assert any(x.startswith("FAIL filament 1 filament_flow_ratio: expected 0.95") for x in lines)
    assert any(x.startswith("FAIL printer change_filament_gcode: expected 4 chars, got 13 chars") for x in lines)


def test_bed_type_and_print_sequence(capsys, tmp_path):
    spec = spec_file(tmp_path, bed_type="Textured PEI Plate")
    spec_d = json.loads(spec.read_text())
    spec_d["plates"][0]["print_sequence"] = "by object"
    spec.write_text(json.dumps(spec_d))
    code, lines = verify(capsys, project(tmp_path), spec)
    assert code == 1
    assert "FAIL bed type: expected Textured PEI Plate, got Cool Plate" in lines
    assert "FAIL plate 1 print sequence: expected by object, got by layer" in lines


def test_json_is_one_document(capsys, tmp_path):
    f = project(tmp_path, different_settings_to_system=[LIST + ";top_shell_layers", "", "", ""], top_shell_layers="7")
    code, out, _ = run(capsys, "--json", "verify", str(f), "--against", str(spec_file(tmp_path)))
    doc = json.loads(out)
    assert code == 0
    assert set(doc) >= {"file", "spec", "ok", "lines"} and doc["ok"] is True and doc["file"] == str(f)
    assert all(set(x) >= {"level", "item", "expected", "got"} for x in doc["lines"])
    assert {"level": "WARN", "item": "process top_shell_layers", "expected": None, "got": "7"}.items() <= next(
        x for x in doc["lines"] if x["item"] == "process top_shell_layers"
    ).items()


def test_file_or_spec_that_does_not_load_is_exit_3(capsys, tmp_path):
    bad = tmp_path / "bad.3mf"
    bad.write_bytes(b"not a zip")
    code, out, _ = run(capsys, "--json", "verify", str(bad), "--against", str(spec_file(tmp_path)))
    assert code == 3 and "error" in json.loads(out)
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    code, _, err = run(capsys, "verify", str(make_3mf(tmp_path / "p.3mf")), "--against", str(broken))
    assert code == 3 and "BuildError" in err
    no_key = tmp_path / "nokey.json"
    no_key.write_text(json.dumps({"name": "x"}), encoding="utf-8")
    assert run(capsys, "verify", str(make_3mf(tmp_path / "p.3mf")), "--against", str(no_key))[0] == 3


def test_missing_against_is_usage(capsys, tmp_path):
    with pytest.raises(SystemExit) as e:
        cli.main(["verify", str(make_3mf(tmp_path / "p.3mf"))])
    assert e.value.code == 2
