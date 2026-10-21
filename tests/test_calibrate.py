"""calibrate.py (M73 track, sessions, polls, fits, the stored-print schema, the hand-back text), the calibration file
in conf.py, the report's estimate line and the `calibrate` verb. Offline: fixed logs and synthetic G-code."""

import json
import threading
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shambu import calibrate as C
from shambu import cli, conf, threemf
from shambu.errors import BambuError, ConfError, ProjectError
from shambu.report import estimate_lines

FIXTURES = Path(__file__).parent / "fixtures"
X2D = "Bambu Lab X2D"
N_LAYERS = 30
T0 = datetime(2026, 10, 21, 10, 0, 0, tzinfo=UTC)


def gcode(material="PETG", marker="M73 L", total_line="1h 40m 0s", header_layers=True, first_marked=True, m73=True) -> str:
    """A synthetic plate G-code: Studio total 100 min, layer k starts at Studio minute 10 + 3 (k - 1), so the M73 R
    at layer k's start is 90 - 3 (k - 1) and Studio's mid-layer elapsed is 11.5 + 3 (k - 1) (97 on the last layer)."""
    out = [
        "; HEADER_BLOCK_START",
        f"; model printing time: 1h 35m 0s; total estimated time: {total_line}",
        f"; total layer number: {N_LAYERS}" if header_layers else "; no layer count",
        "; HEADER_BLOCK_END",
        "; CONFIG_BLOCK_START",
        f'; filament_settings_id = "Generic {material} @BBL X2D 0.4 nozzle"',
        f"; filament_type = {material}",
        "; print_settings_id = 0.20mm Standard @BBL X2D",
        "; printer_model = Bambu Lab X2D",
        "; CONFIG_BLOCK_END",
        "M73 P0 R100" if m73 else "G28",
    ]
    for k in range(1, N_LAYERS + 1):
        if k > 1 or first_marked:
            if marker == "M73 L":
                out += ["; CHANGE_LAYER", f"M73 L{k}"]
            elif marker == ";LAYER:":
                out.append(f";LAYER:{k - 1}")
            else:
                out.append(marker)
        out.append("G1 X1 E1")
        if m73:
            out.append(f"M73 P{round(100 * (10 + 3 * (k - 1)) / 100)} R{90 - 3 * (k - 1)}")
    out.append("M73 P100 R0" if m73 else "G1 X0")
    return "\n".join(out) + "\n"


def track(material="PETG") -> C.M73Track:
    return C.parse_m73(gcode(material).splitlines(keepends=True))


def studio_mid(k: int) -> float:
    return 97.0 if k == N_LAYERS else 11.5 + 3 * (k - 1)


def at(minutes: float) -> str:
    return (T0 + timedelta(seconds=round(minutes * 60))).strftime("%Y-%m-%dT%H:%M:%SZ")


def poll(minutes, state="RUNNING", layer=None, remaining=None, speed=2, percent=100, task="1360") -> dict:
    return {
        "kind": "poll",
        "read_at": at(minutes),
        "state": state,
        "layer": layer,
        "remaining_min": remaining,
        "speed_level": speed,
        "speed_percent": percent,
        "task_id": task,
    }


def print_log(rate=0.8, offset=9.5, start=True, end="FINISH", layers=(1, 5, 11, 20, N_LAYERS), **kw) -> list[dict]:
    """A print that runs at `rate` x Studio after a startup `offset` (real minutes from the first RUNNING poll):
    real = offset + rate x Studio elapsed. The printer's remaining equals Studio's mid-layer M73 R."""
    out = []
    if start:
        out += [poll(-1, "PREPARE", 0, 100, **kw), poll(0, "RUNNING", 0, 100, **kw)]
    for k in layers:
        out.append(poll(offset + rate * studio_mid(k), "RUNNING", k, round(100 - studio_mid(k)), **kw))
    if end:
        out.append({**poll(offset + rate * 100, end, N_LAYERS, 0, **kw), "final": True})
    return out


def header(t=None, gcode3mf="plate.gcode.3mf") -> dict:
    return {"kind": "header", "shambu_version": "test", "gcode3mf": gcode3mf, "track": (t or track()).to_json()}


def fit_log(polls, t=None):
    t = t or track()
    (s,) = C.read_log([header(t), *polls])
    return C.fit(C.polls_from_session(s, t), s, t)


def stored(rate=0.8, **kw):
    return fit_log(print_log(rate=rate, **kw)).to_print("w.jsonl")


