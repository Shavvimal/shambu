"""printer.py TLS policy, report merging and watch(). Offline: a fake paho client, no sockets, no real printer."""

import copy
import dataclasses
import json
import socket
import ssl
import subprocess
import sys
import threading
import time
import types

import pytest

from shambu import conf, printer, profiles
from shambu.errors import ConfError, PrinterError
from shambu.vault import MemoryVault, VaultError

SERIAL = "00X0TEST0000999"
SENTINEL = "SENTINEL-CODE-8812"
TOPIC = f"device/{SERIAL}/report"


class Msg:
    def __init__(self, topic, payload):
        self.topic, self.payload = topic, json.dumps(payload).encode()


class FakeClient:
    """Stands in for paho's Client. `script` is the list of (topic, payload) delivered after connect."""

    instances: list = []

    def __init__(self, script=(), rc=0, connect_exc=None):
        self.script, self.rc, self.connect_exc = list(script), rc, connect_exc
        self.connected = self.stopped = self.disconnected = False
        self.ctx = self.insecure = self.password = None
        FakeClient.instances.append(self)

    def username_pw_set(self, user, password):
        self.password = password

    def tls_set_context(self, ctx):
        self.ctx = ctx

    def tls_insecure_set(self, value):
        self.insecure = value

    def connect(self, host, port, keepalive=60):
        if self.connect_exc:
            raise self.connect_exc
        self.connected = True

    def loop_start(self):
        self.on_connect(self, None, {}, self.rc)
        if self.rc == 0:
            for topic, payload in self.script:
                self.on_message(self, None, Msg(topic, payload))

    def loop_stop(self):
        self.stopped = True

    def disconnect(self):
        self.disconnected = True

    def subscribe(self, topic):
        pass

    def publish(self, topic, payload):
        pass


@pytest.fixture
def fake_paho(monkeypatch, tmp_path):
    """Install a FakeClient factory and a CA file that exists (its content is never loaded by the fake)."""
    ca = tmp_path / "printer.cer"
    ca.write_text("")
    monkeypatch.setattr(printer, "CA_FILE", ca)
    monkeypatch.setattr(printer, "tls_context", lambda serial, insecure=False: _ctx(serial, insecure))
    FakeClient.instances.clear()

    def install(**kw):
        monkeypatch.setattr(printer, "_paho_client", lambda cid: FakeClient(**kw))

    return install


def _ctx(serial, insecure):
    ctx = printer.SerialPinnedContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.expected_serial = serial
    if insecure:
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    return ctx


def full(**extra):
    p = {
        "gcode_state": "RUNNING",
        "mc_percent": 10,
        "device": {
            "extruder": {"info": [{"id": 0, "temp": 215}, {"id": 1, "temp": 248}]},
            "nozzle": {"info": [{"id": 1, "type": "HH01"}]},
            "bed": {"info": {"temp": 59}},
        },
    }
    p.update(extra)
    return {"print": p}


def reader(insecure=False, wait=0.3):
    return printer.MqttReader("10.0.0.9", SERIAL, SENTINEL, wait, insecure)


# ---------------------------------------------------------------------------- TLS policy
def test_missing_ca_raises_before_connect(monkeypatch, tmp_path):
    monkeypatch.setattr(printer, "CA_FILE", tmp_path / "missing.cer")
    made = []
    monkeypatch.setattr(printer, "_paho_client", lambda cid: made.append(cid) or FakeClient())
    with pytest.raises(PrinterError) as e:
        reader().read()
    assert str(tmp_path / "missing.cer") in str(e.value)
    assert made == []
    assert SENTINEL not in str(e.value) + repr(e.value)


def test_verified_context_settings(monkeypatch, tmp_path):
    ca = tmp_path / "ok.cer"
    ca.write_text("x")
    monkeypatch.setattr(printer, "CA_FILE", ca)
    monkeypatch.setattr(printer.SerialPinnedContext, "load_verify_locations", lambda self, path: None)
    ctx = printer.tls_context(SERIAL)
    assert (ctx.verify_mode, ctx.check_hostname, ctx.expected_serial) == (ssl.CERT_REQUIRED, True, SERIAL)


def test_insecure_context_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(printer, "CA_FILE", tmp_path / "missing.cer")  # insecure does not need the bundle
    ctx = printer.tls_context(SERIAL, insecure=True)
    assert (ctx.verify_mode, ctx.check_hostname) == (ssl.CERT_NONE, False)


def test_wrap_socket_sends_serial_not_ip():
    seen = {}

    class Probe(printer.SerialPinnedContext):
        pass

    def fake_super_wrap(
        self, sock, server_side=False, do_handshake_on_connect=True, suppress_ragged_eofs=True, server_hostname=None, session=None
    ):
        seen["host"] = server_hostname

    ctx = Probe(ssl.PROTOCOL_TLS_CLIENT)
    ctx.expected_serial = SERIAL
    orig = ssl.SSLContext.wrap_socket
    try:
        ssl.SSLContext.wrap_socket = fake_super_wrap
        ctx.wrap_socket(None, server_hostname="10.0.0.9", do_handshake_on_connect=False)
    finally:
        ssl.SSLContext.wrap_socket = orig
    assert seen["host"] == SERIAL


@pytest.mark.parametrize(
    "cert",
    [{"subject": ((("commonName", SERIAL),),)}, {"subject": ((("commonName", "other"),),), "subjectAltName": (("DNS", SERIAL.lower()),)}],
)
def test_identity_match(cert):
    printer.check_identity(cert, SERIAL)


@pytest.mark.parametrize(
    "cert", [{"subject": ((("commonName", "00X0EVIL0000111"),),)}, {}, {"subject": ((("organizationName", SERIAL),),)}]
)
def test_identity_mismatch_raises(cert):
    with pytest.raises(PrinterError) as e:
        printer.check_identity(cert, SERIAL)
    assert SERIAL not in str(e.value) and conf.mask(SERIAL) in str(e.value)


def test_tls_failure_raises_without_retry(fake_paho):
    fake_paho(connect_exc=ssl.SSLCertVerificationError(f"certificate verify failed: Hostname mismatch, not valid for '{SERIAL}'"))
    with pytest.raises(PrinterError) as e:
        reader().read()
    assert len(FakeClient.instances) == 1  # no unverified second attempt
    assert FakeClient.instances[0].disconnected  # cleaned up in finally
    assert SENTINEL not in str(e.value) + repr(e.value) and SERIAL not in str(e.value)


def test_identity_failure_in_handshake_propagates(fake_paho):
    fake_paho(connect_exc=PrinterError("TLS identity check failed: certificate is not for printer x"))
    with pytest.raises(PrinterError, match="identity"):
        reader().read()
    assert len(FakeClient.instances) == 1


def test_verified_read_reports_verified(fake_paho):
    fake_paho(script=[(TOPIC, full())])
    r = reader().read()
    c = FakeClient.instances[0]
    assert (r["_tls_verified"], r["_topic"], c.insecure, c.ctx.verify_mode) == (True, TOPIC, False, ssl.CERT_REQUIRED)
    assert c.stopped and c.disconnected


def test_insecure_read_reports_unverified(fake_paho):
    fake_paho(script=[(TOPIC, full())])
    r = reader(insecure=True).read()
    assert r["_tls_verified"] is False
    assert FakeClient.instances[0].ctx.verify_mode == ssl.CERT_NONE


