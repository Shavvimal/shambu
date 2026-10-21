"""printer.py decoding with a faked MQTT reader, and conf.py secret handling. No network, no paho."""

import json
import sys

import pytest

from conftest import FakeStudio
from shambu import cli, conf, printer
from shambu.errors import ConfError
from shambu.facade import Bambu

SERIAL = "00X0TEST0000999"
SENTINEL = "SENTINEL-CODE-8812"


def dual_report() -> dict:
    return {
        "print": {
            "gcode_state": "RUNNING",
            "mc_percent": 42,
            "mc_remaining_time": 75,
            "layer_num": 10,
            "total_layer_num": 100,
            "subtask_name": "plate-11-jaws",
            "wifi_signal": "-50dBm",
            "nozzle_temper": 999,
            "device": {
                "extruder": {"state": 0x10, "info": [{"id": 0, "temp": (220 << 16) | 215}, {"id": 1, "temp": (250 << 16) | 248}]},
                "nozzle": {"info": [{"id": 0, "diameter": 0.4, "type": "HS01"}, {"id": 1, "diameter": 0.4, "type": "HH01"}]},
                "bed": {"info": {"temp": (60 << 16) | 59}},
                "ctc": {"info": {"temp": 35}},
            },
            "hms": [{"attr": 0x0C000100, "code": 0x00020004}, {"attr": 0x05008000, "code": 0x00030001}],
        },
        "_tls_verified": True,
    }


# ---------------------------------------------------------------------------- decoding
def test_paho_not_imported_at_collection():
    assert "paho" not in sys.modules


@pytest.mark.parametrize(
    ("v", "out"), [((250 << 16) | 248, (248.0, 250.0)), (35, (35.0, 0.0)), ("12", (12.0, 0.0)), (None, (None, None)), ("x", (None, None))]
)
def test_packed_temp(v, out):
    assert printer.packed_temp(v) == out


def test_decode_hms_code_format_and_level():
    (h,) = printer.decode_hms([{"attr": 0x0C000100, "code": 0x00020004}], {"0C00010000020004": "Nozzle clog"})
    assert h["code"] == "0C00_0100_0002_0004"
    assert h["level"] == "serious"
    assert h["text"] == "Nozzle clog"
    assert h["wiki"].endswith("/0C00010000020004")


def test_decode_hms_unknown_level():
    assert printer.decode_hms([{"attr": 0, "code": 0x00090000}])[0]["level"] == "9"


def test_decoder_dual_nozzles():
    st = printer.StatusDecoder().decode(dual_report(), SERIAL, "10.0.0.9")
    assert [(n["id"], n["name"], n["temp_c"], n["target_c"], n["type"]) for n in st.nozzles] == [
        (1, "main (left)", 248.0, 250.0, "HH01"),
        (0, "auxiliary (right)", 215.0, 220.0, "HS01"),
    ]
    assert st.active_extruder == 1


def test_decoder_bed_chamber_state():
    st = printer.StatusDecoder().decode(dual_report(), SERIAL, "10.0.0.9")
    assert (st.bed_c, st.bed_target_c, st.chamber_c) == (59.0, 60.0, 35.0)
    assert (st.state, st.busy, st.percent, st.layer) == ("printing", True, 42, 10)
    assert st.serial_masked == "00X…999"
    assert [h["level"] for h in st.hms] == ["serious", "common"]


def test_decoder_single_nozzle_fallback():
    st = printer.StatusDecoder().decode({"print": {"gcode_state": "IDLE", "nozzle_temper": 25.5, "bed_temper": 24}}, SERIAL, "ip")
    assert st.nozzles[0]["temp_c"] == 25.5
    assert (st.bed_c, st.state, st.busy, st.ends_at()) == (24.0, "idle", False, None)


def test_status_lines_and_json():
    st = printer.StatusDecoder().decode(dual_report(), SERIAL, "10.0.0.9")
    text = "\n".join(st.lines())
    assert "1 h 15 min left" in text and "nozzle 1 main (left): 248 C -> 250" in text and "HMS 0C00_0100_0002_0004" in text
    d = json.loads(json.dumps(st.to_json()))
    assert "raw" not in d and d["ends_at"]


# ---------------------------------------------------------------------------- conf and secrets
@pytest.fixture
def conf_file(tmp_path, monkeypatch):
    p = tmp_path / "BambuStudio.conf"
    p.write_text(
        json.dumps(
            {
                "app": {"user_last_selected_machine": SERIAL, "curr_bed_type": "4", "single_instance": False},
                "access_code": {SERIAL: SENTINEL},
                "user_access_token": SENTINEL,
                "presets": {"machine": "Bambu Lab X2D 0.4 nozzle", "filaments": ["A", "B"]},
                "recent_projects": {"1": "/tmp/a.3mf"},
            }
        )
    )
    monkeypatch.setattr(conf, "CONF", p)
    monkeypatch.setattr(conf, "APPSUPPORT", tmp_path)  # hms_table() must not read the real install
    return p


