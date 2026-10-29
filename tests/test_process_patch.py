"""`shambu patch --process-set/--process-unset`, the override lists in `inspect`, and the bookkeeping of
`slice --settings-from` (spec 2026-10-29 sections 5.1 and 5.4)."""

import json

import pytest

from conftest import MACHINE, PROCESS, FakeStudio, make_3mf
from shambu import cli
from shambu.facade import Bambu
from shambu.threemf import Project


@pytest.fixture(autouse=True)
def fake_app(monkeypatch, resolver):
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=FakeStudio(), resolver=resolver))


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def two(path, **over):
    """A two-filament fixture with one colour per filament (Studio counts filament_colour, PresetBundle.cpp:3750-3755)."""
    return make_3mf(path, **{"filament_colour": ["#00AE42", "#FFFFFF"], **over})


def ps_of(path) -> dict:
    return Project(str(path)).project_settings


def test_process_set_writes_the_value_lists_it_and_drops_the_slice(capsys, tmp_path):
    f = two(tmp_path / "f.gcode.3mf", sliced=True, print_settings_id=PROCESS)
    code, out, _ = run(capsys, "--json", "patch", str(f), "--process-set", "wall_loops=4", "--process-set", "layer_height=0.2")
    assert code == 0, out
    ps = ps_of(f)
    # a key set is always listed, even when it equals shambu's base (Studio drops an equal key on save)
    assert ps["wall_loops"] == "4" and ps["different_settings_to_system"] == ["layer_height;wall_loops", "", "", ""]
    assert Project(str(f)).is_sliced() is False
    assert any("wall_loops = 4" in d for d in json.loads(out)["patched"])


def test_process_set_without_a_resolvable_base_appends_the_key(capsys, tmp_path):
    f = two(tmp_path / "f.3mf", different_settings_to_system=["brim_type", "", "", ""])  # "Test Process": not installed
    code, _, _ = run(capsys, "patch", str(f), "--process-set", "wall_loops=4")
    assert code == 0
    assert ps_of(f)["different_settings_to_system"][0] == "brim_type;wall_loops"


def test_process_unset_restores_the_base_value(capsys, tmp_path):
    f = two(tmp_path / "f.3mf", print_settings_id=PROCESS, wall_loops="4", different_settings_to_system=["wall_loops", "", "", ""])
    code, out, _ = run(capsys, "patch", str(f), "--process-unset", "wall_loops")
    assert code == 0, out
    ps = ps_of(f)
    assert ps["wall_loops"] == "2" and ps["different_settings_to_system"][0] == ""


def test_process_unset_uses_the_inherits_group_parent(capsys, tmp_path):
    f = two(
        tmp_path / "f.3mf",
        print_settings_id="My saved process",
        inherits_group=[PROCESS, "", "", ""],
        wall_loops="4",
        different_settings_to_system=["wall_loops", "", "", ""],
    )
    code, _, _ = run(capsys, "patch", str(f), "--process-unset", "wall_loops")
    assert code == 0 and ps_of(f)["wall_loops"] == "2"


def test_process_unset_refuses_without_the_base_preset(capsys, tmp_path):
    f = two(tmp_path / "f.3mf", wall_loops="4")
    raw = f.read_bytes()
    code, out, _ = run(capsys, "patch", str(f), "--process-unset", "wall_loops")
    assert code == 1
    assert "process preset Test Process is not in the Studio library; cannot restore wall_loops" in out
    assert f.read_bytes() == raw


def test_process_unset_refuses_a_key_the_base_does_not_define(capsys, tmp_path):
    f = two(tmp_path / "f.3mf", print_settings_id=PROCESS)
    code, out, _ = run(capsys, "patch", str(f), "--process-unset", "machine_pause_gcode")
    assert code == 1 and f"process preset {PROCESS} does not define machine_pause_gcode" in out


@pytest.mark.parametrize("key", ["no_such_key", "print_settings_id"])
def test_process_set_refuses_an_unknown_or_bookkeeping_key(capsys, tmp_path, key):
    f = two(tmp_path / "f.3mf")
    raw = f.read_bytes()
    code, out, _ = run(capsys, "patch", str(f), "--process-set", f"{key}=1")
    assert code == 1 and "ProjectError" in out and f.read_bytes() == raw


