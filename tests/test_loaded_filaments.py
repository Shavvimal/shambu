"""The loaded-filament reader and the spec reconciliation (docs/errors/2026-10-16-loaded-filament-not-used.md).

Offline: the printer report is the redacted live capture from the owner's X2D with two external spools
(tests/fixtures/x2d_pushall_two_external_spools.json), fed through a fake MQTT reader or straight into the decoder."""

from __future__ import annotations

import json

import pytest

from conftest import FIXTURES, MACHINE, PROCESS, PROCESS_NAME, FakeStudio, make_3mf
from shambu import cli, conf, loaded, printer
from shambu.build import Pipeline
from shambu.errors import PrinterError
from shambu.facade import Bambu
from shambu.spec import Spec

SERIAL = "00X0TEST0000999"
SENTINEL = "SENTINEL-CODE-8812"
GENERIC = "Generic PETG @Test X2D"  # filament_id GFG99 in the test profile tree
BASIC = "Test PETG Basic"  # GFG00
CHANGE_FILAMENT = {"change_filament_gcode": "M600"}


def capture() -> dict:
    return json.loads((FIXTURES / "x2d_pushall_two_external_spools.json").read_text())


@pytest.fixture
def report(resolver) -> printer.LoadedFilaments:
    return printer.FilamentDecoder(resolver, MACHINE).decode(capture(), SERIAL, "10.0.0.9")


@pytest.fixture
def with_printer(monkeypatch, report):
    """The facade reads the fixture instead of the LAN (on top of conftest's offline fixture)."""
    monkeypatch.setattr(Bambu, "loaded_filaments", lambda self, *a, **k: report)
    return report


@pytest.fixture
def conf_file(tmp_path, monkeypatch):
    p = tmp_path / "BambuStudio.conf"
    p.write_text(
        json.dumps(
            {
                "app": {"user_last_selected_machine": SERIAL, "curr_bed_type": "4"},
                "access_code": {SERIAL: SENTINEL},
                "presets": {"machine": MACHINE, "filaments": [BASIC, "Test PLA"], "filament_colors": "#00AE42"},
            }
        )
    )
    monkeypatch.setattr(conf, "CONF", p)
    monkeypatch.setattr(conf, "APPSUPPORT", tmp_path)
    return p


def spec_json(tmp_path, stls, filaments) -> dict:
    return {
        "name": "bracket",
        "outdir": str(tmp_path / "out"),
        "printer": MACHINE,
        "process": {"base": PROCESS, "name": PROCESS_NAME, "set": {"wall_loops": "2"}},
        "filaments": filaments,
        "bed_type": "Textured PEI Plate",
        "plates": [{"name": "p", "parts": [{"path": str(stls["body"]), "name": "body"}]}],
    }


def write_spec(tmp_path, stls, filaments):
    p = tmp_path / "plate.json"
    p.write_text(json.dumps(spec_json(tmp_path, stls, filaments)))
    return p


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


# ---------------------------------------------------------------------------- 1. the capture decodes to the per-nozzle table
def test_fixture_decodes_two_external_spools(report):
    """Section 6 of the error doc, settled on the real printer: `vir_slot` ids 254 (left) and 255 (right), no `vt_tray`."""
    assert [t.key for t in report.trays] == [(254, 0), (255, 0)]
    left, right = report.nozzle(1), report.nozzle(2)
    assert (left.extruder, left.name, right.extruder, right.name) == (1, "main (left)", 0, "auxiliary (right)")
    assert left.tray is not None and left.tray.label == "left external spool"
    assert (left.tray.material, left.tray.colour, left.tray.preset_id, left.tray.preset) == ("PETG", "#FFFFFF", "GFG99", GENERIC)
    assert right.tray is not None and right.tray.label == "right external spool"
    assert (right.tray.material, right.tray.colour, right.tray.preset_id, right.tray.preset) == ("PETG", "#2850E0", "GFG00", BASIC)
    assert left.tray.describe() == "white Generic PETG (GFG99)"
    assert right.tray.describe() == "blue Test PETG Basic (GFG00)"
    assert report.read_at.count(":") == 1 and report.tls_verified is True


