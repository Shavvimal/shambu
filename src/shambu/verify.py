"""The GUI round-trip verifier: compare a project (typically one Bambu Studio saved) with the spec it was built from.

`shambu verify FILE --against SPEC` answers "did the GUI keep what shambu built?" after the user's final pass in Studio
(spec 2026-10-29 section 5.6). The expected presets are composed from the spec alone with the resolver, as
`Pipeline.plan` composes them, so no Studio run and no STL file is needed. The comparison then reads the file's
`project_settings.config`, `Project.summary()` and `edits.object_positions()` and gives one `Line` per item that is
not as expected:

- FAIL: a value differs (a setting, a preset id, a colour, a map, a bed type, a sequence, an object setting, a
  position), or a key Studio will reset on the next open (Preset.cpp:2258-2269 at v02.08.02.61).
- WARN: bookkeeping that does not change a value: a key the file lists that the spec does not set (a GUI edit), an
  `inherits_group` entry that names the base, an object that cannot be matched without doubt.
- INFO: facts the spec does not decide: tower keys from planning, object keys added in the GUI or by the auto-pin,
  filaments loaded from the printer, list keys Studio dropped because they equal the base.

Studio rules encoded (v02.08.02.61): the slot layout `[0]` process, `[1..N]` filaments, `[N+1]` printer of
`inherits_group` and `different_settings_to_system` (PresetBundle.cpp:3775-3776, :3867, :3885, :3990); Studio drops a
list key whose value equals the base on save (PresetBundle.cpp:3306-3313); filament slots 2..N load with
OnlyIfModified, so a modified secondary filament comes back as a project-embedded copy `<base>(<file>.3mf)` with
`inherits_group[i]` = base (PresetBundle.cpp:3855, :3931, :4003-4013; Preset.cpp:2332, :2354-2365, :2401-2439); the
plate's `filament_map_mode` wins over the project value (PartPlate.cpp:300-311); the GUI couples the skin and skeleton
infill keys to `sparse_infill_density` and `sparse_infill_line_width` (Tab.cpp:2105-2117).

What this module does not do: it runs no subprocess and prints nothing (cli.py does), it does not slice or compare
G-code, it does not check height ranges, sub-part filaments or the tower position (`shambu inspect` shows them), and
it changes no file.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field

from . import edits
from . import presets as P
from .build import TOWER_KEYS
from .spec import PartSpec, Spec
from .threemf import Project

FAIL, WARN, INFO = "FAIL", "WARN", "INFO"
POSITION_TOLERANCE_MM = 0.05
# Tab.cpp:2105-2117 at v02.08.02.61: an edit of the first key sets the others to the same value in the GUI
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
    filament loaded from the printer), the keys the spec sets explicitly, and the expected list keys."""

    kind: str
    base: str
    composed: dict | None
    explicit: tuple[str, ...] = ()
    listed: frozenset[str] = frozenset()