def test_summary_hides_access_code(conf_file):
    s = conf.summary()
    assert SENTINEL not in json.dumps(s)
    assert SERIAL not in json.dumps(s)
    assert (s["has_access_code"], s["gui_bed_type"], s["selected_printer_serial"]) == (True, "Textured PEI Plate", "00X…999")


def test_redact_hides_access_code(conf_file):
    r = conf.redact(conf.load())
    assert SENTINEL not in json.dumps(r)
    assert r["access_code"] == "<redacted>"
    assert r["presets"]["machine"] == "Bambu Lab X2D 0.4 nozzle"


def test_access_code_only_through_accessor(conf_file):
    assert conf.access_code() == SENTINEL


def test_access_code_missing_serial_raises(conf_file):
    with pytest.raises(ConfError) as e:
        conf.access_code("XYZ000000999")
    assert "XYZ…999" in str(e.value) and "XYZ000000999" not in str(e.value)


def test_missing_conf_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(conf, "CONF", tmp_path / "none.conf")
    with pytest.raises(ConfError):
        conf.load()


class FakeReader:
    """Stands in for MqttReader: no paho, no socket. Records the code it was given."""

    seen: list = []

    def __init__(self, ip, serial, access_code, wait=8.0, insecure=False):
        FakeReader.seen.append(access_code)
        self.ip, self.serial = ip, serial

    def read(self):
        return dual_report()


def test_printer_service_status_with_fake_reader(conf_file, monkeypatch):
    monkeypatch.setattr(printer, "MqttReader", FakeReader)
    st = printer.PrinterService(ip="10.0.0.9").status()
    assert FakeReader.seen[-1] == SENTINEL
    assert SENTINEL not in json.dumps(st.to_json()) + "\n".join(st.lines()) + repr(st)


def test_mqtt_reader_repr_hides_code():
    assert SENTINEL not in repr(printer.MqttReader("10.0.0.9", SERIAL, SENTINEL))


@pytest.mark.parametrize("as_json", [True, False])
def test_cli_printer_status_never_prints_code(conf_file, monkeypatch, capsys, resolver, as_json):
    monkeypatch.setattr(printer, "MqttReader", FakeReader)
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=FakeStudio(), resolver=resolver))
    code = cli.main((["--json"] if as_json else []) + ["-v", "printer", "status", "--ip", "10.0.0.9"])
    out, err = capsys.readouterr()
    assert code == 0
    assert SENTINEL not in out + err
    if as_json:
        assert json.loads(out)["state"] == "printing"


def test_cli_doctor_never_prints_code(conf_file, monkeypatch, capsys, resolver):
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=FakeStudio(), resolver=resolver))
    assert cli.main(["--json", "doctor"]) == 0
    out, err = capsys.readouterr()
    assert SENTINEL not in out + err
    assert json.loads(out)["config"]["has_access_code"] is True


# ---------------------------------------------------------------------------- time fields, redaction and the watch log (2026-10-21)
SENTINEL_IP = "10.77.66.55"


def timed_report(state="RUNNING", layer=135, remaining=792, **extra) -> dict:
    """The time and job keys of the live X2D report (firmware 20018, 2026-10-21), on the dual-nozzle fixture."""
    r = dual_report()
    r["print"].update(
        gcode_state=state,
        layer_num=layer,
        mc_remaining_time=remaining,
        spd_lvl=2,
        spd_mag=100,
        stg=[29, 2, 13, 74, 11, 73, 75, 4, 8, 14, 54, 1, 3, 255, 51],
        stg_cur=0,
        task_id="123456789",
        **extra,
    )
    return r


def test_status_decodes_the_time_fields():
    st = printer.StatusDecoder(now=lambda: 1792592635.0).decode(timed_report(), SERIAL, SENTINEL_IP)
    assert st.read_at == "2026-10-21T14:23:55Z"
    assert (st.speed_level, st.speed_percent, st.speed_name, st.task_id) == (2, 100, "standard", "123456789")
    assert st.stages == [29, 2, 13, 74, 11, 73, 75, 4, 8, 14, 54, 1, 3, 255, 51]
    assert all(s in printer.STAGE_NAMES for s in st.stages)
    assert st.remaining_min == 792


def test_status_without_the_time_fields_gives_none():
    st = printer.StatusDecoder().decode({"print": {"gcode_state": "IDLE", "stg": "junk"}}, SERIAL, "ip")
    assert (st.speed_level, st.speed_percent, st.stages, st.task_id) == (None, None, None, None)
    assert st.read_at and st.read_at.endswith("Z")


def test_status_json_and_lines_hold_no_ip_and_no_serial():
    st = printer.StatusDecoder().decode(timed_report(), SERIAL, SENTINEL_IP)
    blob = json.dumps(st.to_json()) + "\n".join(st.lines())
    assert SENTINEL_IP not in blob and SERIAL not in blob
    assert st.to_json()["ip"] == "<ip>" and st.to_json()["serial_masked"] == "00X…999"
    rec = json.dumps(st.log_record(), ensure_ascii=False)
    assert SENTINEL_IP not in rec and SERIAL not in rec and "00X…999" not in rec and "dBm" not in rec