def test_snow_names_the_loaded_tray_not_the_fixed_table():
    """The extruder's `snow` decides which spool a nozzle holds; EXTERNAL_TRAY_EXTRUDER only says which side a spool sits on."""
    d = capture()
    ext = d["print"]["device"]["extruder"]["info"]
    assert [(e["id"], e["snow"]) for e in ext] == [(0, 0xFF00), (1, 0xFE00)]
    assert printer.decode_slot(0xFE00) == (254, 0) and printer.decode_slot(0xFFFF) is None and printer.decode_slot("x") is None
    ext[1]["snow"] = 0xFFFF  # the left nozzle unloaded: its spool is fitted, not loaded
    lf = printer.FilamentDecoder().decode(d, SERIAL, "ip")
    assert lf.nozzle(1).tray is None and [t.label for t in lf.nozzle(1).fitted] == ["left external spool"]
    assert lf.nozzle(2).tray.label == "right external spool"


def test_lines_and_json_carry_no_serial(report):
    text = "\n".join(report.lines())
    assert "nozzle 1 (main (left)): left external spool: PETG, #FFFFFF (white), preset id GFG99 -> Generic PETG @Test X2D" in text
    assert "nozzle 2 (auxiliary (right)): right external spool: PETG, #2850E0 (blue), preset id GFG00 -> Test PETG Basic" in text
    j = json.dumps(report.to_json())
    assert SERIAL not in j and SERIAL not in text and "00X…999" in text


def test_single_nozzle_fallback_uses_vt_tray_and_tray_now(resolver):
    d = {
        "print": {
            "gcode_state": "IDLE",
            "vt_tray": {"id": "254", "tray_type": "PLA", "tray_color": "000000FF", "tray_info_idx": "GFA00"},
            "ams": {
                "tray_now": "1",
                "ams": [
                    {
                        "id": "0",
                        "tray": [
                            {"id": "0", "tray_type": ""},
                            {"id": "1", "tray_type": "PETG", "tray_color": "2850E0FF", "tray_info_idx": ""},
                        ],
                    }
                ],
            },
        }
    }
    lf = printer.FilamentDecoder(resolver, MACHINE).decode(d, SERIAL, "ip")
    assert [t.label for t in lf.trays] == ["external spool", "AMS 1 slot 1", "AMS 1 slot 2"]
    assert lf.trays[1].empty and lf.trays[1].describe() == "empty"
    (noz,) = lf.nozzles
    assert noz.nozzle == 1 and noz.tray.label == "AMS 1 slot 2"
    assert noz.tray.preset == GENERIC  # no tray_info_idx: the Generic preset of the material
    assert lf.trays[0].preset == "Test PLA"  # GFA00


def test_unknown_preset_id_without_material_resolves_to_nothing(resolver):
    assert resolver.find_by_filament_id("GFX42", MACHINE) is None
    assert resolver.find_by_filament_id("", MACHINE) is None
    assert resolver.generic_for_material("NYLON", MACHINE) is None
    assert resolver.generic_for_material("petg", MACHINE) == GENERIC


@pytest.mark.parametrize(("hexv", "name"), [("#FFFFFF", "white"), ("#2850E0", "blue"), ("#00AE42", "green"), (None, "unknown colour")])
def test_colour_names(hexv, name):
    assert printer.colour_name(hexv) == name


def test_hex_colour():
    assert printer.hex_colour("2850E0FF") == "#2850E0"
    assert printer.hex_colour("#ffffff") == "#FFFFFF"
    assert printer.hex_colour("00000000") is None and printer.hex_colour("zz") is None and printer.hex_colour(None) is None


