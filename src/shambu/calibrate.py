"""Studio's time estimate against measured prints: the M73 track of a G-code, the polls of a `printer watch --log`
session against it, one fit per print, and the combined fit per printer model and material.

Words: a *poll* is one line of a watch log (one printer read). A *session* is one header line and the polls after
it (one `watch` run). A *print* is one calibration-file entry: the fit of one session.

What it handles: parsing the M73 track of a plate G-code (per layer, Studio's percent and remaining minutes);
reading a watch log into sessions and validating every line (JSON objects, typed fields, timestamps that never go
back, one job per session, layers that never go back); one fit per print: the mid-print rate (real minutes per
Studio minute over the polls at layer 11 and up), the startup offset (the intercept: real minutes from the first
RUNNING poll that the rate does not explain) when the start was logged, the whole-print ratio when both ends were
logged, and flags; the calibration-file schema (`validate_print`); combining prints of one printer model and material
into a range of rates or, only when the prints allow it, a point estimate; and the hand-back text for the report.

What it does not do: any I/O. It reads no file, no clock and no printer. The CLI reads the log and the `.gcode.3mf`,
and `conf` reads and writes the calibration file. It never fits across materials or printer models, and it never
guesses with no measured print.

Design: pure steps, each testable on fixed data. `parse_m73(lines) -> M73Track`; `read_log(lines) -> [Session]`;
`polls_from_session(session, track) -> [Poll]`; `fit(polls, session, track) -> PrintFit`; `PrintFit.to_print()` is the
stored entry, checked by `validate_print`; `combine(prints, model, material) -> Fit`; `handback()`.
A whole-print prediction is `startup offset + rate x Studio minutes`, never `ratio x Studio minutes`, because the
startup is a fixed cost: Studio counts heat waits and the calibration stages as 0 s and G29 as 260 s (reference 06
section 11). The rate comes from layer 11 and up so the first-layer slow-down does not bias it (research C, B4).
The two measured X2D prints ran at rates 0.751 (bracket D.7) and 1.054 (E7a cradle), both with the speed level not
logged, so no single rate exists yet. A point estimate needs n >= 3 finished prints at Standard speed and 100 %,
with the printer agreeing with Studio and no pause, whose rates span less than 0.15.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .errors import BambuError

STANDARD_SPEED = 2  # spd_lvl 2 = standard (printer.SPEED_LEVELS)
STANDARD_PERCENT = 100  # spd_mag at the standard level
# The printer's remaining time departs from Studio's M73 R by more than 5 %: the D.7 signature (research C, B4:
# "when the printer's remaining time departs from M73 R by more than 5 %"). Exactly 5 % is not flagged.
DISAGREE = 0.05
RATE_MIN_LAYER = 11  # the rate uses the polls at this layer and up (research C, B4)
POINT_MIN_N = 3
POINT_MAX_RANGE = 0.15  # a point estimate needs max rate - min rate < 0.15; exactly 0.15 is refused
EPS = 1e-9  # float tolerance at both thresholds
FINAL_STATES = ("FINISH", "FAILED", "IDLE")
WAITING_STATES = ("IDLE", "FINISH", "FAILED")  # `watch --wait-for-start` polls these before a job starts

FLAG_SPEED = "speed_not_standard"
FLAG_SPEED_UNKNOWN = "speed_not_logged"
FLAG_DISAGREE = "printer_disagrees"
FLAG_END = "end_not_logged"
FLAG_START = "start_not_logged"
FLAG_FAILED = "print_failed"
FLAG_PAUSED = "paused"
FLAG_COUNTDOWN = "countdown_not_logged"  # a layer poll without the printer's remaining time: agreement unknown
FLAGS = (FLAG_SPEED, FLAG_SPEED_UNKNOWN, FLAG_DISAGREE, FLAG_END, FLAG_START, FLAG_FAILED, FLAG_PAUSED, FLAG_COUNTDOWN)
# Any of these keeps a print out of a point estimate (start and end must both be logged: a finished whole print).
BLOCKS_POINT = frozenset(FLAGS)

_TOTAL = re.compile(r"total estimated time: (?:(\d+)d ?)?(?:(\d+)h ?)?(?:(\d+)m ?)?(?:(\d+)s)?")
_TOTAL_LAYERS = re.compile(r"; total layer number: (\d+)")
_M73_L = re.compile(r"M73 L(\d+)")
_M73_PR = re.compile(r"M73 P(\d+) R(\d+)")
_LAYER_N = re.compile(r";LAYER:(-?\d+)")  # Cura / Prusa style: 0-based
_CONFIG = re.compile(r"; (printer_model|filament_type|filament_settings_id|printer_settings_id|print_settings_id) = (.*)$")
_LAYER_MARKERS = ("; CHANGE_LAYER", ";LAYER_CHANGE", "; LAYER_CHANGE")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


class CalibrationError(BambuError):
    """A log, a track or a stored print cannot be used: not JSON, a bad field, no RUNNING poll, no M73 track."""


def _finite(x, positive: bool = False, nonneg: bool = False) -> bool:
    """A real, finite number (an int too large for a float is not); `positive` > 0, `nonneg` >= 0."""
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return False
    try:
        if not math.isfinite(x):
            return False
    except OverflowError:
        return False
    return (x > 0) if positive else (x >= 0 if nonneg else True)


# ------------------------------------------------------------------------------------------------ the M73 track
@dataclass
class M73Track:
    """Studio's M73 track of one plate: per layer (1-based) the `(P, R)` in force at the layer's start, from the first
    `M73 P<n> R<m>` after the layer's marker. Markers, in order of preference: `M73 L<n>` (Bambu), `;LAYER:<n>`
    (0-based, so layer n + 1), then unnumbered `; CHANGE_LAYER` / `;LAYER_CHANGE` counted from 1."""

    total_min: float
    layers: dict[int, tuple[int, int]]
    printer_model: str | None = None
    material: str | None = None
    filament_settings_id: str | None = None
    printer_settings_id: str | None = None
    print_settings_id: str | None = None

    def remaining_at(self, layer: int) -> float | None:
        """Studio's remaining minutes in the middle of `layer`: the mean of R at its start and at the next layer's
        start (one layer is the resolution of a poll)."""
        a = self.layers.get(layer)
        if a is None:
            return None
        b = self.layers.get(layer + 1, a)
        return (a[1] + b[1]) / 2

    def to_json(self) -> dict:
        return {
            "total_min": self.total_min,
            "layers": {str(k): list(v) for k, v in self.layers.items()},
            "printer_model": self.printer_model,
            "material": self.material,
            "filament_settings_id": self.filament_settings_id,
            "printer_settings_id": self.printer_settings_id,
            "print_settings_id": self.print_settings_id,
        }

    @classmethod
    def from_json(cls, d) -> M73Track:
        """A track from a log header. Raises CalibrationError for any malformed value."""
        if not isinstance(d, dict) or not _finite(d.get("total_min"), positive=True) or not isinstance(d.get("layers"), dict):
            raise CalibrationError("the header's M73 track is malformed (no positive total_min or no layers)")
        layers: dict[int, tuple[int, int]] = {}
        for k, v in d["layers"].items():
            if not (
                str(k).isdigit()
                and isinstance(v, list)
                and len(v) == 2
                and all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in v)
            ):
                raise CalibrationError("the header's M73 track has a malformed layer entry")
            layers[int(k)] = (v[0], v[1])
        if not layers:
            raise CalibrationError("the header's M73 track has no layers")
        keys = ("printer_model", "material", "filament_settings_id", "printer_settings_id", "print_settings_id")
        if any(d.get(k) is not None and not isinstance(d.get(k), str) for k in keys):
            raise CalibrationError("the header's M73 track has a non-text printer or filament name")
        return cls(float(d["total_min"]), layers, **{k: d.get(k) for k in keys})


def material_key(types: Iterable[str | None]) -> str | None:
    """'PETG' for one material, 'PETG+PLA' for a mix: the key prints are grouped by."""
    got = sorted({str(t).strip().upper() for t in types if t and str(t).strip()})
    return "+".join(got) or None


def _unquote(v: str) -> str:
    return ";".join(x.strip().strip('"') for x in v.split(";"))


def parse_m73(lines: Iterable[str]) -> M73Track:
    """The M73 track and the header facts of a plate G-code. Raises CalibrationError when the G-code has no total
    time, no `M73 P R` line, or unnumbered markers whose count does not match the header's layer count (an unmarked
    first layer would shift every index by one)."""
    total: float | None = None
    total_layers: int | None = None
    found: dict[str, dict[int, tuple[int, int]]] = {"L": {}, "N": {}, "marker": {}}
    pending: dict[str, int | None] = {"L": None, "N": None, "marker": None}
    cfg: dict[str, str] = {}
    pr: tuple[int, int] | None = None
    seen_pr = False
    markers = 0

    def mark(kind: str, n: int) -> None:
        pending[kind] = n
        if pr is not None:
            found[kind][n] = pr  # in force at the marker; the first P R after it replaces it

    for raw in lines:
        line = raw.rstrip("\r\n")
        if line.startswith("M73 "):
            m = _M73_L.match(line)
            if m:
                mark("L", int(m.group(1)))
                continue
            m = _M73_PR.match(line)
            if m:
                pr, seen_pr = (int(m.group(1)), int(m.group(2))), True
                for kind, n in pending.items():
                    if n is not None:
                        found[kind][n] = pr
                        pending[kind] = None
        elif line.startswith(";"):
            m = _LAYER_N.match(line)
            if m:
                mark("N", int(m.group(1)) + 1)
            elif line.startswith(_LAYER_MARKERS):
                markers += 1
                mark("marker", markers)
            elif total is None and "total estimated time:" in line:
                m = _TOTAL.search(line)
                if m and any(m.groups()):
                    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
                    total = d * 1440 + h * 60 + mi + s / 60
            elif total_layers is None and (m := _TOTAL_LAYERS.match(line)):
                total_layers = int(m.group(1))
            else:
                m = _CONFIG.match(line)
                if m and m.group(1) not in cfg:
                    cfg[m.group(1)] = _unquote(m.group(2))
    if not total:
        raise CalibrationError("the G-code has no '; total estimated time' header: no Studio total to compare with")
    if not seen_pr:
        raise CalibrationError("the G-code has no 'M73 P<n> R<m>' line: Studio wrote no time track")
    layers = found["L"] or found["N"]
    if not layers:
        if total_layers is None:
            raise CalibrationError("the G-code has unnumbered layer markers and no '; total layer number' header to check them against")
        if markers != total_layers:
            raise CalibrationError(
                f"the G-code has {markers} unnumbered layer markers for {total_layers} layers: the first layer may be unmarked"
            )
        layers = found["marker"]
    if not layers:
        raise CalibrationError("the G-code has no layer marker with an M73 value after it")
    return M73Track(
        total,
        layers,
        cfg.get("printer_model"),
        material_key(cfg.get("filament_type", "").split(";")),
        cfg.get("filament_settings_id"),
        cfg.get("printer_settings_id"),
        cfg.get("print_settings_id"),
    )


# ------------------------------------------------------------------------------------------------ the log
def _t(iso, where: str) -> float:
    """'2026-10-21T14:23:55Z' -> epoch seconds; CalibrationError naming `where` otherwise."""
    if not isinstance(iso, str) or not _ISO.fullmatch(iso):
        raise CalibrationError(f"{where}: read_at is not a UTC timestamp like 2026-10-21T14:23:55Z")
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except ValueError as e:
        raise CalibrationError(f"{where}: read_at is not a valid date") from e


_POLL_FIELDS = {
    "layer": int,
    "total_layers": int,
    "remaining_min": int,
    "percent": int,
    "speed_level": int,
    "speed_percent": int,
    "stage": int,
}


@dataclass
class Session:
    """One `watch` run in a log: its header (None for polls before any header), its polls with their line numbers,
    and the number of failed reads."""

    header: dict | None
    line: int  # the header's line number (1 when there is none)
    polls: list[tuple[int, dict]] = field(default_factory=list)
    errors: int = 0


def read_log(lines: Iterable[str | dict]) -> list[Session]:
    """A watch log as sessions, one per header line. Every line must be a JSON object; a poll must have a UTC
    `read_at`, a text `state` and integer fields where given; `read_at` never goes back within a session."""
    sessions: list[Session] = []
    last_t: float | None = None
    for i, line in enumerate(lines, 1):
        if isinstance(line, dict):
            rec = line
        else:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise CalibrationError(f"line {i} is not JSON: {e.msg}") from e
        if not isinstance(rec, dict):
            raise CalibrationError(f"line {i} is not a JSON object")
        kind = rec.get("kind")
        if kind == "header":
            sessions.append(Session(rec, i))
            last_t = None
            continue
        if not sessions:
            sessions.append(Session(None, 1))
        s = sessions[-1]
        t = _t(rec.get("read_at"), f"line {i}")
        if last_t is not None and t < last_t:
            raise CalibrationError(f"line {i}: read_at goes back in time (the clock moved); start a new log")
        last_t = t
        if kind == "error" or "error" in rec:
            s.errors += 1
            continue
        if not isinstance(rec.get("state"), str):
            raise CalibrationError(f"line {i}: a poll without a text 'state'")
        for k, typ in _POLL_FIELDS.items():
            v = rec.get(k)
            if v is not None and (not isinstance(v, typ) or isinstance(v, bool)):
                raise CalibrationError(f"line {i}: '{k}' is not an integer")
        if rec.get("task_id") is not None and not isinstance(rec.get("task_id"), str):
            raise CalibrationError(f"line {i}: 'task_id' is not text")
        s.polls.append((i, rec))
    return sessions


@dataclass(frozen=True)
class Poll:
    """One poll against Studio. `kind` is `layer` (a RUNNING poll at a layer of the track), `start` (the first RUNNING
    poll when the log saw the job start: Studio's time 0) or `end` (the FINISH poll: Studio's total).
    `elapsed_min` is the real time since the first RUNNING poll, without the time the print was paused."""

    kind: str
    read_at: str
    layer: int | None
    elapsed_min: float
    studio_elapsed_min: float
    studio_remaining_min: float
    printer_remaining_min: int | None
    speed_level: int | None
    speed_percent: int | None


@dataclass
class _Facts:
    """What the whole session says, before any poll is dropped."""

    started: bool = False
    final: str | None = None
    paused_min: float = 0.0
    speeds: set[int] = field(default_factory=set)
    percents: set[int] = field(default_factory=set)
    speed_unknown: bool = False


def _session_facts(polls: list[tuple[int, dict]]) -> tuple[int | None, _Facts]:
    """Index of the first RUNNING poll, and the session facts. Raises on a changed task_id or a layer that goes back."""
    f = _Facts()
    first = next((k for k, (_, r) in enumerate(polls) if r["state"] == "RUNNING"), None)
    if first is None:
        return None, f
    head = polls[first][1]
    before = [r["state"] for _, r in polls[:first]]
    # start evidence must be observed: a PREPARE poll, or a first RUNNING poll that reports layer 0 (not a missing
    # layer) and is the session's first poll or follows an idle or final state
    early = isinstance(head.get("layer"), int) and head["layer"] < 1
    f.started = "PREPARE" in before or (early and (not before or before[-1] in WAITING_STATES))
    task, layer = None, 0
    for k, (i, r) in enumerate(polls[first:], first):
        st = r["state"]
        if r.get("task_id") is not None:
            if task is not None and r["task_id"] != task:
                raise CalibrationError(f"line {i}: the job changed (another task_id): one watch log session per print")
            task = r["task_id"]
        if st in ("RUNNING", "PAUSE"):
            if (r.get("layer") or 0) < layer:
                raise CalibrationError(f"line {i}: the layer went back from {layer} to {r.get('layer')} (a restart): start a new log")
            layer = max(layer, r.get("layer") or 0)
            if r.get("speed_level") is None or r.get("speed_percent") is None:
                f.speed_unknown = True
        if r.get("speed_level") is not None:
            f.speeds.add(r["speed_level"])
        if r.get("speed_percent") is not None:
            f.percents.add(r["speed_percent"])
        if st == "PAUSE" and k + 1 < len(polls):
            f.paused_min += (_t(polls[k + 1][1]["read_at"], f"line {polls[k + 1][0]}") - _t(r["read_at"], f"line {i}")) / 60
        if st in FINAL_STATES:
            f.final = st
            break
    return first, f


def polls_from_session(session: Session, track: M73Track) -> list[Poll]:
    """The session's polls against the track, in log order. Raises CalibrationError when it has no RUNNING poll."""
    first, facts = _session_facts(session.polls)
    if first is None:
        raise CalibrationError(f"the session at line {session.line} has no RUNNING poll")
    polls = session.polls
    t0 = _t(polls[first][1]["read_at"], "")
    out: list[Poll] = []
    head = polls[first][1]
    if facts.started:
        out.append(
            Poll(
                "start",
                head["read_at"],
                0,
                0.0,
                0.0,
                track.total_min,
                head.get("remaining_min"),
                head.get("speed_level"),
                head.get("speed_percent"),
            )
        )
    paused = 0.0
    for k in range(first, len(polls)):
        _, r = polls[k]
        el = (_t(r["read_at"], "") - t0) / 60 - paused
        if r["state"] == "PAUSE" and k + 1 < len(polls):
            paused += (_t(polls[k + 1][1]["read_at"], "") - _t(r["read_at"], "")) / 60
        if r["state"] == "RUNNING":
            layer = r.get("layer")
            rem = track.remaining_at(layer) if layer and layer >= 1 else None
            if rem is not None:
                out.append(
                    Poll(
                        "layer",
                        r["read_at"],
                        layer,
                        el,
                        track.total_min - rem,
                        rem,
                        r.get("remaining_min"),
                        r.get("speed_level"),
                        r.get("speed_percent"),
                    )
                )
        elif r["state"] in FINAL_STATES:
            if r["state"] == "FINISH":
                last = r.get("total_layers") or max(track.layers)
                out.append(Poll("end", r["read_at"], last, el, track.total_min, 0.0, 0, r.get("speed_level"), r.get("speed_percent")))
            break
    return out


