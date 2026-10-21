"""printer.py TLS policy, report merging and watch(). Offline: a fake paho client, no sockets, no real printer."""

import json
import socket
import ssl
import subprocess
import sys
import threading
import time

import pytest

from shambu import conf, printer
from shambu.errors import PrinterError

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