# ---------------------------------------------------------------------------- 2. the incident: a pinned spool that is no longer loaded
def test_pinned_preset_that_differs_from_the_spool_fails_check(capsys, tmp_path, stls, resolver, with_printer, monkeypatch):
    """13 October spec (Bambu PETG Basic, green) built on 16 October with Generic PETG white on the left: exit 1, one line naming both."""
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=FakeStudio(), resolver=resolver))
    p = write_spec(tmp_path, stls, [{"preset": BASIC, "colour": "#00AE42"}])
    code, out, _ = run(capsys, "--json", "check", str(p))
    j = json.loads(out)
    assert code == 1 and j["plan_ok"] is False
    (problem,) = j["problems"]
    assert problem.startswith(
        f"filament 1: the spec pins '{BASIC}' but nozzle 1 (main (left)) holds '{GENERIC}' (left external spool: white Generic PETG (GFG99), read "
    )
    assert '"preset": "loaded"' in problem and "--allow-filament-mismatch" in problem
    assert j["filaments"]["sources"][0]["source"] == "spec" and "MISMATCH" in j["filaments"]["sources"][0]["text"]
    code, out, _ = run(capsys, "check", str(p))
    assert code == 1 and out.splitlines()[4].startswith("Filaments: from the printer at ")  # title, blank, Project, Printer, then this


def test_loaded_takes_the_spool_preset_and_colour_through_build_and_inspect(capsys, tmp_path, stls, resolver, with_printer, monkeypatch):
    """`"preset": "loaded"`: the built project carries Generic PETG and #FFFFFF, and `inspect` shows them."""
    fixture = make_3mf(
        tmp_path / "cli.3mf", object_names=("body_1", "text_1"), filament_settings_id=[GENERIC], filament_type=["PETG"], **CHANGE_FILAMENT
    )
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=FakeStudio(fixture), resolver=resolver))
    d = spec_json(tmp_path, stls, [{"preset": "loaded"}])
    d["plates"][0]["parts"].append({"path": str(stls["text"]), "name": "text"})  # the fixture 3MF holds two objects
    p = tmp_path / "plate.json"
    p.write_text(json.dumps(d))
    code, out, _ = run(capsys, "--json", "build", str(p))
    j = json.loads(out)
    assert code == 0 and j["ok"] is True and j["problems"] == []
    (src,) = j["filaments"]["sources"]
    assert (src["preset"], src["colour"], src["source"]) == (GENERIC, "#FFFFFF", "printer")
    assert src["text"].startswith("from the printer (left external spool, white Generic PETG (GFG99), read ")
    assert j["filaments"]["headline"].startswith(f"Filaments: from the printer at {with_printer.read_at}: 1 = {GENERIC} #FFFFFF (white)")
    report = (tmp_path / "out" / "bracket.report.md").read_text()
    assert f"Filament 1: `{GENERIC}` (PETG), #FFFFFF, extruder 1 (Direct Drive). Source: from the printer (left external spool" in report
    written = json.loads((tmp_path / "out" / "shambu" / "spec.json").read_text())
    assert written["filaments"][0]["preset"] == GENERIC and written["filaments"][0]["colour"] == "#FFFFFF"
    fil1 = json.loads((tmp_path / "out" / "shambu" / "filament-1.json").read_text())  # what --load-filaments hands Studio
    assert (fil1["name"], fil1["filament_settings_id"], fil1["filament_colour"]) == (GENERIC, [GENERIC], ["#FFFFFF"])
    code, out, _ = run(capsys, "inspect", str(tmp_path / "out" / "bracket.3mf"))
    assert code == 0 and f"filament 1: {GENERIC} (PETG) #FFFFFF" in out


def test_omitted_preset_means_loaded(tmp_path, stls):
    s = Spec.from_json(spec_json(tmp_path, stls, [{}, {"preset": "", "colour": "#000000"}]))
    assert all(f.loaded for f in s.filaments) and s.filaments[0].preset == "loaded"