# ------------------------------------------------------------------------------------------------ one print
@dataclass
class PrintFit:
    """One print against Studio.

    `rate`: real minutes per Studio minute between the first and the last poll at layer 11 and up (`rate_layers`).
    `startup_offset_min`: when the start was logged, the real minutes from the first RUNNING poll to the first of
    those polls that the rate does not explain (the intercept). `whole_ratio`: real minutes from the first RUNNING
    poll to FINISH over Studio's total, when both were logged (descriptive only: it mixes startup and rate).
    `kind` is "whole" when both ends were logged, else "span"."""

    printer_model: str | None
    material: str | None
    studio_total_min: float
    kind: str
    rate: float | None
    rate_layers: tuple[int, int] | None
    startup_offset_min: float | None
    whole_ratio: float | None
    real_min: float | None  # first RUNNING poll to FINISH, pauses removed (whole prints)
    paused_min: float
    n_polls: int
    first_read_at: str
    last_read_at: str
    final_state: str | None
    speed_levels: list[int]
    speed_percents: list[int]
    printer_vs_studio: tuple[float, float] | None  # min and max of the printer's remaining / Studio's M73 R
    flags: list[str] = field(default_factory=list)
    filament_settings_id: str | None = None
    print_settings_id: str | None = None

    def to_print(self, session: str) -> dict:
        """The calibration-file entry: the schema's fixed fields (see `validate_print`), `session` the fingerprint of
        the log session (`session_fingerprint`). It stores no file name and no preset label: the grouping values
        (printer model, material) are kept only when `storable_name` accepts them, else they are None."""
        return {
            "session": session,
            "date": self.first_read_at[:10],
            "printer_model": self.printer_model if storable_name(self.printer_model) else None,
            "material": self.material if storable_name(self.material) else None,
            "studio_total_min": self.studio_total_min,
            "kind": self.kind,
            "rate": self.rate,
            "rate_layers": list(self.rate_layers) if self.rate_layers else None,
            "startup_offset_min": self.startup_offset_min,
            "whole_ratio": self.whole_ratio,
            "real_min": self.real_min,
            "paused_min": self.paused_min,
            "n_polls": self.n_polls,
            "first_read_at": self.first_read_at,
            "last_read_at": self.last_read_at,
            "final_state": self.final_state,
            "speed_levels": self.speed_levels,
            "speed_percents": self.speed_percents,
            "printer_vs_studio": list(self.printer_vs_studio) if self.printer_vs_studio else None,
            "flags": self.flags,
        }

    def lines(self) -> list[str]:
        head = f"{self.printer_model or 'printer ?'}, {self.material or 'material ?'}, {self.first_read_at[:10]}: "
        if self.rate is None or self.rate_layers is None:
            head += f"no rate (fewer than two polls at layer {RATE_MIN_LAYER} and up)"
        else:
            head += f"rate {self.rate:.3f} x Studio over layers {self.rate_layers[0]} to {self.rate_layers[1]}"
        out = [head]
        bits = [f"{self.kind} print", f"Studio total {self.studio_total_min:.1f} min", f"{self.n_polls} polls"]
        if self.startup_offset_min is not None:
            bits.append(f"startup offset {self.startup_offset_min:+.1f} min")
        if self.whole_ratio is not None and self.real_min is not None:
            bits.append(f"whole print {self.real_min:.1f} min real ({self.whole_ratio:.3f} x Studio)")
        if self.paused_min:
            bits.append(f"paused {self.paused_min:.1f} min")
        if self.printer_vs_studio:
            bits.append(f"printer remaining / Studio M73 R {self.printer_vs_studio[0]:.2f} to {self.printer_vs_studio[1]:.2f}")
        out.append("  " + "; ".join(bits))
        if self.flags:
            out.append("  flags: " + ", ".join(self.flags))
        return out


