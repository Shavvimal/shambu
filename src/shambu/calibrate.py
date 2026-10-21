"""Studio's time estimate against measured prints: the M73 track of a G-code, a `printer watch --log` paired with it,
one ratio per print, and the combined ratio per printer model and material.

What it handles: parsing the M73 track of a Bambu plate G-code (per layer, Studio's percent and remaining minutes);
pairing each RUNNING poll of a watch log with Studio's time at that layer; one fit per print (the whole-print ratio
when the log holds both the start and the end, else the mid-print ratio over the logged layers, with the span
stated), a startup offset and flags; combining stored prints per printer model and material into a range or, only
when the data allows it, a point estimate; and the hand-back wording for the build report.

What it does not do: any I/O. It reads no file, no clock and no printer. The CLI reads the log and the
`.gcode.3mf`, and `conf` reads and writes the calibration file. It never fits one ratio across materials or printer
models, and it never guesses a ratio with no measured print.

Design: three pure steps, each testable on fixed data. `parse_m73(lines) -> M73Track`, then
`pairs_from_log(lines, track) -> list[Pair]`, then `fit(pairs, track) -> PrintFit`. `PrintFit.record()` is the
entry the calibration file stores. `combine(records, model, material) -> Fit` and `handback()` read those entries.
Facts behind the rules (research C, 2026-10-21, and reference 06 section 10): Studio counts heat waits and the
calibration stages as 0 s and G29 as 260 s, so the startup is fitted as an offset; the two measured X2D prints ran at
0.751 (bracket D.7, mid-print, speed level not logged) and 1.054 (E7a cradle, mid-print) x Studio, so no single
ratio exists yet, and a point estimate needs n >= 3 prints at Standard speed whose ratios lie within 0.15.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime

STANDARD_SPEED = 2  # spd_lvl 2 = standard (printer.SPEED_LEVELS)
DISAGREE = 0.10  # the printer's remaining time departs from Studio's M73 R by more than this: the D.7 signature
POINT_MIN_N = 3
POINT_MAX_RANGE = 0.15
FINAL_STATES = ("FINISH", "FAILED", "IDLE")

FLAG_SPEED = "speed_not_standard"
FLAG_SPEED_UNKNOWN = "speed_not_logged"
FLAG_DISAGREE = "printer_disagrees"
FLAG_END = "end_not_logged"
FLAG_START = "start_not_logged"
FLAG_FAILED = "print_failed"

_TOTAL = re.compile(r"total estimated time: (?:(\d+)d ?)?(?:(\d+)h ?)?(?:(\d+)m ?)?(?:(\d+)s)?")
_M73_L = re.compile(r"M73 L(\d+)")
_M73_PR = re.compile(r"M73 P(\d+) R(\d+)")
_CONFIG = re.compile(r"; (printer_model|filament_type|filament_settings_id|printer_settings_id|print_settings_id) = (.*)$")
_LAYER_MARKERS = ("; CHANGE_LAYER", ";LAYER_CHANGE", "; LAYER_CHANGE")


class CalibrationError(ValueError):
    """A log or a track cannot be paired: no RUNNING poll, no M73 track, a line that is not JSON."""


# ------------------------------------------------------------------------------------------------ the M73 track
@dataclass
class M73Track:
    """Studio's M73 track of one plate: per layer (1-based) the `(P, R)` in force at the layer's start, from the first
    `M73 P<n> R<m>` after the layer marker (`M73 L<n>`, or `; CHANGE_LAYER` when the file has no `M73 L`)."""

    total_min: float | None
    layers: dict[int, tuple[int, int]]
    printer_model: str | None = None
    material: str | None = None
    filament_settings_id: str | None = None
    printer_settings_id: str | None = None
    print_settings_id: str | None = None

    def remaining_at(self, layer: int) -> float | None:
        """Studio's remaining minutes in the middle of `layer`: the mean of R at its start and at the next layer's
        start (one layer is the resolution of a pair)."""
        a = self.layers.get(layer)
        if a is None:
            return None
        b = self.layers.get(layer + 1, a)
        return (a[1] + b[1]) / 2

    def elapsed_at(self, layer: int) -> float | None:
        r = self.remaining_at(layer)
        return None if r is None or self.total_min is None else self.total_min - r

    def to_json(self) -> dict:
        d = asdict(self)
        d["layers"] = {str(k): list(v) for k, v in self.layers.items()}
        return d

    @classmethod
    def from_json(cls, d: dict) -> M73Track:
        layers = {int(k): (int(v[0]), int(v[1])) for k, v in (d.get("layers") or {}).items()}
        keys = ("printer_model", "material", "filament_settings_id", "printer_settings_id", "print_settings_id")
        return cls(d.get("total_min"), layers, **{k: d.get(k) for k in keys})


def material_key(types: Iterable[str | None]) -> str | None:
    """'PETG' for one material, 'PETG+PLA' for a mix: the key prints are grouped by."""
    got = sorted({str(t).strip().upper() for t in types if t and str(t).strip()})
    return "+".join(got) or None


def _unquote(v: str) -> str:
    return ";".join(x.strip().strip('"') for x in v.split(";"))


def parse_m73(lines: Iterable[str]) -> M73Track:
    """The M73 track and the header facts (total time, printer model, material, preset names) of a plate G-code."""
    total: float | None = None
    by_l: dict[int, tuple[int, int]] = {}
    by_marker: dict[int, tuple[int, int]] = {}
    cfg: dict[str, str] = {}
    pr = (0, 0)
    pending_l = pending_m = None
    markers = 0
    for line in lines:
        if line.startswith("M73 "):
            m = _M73_L.match(line)
            if m:
                pending_l = int(m.group(1))
                by_l[pending_l] = pr
                continue
            m = _M73_PR.match(line)
            if m:
                pr = (int(m.group(1)), int(m.group(2)))
                if pending_l is not None:
                    by_l[pending_l], pending_l = pr, None
                if pending_m is not None:
                    by_marker[pending_m], pending_m = pr, None
        elif line.startswith(";"):
            if line.startswith(_LAYER_MARKERS):
                markers += 1
                pending_m = markers
                by_marker[markers] = pr
            elif total is None and "total estimated time:" in line:
                m = _TOTAL.search(line)
                if m:
                    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
                    total = d * 1440 + h * 60 + mi + s / 60
            else:
                m = _CONFIG.match(line.rstrip("\n"))
                if m and m.group(1) not in cfg:
                    cfg[m.group(1)] = _unquote(m.group(2))
    return M73Track(
        total,
        by_l or by_marker,
        cfg.get("printer_model"),
        material_key(cfg.get("filament_type", "").split(";")),
        cfg.get("filament_settings_id"),
        cfg.get("printer_settings_id"),
        cfg.get("print_settings_id"),
    )


# ------------------------------------------------------------------------------------------------ the log
def _t(iso: str) -> float:
    """'2026-10-21T14:23:55Z' -> epoch seconds."""
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S%z").timestamp()


def read_log(lines: Iterable[str | dict]) -> tuple[dict | None, list[dict]]:
    """A `watch --log` file as (header, records). Blank lines are skipped; a line that is not JSON raises."""
    header, records = None, []
    for i, line in enumerate(lines, 1):
        if isinstance(line, dict):
            rec = line
        else:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise CalibrationError(f"line {i} is not JSON: {e}") from e
        if rec.get("kind") == "header":
            header = header or rec
        else:
            records.append(rec)
    return header, records


@dataclass(frozen=True)
class Pair:
    """One poll against Studio. `kind` is `layer` (a RUNNING poll at a layer of the track), `start` (the first
    RUNNING poll, before layer 1: Studio's time 0) or `end` (the first FINISH poll: Studio's total)."""

    kind: str
    read_at: str
    layer: int | None
    elapsed_min: float  # real minutes since the first RUNNING poll
    studio_elapsed_min: float
    studio_remaining_min: float
    printer_remaining_min: int | None
    speed_level: int | None


def pairs_from_log(lines: Iterable[str | dict], track: M73Track) -> list[Pair]:
    """One pair per usable poll, in log order. Raises CalibrationError when the log has no RUNNING poll or the track
    has no total time."""
    if track.total_min is None:
        raise CalibrationError("the G-code has no '; total estimated time' header: no Studio total to compare with")
    _, records = read_log(lines)
    polls = [r for r in records if "state" in r and r.get("read_at")]
    first = next((i for i, r in enumerate(polls) if r["state"] == "RUNNING"), None)
    if first is None:
        raise CalibrationError("the log has no RUNNING poll")
    t0 = _t(polls[first]["read_at"])
    head = polls[first]
    started = (head.get("layer") or 0) < 1 or any(r["state"] == "PREPARE" for r in polls[:first])
    out: list[Pair] = []
    if started:
        out.append(Pair("start", head["read_at"], 0, 0.0, 0.0, track.total_min, head.get("remaining_min"), head.get("speed_level")))
    for r in polls[first:]:
        el = (_t(r["read_at"]) - t0) / 60
        if r["state"] == "RUNNING":
            layer = r.get("layer")
            rem = track.remaining_at(layer) if layer and layer >= 1 else None
            if rem is not None:
                se = track.total_min - rem
                out.append(Pair("layer", r["read_at"], layer, el, se, rem, r.get("remaining_min"), r.get("speed_level")))
        elif r["state"] in FINAL_STATES:
            if r["state"] == "FINISH":
                out.append(
                    Pair(
                        "end",
                        r["read_at"],
                        r.get("total_layers") or max(track.layers, default=None),
                        el,
                        track.total_min,
                        0.0,
                        0,
                        r.get("speed_level"),
                    )
                )
            break
    return out


# ------------------------------------------------------------------------------------------------ one print
@dataclass
class PrintFit:
    """One print against Studio. `kind` "whole": real start to real end over Studio's total; "span": the logged layers
    only (`layers` names the span). `startup_offset_min` is the real minutes from the first RUNNING poll to the first
    logged layer minus Studio's minutes to that layer (None when the start was not logged)."""

    printer_model: str | None
    material: str | None
    studio_total_min: float
    kind: str
    ratio: float | None
    real_min: float
    studio_min: float
    layers: tuple[int | None, int | None]
    startup_offset_min: float | None
    n_polls: int
    first_read_at: str
    last_read_at: str
    speed_levels: list[int]
    printer_vs_studio: tuple[float, float] | None  # min and max of the printer's remaining / Studio's M73 R
    flags: list[str] = field(default_factory=list)
    filament_settings_id: str | None = None
    print_settings_id: str | None = None

    def record(self, log: str | None = None, gcode3mf: str | None = None, task: str | None = None) -> dict:
        """The calibration-file entry. Holds no IP and no serial."""
        d = asdict(self)
        d["layers"] = list(self.layers)
        d["printer_vs_studio"] = list(self.printer_vs_studio) if self.printer_vs_studio else None
        d["date"] = self.first_read_at[:10]
        d.update(log=log, gcode3mf=gcode3mf, task=task)
        return d

    def lines(self) -> list[str]:
        r = "no ratio (fewer than two usable polls)" if self.ratio is None else f"{self.ratio:.3f} x Studio"
        span = (
            f"whole print ({self.real_min:.1f} min real against Studio {self.studio_total_min:.1f} min)"
            if self.kind == "whole"
            else f"layers {self.layers[0]} to {self.layers[1]} ({self.real_min:.1f} min real against Studio {self.studio_min:.1f} min)"
        )
        out = [f"{self.printer_model or 'printer ?'}, {self.material or 'material ?'}, {self.first_read_at[:10]}: {r} over {span}"]
        out.append(
            f"  Studio total {self.studio_total_min:.1f} min; {self.n_polls} polls"
            + (f"; startup offset {self.startup_offset_min:+.1f} min" if self.startup_offset_min is not None else "")
            + (
                f"; printer remaining / Studio M73 R {self.printer_vs_studio[0]:.2f} to {self.printer_vs_studio[1]:.2f}"
                if self.printer_vs_studio
                else ""
            )
        )
        if self.flags:
            out.append("  flags: " + ", ".join(self.flags))
        return out