def test_allow_mismatch_keeps_the_pinned_preset_and_records_it(tmp_path, stls, resolver, with_printer):
    app = Bambu(studio=FakeStudio(), resolver=resolver)
    r = app.build(
        Spec.from_json(spec_json(tmp_path, stls, [{"preset": BASIC, "colour": "#00AE42"}])), export=False, allow_filament_mismatch=True
    )
    assert r.plan.ok
    assert r.plan.filaments[0]["name"] == BASIC
    (src,) = r.plan.filament_resolution.sources
    assert src.source == "spec (override)" and "--allow-filament-mismatch" in src.text
    assert any("kept because --allow-filament-mismatch was passed" in w for w in r.plan.warnings)
    assert "override --allow-filament-mismatch" in r.report


def test_pinned_preset_that_matches_the_spool_takes_its_colour(tmp_path, stls, resolver, with_printer):
    """Build D7c of the incident: the right preset, but the stale green colour. The spool colour wins unless a colour_note says why not."""
    app = Bambu(studio=FakeStudio(), resolver=resolver)
    r = app.build(Spec.from_json(spec_json(tmp_path, stls, [{"preset": GENERIC, "colour": "#00AE42"}])), export=False)
    assert r.plan.ok and r.plan.filaments[0]["filament_colour"] == ["#FFFFFF"]
    assert any("colour #00AE42 in the spec replaced by the spool colour #FFFFFF" in w for w in r.plan.warnings)
    r = app.build(
        Spec.from_json(spec_json(tmp_path, stls, [{"preset": GENERIC, "colour": "#00AE42", "colour_note": "green sample for the photo"}])),
        export=False,
    )
    assert r.plan.ok and r.plan.filaments[0]["filament_colour"] == ["#00AE42"] and r.plan.warnings == []
    assert (
        "colour #00AE42 pinned by the spec (green sample for the photo), the spool is #FFFFFF" in r.plan.filament_resolution.sources[0].text
    )


def test_loaded_with_nothing_in_the_nozzle_is_a_problem(tmp_path, stls, resolver, monkeypatch):
    d = capture()
    d["print"]["device"]["extruder"]["info"][0]["snow"] = 0xFFFF  # the right nozzle (extruder id 0) unloaded
    d["print"]["vir_slot"][1]["tray_type"] = ""  # and no spool on the right side
    d["print"]["vir_slot"][1]["tray_info_idx"] = ""
    lf = printer.FilamentDecoder(resolver, MACHINE).decode(d, SERIAL, "ip")
    monkeypatch.setattr(Bambu, "loaded_filaments", lambda self, *a, **k: lf)
    app = Bambu(studio=FakeStudio(), resolver=resolver)
    r = app.build(Spec.from_json(spec_json(tmp_path, stls, [{"preset": "loaded"}, {"preset": "loaded"}])), export=False)
    assert not r.plan.ok
    assert r.plan.problems[0] == f'filament 2: "loaded", but nothing is loaded in nozzle 2 (auxiliary (right)) (read {lf.read_at})'
    assert r.plan.filaments[0]["name"] == GENERIC and r.plan.filaments[1]["name"] == "loaded (unresolved)"


def test_fitted_but_unloaded_spool_is_named_not_taken(tmp_path, stls, resolver, monkeypatch):
    """Review finding 1: an unfed external spool is a hint for the owner, never a guess at what the nozzle holds."""
    d = capture()
    d["print"]["device"]["extruder"]["info"][1]["snow"] = 0xFFFF  # left nozzle unloaded, white Generic PETG still fitted
    lf = printer.FilamentDecoder(resolver, MACHINE).decode(d, SERIAL, "ip")
    assert lf.nozzle(1).tray is None and not lf.nozzle(1).unlisted
    monkeypatch.setattr(Bambu, "loaded_filaments", lambda self, *a, **k: lf)
    app = Bambu(studio=FakeStudio(), resolver=resolver)
    r = app.build(Spec.from_json(spec_json(tmp_path, stls, [{"preset": "loaded"}])), export=False)
    assert not r.plan.ok
    assert r.plan.problems[0] == (
        f'filament 1: "loaded", but nothing is loaded in nozzle 1 (main (left)) (read {lf.read_at}); '
        "the left external spool holds white Generic PETG (GFG99): load it, or pin its preset"
    )
    r = app.build(Spec.from_json(spec_json(tmp_path, stls, [{"preset": GENERIC}])), export=False)
    assert not r.plan.ok and "cannot be compared with the spool" in r.plan.problems[0]
    r = app.build(Spec.from_json(spec_json(tmp_path, stls, [{"preset": GENERIC}])), export=False, allow_filament_mismatch=True)
    assert r.plan.ok and r.plan.filament_resolution.sources[0].source == "spec (override)"