def with_derived(settings: Mapping[str, str]) -> dict[str, str]:
    """`settings` plus the keys the GUI couples to them (Tab.cpp:2105-2117) that `settings` does not set itself."""
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
    are expected only when the spec sets them."""
    pset = with_derived(spec.process.set)
    process = resolver.for_cli("process", spec.process.base, pset, spec.process.name)
    plisted = (set(pset) | set(resolver.diff_keys("process", spec.process.base, process))) - (set(TOWER_KEYS) - set(spec.process.set))
    slots = [Slot("process", spec.process.base, process, tuple(sorted(pset)), frozenset(plisted))]
    for f in spec.filaments:
        if f.loaded:
            slots.append(Slot("filament", f.preset, None))
            continue
        comp = resolver.for_cli("filament", f.preset, f.set, f.name)
        slots.append(
            Slot(
                "filament",
                f.preset,
                comp,
                tuple(sorted(f.set)),
                frozenset(set(f.set) | set(resolver.diff_keys("filament", f.preset, comp))),
            )
        )
    machine = resolver.for_cli("machine", spec.printer)
    slots.append(Slot("machine", spec.printer, machine, (), frozenset(resolver.diff_keys("machine", spec.printer, machine))))
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


def _slot_value(v, i: int, n: int):
    """Entry `i` of a filament vector `v` for `n` filaments: one entry per filament, or one block of variants per
    filament (filament_extruder_variant form). None when the vector is too short to hold slot `i`."""
    if not isinstance(v, list):
        return v
    if len(v) == n:
        return v[i]
    if n and len(v) % n == 0:
        b = len(v) // n
        return v[i * b : (i + 1) * b]
    return v[i] if i < len(v) else None


def _same(expected, got) -> bool:
    """Studio's string form on both sides; a one-entry expectation matches a block whose entries all equal it."""
    if isinstance(got, list) and not isinstance(expected, list) and got:
        return all(str(x) == str(expected) for x in got) or _norm(got) == str(expected)
    return _norm(expected) == _norm(got)


# ---------------------------------------------------------------------------- the comparison
def check_project(project: Project, spec: Spec, resolver: P.PresetResolver, spec_path: str = "") -> Report:
    """Read the project and compare it with the spec. ProjectError (from the reads) when the file is not a project."""
    summary = project.summary()
    positions = edits.object_positions(project)
    return compare(spec, expected_slots(spec, resolver), project.project_settings, summary, positions, resolver, project.path, spec_path)


def compare(
    spec: Spec,
    slots: Sequence[Slot],
    ps: Mapping,
    summary: Mapping,
    positions: Mapping[str, dict],
    resolver: P.PresetResolver,
    file: str = "",
    spec_path: str = "",
) -> Report:
    rep = Report(file, spec_path)
    n_fil = len(spec.filaments)
    n_slots = n_fil + 2
    ids = {
        "process": [str(ps.get("print_settings_id") or "")],
        "filament": _vec(ps, "filament_settings_id", n_fil),
        "machine": [str(ps.get("printer_settings_id") or "")],
    }
    inherits = _vec(ps, "inherits_group", n_slots)
    lists = _vec(ps, "different_settings_to_system", n_slots)
    _process(rep, slots[0], ps, ids["process"][0], inherits[0], lists[0], resolver)
    for i, slot in enumerate(slots[1:-1]):
        _filament(rep, i, n_fil, slot, ps, ids["filament"][i], inherits[i + 1], lists[i + 1], spec)
    _machine(rep, slots[-1], ps, ids["machine"][0], lists[n_fil + 1])
    _maps(rep, spec, slots[-1].composed or {}, ps, summary)
    proc_seq = str((slots[0].composed or {}).get("print_sequence") or "by layer")
    _plates(rep, spec, ps, summary, proc_seq)
    _objects(rep, spec, summary, positions)
    return rep


def _add(rep: Report, ok: bool, line: Line) -> None:
    rep.checked += 1
    if not ok:
        rep.lines.append(line)


def _list(rep: Report, what: str, slot: Slot, entry: str, value_of, base_values: Mapping | None) -> set[str]:
    """The slot's `different_settings_to_system` entry against the expected keys, compared as key sets. A missing
    explicit key whose value differs is left to the value line (one FAIL per key); the decoded set is returned so
    that line can say why."""
    got = _keys(entry)
    rep.checked += 1
    composed = slot.composed or {}
    for k in sorted(slot.listed - got):
        want, have = composed.get(k), value_of(k)
        if not _same(want, have):
            if k in slot.explicit:
                continue
            rep.lines.append(
                Line(FAIL, f"{what} list {k}", _norm(want), _norm(have), "not in the file's list: Studio reset it to the base on open")
            )
        elif base_values is not None and k in base_values and _same(want, base_values[k]):
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
        if slot.kind == "process" and k in TOWER_KEYS:
            rep.lines.append(Line(INFO, f"{what} list {k}", None, _norm(value_of(k)), "tower key from planning"))
        else:
            rep.lines.append(Line(WARN, f"{what} {k}", None, _norm(value_of(k)), "GUI edit: listed in the file, not set by the spec", True))
    return got


