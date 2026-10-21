"""calibrate.py (M73 track, pairs, fits, the hand-back text), the calibration file in conf.py, the report's estimate
line and the `calibrate` verb. Offline: fixed logs and a synthetic G-code, no printer, no Studio."""

import json
import zipfile
from pathlib import Path

import pytest

from shambu import calibrate as C
from shambu import cli, conf
from shambu.errors import ConfError
from shambu.report import estimate_lines

FIXTURES = Path(__file__).parent / "fixtures"
X2D = "Bambu Lab X2D"

GCODE = """; HEADER_BLOCK_START
; BambuStudio 02.08.02.61
; model printing time: 1h 35m 0s; total estimated time: 1h 40m 0s
; total layer number: 3
; HEADER_BLOCK_END
; CONFIG_BLOCK_START
; filament_settings_id = "Generic PETG @BBL X2D 0.4 nozzle"
; filament_type = PETG
; print_settings_id = 0.20mm Standard @BBL X2D
; printer_model = Bambu Lab X2D
; printer_settings_id = Bambu Lab X2D 0.4 nozzle
; CONFIG_BLOCK_END
M73 P0 R100
G28
M73 P1 R95
; CHANGE_LAYER
M73 L1
G1 X1
M73 P5 R90
; CHANGE_LAYER
M73 L2
M73 P50 R50
G1 X2
; CHANGE_LAYER
M73 L3
M73 P90 R10
M73 P100 R0
"""


def track() -> C.M73Track:
    return C.parse_m73(GCODE.splitlines(keepends=True))


def poll(t: str, state: str = "RUNNING", layer: int | None = None, remaining: int | None = None, speed: int | None = 2) -> dict:
    return {"kind": "poll", "read_at": f"2026-10-21T{t}Z", "state": state, "layer": layer, "remaining_min": remaining, "speed_level": speed}


def whole_log(scale: float, speed: int | None = 2) -> list[dict]:
    """A print watched from the start to FINISH that runs at `scale` x Studio with a 10 min startup Studio does not
    count: Studio has mid-layer 1 at 30 min, mid-layer 2 at 70, mid-layer 3 at 90 and the end at 100."""

    def at(studio_min: float) -> str:
        m = 10 + studio_min * scale
        return f"10:{int(m):02d}:{round((m % 1) * 60):02d}" if m < 60 else f"11:{int(m - 60):02d}:{round((m % 1) * 60):02d}"

    return [
        poll("10:00:00", "PREPARE", 0, 100, speed),
        poll("10:00:30", "RUNNING", 0, 100, speed),
        poll(at(30), "RUNNING", 1, 70, speed),
        poll(at(70), "RUNNING", 2, 30, speed),
        poll(at(90), "RUNNING", 3, 10, speed),
        {**poll(at(100), "FINISH", 3, 0, speed), "final": True},
    ]


def fixture(name: str):
    d = json.loads((FIXTURES / "calibration_pairs.json").read_text())[name]
    return C.M73Track.from_json(d["track"]), d["log"]


# ---------------------------------------------------------------------------- the M73 track
def test_m73_parser_reads_layers_total_and_header():
    t = track()
    assert t.total_min == 100
    assert t.layers == {1: (5, 90), 2: (50, 50), 3: (90, 10)}
    assert (t.printer_model, t.material, t.filament_settings_id) == (X2D, "PETG", "Generic PETG @BBL X2D 0.4 nozzle")
    assert t.remaining_at(1) == 70 and t.elapsed_at(1) == 30
    assert t.remaining_at(3) == 10  # the last layer pairs with itself
    assert C.M73Track.from_json(json.loads(json.dumps(t.to_json()))) == t


def test_m73_parser_falls_back_to_layer_markers():
    t = C.parse_m73([x for x in GCODE.splitlines() if not x.startswith("M73 L")])
    assert t.layers == {1: (5, 90), 2: (50, 50), 3: (90, 10)}


def test_m73_parser_days_in_total():
    t = C.parse_m73(["; model printing time: 1d 2h 3m 4s; total estimated time: 1d 2h 4m 0s\n"])
    assert t.total_min == 1440 + 120 + 4


# ---------------------------------------------------------------------------- the two measured prints
def test_d7_mid_print_ratio_and_flags():
    t, log = fixture("d7")
    f = C.fit(C.pairs_from_log(log, t), t)
    assert f.ratio == 0.751 and f.kind == "span" and f.layers == (11, 620)
    assert (f.real_min, f.studio_min) == (211.0, 281.0)
    assert set(f.flags) == {C.FLAG_DISAGREE, C.FLAG_END, C.FLAG_START, C.FLAG_SPEED_UNKNOWN}
    assert f.printer_vs_studio[0] < 0.75 and f.printer_vs_studio[1] < 0.82
    assert f.startup_offset_min is None