def test_snow_naming_an_unlisted_slot_is_not_the_external_spool(tmp_path, stls, resolver, monkeypatch):
    """Review finding 1b: the left extruder says it holds AMS 1 slot 2, which the report does not list."""
    d = capture()
    d["print"]["device"]["extruder"]["info"][1]["snow"] = 0x0001
    lf = printer.FilamentDecoder(resolver, MACHINE).decode(d, SERIAL, "ip")
    assert lf.nozzle(1).tray is None and lf.nozzle(1).unlisted and lf.nozzle(1).loaded_slot == (0, 1)
    assert "holds slot (0, 1) which the report does not list" in "\n".join(lf.lines())
    monkeypatch.setattr(Bambu, "loaded_filaments", lambda self, *a, **k: lf)
    r = Bambu(studio=FakeStudio(), resolver=resolver).build(Spec.from_json(spec_json(tmp_path, stls, [{"preset": "loaded"}])), export=False)
    assert not r.plan.ok and "holds slot (0, 1) which the report does not list" in r.plan.problems[0]


def test_nozzle_count_comes_from_the_machine_not_the_report(tmp_path, stls, resolver, monkeypatch):
    """Review finding 2: a report that lists one extruder must not route filament 2 to the left spool."""
    d = capture()
    d["print"]["device"]["extruder"]["info"] = [d["print"]["device"]["extruder"]["info"][1]]  # only the left extruder reported
    lf = printer.FilamentDecoder(resolver, MACHINE).decode(d, SERIAL, "ip")
    assert [z.nozzle for z in lf.nozzles] == [1]
    monkeypatch.setattr(Bambu, "loaded_filaments", lambda self, *a, **k: lf)
    r = Bambu(studio=FakeStudio(), resolver=resolver).build(
        Spec.from_json(spec_json(tmp_path, stls, [{"preset": "loaded"}, {"preset": "loaded"}])), export=False
    )
    assert r.plan.filament_map == [1, 2]
    assert r.plan.filaments[0]["name"] == GENERIC and r.plan.filaments[1]["name"] == "loaded (unresolved)"
    assert r.plan.problems[0] == f'filament 2: "loaded", but the printer did not report nozzle 2 (read {lf.read_at})'


def test_single_extruder_report_with_id_0_is_nozzle_1(resolver):
    """Review finding 4: one extruder entry, whatever its id, is nozzle 1; without `snow` the classic tray_now applies."""
    d = {
        "print": {
            "gcode_state": "IDLE",
            "vt_tray": {"id": "254", "tray_type": "PLA", "tray_color": "000000FF", "tray_info_idx": "GFA00"},
            "ams": {"tray_now": "254", "ams": []},
            "device": {"extruder": {"info": [{"id": 0, "temp": 25}]}},
        }
    }
    lf = printer.FilamentDecoder(resolver, MACHINE).decode(d, SERIAL, "ip")
    (noz,) = lf.nozzles
    assert (noz.nozzle, noz.extruder, noz.tray.preset) == (1, 0, "Test PLA")
    d["print"]["device"]["extruder"]["info"][0]["snow"] = 0xFFFF  # `snow` present wins over tray_now
    assert printer.FilamentDecoder(resolver, MACHINE).decode(d, SERIAL, "ip").nozzles[0].tray is None


