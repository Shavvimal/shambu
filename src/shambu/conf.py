"""Read Bambu Studio's own configuration without leaking secrets.

`~/Library/Application Support/BambuStudio/BambuStudio.conf` is plain JSON. It holds the
selected printer serial, the presets last used in the GUI, the bed type index, and the LAN
access code per serial. The access code is returned only by `access_code()` and must never be
printed or written to disk by callers.

shambu's own data file lives beside it: `~/Library/Application Support/shambu/calibration.json`
(`CALIBRATION`), the measured prints `shambu calibrate --add` stores. `load_calibration()` and
`save_calibration()` read and write it (atomic: a temp file, then `os.replace`). It holds a list of
prints (date, printer model, material, Studio total, ratio, flags, log path) and never an IP, a
serial or an access code: `save_calibration` refuses a record with an `ip`, `serial` or secret key.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .errors import ConfError

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


def load_calibration(path: Path | None = None) -> list[dict]:
    """The stored prints; [] when the file does not exist. A file that is not shambu's calibration JSON raises."""
    p = path or CALIBRATION
    if not p.is_file():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ConfError(f"{p} is not readable JSON: {e}") from e
    if not isinstance(d, dict) or not isinstance(d.get("prints"), list):
        raise ConfError(f'{p} is not a shambu calibration file (no "prints" list)')
    return [r for r in d["prints"] if isinstance(r, dict)]


def _identifying_keys(record: dict) -> list[str]:
    # not SECRET_RE: its "code" would match `gcode3mf`
    return [
        k
        for k in record
        if k.lower() in ("ip", "serial", "serial_masked", "printer_id", "access_code")
        or any(s in k.lower() for s in ("token", "ticket", "password", "secret"))
    ]


def save_calibration(prints: list[dict], path: Path | None = None) -> Path:
    """Write the prints atomically. Refuses a record that carries an IP, a serial or a secret-looking key."""
    p = path or CALIBRATION
    for r in prints:
        bad = _identifying_keys(r)
        if bad:
            raise ConfError(f"refusing to store a calibration record with {bad}: the file never holds an IP, a serial or a secret")
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".calibration-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": CALIBRATION_VERSION, "prints": prints}, f, indent=1)
            f.write("\n")
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return p


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
