"""The plate spec: typed, validated at the boundary, built from JSON or through `SpecBuilder`.

JSON shape:
{
  "name": "plate-11-jaws",                  # output <outdir>/<name>.3mf
  "outdir": "/abs/dir",
  "printer": "Bambu Lab X2D 0.4 nozzle",
  "process": {"base": "0.20mm Standard @BBL X2D", "name": "PETG functional",
              "recipes": ["petg-functional"],            # named recipes from recipes.py, applied first
              "set": {"wall_loops": "6"}},               # explicit overrides, applied last
  "filaments": [{"preset": "loaded"},                 # the spool in that nozzle (left = 1, right = 2), with its colour
                {"preset": "Bambu PLA Basic @BBL X2D 0.4 nozzle", "colour": "#FFFFFF",
                 "colour_note": "owner wants white on the label whatever is loaded"}],
                                            # a pinned preset must match the spool in that nozzle (check/build FAIL
                                            # otherwise, or --allow-filament-mismatch); a pinned colour without a
                                            # colour_note is replaced by the spool colour. Omitting "preset" = "loaded".
  "bed_type": "Textured PEI Plate",
  "filament_map": [1, 2],                   # extruder per filament slot; default slot n -> extruder n
  "aux_x_limit": 235.5,                     # optional stricter x limit for extruder 2
  "layout": {"gap": 5, "edge": 2},
  "plates": [{"name": "jaws",
              "print_sequence": "by object",           # optional; default "by layer". See layout.SequentialClearance
              "group_by_filament": true,               # by object only: same-filament, same-settings parts print as one object
              "parts": [{"path": "/abs/jaw_print.stl", "filament": 1, "count": 2,
                         "recipes": ["tall-outer-brim", "tree-supports"], "set": {"wall_loops": "8"},
                         "height_ranges": [{"min_z": 0, "max_z": 0.6, "set": {"extruder": "2"}}],
                         "lay_on_face": [0, 0, 1], "rotate": [0, 0, 45],   # optional: turn the STL before placing it
                         # or "orientation": "auto": Studio's auto-orient picks the face (single-file parts; a merged
                         # part uses shambu's own best candidate). Not together with lay_on_face or rotate.
                         "at": [10, 10],                    # optional: the bbox min corner on the bed
                         "support_spread": [21, 0.5, 0.5, 3]},  # optional: mm of support past the box on -x, +x, -y, +y
                                            # (else a solo slice with --probe-supports, else an estimate); "brim_margin": 5.2
                                            # replaces brim and support spread on every side
                        {"name": "label", "parts": [{"path": "/abs/label.stl", "filament": 1},
                                                    {"path": "/abs/text.stl", "filament": 2}]}],
              "pause_at_z": [4.2],
              "colour_changes": [{"z": 10.2, "filament": 2}],
              "wipe_tower": "auto"}]                   # two-filament plates: "auto" (default) finds a free spot both
                                                       # nozzles reach; {"x": 172, "y": 150, "width": 22,
                                                       # "rib_wall": false, "brim": 2} fixes it; false = no tower (WARN).
                                                       # width, rib_wall and brim are one value per project in Studio.
}
Unknown keys are dropped with a warning (`shambu spec` lists them), so a typo such as "brim_margn" shows.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .errors import BuildError
from .mesh import Orientation
from .tower import WIDTH_RANGE, TowerSettings

BED_TYPES = ("Default Plate", "Cool Plate", "Engineering Plate", "High Temp Plate", "Textured PEI Plate", "Supertack Plate")
SUBTYPES = ("normal_part", "negative_part", "modifier_part", "support_enforcer", "support_blocker")
PRINT_SEQUENCES = ("by layer", "by object")
COLOUR_RE = re.compile(r"#[0-9A-Fa-f]{6}")
# The keys each JSON object may hold. Anything else is dropped with a warning (Spec.warnings), never silently.
KNOWN_KEYS = {
    "spec": {"name", "outdir", "printer", "process", "filaments", "plates", "bed_type", "filament_map", "aux_x_limit", "layout",
             "_sweep"},  # _sweep: the provenance `sweep --write-spec` writes
    "process": {"base", "name", "recipes", "set"},
    "filament": {"preset", "colour", "name", "set", "colour_note"},
    "layout": {"gap", "edge"},
    "plate": {"name", "parts", "pause_at_z", "colour_changes", "colour_change", "print_sequence", "group_by_filament", "wipe_tower"},
    "part": {
        "path", "parts", "name", "filament", "count", "subtype", "recipes", "set", "height_ranges", "at", "brim_margin", "support_spread",
        "lay_on_face", "rotate", "orientation",
    },
    "file": {"path", "filament", "subtype"},
    "height_range": {"min_z", "max_z", "set", "range_params"},
    "colour_change": {"z", "filament", "colour"},
    "wipe_tower": {"x", "y", "width", "rib_wall", "brim"},
}  # fmt: skip


def unknown_keys(d, kind: str, where: str) -> list[str]:
    """One warning per key of `d` that a `kind` object does not read."""
    if not isinstance(d, dict):
        return []
    extra = sorted(str(k) for k in d if k not in KNOWN_KEYS[kind])
    return [f"{where}: unknown key {k!r} ignored (known: {', '.join(sorted(KNOWN_KEYS[kind]))})" for k in extra]


def spec_warnings(d: dict) -> list[str]:
    """Every unknown key in a spec document, per object, in document order."""
    out = unknown_keys(d, "spec", "spec")
    out += unknown_keys(d.get("process") or {}, "process", "process")
    out += unknown_keys(d.get("layout") or {}, "layout", "layout")
    for i, f in enumerate(d.get("filaments") or [], 1):
        out += unknown_keys(f, "filament", f"filament {i}")
    for pi, pl in enumerate(d.get("plates") or [], 1):
        if not isinstance(pl, dict):
            continue
        out += unknown_keys(pl, "plate", f"plate {pi}")
        out += unknown_keys(pl.get("wipe_tower"), "wipe_tower", f"plate {pi} wipe_tower")
        for c in pl.get("colour_changes", pl.get("colour_change")) or []:
            out += unknown_keys(c, "colour_change", f"plate {pi} colour change")
        for si, part in enumerate(pl.get("parts") or [], 1):
            if not isinstance(part, dict):
                continue
            out += unknown_keys(part, "part", f"plate {pi} part {si}")
            for fi, f in enumerate(part.get("parts") or [], 1):
                out += unknown_keys(f, "file", f"plate {pi} part {si} file {fi}")
            for r in part.get("height_ranges") or []:
                out += unknown_keys(r, "height_range", f"plate {pi} part {si} height range")
    return out


def scope_warnings(process_recipes: Sequence[str], plates: Sequence[PlateSpec]) -> list[str]:
    """A WARN per recipe listed at the other scope (spec 2026-10-29 section 5.3): the keys still apply, so this never
    errors. A `[process]` recipe on a part is written per object, where the slicer ignores its global keys
    (`recipes.GLOBAL_KEYS`) and its object and region keys reach that object only; an `[object]` recipe in
    process.recipes writes its keys for every object. Names unknown to `recipes.RECIPES` are left to the recipe check."""
    from .recipes import GLOBAL_KEYS, by_name

    known = by_name()
    out = []
    for name in process_recipes:
        r = known.get(name)
        if r is not None and r.scope == "object":
            out.append(f"recipe {name} is [object] scope; in process.recipes its keys apply to every object; list it on the parts instead")
    for pl in plates:
        for part in pl.parts:
            for name in part.recipes:
                r = known.get(name)
                if r is None or r.scope != "process":
                    continue
                try:
                    where = part.key()
                except BuildError:
                    where = "?"
                plate_wide = [k for k in r.set if k in GLOBAL_KEYS]
                if plate_wide:
                    out.append(
                        f"recipe {name} is [process] scope; on part {where} it is written per object, where its plate-wide keys "
                        f"({', '.join(plate_wide)}) are ignored by the slicer; list it in process.recipes"
                    )
                else:
                    out.append(
                        f"recipe {name} is [process] scope; on part {where} it is written per object, so its keys apply to that "
                        "object only; list it in process.recipes"
                    )
    return list(dict.fromkeys(out))


def _strs(d: dict | None) -> dict[str, str]:
    return {str(k): (v if isinstance(v, str) else str(v)) for k, v in (d or {}).items()}


def recipe_ranges(name: str) -> list[HeightRangeSpec]:
    from .recipes import by_name

    r = by_name().get(name)
    return [HeightRangeSpec(float(h["min_z"]), float(h["max_z"]), _strs(h["set"])) for h in (r.height_ranges if r else ())]


def _support_spread(v) -> tuple[float, float, float, float] | None:
    """A part's `support_spread`: four non-negative numbers [mx, px, my, py] (mm past the box on -x, +x, -y, +y)."""
    if v is None:
        return None
    try:
        out = tuple(float(x) for x in v)
    except (TypeError, ValueError):
        out = ()
    if len(out) != 4 or any(x < 0 for x in out) or isinstance(v, (str, dict)):
        raise BuildError(f"support_spread must be four non-negative numbers [mx, px, my, py] in mm, got {v!r}")
    return (out[0], out[1], out[2], out[3])


