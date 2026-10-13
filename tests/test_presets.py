"""presets.py: resolution of inherits + include, CLI shaping, compatibility, machine facts."""

import json

import pytest

from conftest import LONG_END, LONG_START, MACHINE, PROCESS
from shambu import presets as P
from shambu.errors import PresetError


def test_inherits_merges_parent_first_child_overrides(resolver):
    d = resolver.resolve("process", PROCESS)
    assert d["wall_loops"] == "2"  # from the parent
    assert d["layer_height"] == "0.2"  # from the child
    assert "inherits" not in d


def test_include_template_replaces_generic_start_gcode(resolver):
    d = resolver.resolve("machine", MACHINE)
    assert d["machine_start_gcode"] == LONG_START
    assert d["machine_end_gcode"] == LONG_END
    assert "include" not in d


def test_include_does_not_copy_template_identity(resolver):
    d = resolver.resolve("machine", MACHINE)
    assert d["name"] == MACHINE
    assert d["instantiation"] == "true"


def test_inherits_loop_raises(tmp_path):
    for a, b in (("a", "b"), ("b", "a")):
        (tmp_path / "process").mkdir(exist_ok=True)
        (tmp_path / "process" / f"{a}.json").write_text(json.dumps({"name": a, "inherits": b}))
    r = P.PresetResolver(P.ChainedSource([P.DirectorySource(tmp_path)]))
    with pytest.raises(PresetError, match="loop"):
        r.resolve("process", "a")


def test_missing_preset_and_bad_kind_raise(resolver):
    with pytest.raises(PresetError):
        resolver.resolve("process", "nope")
    with pytest.raises(PresetError):
        resolver.load_raw("printer", MACHINE)


def test_for_cli_without_override_is_system(resolver):
    d = resolver.for_cli("process", PROCESS)
    assert (d["from"], d["name"], d["type"], d["print_settings_id"]) == ("system", PROCESS, "process", PROCESS)
    assert "instantiation" not in d


def test_for_cli_override_becomes_user_preset(resolver):
    d = resolver.for_cli("process", PROCESS, {"wall_loops": 6})
    assert d["from"] == "User"
    assert d["inherits"] == PROCESS
    assert d["name"] == f"{PROCESS} (modified)"
    assert d["wall_loops"] == "6"  # non-strings become strings


def test_for_cli_filament_id_is_a_list(resolver):
    d = resolver.for_cli("filament", "Test PLA", new_name="My PLA")
    assert d["filament_settings_id"] == ["My PLA"]
    assert d["from"] == "User"


def test_machine_setting_id_dropped(resolver):
    assert "setting_id" not in resolver.for_cli("machine", MACHINE)


def test_chained_source_first_hit_wins(tmp_path, profiles):
    over = tmp_path / "user"
    (over / "process").mkdir(parents=True)
    (over / "process" / f"{PROCESS}.json").write_text(json.dumps({"name": PROCESS, "inherits": "fdm_process_common", "wall_loops": "9"}))
    chain = P.ChainedSource([P.DirectorySource(over, "user"), P.DirectorySource(profiles, "system")])
    assert P.PresetResolver(chain).resolve("process", PROCESS)["wall_loops"] == "9"
    assert {p.stem for p in chain.names("process")} >= {PROCESS, "0.20mm Wrong"}


def test_list_filters_instantiable_and_printer(resolver):
    names = [r["name"] for r in resolver.list("filament", printer=MACHINE)]
    assert names == ["Test PLA", "Test TPU"]  # PETG lists another printer; common is not instantiable


def test_check_compatible_passes_for_matching_set(resolver):
    m = resolver.for_cli("machine", MACHINE)
    pr = resolver.for_cli("process", PROCESS, {"wall_loops": "4"})
    fs = [resolver.for_cli("filament", "Test PLA")]
    assert P.check_compatible(m, pr, fs) == []


def test_check_compatible_uses_inherits_for_user_machine(resolver):
    m = resolver.for_cli("machine", MACHINE, new_name="My X2D")
    pr = resolver.for_cli("process", PROCESS)
    assert P.check_compatible(m, pr, []) == []


def test_check_compatible_flags_process_and_filament(resolver):
    m = resolver.for_cli("machine", MACHINE)
    pr = resolver.for_cli("process", "0.20mm Wrong")
    fs = [resolver.for_cli("filament", "Test PETG")]
    probs = P.check_compatible(m, pr, fs)
    assert any("process '0.20mm Wrong'" in p for p in probs)
    assert any("filament 'Test PETG'" in p for p in probs)


def test_check_compatible_flags_unresolved_includes(resolver):
    m = resolver.for_cli("machine", MACHINE)
    del m["machine_end_gcode"]
    probs = P.check_compatible(m, resolver.for_cli("process", PROCESS), [])
    assert any("machine_end_gcode" in p for p in probs)


def test_check_compatible_flags_missing_required_key(resolver):
    m = resolver.for_cli("machine", MACHINE)
    pr = resolver.for_cli("process", PROCESS)
    pr["from"] = ""
    assert any("lacks required key from" in p for p in P.check_compatible(m, pr, []))


def test_machine_facts_extruder_reach(resolver):
    f = P.machine_facts(resolver.for_cli("machine", MACHINE))
    assert f["bed"] == {"x": (0.0, 256.0), "y": (0.0, 256.0)}
    assert f["z_max"] == 325.0
    e1, e2 = f["extruders"]
    assert (e1["index"], e1["x"], e1["z_max"], e1["type"]) == (1, (0.0, 256.0), 320.0, "Direct Drive")
    assert (e2["index"], e2["x"], e2["z_max"], e2["type"]) == (2, (20.5, 256.0), 325.0, "Bowden")


def test_machine_facts_without_extruder_areas_uses_bed():
    f = P.machine_facts({"name": "m", "printable_area": ["0x0", "200x200"], "printable_height": "180", "nozzle_diameter": ["0.4"]})
    assert f["extruders"][0]["x"] == (0.0, 200.0)
    assert f["extruders"][0]["z_max"] == 180.0


def test_write_round_trips(tmp_path, resolver):
    d = resolver.for_cli("process", PROCESS)
    p = P.write(d, tmp_path / "a" / "p.json")
    assert json.loads(p.read_text()) == d
