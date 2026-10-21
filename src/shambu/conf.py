"""Read Bambu Studio's own configuration without leaking secrets.

`~/Library/Application Support/BambuStudio/BambuStudio.conf` is plain JSON. It holds the
selected printer serial, the presets last used in the GUI, the bed type index, and the LAN
access code per serial. The access code is returned only by `access_code()` and must never be
printed or written to disk by callers.

shambu's own data file lives beside it: `~/Library/Application Support/shambu/calibration.json`
(`CALIBRATION`), the measured prints `shambu calibrate --add` stores. `load_calibration()` and
`save_calibration()` read and write it (atomic: a temp file, then `os.replace`), and
`update_calibration()` does load-change-save under a file lock. It holds a list of prints with the
fixed fields of `calibrate.validate_print` (date, printer model, material, Studio total, rate,
startup offset, flags, the log's file name) and never an IP, a serial, an access code or a path.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from .errors import BambuError, ConfError

APPSUPPORT = Path.home() / "Library/Application Support/BambuStudio"
CONF = APPSUPPORT / "BambuStudio.conf"
CALIBRATION = Path.home() / "Library/Application Support/shambu/calibration.json"
CALIBRATION_VERSION = 1
BED_TYPES = {
    "0": "Default Plate",
    "1": "Cool Plate",
    "2": "Engineering Plate",
    "3": "High Temp Plate",
    "4": "Textured PEI Plate",
    "5": "Supertack Plate",
}
SECRET_RE = ("access", "code", "token", "ticket", "pass", "secret")


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


def _write_calibration(p: Path, prints: list[dict]) -> None:
    from .calibrate import CalibrationError, validate_print

    for r in prints:
        try:
            validate_print(r)
        except CalibrationError as e:
            raise ConfError(str(e)) from None  # a fixed text: it never echoes the refused content
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".calibration-", suffix=".json")
    except OSError as e:
        raise DataFileError(f"{p.parent}: cannot write there ({e.strerror})") from e
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": CALIBRATION_VERSION, "prints": prints}, f, indent=1, allow_nan=False)
            f.write("\n")
        os.replace(tmp, p)
    except OSError as e:
        Path(tmp).unlink(missing_ok=True)
        raise DataFileError(f"{p}: cannot write it ({e.strerror})") from e
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save_calibration(prints: list[dict], path: Path | None = None) -> Path:
    """Write the prints atomically (temp file, then `os.replace`). Refuses a print that does not match the schema."""
    p = path or CALIBRATION
    _write_calibration(p, prints)
    return p


def update_calibration(change: Callable[[list[dict]], list[dict]], path: Path | None = None) -> list[dict]:
    """Load, `change`, save, under an exclusive `fcntl.flock` on `calibration.json.lock` beside the file, so two
    `calibrate --add` runs never lose one addition. Returns the saved prints."""
    p = path or CALIBRATION
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
        prints = change(_read_calibration(p))
        _write_calibration(p, prints)
        return prints
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