@dataclass(frozen=True)
class FileRef:
    path: Path
    filament: int = 1
    subtype: str = "normal_part"


@dataclass(frozen=True)
class HeightRangeSpec:
    min_z: float
    max_z: float
    set: dict[str, str]


@dataclass(frozen=True)
class ColourChange:
    z: float
    filament: int = 2
    colour: str = ""


@dataclass(frozen=True)
class PartSpec:
    """One object on a plate: a single file, or several files merged into one object."""

    path: Path | None = None
    parts: tuple[FileRef, ...] = ()
    name: str | None = None
    filament: int = 1
    count: int = 1
    subtype: str = "normal_part"
    recipes: tuple[str, ...] = ()
    set: dict[str, str] = field(default_factory=dict)
    height_ranges: tuple[HeightRangeSpec, ...] = ()
    at: tuple[float, float] | None = None
    brim_margin: float | None = None
    orientation: Orientation = field(default_factory=Orientation)
    support_spread: tuple[float, float, float, float] | None = None  # mm past the box on -x, +x, -y, +y (layout.Spread)

    def files(self) -> list[FileRef]:
        if self.parts:
            return list(self.parts)
        if self.path is None:
            raise BuildError("part needs 'path' or 'parts'")
        return [FileRef(self.path, self.filament, self.subtype)]

    def key(self) -> str:
        return self.name or self.files()[0].path.stem

    @classmethod
    def from_json(cls, d: dict, recipe_sets: dict[str, dict[str, str]] | None = None) -> PartSpec:
        parts = tuple(FileRef(Path(p["path"]), int(p.get("filament", 1)), p.get("subtype", "normal_part")) for p in d.get("parts", []))
        settings: dict[str, str] = {}
        ranges: list[HeightRangeSpec] = []
        for r in d.get("recipes", []):
            if recipe_sets is not None and r not in recipe_sets:
                raise BuildError(f"unknown recipe '{r}'; run `shambu recipes`")
            settings.update((recipe_sets or {}).get(r, {}))
            ranges += recipe_ranges(r)
        settings.update(_strs(d.get("set")))
        for r in d.get("height_ranges", []):
            hr = HeightRangeSpec(float(r["min_z"]), float(r["max_z"]), _strs(r.get("set") or r.get("range_params")))
            if hr not in ranges:  # a reloaded spec.json lists recipe ranges too: keep one copy
                ranges.append(hr)
        return cls(
            Path(d["path"]) if "path" in d else None,
            parts,
            d.get("name"),
            int(d.get("filament", 1)),
            int(d.get("count", 1)),
            d.get("subtype", "normal_part"),
            tuple(d.get("recipes", [])),
            settings,
            tuple(ranges),
            (float(d["at"][0]), float(d["at"][1])) if d.get("at") else None,
            float(d["brim_margin"]) if d.get("brim_margin") is not None else None,
            Orientation.from_json(d),
            _support_spread(d.get("support_spread")),
        )

    def to_json(self) -> dict:
        d: dict[str, Any] = {"filament": self.filament, "count": self.count}
        if self.path:
            d["path"] = str(self.path)
        if self.parts:
            d["parts"] = [{"path": str(f.path), "filament": f.filament, "subtype": f.subtype} for f in self.parts]
        if self.name:
            d["name"] = self.name
        if self.subtype != "normal_part":
            d["subtype"] = self.subtype
        if self.recipes:
            d["recipes"] = list(self.recipes)
        if self.set:
            d["set"] = dict(self.set)
        if self.height_ranges:
            d["height_ranges"] = [{"min_z": r.min_z, "max_z": r.max_z, "set": dict(r.set)} for r in self.height_ranges]
        if self.at:
            d["at"] = list(self.at)
        if self.brim_margin is not None:
            d["brim_margin"] = self.brim_margin
        d.update(self.orientation.to_json())
        if self.support_spread is not None:
            d["support_spread"] = list(self.support_spread)
        return d