RESET_NOTE = "not in the file's list: Studio reset it to the base on open (Preset.cpp:2258-2269)"


def _process(rep: Report, slot: Slot, ps: Mapping, pid: str, inherits: str, entry: str, resolver: P.PresetResolver) -> None:
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
    try:
        base_values: Mapping | None = resolver.resolve("process", slot.base)
    except P.PresetError:
        base_values = None  # the id check above already names the base; the list check then cannot tell "dropped"
    listed = _list(rep, "process", slot, entry, ps.get, base_values)
    composed = slot.composed or {}
    for k in slot.explicit:
        want, have = composed.get(k), ps.get(k)
        note = "missing from the file" if k not in ps else ("" if k in listed else RESET_NOTE)
        _add(rep, k in ps and _same(want, have), Line(FAIL, f"process {k}", _norm(want), None if k not in ps else _norm(have), note))
    for k in TOWER_KEYS:
        if k not in slot.explicit and k in ps:
            rep.lines.append(Line(INFO, f"process {k}", None, _norm(ps[k]), "tower key from planning, not from the spec"))


def _filament(rep: Report, i: int, n: int, slot: Slot, ps: Mapping, fid: str, inherits: str, entry: str, spec: Spec) -> None:
    what = f"filament {i + 1}"
    fspec = spec.filaments[i]
    if slot.composed is None:
        rep.lines.append(Line(INFO, f"{what} preset id", None, fid, "not checked (loaded from the printer)"))
        _colour(rep, what, fspec.colour, ps, i)
        return
    embedded = i > 0 and fid != slot.base and inherits == slot.base and fid.startswith(slot.base)
    if embedded:
        rep.checked += 1
        rep.lines.append(
            Line(INFO, f"{what} preset id", slot.base, fid, f"project-embedded copy of {slot.base}; its values are compared below")
        )
    else:
        _add(rep, fid == slot.base, Line(FAIL, f"{what} preset id", slot.base, fid, "Studio selects the preset by this name"))
        listed = _list(rep, what, slot, entry, lambda k: _slot_value(ps.get(k), i, n), None)
    for k in slot.explicit:
        want, have = slot.composed.get(k), _slot_value(ps.get(k), i, n)
        note = "" if embedded or k in listed else RESET_NOTE
        _add(
            rep, have is not None and _same(want, have), Line(FAIL, f"{what} {k}", _norm(want), None if have is None else _norm(have), note)
        )
    _colour(rep, what, fspec.colour, ps, i)


def _colour(rep: Report, what: str, want: str | None, ps: Mapping, i: int) -> None:
    cols = ps.get("filament_colour") or []
    have = str(cols[i]) if i < len(cols) else None
    if want:
        _add(rep, have is not None and have.upper() == want.upper(), Line(FAIL, f"{what} colour", want, have))
    else:
        rep.lines.append(Line(INFO, f"{what} colour", None, have, "the spec gives no colour"))


def _machine(rep: Report, slot: Slot, ps: Mapping, mid: str, entry: str) -> None:
    _add(rep, mid == slot.base, Line(FAIL, "printer preset id", slot.base, mid, "Studio selects the preset by this name"))
    _list(rep, "printer", slot, entry, ps.get, None)
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
        if maps is None:
            rep.lines.append(Line(INFO, f"plate {pl['id']} filament_maps", None, None, "not set on the plate: the project map applies"))
        else:
            _add(rep, maps.split() == want, Line(FAIL, f"plate {pl['id']} filament_maps", " ".join(want), maps))
        if pmode is None:
            _add(rep, mode == "Manual", Line(FAIL, f"plate {pl['id']} filament_map_mode", "Manual", f"{mode} (project)"))
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