def test_insecure_other_topic_ignored_and_raises(fake_paho):
    fake_paho(script=[("device/00X0EVIL0000111/report", full())])
    with pytest.raises(PrinterError, match="no report"):
        reader(insecure=True).read()


def test_refused_login_raises_without_code(fake_paho):
    fake_paho(rc=5)
    with pytest.raises(PrinterError) as e:
        reader().read()
    assert "refused" in str(e.value) and SENTINEL not in str(e.value) + repr(e.value)


# ---------------------------------------------------------------------------- merging
def test_deep_merge_keeps_nozzles_on_bed_delta(fake_paho):
    fake_paho(script=[(TOPIC, full()), (TOPIC, {"print": {"device": {"bed": {"info": {"temp": (60 << 16) | 60}}}}})])
    r = reader().read()
    dev = r["print"]["device"]
    assert dev["extruder"]["info"][1]["temp"] == 248 and dev["nozzle"]["info"][0]["type"] == "HH01"
    assert dev["bed"]["info"]["temp"] == (60 << 16) | 60


def test_deep_merge_replaces_lists():
    d = {"a": {"l": [1, 2, 3], "k": 1}}
    printer.deep_merge(d, {"a": {"l": [9]}})
    assert d == {"a": {"l": [9], "k": 1}}


def test_progress_only_delta_is_not_a_snapshot(fake_paho):
    fake_paho(script=[(TOPIC, {"print": {"mc_percent": 50}})])
    with pytest.raises(PrinterError, match="partial"):
        reader().read()


def test_delta_then_full_report(fake_paho):
    fake_paho(script=[(TOPIC, {"print": {"mc_percent": 50}}), (TOPIC, full(mc_percent=51))])
    r = reader().read()
    assert (r["print"]["gcode_state"], r["print"]["mc_percent"]) == ("RUNNING", 51)


# ---------------------------------------------------------------------------- watch and secrets
def _status(state):
    return printer.StatusDecoder().decode({"print": {"gcode_state": state}}, SERIAL, "10.0.0.9")


def test_watch_continues_on_unknown(monkeypatch, caplog):
    seq = iter(["RUNNING", "UNKNOWN", "WEIRD", "FINISH", "IDLE"])
    svc = printer.PrinterService(ip="10.0.0.9")
    monkeypatch.setattr(svc, "status", lambda: _status(next(seq)))
    monkeypatch.setattr(printer.time, "sleep", lambda s: None)
    states = [st.state for st in svc.watch(every_s=0)]
    assert states == ["printing", "unknown", "weird", "finished"]
    assert "not final" in caplog.text


def test_watch_unknown_stops_at_max_polls(monkeypatch):
    svc = printer.PrinterService(ip="10.0.0.9")
    monkeypatch.setattr(svc, "status", lambda: _status("UNKNOWN"))
    monkeypatch.setattr(printer.time, "sleep", lambda s: None)
    assert len(list(svc.watch(every_s=0, max_polls=4))) == 4


def test_service_passes_insecure_and_hides_code(monkeypatch, tmp_path):
    p = tmp_path / "BambuStudio.conf"
    p.write_text(json.dumps({"app": {"user_last_selected_machine": SERIAL}, "access_code": {SERIAL: SENTINEL}}))
    monkeypatch.setattr(conf, "CONF", p)
    monkeypatch.setattr(conf, "APPSUPPORT", tmp_path)
    got = {}

    class Rec:
        def __init__(self, ip, serial, code, wait, insecure):
            got.update(insecure=insecure)

        def read(self):
            return {**full(), "_tls_verified": False, "_topic": "device/OTHER/report"}

    svc = printer.PrinterService(ip="10.0.0.9", reader_factory=Rec, insecure=True)
    with pytest.raises(PrinterError) as e:
        svc.status()
    assert got["insecure"] is True
    assert SENTINEL not in str(e.value) + repr(e.value) + repr(svc)


def test_reader_repr_and_errors_hide_code(fake_paho):
    fake_paho(connect_exc=OSError(f"boom {SENTINEL}"))
    r = reader()
    with pytest.raises(PrinterError) as e:
        r.read()
    assert SENTINEL not in repr(r) + str(e.value) + repr(e.value)


# ---------------------------------------------------------------------------- discovery and connection bounds
ARP_AN = """? (10.0.0.1) at 0:11:22:33:44:55 on en0 ifscope [ethernet]
? (10.0.0.9) at 0:11:22:33:44:66 on en0 ifscope [ethernet]
? (10.0.0.20) at (incomplete) on en0 ifscope [ethernet]
? (10.0.0.9) at 0:11:22:33:44:66 on en1 ifscope [ethernet]
? (224.0.0.251) at 1:0:5e:0:0:fb on en0 ifscope permanent [ethernet]
? (239.255.255.250) at 1:0:5e:7f:ff:fa on en0 ifscope permanent [ethernet]
? (255.255.255.255) at ff:ff:ff:ff:ff:ff on en0 ifscope [ethernet]
"""


def test_arp_hosts_parses_numeric_output():
    assert printer.arp_hosts(ARP_AN) == ["10.0.0.1", "10.0.0.9"]  # no incomplete, multicast, broadcast or repeat


def test_arp_probe_runs_numeric_arp_with_a_timeout():
    calls = []

    def runner(argv, **kw):
        calls.append((argv, kw.get("timeout")))
        return subprocess.CompletedProcess(argv, 0, ARP_AN, "")

    probe = printer.ArpProbe(runner=runner, probe=lambda ip, port, t: ip == "10.0.0.9")
    assert probe.find() == [{"ip": "10.0.0.9", "method": "arp+8883"}]
    assert calls == [(["arp", "-an"], 2.0)]  # numeric: no reverse DNS per entry