def fixture(name: str):
    d = json.loads((FIXTURES / "calibration_prints.json").read_text())[name]
    t = C.M73Track.from_json(d["track"])
    (s,) = C.read_log([{"kind": "header", "track": d["track"]}, *d["log"]])
    return t, s


# ---------------------------------------------------------------------------- the M73 track
def test_m73_parser_reads_layers_total_and_header():
    t = track()
    assert t.total_min == 100 and len(t.layers) == N_LAYERS
    assert t.layers[1] == (10, 90) and t.layers[N_LAYERS] == (97, 3)
    assert (t.printer_model, t.material, t.filament_settings_id) == (X2D, "PETG", "Generic PETG @BBL X2D 0.4 nozzle")
    assert t.remaining_at(1) == 88.5 and t.remaining_at(N_LAYERS) == 3
    assert C.M73Track.from_json(json.loads(json.dumps(t.to_json()))) == t


def test_m73_parser_numbered_layer_markers_are_zero_based():
    t = C.parse_m73(gcode(marker=";LAYER:").splitlines())
    assert t.layers == track().layers


def test_m73_parser_counts_unnumbered_markers():
    assert C.parse_m73(gcode(marker="; CHANGE_LAYER").splitlines()).layers == track().layers


def test_m73_parser_rejects_an_unmarked_first_layer():
    with pytest.raises(C.CalibrationError, match="first layer may be unmarked"):
        C.parse_m73(gcode(marker=";LAYER_CHANGE", first_marked=False).splitlines())
    with pytest.raises(C.CalibrationError, match="no '; total layer number'"):
        C.parse_m73(gcode(marker=";LAYER_CHANGE", header_layers=False).splitlines())


def test_m73_parser_needs_real_m73_lines():
    with pytest.raises(C.CalibrationError, match="no 'M73 P<n> R<m>'"):
        C.parse_m73(gcode(m73=False).splitlines())


def test_m73_parser_crlf_and_totals_without_seconds_or_with_days():
    assert C.parse_m73(gcode().replace("\n", "\r\n").splitlines(keepends=True)) == track()
    assert C.parse_m73(gcode(total_line="1h 40m").splitlines()).total_min == 100
    assert C.parse_m73(gcode(total_line="1d 2h 4m 0s").splitlines()).total_min == 1440 + 124


def test_a_malformed_track_raises_a_calibration_error():
    for bad in (
        None,
        {"total_min": 0, "layers": {"1": [0, 1]}},
        {"total_min": 10, "layers": {"1": [0]}},
        {"total_min": float("nan"), "layers": {}},
    ):
        with pytest.raises(C.CalibrationError):
            C.M73Track.from_json(bad)
    assert issubclass(C.CalibrationError, BambuError)


# ---------------------------------------------------------------------------- the two measured prints
def test_d7_mid_print_rate_and_flags():
    t, s = fixture("d7")
    f = C.fit(C.polls_from_session(s, t), s, t)
    assert f.rate == 0.751 and f.kind == "span" and f.rate_layers == (11, 620)
    assert set(f.flags) == {C.FLAG_DISAGREE, C.FLAG_END, C.FLAG_START, C.FLAG_SPEED_UNKNOWN}
    assert f.printer_vs_studio[0] < 0.75 and f.printer_vs_studio[1] < 0.82
    assert f.startup_offset_min is None and f.whole_ratio is None


def test_e7a_mid_print_rate_and_flags():
    t, s = fixture("e7a")
    f = C.fit(C.polls_from_session(s, t), s, t)
    assert f.rate == 1.054 and f.rate_layers == (135, 182)
    assert set(f.flags) == {C.FLAG_END, C.FLAG_START, C.FLAG_SPEED_UNKNOWN}  # the printer agrees with Studio here


def test_two_prints_give_a_range_and_no_point_estimate():
    prints = []
    for name in ("d7", "e7a"):
        t, s = fixture(name)
        prints.append(C.fit(C.polls_from_session(s, t), s, t).to_print(f"{name}.jsonl"))
    f = C.combine(prints, X2D, "petg")
    assert (f.n, f.min_rate, f.max_rate, f.rate, f.n_eligible) == (2, 0.751, 1.054, None, 0)
    assert C.handback(prints, X2D, "PETG", 6 * 3600) == "; measured prints ran at 0.75 to 1.05 x Studio (n=2)"
    assert C.handback(prints, X2D, "PLA", 6 * 3600) == ""  # another material: n=0
    assert C.handback(prints, "Bambu Lab H2D", "PETG", 6 * 3600) == ""