@dataclass(frozen=True)
class _Want:
    plate: int
    part: PartSpec
    copy: int  # 0-based copy index of the part
    name: str  # the object name the build gives it (`_unique_key` over the spec order)
    cli_name: str | None  # `<stem>_<n>`: the name it keeps when a rename could not tell it apart (build._name_objects)
    grouped: bool  # a single-file part without `at` on a group_by_filament plate: it may be merged into a group object


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


def _objects(rep: Report, spec: Spec, summary: Mapping, positions: Mapping[str, dict]) -> None:
    objects = list(summary.get("objects", []))
    by_name: dict[str, list[dict]] = {}
    for o in objects:
        by_name.setdefault(str(o.get("name")), []).append(o)
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
            if w.grouped:
                rep.lines.append(Line(WARN, item, "present", "not found", "group_by_filament may have merged it into a group-f<n> object"))
            else:
                rep.lines.append(Line(FAIL, item, "present", "not found"))
            continue
        obj = cands[0]
        matched.add(id(obj))
        if via != w.name:
            rep.lines.append(Line(INFO, item, w.name, via, "keeps the CLI name (a rename could not tell such objects apart)"))
        if len(cands) > 1 and w.copy == 0:
            rep.lines.append(Line(WARN, item, None, f"{len(cands)} objects named {via}", "ambiguous match: settings compared on the first"))
        _object_settings(rep, item, w.part, obj)
        if w.part.at is not None and w.copy == 0:
            _position(rep, item, w, via, len(cands) > 1, positions, cands)
    for o in objects:
        if id(o) not in matched:
            rep.lines.append(
                Line(INFO, f"object {o.get('name')}", None, "in the file", "not in the spec (added in the GUI, or a filament group)")
            )


def _object_settings(rep: Report, item: str, part: PartSpec, obj: Mapping) -> None:
    have = obj.get("settings") or {}
    for k, v in part.set.items():
        got = have.get(k)
        note = "not set on the object: the process value applies" if got is None else ""
        _add(rep, got is not None and str(got) == str(v), Line(FAIL, f"{item} {k}", str(v), got, note))
    extra = {k: v for k, v in have.items() if k not in part.set}
    if extra:
        rep.lines.append(
            Line(
                INFO,
                f"{item} settings",
                None,
                ", ".join(f"{k}={v}" for k, v in sorted(extra.items())),
                "added in the GUI or by the auto-pin",
            )
        )


def _position(rep: Report, item: str, w: _Want, via: str, ambiguous: bool, positions: Mapping[str, dict], cands: list[dict]) -> None:
    """`at` is the bbox min corner of copy 1 on its plate (spec.py, build._measure); `object_positions` gives the same
    corner in plate-local mm. With several objects of one name only one position is readable, so a miss there is a
    WARN, not a FAIL."""
    at = w.part.at
    if at is None:
        return
    want = f"plate {w.plate} x {at[0]:.2f} y {at[1]:.2f}"
    pos = positions.get(via)
    rep.checked += 1
    if pos is None or "error" in pos:
        rep.lines.append(Line(WARN, f"{item} position", want, None, f"position not readable: {(pos or {}).get('error', 'no geometry')}"))
        return
    x, y = pos["min"][0], pos["min"][1]
    got = f"plate {pos['plate']} x {x:.2f} y {y:.2f}"
    ok = pos["plate"] == w.plate and abs(x - at[0]) <= POSITION_TOLERANCE_MM and abs(y - at[1]) <= POSITION_TOLERANCE_MM
    if not ok:
        level = WARN if ambiguous else FAIL
        note = f"tolerance {POSITION_TOLERANCE_MM} mm" + (
            "; several objects share the name, so this may be another copy" if ambiguous else ""
        )
        rep.lines.append(Line(level, f"{item} position", want, got, note))