def fit(pairs: list[Pair], track: M73Track) -> PrintFit:
    """One print's ratio, startup offset and flags from its pairs (see `PrintFit`)."""
    if not pairs:
        raise CalibrationError("no poll of the log falls on a layer of the G-code's M73 track")
    if track.total_min is None:
        raise CalibrationError("the G-code has no '; total estimated time' header: no Studio total to compare with")
    layers = [p for p in pairs if p.kind == "layer"]
    start = next((p for p in pairs if p.kind == "start"), None)
    end = next((p for p in pairs if p.kind == "end"), None)
    a, b = pairs[0], pairs[-1]
    real, studio = b.elapsed_min - a.elapsed_min, b.studio_elapsed_min - a.studio_elapsed_min
    ratio = round(real / studio, 3) if studio > 0 else None
    flags: list[str] = []
    speeds = sorted({p.speed_level for p in pairs if p.speed_level is not None})
    if any(s != STANDARD_SPEED for s in speeds):
        flags.append(FLAG_SPEED)
    if any(p.speed_level is None for p in layers or pairs):
        flags.append(FLAG_SPEED_UNKNOWN)
    rel = [
        p.printer_remaining_min / p.studio_remaining_min
        for p in layers
        if p.printer_remaining_min is not None and p.studio_remaining_min > 0
    ]
    if any(abs(x - 1) > DISAGREE for x in rel):
        flags.append(FLAG_DISAGREE)
    if start is None:
        flags.append(FLAG_START)
    if end is None:
        flags.append(FLAG_END)
    offset = None
    if start is not None and layers:
        offset = round(layers[0].elapsed_min - layers[0].studio_elapsed_min, 1)
    return PrintFit(
        track.printer_model,
        track.material,
        round(track.total_min, 2),
        "whole" if start is not None and end is not None else "span",
        ratio,
        round(real, 2),
        round(studio, 2),
        (a.layer, b.layer),
        offset,
        len(pairs),
        a.read_at,
        b.read_at,
        speeds,
        (round(min(rel), 3), round(max(rel), 3)) if rel else None,
        flags,
        track.filament_settings_id,
        track.print_settings_id,
    )