def fit(polls: list[Poll], session: Session, track: M73Track) -> PrintFit:
    """One print's rate, startup offset, whole-print ratio and flags (see `PrintFit`). Flags come from every poll of
    the session, including those that fall on no layer of the track."""
    if not polls:
        raise CalibrationError(f"the session at line {session.line}: no poll falls on a layer of the G-code's M73 track")
    _, facts = _session_facts(session.polls)
    layers = [p for p in polls if p.kind == "layer"]
    rated = [p for p in layers if (p.layer or 0) >= RATE_MIN_LAYER]
    start = next((p for p in polls if p.kind == "start"), None)
    end = next((p for p in polls if p.kind == "end"), None)
    rate = offset = whole = real = None
    rate_layers = None
    if len(rated) >= 2 and rated[-1].studio_elapsed_min - rated[0].studio_elapsed_min > EPS:
        a, b = rated[0], rated[-1]
        rate = round((b.elapsed_min - a.elapsed_min) / (b.studio_elapsed_min - a.studio_elapsed_min), 3)
        rate_layers = (a.layer or 0, b.layer or 0)
        if start is not None:
            offset = round(a.elapsed_min - rate * a.studio_elapsed_min, 1)
    if rate is not None and not rate > 0:
        raise CalibrationError(f"the session at line {session.line} gives a rate of {rate}: the log is not usable")
    if start is not None and end is not None:
        real = round(end.elapsed_min, 2)
        whole = round(real / track.total_min, 3)
    flags: list[str] = []
    if any(s != STANDARD_SPEED for s in facts.speeds) or any(p != STANDARD_PERCENT for p in facts.percents):
        flags.append(FLAG_SPEED)
    if facts.speed_unknown:
        flags.append(FLAG_SPEED_UNKNOWN)
    rel = [
        p.printer_remaining_min / p.studio_remaining_min
        for p in layers
        if p.printer_remaining_min is not None and p.studio_remaining_min > 0
    ]
    if any(round(abs(x - 1), 9) > DISAGREE + EPS for x in rel):
        flags.append(FLAG_DISAGREE)
    if not layers or any(p.printer_remaining_min is None for p in layers):
        flags.append(FLAG_COUNTDOWN)
    if start is None:
        flags.append(FLAG_START)
    if facts.final == "FAILED":
        flags.append(FLAG_FAILED)
    if end is None:
        flags.append(FLAG_END)
    if facts.paused_min > 0:
        flags.append(FLAG_PAUSED)
    return PrintFit(
        track.printer_model,
        track.material,
        round(track.total_min, 2),
        "whole" if start is not None and end is not None else "span",
        rate,
        rate_layers,
        offset,
        whole,
        real,
        round(facts.paused_min, 2),
        len(polls),
        polls[0].read_at,
        polls[-1].read_at,
        facts.final,
        sorted(facts.speeds),
        sorted(facts.percents),
        (round(min(rel), 3), round(max(rel), 3)) if rel else None,
        flags,
        track.filament_settings_id,
        track.print_settings_id,
    )


