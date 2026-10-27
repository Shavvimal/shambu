"""Read-only view of a Bambu Lab printer over LAN MQTT.

The printer runs an MQTT broker on TCP 8883 (TLS, user `bblp`, password = LAN access code). We
subscribe to `device/<serial>/report`, publish exactly one `pushall`, and read the report. Status
pushes are exempt from Bambu's Authorization Control, so this works without Developer Mode.
Nothing here starts, pauses, stops or changes a print, and nothing but `pushall` is published.

Design: `Discovery` strategies find the IP (ARP + port probe, the default-route subnet, passive SSDP). `MqttReader`
adapts paho-mqtt to one call. `StatusDecoder` turns the raw report into a `Status`. `PrinterService` is the facade the
CLI uses.

Credentials: a chain of stateless `CredentialSource`s (`PrinterService.sources`; `(StudioCredentials(),)` by default):
`OneShotCredentials` (a code from a one-shot flag), `ProfileCredentials` (the profile's serial and the code the
credentials file holds for it) and `StudioCredentials` (Studio's config; None when the file or the key is absent).
`resolve()` takes the serial first (`serial` given by the caller, then the first source with one, then, with `ip`
given and TLS verified, the CN of the certificate at that IP, read inside the `credentials` step), then the code for
that serial (the first source with one; `ConfError` when none has it), then the IP (`ip`, then the profile's `ip`, then
discovery). It returns a `Resolved` that names the source of each. When a read at the profile's IP cannot connect, the
error names the profile and the commands that record a new address; the service never writes the profile.

Staged discovery: `discovery` is a tuple of passes (`ArpProbe`, then `SubnetProbe` on the profile path). With the
serial known from a flag or a profile and TLS verified (`check_candidates`), each hit is checked in one `identity` step:
`cert_serial(ip)` must name the serial (any case), within what is left of the read's timeout above the MQTT reserve
(CONNECT_S + HANDSHAKE_S). A mismatch or a failed check skips the hit, and the passes go on until a hit is accepted or
the reserve is reached. A missing CA bundle raises before any pass. Otherwise (insecure, a serial from Studio's config, or
`check_candidates` off) the hit that names the serial wins, else the first hit, as in 0.4.0. No credential leaves the
process during the choice of a candidate.

Typed read result: every read (`raw_report`, `public_report`, `status`, `filaments`, each `watch` poll) goes through
`_resolve_and_read`, which returns a `ReadResult` (the report and the `Resolved`). The printer's report never populates
a `ReadResult`: `_Collector` merges any top-level key the printer sends, so no report key carries authority.

`certificate_serial(ip)` reads the CN of the certificate at `ip` (a TLS handshake against Studio's CA bundle, no SNI,
no MQTT), the connect and the handshake together within the deadline. Its trust boundary: a chain-valid CN proves that a
Bambu device answers at that IP, not that it is the intended printer. The CN must be serial-shaped, and the MQTT read
that follows pins that serial.

Trust: discovery only proposes an IP. With the serial known and TLS on, the first host whose certificate names the
serial wins; otherwise the hit that names the serial, else the first host that answers on 8883. TLS fails closed. By
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

Time fields (live X2D report, firmware 20018, 2026-10-21; Studio's DeviceManager.cpp): the report carries no start time
and no total estimate, only `mc_remaining_time` (min). So `Status.read_at` stamps each read in UTC when the report is
decoded, and `watch` polls more often near the start and the end of a print, where the times matter. `spd_lvl` is the
speed level (1 silent, 2 standard, 3 sport, 4 ludicrous), `spd_mag` its percent, `stg` the list of startup stages this
job runs and `stg_cur` the current one (0 = printing); names in `STAGE_NAMES`. No output of this module names the IP:
`Status.to_json` redacts it and keeps only the masked serial, and `Status.log_poll` (a `watch --log` line) holds
neither the IP nor the serial.
"""

from __future__ import annotations

import calendar
import copy
import ipaddress
import json
import logging
import math
import re
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeVar

from . import conf
from .errors import ConfError, PrinterError
from .vault import Vault, VaultError

if TYPE_CHECKING:
    from .profiles import Profile

