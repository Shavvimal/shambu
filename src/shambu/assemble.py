"""Build and validate the CLI's assemble list (`--load-assemble-list`).

Schema and rules from src/BambuStudio.cpp (load_assemble_plate_list, construct_assemble_list),
v02.08.02.61:
- plates: 1..36. Each plate needs plate_name, need_arrange, objects (>= 1).
- object: path (.stl, or .obj for normal_part), count >= 1, filaments (non-empty; 1 or count
  entries, 1-based filament index), optional subtype, assemble_index (1 or count entries), pos_x/pos_y/pos_z,
  print_params, height_ranges.
- pos_* are translations ADDED to the STL's own coordinates (then the plate origin). They are
  not a target centre. Keep pos_x, pos_y, pos_z the same length (a code bug indexes pos_y and
  pos_z with pos_x's index).
- Every value in print_params, range_params, plate_params must be a JSON string. An unknown
  key fails the whole run (-6).
- assemble_index k > 0 merges the file into object "assemble_k" as a part of `subtype`.
  A merged part's height_ranges are lost: give them in assembled_params[k].
- Non-part subtypes (negative_part, modifier_part, support_enforcer, support_blocker) must
  have assemble_index >= 1 and cannot carry height_ranges.
- Only 8 plate_params keys survive into the saved 3MF: curr_bed_type, print_sequence,
  first_layer_print_sequence, other_layers_print_sequence(_nums), spiral_mode,
  filament_map_mode, filament_map, filament_volume_map.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .errors import AssembleError

SUBTYPES = ("normal_part", "negative_part", "modifier_part", "support_enforcer", "support_blocker")
PLATE_PARAMS_SAVED = (
    "curr_bed_type",
    "print_sequence",
    "first_layer_print_sequence",
    "other_layers_print_sequence",
    "other_layers_print_sequence_nums",
    "spiral_mode",
    "filament_map_mode",
    "filament_map",
    "filament_volume_map",
)
MAX_PLATES = 36


def _strs(d: dict | None) -> dict[str, str]:
    return {k: (v if isinstance(v, str) else json.dumps(v) if isinstance(v, (list, dict)) else str(v)) for k, v in (d or {}).items()}


@dataclass
class HeightRange:
    min_z: float
    max_z: float
    params: dict[str, str]

    def to_json(self) -> dict:
        return {"min_z": float(self.min_z), "max_z": float(self.max_z), "range_params": _strs(self.params)}


@dataclass
class Item:
    """One file placed on a plate (`count` copies). `pos` entries are per copy."""

    path: Path
    filament: int = 1
    count: int = 1
    subtype: str = "normal_part"
    assemble_index: int | list[int] = 0
    pos: list[tuple[float, float, float]] = field(default_factory=list)
    print_params: dict[str, str] = field(default_factory=dict)
    height_ranges: list[HeightRange] = field(default_factory=list)
    name: str = ""

    def to_json(self) -> dict:
        d = {"path": str(Path(self.path).resolve()), "count": int(self.count), "filaments": [int(self.filament)]}
        if self.subtype != "normal_part":
            d["subtype"] = self.subtype
        if self.assemble_index:
            d["assemble_index"] = (
                [int(i) for i in self.assemble_index] if isinstance(self.assemble_index, list) else [int(self.assemble_index)]
            )
        if self.pos:
            if len(self.pos) not in (1, self.count):
                raise AssembleError(f"{self.path.name}: pos has {len(self.pos)} entries, needs 1 or {self.count}")
            d["pos_x"] = [round(p[0], 3) for p in self.pos]
            d["pos_y"] = [round(p[1], 3) for p in self.pos]
            d["pos_z"] = [round(p[2], 3) for p in self.pos]
        if self.print_params:
            d["print_params"] = _strs(self.print_params)
        if self.height_ranges:
            d["height_ranges"] = [r.to_json() for r in self.height_ranges]
        return d


@dataclass
class Assembled:
    """Config for a merged object `assemble_<index>`."""

    index: int
    print_params: dict[str, str] = field(default_factory=dict)
    height_ranges: list[HeightRange] = field(default_factory=list)

    def to_json(self) -> dict:
        d: dict = {"assemble_index": int(self.index)}
        if self.print_params:
            d["print_params"] = _strs(self.print_params)
        if self.height_ranges:
            d["height_ranges"] = [r.to_json() for r in self.height_ranges]
        return d


@dataclass
class Plate:
    name: str
    items: list[Item] = field(default_factory=list)
    need_arrange: bool = False
    plate_params: dict[str, str] = field(default_factory=dict)
    assembled: list[Assembled] = field(default_factory=list)

    def to_json(self) -> dict:
        d = {"plate_name": self.name, "need_arrange": bool(self.need_arrange), "objects": [i.to_json() for i in self.items]}
        if self.plate_params:
            d["plate_params"] = _strs(self.plate_params)
        if self.assembled:
            d["assembled_params"] = [a.to_json() for a in self.assembled]
        return d


@dataclass
class AssembleList:
    plates: list[Plate] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"plates": [p.to_json() for p in self.plates]}

    def write(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.to_json(), indent=1), encoding="utf-8")
        return path


def validate(data: dict, known_keys: set[str] | None = None, filament_count: int | None = None) -> list[str]:
    """Problems in an assemble-list JSON (dict) that would make the CLI fail or misbehave."""
    errs: list[str] = []
    plates = data.get("plates")
    if not isinstance(plates, list) or not plates:
        return ["'plates' must be a non-empty list"]
    if len(plates) > MAX_PLATES:
        errs.append(f"{len(plates)} plates; the CLI allows at most {MAX_PLATES}")

    def check_params(where: str, params) -> None:
        if params is None:
            return
        if not isinstance(params, dict):
            errs.append(f"{where}: must be an object of string values")
            return
        for k, v in params.items():
            if not isinstance(v, str):
                errs.append(f"{where}: value of '{k}' must be a JSON string, got {type(v).__name__}")
            if known_keys is not None and k not in known_keys:
                errs.append(f"{where}: unknown setting key '{k}' (the CLI fails the whole run with -6)")

    for pi, plate in enumerate(plates, 1):
        where = f"plate {pi}"
        for req in ("plate_name", "need_arrange", "objects"):
            if req not in plate:
                errs.append(f"{where}: missing '{req}'")
        if not isinstance(plate.get("need_arrange"), bool):
            errs.append(f"{where}: need_arrange must be true or false")
        check_params(f"{where} plate_params", plate.get("plate_params"))
        for k in plate.get("plate_params") or {}:
            if k not in PLATE_PARAMS_SAVED:
                errs.append(f"{where} plate_params: '{k}' affects a CLI slice but is not saved in the 3MF")
        objs = plate.get("objects") or []
        if not objs:
            errs.append(f"{where}: no objects")
        merged: set[int] = set()
        for oi, o in enumerate(objs, 1):
            ow = f"{where} object {oi} ({Path(o.get('path', '?')).name})"
            p = o.get("path")
            if not p or not Path(p).is_file():
                errs.append(f"{ow}: file not found: {p}")
            elif not Path(p).is_absolute():
                errs.append(f"{ow}: path must be absolute")
            sub = o.get("subtype", "normal_part")
            if sub not in SUBTYPES:
                errs.append(f"{ow}: subtype '{sub}' unknown (the CLI silently uses normal_part)")
            if p and Path(p).suffix.lower() not in (".stl", ".obj"):
                errs.append(f"{ow}: only .stl and .obj load through the assemble list")
            if p and Path(p).suffix.lower() == ".obj" and sub != "normal_part":
                errs.append(f"{ow}: .obj only as normal_part")
            cnt = o.get("count")
            if not isinstance(cnt, int) or cnt < 1:
                errs.append(f"{ow}: count must be an integer >= 1")
                cnt = 1
            fil = o.get("filaments")
            if not isinstance(fil, list) or not fil:
                errs.append(f"{ow}: 'filaments' must be a non-empty list (missing -> -5)")
            else:
                if len(fil) not in (1, cnt):
                    errs.append(f"{ow}: filaments has {len(fil)} entries, needs 1 or count={cnt}")
                for f in fil:
                    if not isinstance(f, int) or f < 1 or (filament_count and f > filament_count):
                        errs.append(f"{ow}: filament {f} outside 1..{filament_count or '?'} (the CLI does not check this)")
            ai = o.get("assemble_index", [0])
            if not isinstance(ai, list) or not all(isinstance(i, int) for i in ai):
                errs.append(f"{ow}: assemble_index must be a list of integers")
                ai = [0]
            if ai and len(ai) not in (1, cnt):
                errs.append(f"{ow}: assemble_index has {len(ai)} entries, needs 1 or count={cnt}")
            if sub != "normal_part" and (not ai or any(i < 1 for i in ai)):
                errs.append(f"{ow}: {sub} needs assemble_index >= 1 (-2)")
            merged.update(i for i in ai if i > 0)
            lens = {k: len(o[k]) for k in ("pos_x", "pos_y", "pos_z") if k in o}
            if lens and len(set(lens.values())) != 1:
                errs.append(f"{ow}: pos_x/pos_y/pos_z lengths differ {lens}: the CLI reads out of bounds")
            for k, n in lens.items():
                if n not in (1, cnt):
                    errs.append(f"{ow}: {k} has {n} entries, needs 1 or count={cnt}")
            if lens and set(lens) != {"pos_x", "pos_y", "pos_z"}:
                errs.append(f"{ow}: give pos_x, pos_y and pos_z together")
            check_params(f"{ow} print_params", o.get("print_params"))
            if "height_ranges" in o:
                if sub != "normal_part":
                    errs.append(f"{ow}: height_ranges only on normal_part (-2)")
                if any(i > 0 for i in ai):
                    errs.append(f"{ow}: height_ranges on a merged part are lost; use assembled_params")
                for r in o["height_ranges"]:
                    if not isinstance(r.get("min_z"), (int, float)) or not isinstance(r.get("max_z"), (int, float)):
                        errs.append(f"{ow}: height range min_z/max_z must be numbers")
                    check_params(f"{ow} range_params", r.get("range_params"))
        for a in plate.get("assembled_params") or []:
            idx = a.get("assemble_index")
            if not isinstance(idx, int):
                errs.append(f"{where} assembled_params: assemble_index must be a scalar integer")
            elif idx not in merged:
                errs.append(f"{where} assembled_params: no object uses assemble_index {idx}")
            check_params(f"{where} assembled_params[{idx}] print_params", a.get("print_params"))
            for r in a.get("height_ranges") or []:
                check_params(f"{where} assembled_params[{idx}] range_params", r.get("range_params"))
    return errs