# ------------------------------------------------------------------------------------------------ the stored print
_MASKED = re.compile("\\w{2,}\u2026\\w{2,}|\\w{3}\\.\\.\\.\\w{3}")
_HOST = re.compile(r"(?i)\b[a-z0-9-]+\.(?:local|lan|home|internal|com|net|org|io)\b")
_TOKEN_SPLIT = re.compile(r"[\s,;()\[\]{}<>\"'=/\\]+")
_NAME_FIELDS = ("printer_model", "material")
_SCHEMA: dict[str, tuple] = {
    "session": ("session",),
    "date": ("date",),
    "printer_model": ("name", None),
    "material": ("name", None),
    "studio_total_min": ("pos",),
    "kind": ("kind",),
    "rate": ("pos", None),
    "rate_layers": ("layers", None),
    "startup_offset_min": ("num", None),
    "whole_ratio": ("pos", None),
    "real_min": ("pos", None),
    "paused_min": ("nonneg",),
    "n_polls": ("count",),
    "first_read_at": ("iso",),
    "last_read_at": ("iso",),
    "final_state": ("state", None),
    "speed_levels": ("ints",),
    "speed_percents": ("ints",),
    "printer_vs_studio": ("pair", None),
    "flags": ("flags",),
}
REFUSAL = "a calibration print must match the schema: fixed fields, numbers that are finite, no identifier"