def test_arp_timeout_gives_no_hosts():
    def runner(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    assert printer.ArpProbe(runner=runner).hosts() == []


def test_probe_pool_is_bounded_and_parallel():
    lock, live, peak = threading.Lock(), [0], [0]

    def slow(ip, port, timeout):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        return ip.endswith(".7")

    hosts = [f"10.0.1.{i}" for i in range(48)]
    t0 = time.monotonic()
    hits = printer.ArpProbe(probe=slow, workers=16).probe_hosts(hosts)
    assert [h["ip"] for h in hits] == ["10.0.1.7"]
    assert 1 < peak[0] <= 16  # a pool of 16, never one thread per host, never one host at a time
    assert time.monotonic() - t0 < 48 * 0.05 / 2  # 3 rounds of 16, not 48 in sequence


def test_probe_sweep_ends_at_its_bound():
    def hang(ip, port, timeout):
        time.sleep(1.0)
        return True

    t0 = time.monotonic()
    assert (
        printer.ArpProbe(probe=hang, probe_window_s=0.1, timeout=0.1).probe_hosts(["10.0.0.1", "10.0.0.2"]) == []
    )  # late counts as closed
    assert time.monotonic() - t0 < 0.5


def test_connect_timeout_and_keepalive(fake_paho, monkeypatch):
    seen = {}

    class Rec(FakeClient):
        def connect(self, host, port, keepalive=60):
            seen.update(connect_timeout=getattr(self, "connect_timeout", None), keepalive=keepalive)
            super().connect(host, port, keepalive)

    monkeypatch.setattr(printer, "_paho_client", lambda cid: Rec(script=[(TOPIC, full())]))
    reader().read()
    assert seen == {"connect_timeout": 3.0, "keepalive": 10}


def test_handshake_has_its_own_bound(monkeypatch):
    """A peer that never answers the TLS hello: the handshake gives up after HANDSHAKE_S, not the 10 s keepalive."""
    monkeypatch.setattr(printer, "HANDSHAKE_S", 0.2)
    a, b = socket.socketpair()
    try:
        s = _ctx(SERIAL, True).wrap_socket(a, do_handshake_on_connect=False)
        s.settimeout(10)
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            s.do_handshake()
        assert time.monotonic() - t0 < 2
        assert s.gettimeout() == 10  # the keepalive timeout again for the session
    finally:
        a.close()
        b.close()


def _service_conf(monkeypatch, tmp_path):
    p = tmp_path / "BambuStudio.conf"
    p.write_text(json.dumps({"app": {"user_last_selected_machine": SERIAL}, "access_code": {SERIAL: SENTINEL}}))
    monkeypatch.setattr(conf, "CONF", p)
    monkeypatch.setattr(conf, "APPSUPPORT", tmp_path)


class _Hang:
    def __init__(self, *a):
        pass

    def read(self):
        time.sleep(1.5)
        return full()


def test_read_budget_names_the_step(monkeypatch, tmp_path):
    _service_conf(monkeypatch, tmp_path)
    steps = []
    svc = printer.PrinterService(ip="10.0.0.9", reader_factory=_Hang, timeout_s=0.3, on_step=lambda n, dt: steps.append(n))
    t0 = time.monotonic()
    with pytest.raises(PrinterError) as e:
        svc.status()
    assert str(e.value) == "timed out after 0.3 s in mqtt"
    assert time.monotonic() - t0 < 1
    time.sleep(1.5)
    assert steps == ["credentials"]  # the abandoned read sends no progress line after the caller gave up


def test_read_budget_default_is_15_s():
    assert printer.PrinterService().timeout_s == 15.0 == printer.READ_TIMEOUT_S


def test_read_times_each_discovery_step(monkeypatch, tmp_path):
    _service_conf(monkeypatch, tmp_path)

    class Ok:
        def __init__(self, *a):
            pass

        def read(self):
            return full()

    def runner(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, ARP_AN, "")

    arp = printer.ArpProbe(runner=runner, probe=lambda ip, port, t: ip == "10.0.0.9")
    svc = printer.PrinterService(discovery=(arp,), reader_factory=Ok)
    assert svc.status().ip == "10.0.0.9"
    assert list(svc.timings) == ["credentials", "arp", "probe", "mqtt"]


# ---------------------------------------------------------------------------- review follow-ups (Codex, 2026-10-21)
def test_a_blocked_probe_never_holds_up_the_process_exit():
    """The sweep ends at 0.05 s; a probe still sleeping 3 s must not keep the interpreter alive at exit."""
    code = (
        "import time\n"
        "from shambu import printer\n"
        "t0 = time.monotonic()\n"
        "hits = printer.ArpProbe(probe=lambda ip, port, t: time.sleep(3) or True, probe_window_s=0.05, timeout=0.05).probe_hosts(['10.0.0.1'])\n"
        "assert hits == [] and time.monotonic() - t0 < 0.5\n"
    )
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    assert time.monotonic() - t0 < 2.5  # interpreter start and import included; a joined probe would take 3 s or more


def test_a_probe_that_raises_is_logged_and_counted_closed(caplog):
    def boom(ip, port, timeout):
        if ip.endswith(".1"):
            raise RuntimeError("socket layer broke")
        return ip.endswith(".2")

    with caplog.at_level("WARNING", logger="shambu.printer"):
        hits = printer.ArpProbe(probe=boom).probe_hosts(["10.0.0.1", "10.0.0.2", "10.0.0.3"])
    assert [h["ip"] for h in hits] == ["10.0.0.2"]
    assert any("RuntimeError: socket layer broke" in r.getMessage() for r in caplog.records)
    assert len(caplog.records) == 1  # a closed port (False) is an ordinary negative, not a warning


def test_a_nonzero_arp_exit_is_logged(caplog):
    def runner(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, ARP_AN, "arp: something odd")

    with caplog.at_level("WARNING", logger="shambu.printer"):
        assert printer.ArpProbe(runner=runner).hosts() == ["10.0.0.1", "10.0.0.9"]
    assert any("exited with status 1" in r.getMessage() for r in caplog.records)


def test_an_expired_read_does_not_touch_the_next_one(monkeypatch, tmp_path):
    _service_conf(monkeypatch, tmp_path)
    n = [0]

    class SlowThenFast:
        def __init__(self, *a):
            n[0] += 1
            self.slow = n[0] == 1

        def read(self):
            if self.slow:
                time.sleep(0.6)
            return full()

    events = []
    svc = printer.PrinterService(ip="10.0.0.9", reader_factory=SlowThenFast, timeout_s=0.2, on_step=lambda s, dt: events.append(s))
    with pytest.raises(PrinterError, match="in mqtt"):
        svc.status()
    assert events == ["credentials"]
    events.clear()
    svc.timeout_s = 2.0
    svc.status()  # the second read starts while the first attempt's thread is still asleep
    second = dict(svc.timings)
    time.sleep(0.7)  # the first attempt wakes and finishes its mqtt step now
    assert events == ["credentials", "mqtt"]  # only the second read's own steps
    assert svc.timings == second and second["mqtt"] < 0.5


def test_a_stalled_read_never_holds_up_the_process_exit(tmp_path):
    """The read's worker thread is a daemon: a reader stalled for 3 s must not keep the process alive past its budget."""
    conf_path = tmp_path / "BambuStudio.conf"
    conf_path.write_text(json.dumps({"app": {"user_last_selected_machine": SERIAL}, "access_code": {SERIAL: SENTINEL}}))
    code = (
        "import sys, time, pathlib\n"
        "from shambu import conf, printer\n"
        "from shambu.errors import PrinterError\n"
        "conf.CONF = pathlib.Path(sys.argv[1]); conf.APPSUPPORT = pathlib.Path(sys.argv[2])\n"
        "class Stall:\n"
        "    def __init__(self, *a): pass\n"
        "    def read(self): time.sleep(3); return {}\n"
        "try:\n"
        "    printer.PrinterService(ip='10.0.0.9', reader_factory=Stall, timeout_s=0.1).status()\n"
        "except PrinterError as e:\n"
        "    print(e)\n"
    )
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "-c", code, str(conf_path), str(tmp_path)], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "timed out after 0.1 s in mqtt"
    assert time.monotonic() - t0 < 2.5  # interpreter start and import included; a joined reader would take 3 s or more


# ---------------------------------------------------------------------------- rubric review follow-ups (2026-10-21)
class _Ok:
    thread = None

    def __init__(self, *a):
        pass

    def read(self):
        _Ok.thread = threading.current_thread()
        return full()


def test_a_large_lan_is_probed_to_the_end():
    hosts = [f"10.0.3.{i}" for i in range(1, 100)]  # 99 hosts, the printer last

    def probe(ip, port, timeout):
        time.sleep(0.01)
        return ip == "10.0.3.99"

    a = printer.ArpProbe(probe=probe)
    assert a.probe_hosts(hosts) == [{"ip": "10.0.3.99", "method": "arp+8883"}]
    assert a.shortfall is None