# ---------------------------------------------------------------------------- the synthetic print: rate and offset
def test_rate_and_startup_offset_are_fitted_apart():
    f = fit_log(print_log(rate=0.8, offset=9.5))
    assert f.kind == "whole" and f.rate == 0.8 and f.rate_layers == (11, N_LAYERS)
    assert f.startup_offset_min == 9.5
    assert f.whole_ratio == pytest.approx((9.5 + 80) / 100, abs=0.001)  # descriptive: startup and rate mixed
    assert not f.flags


def test_a_whole_print_prediction_is_offset_plus_rate_times_studio():
    prints = [stored(rate=0.8, offset=9.5) | {"first_read_at": f"2026-10-2{i}T10:00:00Z"} for i in range(3)]
    f = C.combine(prints, X2D, "PETG")
    assert (f.rate, f.offset_min, f.n_eligible) == (0.8, 9.5, 3)
    assert f.predict_min(1000) == pytest.approx(809.5)
    assert C.handback(prints, X2D, "PETG", 1000 * 60) == "; calibrated about 13 h 30 min (n=3)"


def test_a_logged_start_without_an_end_keeps_the_startup_out_of_the_rate():
    f = fit_log(print_log(rate=1.0, offset=25, end=None))
    assert f.kind == "span" and f.rate == 1.0 and f.startup_offset_min == 25
    assert C.FLAG_END in f.flags


# ---------------------------------------------------------------------------- what spoils a print
def test_a_pause_is_subtracted_and_flagged():
    """A one-hour pause at layer 15: a PAUSE poll, then a RUNNING poll at the resume; the rate stays 0.8."""
    t15 = 9.5 + 0.8 * studio_mid(15)
    log = [
        *print_log(layers=(1, 5, 11), end=None),
        poll(t15, "PAUSE", 15, 55),
        poll(t15 + 60, "RUNNING", 15, round(100 - studio_mid(15))),
        *[poll(60 + 9.5 + 0.8 * studio_mid(k), "RUNNING", k, round(100 - studio_mid(k))) for k in (20, N_LAYERS)],
        {**poll(60 + 9.5 + 80, "FINISH", N_LAYERS, 0), "final": True},
    ]
    f = fit_log(log)
    assert f.rate == 0.8 and f.paused_min == pytest.approx(60, abs=0.02)
    assert f.real_min == pytest.approx(89.5, abs=0.02) and C.FLAG_PAUSED in f.flags


def test_a_clock_that_goes_back_rejects_the_log():
    log = print_log()
    log[4]["read_at"] = log[2]["read_at"]
    log[3]["read_at"] = at(100)
    with pytest.raises(C.CalibrationError, match="line 6: read_at goes back"):
        fit_log(log)


def test_a_changed_task_id_or_a_layer_going_back_rejects_the_log():
    log = print_log()
    log[4]["task_id"] = "9999"
    with pytest.raises(C.CalibrationError, match="job changed"):
        fit_log(log)
    log = print_log()
    log[4]["layer"] = 3
    with pytest.raises(C.CalibrationError, match="layer went back"):
        fit_log(log)


@pytest.mark.parametrize(
    ("change", "flag"),
    [
        ({"speed_percent": 150}, C.FLAG_SPEED),
        ({"speed_level": 3}, C.FLAG_SPEED),
        ({"remaining_min": 40}, C.FLAG_DISAGREE),
        ({"speed_level": None}, C.FLAG_SPEED_UNKNOWN),
    ],
)
def test_a_spoiled_poll_flags_the_print_and_blocks_a_point_estimate(change, flag):
    def spoiled(i):
        log = print_log()
        log[4] = {**log[4], **change}  # layer 11
        return fit_log(log).to_print(f"w{i}.jsonl")

    prints = [stored() | {"log": "a.jsonl"}, stored() | {"log": "b.jsonl"}, spoiled(0)]
    assert flag in prints[2]["flags"]
    f = C.combine(prints, X2D, "PETG")
    assert f.rate is None and f.n_eligible == 2


def test_a_speed_change_on_a_poll_off_the_track_is_still_seen():
    log = print_log()
    log[1] = {**log[1], "speed_level": 4}  # the RUNNING layer-0 poll, which pairs with no layer
    assert C.FLAG_SPEED in fit_log(log).flags