def session_fingerprint(header: dict | None, first_poll: dict, last_poll: dict) -> str:
    """A stable id of one log session, from its content only (the header and the first and last poll), so the same
    session imported under several file names is one print."""
    blob = json.dumps([header, first_poll, last_poll], sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def identifying(s: str) -> bool:
    """True when the text holds an IP address (v4 or v6, by `ipaddress`, with or without a port), a masked serial or
    a LAN host name. Known secrets (the serial, the access code) are the caller's to scrub: `conf.sensitive_values`."""
    if _MASKED.search(s) or _HOST.search(s):
        return True
    for tok in _TOKEN_SPLIT.split(s):
        for cand in (tok, tok.rsplit(":", 1)[0] if tok.count(":") == 1 else tok, tok.split("%", 1)[0]):
            try:
                ipaddress.ip_address(cand)
                return True
            except ValueError:
                continue
    return False


def storable_name(v) -> bool:
    """A display name the file may keep: printable text of 1 to 120 characters (any script, slashes and brackets
    allowed) that names no identifier."""
    return isinstance(v, str) and 0 < len(v) <= 120 and v.isprintable() and not identifying(v)


def _ok(kind: str, v) -> bool:
    if kind == "name":
        return storable_name(v)
    if kind == "session":
        return isinstance(v, str) and bool(re.fullmatch(r"[0-9a-f]{16}", v))
    if kind == "date":
        return isinstance(v, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", v))
    if kind == "iso":
        return isinstance(v, str) and bool(_ISO.fullmatch(v))
    if kind == "pos":
        return _finite(v, positive=True)
    if kind == "num":
        return _finite(v)
    if kind == "nonneg":
        return _finite(v, nonneg=True)
    if kind == "count":
        return isinstance(v, int) and not isinstance(v, bool) and v > 0
    if kind == "kind":
        return v in ("whole", "span")
    if kind == "state":
        return v in FINAL_STATES
    if kind == "ints":
        return isinstance(v, list) and all(isinstance(x, int) and not isinstance(x, bool) for x in v)
    if kind == "layers":
        return isinstance(v, list) and len(v) == 2 and all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in v)
    if kind == "pair":  # the printer's countdown over Studio's: 0 when the printer says 0 min left
        return isinstance(v, list) and len(v) == 2 and all(_finite(x, nonneg=True) for x in v)
    if kind == "flags":
        return isinstance(v, list) and all(x in FLAGS for x in v)
    return False


def validate_print(d) -> None:
    """Raises CalibrationError(REFUSAL) unless `d` holds exactly the schema's fields with valid values: no other key
    (so nothing nested), no identifier in a name, finite numbers. Called when the file is written and read."""
    if not isinstance(d, dict) or set(d) != set(_SCHEMA):
        raise CalibrationError(REFUSAL)
    for k, kinds in _SCHEMA.items():
        v = d[k]
        if v is None and None in kinds:
            continue
        if not _ok(str(kinds[0]), v):
            raise CalibrationError(f"{REFUSAL} (field {k})")


# ------------------------------------------------------------------------------------------------ many prints
@dataclass
class Fit:
    """The stored prints of one printer model and material. `rates` are all their mid-print rates. A point estimate
    (`rate` and `offset_min`) comes only from the prints with no blocking flag (finished, start logged, Standard
    speed at 100 %, the printer agreeing, no pause), when there are at least 3 of them and their rates span less than
    0.15. A prediction is then `offset_min + rate x Studio minutes`."""

    printer_model: str | None
    material: str | None
    n: int
    rates: list[float]
    min_rate: float | None
    max_rate: float | None
    n_eligible: int
    rate: float | None
    offset_min: float | None
    why_no_point: str | None
    flags: list[str]

    def predict_min(self, studio_min: float) -> float | None:
        if self.rate is None or self.offset_min is None:
            return None
        return self.offset_min + self.rate * studio_min

    def lines(self) -> list[str]:
        head = f"{self.printer_model or 'printer ?'}, {self.material or 'material ?'}: n={self.n}"
        if not self.n or self.min_rate is None or self.max_rate is None:
            return [head + ", no measured print with a rate"]
        rng = f"{self.min_rate:.2f}" if self.min_rate == self.max_rate else f"{self.min_rate:.2f} to {self.max_rate:.2f}"
        out = [head + f", mid-print rates {rng} x Studio"]
        if self.rate is not None and self.offset_min is not None:
            out.append(f"  point estimate from {self.n_eligible} prints: {self.offset_min:+.1f} min + {self.rate:.3f} x Studio")
        else:
            out.append(f"  no point estimate: {self.why_no_point}")
        if self.flags:
            out.append("  flags seen: " + ", ".join(self.flags))
        return out


def _same(a: str | None, b: str | None) -> bool:
    return (a or "").strip().upper() == (b or "").strip().upper()


def combine(prints: Iterable[dict], printer_model: str | None, material: str | None) -> Fit:
    """The stored prints of this printer model and material, combined."""
    mine = [
        p
        for p in prints
        if p.get("rate") is not None and _same(p.get("printer_model"), printer_model) and _same(p.get("material"), material)
    ]
    rates = [float(p["rate"]) for p in mine]
    flags = sorted({f for p in mine for f in p.get("flags") or []})
    ok = [
        p
        for p in mine
        if not (set(p.get("flags") or []) & BLOCKS_POINT) and p.get("kind") == "whole" and p.get("startup_offset_min") is not None
    ]
    lo, hi = (min(rates), max(rates)) if rates else (None, None)
    why: str | None = None
    if len(ok) < POINT_MIN_N:
        why = (
            f"{len(ok)} of n={len(mine)} prints are finished, logged from the start at Standard speed and 100 %, with the printer "
            f"agreeing and no pause; a point estimate needs {POINT_MIN_N}"
        )
    else:
        r = [float(p["rate"]) for p in ok]
        if max(r) - min(r) >= POINT_MAX_RANGE - EPS:
            why = f"the eligible rates span {max(r) - min(r):.2f}, and a point estimate needs less than {POINT_MAX_RANGE}"
    rate = offset = None
    if why is None:
        rate = round(sum(float(p["rate"]) for p in ok) / len(ok), 3)
        offset = round(sum(float(p["startup_offset_min"]) for p in ok) / len(ok), 1)
    return Fit(printer_model, material, len(mine), rates, lo, hi, len(ok), rate, offset, why, flags)


def hm(minutes: float) -> str:
    m = round(minutes)
    return f"{m // 60} h {m % 60} min"


def handback(prints: Iterable[dict], printer_model: str | None, material: str | None, studio_s: float) -> str:
    """The text the build report appends to "Studio estimate H h M min": nothing when n=0, the measured range of
    mid-print rates, or a calibrated time (offset + rate x Studio) when `combine` allows a point estimate."""
    f = combine(prints, printer_model, material)
    if not f.n or f.min_rate is None or f.max_rate is None:
        return ""
    pred = f.predict_min(studio_s / 60)
    if pred is not None and math.isfinite(pred) and pred > 0:
        return f"; calibrated about {hm(pred)} (n={f.n_eligible})"
    rng = f"{f.min_rate:.2f}" if f.min_rate == f.max_rate else f"{f.min_rate:.2f} to {f.max_rate:.2f}"
    return f"; measured prints ran at {rng} x Studio (n={f.n})"