def test_probe_window_scales_with_the_host_count_and_respects_the_limit():
    a = printer.ArpProbe()
    assert a.window(10) == 2.0  # one round of 0.5 s, the 2 s floor
    assert a.window(99) == 2.0  # 64 threads, 2 rounds = 1 s, the 2 s floor
    assert a.window(300) == 2.5  # 5 rounds of 0.5 s
    assert a.window(300, limit=1.0) == 1.0  # never past the read's deadline


def test_an_expired_probe_window_names_the_unprobed_count(monkeypatch, tmp_path, caplog):
    _service_conf(monkeypatch, tmp_path)
    hosts = "".join(f"? (10.0.4.{i}) at 0:1:2:3:4:{i} on en0 ifscope [ethernet]\n" for i in range(1, 11))

    def runner(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, hosts, "")

    def slow(ip, port, timeout):
        time.sleep(0.5)
        return False

    arp = printer.ArpProbe(runner=runner, probe=slow, workers=2, probe_window_s=0.1, timeout=0.05)
    with caplog.at_level("WARNING", logger="shambu.printer"), pytest.raises(PrinterError) as e:
        printer.PrinterService(discovery=(arp,), reader_factory=_Ok, timeout_s=None).status()
    assert "(0 of 10 hosts probed before the 0.25 s probe window ended)" in str(e.value)  # 5 rounds of 0.05 s
    assert any("0 of 10 hosts probed" in r.getMessage() for r in caplog.records)


def test_discovery_goes_through_the_protocol(monkeypatch, tmp_path):
    _service_conf(monkeypatch, tmp_path)
    seen = []

    class FakeDiscovery:
        timings: dict = {}
        shortfall = None

        def with_deadline(self, seconds):
            seen.append(seconds)
            d = FakeDiscovery()
            d.timings = {"fake": 0.1}
            return d

        def find(self):
            return [{"ip": "10.0.0.9", "method": "fake"}]

    svc = printer.PrinterService(discovery=(FakeDiscovery(),), reader_factory=_Ok)
    assert svc.status().ip == "10.0.0.9"
    assert 6.0 < seen[0] <= 15.0 - printer.CONNECT_S - printer.HANDSHAKE_S  # the read's timeout, less connect and handshake
    assert list(svc.timings) == ["credentials", "fake", "mqtt"]
    printer.PrinterService(discovery=(FakeDiscovery(),), reader_factory=_Ok, timeout_s=None).status()
    assert seen[1] is None


def test_a_raising_progress_callback_is_logged_and_the_read_completes(monkeypatch, tmp_path, caplog):
    _service_conf(monkeypatch, tmp_path)

    def bad(name, dt):
        raise ValueError("printer of progress lines broke")

    with caplog.at_level("WARNING", logger="shambu.printer"):
        st = printer.PrinterService(ip="10.0.0.9", reader_factory=_Ok, on_step=bad).status()
    assert st.state == "printing"
    assert any("ValueError: printer of progress lines broke" in r.getMessage() for r in caplog.records)


def test_arp_oserror_gives_no_hosts_and_a_warning(caplog):
    def runner(argv, **kw):
        raise FileNotFoundError("arp")

    with caplog.at_level("WARNING", logger="shambu.printer"):
        assert printer.ArpProbe(runner=runner).hosts() == []
    assert any("arp -an failed" in r.getMessage() for r in caplog.records)


def test_a_read_without_a_timeout_runs_in_the_callers_thread(monkeypatch, tmp_path):
    _service_conf(monkeypatch, tmp_path)
    svc = printer.PrinterService(ip="10.0.0.9", reader_factory=_Ok, timeout_s=None)
    assert svc.status().state == "printing"
    assert _Ok.thread is threading.current_thread()
    assert list(svc.timings) == ["credentials", "mqtt"]


# ---------------------------------------------------------------------------- adaptive polling and failed reads (2026-10-21)
def _timed(state, layer=None, remaining=None):
    p = {"gcode_state": state}
    if layer is not None:
        p["layer_num"] = layer
    if remaining is not None:
        p["mc_remaining_time"] = remaining
    return printer.StatusDecoder().decode({"print": p}, SERIAL, "10.0.0.9")


def _service(items):
    """A PrinterService whose status() plays `items` (an Exception item is raised)."""
    items = list(items)
    svc = printer.PrinterService(ip="10.0.0.9")

    def status():
        x = items.pop(0)
        if isinstance(x, Exception):
            raise x
        return x

    svc.status = status  # type: ignore[method-assign]
    return svc


def test_poll_interval_is_fast_near_the_start_and_the_end():
    every = 300
    assert printer.poll_interval(every, _timed("PREPARE"), None) == 60
    assert printer.poll_interval(every, _timed("RUNNING", 1, 400), 0) == 60
    assert printer.poll_interval(every, _timed("RUNNING", 30, 380), 14 * 60) == 60
    assert printer.poll_interval(every, _timed("RUNNING", 60, 360), 16 * 60) == 300
    assert printer.poll_interval(every, _timed("RUNNING", 500, 300), None) == 300  # the start was not seen: no window
    assert printer.poll_interval(every, _timed("RUNNING", 800, 14), None) == 60
    assert printer.poll_interval(20, _timed("RUNNING", 800, 14), None) == 20  # never slower than --every


def test_watch_sleeps_follow_the_print():
    """Start window from the first RUNNING poll, `every` mid-print, fast again in the last 15 minutes."""
    svc = _service(
        [
            _timed("PREPARE", 0, 400),
            _timed("RUNNING", 0, 400),
            _timed("RUNNING", 40, 300),
            _timed("RUNNING", 900, 10),
            _timed("FINISH", 900, 0),
        ]
    )
    ticks = iter([0.0, 0.0, 1200.0, 5000.0])
    sleeps: list[float] = []
    states = [st.raw_state for st in svc.watch(every_s=300, sleep=sleeps.append, clock=lambda: next(ticks))]
    assert states == ["PREPARE", "RUNNING", "RUNNING", "RUNNING", "FINISH"]
    assert sleeps == [60, 60, 300, 60]


def test_watch_joined_mid_print_has_no_start_window():
    svc = _service([_timed("RUNNING", 500, 300), _timed("FINISH", 900, 0)])
    sleeps: list[float] = []
    list(svc.watch(every_s=300, sleep=sleeps.append, clock=lambda: 0.0))
    assert sleeps == [300]


def test_an_unknown_or_paused_poll_is_not_a_seen_start():
    svc = _service([_timed("UNKNOWN"), _timed("RUNNING", 1, 300), _timed("FINISH", 900, 0)])
    sleeps: list[float] = []
    list(svc.watch(every_s=300, sleep=sleeps.append, clock=lambda: 0.0))
    assert sleeps == [300, 300]  # no start window: the job may have run before the UNKNOWN poll