def test_process_set_bad_format_is_usage(capsys, tmp_path):
    code, out, _ = run(capsys, "patch", str(two(tmp_path / "f.3mf")), "--process-set", "wall_loops")
    assert code == 2 and "bad patch option" in out


def test_inspect_shows_the_override_lists(capsys, tmp_path):
    f = two(
        tmp_path / "f.3mf", print_settings_id=PROCESS, different_settings_to_system=["brim_type;wall_loops", "", "nozzle_temperature", ""]
    )
    code, out, _ = run(capsys, "inspect", str(f))
    assert code == 0
    assert f"process {PROCESS} (+2 overrides: brim_type;wall_loops);" in out
    lines = out.splitlines()
    assert next(x for x in lines if x.startswith("filament 1:")).startswith("filament 1: Test PLA (PLA)")
    assert next(x for x in lines if x.startswith("filament 2:")).startswith("filament 2: Test PLA (+1 overrides) (PLA)")


def test_inspect_without_a_list_keeps_the_plain_process(capsys, tmp_path):
    code, out, _ = run(capsys, "inspect", str(two(tmp_path / "f.3mf")))
    assert code == 0 and "process Test Process; bed" in out


def test_slice_settings_from_writes_the_process_and_printer_bookkeeping(capsys, tmp_path, monkeypatch, resolver):
    from test_edits import SequenceStudio

    src = two(tmp_path / "in.3mf")
    gcode = two(tmp_path / "studio.gcode.3mf", sliced=True, print_settings_id="Sweeper", inherits_group=[PROCESS, "", "", ""])
    studio = SequenceStudio(gcode, codes=[0])
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=studio, resolver=resolver))
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "name": "s",
                "outdir": str(tmp_path),
                "printer": MACHINE,
                "process": {"base": PROCESS, "name": "Sweeper", "set": {"wall_loops": "4"}},
                "filaments": [{"preset": "Test PLA"}],
                "plates": [{"parts": []}],
            }
        ),
        encoding="utf-8",
    )
    dest = tmp_path / "o.gcode.3mf"
    code, out, _ = run(capsys, "--json", "slice", str(src), "--settings-from", str(spec), "-o", str(dest))
    doc = json.loads(out)
    assert code == 0, doc
    ps = ps_of(dest)
    assert ps["print_settings_id"] == PROCESS and ps["printer_settings_id"] == MACHINE
    assert ps["inherits_group"] == ["", "", "", ""] and ps["different_settings_to_system"][0] == "wall_loops"
    assert Project(str(dest)).is_sliced()
    assert f"preset bookkeeping: process {PROCESS} + 1 overrides" in doc["notes"]


def _user_process(profiles, name="My 4 walls"):
    (profiles / "process" / f"{name}.json").write_text(
        json.dumps({"name": name, "from": "User", "inherits": PROCESS, "wall_loops": "4"}), encoding="utf-8"
    )


def test_process_set_measures_against_the_inherits_parent_of_an_installed_user_preset(capsys, tmp_path, profiles):
    _user_process(profiles)  # installed: 4 walls over the 2-wall system preset
    f = two(tmp_path / "f.3mf", print_settings_id="My 4 walls", inherits_group=[PROCESS, "", "", ""], wall_loops="4", brim_type="auto_brim")
    code, out, _ = run(capsys, "patch", str(f), "--process-set", "brim_type=no_brim")
    assert code == 0, out
    # Studio resets to the inherits parent (Preset.cpp:2258-2264): wall_loops 4 differs from it, so it is listed
    assert ps_of(f)["different_settings_to_system"][0] == "brim_type;wall_loops"


def test_process_set_on_a_project_only_name_appends_the_key(capsys, tmp_path):
    f = two(tmp_path / "f.3mf", print_settings_id="Sweeper", wall_loops="4", brim_type="auto_brim")
    code, _, _ = run(capsys, "patch", str(f), "--process-set", "brim_type=no_brim")
    assert code == 0 and ps_of(f)["different_settings_to_system"][0] == "brim_type"


