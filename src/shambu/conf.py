"""Read Bambu Studio's own configuration without leaking secrets.

`~/Library/Application Support/BambuStudio/BambuStudio.conf` is plain JSON. It holds the
selected printer serial, the presets last used in the GUI, the bed type index, and the LAN
access code per serial. The access code is returned only by `access_code()` and must never be
printed or written to disk by callers.

shambu's own data file lives beside it: `~/Library/Application Support/shambu/calibration.json`
(`CALIBRATION`), the measured prints `shambu calibrate --add` stores. `load_calibration()` and
`save_calibration()` read and write it (atomic: a temp file, then `os.replace`), and
`update_calibration()` does load-change-save under a file lock. It holds a list of prints with the
fixed fields of `calibrate.validate_print` (a session fingerprint, date, printer model, material, Studio total,
rate, startup offset, poll times, speed levels, flags) and never a file name, an IP, a serial, an access code or a
path.

`~/Library/Application Support/shambu/printers.json` (`PRINTERS`) holds the printer profiles (profiles.py has the
schema). `_atomic_write()` and `_locked()` are the write and lock helpers both data files use.

The runtime secret set holds every LAN access code this process read from any source (Studio's config, the vault, a
one-shot flag): `remember_secret()` adds one, `forget_secrets()` clears the set. `sensitive_values()` and
`secret_values()` include it even when Studio's config cannot be read, so `scrub_text` removes a code wherever it came
from.
"""

from __future__ import annotations

import fcntl
import ipaddress
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from .errors import BambuError, ConfError

log = logging.getLogger(__name__)
T = TypeVar("T")

APPSUPPORT = Path.home() / "Library/Application Support/BambuStudio"
CONF = APPSUPPORT / "BambuStudio.conf"
CALIBRATION = Path.home() / "Library/Application Support/shambu/calibration.json"
CALIBRATION_VERSION = 1
PRINTERS = Path.home() / "Library/Application Support/shambu/printers.json"
PRINTERS_VERSION = 1
BED_TYPES = {
    "0": "Default Plate",
    "1": "Cool Plate",
    "2": "Engineering Plate",
    "3": "High Temp Plate",
    "4": "Textured PEI Plate",
    "5": "Supertack Plate",
}
SECRET_RE = ("access", "code", "token", "ticket", "pass", "secret")
_RUNTIME_SECRETS: set[str] = set()  # every code this process read, from any source; never printed or written


def remember_secret(value: str) -> None:
    """Add a code this process read to the runtime secret set, so every outward text scrubs it. Ignores ''."""
    if value:
        _RUNTIME_SECRETS.add(value)


def forget_secrets() -> None:
    """Empty the runtime secret set (the tests call it between tests)."""
    _RUNTIME_SECRETS.clear()


def load() -> dict:
    if not CONF.is_file():
        raise ConfError(f"{CONF} not found: has Bambu Studio run on this Mac?")
    with open(CONF, encoding="utf-8") as f:
        return json.load(f)


def selected_serial(conf: dict | None = None) -> str | None:
    conf = conf or load()
    return conf.get("app", {}).get("user_last_selected_machine") or None


def access_code(serial: str | None = None, conf: dict | None = None) -> str:
    """The LAN access code for a serial. Keep it in memory; never log it."""
    conf = conf or load()
    serial = serial or selected_serial(conf)
    if not serial:
        raise ConfError("no printer selected in Bambu Studio (app.user_last_selected_machine is empty)")
    codes = conf.get("access_code", {})
    if serial not in codes:
        raise ConfError(f"no access code stored for serial {mask(serial)}; open the printer once in Studio's Device tab")
    return codes[serial]


def mask(serial: str) -> str:
    return serial[:3] + "…" + serial[-3:] if len(serial) > 6 else "…"


def summary(conf: dict | None = None) -> dict:
    """Everything useful from the config, with secrets removed and the serial masked."""
    conf = conf or load()
    app = conf.get("app", {})
    presets = conf.get("presets", {})
    serial = app.get("user_last_selected_machine", "")
    return {
        "selected_printer_serial": mask(serial) if serial else None,
        "has_access_code": bool(serial and serial in conf.get("access_code", {})),
        "gui_machine_preset": presets.get("machine"),
        "gui_process_preset": presets.get("process"),
        "gui_filament_presets": presets.get("filaments"),
        "gui_filament_colours": presets.get("filament_colors"),
        "gui_bed_type": BED_TYPES.get(str(app.get("curr_bed_type", "")), app.get("curr_bed_type")),
        "single_instance": app.get("single_instance"),
        "preset_folder": app.get("preset_folder"),
        "region": app.get("region"),
        "recent_projects": list((conf.get("recent_projects") or {}).values()),
        "models": conf.get("models"),
        "nozzle_volume_types": conf.get("nozzle_volume_types"),
    }