def _num_field(v, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise BuildError(f"{what} must be a number, got {v!r}")
    return float(v)


@dataclass(frozen=True)
class WipeTowerSpec:
    """A plate's prime tower. `mode` "auto" (the default for a plate with two or more filaments) lets shambu find a
    free spot both extruders reach; "fixed" puts it at (x, y); "off" writes enable_prime_tower 0 (a WARN line).
    width, rib_wall and brim are None for the process value; Studio keeps one value of each per project."""

    mode: str = "auto"
    x: float | None = None
    y: float | None = None
    width: float | None = None
    rib_wall: bool | None = None
    brim: float | None = None

    @classmethod
    def from_json(cls, v, plate_no: int) -> WipeTowerSpec:
        where = f"plate {plate_no} wipe_tower"
        if v is None or v == "auto" or v is True:
            return cls("auto")
        if v is False:
            return cls("off")
        if not isinstance(v, dict):
            raise BuildError(f'{where} must be "auto", false or {{"x", "y", "width", "rib_wall", "brim"}}, got {v!r}')
        for k in ("x", "y"):
            if k not in v:
                raise BuildError(f"{where} needs '{k}' (or use \"auto\")")
        width = _num_field(v["width"], f"{where} width") if v.get("width") is not None else None
        if width is not None and not WIDTH_RANGE[0] <= width <= WIDTH_RANGE[1]:
            raise BuildError(f"{where} width {width:g} is outside {WIDTH_RANGE[0]:g}..{WIDTH_RANGE[1]:g} mm")
        rib = v.get("rib_wall")
        if rib is not None and not isinstance(rib, bool):
            raise BuildError(f"{where} rib_wall must be true or false, got {rib!r}")
        brim = _num_field(v["brim"], f"{where} brim") if v.get("brim") is not None else None
        if brim is not None and brim < 0:
            raise BuildError(f"{where} brim {brim:g} must be 0 or more (leave it out for Studio's auto brim)")
        return cls("fixed", _num_field(v["x"], f"{where} x"), _num_field(v["y"], f"{where} y"), width, rib, brim)

    def to_json(self):
        if self.mode == "auto":
            return "auto"
        if self.mode == "off":
            return False
        d: dict[str, Any] = {"x": self.x, "y": self.y}
        for k in ("width", "rib_wall", "brim"):
            if getattr(self, k) is not None:
                d[k] = getattr(self, k)
        return d

    def settings(self, base: TowerSettings) -> TowerSettings:
        """The process settings with this spec's width, rib wall and brim on top."""
        return replace(
            base,
            width=self.width if self.width is not None else base.width,
            rib_wall=self.rib_wall if self.rib_wall is not None else base.rib_wall,
            brim=self.brim if self.brim is not None else base.brim,
        )


@dataclass(frozen=True)
class PlateSpec:
    name: str
    parts: tuple[PartSpec, ...] = ()
    pause_at_z: tuple[float, ...] = ()
    colour_changes: tuple[ColourChange, ...] = ()
    print_sequence: str | None = None  # None = the process preset's print_sequence (by layer unless the user set it)
    group_by_filament: bool = False  # by object: merge single-file parts with one filament and identical settings into one object
    wipe_tower: WipeTowerSpec = field(default_factory=WipeTowerSpec)  # used only when the plate prints two or more filaments

    @property
    def by_object(self) -> bool:
        return self.print_sequence == "by object"

    def sequence(self, process_default: str = "by layer") -> str:
        """The effective print sequence: the plate's own value, else the process value (reference 08 section 2)."""
        return self.print_sequence or (process_default or "by layer")

    @classmethod
    def from_json(cls, d: dict, i: int, recipe_sets=None) -> PlateSpec:
        return cls(
            d.get("name") or f"plate {i}",
            tuple(PartSpec.from_json(p, recipe_sets) for p in d.get("parts", [])),
            tuple(float(z) for z in d.get("pause_at_z", [])),
            tuple(
                ColourChange(float(c["z"]), int(c.get("filament", 2)), c.get("colour", ""))
                for c in d.get("colour_changes", d.get("colour_change", []))
            ),
            str(d["print_sequence"]).replace("_", " ") if d.get("print_sequence") else None,
            bool(d.get("group_by_filament", False)),
            WipeTowerSpec.from_json(d.get("wipe_tower"), i),
        )

    def to_json(self) -> dict:
        d: dict[str, Any] = {"name": self.name, "parts": [p.to_json() for p in self.parts]}
        if self.print_sequence:
            d["print_sequence"] = self.print_sequence
        if self.group_by_filament:
            d["group_by_filament"] = True
        if self.wipe_tower.mode != "auto":
            d["wipe_tower"] = self.wipe_tower.to_json()
        if self.pause_at_z:
            d["pause_at_z"] = list(self.pause_at_z)
        if self.colour_changes:
            d["colour_changes"] = [{"z": c.z, "filament": c.filament, "colour": c.colour} for c in self.colour_changes]
        return d


@dataclass(frozen=True)
class ProcessSpec:
    base: str
    name: str | None = None
    recipes: tuple[str, ...] = ()
    set: dict[str, str] = field(default_factory=dict)


LOADED = "loaded"  # `preset` value meaning "whatever spool the printer holds in this nozzle"


@dataclass(frozen=True)
class FilamentSpec:
    preset: str = LOADED
    colour: str | None = None
    name: str | None = None
    set: dict[str, str] = field(default_factory=dict)
    colour_note: str | None = None  # why the spec's colour stands even when the spool's colour differs

    @property
    def loaded(self) -> bool:
        """True when the preset is left to the printer (`"loaded"`, empty or omitted)."""
        return not self.preset or self.preset.strip().lower() == LOADED


@dataclass(frozen=True)
class LayoutSpec:
    gap: float = 5.0
    edge: float = 2.0


@dataclass(frozen=True)
class Spec:
    name: str
    outdir: Path
    printer: str
    process: ProcessSpec
    filaments: tuple[FilamentSpec, ...]
    plates: tuple[PlateSpec, ...]
    bed_type: str = "Textured PEI Plate"
    filament_map: tuple[int, ...] | None = None
    aux_x_limit: float | None = None
    layout: LayoutSpec = LayoutSpec()
    warnings: tuple[str, ...] = field(default=(), compare=False)  # unknown keys dropped while parsing

    @classmethod
    def from_json(cls, d: dict, recipe_sets: dict[str, dict[str, str]] | None = None) -> Spec:
        for req in ("name", "outdir", "printer", "process", "filaments", "plates"):
            if req not in d:
                raise BuildError(f"spec lacks '{req}'")
        if recipe_sets is None:
            from .recipes import recipe_settings

            recipe_sets = recipe_settings()
        pr = d["process"]
        pset: dict[str, str] = {}
        for r in pr.get("recipes", []):
            if r not in recipe_sets:
                raise BuildError(f"unknown recipe '{r}'; run `shambu recipes`")
            pset.update(recipe_sets[r])
        pset.update(_strs(pr.get("set")))
        lay = d.get("layout", {})
        # Different tower sizes on two plates are a plan problem, compared after resolving against the process
        plates = tuple(PlateSpec.from_json(p, i, recipe_sets) for i, p in enumerate(d["plates"], 1))
        return cls(
            str(d["name"]),
            Path(d["outdir"]),
            str(d["printer"]),
            ProcessSpec(pr["base"], pr.get("name"), tuple(pr.get("recipes", [])), pset),
            tuple(
                FilamentSpec(str(f.get("preset") or LOADED), f.get("colour"), f.get("name"), _strs(f.get("set")), f.get("colour_note"))
                for f in d["filaments"]
            ),
            plates,
            d.get("bed_type", "Textured PEI Plate"),
            tuple(int(x) for x in d["filament_map"]) if d.get("filament_map") else None,
            float(d["aux_x_limit"]) if d.get("aux_x_limit") is not None else None,
            LayoutSpec(float(lay.get("gap", 5.0)), float(lay.get("edge", 2.0))),
            tuple(spec_warnings(d) + scope_warnings(tuple(pr.get("recipes", [])), plates)),
        )

    def to_json(self) -> dict:
        d: dict[str, Any] = {
            "name": self.name,
            "outdir": str(self.outdir),
            "printer": self.printer,
            "process": {
                "base": self.process.base,
                "name": self.process.name,
                "recipes": list(self.process.recipes),
                "set": dict(self.process.set),
            },
            "filaments": [
                {
                    "preset": f.preset,
                    "colour": f.colour,
                    "name": f.name,
                    "set": dict(f.set),
                    **({"colour_note": f.colour_note} if f.colour_note else {}),
                }
                for f in self.filaments
            ],
            "bed_type": self.bed_type,
            "layout": {"gap": self.layout.gap, "edge": self.layout.edge},
            "plates": [p.to_json() for p in self.plates],
        }
        if self.filament_map:
            d["filament_map"] = list(self.filament_map)
        if self.aux_x_limit is not None:
            d["aux_x_limit"] = self.aux_x_limit
        return d

    def validate(self) -> list[str]:
        """Boundary checks that need no file system or presets."""
        errs = []
        if not self.name or "/" in self.name:
            errs.append("name must be a bare file stem")
        if self.bed_type not in BED_TYPES:
            errs.append(f"bed_type {self.bed_type!r} not in {BED_TYPES}")
        if not self.filaments:
            errs.append("at least one filament")
        for i, fs in enumerate(self.filaments, 1):
            if fs.colour is not None and not COLOUR_RE.fullmatch(fs.colour):
                errs.append(f"filament {i}: colour {fs.colour!r} is not #RRGGBB")
        if not self.plates:
            errs.append("at least one plate")
        n = len(self.filaments)
        for pi, pl in enumerate(self.plates, 1):
            if not pl.parts:
                errs.append(f"plate {pi}: no parts")
            if pl.print_sequence is not None and pl.print_sequence not in PRINT_SEQUENCES:
                errs.append(f"plate {pi}: print_sequence {pl.print_sequence!r} not in {PRINT_SEQUENCES}")
            if pl.group_by_filament and pl.print_sequence == "by layer":
                errs.append(f"plate {pi}: group_by_filament needs print_sequence 'by object' (by layer already prints every part together)")
            for si, part in enumerate(pl.parts, 1):
                try:
                    files = part.files()
                except BuildError as e:
                    errs.append(f"plate {pi} part {si}: {e}")
                    continue
                for f in files:
                    if not 1 <= f.filament <= n:
                        errs.append(f"plate {pi} part {si}: filament {f.filament} outside 1..{n}")
                    if f.subtype not in SUBTYPES:
                        errs.append(f"plate {pi} part {si}: subtype {f.subtype!r} not in {SUBTYPES}")
                if part.count < 1:
                    errs.append(f"plate {pi} part {si}: count must be >= 1")
                o = part.orientation
                if o.auto and (o.lay_on_face is not None or any(abs(a) > 1e-9 for a in o.rotate)):
                    errs.append(
                        f"plate {pi} part {si}: orientation 'auto' lets Studio choose the turn; remove lay_on_face and rotate, or drop 'auto'"
                    )
                if part.parts and part.height_ranges:
                    pass  # allowed: ranges go to assembled_params
                for r in part.height_ranges:
                    if r.max_z <= r.min_z:
                        errs.append(f"plate {pi} part {si}: height range {r.min_z}..{r.max_z} is empty")
            for c in pl.colour_changes:
                if not 1 <= c.filament <= n:
                    errs.append(f"plate {pi}: colour change to filament {c.filament} outside 1..{n}")
        return errs


class SpecBuilder:
    """Fluent Builder for programmatic specs (tests, other skills)."""

    def __init__(self, name: str, outdir: str | Path, printer: str = "Bambu Lab X2D 0.4 nozzle"):
        self._name, self._outdir, self._printer = name, Path(outdir), printer
        self._process = ProcessSpec("0.20mm Standard @BBL X2D")
        self._recipes: list[str] = []
        self._filaments: list[FilamentSpec] = []
        self._plates: list[PlateSpec] = []
        self._bed = "Textured PEI Plate"
        self._map: tuple[int, ...] | None = None
        self._aux: float | None = None
        self._layout = LayoutSpec()

    def process(self, base: str, name: str | None = None, **settings: str) -> SpecBuilder:
        self._process = ProcessSpec(base, name, (), _strs(settings))
        return self

    def recipe(self, *names: str) -> SpecBuilder:
        """Recipes apply first; explicit `process(...)` settings win over them, as in `Spec.from_json`."""
        from .recipes import recipe_settings

        sets = recipe_settings()
        for n in names:
            if n not in sets:
                raise BuildError(f"unknown recipe '{n}'")
        self._recipes += names
        return self

    def filament(self, preset: str, colour: str | None = None, **settings: str) -> SpecBuilder:
        self._filaments.append(FilamentSpec(preset, colour, None, _strs(settings)))
        return self

    def bed(self, bed_type: str) -> SpecBuilder:
        self._bed = bed_type
        return self

    def filament_map(self, *extruders: int) -> SpecBuilder:
        self._map = tuple(extruders)
        return self

    def aux_x_limit(self, x: float) -> SpecBuilder:
        self._aux = x
        return self

    def layout(self, gap: float = 5.0, edge: float = 2.0) -> SpecBuilder:
        self._layout = LayoutSpec(gap, edge)
        return self

    def plate(
        self,
        name: str,
        *parts: PartSpec,
        pause_at_z: tuple[float, ...] = (),
        colour_changes: tuple[ColourChange, ...] = (),
        print_sequence: str | None = None,
        group_by_filament: bool = False,
        wipe_tower: WipeTowerSpec | None = None,
    ) -> SpecBuilder:
        self._plates.append(
            PlateSpec(
                name,
                tuple(parts),
                tuple(pause_at_z),
                tuple(colour_changes),
                print_sequence,
                group_by_filament,
                wipe_tower or WipeTowerSpec(),
            )
        )
        return self

    @staticmethod
    def part(
        path: str | Path,
        filament: int = 1,
        count: int = 1,
        name: str | None = None,
        at: tuple[float, float] | None = None,
        height_ranges: tuple[HeightRangeSpec, ...] = (),
        orientation: Orientation | None = None,
        **settings: str,
    ) -> PartSpec:
        return PartSpec(
            Path(path), (), name, filament, count, "normal_part", (), _strs(settings), height_ranges, at, None, orientation or Orientation()
        )

    @staticmethod
    def multipart(name: str, *files: tuple[str | Path, int], at: tuple[float, float] | None = None, **settings: str) -> PartSpec:
        return PartSpec(
            None, tuple(FileRef(Path(p), f) for p, f in files), name, files[0][1], 1, "normal_part", (), _strs(settings), (), at
        )

    def build(self) -> Spec:
        from .recipes import recipe_settings

        merged: dict[str, str] = {}
        for n in self._recipes:
            merged.update(recipe_settings()[n])
        merged.update(self._process.set)
        process = replace(self._process, recipes=tuple(self._recipes), set=merged)
        s = Spec(
            self._name,
            self._outdir,
            self._printer,
            process,
            tuple(self._filaments),
            tuple(self._plates),
            self._bed,
            self._map,
            self._aux,
            self._layout,
        )
        errs = s.validate()
        if errs:
            raise BuildError("; ".join(errs))
        return s