def test_watch_yields_a_categorised_error_and_goes_on():
    err = PrinterError("cannot connect to 10.0.0.9:8883: printer 00X…999 at [fd00::42] refused")
    svc = _service([_timed("RUNNING", 5, 300), err, _timed("FINISH", 9, 0)])
    got = list(svc.watch(every_s=1, max_errors=2, sleep=lambda s: None, now=lambda: 0.0))
    e = got[1]
    assert isinstance(e, printer.WatchError)
    assert (e.category, e.error, e.read_at, e.gave_up) == ("connect", "the connection to the printer failed", "1970-01-01T00:00:00Z", False)
    assert "00X" not in json.dumps(e.log_poll(), ensure_ascii=False) and "fd00" not in json.dumps(e.log_poll())
    assert got[-1].raw_state == "FINISH"


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("timed out after 15 s in mqtt", "timeout"),
        ("TLS verification failed for printer 00X…999 at 10.0.0.9: x", "tls"),
        ("printer 00X…999 at 10.0.0.9 refused the MQTT login (5): check", "login"),
        ("no host on this LAN answers on port 8883", "discovery"),
        ("host 10.0.0.9 answered for another device, not printer 00X…999", "identity"),
        ("connected to 10.0.0.9 but no report on device/00X…999/report within 8 s", "report"),
        ("something new at printer.local", "other"),
    ],
)
def test_error_categories_have_fixed_messages(text, category):
    cat, msg = printer.error_category(PrinterError(text))
    assert cat == category
    for s in ("10.0.0.9", "00X", "printer.local"):
        assert s not in msg


def test_watch_gives_up_after_max_errors_in_a_row():
    svc = _service([PrinterError("no host on this LAN answers")] * 3)
    got = list(svc.watch(every_s=1, max_errors=3, sleep=lambda s: None))
    assert [g.consecutive for g in got] == [1, 2, 3] and got[-1].gave_up


def test_watch_without_max_errors_still_raises():
    svc = _service([PrinterError("boom")])
    with pytest.raises(PrinterError):
        list(svc.watch(every_s=1, sleep=lambda s: None))


def test_a_failed_read_near_the_end_keeps_the_fast_interval():
    svc = _service([_timed("RUNNING", 900, 10), PrinterError("timed out after 15 s in mqtt"), _timed("FINISH", 900, 0)])
    sleeps: list[float] = []
    list(svc.watch(every_s=300, max_errors=3, sleep=sleeps.append, clock=lambda: 0.0))
    assert sleeps == [60, 60]


def test_no_sleep_after_the_last_permitted_poll():
    sleeps: list[float] = []
    list(_service([_timed("RUNNING", 500, 300)]).watch(every_s=300, max_polls=1, sleep=sleeps.append))
    assert sleeps == []
    list(_service([PrinterError("boom")]).watch(every_s=300, max_polls=1, max_errors=5, sleep=sleeps.append))
    assert sleeps == []


def test_wait_for_start_polls_an_idle_printer_until_a_job_runs():
    svc = _service([_timed("IDLE"), _timed("FINISH"), _timed("PREPARE", 0, 100), _timed("RUNNING", 0, 100), _timed("FINISH", 30, 0)])
    sleeps: list[float] = []
    got = [st.raw_state for st in svc.watch(every_s=300, wait_for_start=True, sleep=sleeps.append, clock=lambda: 0.0)]
    assert got == ["IDLE", "FINISH", "PREPARE", "RUNNING", "FINISH"]
    assert sleeps == [60, 60, 60, 60]
    assert [st.raw_state for st in _service([_timed("IDLE")]).watch(every_s=300, sleep=lambda s: None)] == ["IDLE"]  # default: stop


# ---------------------------------------------------------------------------- 0.5.0: credential chain, staged discovery, typed results
PROFILE = profiles.Profile(
    name="x2d",
    serial=SERIAL,
    ip="10.0.0.9",
    model=None,
    mode="lan",
    code_source="keychain",
    insecure=False,
    added="2026-10-01T10:00:00Z",
    verified="2026-10-20T09:15:00Z",
    revision=3,
)
NEW_IP = "10.0.0.42"


class FakeSource:
    """A `CredentialSource` with fixed answers; `calls` records which methods ran (never the code)."""

    def __init__(self, name, serial=None, code=None):
        self.name, self._serial, self._code, self.calls = name, serial, code, []

    def serial(self):
        self.calls.append("serial")
        return self._serial

    def code(self, serial):
        self.calls.append("code")
        return self._code


class _Pass:
    """A discovery pass with fixed hits; `found` counts its find() calls across copies."""

    def __init__(self, hits, name="fake", found=None):
        self.hits, self.name, self.found = hits, name, found if found is not None else [0]
        self.timings, self.shortfall = {}, None

    def with_deadline(self, seconds):
        return _Pass(self.hits, self.name, self.found)

    def find(self):
        self.found[0] += 1
        self.timings = {self.name: 0.0}
        return list(self.hits)


class _Rec:
    """A reader factory that records (ip, code) per read and plays a report or an error per IP."""

    def __init__(self, by_ip=None, report=None):
        self.by_ip, self.report, self.seen = by_ip or {}, report, []

    def __call__(self, ip, serial, code, wait, insecure):
        rec = self

        class R:
            def read(self):
                rec.seen.append((ip, code))
                x = rec.by_ip.get(ip, rec.report if rec.report is not None else full())
                if isinstance(x, Exception):
                    raise x
                return copy.deepcopy(x)

        return R()


def _profile_svc(vault=None, **kw):
    v = vault if vault is not None else MemoryVault(items={SERIAL: SENTINEL})
    kw.setdefault("sources", (printer.ProfileCredentials(PROFILE, v),))
    kw.setdefault("timeout_s", None)
    return printer.PrinterService(profile=PROFILE, profile_ip=PROFILE.ip, **kw)


def test_chain_order_one_shot_then_keychain_then_studio(monkeypatch, tmp_path):
    _service_conf(monkeypatch, tmp_path)  # Studio holds SENTINEL for SERIAL
    rec = _Rec()
    keychain = printer.ProfileCredentials(PROFILE, MemoryVault(items={SERIAL: "KEYCHAIN-CODE-1"}))
    one_shot = printer.OneShotCredentials("FLAG-CODE-2")
    studio = printer.StudioCredentials()

    def used(*sources):
        res = printer.PrinterService(ip="10.0.0.9", reader_factory=rec, timeout_s=None, sources=sources)._bounded(
            printer.PrinterService(ip="10.0.0.9", reader_factory=rec, timeout_s=None, sources=sources)._resolve_and_read
        )
        return rec.seen[-1][1], res.resolved.code_source, res.resolved.serial_source

    assert used(one_shot, keychain, studio) == ("FLAG-CODE-2", "flag", "profile")  # the one-shot names no serial
    assert used(keychain, studio) == ("KEYCHAIN-CODE-1", "keychain", "profile")
    assert used(printer.ProfileCredentials(PROFILE, MemoryVault()), studio) == (SENTINEL, "studio", "profile")
    assert used(studio) == (SENTINEL, "studio", "studio")
    assert "FLAG-CODE-2" not in repr(one_shot) and "KEYCHAIN-CODE-1" not in repr(keychain)


def test_a_fake_source_satisfies_the_protocol_and_serial_comes_before_code():
    src = FakeSource("fake", serial=SERIAL, code=SENTINEL)
    r = printer.PrinterService(ip="10.0.0.9", sources=(src,), timeout_s=None).resolve()
    assert (r.serial, r.code, r.serial_source, r.code_source, r.ip_source) == (SERIAL, SENTINEL, "fake", "fake", "flag")
    assert src.calls == ["serial", "code"]
    assert SENTINEL not in repr(r)


