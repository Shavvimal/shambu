"""Read-only view of a Bambu Lab printer over LAN MQTT.

The printer runs an MQTT broker on TCP 8883 (TLS, user `bblp`, password = LAN access code). We
subscribe to `device/<serial>/report`, publish exactly one `pushall`, and read the report. Status
pushes are exempt from Bambu's Authorization Control, so this works without Developer Mode.
Nothing here starts, pauses, stops or changes a print, and nothing but `pushall` is published.

Design: `Discovery` strategies find the IP (ARP + port probe, passive SSDP). `MqttReader` adapts
paho-mqtt to one call. `StatusDecoder` turns the raw report into a `Status`. `PrinterService`
is the facade the CLI uses.

Trust: discovery only proposes an IP (the first host that answers on 8883). TLS fails closed. By
default Studio's CA bundle must exist, the chain must verify, and the certificate must name the
selected serial (CN = serial, sent as SNI). OpenSSL checks the name in the handshake and
`check_identity` checks it again, both before the access code is sent. There is no retry and no
downgrade. `insecure=True` (CLI `--insecure`) skips all of that and marks the report
`_tls_verified: False`. In that mode the only check is that the report arrives on
`device/<serial>/report`. It catches the wrong printer at an IP, not an attacker. Reports merge
deeply, and a report counts as complete only when it carries `print.gcode_state`.

Dual-nozzle facts (X2D; pybambu and Studio's DevExtruderSystem.cpp): `print.device.extruder.info[]`
has one entry per extruder, id 0 = auxiliary (right), id 1 = main (left). `temp` packs the current
value (low 16 bits) and the target (high 16 bits). The top-level `nozzle_temper` mirrors one nozzle.

Loaded filament (the same `pushall`; live X2D capture 2026-10-16, tests/fixtures/x2d_pushall_two_external_spools.json):
the two external spools are `print.vir_slot[]` with ids "254" (feeds the main/left extruder, id 1) and "255"
(feeds the auxiliary/right extruder, id 0); there is no `vt_tray` on this firmware. AMS slots are
`print.ams.ams[].tray[]`. Each tray carries `tray_type` (material), `tray_color` (RRGGBBAA), `tray_info_idx`
(Bambu's filament id, e.g. GFG99 = Generic PETG, GFG00 = Bambu PETG Basic; the installed filament presets carry the
same value as `filament_id`), `tray_sub_brands`, `remain` and `k`. Each extruder's `snow` ("slot now") packs the tray
it holds: (ams id << 8) | slot, 0xFFFF for none; the capture showed left snow 0xFE00 (= 254) and right 0xFF00 (= 255).
"""

from __future__ import annotations

import copy
import json
import logging
import re
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypeVar

from . import conf
from .errors import PrinterError

log = logging.getLogger(__name__)
T = TypeVar("T")

CA_FILE = Path("/Applications/BambuStudio.app/Contents/Resources/cert/printer.cer")
PUSHALL = {"pushing": {"sequence_id": "1", "command": "pushall", "version": 1, "push_target": 1}}
STATES = {
    "IDLE": "idle",
    "PREPARE": "preparing",
    "RUNNING": "printing",
    "PAUSE": "paused",
    "FINISH": "finished",
    "FAILED": "failed",
    "SLICING": "slicing",
    "INIT": "initialising",
    "OFFLINE": "offline",
    "UNKNOWN": "unknown",
}
ACTIVE_STATES = ("printing", "paused", "preparing", "slicing")
DONE_STATES = ("idle", "finished", "failed")  # watch() stops only on these; unknown or offline keeps it polling
EXTRUDER_NAMES = {0: "auxiliary (right)", 1: "main (left)"}
HMS_LEVELS = {1: "fatal", 2: "serious", 3: "common", 4: "info"}
# Network bounds for one read (doctor timings, 2026-10-21): TCP connect, TLS handshake, MQTT keepalive, and the whole
# read (discovery + connect + report) unless the caller gives another budget.
CONNECT_S = 3.0
HANDSHAKE_S = 5.0
KEEPALIVE_S = 10
BUDGET_S = 15.0
TRAY_NONE = 0xFFFF  # `snow` / `tray_now` value for "nothing loaded"
# External spools on a dual-nozzle printer: virtual tray id -> the extruder id it feeds (live X2D, 2026-10-16; each
# extruder's `snow` confirmed it). Single-nozzle printers report one external spool as `vt_tray` with tray_now 254.
EXTERNAL_TRAY_EXTRUDER = {254: 1, 255: 0}
EXTERNAL_TRAY_LABELS = {254: "left external spool", 255: "right external spool"}
# The spec's nozzle numbers (filament_map values): 1 = left/main (extruder id 1), 2 = right/auxiliary (extruder id 0).
NOZZLE_OF_EXTRUDER = {1: 1, 0: 2}
COLOUR_NAMES = {
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "grey": (128, 128, 128),
    "red": (220, 30, 30),
    "orange": (255, 128, 0),
    "yellow": (255, 230, 0),
    "green": (0, 174, 66),
    "cyan": (0, 200, 220),
    "blue": (30, 70, 220),
    "purple": (128, 0, 160),
    "pink": (255, 150, 190),
    "brown": (120, 70, 20),
}


# ---------------------------------------------------------------------------------- discovery
class Discovery(Protocol):
    def find(self) -> list[dict]: ...


