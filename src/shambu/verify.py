"""The GUI round-trip verifier: compare a project (typically one Bambu Studio saved) with the spec it was built from.

`shambu verify FILE --against SPEC` answers "did the GUI keep what shambu built?" after the user's final pass in Studio
(spec 2026-10-29 section 5.6). The expected presets are composed from the spec alone with the resolver, as
`Pipeline.plan` composes them, so no Studio run and no STL file is needed. The comparison then reads the file's
`project_settings.config`, `Project.summary()` and `edits.object_positions()` and gives one `Line` per item that is
not as expected:

- FAIL: a value differs (a setting, a preset id, a colour, a map, a bed type, a sequence, an object setting or
  filament, a plate membership, a position, a missing object), or a key Studio will reset on the next open
  (Preset.cpp:2258-2269 at v02.08.02.61).
- WARN: a key the spec does not set, changed in the GUI (reported with its file value), and bookkeeping that does
  not change a spec value: an `inherits_group` entry that names the base, a project-level `filament_map_mode` the
  plate overrides, an object that cannot be matched without doubt, a filament count that differs between vectors.
- INFO: facts the spec does not decide: tower keys from planning (and the tower keys the tower plan may rewrite),
  object keys added in the GUI, project options in a list (`s_project_options`, PresetBundle.cpp:44), filaments loaded
  from the printer, list keys Studio dropped because they equal the base, objects the spec does not name.

Exit 1 is for a FAIL only: the exit code answers "is the spec honoured", a WARN answers "what did the user change".
Values compare in Studio's string form after a light normalisation (`_scalar_same`): "0.20" equals "0.2", "25%"
equals "25" when only one side carries the sign, true/false equal 1/0, and vectors compare entry by entry.

Studio rules encoded (v02.08.02.61): the slot layout `[0]` process, `[1..N]` filaments, `[N+1]` printer of
`inherits_group` and `different_settings_to_system` (PresetBundle.cpp:3775-3776, :3867, :3885, :3985); the filament
count is the length of `filament_colour` (PresetBundle.cpp:3751-3755); Studio drops a list key whose value equals the
base on save (PresetBundle.cpp:3306-3313); filament slot 1 loads with Always and slots 2..N with
`i == 0 ? Always : any_modified ? Never : OnlyIfModified` (PresetBundle.cpp:3931, :4003-4013), so a modified secondary
filament comes back as a project-embedded copy `<base>(<file>.3mf)` with `inherits_group[i]` = base (Preset.cpp:2332,
:2354-2365, :2401-2439); the plate's `filament_map_mode` wins over the project value (PartPlate.cpp:300-311); the GUI
couples the skin and skeleton infill keys to `sparse_infill_density` and `sparse_infill_line_width`
(Tab.cpp:2106-2120); a plate without `filament_map_mode` metadata is Auto For Flush (PartPlate.cpp:172, metadata
applied over it on load) and an empty plate `filament_maps` falls back to the project map (PartPlate.cpp:289); a
filament's variant block is the run of entries whose `filament_self_index` names it (PresetBundle.cpp:3792-3822,
:3960). The object settings mirror the auto-pin of `Pipeline.plan` (build.py: on a project with two or more
filaments a supported part's "0" support and interface filaments become its own filament).

What this module does not do: it runs no subprocess and prints nothing (cli.py does), it does not slice or compare
G-code, it does not check height ranges, sub-part filaments or the tower position (`shambu inspect` shows them), it
does not compare the settings of a `group-f<n>` object that group_by_filament merged (its members are reported as not
found, WARN), and it changes no file.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field

from . import edits
from . import layout as L
from . import presets as P
from .build import TOWER_KEYS, effective_filaments
from .errors import BuildError, ProjectError
from .spec import PartSpec, Spec
from .threemf import Project

FAIL, WARN, INFO = "FAIL", "WARN", "INFO"
# Tower keys the tower plan writes in its own form or moves (build.py tower planning): a spec value for one of these is
# a request the plan may override, so a difference is INFO
PLAN_MOVES = frozenset({"prime_tower_width", "prime_tower_brim_width", "wipe_tower_x", "wipe_tower_y"})
# Studio's project options (s_project_options, PresetBundle.cpp:44 at v02.08.02.61): saved with the project, not
# part of a preset, so a list naming one is not a GUI edit of the preset
PROJECT_OPTIONS = frozenset(
    {
        "curr_bed_type",
        "filament_map",
        "filament_map_mode",
        "filament_colour",
        "wipe_tower_x",
        "wipe_tower_y",
        "print_sequence",
        "filament_volume_map",
        "filament_nozzle_map",
    }
)
PIN_KEYS = ("support_filament", "support_interface_filament")
POSITION_TOLERANCE_MM = 0.05
# Tab.cpp:2106-2120 at v02.08.02.61: an edit of the first key sets the others to the same value in the GUI
DERIVED_KEYS = {
    "sparse_infill_density": ("skeleton_infill_density", "skin_infill_density"),
    "sparse_infill_line_width": ("skeleton_infill_line_width", "skin_infill_line_width"),
}


@dataclass(frozen=True)
class Line:
    level: str  # FAIL, WARN or INFO
    item: str  # what was compared, e.g. "process wall_loops"
    expected: str | None
    got: str | None
    note: str = ""
    gui_edit: bool = False  # a WARN for a key the file lists that the spec does not set

    def text(self) -> str:
        what = []
        if self.expected is not None:
            what.append(f"expected {self.expected}")
        if self.got is not None:
            what.append(f"got {self.got}")
        body = ", ".join(what)
        return f"{self.level} {self.item}" + (f": {body}" if body else "") + (f" ({self.note})" if self.note else "")

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("gui_edit")
        return d


@dataclass
class Report:
    file: str
    spec: str
    lines: list[Line] = field(default_factory=list)
    checked: int = 0  # items compared, matching ones included

    @property
    def ok(self) -> bool:
        return not any(x.level == FAIL for x in self.lines)

    @property
    def differences(self) -> int:
        return sum(x.level == FAIL for x in self.lines)

    @property
    def gui_edits(self) -> int:
        return sum(x.gui_edit for x in self.lines)

    @property
    def other_warnings(self) -> int:
        return sum(x.level == WARN and not x.gui_edit for x in self.lines)

    def last_line(self) -> str:
        if not self.differences and not self.gui_edits and not self.other_warnings:
            return f"verify: the file matches the spec ({self.checked} items checked)"
        extra = f", {self.other_warnings} other warning(s)" if self.other_warnings else ""
        return f"verify: {self.differences} differences, {self.gui_edits} GUI edits{extra}"

    def to_json(self) -> dict:
        return {"file": self.file, "spec": self.spec, "ok": self.ok, "checked": self.checked, "lines": [x.to_json() for x in self.lines]}


# ---------------------------------------------------------------------------- expected presets
@dataclass(frozen=True)
class Slot:
    """One Studio preset slot as the spec expects it: the base system preset, the composed preset (None for a
    filament loaded from the printer or one the library lacks), the base preset's resolved values, the keys the spec
    sets explicitly, the expected list keys, and why it cannot be composed (`missing`)."""

    kind: str
    base: str
    composed: dict | None
    base_values: Mapping | None = None
    explicit: tuple[str, ...] = ()
    listed: frozenset[str] = frozenset()
    missing: str = ""


def with_derived(settings: Mapping[str, str]) -> dict[str, str]:
    """`settings` plus the keys the GUI couples to them (Tab.cpp:2106-2120) that `settings` does not set itself."""
    out = dict(settings)
    for src, derived in DERIVED_KEYS.items():
        if src in settings:
            for k in derived:
                out.setdefault(k, settings[src])
    return out


def expected_slots(spec: Spec, resolver: P.PresetResolver) -> list[Slot]:
    """The process, each filament and the printer as Studio's slots, composed from the spec as `Pipeline.plan`
    composes them (`for_cli` with the spec's base, `set` and `name`). The expected list of a slot is its explicit keys
    plus every key that differs from the base (`diff_keys`); tower keys come from planning, not from the spec, so they
    are expected only when the spec sets them. A process or printer preset the library lacks is PresetError (the
    environment error); a filament preset the library lacks gives a slot with `missing` set, so the rest still runs."""
    pset = with_derived(spec.process.set)
    process = resolver.for_cli("process", spec.process.base, pset, spec.process.name)
    plisted = (set(pset) | set(resolver.diff_keys("process", spec.process.base, process))) - (set(TOWER_KEYS) - set(spec.process.set))
    slots = [
        Slot("process", spec.process.base, process, resolver.resolve("process", spec.process.base), tuple(sorted(pset)), frozenset(plisted))
    ]
    for f in spec.filaments:
        if f.loaded:
            slots.append(Slot("filament", f.preset, None))
            continue
        try:
            comp = resolver.for_cli("filament", f.preset, f.set, f.name)
            base = resolver.resolve("filament", f.preset)
            listed = frozenset(set(f.set) | set(resolver.diff_keys("filament", f.preset, comp)))
        except P.PresetError as e:
            slots.append(Slot("filament", f.preset, None, missing=str(e)))
            continue
        slots.append(Slot("filament", f.preset, comp, base, tuple(sorted(f.set)), listed))
    machine = resolver.for_cli("machine", spec.printer)
    slots.append(Slot("machine", spec.printer, machine, None, (), frozenset(resolver.diff_keys("machine", spec.printer, machine))))
    return slots


# ---------------------------------------------------------------------------- value helpers
def _norm(v) -> str:
    """A value as one string: a vector joins with commas, as Studio's ini form does."""
    if isinstance(v, (list, tuple)):
        return ",".join(str(x) for x in v)
    return "" if v is None else str(v)


def _vec(ps: Mapping, key: str, n: int) -> list[str]:
    """A per-slot vector padded with "" to `n` entries; a missing key is all empty (Studio pads it the same way on
    load, PresetBundle.cpp:3775-3776)."""
    v = ps.get(key)
    out = [str(x) for x in v] if isinstance(v, list) else ([] if v is None else [str(v)])
    return out + [""] * (n - len(out))


def _keys(entry: str) -> set[str]:
    return {k for k in entry.split(";") if k}


def slot_value(ps: Mapping, key: str, i: int, n: int):
    """Filament `i`'s (0-based) entry of `key` for `n` filaments. A vector as long as `filament_self_index` holds one
    block of variants per filament: the entries whose self index is i + 1 (PresetBundle.cpp:3792-3822, :3960), so
    blocks need not be equal. A vector of `n` entries is one per filament. None when the vector cannot hold slot i."""
    v = ps.get(key)
    if not isinstance(v, list):
        return v
    idx = ps.get("filament_self_index")
    if isinstance(idx, list) and idx and len(v) == len(idx) and len(v) != n:
        block = [x for x, s in zip(v, idx, strict=True) if str(s) == str(i + 1)]
        return block or None
    if len(v) == n:
        return v[i]
    return None


def _scalar(v) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    t = str(v).strip()
    return {"true": "1", "false": "0"}.get(t.lower(), t)


def _scalar_same(a, b) -> bool:
    """One value against one value: equal strings, or equal after dropping a "%" only one side has, or equal numbers
    ("0.20" and "0.2", "35.0" and "35"), with true/false as 1/0."""
    x, y = _scalar(a), _scalar(b)
    if x == y:
        return True
    if x.endswith("%") != y.endswith("%"):
        x, y = x.rstrip("%"), y.rstrip("%")
    try:
        return float(x) == float(y)
    except ValueError:
        return x == y


def _same(expected, got) -> bool:
    """Studio's value on both sides (`_scalar_same` per entry). Vectors compare entry by entry, a comma-joined string
    against a vector of the same length likewise, and a one-entry expectation matches a vector whose entries all
    equal it (a value per nozzle or per variant)."""
    if expected is None or got is None:
        return expected is None and got is None
    el = [str(x) for x in expected] if isinstance(expected, (list, tuple)) else None
    gl = [str(x) for x in got] if isinstance(got, (list, tuple)) else None
    if el is None and gl is None:
        return _scalar_same(expected, got)
    if gl is None:
        gl = str(got).split(",") if el is not None and len(el) > 1 else [str(got)]
    if el is None:
        el = str(expected).split(",") if len(gl) > 1 and "," in str(expected) else [str(expected)]
    if len(el) == 1 and len(gl) > 1:
        return all(_scalar_same(el[0], g) for g in gl)
    if len(gl) == 1 and len(el) > 1:
        return all(_scalar_same(e, gl[0]) for e in el)
    return len(el) == len(gl) and all(_scalar_same(e, g) for e, g in zip(el, gl, strict=True))


# ---------------------------------------------------------------------------- the comparison
def check_project(path: str, spec: Spec, resolver: P.PresetResolver, spec_path: str = "") -> Report:
    """Read the project and compare it with the spec. ProjectError when the file is not a project or its metadata
    does not parse (a plate id that is not a number, a missing attribute), so the CLI gives one error document."""
    try:
        project = Project(str(path))
        ps = project.project_settings
        summary = project.summary()
        positions = edits.object_positions(project)
    except (ValueError, KeyError, TypeError) as e:
        raise ProjectError(f"{path}: malformed project metadata: {type(e).__name__}: {e}") from e
    return compare(spec, expected_slots(spec, resolver), ps, summary, positions, str(path), spec_path)


def compare(
    spec: Spec,
    slots: Sequence[Slot],
    ps: Mapping,
    summary: Mapping,
    positions: Mapping[str, dict],
    file: str = "",
    spec_path: str = "",
) -> Report:
    rep = Report(file, spec_path)
    n_spec = len(spec.filaments)
    n_fil = file_filaments(ps)
    n_ids = len(ps.get("filament_settings_id") or [])
    _add(rep, n_fil == n_spec, Line(FAIL, "filament count", str(n_spec), str(n_fil), "Studio counts filament_colour"))
    if ps.get("filament_colour") and n_ids != n_fil:
        rep.lines.append(Line(WARN, "filament vectors", f"{n_fil} filament_colour", f"{n_ids} filament_settings_id", "the counts disagree"))
    n_slots = max(n_fil, n_spec) + 2
    fids = _vec(ps, "filament_settings_id", max(n_fil, n_spec))
    inherits = _vec(ps, "inherits_group", n_slots)
    lists = _vec(ps, "different_settings_to_system", n_slots)
    _process(rep, slots[0], ps, inherits[0], lists[0], spec)
    for i, slot in enumerate(slots[1:-1]):
        if i < n_fil:  # a slot the file lacks is the filament count FAIL above
            _filament(rep, i, n_fil, slot, ps, fids[i], inherits[i + 1], lists[i + 1], spec)
    _machine(rep, slots[-1], ps, lists[n_fil + 1])
    _maps(rep, spec, slots[-1].composed or {}, ps, summary)
    process = slots[0].composed or {}
    _plates(rep, spec, ps, summary, str(process.get("print_sequence") or "by layer"))
    _objects(rep, spec, summary, positions, process)
    return rep


def file_filaments(ps: Mapping) -> int:
    """The file's filament count as Studio takes it: the length of `filament_colour` (PresetBundle.cpp:3751-3755),
    else of `filament_settings_id`. The printer's list slot is the one after the last filament."""
    cols = ps.get("filament_colour")
    if isinstance(cols, list) and cols:
        return len(cols)
    return len(ps.get("filament_settings_id") or [])


def _add(rep: Report, ok: bool, line: Line) -> None:
    rep.checked += 1
    if not ok:
        rep.lines.append(line)


RESET_NOTE = "not in the file's list: Studio reset it to the base on open (Preset.cpp:2258-2269)"
TOWER_NOTE = "the tower plan sets it; `shambu build --slice` verifies the tower"


def _list(rep: Report, what: str, slot: Slot, entry: str, value_of) -> set[str]:
    """The slot's `different_settings_to_system` entry against the expected keys, compared as key sets. A missing
    explicit key whose value differs is left to the value line (one FAIL per key); the decoded set is returned so
    that line can say why."""
    got = _keys(entry)
    rep.checked += 1
    composed = slot.composed or {}
    base = slot.base_values
    for k in sorted(slot.listed - got):
        want, have = composed.get(k), value_of(k)
        if slot.kind == "process" and (k in TOWER_KEYS or k in PLAN_MOVES):
            # the value line decides; the tower plan writes these after the spec (build.py tower planning)
            rep.lines.append(Line(INFO, f"{what} list {k}", "listed", "not listed", "tower key: the tower plan sets it"))
            continue
        if not _same(want, have):
            if k in slot.explicit:
                continue
            rep.lines.append(Line(FAIL, f"{what} list {k}", _norm(want), _norm(have), RESET_NOTE))
        elif base is not None and k in base and _same(want, base[k]):
            rep.lines.append(Line(INFO, f"{what} list {k}", None, None, "listed by shambu, equal to the base, dropped by Studio on save"))
        else:
            rep.lines.append(
                Line(
                    FAIL,
                    f"{what} list {k}",
                    "listed",
                    "not listed",
                    "the value is right in this file, but Studio resets it to the base on the next open (Preset.cpp:2258-2269)",
                )
            )
    for k in sorted(got - slot.listed):
        if k in PROJECT_OPTIONS:
            rep.lines.append(
                Line(INFO, f"{what} list {k}", None, _norm(value_of(k)), "project option (PresetBundle.cpp:44), not a preset edit")
            )
        elif slot.kind == "process" and (k in TOWER_KEYS or k in PLAN_MOVES):
            rep.lines.append(Line(INFO, f"{what} list {k}", None, _norm(value_of(k)), "tower key from planning"))
        else:
            rep.lines.append(Line(WARN, f"{what} {k}", None, _norm(value_of(k)), "GUI edit: listed in the file, not set by the spec", True))
    return got


def _tower_override(spec: Spec) -> bool:
    """True when any plate's wipe_tower is not the default "auto": planning then rewrites the tower keys."""
    return any(pl.wipe_tower.mode != "auto" for pl in spec.plates)


def _process(rep: Report, slot: Slot, ps: Mapping, inherits: str, entry: str, spec: Spec) -> None:
    pid = str(ps.get("print_settings_id") or "")
    _add(rep, pid == slot.base, Line(FAIL, "process preset id", slot.base, pid, "Studio selects the preset by this name"))
    if inherits:
        level = WARN if inherits == slot.base else FAIL
        note = (
            "names the base itself; Studio resets against the same preset"
            if level == WARN
            else "Studio resets the listed keys against this preset"
        )
        _add(rep, False, Line(level, "process inherits_group[0]", '""', inherits, note))
    else:
        rep.checked += 1
    listed = _list(rep, "process", slot, entry, ps.get)
    composed = slot.composed or {}
    tower_planned = _tower_override(spec)
    for k in slot.explicit:
        want, have = composed.get(k), ps.get(k)
        if k in ps and (k in PLAN_MOVES or (k in TOWER_KEYS and tower_planned)):
            rep.checked += 1
            if not _same(want, have):
                rep.lines.append(Line(INFO, f"process {k}", _norm(want), _norm(have), TOWER_NOTE))
            continue
        note = "missing from the file" if k not in ps else ("" if k in listed else RESET_NOTE)
        _add(rep, k in ps and _same(want, have), Line(FAIL, f"process {k}", _norm(want), None if k not in ps else _norm(have), note))
    for k in TOWER_KEYS:
        if k not in slot.explicit and k in ps:
            rep.lines.append(Line(INFO, f"process {k}", None, _norm(ps[k]), "tower key from planning, not from the spec"))


def _filament(rep: Report, i: int, n: int, slot: Slot, ps: Mapping, fid: str, inherits: str, entry: str, spec: Spec) -> None:
    what = f"filament {i + 1}"
    fspec = spec.filaments[i]
    if slot.composed is None:
        if slot.missing:
            rep.lines.append(Line(WARN, f"{what} preset {slot.base}", None, fid, "not in the Studio library: values not compared"))
        else:
            rep.lines.append(Line(INFO, f"{what} preset id", None, fid, "not checked: loaded from the printer"))
        _colour(rep, what, fspec.colour, ps, i, n)
        return
    if i > 0 and fid != slot.base and inherits == slot.base and fid.startswith(slot.base):
        # Slots 2..N load OnlyIfModified or Never (PresetBundle.cpp:4003-4013): Studio keeps a project-embedded copy.
        # On reopen it resets unlisted keys against `inherits` (Preset.cpp:2258) and recomputes the list against the
        # parent on save (PresetBundle.cpp:3402-3421), so the list and the values are checked as for the base.
        rep.checked += 1
        rep.lines.append(Line(INFO, f"{what} preset id", slot.base, fid, f"project-embedded copy of {slot.base}"))
    else:
        _add(rep, fid == slot.base, Line(FAIL, f"{what} preset id", slot.base, fid, "Studio selects the preset by this name"))
    listed = _list(rep, what, slot, entry, lambda k: slot_value(ps, k, i, n))
    for k in slot.explicit:
        want, have = slot.composed.get(k), slot_value(ps, k, i, n)
        note = "missing from the file" if have is None else ("" if k in listed else RESET_NOTE)
        _add(
            rep, have is not None and _same(want, have), Line(FAIL, f"{what} {k}", _norm(want), None if have is None else _norm(have), note)
        )
    _colour(rep, what, fspec.colour, ps, i, n)


def _colour(rep: Report, what: str, want: str | None, ps: Mapping, i: int, n: int) -> None:
    have = _vec(ps, "filament_colour", n)[i] or None
    if want:
        _add(rep, have is not None and have.upper() == want.upper(), Line(FAIL, f"{what} colour", want, have))
    else:
        rep.lines.append(Line(INFO, f"{what} colour", None, have, "the spec gives no colour"))


def _machine(rep: Report, slot: Slot, ps: Mapping, entry: str) -> None:
    mid = str(ps.get("printer_settings_id") or "")
    _add(rep, mid == slot.base, Line(FAIL, "printer preset id", slot.base, mid, "Studio selects the preset by this name"))
    _list(rep, "printer", slot, entry, ps.get)
    for k in ("machine_start_gcode", "machine_end_gcode", "change_filament_gcode"):
        want, have = str((slot.composed or {}).get(k, "")), str(ps.get(k, ""))
        _add(
            rep,
            want == have,
            Line(FAIL, f"printer {k}", f"{len(want)} chars", f"{len(have)} chars", "differs from the resolved printer preset"),
        )


def _maps(rep: Report, spec: Spec, machine: Mapping, ps: Mapping, summary: Mapping) -> None:
    n = len(spec.filaments)
    n_ext = len(P.machine_facts(dict(machine))["extruders"]) if machine else 2
    want = [str(v) for v in (spec.filament_map or [min(i, n_ext) for i in range(1, n + 1)])]
    applies = bool(spec.filament_map) or n > 1
    got = [str(v) for v in ps.get("filament_map") or []]
    if not applies:
        rep.lines.append(Line(INFO, "filament_map", None, _norm(got), "one filament and no map in the spec"))
        return
    _add(rep, got == want, Line(FAIL, "filament_map", _norm(want), _norm(got)))
    mode = ps.get("filament_map_mode")
    _add(
        rep,
        mode == "Manual",
        Line(WARN, "filament_map_mode", "Manual", str(mode), "project level; the plate's own mode wins (PartPlate.cpp:300-311)"),
    )
    for pl in summary.get("plates", []):
        maps, pmode = pl.get("filament_maps"), pl.get("filament_map_mode")
        if not maps:  # empty or absent: the plate uses the project map (PartPlate.cpp:289), compared above
            rep.lines.append(Line(INFO, f"plate {pl['id']} filament_maps", None, None, "empty on the plate: the project map applies"))
        else:
            _add(rep, maps.split() == want, Line(FAIL, f"plate {pl['id']} filament_maps", " ".join(want), maps))
        if pmode is None:
            _add(
                rep,
                False,
                Line(
                    FAIL,
                    f"plate {pl['id']} filament_map_mode",
                    "Manual",
                    "absent",
                    "Studio treats the plate as Auto For Flush (PartPlate.cpp:172)",
                ),
            )
        else:
            _add(rep, pmode == "Manual", Line(FAIL, f"plate {pl['id']} filament_map_mode", "Manual", pmode))


def _plates(rep: Report, spec: Spec, ps: Mapping, summary: Mapping, proc_seq: str) -> None:
    plates = list(summary.get("plates", []))
    _add(rep, len(plates) == len(spec.plates), Line(FAIL, "plate count", str(len(spec.plates)), str(len(plates))))
    bed = ps.get("curr_bed_type")
    _add(rep, bed == spec.bed_type, Line(FAIL, "bed type", spec.bed_type, str(bed)))
    for pl in plates:
        n = pl["id"]
        bt = pl.get("bed_type")
        if bt is None:
            rep.lines.append(Line(INFO, f"plate {n} bed type", None, None, "not set on the plate: the project bed type applies"))
        else:
            _add(rep, bt == spec.bed_type, Line(FAIL, f"plate {n} bed type", spec.bed_type, bt))
        if 1 <= n <= len(spec.plates):
            want = spec.plates[n - 1].sequence(proc_seq)
            got = pl.get("print_sequence") or str(ps.get("print_sequence") or "by layer")
            _add(rep, want == got, Line(FAIL, f"plate {n} print sequence", want, got))


# ---------------------------------------------------------------------------- objects
@dataclass(frozen=True)
class _Want:
    plate: int
    part: PartSpec
    copy: int  # 0-based copy index of the part
    name: str  # the object name the build gives it (`_unique_key` over the spec order)
    cli_name: str | None  # `<stem>_<n>`: the name it keeps when a rename could not tell it apart (build._name_objects)
    groupable: bool  # a single-file part without `at` on a group_by_filament plate: it may be merged into a group object


def _wanted(spec: Spec) -> list[_Want]:
    """Object names in the order the build allocates them (build._measure: `key`, then `key #2`, ... across plates)."""
    used: set[str] = set()
    out = []
    for pi, pl in enumerate(spec.plates, 1):
        for ps in pl.parts:
            base = ps.key()
            single = not ps.parts
            for c in range(ps.count):
                key, k = base, 2
                while key in used:
                    key, k = f"{base} #{k}", k + 1
                used.add(key)
                cli = f"{ps.files()[0].path.stem}_{c + 1}" if single else None
                out.append(_Want(pi, ps, c, key, cli, pl.group_by_filament and single and ps.at is None))
    return out


def expected_object_settings(part: PartSpec, n_filaments: int, process: Mapping) -> dict[str, str]:
    """The per-object settings the build writes for `part`: its `set` (recipes included) plus the auto-pin.
    Mirrors build.Pipeline.plan's auto-pin (build.py, the `pinned` loop); unify after merge: on a project with two or
    more filaments, a part that needs support gets its own filament for a support or interface filament that is "0"
    or empty (explicitly or through the process), so Studio's default never prints it with the other nozzle."""
    out = {k: str(v) for k, v in part.set.items() if k != "extruder"}
    if n_filaments > 1 and L.needs_support({**process, **part.set}):
        main = _main_filament(part, process)
        for k in PIN_KEYS:
            if str(part.set.get(k, process.get(k, "0"))) in ("0", ""):
                out[k] = str(main)
    return out


def _main_filament(part: PartSpec, process: Mapping) -> int:
    """The object's own filament as build._measure picks it: the first file's, unless overrides replace it."""
    first = part.files()[0].filament
    try:
        eff = effective_filaments(part, None, process)
    except BuildError:
        return first  # a non-integer slot is a spec problem `shambu check` reports; the first file's filament stands
    return first if not eff or first in eff else min(eff)


def _plate_of(summary: Mapping) -> dict[str, list[int]]:
    """Object name -> the plates (1-based ids) whose model instances name it."""
    out: dict[str, list[int]] = {}
    for pl in summary.get("plates", []):
        for name in pl.get("objects") or []:
            out.setdefault(str(name), []).append(int(pl["id"]))
    return out


def _objects(rep: Report, spec: Spec, summary: Mapping, positions: Mapping[str, dict], process: Mapping) -> None:
    objects = list(summary.get("objects", []))
    by_name: dict[str, list[dict]] = {}
    for o in objects:
        by_name.setdefault(str(o.get("name")), []).append(o)
    plates_of = _plate_of(summary)
    groups = any(str(o.get("name", "")).startswith("group-f") for o in objects)
    n_fil = len(spec.filaments)
    matched: set[int] = set()
    for w in _wanted(spec):
        item = f"object {w.name}"
        cands = [o for o in by_name.get(w.name, []) if id(o) not in matched]
        via = w.name
        if not cands and w.cli_name:
            cands = [o for o in by_name.get(w.cli_name, []) if id(o) not in matched]
            via = w.cli_name
        rep.checked += 1
        if not cands:
            if w.groupable and groups:
                rep.lines.append(Line(WARN, item, "present", "not found", "group_by_filament merged it into a group-f<n> object"))
            else:
                rep.lines.append(Line(FAIL, item, "present", "not found"))
            continue
        obj = cands[0]
        matched.add(id(obj))
        ambiguous = len(by_name.get(via, [])) > 1
        if via != w.name:
            rep.lines.append(Line(INFO, item, w.name, via, "keeps the CLI name: a rename could not tell such objects apart"))
        if ambiguous and w.copy == 0:
            rep.lines.append(Line(WARN, item, None, f"{len(by_name[via])} objects named {via}", "ambiguous match: compared on the first"))
        on = plates_of.get(via, [])
        _add(
            rep,
            w.plate in on if ambiguous else on == [w.plate],
            Line(FAIL, f"{item} plate", str(w.plate), ", ".join(str(x) for x in on) or "none"),
        )
        _object_settings(rep, item, w.part, obj, n_fil, process)
        if w.part.at is not None and w.copy == 0:
            _position(rep, item, w, via, ambiguous, positions)
    for o in objects:
        if id(o) not in matched:
            rep.lines.append(
                Line(INFO, f"object {o.get('name')}", None, "in the file", "not in the spec: added in the GUI, or a filament group")
            )


def _object_settings(rep: Report, item: str, part: PartSpec, obj: Mapping, n_fil: int, process: Mapping) -> None:
    have = obj.get("settings") or {}
    want = expected_object_settings(part, n_fil, process)
    for k, v in want.items():
        got = have.get(k)
        note = "not set on the object: the process value applies" if got is None else ""
        if k in PIN_KEYS and k not in part.set:
            note = (note + "; " if note else "") + "the build pins it to the part's filament"
        _add(rep, got is not None and _same(v, got), Line(FAIL, f"{item} {k}", v, got, note))
    if not part.parts:  # a single file: the object prints with its filament or set.extruder (merged objects: per part)
        want_ext = str(part.set.get("extruder", part.filament))
        got_ext = obj.get("extruder")
        _add(rep, got_ext is not None and _same(want_ext, got_ext), Line(FAIL, f"{item} filament", want_ext, got_ext))
    extra = {k: v for k, v in have.items() if k not in want}
    if extra:
        rep.lines.append(Line(INFO, f"{item} settings", None, ", ".join(f"{k}={v}" for k, v in sorted(extra.items())), "added in the GUI"))


def _position(rep: Report, item: str, w: _Want, via: str, ambiguous: bool, positions: Mapping[str, dict]) -> None:
    """`at` is the bbox min corner of copy 1 on its plate (spec.py, build._measure); `object_positions` gives the same
    corner in plate-local mm. With several objects of one name only one position is readable, so a miss there is a
    WARN, not a FAIL."""
    at = w.part.at
    if at is None:
        return
    want = f"x {at[0]:.2f} y {at[1]:.2f}"
    pos = positions.get(via)
    rep.checked += 1
    if pos is None or "error" in pos:
        rep.lines.append(Line(WARN, f"{item} position", want, None, f"position not readable: {(pos or {}).get('error', 'no geometry')}"))
        return
    x, y = pos["min"][0], pos["min"][1]
    if abs(x - at[0]) > POSITION_TOLERANCE_MM or abs(y - at[1]) > POSITION_TOLERANCE_MM:
        level = WARN if ambiguous else FAIL
        note = f"tolerance {POSITION_TOLERANCE_MM} mm" + (
            "; several objects share the name, so this may be another copy" if ambiguous else ""
        )
        rep.lines.append(Line(level, f"{item} position", want, f"x {x:.2f} y {y:.2f}", note))