def test_a_one_shot_code_with_no_serial_takes_the_serial_from_the_certificate():
    seen = []

    def cert(ip, deadline):
        seen.append((ip, deadline))
        return SERIAL

    svc = printer.PrinterService(ip="10.0.0.9", reader_factory=_Rec(), sources=(printer.OneShotCredentials(SENTINEL),), cert_serial=cert)
    res = svc._bounded(svc._resolve_and_read)
    assert (res.resolved.serial, res.resolved.serial_source, res.resolved.code_source) == (SERIAL, "certificate", "flag")
    assert seen[0][0] == "10.0.0.9" and 0 < seen[0][1] <= 15.0
    assert list(svc.timings) == ["cert", "credentials", "mqtt"]


def test_no_serial_anywhere_is_a_setup_error():
    svc = printer.PrinterService(sources=(printer.OneShotCredentials(SENTINEL),), timeout_s=None)
    with pytest.raises(PrinterError) as e:
        svc.status()
    assert str(e.value) == "no printer selected: run shambu printer login, or select it in Bambu Studio (Device tab)"
    assert printer.error_category(e.value)[0] == "setup"
    insecure = printer.PrinterService(ip="10.0.0.9", insecure=True, sources=(printer.OneShotCredentials(SENTINEL),), timeout_s=None)
    with pytest.raises(PrinterError, match="no printer selected"):  # no certificate read under --insecure
        insecure.status()


def test_no_code_anywhere_is_a_conf_error(monkeypatch, tmp_path):
    p = tmp_path / "BambuStudio.conf"
    p.write_text(json.dumps({"app": {"user_last_selected_machine": SERIAL}, "access_code": {}}))
    monkeypatch.setattr(conf, "CONF", p)
    with pytest.raises(ConfError) as e:
        printer.PrinterService(ip="10.0.0.9", timeout_s=None).status()
    assert str(e.value) == (
        f"no access code for serial {conf.mask(SERIAL)}: run shambu printer login, or open the printer once in Bambu Studio's Device tab"
    )
    assert printer.error_category(e.value)[0] == "setup"


def test_no_studio_config_with_an_ip_and_a_one_shot_code_reads():
    """`conf.CONF` is absent (the offline fixture): `status --ip X --serial S --access-code-file F` needs no Studio."""
    rec = _Rec()
    svc = printer.PrinterService(
        ip="10.0.0.9",
        serial=SERIAL,
        reader_factory=rec,
        timeout_s=None,
        sources=(
            printer.OneShotCredentials(SENTINEL),
            printer.StudioCredentials(),
        ),
    )
    assert svc.status().state == "printing"
    assert rec.seen == [("10.0.0.9", SENTINEL)] and list(svc.timings) == ["credentials", "mqtt"]
    assert printer.StudioCredentials().serial() is None and printer.StudioCredentials().code(SERIAL) is None


def test_the_keychain_step_is_timed_only_when_the_profile_source_is_consulted():
    v = MemoryVault(items={SERIAL: SENTINEL})
    svc = _profile_svc(v, ip="10.0.0.9", reader_factory=_Rec())
    svc.status()
    assert list(svc.timings) == ["keychain", "credentials", "mqtt"]
    svc = _profile_svc(
        v, ip="10.0.0.9", reader_factory=_Rec(), sources=(printer.OneShotCredentials(SENTINEL), printer.ProfileCredentials(PROFILE, v))
    )
    svc.status()
    assert list(svc.timings) == ["credentials", "mqtt"]


def test_a_profile_without_a_keychain_code_or_with_another_serial_gives_none():
    v = MemoryVault(items={SERIAL: SENTINEL})
    assert printer.ProfileCredentials(dataclasses.replace(PROFILE, code_source="studio"), v).code(SERIAL) is None
    assert printer.ProfileCredentials(PROFILE, v).code("00X0OTHER000111") is None
    assert v.calls == []


def test_a_keychain_failure_names_the_keychain():
    v = MemoryVault(items={SERIAL: SENTINEL}, error=VaultError("security exited 51"))
    with pytest.raises(PrinterError) as e:
        printer.ProfileCredentials(PROFILE, v).code(SERIAL)
    assert str(e.value) == f"the Keychain could not give the access code for {conf.mask(SERIAL)}: security exited 51"


ROUTE = """   route to: default
destination: default
       mask: default
    gateway: 10.0.0.1
  interface: en0
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
"""
IFCONFIG_24 = """en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 0:11:22:33:44:77
\tinet6 fe80::1%en0 prefixlen 64 secured scopeid 0xb
\tinet 10.0.0.5 netmask 0xffffff00 broadcast 10.0.0.255
\tstatus: active
"""
IFCONFIG_16 = IFCONFIG_24.replace("0xffffff00", "0xffff0000")


def test_subnet_hosts_from_route_and_ifconfig():
    hosts = printer.subnet_hosts(ROUTE, IFCONFIG_24)
    assert len(hosts) == 253 and hosts[:5] == ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.6"]  # not its own
    assert hosts[-1] == "10.0.0.254"
    assert printer.subnet_hosts(ROUTE, IFCONFIG_16) == []  # wider than /24: no scan
    assert printer.subnet_hosts("nothing here", IFCONFIG_24) == []
    assert printer.subnet_hosts(ROUTE, "en0: flags=0<>") == []
    assert len(printer.subnet_hosts(ROUTE, IFCONFIG_24.replace("0xffffff00", "0xfffffff0"))) == 13  # a /28


def _runner(ifconfig=IFCONFIG_24, arp=ARP_AN, calls=None):
    def run(argv, **kw):
        if calls is not None:
            calls.append((argv, kw.get("timeout")))
        out = {"route": ROUTE, "ifconfig": ifconfig, "arp": arp}[argv[0]]
        return subprocess.CompletedProcess(argv, 0, out, "")

    return run


def test_subnet_probe_skips_arp_hosts_and_tags_its_method():
    calls, probed = [], []

    def probe(ip, port, timeout):
        probed.append(ip)
        return ip in ("10.0.0.9", NEW_IP)

    sp = printer.SubnetProbe(runner=_runner(calls=calls), probe=probe)
    assert sp.find() == [{"ip": NEW_IP, "method": "subnet+8883"}]  # 10.0.0.9 is in the ARP table: the ARP pass covers it
    assert "10.0.0.9" not in probed and "10.0.0.1" not in probed and len(probed) == 251
    assert [c[0] for c in calls] == [["route", "-n", "get", "default"], ["ifconfig", "en0"], ["arp", "-an"]]
    assert {c[1] for c in calls} == {2.0}
    assert list(sp.timings) == ["subnet"] and sp.shortfall is None