def port_open(ip: str, port: int = 8883, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


_ARP_IP = re.compile(r"\((\d+\.\d+\.\d+\.\d+)\)")


def arp_hosts(text: str) -> list[str]:
    """IPv4 hosts from `arp -an` output, in table order, once each. Skips multicast and broadcast addresses and
    `(incomplete)` entries (no host answered ARP there, so nothing answers on 8883 either)."""
    out: list[str] = []
    for line in text.splitlines():
        m = _ARP_IP.search(line)
        if not m or "incomplete" in line:
            continue
        ip = m.group(1)
        if ip.startswith(("224.", "239.", "255.")) or ip in out:
            continue
        out.append(ip)
    return out


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass
class ArpProbe:
    """Hosts in the ARP table that answer on 8883. On a home LAN only Bambu printers do.

    Bounds (a slow resolver or a large LAN must not stall a check): `arp -an` (numeric, so no reverse DNS per entry)
    with a 2 s timeout, then the 8883 probe in a pool of `workers` threads, `timeout` s per host and `sweep_s` for the
    whole probe. A host that has not answered when the sweep ends counts as closed. The probe threads are daemons: a
    probe still blocked when the sweep ends never holds up the process exit."""

    timeout: float = 0.5
    sweep_s: float = 2.0
    workers: int = 16
    arp_timeout: float = 2.0
    runner: Runner = field(default=subprocess.run, repr=False)  # injectable: tests pass a fake
    probe: Callable[[str, int, float], bool] = field(default=port_open, repr=False)

    def hosts(self) -> list[str]:
        try:
            r = self.runner(["arp", "-an"], capture_output=True, text=True, timeout=self.arp_timeout)
        except subprocess.TimeoutExpired:
            log.warning("arp -an gave no answer within %g s; no ARP discovery", self.arp_timeout)
            return []
        except OSError as e:
            log.warning("arp -an failed (%s); no ARP discovery", e)
            return []
        if r.returncode != 0:
            log.warning("arp -an exited with status %s (%s); reading what it printed", r.returncode, (r.stderr or "").strip()[:200])
        return arp_hosts(r.stdout or "")

    def probe_hosts(self, hosts: list[str]) -> list[dict]:
        if not hosts:
            return []
        todo = list(hosts)
        open_: set[str] = set()
        lock = threading.Lock()
        left = [len(hosts)]
        finished = threading.Event()
        deadline = time.monotonic() + self.sweep_s

        def worker() -> None:
            while True:
                with lock:
                    if not todo or time.monotonic() >= deadline:
                        return
                    h = todo.pop(0)
                try:
                    ok = bool(self.probe(h, 8883, self.timeout))
                except Exception as e:  # noqa: BLE001 - a closed port is False; anything else is worth a warning
                    log.warning("port probe of %s raised %s: %s; counted as closed", h, type(e).__name__, e)
                    ok = False
                with lock:
                    if ok:
                        open_.add(h)
                    left[0] -= 1
                    if left[0] == 0:
                        finished.set()

        for i in range(max(1, min(self.workers, len(hosts)))):
            threading.Thread(target=worker, name=f"shambu-probe-{i}", daemon=True).start()
        finished.wait(self.sweep_s)
        with lock:
            found = set(open_)
        return [{"ip": h, "method": "arp+8883"} for h in hosts if h in found]  # ARP order, not finish order

    def find(self) -> list[dict]:
        return self.probe_hosts(self.hosts())


@dataclass
class SsdpListen:
    """Passive listen for the printer's SSDP NOTIFY on UDP 2021 and 1990. SO_REUSEPORT is set
    because the Studio GUI holds 2021 while it runs."""

    seconds: float = 10.0

    def find(self) -> list[dict]:
        found: dict[str, dict] = {}
        socks = []
        for port in (2021, 1990):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass
            try:
                s.bind(("", port))
            except OSError:
                s.close()
                continue
            if port == 1990:
                try:
                    s.setsockopt(
                        socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton("239.255.255.250") + socket.inet_aton("0.0.0.0")
                    )
                except OSError:
                    pass
            s.settimeout(0.5)
            socks.append(s)
        t0 = time.time()
        while time.time() - t0 < self.seconds and socks:
            for s in socks:
                try:
                    data, addr = s.recvfrom(4096)
                except (TimeoutError, OSError):
                    continue
                hdr = dict(re.findall(r"^([A-Za-z.-]+):\s*(.*?)\r?$", data.decode(errors="replace"), re.M))
                if "USN" in hdr:
                    found[hdr["USN"]] = {
                        "ip": hdr.get("Location", addr[0]),
                        "serial": hdr["USN"],
                        "model": hdr.get("DevModel.bambu.com"),
                        "name": hdr.get("DevName.bambu.com"),
                        "connect": hdr.get("DevConnect.bambu.com"),
                        "version": hdr.get("DevVersion.bambu.com"),
                        "method": "ssdp",
                    }
        for s in socks:
            s.close()
        return list(found.values())


# ---------------------------------------------------------------------------------- transport
def cert_names(peercert: dict) -> list[str]:
    """Subject CNs and DNS SANs of a `getpeercert()` dict."""
    names = [v for rdn in peercert.get("subject", ()) for k, v in rdn if k == "commonName"]
    return names + [v for k, v in peercert.get("subjectAltName", ()) if k == "DNS"]


def check_identity(peercert: dict, serial: str) -> None:
    """Raise `PrinterError` unless the certificate names the printer: CN or a DNS SAN equals `serial`. Case-insensitive,
    as OpenSSL's host check is. An empty dict (what `getpeercert()` returns for an unverified chain) never passes."""
    names = cert_names(peercert or {})
    if serial.casefold() not in {n.casefold() for n in names}:
        shown = ", ".join(conf.mask(n) for n in names) or "none"
        raise PrinterError(f"TLS identity check failed: certificate is not for printer {conf.mask(serial)} (names: {shown})")


class PinnedSSLSocket(ssl.SSLSocket):
    """Runs `check_identity` right after the handshake, so it runs before paho sends CONNECT with the access code."""

    def do_handshake(self, block: bool = False) -> None:
        # paho sets the socket timeout to its keepalive (10 s) before the handshake and cannot change keepalive on an
        # open connection, so the handshake bound lives here: HANDSHAKE_S, then the keepalive timeout again.
        prev = self.gettimeout()
        self.settimeout(HANDSHAKE_S if prev is None else min(prev, HANDSHAKE_S))
        try:
            super().do_handshake(block)
        finally:
            self.settimeout(prev)
        serial = getattr(self.context, "expected_serial", "")
        if self.context.verify_mode != ssl.CERT_NONE:
            try:
                check_identity(self.getpeercert() or {}, serial)
            except PrinterError:
                self.close()
                raise


class SerialPinnedContext(ssl.SSLContext):
    """TLS context that sends the printer serial as SNI and as the name to verify, whatever host paho connects to.

    paho 2.x calls `context.wrap_socket(sock, server_hostname=<ip>)`. This override swaps the IP for the serial, so with
    `check_hostname=True` OpenSSL checks the chain and the name (CN = serial) inside the handshake. `PinnedSSLSocket` then
    repeats the name check explicitly. Both run before the MQTT CONNECT packet, so the access code never reaches a broker
    that failed either check. paho skips its own `ssl.match_hostname` (gone in 3.12) when `check_hostname` is on."""

    expected_serial: str = ""
    sslsocket_class = PinnedSSLSocket

    def wrap_socket(
        self,
        sock: socket.socket,
        server_side: bool = False,
        do_handshake_on_connect: bool = True,
        suppress_ragged_eofs: bool = True,
        server_hostname: str | bytes | None = None,
        session: ssl.SSLSession | None = None,
    ) -> ssl.SSLSocket:
        return super().wrap_socket(
            sock,
            server_side,
            do_handshake_on_connect,
            suppress_ragged_eofs,
            server_hostname=self.expected_serial or server_hostname,
            session=session,
        )


def tls_context(serial: str, insecure: bool = False) -> SerialPinnedContext:
    """Verified context by default: Studio's CA bundle, chain and name (= serial) checked. A missing CA bundle raises; it
    never falls back to an unverified context. `insecure=True` gives CERT_NONE and no name check, on request only."""
    ctx = SerialPinnedContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.expected_serial = serial
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if not CA_FILE.is_file():
        raise PrinterError(f"printer CA bundle not found at {CA_FILE} (install Bambu Studio), or pass --insecure to skip TLS verification")
    try:
        ctx.load_verify_locations(str(CA_FILE))
    except (ssl.SSLError, OSError) as e:
        raise PrinterError(f"cannot load printer CA bundle {CA_FILE}: {e}") from e
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    ctx.hostname_checks_common_name = True  # Bambu printer certificates carry the serial as CN
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT  # Python 3.13: Bambu's CA fails the strict profile
    return ctx


def deep_merge(dst: dict, src: dict) -> dict:
    """Merge a report delta into `dst` in place: nested dicts merge key by key, lists and scalars replace."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)
    return dst


def is_snapshot(report: dict) -> bool:
    """A full `pushall` report carries `print.gcode_state`. A delta (progress, one temperature) usually does not."""
    p = report.get("print")
    return isinstance(p, dict) and "gcode_state" in p


def _paho_client(client_id: str):
    """The real paho client. Tests replace this function with a fake; nothing else in this module imports paho."""
    try:
        import paho.mqtt.client as mqtt
    except ImportError as e:
        raise PrinterError(
            "paho-mqtt missing: reinstall shambu (uv tool install --reinstall 'shambu @ git+https://github.com/Shavvimal/shambu')"
        ) from e
    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)


@dataclass
class _Collector:
    """Per-attempt state, written by paho's network thread and read by the caller under one lock."""

    topic: str
    report: dict = field(default_factory=dict)
    first_topic: str | None = None
    messages: int = 0
    refused: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, topic: str, payload: bytes) -> None:
        if topic != self.topic:
            return
        try:
            d = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(d, dict):
            return
        with self.lock:
            self.first_topic = self.first_topic or topic
            self.messages += 1
            deep_merge(self.report, d)

    def complete(self) -> bool:
        with self.lock:
            return is_snapshot(self.report)

    def snapshot(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.report)