def test_a_failed_print_is_flagged_and_blocks_a_point_estimate():
    f = fit_log(print_log(end="FAILED"))
    assert C.FLAG_FAILED in f.flags and C.FLAG_END in f.flags and f.final_state == "FAILED"
    prints = [stored() | {"log": "a.jsonl"}, stored() | {"log": "b.jsonl"}, f.to_print("c.jsonl")]
    assert C.combine(prints, X2D, "PETG").rate is None


def test_thresholds_at_their_boundaries():
    """The printer may depart from Studio's M73 R by exactly 5 % unflagged (research C, B4); 6 % is flagged. A rate
    span of exactly 0.15 gets no point estimate; 0.14 does."""
    t = track()
    (s,) = C.read_log([header(), *print_log()])
    agree = [C.Poll("layer", at(0), 11, 0, 0, 70.0, 73, 2, 100), C.Poll("layer", at(9), 12, 9, 9, 60.0, 63, 2, 100)]  # +4.3 %, +5.0 %
    assert C.FLAG_DISAGREE not in C.fit(agree, s, t).flags
    over = [*agree, C.Poll("layer", at(18), 13, 18, 18, 50.0, 53, 2, 100)]  # +6 %
    assert C.FLAG_DISAGREE in C.fit(over, s, t).flags
    edge = [stored(r) | {"log": f"{i}.jsonl"} for i, r in enumerate((0.8, 0.9, 0.95))]
    assert C.combine(edge, X2D, "PETG").rate is None
    inside = [stored(r) | {"log": f"{i}.jsonl"} for i, r in enumerate((0.8, 0.9, 0.94))]
    assert C.combine(inside, X2D, "PETG").rate is not None


# ---------------------------------------------------------------------------- the log's shape
@pytest.mark.parametrize("line", ["null", "[]", "42", '"text"'])
def test_a_line_that_is_not_an_object_raises(line):
    with pytest.raises(C.CalibrationError, match="line 2 is not a JSON object"):
        C.read_log(['{"kind": "header"}\n', line + "\n"])


@pytest.mark.parametrize(
    ("bad", "match"),
    [
        ({"read_at": "bad"}, "not a UTC timestamp"),
        ({"read_at": "2026-13-41T10:00:00Z"}, "not a valid date"),
        ({"layer": "12"}, "'layer' is not an integer"),
        ({"state": None}, "without a text 'state'"),
        ({"task_id": 5}, "'task_id' is not text"),
    ],
)
def test_a_malformed_poll_raises_with_its_line(bad, match):
    with pytest.raises(C.CalibrationError, match=match):
        C.read_log([header(), {**poll(0), **bad}])


def test_each_header_starts_a_session():
    sessions = C.read_log([header(track("PETG")), *print_log(), header(track("PLA")), *print_log()])
    assert len(sessions) == 2 and sessions[1].line == len(print_log()) + 2
    assert [C.M73Track.from_json(s.header["track"]).material for s in sessions] == ["PETG", "PLA"]


def test_a_log_without_a_running_poll_raises():
    (s,) = C.read_log([header(), poll(0, "PREPARE", 0, 100)])
    with pytest.raises(C.CalibrationError, match="no RUNNING poll"):
        C.polls_from_session(s, track())


# ---------------------------------------------------------------------------- the stored print and the file
def test_calibration_file_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(conf, "CALIBRATION", tmp_path / "shambu" / "calibration.json")
    assert conf.load_calibration() == []
    prints = [stored()]
    conf.save_calibration(prints)
    assert conf.load_calibration() == prints
    assert json.loads(conf.CALIBRATION.read_text())["version"] == 1
    assert [p.name for p in conf.CALIBRATION.parent.iterdir()] == ["calibration.json"]  # no temp file left


@pytest.mark.parametrize(
    "change",
    [
        {"meta": {"serial": "00X0TEST0000999"}},  # a nested field
        {"material": "10.0.0.9"},  # an IP in a name
        {"material": "fe80::1"},
        {"printer_model": "X2D 00X…999"},  # a masked serial
        {"printer_model": "printer.local"},  # a host name
        {"log": "/home/someone/w.jsonl"},  # a path, not a file name
        {"rate": float("nan")},
        {"rate": -0.5},
        {"studio_total_min": float("inf")},
        {"flags": ["made_up"]},
    ],
)
def test_calibration_file_refuses_identifiers_paths_and_bad_numbers(tmp_path, change):
    with pytest.raises(ConfError) as e:
        conf.save_calibration([stored() | change], tmp_path / "c.json")
    assert not (tmp_path / "c.json").exists()
    text = str(e.value)
    assert C.REFUSAL in text and "00X" not in text and "10.0.0.9" not in text and "/home" not in text