def test_override_still_applies_the_colour_rule(tmp_path, stls, resolver, with_printer):
    """Review finding 6: --allow-filament-mismatch waives the preset check, not the stale colour."""
    app = Bambu(studio=FakeStudio(), resolver=resolver)
    r = app.build(
        Spec.from_json(spec_json(tmp_path, stls, [{"preset": BASIC, "colour": "#00AE42"}])), export=False, allow_filament_mismatch=True
    )
    assert r.plan.ok and r.plan.filaments[0]["filament_colour"] == ["#FFFFFF"]
    assert any("colour #00AE42 in the spec replaced by the spool colour #FFFFFF" in w for w in r.plan.warnings)


def test_bad_colour_is_a_spec_problem_not_a_traceback(capsys, tmp_path, stls, resolver, with_printer, monkeypatch):
    """Review finding 7."""
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=FakeStudio(), resolver=resolver))
    p = write_spec(tmp_path, stls, [{"preset": GENERIC, "colour": "#GGGGGG", "colour_note": "kept on purpose"}])  # the note keeps it
    code, out, err = run(capsys, "--json", "check", str(p))
    assert code == 1 and err == ""
    j = json.loads(out)
    assert "filament 1: colour '#GGGGGG' is not #RRGGBB" in j["problems"]
    assert "#GGGGGG (unknown colour)" in j["filaments"]["headline"]
    assert printer.colour_name("#GGGGGG") == "unknown colour"


def test_printer_error_text_never_carries_the_ip_into_the_report(tmp_path, stls, resolver, monkeypatch):
    """Review finding 8: the report file is persisted; a connection error names the host it tried."""
    from shambu.facade import redact_ip

    def boom(self, *a, **k):
        raise PrinterError("cannot connect to 10.11.12.13:8883: timed out")

    monkeypatch.setattr(Bambu, "loaded_filaments", boom)
    r = Bambu(studio=FakeStudio(), resolver=resolver).build(Spec.from_json(spec_json(tmp_path, stls, [{"preset": GENERIC}])), export=False)
    blob = r.report + json.dumps(r.to_json())
    assert "10.11.12.13" not in blob and "cannot connect to <ip>:8883" in blob
    assert redact_ip("x 192.168.0.40 y 1.2.3.4") == "x <ip> y <ip>"


def test_filament_map_decides_which_nozzle_is_compared(tmp_path, stls, resolver, with_printer):
    """filament_map [2]: filament 1 prints on the right nozzle, so it is compared with the right spool."""
    app = Bambu(studio=FakeStudio(), resolver=resolver)
    r = app.build(Spec.from_json(spec_json(tmp_path, stls, [{"preset": "loaded"}]) | {"filament_map": [2]}), export=False)
    assert r.plan.filaments[0]["name"] == BASIC and r.plan.filaments[0]["filament_colour"] == ["#2850E0"]


def test_keep_going_never_exports_a_placeholder_filament(tmp_path, stls, resolver):
    studio = FakeStudio()
    r = Bambu(studio=studio, resolver=resolver).build(Spec.from_json(spec_json(tmp_path, stls, [{"preset": "loaded"}])), keep_going=True)
    assert r.plan.unresolved_filaments == [1] and r.run is None and not any(k == "run" for k, _ in studio.calls)


def test_pipeline_without_a_resolution_flags_loaded(tmp_path, stls, resolver):
    r = Pipeline(FakeStudio(), resolver).build(Spec.from_json(spec_json(tmp_path, stls, [{"preset": "loaded"}])), export=False)
    assert not r.plan.ok
    assert r.plan.problems == ['filament 1: preset "loaded" was not resolved against the printer (`shambu check` and `build` read it)']