@dataclass
class MqttReader:
    """Adapter over paho-mqtt: connect, subscribe, one pushall, collect, disconnect. One attempt, no TLS fallback.

    Default (`insecure=False`): the CA bundle must exist, and the chain and the name (= serial) are checked in the TLS
    handshake, before the access code is sent. Any TLS or identity failure raises `PrinterError`. With `insecure=True`
    the TLS layer checks nothing and the report says `_tls_verified: False`."""

    ip: str
    serial: str
    access_code: str = field(repr=False)
    wait: float = 8.0
    insecure: bool = False

    def read(self) -> dict:
        ctx = tls_context(self.serial, self.insecure)  # raises before any socket opens if the CA bundle is missing
        topic = f"device/{self.serial}/report"
        col = _Collector(topic)

        def on_connect(c, u, flags, rc, *a):
            ok = (rc == 0) if isinstance(rc, int) else (getattr(rc, "value", 1) == 0)
            if not ok:
                with col.lock:
                    col.refused = str(rc)
                return
            c.subscribe(topic)
            c.publish(f"device/{self.serial}/request", json.dumps(PUSHALL))

        def on_message(c, u, m):
            col.add(m.topic, m.payload)

        c = _paho_client(f"shambu-{int(time.time() * 1000) % 100000}")
        started = False
        try:
            c.username_pw_set("bblp", self.access_code)
            c.tls_set_context(ctx)
            # insecure: tell paho not to call ssl.match_hostname (gone in 3.12). Verified: keeps check_hostname=True.
            c.tls_insecure_set(self.insecure)
            c.on_connect, c.on_message = on_connect, on_message
            c.connect_timeout = CONNECT_S  # paho's default is 5 s; an unroutable --ip then fails in 3 s
            try:
                c.connect(self.ip, 8883, keepalive=KEEPALIVE_S)
            except ssl.SSLError as e:
                raise PrinterError(f"TLS verification failed for printer {conf.mask(self.serial)} at {self.ip}: {self._clean(e)}") from None
            except OSError as e:
                raise PrinterError(f"cannot connect to {self.ip}:8883: {self._clean(e)}") from None
            c.loop_start()
            started = True
            t0 = time.time()
            while time.time() - t0 < self.wait and col.refused is None and not col.complete():
                time.sleep(0.1)
            if col.complete():  # short grace period: deltas right after the full report merge in
                time.sleep(min(1.0, max(0.0, self.wait - (time.time() - t0))))
        finally:
            if started:
                try:
                    c.loop_stop()
                except Exception:  # noqa: BLE001
                    pass
            try:
                c.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if col.refused is not None:
            raise PrinterError(
                f"printer {conf.mask(self.serial)} at {self.ip} refused the MQTT login ({col.refused}): check the LAN access code in Studio"
            )
        report = col.snapshot()
        if not is_snapshot(report):
            if col.messages:
                raise PrinterError(
                    f"printer {conf.mask(self.serial)} at {self.ip} sent {col.messages} partial update(s) but no full report within {self.wait} s"
                )
            raise PrinterError(
                f"connected to {self.ip} but no report on device/{conf.mask(self.serial)}/report within {self.wait} s "
                "(wrong printer at this IP, LAN access off, or Developer Mode needed on this firmware)"
            )
        report["_tls_verified"] = not self.insecure
        report["_topic"] = col.first_topic
        return report

    def _clean(self, e: BaseException) -> str:
        """Error text with the serial masked and the access code removed, whatever the lower layer put in it."""
        s = str(e).replace(self.serial, conf.mask(self.serial))
        return s.replace(self.access_code, "<redacted>") if self.access_code else s