def test_e7a_mid_print_ratio_and_flags():
    t, log = fixture("e7a")
    f = C.fit(C.pairs_from_log(log, t), t)
    assert f.ratio == 1.054 and f.layers == (135, 182)
    assert C.FLAG_DISAGREE not in f.flags  # the printer's countdown is Studio's M73 R here
    assert set(f.flags) == {C.FLAG_END, C.FLAG_START, C.FLAG_SPEED_UNKNOWN}


def test_two_prints_give_a_range_and_no_point_estimate():
    recs = []
    for name in ("d7", "e7a"):
        t, log = fixture(name)
        recs.append(C.fit(C.pairs_from_log(log, t), t).record())
    f = C.combine(recs, X2D, "petg")
    assert (f.n, f.min_ratio, f.max_ratio, f.point) == (2, 0.751, 1.054, None)
    assert "n=2" in f.why_no_point
    assert C.handback(recs, X2D, "PETG", 6 * 3600) == "; measured prints ran at 0.75 to 1.05 x Studio (n=2)"
    assert C.handback(recs, X2D, "PLA", 6 * 3600) == ""  # another material: n=0
    assert C.handback(recs, "Bambu Lab H2D", "PETG", 6 * 3600) == ""


# ---------------------------------------------------------------------------- synthetic whole prints
def test_whole_print_ratio_and_startup_offset():
    t = track()
    f = C.fit(C.pairs_from_log(whole_log(0.8), t), t)
    assert f.kind == "whole" and f.layers == (0, 3)
    assert f.ratio == pytest.approx((10 - 0.5 + 80) / 100, abs=0.002)  # from the first RUNNING poll to FINISH
    assert f.startup_offset_min == pytest.approx(9.5 + 30 * 0.8 - 30, abs=0.1)
    assert C.FLAG_END not in f.flags and C.FLAG_START not in f.flags and C.FLAG_SPEED not in f.flags


def test_speed_level_other_than_standard_is_flagged():
    t = track()
    log = whole_log(1.0)
    log[3]["speed_level"] = 3
    assert C.FLAG_SPEED in C.fit(C.pairs_from_log(log, t), t).flags


def test_printer_far_from_studio_is_flagged():
    t = track()
    log = whole_log(1.0)
    log[3]["remaining_min"] = 52  # Studio's M73 R mid-layer 2 is 30: the printer says 1.73 x
    assert C.FLAG_DISAGREE in C.fit(C.pairs_from_log(log, t), t).flags


def test_three_agreeing_standard_prints_give_a_point_estimate():
    t = track()
    recs = [C.fit(C.pairs_from_log(whole_log(s), t), t).record() for s in (0.8, 0.82, 0.85)]
    f = C.combine(recs, X2D, "PETG")
    assert f.n == 3 and f.point is not None and f.max_ratio - f.min_ratio < 0.15
    assert C.handback(recs, X2D, "PETG", 6 * 3600 + 50 * 60).startswith("; calibrated about ")
    assert C.handback(recs, X2D, "PETG", 6 * 3600 + 50 * 60).endswith("(n=3)")


def test_three_prints_with_one_unlogged_speed_give_no_point():
    t = track()
    recs = [C.fit(C.pairs_from_log(whole_log(s, speed), t), t).record() for s, speed in ((0.8, 2), (0.82, 2), (0.85, None))]
    f = C.combine(recs, X2D, "PETG")
    assert f.point is None and "Standard" in f.why_no_point


def test_three_prints_that_disagree_give_no_point():
    t = track()
    recs = [C.fit(C.pairs_from_log(whole_log(s), t), t).record() for s in (0.8, 0.9, 1.05)]
    assert C.combine(recs, X2D, "PETG").point is None


def test_a_log_without_a_running_poll_raises():
    with pytest.raises(C.CalibrationError, match="no RUNNING poll"):
        C.pairs_from_log([poll("10:00:00", "PREPARE", 0, 100)], track())


def test_a_line_that_is_not_json_raises():
    with pytest.raises(C.CalibrationError, match="line 2"):
        C.read_log(['{"kind": "header"}\n', "not json\n"])