@pytest.mark.parametrize("key", ["filament_type=PETG", "change_filament_gcode=M601"])
def test_process_set_refuses_a_filament_or_printer_key(capsys, tmp_path, key):
    f = two(tmp_path / "f.3mf", print_settings_id=PROCESS, change_filament_gcode="M600")
    raw = f.read_bytes()
    code, out, _ = run(capsys, "patch", str(f), "--process-set", key)
    assert code == 1 and f"{key.split('=')[0]} is a filament/printer setting; `--process-set` takes process keys" in out
    assert f.read_bytes() == raw


def test_process_set_accepts_a_key_no_preset_defines(capsys, tmp_path):
    f = two(tmp_path / "f.3mf", print_settings_id=PROCESS, post_process=[""])
    code, out, _ = run(capsys, "patch", str(f), "--process-set", "post_process=a.py --x=1,2")
    assert code == 0, out
    assert ps_of(f)["post_process"] == ["a.py --x=1,2"]  # one string entry: the vector held no number


def test_process_set_accepts_with_a_warning_when_no_preset_resolves(capsys, tmp_path, caplog):
    f = two(tmp_path / "f.3mf", printer_settings_id="Gone printer", filament_settings_id=["Gone 1", "Gone 2"])
    code, _, _ = run(capsys, "patch", str(f), "--process-set", "filament_type=PETG")
    assert code == 0 and "cannot tell a process key from them" in caplog.text


def _slice_spec(tmp_path, process_set):
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "name": "s",
                "outdir": str(tmp_path),
                "printer": MACHINE,
                "process": {"base": PROCESS, "name": "Sweeper", "set": process_set},
                "filaments": [{"preset": "Test PLA"}],
                "plates": [{"parts": []}],
            }
        ),
        encoding="utf-8",
    )
    return spec


def _slice(capsys, tmp_path, monkeypatch, resolver, src, process_set):
    from test_edits import SequenceStudio

    studio = SequenceStudio(two(tmp_path / "studio.gcode.3mf", sliced=True), codes=[0])
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=studio, resolver=resolver))
    dest = tmp_path / "o.gcode.3mf"
    code, out, _ = run(capsys, "--json", "slice", str(src), "--settings-from", str(_slice_spec(tmp_path, process_set)), "-o", str(dest))
    assert code == 0, out
    return ps_of(dest)


def test_slice_settings_from_lists_the_derived_infill_keys(capsys, tmp_path, monkeypatch, resolver):
    ps = _slice(capsys, tmp_path, monkeypatch, resolver, two(tmp_path / "in.3mf"), {"sparse_infill_density": "25%"})
    assert ps["different_settings_to_system"][0] == "skeleton_infill_density;skin_infill_density;sparse_infill_density"


def test_slice_settings_from_keeps_the_keys_the_input_listed(capsys, tmp_path, monkeypatch, resolver):
    """A GUI-listed key the supplied preset does not define keeps the input's value in the slice (the CLI updates only
    the supplied keys, BambuStudio.cpp:3352-3360), so it stays listed."""
    src = two(tmp_path / "in.3mf", post_process=["x.py"], different_settings_to_system=["post_process", "", "", "z_hop"])
    ps = _slice(capsys, tmp_path, monkeypatch, resolver, src, {"wall_loops": "4"})
    assert ps["different_settings_to_system"][0] == "post_process;wall_loops"
    assert ps["different_settings_to_system"][3] == "z_hop"


def test_slice_settings_from_skips_an_ambiguous_export_with_a_warning(capsys, tmp_path, monkeypatch, resolver):
    from test_edits import SequenceStudio

    studio = SequenceStudio(make_3mf(tmp_path / "studio.gcode.3mf", sliced=True), codes=[0])  # 2 ids, 1 colour
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=studio, resolver=resolver))
    spec = _slice_spec(tmp_path, {"wall_loops": "4"})
    code, out, _ = run(
        capsys, "--json", "slice", str(two(tmp_path / "in.3mf")), "--settings-from", str(spec), "-o", str(tmp_path / "o.gcode.3mf")
    )
    assert code == 0 and any(n.startswith("WARN: preset bookkeeping not written") for n in json.loads(out)["notes"])
