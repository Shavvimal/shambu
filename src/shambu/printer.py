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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import conf
from .errors import PrinterError

log = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------------- discovery
class Discovery(Protocol):
    def find(self) -> list[dict]: ...


def port_open(ip: str, port: int = 8883, timeout: float = 0.7) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass
class ArpProbe:
    """Hosts in the ARP table that answer on 8883. On a home LAN only Bambu printers do."""

    timeout: float = 0.7

    def find(self) -> list[dict]:
        out = subprocess.run(["arp", "-a"], capture_output=True, text=True).stdout
        hosts = [m for m in re.findall(r"\((\d+\.\d+\.\d+\.\d+)\)", out) if not m.startswith(("224.", "239.", "255."))]
        return [{"ip": h, "method": "arp+8883"} for h in hosts if port_open(h, 8883, self.timeout)]


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
        super().do_handshake(block)
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
            try:
                c.connect(self.ip, 8883, keepalive=10)
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


# ---------------------------------------------------------------------------------- service
@dataclass
class PrinterService:
    """Facade: serial and access code from Studio's config, IP from discovery or the caller.

    `insecure=True` turns off TLS verification for every read (the CLI's `--insecure`). Leave it off: a verified read
    proves the host at the IP is the selected printer before the access code leaves this machine."""

    ip: str | None = None
    serial: str | None = None
    wait: float = 8.0
    discovery: tuple[Discovery, ...] = (ArpProbe(),)
    reader_factory: Callable[[str, str, str, float, bool], MqttReader] | None = None  # injectable transport; None = MqttReader at call time
    insecure: bool = False

    def resolve(self) -> tuple[str, str, str]:
        """IP, serial, code. Discovery only proposes a host: the TLS name check (or, insecure, the report topic) in
        `MqttReader.read` confirms it is the selected printer. An SSDP hit that names the serial wins over other hits."""
        c = conf.load()
        serial = self.serial or conf.selected_serial(c)
        if not serial:
            raise PrinterError("no printer selected in Bambu Studio (Device tab)")
        code = conf.access_code(serial, c)
        ip = self.ip
        if not ip:
            for d in self.discovery:
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
        report = (self.reader_factory or MqttReader)(ip, serial, code, self.wait, self.insecure).read()
        if self.insecure and report.get("_topic", f"device/{serial}/report") != f"device/{serial}/report":
            raise PrinterError(f"host {ip} answered for another device, not printer {conf.mask(serial)}")
        return report

    def raw_report(self) -> dict:
        return self._read(*self.resolve())

    def status(self) -> Status:
        ip, serial, code = self.resolve()
        report = self._read(ip, serial, code)
        return StatusDecoder(conf.hms_table(serial[:3])).decode(report, serial, ip)

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
