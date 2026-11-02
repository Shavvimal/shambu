"""File integrity checks for `shambu inspect` and `shambu verify`: does a `.3mf` or `.gcode.3mf` hold what a
sliced Bambu Studio project holds, and do its parts agree with each other?

What it handles, per plate (reference 03 sections 2.5, 2.8 to 2.10):

- the plate G-code against its `.md5` entry (present, equal, in Studio's form: 32 uppercase hex digits);
- the G-code header layer count (`; total layer number: N`) against the `M73 L` layer markers, and the
  `M991 S0 P` layer notices;
- `slice_info.config`'s `prediction` against the first `M73 P R` line of the G-code;
- the plate thumbnail's PNG header (present, readable, larger than 1 x 1, RGBA);
- the `bed_type` in `plate_N.json` against the plate's (or the project's) bed type;
- every `<warning>` Studio wrote into `slice_info.config`, with its `level` and `error_code`;
- the length of `flush_volumes_matrix`: extruders x n x n (n = filaments). On the two-nozzle X2D the matrix holds one
  n x n block per extruder (edits.py, AddFilament), so it is not a perfect square and is never tested as one.

Every finding is WARN or INFO, never FAIL, and carries its evidence level (findings.py). Facts that need a live X2D
export are unverified: they are labelled external and kept at INFO until a reference records that export. They are
the `.md5` byte form beyond the case, the `M991` notices, the thumbnail size and colour type, and the `plate_N.json`
bed ids (BED_IDS).

What it does not do: it never writes a file, runs no subprocess and prints nothing; it does not judge temperatures,
tool changes or start and end G-code (Studio wrote them); it reads the entries `threemf.Project` already holds.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import struct
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

from .findings import EXTERNAL, HEURISTIC, INFO, MEASURED, OBSERVED, WARN, Finding, Fix, tally
from .threemf import PROJECT_SETTINGS, Project

CHECKS = ("gcode.md5", "gcode.layers", "gcode.prediction", "thumbnails", "bed", "slice_warnings", "flush")
GCODE_CHECKS = ("gcode.md5", "gcode.layers", "gcode.prediction", "thumbnails")
DRIFT_S = 120  # heuristic: a time prediction this far from the G-code's own M73 R is flagged ...
DRIFT_SHARE = 0.05  # ... or this share of it, whichever is larger (the 5 % band calibrate.py uses)
PNG_SIG = b"\x89PNG\r\n\x1a\n"
# Studio's plate name to the id `plate_N.json` stores. Unverified: needs one live X2D export per plate type.
BED_IDS = {
    "Cool Plate": "cool_plate",
    "Engineering Plate": "eng_plate",
    "High Temp Plate": "hot_plate",
    "Textured PEI Plate": "textured_plate",
    "Supertack Plate": "supertack_plate",
}
_PLATE_GCODE = re.compile(r"Metadata/plate_(\d+)\.gcode")
_TOTAL_LAYERS = re.compile(rb"; total layer number: (\d+)")
_M73_L = re.compile(rb"M73 L(\d+)")
_M73_PR = re.compile(rb"M73 P(\d+) R(\d+)")


@dataclass
class GcodeFacts:
    """One pass over a plate G-code: its MD5 and the markers the checks compare."""

    size: int = 0
    md5: str = ""  # uppercase hex
    header_layers: int | None = None
    m73_layers: set[int] = field(default_factory=set)
    m991_lines: int = 0
    first_r_min: int | None = None


def scan_gcode(lines: Iterable[bytes]) -> GcodeFacts:
    """MD5 of the raw bytes and the layer and time markers, line by line (a plate G-code can be 76 MB)."""
    h = hashlib.md5()
    g = GcodeFacts()
    for raw in lines:
        h.update(raw)
        g.size += len(raw)
        if raw.startswith(b"M73 "):
            m = _M73_L.match(raw)
            if m:
                g.m73_layers.add(int(m.group(1)))
            elif g.first_r_min is None and (m := _M73_PR.match(raw)):
                g.first_r_min = int(m.group(2))
        elif raw.startswith(b"M991 S0 P"):
            g.m991_lines += 1
        elif g.header_layers is None and raw.startswith(b";") and (m := _TOTAL_LAYERS.match(raw)):
            g.header_layers = int(m.group(1))
    g.md5 = h.hexdigest().upper()
    return g


def png_header(data: bytes) -> tuple[int, int, int] | None:
    """(width, height, colour type) from a PNG's IHDR chunk, or None when the bytes are not a PNG."""
    if len(data) < 26 or not data.startswith(PNG_SIG) or data[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", data[16:24])
    return w, h, data[25]


@dataclass
class IntegrityReport:
    file: str
    checked: list[str]
    not_applicable: list[str]
    plates: list[dict]
    findings: list[Finding]
    elapsed_s: float = 0.0

    @property
    def counts(self) -> dict[str, int]:
        return tally(self.findings)

    def to_json(self) -> dict:
        return {
            "checked": self.checked,
            "not_applicable": self.not_applicable,
            "plates": self.plates,
            "findings": [f.to_json() for f in self.findings],
            "counts": self.counts,
            "elapsed_s": round(self.elapsed_s, 2),
        }

    def lines(self) -> list[str]:
        c = self.counts
        head = f"integrity: {c['WARN']} WARN, {c['INFO']} INFO ({', '.join(self.checked)})"
        if self.not_applicable:
            head += f"; not applicable (unsliced): {', '.join(self.not_applicable)}"
        return [head] + ["  " + f.text() for f in self.findings]


def check(project: Project) -> IntegrityReport:
    """Every integrity check on a loaded project. The G-code checks run on each sliced plate."""
    start = time.monotonic()
    names = project.names()
    plates = sorted(int(m.group(1)) for n in names if (m := _PLATE_GCODE.fullmatch(n)))
    findings: list[Finding] = []
    facts: list[dict] = []
    info = project.slice_info()
    for p in plates:
        g = scan_gcode(io.BytesIO(project.read(f"Metadata/plate_{p}.gcode")))
        row: dict = {"plate": p, "gcode_bytes": g.size, "md5": g.md5}
        findings += md5_findings(project, p, g, row)
        findings += gcode_findings(p, g, (info.get(p) or {}).get("prediction_s"), row)
        findings += thumbnail_findings(project, p, row)
        facts.append(row)
    findings += bed_findings(project)
    findings += slice_warning_findings(info)
    findings += flush_findings(project)
    findings.sort(key=lambda f: (f.plate or 0, f.code, f.message))
    checked = list(CHECKS) if plates else [c for c in CHECKS if c not in GCODE_CHECKS]
    not_applicable = [] if plates else list(GCODE_CHECKS)
    return IntegrityReport(project.path, checked, not_applicable, facts, findings, time.monotonic() - start)


def md5_findings(project: Project, plate: int, g: GcodeFacts, row: dict) -> list[Finding]:
    name = f"Metadata/plate_{plate}.gcode.md5"
    if not project.has(name):
        row["md5_stored"] = None
        return [
            Finding(
                "gcode.md5_missing",
                WARN,
                "gcode",
                f"{name} is missing; Studio writes it with every sliced plate",
                OBSERVED,
                plate=plate,
                fix=Fix("slice the project again in Studio or with `shambu slice`"),
            )
        ]
    raw = project.read(name)
    stored = raw.decode("ascii", errors="replace")
    row["md5_stored"] = stored.strip()
    row["md5_ok"] = stored.strip().upper() == g.md5
    if not row["md5_ok"]:
        return [
            Finding(
                "gcode.md5_mismatch",
                WARN,
                "gcode",
                f"the G-code MD5 is {g.md5} but {name} holds {stored.strip()!r}: the G-code changed after the slice "
                "(a printer is reported to refuse such a file, not reproduced on X2D)",
                MEASURED,
                plate=plate,
                measure={"expected": g.md5, "got": stored.strip()},
                fix=Fix("slice the project again in Studio or with `shambu slice`"),
            )
        ]
    if raw != g.md5.encode():
        case = stored.strip() != g.md5
        return [
            Finding(
                "gcode.md5_format",
                INFO,
                "gcode",
                f"{name} matches but is not in Studio's form (32 uppercase hex digits, no newline): {stored!r}",
                OBSERVED if case else EXTERNAL,
                plate=plate,
                measure={"bytes": len(raw)},
            )
        ]
    return []


def gcode_findings(plate: int, g: GcodeFacts, prediction_s: int | None, row: dict) -> list[Finding]:
    out: list[Finding] = []
    row.update(
        {
            "header_layers": g.header_layers,
            "m73_layers": max(g.m73_layers) if g.m73_layers else 0,
            "m991_lines": g.m991_lines,
            "prediction_s": prediction_s,
            "first_m73_r_min": g.first_r_min,
        }
    )
    if g.header_layers is None:
        out.append(
            Finding(
                "gcode.no_header",
                WARN,
                "gcode",
                "the G-code has no `; total layer number: N` header line",
                OBSERVED,
                plate=plate,
                fix=Fix("slice the project again; hand-edited G-code loses the header"),
            )
        )
    elif g.m73_layers != set(range(1, g.header_layers + 1)):
        got = len(g.m73_layers)
        out.append(
            Finding(
                "gcode.layer_markers",
                WARN,
                "gcode",
                f"the header says {g.header_layers} layers, the `M73 L` markers number {got} "
                f"(highest {max(g.m73_layers) if g.m73_layers else 0}): the printer's layer count will be off",
                OBSERVED,
                plate=plate,
                measure={"expected": g.header_layers, "got": got},
            )
        )
    if g.m991_lines == 0:
        out.append(
            Finding(
                "gcode.no_layer_notice",
                INFO,
                "gcode",
                "no `M991 S0 P` layer notice lines (reported for other printers; unverified on the X2D)",
                EXTERNAL,
                plate=plate,
            )
        )
    if prediction_s and g.first_r_min is not None:
        gcode_s = g.first_r_min * 60
        diff = abs(prediction_s - gcode_s)
        if diff > max(DRIFT_S, DRIFT_SHARE * prediction_s):
            out.append(
                Finding(
                    "gcode.prediction_drift",
                    WARN,
                    "gcode",
                    f"slice_info predicts {prediction_s} s, the G-code's first M73 R says {gcode_s} s: one of them is stale",
                    HEURISTIC,
                    plate=plate,
                    measure={"expected": prediction_s, "got": gcode_s},
                    fix=Fix("slice the project again"),
                )
            )
    return out


def thumbnail_findings(project: Project, plate: int, row: dict) -> list[Finding]:
    name = f"Metadata/plate_{plate}.png"
    if not project.has(name):
        row["thumbnail"] = None
        return [
            Finding(
                "thumb.missing",
                INFO,
                "thumbnails",
                f"{name} is missing: the printer screen shows no preview (Studio does not need it)",
                OBSERVED,
                plate=plate,
            )
        ]
    hdr = png_header(project.read(name))
    if hdr is None:
        row["thumbnail"] = {"readable": False}
        return [Finding("thumb.unreadable", INFO, "thumbnails", f"{name} is not a PNG", MEASURED, plate=plate)]
    w, h, ct = hdr
    row["thumbnail"] = {"width": w, "height": h, "colour_type": ct}
    out: list[Finding] = []
    if w <= 1 or h <= 1:
        out.append(
            Finding("thumb.placeholder", INFO, "thumbnails", f"{name} is {w} x {h}: a placeholder, not a preview", EXTERNAL, plate=plate)
        )
    if ct != 6:
        out.append(Finding("thumb.not_rgba", INFO, "thumbnails", f"{name} has PNG colour type {ct}, not 6 (RGBA)", EXTERNAL, plate=plate))
    return out


def bed_findings(project: Project) -> list[Finding]:
    out: list[Finding] = []
    ps = project.project_settings
    project_bed = ps.get("curr_bed_type")
    plates = {p["id"]: p for p in project.summary()["plates"]}
    for name in project.names():
        m = re.fullmatch(r"Metadata/plate_(\d+)\.json", name)
        if not m:
            continue
        pid = int(m.group(1))
        try:
            data = json.loads(project.read(name))
        except (ValueError, UnicodeDecodeError) as e:
            out.append(Finding("bed.plate_json", INFO, "bed", f"{name} does not parse as JSON ({e})", MEASURED, plate=pid))
            continue
        got = data.get("bed_type") if isinstance(data, dict) else None
        if not got:
            continue
        want_name = (plates.get(pid) or {}).get("bed_type") or project_bed
        want = BED_IDS.get(want_name or "")
        if got not in BED_IDS.values():
            out.append(
                Finding(
                    "bed.plate_vs_project",
                    INFO,
                    "bed",
                    f"{name} bed_type {got!r} is not a known plate id",
                    EXTERNAL,
                    plate=pid,
                    measure={"got": got},
                )
            )
        elif want is not None and got != want:
            out.append(
                Finding(
                    "bed.plate_vs_project",
                    INFO,
                    "bed",
                    f"{name} says {got!r}, the project says {want_name!r} ({want!r}); the id table is unverified on the X2D",
                    EXTERNAL,
                    plate=pid,
                    measure={"expected": want, "got": got},
                    fix=Fix("slice again after the bed type change"),
                )
            )
    return out


def slice_warning_findings(info: dict[int, dict]) -> list[Finding]:
    out: list[Finding] = []
    for pid in sorted(info):
        for w in info[pid].get("warning_details") or []:
            out.append(
                Finding(
                    "slice.warning",
                    WARN,
                    "slice",
                    f"Studio's slice warning: {w['msg']!r} (level {w.get('level')}, code {w.get('error_code')})",
                    OBSERVED,
                    plate=pid,
                    measure={"msg": w["msg"], "level": w.get("level"), "error_code": w.get("error_code")},
                )
            )
    return out


def flush_findings(project: Project) -> list[Finding]:
    ps = project.project_settings
    fvm = ps.get("flush_volumes_matrix")
    if not isinstance(fvm, list):
        return []
    n = len(ps.get("filament_colour") or [])
    e = project.extruder_count()
    if n == 0 or len(fvm) == e * n * n:
        return []
    return [
        Finding(
            "flush.matrix_shape",
            WARN,
            "flush",
            f"flush_volumes_matrix has {len(fvm)} entries, not {e} extruders x {n} x {n} = {e * n * n} "
            f"({PROJECT_SETTINGS}); a short matrix crashed the CLI slice with exit 139 (reference 02)",
            OBSERVED,
            measure={"expected": e * n * n, "got": len(fvm)},
            fix=Fix("re-add the filament with `shambu patch --add-filament`, or set the filaments again in Studio"),
        )
    ]