# ---------------------------------------------------------------------------------- decoding
def packed_temp(v) -> tuple[float | None, float | None]:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None, None
    return float(v & 0xFFFF), float((v >> 16) & 0xFFFF)


def decode_hms(hms: list[dict], table: dict | None = None) -> list[dict]:
    """Studio DevHMS.cpp: module/part from attr, level and code from code; 16-hex long code."""
    out = []
    for h in hms or []:
        attr, code = int(h.get("attr", 0)), int(h.get("code", 0))
        long_code = f"{(attr >> 24) & 0xFF:02X}{(attr >> 16) & 0xFF:02X}{(attr >> 8) & 0xFF:02X}{attr & 0xFF:02X}00{(code >> 16) & 0xFF:02X}{code & 0xFFFF:04X}"
        out.append(
            {
                "code": "_".join(long_code[i : i + 4] for i in range(0, 16, 4)),
                "level": HMS_LEVELS.get(code >> 16, str(code >> 16)),
                "text": (table or {}).get(long_code, ""),
                "wiki": f"https://wiki.bambulab.com/en/hms/{long_code}",
            }
        )
    return out


@dataclass
class Status:
    serial_masked: str
    ip: str
    state: str
    raw_state: str
    percent: int | None
    remaining_min: int | None
    layer: int | None
    total_layers: int | None
    task: str | None
    file: str | None
    bed_c: float | None
    bed_target_c: float | None
    chamber_c: float | None
    nozzles: list[dict]
    active_extruder: int | None
    print_error: int | None
    hms: list[dict]
    wifi: str | None
    stage: int | None
    tls_verified: bool | None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def busy(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def done(self) -> bool:
        """True only for a known final state. `unknown`, `offline` and unrecognised states are neither busy nor done."""
        return self.state in DONE_STATES

    def ends_at(self) -> str | None:
        if self.remaining_min is None or not self.busy:
            return None
        return time.strftime("%H:%M", time.localtime(time.time() + self.remaining_min * 60))

    def to_json(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "raw"}
        d["ends_at"] = self.ends_at()
        return d

    def lines(self) -> list[str]:
        out = [
            f"printer {self.serial_masked} at {self.ip}: {self.state} ({self.raw_state})"
            + ("" if self.tls_verified else ", TLS unverified")
        ]
        if self.task:
            out.append(f"job: {self.task}" + (f"  file {self.file}" if self.file and self.file != self.task else ""))
        if self.percent is not None and self.busy:
            prog = f"progress {self.percent} %"
            if self.layer is not None:
                prog += f", layer {self.layer}" + (f" of {self.total_layers}" if self.total_layers else "")
            if self.remaining_min is not None:
                prog += f", {self.remaining_min // 60} h {self.remaining_min % 60} min left, ends about {self.ends_at()}"
            out.append(prog)
        for n in self.nozzles:
            tgt = f" -> {n['target_c']:.0f}" if n.get("target_c") else ""
            out.append(
                f"nozzle {n['id']} {n['name']}: {n['temp_c']:.0f} C{tgt}"
                + (f", {n['diameter']} mm" if n.get("diameter") else "")
                + (", active" if self.active_extruder == n["id"] else "")
            )
        if self.bed_c is not None:
            out.append(
                f"bed {self.bed_c:.0f} C"
                + (f" -> {self.bed_target_c:.0f}" if self.bed_target_c else "")
                + (f", chamber {self.chamber_c:.0f} C" if self.chamber_c is not None else "")
            )
        if self.print_error:
            out.append(f"print_error {self.print_error:#010x}")
        for h in self.hms:
            out.append(f"HMS {h['code']} [{h['level']}] {h['text'] or h['wiki']}")
        if self.wifi:
            out.append(f"wifi {self.wifi}")
        return out


class StatusDecoder:
    def __init__(self, hms_table: dict | None = None):
        self.hms_table = hms_table or {}

    def decode(self, report: dict, serial: str, ip: str) -> Status:
        p = report.get("print", {})
        dev = p.get("device", {}) or {}
        nozzles = []
        noz_infos = {n.get("id"): n for n in ((dev.get("nozzle") or {}).get("info") or [])}
        for e in (dev.get("extruder") or {}).get("info") or []:
            cur, tgt = packed_temp(e.get("temp"))
            n = noz_infos.get(e.get("id"), {})
            nozzles.append(
                {
                    "id": e.get("id"),
                    "name": EXTRUDER_NAMES.get(e.get("id"), "?"),
                    "temp_c": cur or 0.0,
                    "target_c": tgt,
                    "diameter": n.get("diameter"),
                    "type": n.get("type"),
                }
            )
        if not nozzles and "nozzle_temper" in p:
            nozzles.append(
                {
                    "id": 0,
                    "name": "nozzle",
                    "temp_c": float(p["nozzle_temper"]),
                    "target_c": float(p.get("nozzle_target_temper", 0)),
                    "diameter": p.get("nozzle_diameter"),
                    "type": p.get("nozzle_type"),
                }
            )
        nozzles.sort(key=lambda n: -(n["id"] or 0))
        active = None
        st = (dev.get("extruder") or {}).get("state")
        if st is not None:
            try:
                active = (int(st) >> 4) & 0xF
            except (TypeError, ValueError):
                active = None
        bed_c, bed_t = (
            packed_temp(((dev.get("bed") or {}).get("info") or {}).get("temp")) if isinstance(dev.get("bed"), dict) else (None, None)
        )
        if bed_c is None and "bed_temper" in p:
            bed_c, bed_t = float(p["bed_temper"]), float(p.get("bed_target_temper", 0))
        cham, _ = packed_temp(((dev.get("ctc") or {}).get("info") or {}).get("temp")) if isinstance(dev.get("ctc"), dict) else (None, None)
        if cham is None and "chamber_temper" in p:
            cham = float(p["chamber_temper"])
        raw_state = str(p.get("gcode_state", "UNKNOWN"))
        return Status(
            conf.mask(serial),
            ip,
            STATES.get(raw_state, raw_state.lower()),
            raw_state,
            p.get("mc_percent"),
            p.get("mc_remaining_time"),
            p.get("layer_num"),
            p.get("total_layer_num"),
            p.get("subtask_name"),
            p.get("gcode_file"),
            bed_c,
            bed_t or None,
            cham,
            nozzles,
            active,
            p.get("print_error"),
            decode_hms(p.get("hms", []), self.hms_table),
            p.get("wifi_signal"),
            p.get("stg_cur"),
            report.get("_tls_verified"),
            report,
        )


# ---------------------------------------------------------------------------------- loaded filament
def hex_colour(tray_color) -> str | None:
    """`tray_color` RRGGBBAA (or RRGGBB) -> '#RRGGBB'; None when absent or all zero."""
    s = str(tray_color or "").strip().lstrip("#")
    if len(s) < 6 or not all(c in "0123456789abcdefABCDEF" for c in s[:6]):
        return None
    rgb = s[:6].upper()
    return None if rgb == "000000" and s[6:8] == "00" else f"#{rgb}"


def colour_name(colour: str | None) -> str:
    """The nearest of a dozen plain colour names, for the hand-back ('white Generic PETG')."""
    if not colour or len(colour) != 7:
        return "unknown colour"
    try:
        r, g, b = (int(colour[i : i + 2], 16) for i in (1, 3, 5))
    except ValueError:  # a malformed colour must not break a report line
        return "unknown colour"
    return min(COLOUR_NAMES, key=lambda n: sum((a - c) ** 2 for a, c in zip((r, g, b), COLOUR_NAMES[n], strict=True)))


def decode_slot(value) -> tuple[int, int] | None:
    """An extruder's `snow` (dual-nozzle) -> (ams id, slot); None for 0xFFFF or junk. 254/255 are the external spools."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if v < 0 or v == TRAY_NONE:
        return None
    return (v >> 8) & 0xFF, v & 0xFF


def decode_tray_now(value) -> tuple[int, int] | None:
    """A single-nozzle `ams.tray_now` -> (ams id, slot): 254 = the external spool, 255 = none, else ams*4 + slot."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if v == 255:
        return None
    if v == 254:
        return 254, 0
    return v // 4, v % 4


@dataclass
class Tray:
    """One spool position: an external spool (ams 254 / 255, slot 0) or an AMS slot."""

    ams: int
    slot: int
    label: str
    material: str  # tray_type; "" when the slot is empty
    colour: str | None  # "#RRGGBB"
    preset_id: str  # tray_info_idx (Bambu filament id); "" when unknown
    sub_brand: str = ""
    remain: int | None = None
    k: float | None = None
    extruder: int | None = None  # the extruder id this position feeds, when known
    preset: str | None = None  # the installed filament preset name, when resolved

    @property
    def empty(self) -> bool:
        return not self.material and not self.preset_id

    @property
    def key(self) -> tuple[int, int]:
        return self.ams, self.slot

    def describe(self) -> str:
        """'white Generic PETG (GFG99)' or 'white PETG (GFG99)' when no preset is named; 'empty' for an empty slot."""
        if self.empty:
            return "empty"
        base = self.preset.split(" @", 1)[0] if self.preset else (self.sub_brand or self.material or "unknown material")
        return f"{colour_name(self.colour)} {base}" + (f" ({self.preset_id})" if self.preset_id else "")

    def to_json(self) -> dict:
        return dict(self.__dict__)


@dataclass
class NozzleLoad:
    nozzle: int  # 1 = left/main, 2 = right/auxiliary (the spec's filament_map numbers)
    extruder: int  # the printer's extruder id
    name: str
    tray: Tray | None  # what the nozzle holds now; None when nothing is loaded, or the named slot is not in the report
    fitted: list[Tray] = field(default_factory=list)  # positions that feed this nozzle (the external spool on its side)
    loaded_slot: tuple[int, int] | None = None  # the (ams, slot) the extruder says it holds, even when no tray matches

    @property
    def unlisted(self) -> bool:
        """The extruder names a slot the report does not list: not "nothing loaded", and not a spool to guess at."""
        return self.tray is None and self.loaded_slot is not None

    def to_json(self) -> dict:
        return {
            "nozzle": self.nozzle,
            "extruder": self.extruder,
            "name": self.name,
            "tray": self.tray.to_json() if self.tray else None,
            "loaded_slot": list(self.loaded_slot) if self.loaded_slot else None,
            "fitted": [t.to_json() for t in self.fitted],
        }


@dataclass
class LoadedFilaments:
    """What the printer holds, per nozzle and per slot, from one `pushall`."""

    serial_masked: str
    ip: str
    read_at: str  # HH:MM local
    read_epoch: float
    tls_verified: bool | None
    nozzles: list[NozzleLoad]
    trays: list[Tray]
    printer_preset: str | None = None  # the machine preset the preset names were resolved for

    def nozzle(self, n: int) -> NozzleLoad | None:
        return next((z for z in self.nozzles if z.nozzle == n), None)

    def to_json(self) -> dict:
        return {
            "serial_masked": self.serial_masked,
            "ip": self.ip,
            "read_at": self.read_at,
            "read_epoch": self.read_epoch,
            "tls_verified": self.tls_verified,
            "printer_preset": self.printer_preset,
            "nozzles": [z.to_json() for z in self.nozzles],
            "trays": [t.to_json() for t in self.trays],
        }

    def lines(self) -> list[str]:
        out = [
            f"printer {self.serial_masked} at {self.ip}: filaments read at {self.read_at}"
            + ("" if self.tls_verified else ", TLS unverified")
            + (f"; presets for {self.printer_preset}" if self.printer_preset else "")
        ]
        for z in self.nozzles:
            t = z.tray
            if t is None:
                fitted = ", ".join(f"{f.label}: {f.describe()}" for f in z.fitted if not f.empty)
                state = f"holds slot {z.loaded_slot} which the report does not list" if z.unlisted else "nothing loaded"
                out.append(f"nozzle {z.nozzle} ({z.name}): {state}" + (f"; fitted {fitted}" if fitted else ""))
                continue
            out.append(
                f"nozzle {z.nozzle} ({z.name}): {t.label}: {t.material or '?'}, {t.colour or 'colour unknown'} ({colour_name(t.colour)}), "
                f"preset id {t.preset_id or 'none'} -> {t.preset or 'no installed preset matches'}"
            )
        for t in self.trays:
            if any(z.tray is t for z in self.nozzles):
                continue
            out.append(
                f"slot {t.label}: "
                + ("empty" if t.empty else f"{t.describe()}, {t.colour or 'colour unknown'} -> {t.preset or 'no preset'}")
            )
        return out


class FilamentDecoder:
    """Trays and per-nozzle loads from a report. With a preset resolver and the machine preset name, each tray gets the
    installed filament preset name: by `filament_id` = `tray_info_idx`, else the Generic preset of its material."""

    def __init__(self, resolver=None, printer_preset: str | None = None):
        self.resolver = resolver
        self.printer_preset = printer_preset

    def tray(self, d: dict, ams: int, slot: int, label: str, extruder: int | None) -> Tray:
        def num(v, cast):
            try:
                return cast(v)
            except (TypeError, ValueError):
                return None

        return Tray(
            ams,
            slot,
            label,
            str(d.get("tray_type") or ""),
            hex_colour(d.get("tray_color")),
            str(d.get("tray_info_idx") or ""),
            str(d.get("tray_sub_brands") or ""),
            num(d.get("remain"), int),
            num(d.get("k"), float),
            extruder,
        )

    def resolve(self, t: Tray) -> None:
        if self.resolver is None or t.empty:
            return
        if t.preset_id:
            t.preset = self.resolver.find_by_filament_id(t.preset_id, self.printer_preset)
        if t.preset is None and t.material:
            t.preset = self.resolver.generic_for_material(t.material, self.printer_preset)

    def decode(self, report: dict, serial: str, ip: str) -> LoadedFilaments:
        p = report.get("print", {}) or {}
        trays: list[Tray] = []
        for d in p.get("vir_slot") or []:
            try:
                vid = int(d.get("id"))
            except (TypeError, ValueError):
                continue
            trays.append(self.tray(d, vid, 0, EXTERNAL_TRAY_LABELS.get(vid, f"external spool {vid}"), EXTERNAL_TRAY_EXTRUDER.get(vid)))
        vt = p.get("vt_tray")
        if isinstance(vt, dict) and not any(t.ams == 254 for t in trays):
            trays.append(self.tray(vt, 254, 0, "external spool", 0))
        for unit in (p.get("ams") or {}).get("ams") or []:
            try:
                uid = int(unit.get("id"))
            except (TypeError, ValueError):
                continue
            for s in unit.get("tray") or []:
                try:
                    sid = int(s.get("id"))
                except (TypeError, ValueError):
                    continue
                trays.append(self.tray(s, uid, sid, f"AMS {uid + 1} slot {sid + 1}", None))
        for t in trays:
            self.resolve(t)
        by_key = {t.key: t for t in trays}
        dev = p.get("device", {}) or {}
        infos = (dev.get("extruder") or {}).get("info") or []
        nozzles: list[NozzleLoad] = []
        tray_now = decode_tray_now((p.get("ams") or {}).get("tray_now"))
        infos = [e for e in infos if e.get("id") is not None]
        if len(infos) > 1:  # dual nozzle: each extruder's `snow` names its slot; ids 0/1 map to the spec's 2/1
            for e in infos:
                eid = e["id"]
                slot = decode_slot(e.get("snow"))
                fitted = [t for t in trays if t.extruder == eid]
                nozzles.append(
                    NozzleLoad(
                        NOZZLE_OF_EXTRUDER.get(eid, 1), eid, EXTRUDER_NAMES.get(eid, "?"), by_key.get(slot) if slot else None, fitted, slot
                    )
                )
        else:  # one extruder, whatever id it reports: nozzle 1; `snow` when present, else the classic `ams.tray_now`
            eid = infos[0]["id"] if infos else 0
            slot = decode_slot(infos[0].get("snow")) if infos and "snow" in infos[0] else tray_now
            nozzles.append(NozzleLoad(1, eid, "nozzle", by_key.get(slot) if slot else None, [t for t in trays if t.ams == 254], slot))
        nozzles.sort(key=lambda z: z.nozzle)
        now = time.time()
        return LoadedFilaments(
            conf.mask(serial),
            ip,
            time.strftime("%H:%M", time.localtime(now)),
            now,
            report.get("_tls_verified"),
            nozzles,
            trays,
            self.printer_preset,
        )


# ---------------------------------------------------------------------------------- service
@dataclass
class _Attempt:
    """One read's state: the step it is in, its step times, and whether the caller gave up on it."""

    step: str = "start"
    timings: dict[str, float] = field(default_factory=dict)
    abandoned: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class PrinterService:
    """Facade: serial and access code from Studio's config, IP from discovery or the caller.

    `insecure=True` turns off TLS verification for every read (the CLI's `--insecure`). Leave it off: a verified read
    proves the host at the IP is the selected printer before the access code leaves this machine.

    `budget_s` bounds one whole read (config, discovery, MQTT): the read runs in a worker thread, and when the budget
    runs out the caller gets `PrinterError("timed out after 15 s in <step>")`. The expired attempt's thread is not
    stopped: it is a daemon, it finishes or dies with the process, and it never calls `on_step` or writes `timings`
    again. That is fine for the CLI; a long-lived caller keeps such a thread until its socket times out.
    `on_step(name, seconds)` hears each step of the current read as it ends (`config`, `arp`, `probe`, `mqtt`);
    `timings` holds the steps of the last read."""

    ip: str | None = None
    serial: str | None = None
    wait: float = 8.0
    discovery: tuple[Discovery, ...] = (ArpProbe(),)
    reader_factory: Callable[[str, str, str, float, bool], MqttReader] | None = None  # injectable transport; None = MqttReader at call time
    insecure: bool = False
    budget_s: float | None = BUDGET_S
    on_step: Callable[[str, float], None] | None = field(default=None, repr=False)
    timings: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _local: threading.local = field(default_factory=threading.local, init=False, repr=False)

    def _attempt(self) -> _Attempt:
        a = getattr(self._local, "attempt", None)
        if a is None:  # resolve() or _read() called directly, outside _bounded
            a = self._local.attempt = _Attempt()
            self.timings = a.timings
        return a

    @contextmanager
    def _timed(self, name: str) -> Iterator[None]:
        a = self._attempt()
        a.step = name
        t0 = time.monotonic()
        try:
            yield
        finally:
            dt = round(time.monotonic() - t0, 2)
            with a.lock:
                if not a.abandoned:  # an expired attempt reports nothing: the caller gave up on it
                    a.timings[name] = dt
                    if self.on_step is not None:
                        try:
                            self.on_step(name, dt)
                        except Exception:  # noqa: BLE001 - a progress printer must never fail the read
                            pass

    def _bounded(self, fn: Callable[[], T]) -> T:
        """`fn()` within `budget_s` in a worker thread with its own attempt state, or PrinterError naming the step it was
        in. No budget: the same attempt state, in the caller's thread."""
        attempt = _Attempt()
        self.timings = attempt.timings
        box: dict = {}

        def work() -> None:
            self._local.attempt = attempt
            try:
                box["value"] = fn()
            except BaseException as e:  # noqa: BLE001 - re-raised in the caller's thread
                box["error"] = e
            finally:
                self._local.attempt = None

        if not self.budget_s or self.budget_s <= 0:
            work()
        else:
            t = threading.Thread(target=work, name="shambu-printer-read", daemon=True)
            t.start()
            t.join(self.budget_s)
            if t.is_alive():
                with attempt.lock:
                    attempt.abandoned = True
                    self.timings = dict(attempt.timings)  # the steps that ended in time, frozen
                raise PrinterError(f"timed out after {self.budget_s:g} s in {attempt.step}")
        if "error" in box:
            raise box["error"]
        return box["value"]

    def resolve(self) -> tuple[str, str, str]:
        """IP, serial, code. Discovery only proposes a host: the TLS name check (or, insecure, the report topic) in
        `MqttReader.read` confirms it is the selected printer. An SSDP hit that names the serial wins over other hits."""
        with self._timed("config"):
            c = conf.load()
            serial = self.serial or conf.selected_serial(c)
            if not serial:
                raise PrinterError("no printer selected in Bambu Studio (Device tab)")
            code = conf.access_code(serial, c)
        ip = self.ip
        if not ip:
            for d in self.discovery:
                if isinstance(d, ArpProbe):
                    with self._timed("arp"):
                        hosts = d.hosts()
                    with self._timed("probe"):
                        hits = d.probe_hosts(hosts)
                else:
                    with self._timed(type(d).__name__.lower()):
                        hits = d.find()
                if hits:
                    named = [h for h in hits if h.get("serial") == serial]
                    ip = (named or hits)[0]["ip"]
                    break
        if not ip:
            raise PrinterError("no host on this LAN answers on port 8883. Is the printer on and on this network? Pass --ip.")
        return ip, serial, code

    def _read(self, ip: str, serial: str, code: str) -> dict:
        if self.insecure:
            log.warning("TLS verification is off: the access code goes to whatever answers at %s", ip)
        with self._timed("mqtt"):
            report = (self.reader_factory or MqttReader)(ip, serial, code, self.wait, self.insecure).read()
        if self.insecure and report.get("_topic", f"device/{serial}/report") != f"device/{serial}/report":
            raise PrinterError(f"host {ip} answered for another device, not printer {conf.mask(serial)}")
        return report

    def _resolve_and_read(self) -> tuple[str, str, dict]:
        ip, serial, code = self.resolve()
        return ip, serial, self._read(ip, serial, code)

    def raw_report(self) -> dict:
        return self._bounded(self._resolve_and_read)[2]

    def status(self) -> Status:
        ip, serial, report = self._bounded(self._resolve_and_read)
        return StatusDecoder(conf.hms_table(serial[:3])).decode(report, serial, ip)

    def filaments(self, resolver=None, printer_preset: str | None = None) -> LoadedFilaments:
        """The spools in each nozzle and slot, from the same single `pushall` as `status()`. Read-only."""
        ip, serial, report = self._bounded(self._resolve_and_read)
        return FilamentDecoder(resolver, printer_preset).decode(report, serial, ip)

    def watch(self, every_s: int = 300, until_idle: bool = True, max_polls: int = 200):
        """Generator of Status every `every_s` seconds; stops on a final state (idle, finished, failed) or after
        `max_polls`. An unknown or unrecognised state is not final: it logs a warning and keeps polling."""
        for _ in range(max_polls):
            st = self.status()
            yield st
            if until_idle and st.done:
                return
            if not st.busy and not st.done:
                log.warning("printer state %s (%s) is not final; still watching", st.state, st.raw_state)
            time.sleep(every_s)
