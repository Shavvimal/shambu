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