def redact(obj):
    """Deep copy with any key that looks secret replaced by '<redacted>'."""
    if isinstance(obj, dict):
        return {k: ("<redacted>" if any(s in k.lower() for s in SECRET_RE) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


class DataFileError(BambuError):
    """A shambu data file (a watch log, the calibration file) cannot be opened, written, locked or replaced."""


def _read_calibration(p: Path) -> list[dict]:
    from .calibrate import CalibrationError, validate_print

    if not p.is_file():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise DataFileError(f"{p}: cannot read it ({e.strerror})") from e
    except ValueError as e:
        raise ConfError(f"{p} is not readable JSON: {e}") from e
    if not isinstance(d, dict) or set(d) != {"version", "prints"} or not isinstance(d.get("prints"), list):
        raise ConfError(f'{p} is not a shambu calibration file (want {{"version": {CALIBRATION_VERSION}, "prints": [...]}})')
    if d.get("version") != CALIBRATION_VERSION:
        raise ConfError(f"{p} has calibration version {d.get('version')!r}; this shambu reads version {CALIBRATION_VERSION}")
    for i, r in enumerate(d["prints"], 1):
        try:
            validate_print(r)
        except CalibrationError as e:
            raise ConfError(f"{p}: print {i} does not match the calibration schema ({e})") from e
    return d["prints"]


def load_calibration(path: Path | None = None) -> list[dict]:
    """The stored prints; [] when the file does not exist. Raises ConfError for a file that is not version-1
    calibration JSON or holds a print that does not match the schema, DataFileError when it cannot be read."""
    return _read_calibration(path or CALIBRATION)


# One identifier detector for every outward text (the facade's printer errors, the calibration names)
_MASKED = re.compile("\\w{2,}\u2026\\w{2,}|\\w{3}\\.\\.\\.\\w{3}")
_HOST = re.compile(r"(?i)\b[a-z0-9-]+\.(?:local|lan|home|internal|com|net|org|io)\b")
_TOKEN_SPLIT = re.compile(r"[\s,;()\[\]{}<>\"'=/\\]+")
_IP_CAND = re.compile(r"(?<![\w.:%\]])\[?[0-9A-Fa-f:.%]*[.:][0-9A-Fa-f:.%]*\]?(?::\d+)?(?![\w%])")  # whole tokens only


def _is_ip(tok: str) -> bool:
    """`tok` is an IPv4 or IPv6 address, with or without brackets, a port or a zone (`ipaddress`; a version string such
    as 02.08.02.61 is not one: leading zeros are refused)."""
    t = tok.strip("[]")
    for cand in (t, t.rsplit(":", 1)[0] if t.count(":") == 1 else t, t.split("%", 1)[0], tok.split("]", maxsplit=1)[0].lstrip("[")):
        try:
            ipaddress.ip_address(cand)
            return True
        except ValueError:
            continue
    return False


def identifying(s: str) -> bool:
    """True when the text holds an IP address (v4 or v6, with or without a port), a masked serial or a LAN host name.
    Known secrets (the serial, the access code) are `sensitive_values`, scrubbed by `scrub_text`."""
    if _MASKED.search(s) or _HOST.search(s):
        return True
    return any(_is_ip(tok) for tok in _TOKEN_SPLIT.split(s) if tok)


def redact_ip(text: str) -> str:
    """`text` with every IP address (v4, v6, with a port) as '<ip>' and every LAN host name as '<host>': a printer
    error names the host it tried, and no output or file may (CONTRIBUTING: never commit an IP address)."""

    def one(m: re.Match) -> str:
        tok = m.group(0)
        core = tok.rstrip(":.")  # "10.1.2.3:8883:" in "connect to 10.1.2.3:8883: timed out"
        if not _is_ip(core):
            return tok
        _host, sep, port = core.rpartition(":")
        keep = sep + port if port.isdigit() and (core.startswith("[") or core.count(":") == 1) else ""
        return "<ip>" + keep + tok[len(core) :]  # the port stays: it is not an identifier

    text = _IP_CAND.sub(one, text)
    return _HOST.sub("<host>", text)


def redact_identifiers(text: str) -> str:
    """`redact_ip` plus every masked serial (`00X…999`) as '<redacted>': the identifier detector applied to free text."""
    return _MASKED.sub("<redacted>", redact_ip(text))


def _longest_first(vals: set[str]) -> list[str]:
    return sorted((v for v in vals if len(v) >= 4), key=len, reverse=True)


def sensitive_values(conf: dict | None = None) -> list[str]:
    """The runtime secrets plus the serials, their masks and the access codes in Studio's config: text no shambu
    output may carry. Only the runtime secrets when the config cannot be read."""
    vals = set(_RUNTIME_SECRETS)
    try:
        c = conf or load()
    except (ConfError, OSError, ValueError):
        return _longest_first(vals)
    serials = {str(k) for k in (c.get("access_code") or {})} | {str(selected_serial(c) or "")}
    codes = {str(v) for v in (c.get("access_code") or {}).values() if v}
    vals |= {s for s in serials if s} | {mask(s) for s in serials if len(s) > 6} | codes
    return _longest_first(vals)


def secret_values(conf: dict | None = None) -> list[str]:
    """The codes only (the runtime secrets plus Studio's codes, no serial and no mask): what `printer list` and
    `printer discover` scrub, because they show the masked serial on purpose."""
    vals = set(_RUNTIME_SECRETS)
    try:
        c = conf or load()
    except (ConfError, OSError, ValueError):
        return _longest_first(vals)
    vals |= {str(v) for v in (c.get("access_code") or {}).values() if v}
    return _longest_first(vals)


def scrub_text(text: str, values: list[str] | None = None) -> str:
    """`text` with every known sensitive value (`sensitive_values`) replaced by '<redacted>'."""
    for v in sensitive_values() if values is None else values:
        text = text.replace(v, "<redacted>")
    return text


def scrub_prints(prints: list[dict], values: list[str] | None = None) -> list[dict]:
    """Calibration prints with any name that holds a known sensitive value set to None."""
    vals = sensitive_values() if values is None else values
    out = []
    for r in prints:
        r = dict(r)
        for k in ("printer_model", "material"):
            if isinstance(r.get(k), str) and any(v in r[k] for v in vals):
                r[k] = None
        out.append(r)
    return out


def _unlink_quietly(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as e:  # cleanup is best effort: the caller's error is the one to report
        log.warning("could not remove the temp file %s: %s", path, e.strerror)


def _write_calibration(p: Path, prints: list[dict]) -> None:
    from .calibrate import CalibrationError, validate_print

    prints = scrub_prints(prints)
    for r in prints:
        try:
            validate_print(r)
        except CalibrationError as e:
            raise ConfError(str(e)) from None  # a fixed text: it never echoes the refused content
    _atomic_write(p, {"version": CALIBRATION_VERSION, "prints": prints}, ".calibration-")


def _atomic_write(p: Path, doc: dict, prefix: str) -> None:
    """Write the already-validated `doc` to `p` as JSON: a temp file named `prefix...` beside it, then `os.replace`.
    DataFileError when the folder or the file cannot be written; the temp file never stays behind."""
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=prefix, suffix=".json")
    except OSError as e:
        raise DataFileError(f"{p.parent}: cannot write there ({e.strerror})") from e
    failure: OSError | None = None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1, allow_nan=False)
            f.write("\n")
        os.replace(tmp, p)
    except OSError as e:
        failure = e
    except BaseException:
        _unlink_quietly(tmp)
        raise
    if failure is not None:
        _unlink_quietly(tmp)
        raise DataFileError(f"{p}: cannot write it ({failure.strerror})") from failure


def save_calibration(prints: list[dict], path: Path | None = None) -> Path:
    """Write the prints atomically (temp file, then `os.replace`). Refuses a print that does not match the schema."""
    p = path or CALIBRATION
    _write_calibration(p, prints)
    return p


def update_calibration(change: Callable[[list[dict]], list[dict]], path: Path | None = None) -> list[dict]:
    """Load, `change`, save, under an exclusive `fcntl.flock` on `calibration.json.lock` beside the file, so two
    `calibrate --add` runs never lose one addition. Returns the saved prints."""
    p = path or CALIBRATION

    def run() -> list[dict]:
        prints = change(_read_calibration(p))
        _write_calibration(p, prints)
        return prints

    return _locked(p, run)


def _locked(p: Path, fn: Callable[[], T]) -> T:
    """`fn()` under an exclusive `fcntl.flock` on `<p>.lock` beside `p` (created when missing), so two shambu
    processes never interleave a load-change-save of the same file. DataFileError when it cannot be locked."""
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        lock = open(p.with_name(p.name + ".lock"), "a")  # closed in the finally below
    except OSError as e:
        raise DataFileError(f"{p}: cannot create its lock file ({e.strerror})") from e
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except OSError as e:
            raise DataFileError(f"{p}: cannot lock it ({e.strerror})") from e
        return fn()
    finally:
        lock.close()  # closing the file releases the lock


def hms_table(serial_prefix: str = "20P", lang: str = "en") -> dict:
    """Offline HMS text: {long_code_hex: intro}, plus {print_error_hex: intro} under key '_errors'."""
    p = APPSUPPORT / "hms" / f"hms_{lang}_{serial_prefix}.json"
    out: dict = {"_errors": {}}
    if not p.is_file():
        return out
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    data = d.get("data", {})
    for e in data.get("device_hms", {}).get(lang, []):
        out[e.get("ecode", "").upper()] = e.get("intro", "")
    for e in data.get("device_error", {}).get(lang, []):
        out["_errors"][e.get("ecode", "").upper()] = e.get("intro", "")
    return out