log = logging.getLogger(__name__)
T = TypeVar("T")
C = TypeVar("C", bound=ssl.SSLContext)

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
# Network bounds for one read (doctor timings, 2026-10-21): TCP connect, TLS handshake, MQTT keepalive, and the
# timeout for the whole read (discovery + connect + report) unless the caller gives another.
CONNECT_S = 3.0
HANDSHAKE_S = 5.0
KEEPALIVE_S = 10
READ_TIMEOUT_S = 15.0
TRAY_NONE = 0xFFFF  # `snow` / `tray_now` value for "nothing loaded"
SPEED_LEVELS = {1: "silent", 2: "standard", 3: "sport", 4: "ludicrous"}  # `spd_lvl`; Studio StatusPanel.cpp
# `stg` / `stg_cur` ids -> Studio's text (DeviceManager.cpp get_stage_string, v02.08.02.61). Every id the live X2D job
# listed on 2026-10-21 is here; 255 is in that list, but Studio's table has no text for it.
STAGE_NAMES = {
    0: "printing",
    1: "auto bed levelling",
    2: "heatbed preheating",
    3: "vibration compensation",
    4: "changing filament",
    7: "heating nozzle",
    8: "calibrating dynamic flow",
    11: "identifying build plate type",
    13: "homing toolhead",
    14: "cleaning nozzle tip",
    29: "cooling chamber",
    39: "nozzle offset calibration",
    51: "printing calibration lines",
    54: "waiting for the heatbed to reach its target",
    73: "build plate alignment detection",
    74: "heatbed surface foreign object detection",
    75: "heatbed underside foreign object detection",
    255: "no name in Studio's table",
}
REDACTED_IP = "<ip>"
# watch(): at most FAST_POLL_S between polls near the start and the end of a print, where Studio's model is worst.
FAST_POLL_S = 60
START_WINDOW_S = 15 * 60
END_WINDOW_MIN = 15
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
    """A way to propose printer IPs. `with_deadline(seconds)` gives a copy that finishes within that time (one copy per
    read, so its `timings` and `shortfall` belong to that read). After `find()`, `timings` holds its steps in seconds
    and `shortfall` says what it could not cover in time (None when it covered everything)."""

    timings: dict[str, float]
    shortfall: str | None

    def with_deadline(self, seconds: float | None) -> Discovery: ...

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
    with a 2 s timeout, then the 8883 probe on min(`workers`, hosts) threads, `timeout` s per host. The probe window
    is max(`probe_window_s`, rounds x `timeout`), so every host gets its turn on a large LAN, and never longer than
    the deadline `with_deadline` set. A host not probed when the window ends counts as closed, and `shortfall` says how
    many were probed. The probe threads are daemons: a probe still blocked at the end never holds up the process exit."""

    timeout: float = 0.5
    probe_window_s: float = 2.0
    workers: int = 64
    arp_timeout: float = 2.0
    deadline_s: float | None = None  # the most this find() may take; None: only its own bounds
    runner: Runner = field(default=subprocess.run, repr=False)  # injectable: tests pass a fake
    probe: Callable[[str, int, float], bool] = field(default=port_open, repr=False)
    timings: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    shortfall: str | None = field(default=None, init=False, repr=False)

    def with_deadline(self, seconds: float | None) -> ArpProbe:
        return replace(self, deadline_s=seconds)

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

    def window(self, n_hosts: int, limit: float | None = None) -> float:
        threads = max(1, min(self.workers, n_hosts))
        w = max(self.probe_window_s, math.ceil(n_hosts / threads) * self.timeout)
        return w if limit is None else max(0.0, min(w, limit))

    def probe_hosts(self, hosts: list[str], limit: float | None = None, method: str = "arp+8883") -> list[dict]:
        self.shortfall = None
        if not hosts:
            return []
        todo = list(hosts)
        open_: set[str] = set()
        lock = threading.Lock()
        probed = [0]
        finished = threading.Event()
        window = self.window(len(hosts), limit)
        deadline = time.monotonic() + window

        def worker() -> None:
            while True:
                with lock:
                    if not todo or time.monotonic() >= deadline:
                        return
                    h = todo.pop(0)
                try:
                    ok = bool(self.probe(h, 8883, self.timeout))
                except Exception as e:  # noqa: BLE001 - the probe is injectable; a closed port is False, anything else is logged
                    log.warning("port probe of %s raised %s: %s; counted as closed", h, type(e).__name__, e)
                    ok = False
                with lock:
                    if ok:
                        open_.add(h)
                    probed[0] += 1
                    if probed[0] == len(hosts):
                        finished.set()

        for i in range(max(1, min(self.workers, len(hosts)))):
            threading.Thread(target=worker, name=f"shambu-probe-{i}", daemon=True).start()
        finished.wait(window)
        with lock:
            found, n = set(open_), probed[0]
        if n < len(hosts):
            self.shortfall = f"{n} of {len(hosts)} hosts probed before the {window:g} s probe window ended"
            log.warning("port probe: %s", self.shortfall)
        return [{"ip": h, "method": method} for h in hosts if h in found]  # input order, not finish order

    def find(self) -> list[dict]:
        self.timings = {}
        t0 = time.monotonic()
        hosts = self.hosts()
        self.timings["arp"] = round(time.monotonic() - t0, 2)
        left = None if self.deadline_s is None else self.deadline_s - (time.monotonic() - t0)
        t1 = time.monotonic()
        hits = self.probe_hosts(hosts, left)
        self.timings["probe"] = round(time.monotonic() - t1, 2)
        return hits


_ROUTE_IF = re.compile(r"^\s*interface:\s*([A-Za-z0-9]+)\s*$", re.M)
_ROUTE_GW = re.compile(r"^\s*gateway:\s*(\d+\.\d+\.\d+\.\d+)\s*$", re.M)
# `inet A netmask 0x...` only: a point-to-point `inet A --> B netmask ...` line never matches
_IFCONFIG_INET = re.compile(r"^\s*inet (\d+\.\d+\.\d+\.\d+) netmask 0x([0-9A-Fa-f]{8})\b", re.M)


def _prefix(mask_hex: str) -> int | None:
    """The prefix length of a netmask in hex; None for a non-contiguous mask."""
    m = int(mask_hex, 16)
    host = ~m & 0xFFFFFFFF
    return None if host & (host + 1) else 32 - host.bit_length()


def subnet_hosts(route_text: str, ifconfig_text: str, limit_prefix: int = 24) -> list[str]:
    """The other hosts of the default-route network, in numeric order. `route_text` is `route -n get default`,
    `ifconfig_text` is `ifconfig <interface>`. The network is the one of the interface address that contains the
    route's gateway; every local address of the interface is left out. `[]` when there is no gateway, no address's
    network holds it, the mask is not contiguous or wider than /`limit_prefix`, or a text does not parse."""
    route_text = route_text or ""
    gw = _ROUTE_GW.search(route_text)
    if not _ROUTE_IF.search(route_text) or not gw:
        return []
    try:
        gateway = ipaddress.IPv4Address(gw.group(1))
    except ValueError:
        return []
    own: list[ipaddress.IPv4Interface] = []
    for addr, mask_hex in _IFCONFIG_INET.findall(ifconfig_text or ""):
        prefix = _prefix(mask_hex)
        if prefix is None:
            continue
        try:
            own.append(ipaddress.IPv4Interface(f"{addr}/{prefix}"))
        except ValueError:
            continue
    net = next((i.network for i in own if gateway in i.network), None)
    if net is None or net.prefixlen < limit_prefix:
        return []
    local = {i.ip for i in own}
    return [str(h) for h in net.hosts() if h not in local]


@dataclass
class SubnetProbe(ArpProbe):
    """The second discovery pass: the hosts of the default-route interface's network (/24 or narrower) that are not in
    the ARP table and answer on 8883. It runs `route -n get default`, `ifconfig <interface>` and `arp -an`, each bounded
    by min(`arp_timeout`, what is left of the deadline) and skipped when nothing is left, never scans a network wider than /24, and sends nothing but a TCP connect to a candidate. It is
    always the pass after `ArpProbe`, never the only one. A runner failure, a timeout or a non-zero exit status gives
    no hosts and a `shortfall`."""

    def with_deadline(self, seconds: float | None) -> SubnetProbe:
        return replace(self, deadline_s=seconds)

    def _run(self, argv: list[str], t0: float) -> subprocess.CompletedProcess[str]:
        """`argv` with a timeout of min(`arp_timeout`, what is left of the deadline). TimeoutExpired when nothing is
        left (the command does not run), OSError for a non-zero exit, else what the runner raises."""
        limit = self.arp_timeout
        if self.deadline_s is not None:
            limit = min(limit, self.deadline_s - (time.monotonic() - t0))
        if limit <= 0:
            raise subprocess.TimeoutExpired(argv, 0)
        r = self.runner(argv, capture_output=True, text=True, timeout=limit)
        if r.returncode != 0:
            raise OSError(f"{argv[0]} exited with status {r.returncode}")
        return r

    def interface_hosts(self, t0: float | None = None) -> list[str]:
        """The other hosts of the default-route network (see `subnet_hosts`). Raises what `_run` raises."""
        t0 = time.monotonic() if t0 is None else t0
        r = self._run(["route", "-n", "get", "default"], t0)
        m = _ROUTE_IF.search(r.stdout or "")
        if not m:
            return []
        i = self._run(["ifconfig", m.group(1)], t0)
        return subnet_hosts(r.stdout or "", i.stdout or "")

    def find(self) -> list[dict]:
        self.timings = {}
        self.shortfall = None
        t0 = time.monotonic()
        why = "the default-route network is wider than /24 or could not be read, so no subnet scan"
        skip: set[str] = set()
        try:
            hosts = self.interface_hosts(t0)
            if hosts:  # the ARP hosts are the first pass's: inside the same failure boundary
                skip = set(arp_hosts(self._run(["arp", "-an"], t0).stdout or ""))
        except (subprocess.TimeoutExpired, OSError) as e:  # no subnet pass, never a crash
            msg = conf.redact_identifiers(conf.scrub_text(str(e)))
            log.warning("reading the default-route network failed (%s: %s); no subnet scan", type(e).__name__, msg)
            hosts, why = [], f"the default-route network could not be read ({type(e).__name__}), so no subnet scan"
        if not hosts:
            self.shortfall = why
            self.timings["subnet"] = round(time.monotonic() - t0, 2)
            return []
        left = None if self.deadline_s is None else self.deadline_s - (time.monotonic() - t0)
        hits = self.probe_hosts([h for h in hosts if h not in skip], left, method="subnet+8883")
        self.timings["subnet"] = round(time.monotonic() - t0, 2)
        return hits


@dataclass
class SsdpListen:
    """Passive listen for the printer's SSDP NOTIFY on UDP 2021 and 1990. SO_REUSEPORT is set
    because the Studio GUI holds 2021 while it runs."""

    seconds: float = 10.0
    timings: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    shortfall: str | None = field(default=None, init=False, repr=False)

    def with_deadline(self, seconds: float | None) -> SsdpListen:
        return self if seconds is None else replace(self, seconds=max(0.0, min(self.seconds, seconds)))

    def find(self) -> list[dict]:
        t_start = time.monotonic()
        try:
            return self._listen()
        finally:
            self.timings = {"ssdp": round(time.monotonic() - t_start, 2)}

    def _listen(self) -> list[dict]:
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


def _ca_context(cls: type[C]) -> C:
    """A client context of class `cls` that trusts only Studio's printer CA bundle, with CERT_REQUIRED. A missing or
    unloadable bundle raises; nothing falls back to an unverified context. The caller sets the name check."""
    if not CA_FILE.is_file():
        raise PrinterError(f"printer CA bundle not found at {CA_FILE} (install Bambu Studio), or pass --insecure to skip TLS verification")
    ctx = cls(ssl.PROTOCOL_TLS_CLIENT)
    try:
        ctx.load_verify_locations(str(CA_FILE))
    except (ssl.SSLError, OSError) as e:
        raise PrinterError(f"cannot load printer CA bundle {CA_FILE}: {e}") from e
    ctx.verify_mode = ssl.CERT_REQUIRED
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT  # Python 3.13: Bambu's CA fails the strict profile
    return ctx


def tls_context(serial: str, insecure: bool = False) -> SerialPinnedContext:
    """Verified context by default: Studio's CA bundle, chain and name (= serial) checked. A missing CA bundle raises; it
    never falls back to an unverified context. `insecure=True` gives CERT_NONE and no name check, on request only."""
    if insecure:
        ctx = SerialPinnedContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.expected_serial = serial
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ctx = _ca_context(SerialPinnedContext)
    ctx.expected_serial = serial
    ctx.check_hostname = True
    ctx.hostname_checks_common_name = True  # Bambu printer certificates carry the serial as CN
    return ctx


_SERIAL_CN = re.compile(r"[A-Za-z0-9]{8,32}")


def _identity_context() -> ssl.SSLContext:
    """The context `certificate_serial` uses: Studio's CA bundle, CERT_REQUIRED, no name check. A missing or unloadable
    bundle raises `PrinterError` (`_ca_context`). `PrinterService._discover` builds it once, before any identity check,
    so a missing bundle surfaces at once instead of reading as "not the printer"; the tests patch it."""
    ctx = _ca_context(ssl.SSLContext)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def certificate_serial(ip: str, deadline: float | None = None, ctx: ssl.SSLContext | None = None) -> str:
    """The printer serial that the certificate at `ip`:8883 names (its first subject CN). One TLS handshake against
    Studio's CA bundle, no SNI and no name check, then close: no MQTT CONNECT, no credential. `deadline` (seconds)
    bounds the connect and the handshake together: the connect gets min(CONNECT_S, deadline), the handshake
    min(HANDSHAKE_S, what the connect left of the deadline). `ctx` is a prebuilt `_identity_context()`; None builds one.
    A deadline spent before the connect or before the handshake raises a `timed out` PrinterError (no floor).

    Trust boundary: a chain-valid CN proves that a Bambu device answers at `ip`, not that it is the intended printer.
    The CN must be serial-shaped; the MQTT read that follows pins that serial."""
    if ctx is None:
        ctx = _identity_context()
    no_time = f"timed out: no time left for the TLS handshake with {ip}:8883"
    if deadline is not None and deadline <= 0:
        raise PrinterError(no_time)
    connect_s = CONNECT_S if deadline is None else min(CONNECT_S, deadline)
    t0 = time.monotonic()
    try:
        sock = socket.create_connection((ip, 8883), timeout=connect_s)
    except OSError as e:
        raise PrinterError(f"cannot connect to {ip}:8883: {e}") from None
    try:
        left = HANDSHAKE_S if deadline is None else deadline - (time.monotonic() - t0)
        if left <= 0:
            raise PrinterError(no_time)
        sock.settimeout(min(HANDSHAKE_S, left))
        tls = ctx.wrap_socket(sock, server_hostname=None)
        try:
            cert: dict = dict(tls.getpeercert() or {})
        finally:
            tls.close()
    except ssl.SSLError as e:
        raise PrinterError(f"TLS verification failed for the host at {ip}: {e}") from None
    except TimeoutError:
        raise PrinterError(f"TLS handshake with {ip}:8883 timed out") from None
    except OSError as e:
        raise PrinterError(f"cannot connect to {ip}:8883: {e}") from None
    finally:
        sock.close()
    cns = [v for rdn in cert.get("subject", ()) for k, v in rdn if k == "commonName"]
    if not cns or not _SERIAL_CN.fullmatch(cns[0]):
        raise PrinterError(f"the certificate at {ip} names no printer")
    return cns[0]


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


class LoginRefused(PrinterError):
    """The printer refused the MQTT login: the access code is wrong for that printer (`refused the MQTT login`)."""


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
            raise LoginRefused(
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
    read_at: str | None = None  # UTC ISO 8601 with seconds, set when the report is decoded
    speed_level: int | None = None  # spd_lvl: 1 silent, 2 standard, 3 sport, 4 ludicrous
    speed_percent: int | None = None  # spd_mag
    stages: list[int] | None = None  # stg: the startup stages of this job, as the printer sends them
    task_id: str | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def busy(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def done(self) -> bool:
        """True only for a known final state. `unknown`, `offline` and unrecognised states are neither busy nor done."""
        return self.state in DONE_STATES

    def ends_at(self) -> str | None:
        """Local HH:MM of the forecast end, for the text lines."""
        if self.remaining_min is None or not self.busy:
            return None
        return time.strftime("%H:%M", time.localtime(time.time() + self.remaining_min * 60))

    @property
    def speed_name(self) -> str | None:
        return None if self.speed_level is None else SPEED_LEVELS.get(self.speed_level, f"level {self.speed_level}")

    def ends_at_utc(self) -> str | None:
        """The printer's forecast end as a dated UTC timestamp: `read_at` plus the remaining minutes."""
        if self.remaining_min is None or not self.busy or not self.read_at:
            return None
        return utc_iso(calendar.timegm(time.strptime(self.read_at, "%Y-%m-%dT%H:%M:%SZ")) + self.remaining_min * 60)

    def to_json(self) -> dict:
        """Every field but `raw`, with the IP redacted (CONTRIBUTING: an IP never lands in a file) and the serial masked.
        `ends_at` is the dated UTC forecast (`ends_at_utc`); the text lines show the local clock time."""
        d = {k: v for k, v in self.__dict__.items() if k != "raw"}
        d["ip"] = REDACTED_IP
        d["ends_at"] = self.ends_at_utc()
        d["speed"] = self.speed_name
        return d

    def log_poll(self) -> dict:
        """One `watch --log` poll line: the time and job fields only. No IP, no serial (not even masked), no wifi."""
        return {
            "read_at": self.read_at,
            "task": self.task,
            "task_id": self.task_id,
            "state": self.raw_state,
            "percent": self.percent,
            "layer": self.layer,
            "total_layers": self.total_layers,
            "remaining_min": self.remaining_min,
            "speed_level": self.speed_level,
            "speed_percent": self.speed_percent,
            "stage": self.stage,
            "stages": self.stages,
            "print_error": self.print_error,
        }

    def lines(self) -> list[str]:
        out = [
            f"printer {self.serial_masked}: {self.state} ({self.raw_state})"
            + ("" if self.tls_verified else ", TLS unverified")
            + (f", read {self.read_at}" if self.read_at else "")
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
        if self.speed_level is not None and self.busy:
            out.append(f"speed {self.speed_name}" + (f" ({self.speed_percent} %)" if self.speed_percent is not None else ""))
        if self.stage and self.busy:
            out.append(f"stage {self.stage}: {STAGE_NAMES.get(self.stage, 'no name in this table')}")
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


def utc_iso(t: float) -> str:
    """Epoch seconds -> '2026-10-21T14:23:55Z'."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _int_or_none(v) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class StatusDecoder:
    """Raw report -> Status. `now` is the clock for `read_at`, injected so that tests can fix it."""

    def __init__(self, hms_table: dict | None = None, now: Callable[[], float] = time.time):
        self.hms_table = hms_table or {}
        self.now = now

    def decode(self, report: dict, serial: str, ip: str) -> Status:
        p = report.get("print", {})
        secrets = outward_secrets(serial, ip)
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
        stg = p.get("stg")
        stages = [s for s in (_int_or_none(x) for x in stg) if s is not None] if isinstance(stg, list) else None
        task_id = p.get("task_id") if p.get("task_id") not in (None, "") else p.get("subtask_id")
        return Status(
            conf.mask(serial),
            ip,
            STATES.get(raw_state, raw_state.lower()),
            raw_state,
            p.get("mc_percent"),
            p.get("mc_remaining_time"),
            p.get("layer_num"),
            p.get("total_layer_num"),
            scrub(p.get("subtask_name"), secrets),  # free text: the user can name a job after anything
            scrub(p.get("gcode_file"), secrets),
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
            read_at=utc_iso(self.now()),
            speed_level=_int_or_none(p.get("spd_lvl")),
            speed_percent=_int_or_none(p.get("spd_mag")),
            stages=stages,
            task_id=None if task_id in (None, "") else scrub(str(task_id), secrets),
            raw=report,
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
            "ip": REDACTED_IP,
            "read_at": self.read_at,
            "read_epoch": self.read_epoch,
            "tls_verified": self.tls_verified,
            "printer_preset": self.printer_preset,
            "nozzles": [z.to_json() for z in self.nozzles],
            "trays": [t.to_json() for t in self.trays],
        }

    def lines(self) -> list[str]:
        out = [
            f"printer {self.serial_masked}: filaments read at {self.read_at}"
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


# ---------------------------------------------------------------------------------- credentials
class CredentialSource(Protocol):
    """Where a serial and its access code can come from. Stateless: no timings and no cache, so overlapping reads on
    one service share nothing. A method returns None when the source has nothing, and raises only when the source is
    broken (an unreadable or damaged credentials file)."""

    @property
    def name(self) -> str: ...

    def serial(self) -> str | None: ...

    def code(self, serial: str) -> str | None: ...


@dataclass(frozen=True)
class OneShotCredentials:
    """A code from a one-shot flag (`--access-code-file`, `--access-code-stdin`). It names no serial."""

    value: str = field(repr=False)
    name: str = field(default="flag", init=False)

    def serial(self) -> str | None:
        return None

    def code(self, serial: str) -> str | None:
        conf.remember_secret(self.value)
        return self.value or None


@dataclass(frozen=True)
class ProfileCredentials:
    """A printer profile's serial and the code the vault (the credentials file) holds for that serial. A vault failure
    becomes a `PrinterError` that names the credentials file."""

    profile: Profile
    vault: Vault = field(repr=False)
    name: str = field(default="profile", init=False)

    def serial(self) -> str | None:
        return self.profile.serial

    def code(self, serial: str) -> str | None:
        if serial != self.profile.serial:
            return None
        try:
            return self.vault.get(serial)
        except VaultError as e:
            raise PrinterError(f"the credentials file could not give the access code for {conf.mask(serial)}: {e}") from None


@dataclass(frozen=True)
class StudioCredentials:
    """Bambu Studio's config: the selected serial and the code Studio stored for a serial. Both return None when the
    config file is absent, unreadable or not JSON, has the wrong shape (a non-dict root, `app` or `access_code`, a
    non-str value) or lacks the key, as `conf._studio_values` treats it; this source never raises."""

    load: Callable[[], dict] = field(default=conf.load, repr=False)
    name: str = field(default="studio", init=False)

    def _conf(self) -> dict | None:
        try:
            c = self.load()
        except (ConfError, OSError, ValueError):
            return None
        return c if isinstance(c, dict) else None

    def serial(self) -> str | None:
        c = self._conf()
        app = None if c is None else c.get("app")
        selected = app.get("user_last_selected_machine") if isinstance(app, dict) else None
        return selected if isinstance(selected, str) and selected else None

    def code(self, serial: str) -> str | None:
        c = self._conf()
        codes = None if c is None else c.get("access_code")
        code = codes.get(serial) if isinstance(codes, dict) else None
        if not isinstance(code, str) or not code:
            return None
        conf.remember_secret(code)
        return code


SOURCE_LABELS = {"flag": "a one-shot flag", "profile": "the credentials file", "studio": "Bambu Studio's config"}
_LAN_HINT = (
    "The printer can regenerate it when LAN-only mode is toggled, when the refresh icon next to the code is pressed, "
    "after some firmware updates, and sometimes after a reboot. Read the code off the touchscreen and run: "
    "shambu printer login {login}"
)
_CLOUD_HINT = (
    "Open Bambu Studio once so it fetches the current code from your account, then run: shambu printer login --from-studio {login}"
)
REFUSAL_HINT = {  # str.format templates: `{login}` is `--printer NAME` or `--ip IP`
    "lan": _LAN_HINT,
    "cloud": _CLOUD_HINT,
    "unknown": "In LAN-only mode: " + _LAN_HINT + "\nIn cloud mode: " + _CLOUD_HINT,
}


@dataclass(frozen=True)
class Resolved:
    """What one read uses, and where each part came from. Sources: "flag", "profile", "studio", "certificate", or the
    discovery method of the hit ("arp+8883", "subnet+8883", "ssdp")."""

    ip: str = field(repr=False)
    serial: str = field(repr=False)
    code: str = field(repr=False)
    ip_source: str
    serial_source: str
    code_source: str


@dataclass
class ReadResult:
    """One read: the report as the printer sent it (with the transport keys) and what the read used."""

    report: dict = field(repr=False)  # free text from the printer (job and file names)
    resolved: Resolved


# ---------------------------------------------------------------------------------- service
@dataclass
class _Attempt:
    """One read's state: the step it is in, its step times, and whether the caller gave up on it."""

    step: str = "start"
    started: float = field(default_factory=time.monotonic)
    timings: dict[str, float] = field(default_factory=dict)
    abandoned: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class PrinterService:
    """Facade: serial and access code from the credential chain (`sources`), IP from the caller, the profile or
    discovery.

    `insecure=True` turns off TLS verification for every read (the CLI's `--insecure`). Leave it off: a verified read
    proves the host at the IP is the selected printer before the access code leaves this machine.

    `timeout_s` bounds one whole read (credentials, discovery, MQTT): the read runs in a worker thread, and when the
    timeout runs out the caller gets `PrinterError("timed out after 15 s in <step>")`. The expired attempt's thread is not
    stopped: it is a daemon, it finishes or dies with the process, and it never calls `on_step` or writes `timings`
    again. That is fine for the CLI; a long-lived caller keeps such a thread until its socket times out.
    `on_step(name, seconds)` hears each step of the current read as it ends: `credentials` (the chain and, for an `ip`
    given with no serial, the certificate read), the discovery passes' steps (`arp`, `probe`, `subnet`), `identity`
    (the certificate checks of discovery hits) and `mqtt`. `timings` holds the steps of the last read.

    Profile path (set by the facade): `sources` holds `ProfileCredentials`, `profile` the profile, and `ip` the
    profile's IP (or the caller's override). `check_candidates` turns the identity check of discovery hits on (the
    default) or off. `cert_serial(ip, deadline, ctx)` is `certificate_serial`, injectable for tests.

    `last_resolved` is the `Resolved` of the last read that came back in time (set on the caller's thread, never by the
    worker and never for an abandoned read; None after a failed one), so a caller learns the IP and the sources a read
    used without wrapping the reader."""

    ip: str | None = None
    serial: str | None = None
    wait: float = 8.0
    discovery: tuple[Discovery, ...] = (ArpProbe(),)
    reader_factory: Callable[[str, str, str, float, bool], MqttReader] | None = None  # injectable transport; None = MqttReader at call time
    insecure: bool = False
    timeout_s: float | None = READ_TIMEOUT_S
    on_step: Callable[[str, float], None] | None = field(default=None, repr=False)
    sources: tuple[CredentialSource, ...] = field(default_factory=lambda: (StudioCredentials(),), repr=False)
    profile: Profile | None = None
    check_candidates: bool = True
    cert_serial: Callable[[str, float | None, ssl.SSLContext | None], str] = field(default=certificate_serial, repr=False)
    timings: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    last_resolved: Resolved | None = field(default=None, init=False, repr=False)
    _local: threading.local = field(default_factory=threading.local, init=False, repr=False)

    def _attempt(self) -> _Attempt:
        a = getattr(self._local, "attempt", None)
        if a is None:  # resolve() or _read() called directly, outside _bounded
            a = self._local.attempt = _Attempt()
            self.timings = a.timings
        return a

    def _record(self, a: _Attempt, name: str, dt: float) -> None:
        with a.lock:
            if a.abandoned:  # an expired attempt reports nothing: the caller gave up on it
                return
            a.timings[name] = dt
            if self.on_step is not None:
                try:
                    self.on_step(name, dt)
                except Exception as e:  # noqa: BLE001 - the caller's progress printer must never fail the read
                    log.warning("progress callback for step %s raised %s: %s", name, type(e).__name__, e)

    @contextmanager
    def _timed(self, name: str) -> Iterator[None]:
        a = self._attempt()
        a.step = name
        t0 = time.monotonic()
        try:
            yield
        finally:
            self._record(a, name, round(time.monotonic() - t0, 2))

    def _bounded(self, fn: Callable[[], T]) -> T:
        """`fn()` within `timeout_s` in a worker thread with its own attempt state, or PrinterError naming the step it
        was in. No timeout: the same attempt state, in the caller's thread. A `ReadResult` that came back in time sets
        `last_resolved`, on the caller's thread."""
        attempt = _Attempt()
        self.timings = attempt.timings
        self.last_resolved = None
        box: dict = {}

        def work() -> None:
            self._local.attempt = attempt
            try:
                box["value"] = fn()
            except BaseException as e:  # noqa: BLE001 - re-raised in the caller's thread
                box["error"] = e
            finally:
                self._local.attempt = None

        if not self.timeout_s or self.timeout_s <= 0:
            work()
        else:
            t = threading.Thread(target=work, name="shambu-printer-read", daemon=True)
            t.start()
            t.join(self.timeout_s)
            if t.is_alive():
                with attempt.lock:
                    attempt.abandoned = True
                    self.timings = dict(attempt.timings)  # the steps that ended in time, frozen
                raise PrinterError(f"timed out after {self.timeout_s:g} s in {attempt.step}")
        if "error" in box:
            raise box["error"]
        value = box["value"]
        if isinstance(value, ReadResult):
            self.last_resolved = value.resolved
        return value

    def _remaining(self, a: _Attempt) -> float | None:
        """What is left of the read's timeout; None: no timeout."""
        if not self.timeout_s or self.timeout_s <= 0:
            return None
        return max(0.1, self.timeout_s - (time.monotonic() - a.started))

    def _left(self, a: _Attempt) -> float | None:
        """What is left of the read's timeout, possibly 0 or less; None: no timeout."""
        if not self.timeout_s or self.timeout_s <= 0:
            return None
        return self.timeout_s - (time.monotonic() - a.started)

    def _chain_serial(self) -> tuple[str | None, str | None]:
        """(serial, source) from the caller's `serial`, then the first source that names one (the source's own name).
        No network."""
        if self.serial:
            return self.serial, "flag"
        for src in self.sources:
            serial = src.serial()
            if serial:
                return serial, src.name
        return None, None

    def _chain_code(self, serial: str) -> tuple[str | None, str | None]:
        """(code, source) from the first source that has a code for `serial`. No network."""
        for src in self.sources:
            code = src.code(serial)
            if code:
                return code, src.name
        return None, None

    def prepare_secrets(self) -> None:
        """Run the serial and code lookup of the chain (no network, no certificate read) so that a code the chain
        holds is registered with `conf.remember_secret` before any output. A missing serial or code registers
        nothing and is not an error; a broken source (a damaged credentials file) still raises."""
        serial, _ = self._chain_serial()
        if not serial:
            return
        code, _ = self._chain_code(serial)
        if code:
            conf.remember_secret(code)

    def resolve(self) -> Resolved:
        """Serial, then code, then IP (`ip`, then the profile's `ip`, then discovery). Discovery only proposes a host:
        with the serial from a flag or a profile and TLS on, each hit's certificate must name the serial; the TLS name
        check (or, insecure, the report topic) in `MqttReader.read` confirms it again."""
        with self._timed("credentials"):
            serial, serial_source = self._chain_serial()
            if not serial and self.ip and not self.insecure:
                serial, serial_source = self.cert_serial(self.ip, self._remaining(self._attempt()), None), "certificate"
            if not serial or not serial_source:
                text = "no printer selected: run shambu printer login, or select it in Bambu Studio (Device tab)"
                if not conf.CONF.exists():
                    text += "; BambuStudio.conf not found: has Bambu Studio run on this Mac?"
                raise PrinterError(text)
            code, code_source = self._chain_code(serial)
            if not code or not code_source:
                raise ConfError(
                    f"no access code for serial {conf.mask(serial)}: run shambu printer login, "
                    "or open the printer once in Bambu Studio's Device tab"
                )
        ip = self.ip or (self.profile.ip if self.profile is not None else None)
        if ip:
            ip_source = "profile" if self.profile is not None and ip == self.profile.ip else "flag"
        else:
            check = self.check_candidates and not self.insecure and serial_source in ("flag", "profile")
            ip, ip_source = self._discover(serial, check)
        return Resolved(ip, serial, code, ip_source, serial_source, code_source)

    def _discover(self, serial: str, check: bool = False) -> tuple[str, str]:
        """(ip, method) of the first accepted hit over the staged passes in `discovery`, or the no-host PrinterError.
        `check`: a hit counts only when `cert_serial` names `serial` (any case), timed as one `identity` step; else the
        hit that names the serial, else the first hit.

        With `check`, the CA context is built once before any pass, so a missing CA bundle raises at once; only a
        per-host connect, TLS or CN failure skips a hit. A check starts only while more than the MQTT reserve
        (CONNECT_S + HANDSHAKE_S) is left of the read's timeout and gets only what is left above it; the hits not checked
        in time are counted apart and end discovery. A pass after the first starts only while more than the reserve is
        left; the passes not run are counted too."""
        a = self._attempt()
        ctx = _identity_context() if check else None
        reserve = CONNECT_S + HANDSHAKE_S
        gaps: list[str] = []
        rejected = 0
        unchecked = 0
        not_run = 0
        checked = 0.0
        checks_ran = False
        try:
            for n, proto in enumerate(self.discovery):
                left = self._left(a)
                if unchecked or (n and left is not None and left <= reserve):
                    not_run = len(self.discovery) - n  # the MQTT read keeps its reserve: no later pass starts
                    break
                d = proto.with_deadline(self._discovery_deadline(a))
                a.step = "discovery"
                hits = d.find()
                for name, dt in d.timings.items():
                    self._record(a, name, dt)
                if d.shortfall:
                    gaps.append(d.shortfall)
                if not hits:
                    continue
                if not check:
                    named = [h for h in hits if h.get("serial") == serial]
                    h = (named or hits)[0]
                    return h["ip"], str(h.get("method") or "discovery")
                for i, h in enumerate(hits):
                    left = self._left(a)
                    if left is not None and left <= reserve:
                        unchecked = len(hits) - i  # the MQTT read keeps its reserve: these hits go unchecked
                        break
                    a.step = "identity"
                    checks_ran = True
                    t0 = time.monotonic()
                    try:
                        budget = None if left is None else left - reserve
                        ok = self.cert_serial(h["ip"], budget, ctx).casefold() == serial.casefold()
                    except PrinterError as e:  # the identity check could not read the host: it is not the printer
                        log.info("discovery hit skipped: %s", conf.redact_identifiers(conf.scrub_text(str(e))))
                        ok = False
                    finally:
                        checked += time.monotonic() - t0
                    if ok:
                        return h["ip"], str(h.get("method") or "discovery")
                    log.info("discovery hit skipped: its certificate names another device")
                    rejected += 1
        finally:
            if checks_ran:
                self._record(a, "identity", round(checked, 2))
        if rejected and not unchecked:
            gaps.append(f"{rejected} host(s) answered but none is printer {conf.mask(serial)}")
        elif rejected:
            gaps.append(f"{rejected} host(s) checked are not printer {conf.mask(serial)}")
        if unchecked:
            gaps.append(f"{unchecked} candidate(s) not checked before the timeout")
        if not_run:
            gaps.append(f"{not_run} discovery pass(es) not run before the timeout")
        why = f" ({'; '.join(gaps)})" if gaps else ""
        raise PrinterError(f"no host on this LAN answers on port 8883{why}. Is the printer on and on this network? Pass --ip.")

    def _discovery_deadline(self, a: _Attempt) -> float | None:
        """What is left of the read's timeout once the connect and the TLS handshake are kept back; None: no timeout."""
        if not self.timeout_s or self.timeout_s <= 0:
            return None
        return max(0.5, self.timeout_s - (time.monotonic() - a.started) - CONNECT_S - HANDSHAKE_S)

    def _read(self, r: Resolved) -> dict:
        ip = r.ip
        if self.insecure:
            log.warning("TLS verification is off: the access code goes to whatever answers at %s", ip)
        try:
            with self._timed("mqtt"):
                report = (self.reader_factory or MqttReader)(ip, r.serial, r.code, self.wait, self.insecure).read()
        except LoginRefused as e:
            raise LoginRefused(self._refusal_text(str(e), r.code_source, ip)) from None
        if self.insecure and report.get("_topic", f"device/{r.serial}/report") != f"device/{r.serial}/report":
            raise PrinterError(f"host {ip} answered for another device, not printer {conf.mask(r.serial)}")
        return report

    def _refusal_text(self, base: str, code_source: str, ip: str) -> str:
        label = SOURCE_LABELS.get(code_source, code_source)
        who = f", profile {self.profile.name}" if self.profile is not None else ""
        mode = self.profile.mode if self.profile is not None else "unknown"
        login = f"--printer {self.profile.name}" if self.profile is not None else f"--ip {ip}"
        hint = REFUSAL_HINT.get(mode, REFUSAL_HINT["unknown"]).format(login=login)
        return conf.scrub_text(f"{base} (source: {label}{who})\n{hint}")  # a profile name can hold a code read later

    def _resolve_and_read(self) -> ReadResult:
        """One read. When the read at the profile's IP cannot connect, the error names the profile, its last login and
        the commands that record a new address; the profile is never written here."""
        r = self.resolve()
        try:
            return ReadResult(self._read(r), r)
        except LoginRefused:
            raise
        except PrinterError as e:
            profile = self.profile
            if not (r.ip_source == "profile" and profile is not None and str(e).startswith("cannot connect")):
                raise
            v = profile.verified
            stamp = f"{v[:10]} {v[11:16]} UTC" if v else "never"
            raise PrinterError(
                conf.scrub_text(
                    f"{e} The profile {profile.name} expected this address (last login {stamp}); run: shambu printer login "
                    f"--printer {profile.name} --ip <new ip>, or shambu printer discover"
                )
            ) from None

    def raw_report(self) -> dict:
        """The merged report as the printer sent it, with shambu's transport keys. Internal: print `public_report`."""
        return self._bounded(self._resolve_and_read).report

    def public_report(self) -> dict:
        """The report for `printer raw`: `public_raw()` of one read, which removes the transport metadata (`_topic`
        names the full serial), the network and wifi fields, secret-looking keys, and any text naming the IP or the
        serial."""
        res = self._bounded(self._resolve_and_read)
        return public_raw(res.report, res.resolved.ip, res.resolved.serial)

    def status(self) -> Status:
        res = self._bounded(self._resolve_and_read)
        return StatusDecoder(conf.hms_table(res.resolved.serial[:3])).decode(res.report, res.resolved.serial, res.resolved.ip)

    def filaments(self, resolver=None, printer_preset: str | None = None) -> LoadedFilaments:
        """The spools in each nozzle and slot, from the same single `pushall` as `status()`. Read-only."""
        res = self._bounded(self._resolve_and_read)
        return FilamentDecoder(resolver, printer_preset).decode(res.report, res.resolved.serial, res.resolved.ip)

    def watch(
        self,
        every_s: float = 300,
        until_idle: bool = True,
        max_polls: int = 200,
        max_errors: int | None = None,
        wait_for_start: bool = False,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        now: Callable[[], float] | None = None,
    ) -> Iterator[Status | WatchError]:
        """Generator of Status, one per poll; stops on a final state (idle, finished, failed) or after `max_polls`.
        An unknown or unrecognised state is not final: it logs a warning and keeps polling.

        The interval is `poll_interval()`: `every_s`, but at most 60 s while preparing, in the first 15 minutes of
        RUNNING when this watch saw the job start (a PREPARE poll, or a first RUNNING poll before layer 2 that is the
        watch's first poll or follows an idle or final state), and while fewer than 15 minutes remain.
        `wait_for_start=True`: a final state seen before any job is active does not stop the watch; it polls every
        60 s (at most `every_s`) until a job starts, still bounded by `max_polls`.
        `max_errors=None` lets a failed read raise. With a number, a failed read yields a `WatchError` (a category
        and a fixed message, no endpoint text) and the watch goes on; after `max_errors` failures in a row the last
        `WatchError` has `gave_up=True` and the generator ends. After a failure the next poll follows the interval of
        the last good poll. There is no sleep after the last permitted poll.
        `sleep`, `clock` (monotonic) and `now` (epoch, for an error's `read_at`) are injectable for tests."""
        sleep = sleep or time.sleep
        clock = clock or time.monotonic
        now = now or time.time
        running_since: float | None = None  # clock() at the first RUNNING poll, when this watch saw the start
        prev: str | None = None  # the raw state of the last good poll
        decided = False  # the start window is decided (seen, or the watch joined mid-print)
        active_seen = False
        last: Status | None = None
        errors = 0
        for n in range(max_polls):
            remaining = n + 1 < max_polls
            try:
                st = self.status()
            except (PrinterError, ConfError) as e:
                if max_errors is None:
                    raise
                errors += 1
                gave_up = errors >= max_errors or isinstance(e, ConfError)  # a config problem does not heal by waiting
                yield WatchError(utc_iso(now()), *error_category(e), consecutive=errors, gave_up=gave_up)
                if gave_up:
                    return
                if remaining:
                    sleep(self._interval(every_s, last, running_since, clock) if last else every_s)
                continue
            errors = 0
            if st.raw_state == "RUNNING" and not decided:
                decided = True
                if prev == "PREPARE" or ((st.layer or 0) <= 1 and (prev is None or prev in ("IDLE", "FINISH", "FAILED"))):
                    running_since = clock()
            active_seen = active_seen or st.busy
            prev, last = st.raw_state, st
            yield st
            waiting = wait_for_start and not active_seen
            if until_idle and st.done and not waiting:
                return
            if not st.busy and not st.done:
                log.warning("printer state %s (%s) is not final; still watching", st.state, st.raw_state)
            if remaining:
                sleep(min(every_s, FAST_POLL_S) if waiting else self._interval(every_s, st, running_since, clock))

    @staticmethod
    def _interval(every_s: float, st: Status, running_since: float | None, clock: Callable[[], float]) -> float:
        return poll_interval(every_s, st, None if running_since is None else clock() - running_since)


SETUP_MESSAGE = "shambu is not set up for the printer (shambu printer login, or Bambu Studio's Device tab)"
ERROR_CATEGORIES = (  # (category, words in the PrinterError text, the fixed message a watch log holds)
    ("timeout", ("timed out",), "the read timed out"),
    ("tls", ("TLS", "certificate", "CA bundle"), "the TLS check of the printer failed"),
    ("login", ("refused the MQTT login",), "the printer refused the access code (run shambu printer login, or check the code in Studio)"),
    ("discovery", ("no host on this LAN",), "no printer answered on port 8883 on this LAN"),
    ("identity", ("another device",), "the host answered for another device"),
    ("report", ("no full report", "no report"), "the printer sent no full report in time"),
    ("connect", ("cannot connect",), "the connection to the printer failed"),
    ("setup", ("paho-mqtt missing", "no printer selected", "no access code"), SETUP_MESSAGE),
)


def error_category(e: BaseException) -> tuple[str, str]:
    """(category, fixed message) for a failed read. The message never quotes the exception: the lower layers put the
    IP, the host name, the masked serial and the TLS names in their text, and a watch log must hold none of them."""
    if isinstance(e, ConfError):  # Studio's config: no printer selected, no access code (its text names the serial)
        return "setup", SETUP_MESSAGE
    text = str(e)
    for cat, words, msg in ERROR_CATEGORIES:
        if any(w in text for w in words):
            return cat, msg
    return "other", f"the read failed ({type(e).__name__})"


@dataclass(frozen=True)
class WatchError:
    """One failed poll of `PrinterService.watch`: a category and its fixed message (see `error_category`).
    `consecutive` counts the failures in a row; `gave_up` is True on the failure that reached `max_errors`."""

    read_at: str
    category: str
    error: str
    consecutive: int = 1
    gave_up: bool = False

    def log_poll(self) -> dict:
        return {
            "read_at": self.read_at,
            "error": self.error,
            "category": self.category,
            "consecutive": self.consecutive,
            "gave_up": self.gave_up,
        }


def outward_secrets(serial: str = "", ip: str = "") -> list[str]:
    """Every value no printer output or log may carry: the known serials, their masks and the access codes
    (`conf.sensitive_values`), plus this printer's serial, masked serial and endpoint."""
    vals = set(conf.sensitive_values()) | {v for v in (serial, conf.mask(serial) if serial else "", ip) if v and len(v) >= 4}
    return sorted(vals, key=len, reverse=True)


def scrub(text: str | None, secrets: list[str]) -> str | None:
    """`text` with every value in `secrets` replaced by '<redacted>' (None stays None). The one sanitiser at the printer
    output and log boundary: job names, file names and G-code labels are free text."""
    if text is None:
        return None
    for v in secrets:
        if v:  # an empty value would put '<redacted>' between every character
            text = text.replace(v, "<redacted>")
    return text


def scrub_tree(x, secrets: list[str]):
    """A structured record (a watch header) with every string value cleaned: the known secrets (`scrub`), then any IP
    address, LAN host name or masked serial (`conf.redact_identifiers`). Numbers and keys are never touched, so a code
    that happens to be digits inside a float cannot corrupt the record (it was once scrubbed as JSON text)."""
    if isinstance(x, dict):
        return {k: scrub_tree(v, secrets) for k, v in x.items()}
    if isinstance(x, list):
        return [scrub_tree(v, secrets) for v in x]
    if isinstance(x, str):
        return conf.redact_identifiers(scrub(x, secrets) or "")
    return x


RAW_SECRET = ("access", "password", "passwd", "token", "ticket", "secret", "auth")
RAW_DROP = ("net", "wifi_signal", "ipcam", "sn", "dev_id", "serial")  # network, camera URL (holds the IP), serial keys


def public_raw(report: dict, ip: str, serial: str) -> dict:
    """A deep copy of a raw report for output: transport keys (`_topic` holds the full serial) removed except
        `_tls_verified`, the RAW_DROP keys removed at any depth, secret-looking keys redacted (RAW_SECRET; not `conf.redact`,
    whose "code" would hide `gcode_state`), and any text
        that names the IP, the serial or the masked serial replaced by '<redacted>'."""
    masked = conf.mask(serial)
    secrets = outward_secrets(serial, ip)  # a known access code under an ordinary key too

    def clean(x):
        if isinstance(x, dict):
            return {
                k: ("<redacted>" if any(s in k.lower() for s in RAW_SECRET) else clean(v))
                for k, v in x.items()
                if not (k.startswith("_") and k != "_tls_verified") and k not in RAW_DROP
            }
        if isinstance(x, list):
            return [clean(v) for v in x]
        if isinstance(x, str) and ((ip and ip in x) or (serial and serial in x) or masked in x or any(v in x for v in secrets)):
            return "<redacted>"
        return x

    return clean(report)


def poll_interval(every_s: float, st: Status, running_for_s: float | None) -> float:
    """Seconds to the next poll. At most FAST_POLL_S while the printer prepares, in the first START_WINDOW_S of
    RUNNING (`running_for_s` is None when the watch did not see the start), and while fewer than END_WINDOW_MIN
    minutes remain: Studio's model is worst at the start and the end, and these polls bound the real start and end."""
    fast = st.raw_state == "PREPARE"
    if st.raw_state == "RUNNING" and running_for_s is not None and running_for_s < START_WINDOW_S:
        fast = True
    if st.raw_state in ("RUNNING", "PAUSE") and st.remaining_min is not None and st.remaining_min < END_WINDOW_MIN:
        fast = True
    return min(every_s, FAST_POLL_S) if fast else every_s