# ---------------------------------------------------------------------------- 3. printer unreachable: WARN, then the GUI presets
def test_offline_falls_back_to_the_gui_presets_with_a_warning_first(capsys, tmp_path, stls, resolver, conf_file, monkeypatch):
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=FakeStudio(), resolver=resolver))
    p = write_spec(tmp_path, stls, [{"preset": "loaded"}, {"preset": "loaded", "colour": "#101010"}])
    code, out, _ = run(capsys, "--json", "check", str(p))
    j = json.loads(out)
    assert code == 0 and j["plan_ok"] is True
    assert j["warnings"][0].startswith("WARNING: printer not read (PrinterError: no printer in tests): filaments come from the GUI presets")
    assert [(s["preset"], s["colour"], s["source"]) for s in j["filaments"]["sources"]] == [
        (BASIC, "#00AE42", "gui"),
        ("Test PLA", "#101010", "gui"),
    ]
    code, out, _ = run(capsys, "check", str(p))
    lines = out.splitlines()
    assert lines[4].startswith("Filaments: WARNING printer not read (PrinterError: no printer in tests); 1 = Test PETG Basic #00AE42 (gui)")
    assert any(ln.startswith("- WARNING: printer not read") for ln in lines)


def test_offline_pinned_preset_is_kept_and_compared_with_the_gui(tmp_path, stls, resolver, conf_file):
    app = Bambu(studio=FakeStudio(), resolver=resolver)
    r = app.build(Spec.from_json(spec_json(tmp_path, stls, [{"preset": GENERIC, "colour": "#FFFFFF"}])), export=False)
    assert r.plan.ok and r.plan.filaments[0]["name"] == GENERIC
    assert any(f"the spec pins '{GENERIC}' but the GUI's last-used filament 1 is '{BASIC}'" in w for w in r.plan.warnings)


def test_offline_without_a_gui_preset_cannot_resolve_loaded(tmp_path, stls, resolver):
    app = Bambu(studio=FakeStudio(), resolver=resolver)  # conftest: no BambuStudio.conf either
    r = app.build(Spec.from_json(spec_json(tmp_path, stls, [{"preset": "loaded"}])), export=False)
    assert not r.plan.ok
    assert r.plan.problems[0].startswith(
        'filament 1: "loaded" needs the printer or a GUI filament preset; neither is available (PrinterError: no printer in tests)'
    )


def test_no_printer_flag_skips_the_read(tmp_path, stls, resolver, with_printer, conf_file):
    app = Bambu(studio=FakeStudio(), resolver=resolver)
    r = app.build(Spec.from_json(spec_json(tmp_path, stls, [{"preset": "loaded"}])), export=False, printer=False)
    assert r.plan.filaments[0]["name"] == BASIC  # the GUI preset, not the spool
    assert r.plan.filament_resolution.unreachable == "skipped (--no-printer)"


def test_printer_read_is_cached_per_machine_preset(tmp_path, stls, resolver, report, monkeypatch):
    calls = []

    def once(self, *a, **k):
        calls.append(a)
        return report

    monkeypatch.setattr(Bambu, "loaded_filaments", once)
    app = Bambu(studio=FakeStudio(), resolver=resolver)
    s = Spec.from_json(spec_json(tmp_path, stls, [{"preset": "loaded"}]))
    app.build(s, export=False)
    app.build(s, export=False)
    assert len(calls) == 1  # a sweep of many variants reads the printer once


# ---------------------------------------------------------------------------- 4. the commands
class FakeReader:
    seen: list = []

    def __init__(self, ip, serial, access_code, wait=8.0, insecure=False):
        FakeReader.seen.append(access_code)

    def read(self):
        return capture()


@pytest.mark.parametrize("as_json", [True, False])
def test_cli_printer_filaments(conf_file, monkeypatch, capsys, resolver, as_json):
    monkeypatch.setattr(printer, "MqttReader", FakeReader)
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=FakeStudio(), resolver=resolver))
    code, out, err = run(capsys, *(["--json"] if as_json else []), "printer", "filaments", "--ip", "10.0.0.9")
    assert code == 0 and FakeReader.seen[-1] == SENTINEL and SENTINEL not in out + err and SERIAL not in out + err
    if as_json:
        j = json.loads(out)
        assert j["printer_preset"] == MACHINE
        assert [(z["nozzle"], z["tray"]["preset"], z["tray"]["colour"]) for z in j["nozzles"]] == [
            (1, GENERIC, "#FFFFFF"),
            (2, BASIC, "#2850E0"),
        ]
    else:
        assert "nozzle 1 (main (left)): left external spool: PETG, #FFFFFF (white), preset id GFG99 -> Generic PETG @Test X2D" in out