@pytest.mark.parametrize(
    "doc",
    [
        {"version": 1, "prints": [None, "damaged"]},
        {"version": 2, "prints": []},
        {"prints": []},
        [1, 2],
        {"version": 1, "prints": [{"rate": 1.0}]},
    ],
)
def test_a_damaged_calibration_file_raises(tmp_path, doc):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(doc))
    with pytest.raises(ConfError):
        conf.load_calibration(p)


def test_concurrent_additions_both_land(tmp_path):
    """Thread A holds the lock inside its change; thread B's update waits for it and then sees A's print."""
    p = tmp_path / "calibration.json"
    inside, release = threading.Event(), threading.Event()

    def slow(prints):
        inside.set()
        release.wait(5)
        return [*prints, stored() | {"log": "a.jsonl"}]

    a = threading.Thread(target=conf.update_calibration, args=(slow, p))
    a.start()
    assert inside.wait(5)
    b = threading.Thread(target=conf.update_calibration, args=(lambda prints: [*prints, stored() | {"log": "b.jsonl"}], p))
    b.start()
    b.join(0.3)
    assert b.is_alive()  # B waits on the lock
    release.set()
    a.join(5)
    b.join(5)
    assert sorted(x["log"] for x in conf.load_calibration(p)) == ["a.jsonl", "b.jsonl"]


def test_a_write_failure_is_a_data_file_error(tmp_path, monkeypatch):
    def boom(*a):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(conf.os, "replace", boom)
    with pytest.raises(conf.DataFileError, match="No space left"):
        conf.save_calibration([stored()], tmp_path / "c.json")
    assert [x.name for x in tmp_path.iterdir()] == []


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
    assert line == "- plate 1: Studio estimate 6 h 50 min, 179.6 g (filament 1 PETG 179.6 g)"
    assert estimate_lines(SL, [], X2D)[0] == line


def test_report_wording_with_two_prints_gives_the_range():
    prints = []
    for name in ("d7", "e7a"):
        t, s = fixture(name)
        prints.append(C.fit(C.polls_from_session(s, t), s, t).to_print(f"{name}.jsonl"))
    line = estimate_lines(SL, prints, X2D)[0]
    assert line.startswith("- plate 1: Studio estimate 6 h 50 min; measured prints ran at 0.75 to 1.05 x Studio (n=2), 179.6 g")


def test_report_wording_with_three_standard_prints_gives_a_point():
    prints = [stored() | {"log": f"{i}.jsonl"} for i in range(3)]
    line = estimate_lines(SL, prints, X2D)[0]
    assert "Studio estimate 6 h 50 min; calibrated about 5 h 38 min (n=3)" in line  # 9.5 + 0.8 x 410


def test_render_reads_the_calibration_file_and_survives_a_corrupt_one(tmp_path, monkeypatch, stls, resolver):
    """The plan's printer model (the machine preset's `printer_model`, which Studio also writes as `; printer_model`
    in the G-code header) keys the lookup."""
    from test_result_json import build

    model = resolver.for_cli("machine", "Test X2D 0.4 nozzle")["printer_model"]
    monkeypatch.setattr(conf, "CALIBRATION", tmp_path / "calibration.json")
    conf.save_calibration([stored() | {"printer_model": model, "material": "PLA", "log": f"{i}.jsonl"} for i in range(2)])
    r = build(tmp_path, stls, resolver)
    assert "Studio estimate" in r.report and "measured prints ran at 0.80 x Studio (n=2)" in r.report
    conf.CALIBRATION.write_text("{broken")
    (tmp_path / "again").mkdir()
    r = build(tmp_path / "again", stls, resolver)
    assert "calibration file not read" in r.report and str(tmp_path) not in r.report.split("calibration file not read")[1].splitlines()[0]


# ---------------------------------------------------------------------------- the calibrate verb
def gcode3mf(path: Path, plates=(1,)) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/project_settings.config", "{}")
        z.writestr("Metadata/model_settings.config", "<config/>")
        for n in plates:
            z.writestr(f"Metadata/plate_{n}.gcode", gcode("PETG" if n == 1 else "PLA"))
    return path


