"""The plate spec: typed, validated at the boundary, built from JSON or through `SpecBuilder`.

JSON shape:
{
  "name": "plate-11-jaws",                  # output <outdir>/<name>.3mf
  "outdir": "/abs/dir",
  "printer": "Bambu Lab X2D 0.4 nozzle",
  "process": {"base": "0.20mm Standard @BBL X2D", "name": "PETG functional",
              "recipes": ["petg-functional"],            # named recipes from recipes.py, applied first
              "set": {"wall_loops": "6"}},               # explicit overrides, applied last
  "filaments": [{"preset": "Bambu PETG Basic @BBL X2D 0.4 nozzle", "colour": "#000000"},
                {"preset": "Bambu PLA Basic @BBL X2D 0.4 nozzle", "colour": "#FFFFFF"}],
  "bed_type": "Textured PEI Plate",
  "filament_map": [1, 2],                   # extruder per filament slot; default slot n -> extruder n
  "aux_x_limit": 235.5,                     # optional stricter x limit for extruder 2
  "layout": {"gap": 5, "edge": 2},
  "plates": [{"name": "jaws",
              "parts": [{"path": "/abs/jaw_print.stl", "filament": 1, "count": 2,
                         "recipes": ["tall-outer-brim", "tree-supports"], "set": {"wall_loops": "8"},
                         "height_ranges": [{"min_z": 0, "max_z": 0.6, "set": {"extruder": "2"}}],
                         "at": [10, 10]},
                        {"name": "label", "parts": [{"path": "/abs/label.stl", "filament": 1},
                                                    {"path": "/abs/text.stl", "filament": 2}]}],
              "pause_at_z": [4.2],
              "colour_changes": [{"z": 10.2, "filament": 2}]}]
}
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .errors import BuildError

BED_TYPES = ("Default Plate", "Cool Plate", "Engineering Plate", "High Temp Plate", "Textured PEI Plate", "Supertack Plate")
SUBTYPES = ("normal_part", "negative_part", "modifier_part", "support_enforcer", "support_blocker")


def _strs(d: dict | None) -> dict[str, str]:
    return {str(k): (v if isinstance(v, str) else str(v)) for k, v in (d or {}).items()}


def recipe_ranges(name: str) -> list[HeightRangeSpec]:
    from .recipes import by_name

    r = by_name().get(name)
    return [HeightRangeSpec(float(h["min_z"]), float(h["max_z"]), _strs(h["set"])) for h in (r.height_ranges if r else ())]


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
                raise BuildError(f"unknown recipe '{r}'; run `bambu recipes`")
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
        return d


@dataclass(frozen=True)
class PlateSpec:
    name: str
    parts: tuple[PartSpec, ...] = ()
    pause_at_z: tuple[float, ...] = ()
    colour_changes: tuple[ColourChange, ...] = ()

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
        )

    def to_json(self) -> dict:
        d: dict[str, Any] = {"name": self.name, "parts": [p.to_json() for p in self.parts]}
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


@dataclass(frozen=True)
class FilamentSpec:
    preset: str
    colour: str | None = None
    name: str | None = None
    set: dict[str, str] = field(default_factory=dict)


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
                raise BuildError(f"unknown recipe '{r}'; run `bambu recipes`")
            pset.update(recipe_sets[r])
        pset.update(_strs(pr.get("set")))
        lay = d.get("layout", {})
        return cls(
            str(d["name"]),
            Path(d["outdir"]),
            str(d["printer"]),
            ProcessSpec(pr["base"], pr.get("name"), tuple(pr.get("recipes", [])), pset),
            tuple(FilamentSpec(f["preset"], f.get("colour"), f.get("name"), _strs(f.get("set"))) for f in d["filaments"]),
            tuple(PlateSpec.from_json(p, i, recipe_sets) for i, p in enumerate(d["plates"], 1)),
            d.get("bed_type", "Textured PEI Plate"),
            tuple(int(x) for x in d["filament_map"]) if d.get("filament_map") else None,
            float(d["aux_x_limit"]) if d.get("aux_x_limit") is not None else None,
            LayoutSpec(float(lay.get("gap", 5.0)), float(lay.get("edge", 2.0))),
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
            "filaments": [{"preset": f.preset, "colour": f.colour, "name": f.name, "set": dict(f.set)} for f in self.filaments],
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
        if not self.plates:
            errs.append("at least one plate")
        n = len(self.filaments)
        for pi, pl in enumerate(self.plates, 1):
            if not pl.parts:
                errs.append(f"plate {pi}: no parts")
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
        self, name: str, *parts: PartSpec, pause_at_z: tuple[float, ...] = (), colour_changes: tuple[ColourChange, ...] = ()
    ) -> SpecBuilder:
        self._plates.append(PlateSpec(name, tuple(parts), tuple(pause_at_z), tuple(colour_changes)))
        return self

    @staticmethod
    def part(
        path: str | Path,
        filament: int = 1,
        count: int = 1,
        name: str | None = None,
        at: tuple[float, float] | None = None,
        height_ranges: tuple[HeightRangeSpec, ...] = (),
        **settings: str,
    ) -> PartSpec:
        return PartSpec(Path(path), (), name, filament, count, "normal_part", (), _strs(settings), height_ranges, at)

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