class StreamReader:
    """A fake MqttReader that plays one report per read from `StreamReader.script`; an Exception item is raised."""

    script: list = []

    def __init__(self, ip, serial, access_code, wait=8.0, insecure=False):
        pass

    def read(self):
        item = StreamReader.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def stream(conf_file, monkeypatch, resolver):
    monkeypatch.setattr(printer, "MqttReader", StreamReader)
    monkeypatch.setattr(printer.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=FakeStudio(), resolver=resolver))
    return StreamReader


def test_cli_status_json_has_no_ip_and_no_serial(stream, capsys):
    stream.script = [timed_report()]
    assert cli.main(["--json", "printer", "status", "--ip", SENTINEL_IP]) == 0
    out, err = capsys.readouterr()
    assert SENTINEL_IP not in out + err and SERIAL not in out + err and SENTINEL not in out + err
    d = json.loads(out)
    assert (d["speed_level"], d["stages"][0], d["task_id"]) == (2, 29, "123456789") and d["read_at"].endswith("Z")


def test_cli_watch_log_holds_no_identifiers_and_ends_on_the_final_state(stream, capsys, tmp_path):
    log = tmp_path / "watch.jsonl"
    stream.script = [
        timed_report(layer=1, remaining=100),
        printer.PrinterError(f"connect to {SENTINEL_IP} failed"),
        timed_report(layer=2, remaining=50),
        timed_report("FINISH", layer=3, remaining=0),
    ]
    assert cli.main(["--json", "printer", "watch", "--ip", SENTINEL_IP, "--every", "20", "--log", str(log)]) == 0
    out, err = capsys.readouterr()
    text = log.read_text(encoding="utf-8")
    for s in (SENTINEL_IP, SERIAL, SENTINEL, "00X…999", "00X\\u2026999"):
        assert s not in text
    assert SENTINEL_IP not in out + err and SENTINEL not in out + err
    lines = [json.loads(x) for x in text.splitlines()]
    assert [x["kind"] for x in lines] == ["header", "poll", "error", "poll", "poll"]
    assert lines[2]["error"].endswith("connect to <ip> failed") and lines[2]["consecutive"] == 1
    assert lines[-1]["state"] == "FINISH" and lines[-1]["final"] is True and lines[-1]["read_at"].endswith("Z")
    assert lines[1]["speed_level"] == 2 and lines[1]["stages"][-1] == 51 and lines[1]["task_id"] == "123456789"
    docs, dec, i = [], json.JSONDecoder(), 0
    while i < len(out):
        if out[i].isspace():
            i += 1
            continue
        d, i = dec.raw_decode(out, i)
        docs.append(d)
    assert len(docs) == 4 and "error" in docs[1]  # one JSON document per poll, the failed read included


def test_cli_watch_stops_after_max_errors_with_exit_3(stream, caplog, tmp_path):
    log = tmp_path / "watch.jsonl"
    stream.script = [timed_report()] + [printer.PrinterError("no host on this LAN answers") for _ in range(3)]
    assert cli.main(["printer", "watch", "--ip", SENTINEL_IP, "--log", str(log), "--max-errors", "3"]) == 3
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    assert [x["kind"] for x in lines] == ["header", "poll", "error", "error", "error"]
    assert lines[-1]["gave_up"] is True
    assert "read failed" in caplog.text and "3 of 3 in a row" in caplog.text


def test_cli_watch_gcode3mf_puts_the_m73_track_in_the_header(stream, tmp_path):
    from test_calibrate import gcode3mf, track

    log = tmp_path / "watch.jsonl"
    stream.script = [timed_report("FINISH", layer=3, remaining=0)]
    g = gcode3mf(tmp_path / "plate.gcode.3mf")
    assert cli.main(["printer", "watch", "--ip", SENTINEL_IP, "--log", str(log), "--gcode3mf", str(g)]) == 0
    header = json.loads(log.read_text().splitlines()[0])
    assert header["gcode3mf"] == str(g.resolve()) and header["gcode"] == "Metadata/plate_1.gcode"
    assert header["track"] == track().to_json()


def test_cli_watch_gcode3mf_without_gcode_is_exit_1(stream, tmp_path, capsys):
    import zipfile

    g = tmp_path / "unsliced.3mf"
    with zipfile.ZipFile(g, "w") as z:
        z.writestr("Metadata/project_settings.config", "{}")
        z.writestr("Metadata/model_settings.config", "<config/>")
    assert cli.main(["printer", "watch", "--log", str(tmp_path / "w.jsonl"), "--gcode3mf", str(g)]) == 1
    assert "expected the G-code" in capsys.readouterr().out