def write_log(path: Path, polls: list[dict], tied: bool = True, t=None) -> Path:
    h = header(t) if tied else {"kind": "header", "shambu_version": "test", "track": None}
    path.write_text("".join(json.dumps(r) + "\n" for r in [h, *polls]))
    return path


def test_cli_calibrate_add_then_show(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(conf, "CALIBRATION", tmp_path / "calibration.json")
    log = write_log(tmp_path / "w.jsonl", print_log())
    assert cli.main(["--json", "calibrate", "--log", str(log), "--add"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["added"] == 1 and d["prints"][0]["kind"] == "whole" and d["prints"][0]["log"] == "w.jsonl"
    assert str(tmp_path) not in json.dumps(d["prints"])
    assert cli.main(["--json", "calibrate", "--log", str(log), "--add"]) == 0  # the same log again replaces its print
    capsys.readouterr()
    assert len(conf.load_calibration()) == 1
    assert cli.main(["calibrate", "--show"]) == 0
    text = capsys.readouterr().out
    assert "n=1" in text and "no point estimate" in text


def test_cli_calibrate_two_sessions_give_two_prints(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(conf, "CALIBRATION", tmp_path / "calibration.json")
    log = tmp_path / "w.jsonl"
    lines = [header(track("PETG")), *print_log(), header(track("PLA")), *[{**p, "read_at": at(200 + i)} for i, p in enumerate(print_log())]]
    log.write_text("".join(json.dumps(r) + "\n" for r in lines))
    assert cli.main(["--json", "calibrate", "--log", str(log), "--add"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert [p["material"] for p in d["prints"]] == ["PETG", "PLA"]


def test_cli_calibrate_exit_1_for_an_untied_log_no_running_poll_or_bad_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(conf, "CALIBRATION", tmp_path / "calibration.json")
    untied = write_log(tmp_path / "u.jsonl", print_log(), tied=False)
    idle = write_log(tmp_path / "i.jsonl", [poll(0, "PREPARE", 0, 100)])
    bad = write_log(tmp_path / "b.jsonl", [{**poll(0), "read_at": "bad"}])
    args = ["--json", "calibrate", "--log", str(untied), "--log", str(idle), "--log", str(bad), "--log", str(tmp_path), "--add"]
    assert cli.main(args) == 1
    d = json.loads(capsys.readouterr().out)
    assert len(d["errors"]) == 4 and d["added"] == 0
    assert "not tied" in d["errors"][0] and "no RUNNING poll" in d["errors"][1] and "UTC timestamp" in d["errors"][2]
    assert "cannot read it" in d["errors"][3]
    assert not conf.CALIBRATION.exists()


def test_cli_calibrate_show_on_a_corrupt_file_is_exit_3(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(conf, "CALIBRATION", tmp_path / "calibration.json")
    conf.CALIBRATION.write_text('{"version": 1, "prints": [null]}')
    assert cli.main(["--json", "calibrate", "--show"]) == 3
    assert "ConfError" in json.loads(capsys.readouterr().out)["error"]


def test_cli_calibrate_add_write_failure_is_exit_3(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(conf, "CALIBRATION", tmp_path / "calibration.json")
    log = write_log(tmp_path / "w.jsonl", print_log())

    def boom(*a):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(conf.os, "replace", boom)
    assert cli.main(["--json", "calibrate", "--log", str(log), "--add"]) == 3
    assert "DataFileError" in json.loads(capsys.readouterr().out)["error"]


def test_gcode_track_streams_only_the_chosen_plate(tmp_path, monkeypatch):
    path = gcode3mf(tmp_path / "p.gcode.3mf", plates=(1, 2))
    opened: list[str] = []
    real_open = zipfile.ZipFile.open

    def spy(self, name, *a, **k):
        opened.append(name if isinstance(name, str) else name.filename)
        return real_open(self, name, *a, **k)

    monkeypatch.setattr(zipfile.ZipFile, "open", spy)
    monkeypatch.setattr(zipfile.ZipFile, "read", lambda *a, **k: pytest.fail("a whole entry was read"))
    t, entry = cli._gcode_track(str(path), 2)
    assert entry == "Metadata/plate_2.gcode" and t.material == "PLA" and opened == ["Metadata/plate_2.gcode"]
    with pytest.raises(ProjectError, match="pass --plate"):
        cli._gcode_track(str(path), None)
    assert threemf.plate_gcode_names(str(path)) == ["Metadata/plate_1.gcode", "Metadata/plate_2.gcode"]