# ------------------------------------------------------------------------------------------------ many prints
@dataclass
class Fit:
    """The stored prints of one printer model and material. `point` is set only when n >= 3, every print ran at
    Standard speed (logged, not assumed) and the ratios lie within 0.15 of each other; otherwise the range stands."""

    printer_model: str | None
    material: str | None
    n: int
    ratios: list[float]
    min_ratio: float | None
    max_ratio: float | None
    point: float | None
    why_no_point: str | None
    flags: list[str]

    def lines(self) -> list[str]:
        head = f"{self.printer_model or 'printer ?'}, {self.material or 'material ?'}: n={self.n}"
        if not self.n:
            return [head + ", no measured print"]
        rng = f"{self.min_ratio:.2f}" if self.min_ratio == self.max_ratio else f"{self.min_ratio:.2f} to {self.max_ratio:.2f}"
        out = [head + f", ratios {rng} x Studio" + (f", point estimate {self.point:.2f}" if self.point is not None else "")]
        if self.point is None:
            out.append(f"  no point estimate: {self.why_no_point}")
        if self.flags:
            out.append("  flags seen: " + ", ".join(self.flags))
        return out


def _same(a: str | None, b: str | None) -> bool:
    return (a or "").strip().upper() == (b or "").strip().upper()


def combine(records: Iterable[dict], printer_model: str | None, material: str | None) -> Fit:
    """The calibration-file entries for this printer model and material, combined."""
    mine = [
        r
        for r in records
        if r.get("ratio") is not None and _same(r.get("printer_model"), printer_model) and _same(r.get("material"), material)
    ]
    ratios = [float(r["ratio"]) for r in mine]
    flags = sorted({f for r in mine for f in r.get("flags") or []})
    lo, hi = (min(ratios), max(ratios)) if ratios else (None, None)
    why = None
    if len(mine) < POINT_MIN_N:
        why = f"n={len(mine)}, a point estimate needs n >= {POINT_MIN_N}"
    elif any(FLAG_SPEED in (r.get("flags") or []) or FLAG_SPEED_UNKNOWN in (r.get("flags") or []) for r in mine):
        why = "not every print is logged at Standard speed"
    elif hi is not None and lo is not None and hi - lo >= POINT_MAX_RANGE:
        why = f"the ratios span {hi - lo:.2f}, more than {POINT_MAX_RANGE}"
    point = round(sum(ratios) / len(ratios), 3) if why is None else None
    return Fit(printer_model, material, len(mine), ratios, lo, hi, point, why, flags)


def hm(minutes: float) -> str:
    m = round(minutes)
    return f"{m // 60} h {m % 60} min"


def handback(records: Iterable[dict], printer_model: str | None, material: str | None, studio_s: float) -> str:
    """The text the build report appends to "Studio estimate H h M min": nothing when n=0, the measured range, or a
    calibrated time when `combine` allows a point estimate."""
    f = combine(records, printer_model, material)
    if not f.n or f.min_ratio is None or f.max_ratio is None:
        return ""
    if f.point is not None:
        return f"; calibrated about {hm(studio_s / 60 * f.point)} (n={f.n})"
    rng = f"{f.min_ratio:.2f}" if f.min_ratio == f.max_ratio else f"{f.min_ratio:.2f} to {f.max_ratio:.2f}"
    return f"; measured prints ran at {rng} x Studio (n={f.n})"