def test_subnet_probe_wide_mask_or_runner_failure_gives_a_shortfall(caplog):
    wide = printer.SubnetProbe(runner=_runner(ifconfig=IFCONFIG_16), probe=lambda *a: True)
    assert wide.find() == [] and "wider than /24" in wide.shortfall

    def broken(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    with caplog.at_level("WARNING", logger="shambu.printer"):
        b = printer.SubnetProbe(runner=broken, probe=lambda *a: True)
        assert b.find() == [] and "TimeoutExpired" in b.shortfall
    assert any("no subnet scan" in r.getMessage() for r in caplog.records)


def test_accept_skips_a_foreign_hit_and_goes_on_to_the_subnet_pass(monkeypatch, tmp_path):
    _service_conf(monkeypatch, tmp_path)
    probe = lambda ip, port, t: ip in ("10.0.0.9", NEW_IP)  # noqa: E731 - 10.0.0.9 is another 8883 host
    rec = _Rec()
    svc = printer.PrinterService(
        discovery=(printer.ArpProbe(runner=_runner(), probe=probe), printer.SubnetProbe(runner=_runner(), probe=probe)),
        reader_factory=rec,
        accept=lambda h: h["ip"] == NEW_IP,
    )
    res = svc._bounded(svc._resolve_and_read)
    assert (res.resolved.ip, res.resolved.ip_source) == (NEW_IP, "subnet+8883")
    assert rec.seen == [(NEW_IP, SENTINEL)]
    assert list(svc.timings) == ["credentials", "arp", "probe", "subnet", "mqtt"]


def test_an_arp_hit_ends_discovery_before_the_subnet_pass(monkeypatch, tmp_path):
    _service_conf(monkeypatch, tmp_path)
    probe = lambda ip, port, t: ip == "10.0.0.9"  # noqa: E731
    svc = printer.PrinterService(
        discovery=(printer.ArpProbe(runner=_runner(), probe=probe), printer.SubnetProbe(runner=_runner(), probe=probe)),
        reader_factory=_Rec(),
        timeout_s=None,
    )
    assert svc.status().ip == "10.0.0.9"
    assert list(svc.timings) == ["credentials", "arp", "probe", "mqtt"]


def test_no_accepted_hit_keeps_the_no_host_text(monkeypatch, tmp_path):
    _service_conf(monkeypatch, tmp_path)
    svc = printer.PrinterService(discovery=(_Pass([{"ip": "10.0.0.9", "method": "arp+8883"}]),), accept=lambda h: False, timeout_s=None)
    with pytest.raises(PrinterError) as e:
        svc.status()
    assert str(e.value).startswith("no host on this LAN answers on port 8883 (1 host(s) answered but none is printer ")
    assert printer.error_category(e.value)[0] == "discovery"
    plain = printer.PrinterService(discovery=(_Pass([]),), timeout_s=None)
    with pytest.raises(PrinterError) as e:
        plain.status()
    assert str(e.value) == "no host on this LAN answers on port 8883. Is the printer on and on this network? Pass --ip."


# ---------------------------------------------------------------------------- certificate_serial
class _TlsSock:
    def __init__(self, cert):
        self.cert, self.closed = cert, False

    def getpeercert(self):
        return self.cert

    def close(self):
        self.closed = True


@pytest.fixture
def cert_env(monkeypatch, tmp_path):
    """A CA bundle that 'loads', a fake TCP connect and a fake wrap_socket. `env.cert` is what the peer presents,
    `env.wrap_exc` / `env.connect_exc` make that layer fail."""
    ca = tmp_path / "printer.cer"
    ca.write_text("x")
    monkeypatch.setattr(printer, "CA_FILE", ca)
    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", lambda self, *a, **k: None)
    env = types.SimpleNamespace(cert={"subject": ((("commonName", SERIAL),),)}, wrap_exc=None, connect_exc=None, seen={})

    class Sock:
        def settimeout(self, t):
            env.seen["handshake"] = t

        def close(self):
            env.seen["closed"] = True

    def connect(addr, timeout=None):
        env.seen.update(addr=addr, connect=timeout)
        if env.connect_exc:
            raise env.connect_exc
        return Sock()

    def wrap(self, sock, **kw):
        env.seen.update(wrap_kw=kw, check_hostname=self.check_hostname, verify_mode=self.verify_mode)
        if env.wrap_exc:
            raise env.wrap_exc
        return _TlsSock(env.cert)

    monkeypatch.setattr(printer.socket, "create_connection", connect)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", wrap)
    return env


def test_certificate_serial_reads_the_cn_without_sni(cert_env):
    assert printer.certificate_serial("10.0.0.9", deadline=1.5) == SERIAL
    s = cert_env.seen
    assert s["addr"] == ("10.0.0.9", 8883) and s["connect"] == 1.5 and s["handshake"] == 1.5
    assert s["wrap_kw"] == {"server_hostname": None}  # no SNI
    assert (s["check_hostname"], s["verify_mode"]) == (False, ssl.CERT_REQUIRED) and s["closed"]
    printer.certificate_serial("10.0.0.9")
    assert (cert_env.seen["connect"], cert_env.seen["handshake"]) == (printer.CONNECT_S, printer.HANDSHAKE_S)


def test_certificate_serial_refuses_a_cn_that_is_not_a_serial(cert_env):
    cert_env.cert = {"subject": ((("commonName", "not a printer"),),)}  # "nonsense" is 8 letters: serial-shaped
    with pytest.raises(PrinterError, match=r"^the certificate at 10\.0\.0\.9 names no printer$"):
        printer.certificate_serial("10.0.0.9")
    cert_env.cert = {}
    with pytest.raises(PrinterError, match="names no printer"):
        printer.certificate_serial("10.0.0.9")


def test_certificate_serial_error_texts(cert_env, monkeypatch, tmp_path):
    cert_env.wrap_exc = ssl.SSLCertVerificationError("certificate verify failed")
    with pytest.raises(PrinterError, match=r"^TLS verification failed for the host at 10\.0\.0\.9: "):
        printer.certificate_serial("10.0.0.9")
    cert_env.wrap_exc, cert_env.connect_exc = None, ConnectionRefusedError("refused")
    with pytest.raises(PrinterError, match=r"^cannot connect to 10\.0\.0\.9:8883: refused"):
        printer.certificate_serial("10.0.0.9")
    monkeypatch.setattr(printer, "CA_FILE", tmp_path / "missing.cer")
    with pytest.raises(PrinterError, match="CA bundle not found"):
        printer.certificate_serial("10.0.0.9")


# ---------------------------------------------------------------------------- refused login
def test_a_refused_login_names_the_source_and_the_hint(fake_paho):
    fake_paho(rc=5)
    svc = printer.PrinterService(ip="10.0.0.9", serial=SERIAL, sources=(printer.OneShotCredentials(SENTINEL),), timeout_s=None)
    with pytest.raises(printer.LoginRefused) as e:
        svc.status()
    text = str(e.value)
    assert "refused the MQTT login" in text and "(source: a one-shot flag)\n" in text
    assert "shambu printer login --ip 10.0.0.9" in text and "--from-studio --printer --ip" not in text
    assert "In LAN-only mode:" in text and "In cloud mode:" in text and "can regenerate it" in text
    assert SENTINEL not in text + repr(e.value)
    assert printer.error_category(e.value) == (
        "login",
        "the printer refused the access code (run shambu printer login, or check the code in Studio)",
    )


@pytest.mark.parametrize(
    ("mode", "want", "absent"), [("lan", "--printer x2d", "--from-studio"), ("cloud", "--from-studio --printer x2d", "touchscreen")]
)
def test_a_refused_profile_login_gives_the_mode_hint(fake_paho, mode, want, absent):
    fake_paho(rc=5)
    prof = dataclasses.replace(PROFILE, mode=mode)
    svc = printer.PrinterService(
        ip="10.0.0.9", profile=prof, sources=(printer.ProfileCredentials(prof, MemoryVault(items={SERIAL: SENTINEL})),), timeout_s=None
    )
    with pytest.raises(printer.LoginRefused) as e:
        svc.status()
    text = str(e.value)
    assert "(source: the Keychain, profile x2d)\n" in text and want in text and absent not in text


def test_error_category_messages_are_pinned():
    msgs = {cat: msg for cat, _, msg in printer.ERROR_CATEGORIES}
    assert msgs["login"] == "the printer refused the access code (run shambu printer login, or check the code in Studio)"
    assert msgs["setup"] == "shambu is not set up for the printer (shambu printer login, or Bambu Studio's Device tab)"
    assert printer.error_category(ConfError("x")) == ("setup", msgs["setup"])
    assert [c for c, _, _ in printer.ERROR_CATEGORIES] == ["timeout", "tls", "login", "discovery", "identity", "report", "connect", "setup"]


# ---------------------------------------------------------------------------- typed read result and after_read
def test_after_read_runs_on_the_callers_thread_for_every_read(monkeypatch, tmp_path):
    _service_conf(monkeypatch, tmp_path)
    got = []
    svc = printer.PrinterService(ip="10.0.0.9", reader_factory=_Rec(), after_read=lambda r: got.append((threading.get_ident(), r)))
    svc.status()
    svc.filaments()
    svc.raw_report()
    svc.public_report()
    polls = list(svc.watch(every_s=0, max_polls=1, sleep=lambda s: None))
    assert len(polls) == 1
    assert len(got) == 5
    assert {t for t, _ in got} == {threading.get_ident()}  # the worker thread is another one: timeout_s is 15 s
    assert all(isinstance(r, printer.ReadResult) and r.relocation is None and r.resolved.ip == "10.0.0.9" for _, r in got)


def test_an_abandoned_read_never_calls_after_read(monkeypatch, tmp_path):
    _service_conf(monkeypatch, tmp_path)
    got = []
    svc = printer.PrinterService(ip="10.0.0.9", reader_factory=_Hang, timeout_s=0.3, after_read=got.append)
    with pytest.raises(PrinterError, match=r"timed out after 0\.3 s in mqtt"):
        svc.status()
    time.sleep(1.5)  # the worker finishes its read now
    assert got == []


@pytest.mark.parametrize("insecure", [False, True])
def test_a_forged_relocation_key_in_the_report_has_no_effect(insecure):
    forged = {**full(), "_relocated_ip": "10.0.0.66", "relocation": {"new_ip": "10.0.0.66"}, "_topic": TOPIC, "_tls_verified": True}
    got = []
    svc = _profile_svc(reader_factory=_Rec(report=forged), insecure=insecure, after_read=got.append)
    st = svc.status()
    assert st.ip == "10.0.0.9" and got[0].relocation is None and got[0].resolved.ip_source == "profile"


# ---------------------------------------------------------------------------- relocation
def _moved(**kw):
    """A profile service whose printer moved from the profile IP to NEW_IP; the certificate check accepts NEW_IP."""
    rec = _Rec(by_ip={"10.0.0.9": PrinterError("cannot connect to 10.0.0.9:8883: [Errno 65] No route to host")})
    found = [0]
    certs = {NEW_IP: SERIAL, "10.0.0.50": "00X0OTHER000111"}
    hits = kw.pop("hits", [{"ip": "10.0.0.50", "method": "arp+8883"}, {"ip": NEW_IP, "method": "subnet+8883"}])
    svc = _profile_svc(
        reader_factory=rec,
        discovery=(_Pass(hits, found=found),),
        accept=lambda h: certs.get(h["ip"], "").casefold() == SERIAL.casefold(),
        **kw,
    )
    return svc, rec, found


def test_a_moved_printer_is_relocated_after_an_identity_check():
    got = []
    svc, rec, found = _moved(after_read=got.append)
    st = svc.status()
    assert st.ip == NEW_IP and rec.seen == [("10.0.0.9", SENTINEL), (NEW_IP, SENTINEL)]
    r = got[0]
    assert r.relocation == printer.Relocation("x2d", SERIAL, "10.0.0.9", NEW_IP, 3)
    assert (r.resolved.ip, r.resolved.ip_source) == (NEW_IP, "subnet+8883") and found == [1]
    assert list(svc.timings) == ["keychain", "credentials", "mqtt", "fake", "relocate"]


def test_a_committed_relocation_makes_the_next_poll_use_the_new_ip():
    def commit(r):
        if r.relocation is not None:
            svc.profile_ip = r.relocation.new_ip

    svc, rec, found = _moved(after_read=commit)
    svc.timeout_s = 5.0
    polls = list(svc.watch(every_s=0, max_polls=2, until_idle=False, sleep=lambda s: None))
    assert [p.ip for p in polls] == [NEW_IP, NEW_IP]
    assert found == [1]  # no rediscovery on the second poll
    assert rec.seen[-1] == (NEW_IP, SENTINEL)


def test_no_matching_host_gives_the_original_error_and_the_profile_address():
    svc, rec, _ = _moved(hits=[{"ip": "10.0.0.50", "method": "arp+8883"}])
    with pytest.raises(PrinterError) as e:
        svc.status()
    assert str(e.value) == (
        "cannot connect to 10.0.0.9:8883: [Errno 65] No route to host The profile x2d expected 10.0.0.9 (last verified 09:15)."
    )
    assert rec.seen == [("10.0.0.9", SENTINEL)]  # no credential to the foreign host


def test_a_failed_second_read_gives_the_original_error():
    svc, rec, _ = _moved()
    rec.by_ip[NEW_IP] = PrinterError("connected to 10.0.0.42 but no report on device/x/report within 8 s")
    with pytest.raises(
        PrinterError, match=r"^cannot connect to 10\.0\.0\.9:8883: .* The profile x2d expected 10\.0\.0\.9 \(last verified 09:15\)\.$"
    ):
        svc.status()


def test_relocation_is_skipped_under_insecure_and_off_the_profile_path():
    svc, _rec, found = _moved(insecure=True)
    with pytest.raises(PrinterError) as e:
        svc.status()
    assert str(e.value).startswith("cannot connect") and "The profile" not in str(e.value) and found == [0]
    svc, _rec, found = _moved(ip="10.0.0.9")  # an explicit --ip is the caller's: never relocated
    with pytest.raises(PrinterError, match=r"^cannot connect"):
        svc.status()
    assert found == [0]


def test_prepare_secrets_registers_a_keychain_code_without_network():
    conf.forget_secrets()
    v = MemoryVault(items={SERIAL: SENTINEL})

    def no_network(*a):
        raise AssertionError("prepare_secrets must not read the printer")

    svc = _profile_svc(v, reader_factory=no_network, cert_serial=no_network, discovery=())
    svc.prepare_secrets()
    assert SENTINEL in conf.sensitive_values() and v.calls == [("get", SERIAL)]
    conf.forget_secrets()
    printer.PrinterService(sources=(printer.ProfileCredentials(PROFILE, MemoryVault()),), cert_serial=no_network).prepare_secrets()
    printer.PrinterService(sources=(), cert_serial=no_network).prepare_secrets()  # no serial: nothing, no error
    assert SENTINEL not in conf.sensitive_values()
    with pytest.raises(PrinterError, match="Keychain"):
        _profile_svc(MemoryVault(error=VaultError("locked"))).prepare_secrets()