# ---------------------------------------------------------------------------- the calibration file
def test_calibration_file_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(conf, "CALIBRATION", tmp_path / "shambu" / "calibration.json")
    assert conf.load_calibration() == []
    t = track()
    recs = [C.fit(C.pairs_from_log(whole_log(0.8), t), t).record(log="a.jsonl", gcode3mf="a.gcode.3mf", task="a.gcode")]
    conf.save_calibration(recs)
    assert conf.load_calibration() == json.loads(json.dumps(recs))
    assert json.loads(conf.CALIBRATION.read_text())["version"] == 1
    assert [p.name for p in conf.CALIBRATION.parent.iterdir()] == ["calibration.json"]  # no temp file left


def test_calibration_file_refuses_identifiers(tmp_path):
    with pytest.raises(ConfError):
        conf.save_calibration([{"ratio": 1.0, "ip": "10.0.0.9"}], tmp_path / "c.json")
    with pytest.raises(ConfError):
        conf.save_calibration([{"ratio": 1.0, "serial": "X"}], tmp_path / "c.json")
    assert not (tmp_path / "c.json").exists()


def test_a_broken_calibration_file_raises(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("[1, 2")
    with pytest.raises(ConfError):
        conf.load_calibration(p)


# ---------------------------------------------------------------------------- the report's estimate line
SL = {
    "plate": 1,
    "prediction_s": 6 * 3600 + 50 * 60,
    "weight_g": 179.6,
    "filaments": [{"id": "1", "type": "PETG", "used_g": 179.6}],
    "warnings": [],
}


def test_report_wording_without_calibration():
    line = estimate_lines(SL)[0]
    assert line == "- plate 1 estimate: Studio estimate 6 h 50 min, 179.6 g (filament 1 PETG 179.6 g)"
    assert estimate_lines(SL, [], X2D)[0] == line


def test_report_wording_with_two_prints_gives_the_range():
    recs = []
    for name in ("d7", "e7a"):
        t, log = fixture(name)
        recs.append(C.fit(C.pairs_from_log(log, t), t).record())
    line = estimate_lines(SL, recs, X2D)[0]
    assert line.startswith("- plate 1 estimate: Studio estimate 6 h 50 min; measured prints ran at 0.75 to 1.05 x Studio (n=2), 179.6 g")


def test_report_wording_with_three_standard_prints_gives_a_point():
    t = track()
    recs = [C.fit(C.pairs_from_log(whole_log(s), t), t).record() for s in (0.8, 0.8, 0.8)]
    line = estimate_lines(SL, recs, X2D)[0]
    assert "Studio estimate 6 h 50 min; calibrated about" in line and "(n=3)" in line


# ---------------------------------------------------------------------------- the calibrate verb
def gcode3mf(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/project_settings.config", "{}")
        z.writestr("Metadata/model_settings.config", "<config/>")
        z.writestr("Metadata/plate_1.gcode", GCODE)
    return path


def write_log(path: Path, records: list[dict], tied: bool = True) -> Path:
    header = {
        "kind": "header",
        "shambu_version": "test",
        "track": track().to_json() if tied else None,
        "gcode3mf": "x.gcode.3mf" if tied else None,
    }
    path.write_text("".join(json.dumps(r) + "\n" for r in [header, *records]))
    return path


def test_cli_calibrate_add_then_show(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(conf, "CALIBRATION", tmp_path / "calibration.json")
    log = write_log(tmp_path / "w.jsonl", whole_log(0.8))
    assert cli.main(["--json", "calibrate", "--log", str(log), "--add"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["added"] == 1 and d["prints"][0]["kind"] == "whole"
    assert cli.main(["--json", "calibrate", "--log", str(log), "--add"]) == 0  # the same log again replaces its entry
    capsys.readouterr()
    assert len(conf.load_calibration()) == 1
    assert cli.main(["calibrate", "--show"]) == 0
    text = capsys.readouterr().out
    assert "n=1" in text and "no point estimate" in text


def test_cli_calibrate_exit_1_for_an_untied_log_or_no_running_poll(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(conf, "CALIBRATION", tmp_path / "calibration.json")
    untied = write_log(tmp_path / "u.jsonl", whole_log(0.8), tied=False)
    idle = write_log(tmp_path / "i.jsonl", [poll("10:00:00", "PREPARE", 0, 100)])
    assert cli.main(["--json", "calibrate", "--log", str(untied), "--log", str(idle), "--add"]) == 1
    d = json.loads(capsys.readouterr().out)
    assert len(d["errors"]) == 2 and d["added"] == 0
    assert "not tied" in d["errors"][0] and "no RUNNING poll" in d["errors"][1]
    assert not conf.CALIBRATION.exists()


def test_gcode_track_from_a_sliced_project(tmp_path):
    t, entry = cli._gcode_track(str(gcode3mf(tmp_path / "p.gcode.3mf")), None)
    assert entry == "Metadata/plate_1.gcode" and t == track()
