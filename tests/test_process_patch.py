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


def ps_of(path) -> dict:
    return Project(str(path)).project_settings


def test_process_set_writes_the_value_lists_it_and_drops_the_slice(capsys, tmp_path):
    f = make_3mf(tmp_path / "f.gcode.3mf", sliced=True, print_settings_id=PROCESS)
    code, out, _ = run(capsys, "--json", "patch", str(f), "--process-set", "wall_loops=4", "--process-set", "layer_height=0.2")
    assert code == 0, out
    ps = ps_of(f)
    # the base resolves: layer_height 0.2 equals it, so only wall_loops is listed
    assert ps["wall_loops"] == "4" and ps["different_settings_to_system"] == ["wall_loops", "", "", ""]
    assert Project(str(f)).is_sliced() is False
    assert any("wall_loops = 4" in d for d in json.loads(out)["patched"])


def test_process_set_without_a_resolvable_base_appends_the_key(capsys, tmp_path):
    f = make_3mf(tmp_path / "f.3mf", different_settings_to_system=["brim_type", "", "", ""])  # "Test Process": not installed
    code, _, _ = run(capsys, "patch", str(f), "--process-set", "wall_loops=4")
    assert code == 0
    assert ps_of(f)["different_settings_to_system"][0] == "brim_type;wall_loops"


def test_process_unset_restores_the_base_value(capsys, tmp_path):
    f = make_3mf(tmp_path / "f.3mf", print_settings_id=PROCESS, wall_loops="4", different_settings_to_system=["wall_loops", "", "", ""])
    code, out, _ = run(capsys, "patch", str(f), "--process-unset", "wall_loops")
    assert code == 0, out
    ps = ps_of(f)
    assert ps["wall_loops"] == "2" and ps["different_settings_to_system"][0] == ""


def test_process_unset_uses_the_inherits_group_parent(capsys, tmp_path):
    f = make_3mf(
        tmp_path / "f.3mf",
        print_settings_id="My saved process",
        inherits_group=[PROCESS, "", "", ""],
        wall_loops="4",
        different_settings_to_system=["wall_loops", "", "", ""],
    )
    code, _, _ = run(capsys, "patch", str(f), "--process-unset", "wall_loops")
    assert code == 0 and ps_of(f)["wall_loops"] == "2"


def test_process_unset_refuses_without_the_base_preset(capsys, tmp_path):
    f = make_3mf(tmp_path / "f.3mf", wall_loops="4")
    raw = f.read_bytes()
    code, out, _ = run(capsys, "patch", str(f), "--process-unset", "wall_loops")
    assert code == 1
    assert "process preset Test Process is not in the Studio library; cannot restore wall_loops" in out
    assert f.read_bytes() == raw


def test_process_unset_refuses_a_key_the_base_does_not_define(capsys, tmp_path):
    f = make_3mf(tmp_path / "f.3mf", print_settings_id=PROCESS)
    code, out, _ = run(capsys, "patch", str(f), "--process-unset", "machine_pause_gcode")
    assert code == 1 and f"process preset {PROCESS} does not define machine_pause_gcode" in out


@pytest.mark.parametrize("key", ["no_such_key", "print_settings_id"])
def test_process_set_refuses_an_unknown_or_bookkeeping_key(capsys, tmp_path, key):
    f = make_3mf(tmp_path / "f.3mf")
    raw = f.read_bytes()
    code, out, _ = run(capsys, "patch", str(f), "--process-set", f"{key}=1")
    assert code == 1 and "ProjectError" in out and f.read_bytes() == raw


def test_process_set_bad_format_is_usage(capsys, tmp_path):
    code, out, _ = run(capsys, "patch", str(make_3mf(tmp_path / "f.3mf")), "--process-set", "wall_loops")
    assert code == 2 and "bad patch option" in out


def test_inspect_shows_the_override_lists(capsys, tmp_path):
    f = make_3mf(
        tmp_path / "f.3mf", print_settings_id=PROCESS, different_settings_to_system=["brim_type;wall_loops", "", "nozzle_temperature", ""]
    )
    code, out, _ = run(capsys, "inspect", str(f))
    assert code == 0
    assert f"process {PROCESS} (+2 overrides: brim_type;wall_loops);" in out
    lines = out.splitlines()
    assert next(x for x in lines if x.startswith("filament 1:")).startswith("filament 1: Test PLA (PLA)")
    assert next(x for x in lines if x.startswith("filament 2:")).startswith("filament 2: Test PLA (+1 overrides) (PLA)")


def test_inspect_without_a_list_keeps_the_plain_process(capsys, tmp_path):
    code, out, _ = run(capsys, "inspect", str(make_3mf(tmp_path / "f.3mf")))
    assert code == 0 and "process Test Process; bed" in out


def test_slice_settings_from_writes_the_process_and_printer_bookkeeping(capsys, tmp_path, monkeypatch, resolver):
    from test_edits import SequenceStudio

    src = make_3mf(tmp_path / "in.3mf")
    gcode = make_3mf(tmp_path / "studio.gcode.3mf", sliced=True, print_settings_id="Sweeper", inherits_group=[PROCESS, "", "", ""])
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