def test_doctor_shows_the_spools_next_to_the_gui_presets(conf_file, monkeypatch, capsys, resolver, with_printer):
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=FakeStudio(), resolver=resolver))
    code, out, err = run(capsys, "doctor")
    assert code == 0 and SENTINEL not in out + err
    assert f"GUI presets: machine {MACHINE}, process None, filaments ['{BASIC}', 'Test PLA']" in out
    assert f"loaded on the printer (read {with_printer.read_at}, presets for {MACHINE}):" in out
    assert f"  nozzle 1 (main (left)): left external spool: PETG #FFFFFF (GFG99) -> {GENERIC}" in out
    assert f"  MISMATCH: the GUI's last-used filament 1 is '{BASIC}', the spool in nozzle 1 is '{GENERIC}'" in out
    assert "MISMATCH: the GUI's last-used filament 2" in out  # Test PLA in the GUI, PETG Basic on the right spool
    code, out, _ = run(capsys, "--json", "doctor")
    assert json.loads(out)["printer"]["nozzles"][0]["tray"]["preset"] == GENERIC


def test_doctor_without_the_printer_says_so(conf_file, monkeypatch, capsys, resolver):
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=FakeStudio(), resolver=resolver))
    code, out, _ = run(capsys, "doctor")
    assert code == 0 and "loaded on the printer: not read (PrinterError: no printer in tests)" in out
    code, out, _ = run(capsys, "--json", "doctor", "--no-printer")
    assert code == 0 and json.loads(out)["printer"] == {"error": "skipped (--no-printer)"}


def test_service_filaments_uses_one_read(conf_file, monkeypatch, resolver):
    monkeypatch.setattr(printer, "MqttReader", FakeReader)
    n = len(FakeReader.seen)
    lf = printer.PrinterService(ip="10.0.0.9").filaments(resolver, MACHINE)
    assert len(FakeReader.seen) == n + 1 and lf.nozzle(1).tray.preset == GENERIC


def test_reconcile_offline_with_colour_list():
    """`filament_colors` may be one string, a ';' list or a JSON list: each maps by slot."""
    assert loaded._split_colours("#000000;#FFFFFF") == ["#000000", "#FFFFFF"]
    assert loaded._split_colours(["#000000"]) == ["#000000"] and loaded._split_colours(None) == []


def test_unreachable_error_type_is_named(tmp_path, stls, resolver, monkeypatch):
    def boom(self, *a, **k):
        raise PrinterError("no host on this LAN answers on port 8883")

    monkeypatch.setattr(Bambu, "loaded_filaments", boom)
    r = Bambu(studio=FakeStudio(), resolver=resolver).build(Spec.from_json(spec_json(tmp_path, stls, [{"preset": GENERIC}])), export=False)
    assert r.plan.filament_resolution.unreachable == "PrinterError: no host on this LAN answers on port 8883"


def test_sweep_carries_the_fallback_warning_and_the_sources(capsys, tmp_path, stls, resolver, conf_file, monkeypatch):
    """Review finding 5: the sweep table and JSON must say where the filaments came from, not only the report files."""
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=FakeStudio(), resolver=resolver))
    p = write_spec(tmp_path, stls, [{"preset": "loaded"}])
    _, out, _ = run(capsys, "sweep", str(p), "--by-object", "--no-slice")
    lines = out.splitlines()
    assert lines[0].startswith("WARNING: printer not read (PrinterError: no printer in tests)")
    assert lines[1].startswith(f"Filaments: WARNING printer not read (PrinterError: no printer in tests); 1 = {BASIC} #00AE42 (gui)")
    _, out, _ = run(capsys, "--json", "sweep", str(p), "--by-object", "--no-slice")
    j = json.loads(out)
    assert all(r["warnings"][0].startswith("WARNING: printer not read") and r["filaments"].startswith("Filaments: WARNING") for r in j)
