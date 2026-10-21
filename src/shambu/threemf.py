"""Read and patch Bambu Studio project files (.3mf) with the standard library only.

Tested against Bambu Studio 02.08.02.61 CLI exports (X2D, two nozzles, no AMS); every patch below
survives a CLI load and re-export. Format facts come from those files and from bbs_3mf.cpp.
Higher-level code uses `patches.PatchSet` (Command objects) rather than calling these methods.

Rules the code follows:
- Never rewrite 3D/3dmodel.model or the mesh files. Patches touch only Metadata/*.
- Keep the zip entry order and each entry's compression. model_settings.config must stay
  before slice_info.config and filament_sequence.json (the loader applies those to plates
  that model_settings already created).
- layer_config_ranges.xml keys objects by the 1-based order of their first <build><item>,
  not by object id.
- A change to settings makes an existing slice stale, so a patch on a sliced project drops
  the G-code entries and the plate slice data (invalidate_slices()). Renames do not.
- Every height range carries `layer_height`: the slicer reads it without a null check and
  segfaults in layer_height_profile_from_ranges when it is absent.
- save() writes a temporary file next to the destination, checks it, then renames it over the
  destination, so a failed save never damages the original.
- Every patch is idempotent: applying the same patch twice gives the same project.
- `filament_extruder_variant` and `filament_self_index` stay in block form (one block of variants per filament,
  paired index by index), and `extruder_nozzle_stats` is never left `[]`: a later CLI slice of such a file exits
  -100 "No valid nozzle found" (reference 03 section 8).
"""

from __future__ import annotations

import copy
import io
import json
import math
import os
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .errors import ProjectError

NS_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
NS_PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"

MODEL = "3D/3dmodel.model"
MODEL_SETTINGS = "Metadata/model_settings.config"
PROJECT_SETTINGS = "Metadata/project_settings.config"
LAYER_RANGES = "Metadata/layer_config_ranges.xml"
CUSTOM_GCODE = "Metadata/custom_gcode_per_layer.xml"
SLICE_INFO = "Metadata/slice_info.config"

# PrintConfig.cpp enum strings (s_keys_map_BedType, s_keys_map_FilamentMapMode, NozzleVolumeType)
BED_TYPES = ("Default Plate", "Cool Plate", "Engineering Plate", "High Temp Plate", "Textured PEI Plate", "Supertack Plate")
FILAMENT_MAP_MODES = ("Auto For Flush", "Auto For Match", "Manual", "Nozzle Manual", "Auto For Quality")
# CustomGCode.hpp: enum Type { ColorChange, PausePrint, ToolChange, Template, Custom, Unknown }
GCODE_TYPES = {"color_change": 0, "pause": 1, "tool_change": 2, "template": 3, "custom": 4}
GCODE_TYPE_NAMES = {v: k for k, v in GCODE_TYPES.items()}


STATS_NEW, STATS_OLD = "extruder_nozzle_stats_new", "extruder_nozzle_stats"


def _entry_count(entry) -> int:
    """Nozzles in one extruder's stat, `Type#n` or `Type#n|Type#m` (PrintConfig.cpp:751-783 sums them)."""
    total = 0
    for part in str(entry).split("|"):
        _, sep, n = part.partition("#")
        if sep:
            try:
                total += int(n.strip() or 0)
            except ValueError:
                continue
    return total


def _stats_bad(v, n_ext: int = 0) -> bool:
    """A stat list the CLI cannot slice with: empty, shorter than the extruder count, or an extruder with 0 nozzles."""
    if not isinstance(v, list) or not v or len(v) < n_ext:
        return True
    return any(_entry_count(e) <= 0 for e in v)


def effective_stats(ps: dict) -> tuple[str, list] | None:
    """The stat list a CLI run uses, and its key: a non-empty `extruder_nozzle_stats_new` wins, else the plain key
    (BambuStudio.cpp:4151-4155). None when neither key is in the file: the CLI then derives the stat from
    `extruder_max_nozzle_count` (4101, 4135-4146)."""
    if STATS_NEW not in ps and STATS_OLD not in ps:
        return None
    new = ps.get(STATS_NEW)
    if isinstance(new, list) and new:
        return STATS_NEW, new
    old = ps.get(STATS_OLD)
    return STATS_OLD, old if isinstance(old, list) else []


def _float_or(text: str, default: float) -> float:
    try:
        return float(text)
    except ValueError:
        return default


_FEATURE_CLASS = {
    "Support": "support",
    "Support interface": "support",
    "Support transition": "support",
    "Prime tower": "tower",
    "Custom": "other",
}
_SLICE_ENTRY = re.compile(r"^Metadata/plate_\d+\.(gcode|gcode\.md5|json)$|^Metadata/plate_\d+\.gcode\.md5$")
# plate metadata keys that only make sense for a sliced plate (bbs_3mf.cpp _handle_start_config_metadata)
_SLICE_PLATE_KEYS = {
    "prediction",
    "weight",
    "outside",
    "support_used",
    "label_object_enabled",
    "support_material_on_wipe_tower",
    "pattern_bbox_file",
}


@dataclass
class _Entry:
    info: zipfile.ZipInfo
    data: bytes


def _xml_bytes(root: ET.Element, indent: str) -> bytes:
    ET.indent(root, space=indent)
    body = ET.tostring(root, encoding="unicode", short_empty_elements=True).replace(" />", "/>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n").encode("utf-8")


def _meta(el: ET.Element, key: str):
    for m in el.findall("metadata"):
        if m.get("key") == key:
            return m.get("value")
    return None


def _set_meta(el: ET.Element, key: str, value, after: str | None = None) -> None:
    """Set <metadata key=key value=value/> in el. A new element goes after the 'after' key,
    else after the last existing <metadata>."""
    for m in el.findall("metadata"):
        if m.get("key") == key:
            m.set("value", str(value))
            return
    new = ET.Element("metadata", {"key": key, "value": str(value)})
    kids = list(el)
    pos = None
    if after is not None:
        for i, k in enumerate(kids):
            if k.tag == "metadata" and k.get("key") == after:
                pos = i + 1
    if pos is None:
        pos = max([i + 1 for i, k in enumerate(kids) if k.tag == "metadata"] or [0])
    el.insert(pos, new)


def _del_meta(el: ET.Element, key: str) -> bool:
    hit = False
    for m in list(el.findall("metadata")):
        if m.get("key") == key:
            el.remove(m)
            hit = True
    return hit


class Project:
    """A Bambu Studio .3mf held in memory. Load, inspect, patch, save."""

    def __init__(self, path: str):
        self.path = path
        try:
            with zipfile.ZipFile(path) as z:
                self._entries = [_Entry(copy.copy(i), z.read(i.filename)) for i in z.infolist()]
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError) as e:
            raise ProjectError(f"{path}: not a readable 3MF zip ({e})") from e
        if not self.has(PROJECT_SETTINGS) or not self.has(MODEL_SETTINGS):
            raise ProjectError(f"{path}: no Metadata/project_settings.config or model_settings.config: not a Bambu Studio project")
        self._dirty: set[str] = set()
        self._changed = False  # an entry was put or removed (slices invalidated, a new entry): save() must write
        self._ms: ET.Element | None = None  # parsed model_settings root
        self._ps: dict | None = None  # parsed project_settings dict
        self._ps_trailing_nl = False

    # ------------------------------------------------------------------ raw access
    def names(self) -> list[str]:
        return [e.info.filename for e in self._entries]

    def has(self, name: str) -> bool:
        return any(e.info.filename == name for e in self._entries)

    def read(self, name: str) -> bytes:
        for e in self._entries:
            if e.info.filename == name:
                return e.data
        raise ProjectError(f"{self.path}: no entry {name!r} in the zip")

    def _parse_xml(self, name: str) -> ET.Element:
        try:
            return ET.fromstring(self.read(name))
        except ET.ParseError as e:
            raise ProjectError(f"{self.path}: {name} is not valid XML ({e})") from e

    def _put(self, name: str, data: bytes, before: tuple[str, ...] = ()) -> None:
        self._changed = True
        for e in self._entries:
            if e.info.filename == name:
                e.data = data
                return
        info = zipfile.ZipInfo(name, date_time=self._entries[0].info.date_time)
        info.compress_type = zipfile.ZIP_STORED if name.endswith(".png") else zipfile.ZIP_DEFLATED
        pos = len(self._entries)
        for b in before:
            idx = [i for i, e in enumerate(self._entries) if e.info.filename == b]
            if idx:
                pos = min(pos, idx[0])
        self._entries.insert(pos, _Entry(info, data))

    def _remove(self, name: str) -> bool:
        n = len(self._entries)
        self._entries = [e for e in self._entries if e.info.filename != name]
        self._changed = self._changed or len(self._entries) != n
        return len(self._entries) != n

    # ------------------------------------------------------------------ parsed parts
    @property
    def model_settings(self) -> ET.Element:
        if self._ms is None:
            if not self.has(MODEL_SETTINGS):
                raise ProjectError("no Metadata/model_settings.config: not a Bambu project")
            self._ms = self._parse_xml(MODEL_SETTINGS)
        return self._ms

    @property
    def project_settings(self) -> dict:
        if self._ps is None:
            try:
                raw = self.read(PROJECT_SETTINGS).decode("utf-8")
                ps = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise ProjectError(f"{self.path}: {PROJECT_SETTINGS} is not valid UTF-8 JSON ({e})") from e
            if not isinstance(ps, dict):
                raise ProjectError(f"{self.path}: {PROJECT_SETTINGS} is not a JSON object")
            self._ps_trailing_nl = raw.endswith("\n")
            self._ps = ps
        return self._ps

    def project_layer_height(self) -> str:
        """The project's process layer_height in string form ('0.2'). Height ranges need it."""
        v = self.project_settings.get("layer_height")
        if isinstance(v, list):
            v = v[0] if v else None
        if v is None or str(v).strip() == "":
            raise ProjectError(f"{self.path}: no layer_height in {PROJECT_SETTINGS}; pass layer_height in the range options")
        try:
            ok = float(v) > 0
        except ValueError:
            ok = False
        if not ok:
            raise ProjectError(f"{self.path}: bad layer_height {v!r} in {PROJECT_SETTINGS}")
        return str(v)

    def _model_root(self) -> ET.Element:
        return self._parse_xml(MODEL)

    def build_order(self) -> list[str]:
        """3dmodel.model object ids in the order of their first build item. Position+1 is the
        index that layer_config_ranges.xml and cut_information.xml use."""
        root = self._model_root()
        order: list[str] = []
        for item in root.iter(f"{{{NS_CORE}}}item"):
            oid = item.get("objectid") or ""
            if oid and oid not in order:
                order.append(oid)
        return order

    def application(self) -> str | None:
        for m in self._model_root().findall(f"{{{NS_CORE}}}metadata"):
            if m.get("name") == "Application":
                return m.text
        return None

    def _objects(self) -> list[ET.Element]:
        return self.model_settings.findall("object")

    def _object_by_name(self, name: str) -> ET.Element:
        hits = [o for o in self._objects() if _meta(o, "name") == name]
        if not hits:
            names = [_meta(o, "name") for o in self._objects()]
            raise ProjectError(f"no object named {name!r}; objects: {names}")
        if len(hits) > 1:
            raise ProjectError(f"{len(hits)} objects named {name!r}; rename them first")
        return hits[0]

    def _plates(self) -> list[ET.Element]:
        return self.model_settings.findall("plate")

    def _plate(self, plate_id: int) -> ET.Element:
        for p in self._plates():
            if _meta(p, "plater_id") == str(plate_id):
                return p
        raise ProjectError(f"no plate {plate_id}")

    def _layer_ranges_root(self) -> ET.Element:
        if self.has(LAYER_RANGES):
            return self._parse_xml(LAYER_RANGES)
        return ET.Element("objects")

    def _custom_gcode_root(self) -> ET.Element:
        if self.has(CUSTOM_GCODE):
            return self._parse_xml(CUSTOM_GCODE)
        return ET.Element("custom_gcodes_per_layer")

    # ------------------------------------------------------------------ (a) summary
    def is_sliced(self) -> bool:
        if any(re.match(r"^Metadata/plate_\d+\.gcode$", n) for n in self.names()):
            return True
        return any((_meta(p, "gcode_file") or "") for p in self._plates())

    def slice_info(self) -> dict[int, dict]:
        """Per plate (1-based) slice results from Metadata/slice_info.config: time, weight, filament use."""
        out: dict[int, dict] = {}
        if not self.has(SLICE_INFO):
            return out
        si = self._parse_xml(SLICE_INFO)
        for pl in si.findall("plate"):
            keys = {m.get("key"): m.get("value") for m in pl.findall("metadata")}
            idx = int(keys.get("index") or 0)
            fils = [
                {
                    "id": f.get("id"),
                    "type": f.get("type"),
                    "color": f.get("color"),
                    "used_m": float(f.get("used_m") or 0),
                    "used_g": float(f.get("used_g") or 0),
                }
                for f in pl.findall("filament")
            ]
            out[idx] = {
                "prediction_s": int(float(keys.get("prediction") or 0)),
                "weight_g": float(keys.get("weight") or 0),
                "support_used": keys.get("support_used"),
                "outside": keys.get("outside"),
                "filament_maps": keys.get("filament_maps"),
                "filaments": fils,
                "warnings": [w.get("msg") or (w.text or "") for w in pl.findall("warning")],
            }
        return out

    def feature_usage(self, plate: int) -> dict | None:
        """Filament by feature from the plate's G-code (Metadata/plate_N.gcode), which is the only headless output
        that separates support, prime tower and flush from the model (reference 08 section 4). Extrusion is
        relative (M83); `; FEATURE: <name>` tags each block, `; FLUSH_START/END` brackets a flush, and a `T<n>` line
        selects the filament: the start G-code selects the loaded filament with an indented `T<n> H-1` line, so the first
        T line sets the tool without counting and every later T line with another tool is a change (live-checked on
        by-layer, by-object and filament-2-first plates). Retractions are repaid by the following positive moves
        before anything counts as deposited, so a retract in one feature and its deretract in the next cancel.
        Absolute extrusion (M82, G92) is handled.
        Returns grams per class and per filament, plus the number of filament changes."""
        name = f"Metadata/plate_{plate}.gcode"
        if not self.has(name):
            return None
        text = self.read(name).decode("utf-8", errors="replace")
        densities: list[float] = []
        diameters: list[float] = []
        mm: dict[tuple[str, int], float] = {}
        feature, tool, in_flush, changes = "other", -1, False, 0  # the first T line (in the start G-code) is the loaded filament
        absolute, last_e = False, 0.0  # M82 absolute / M83 relative extrusion; G92 E resets the absolute position
        debt: dict[int, float] = {}  # retracted filament per tool, repaid by the next positive moves (not deposited)
        for raw in text.splitlines():
            line = raw.lstrip()  # the start G-code indents its T lines
            if not line:
                continue
            c = line[0]
            if c == ";":
                if line.startswith("; FEATURE: "):
                    feature = _FEATURE_CLASS.get(line[11:].strip(), "model")
                elif line.startswith("; FLUSH_START"):
                    in_flush = True
                elif line.startswith("; FLUSH_END"):
                    in_flush = False
                elif line.startswith("; filament_density:"):
                    densities = [float(v) for v in line.split(":", 1)[1].split(",") if v.strip()]
                elif line.startswith("; filament_diameter:"):
                    diameters = [float(v) for v in line.split(":", 1)[1].split(",") if v.strip()]
                continue
            head = line.split(maxsplit=1)
            if not head:
                continue  # a line of spaces
            word = head[0]
            if c == "T" and len(word) > 1 and word[1:].isdigit():
                t = int(word[1:])
                if t < 100 and t != tool:  # T255 / T65535 are "no tool" markers
                    if tool >= 0:
                        changes += 1
                    tool = t
                    last_e = 0.0
            elif word == "M82":
                absolute = True
            elif word == "M83":
                absolute = False
            elif word == "G92":
                for tok in line.split()[1:]:
                    if tok[0] == "E":
                        last_e = _float_or(tok[1:], last_e)
            elif word in ("G1", "G2", "G3") and " E" in line:
                for tok in line.split()[1:]:
                    if tok[0] == "E":
                        e = _float_or(tok[1:], 0.0)
                        if absolute:
                            e, last_e = e - last_e, e
                        tl = max(tool, 0)
                        if e < 0:
                            debt[tl] = debt.get(tl, 0.0) - e
                        else:
                            repay = min(e, debt.get(tl, 0.0))
                            debt[tl] = debt.get(tl, 0.0) - repay
                            k = ("flush" if in_flush else feature, max(tool, 0))
                            mm[k] = mm.get(k, 0.0) + e - repay
                        break
        out: dict = {
            "model_g": 0.0,
            "support_g": 0.0,
            "tower_g": 0.0,
            "flush_g": 0.0,
            "other_g": 0.0,
            "filament_changes": changes,
            "per_filament": {},
        }
        for (cls, t), length in mm.items():
            d = diameters[t] if t < len(diameters) else 1.75
            rho = densities[t] if t < len(densities) else 1.24
            g = length * math.pi * (d / 2) ** 2 * rho / 1000
            out[f"{cls}_g"] = round(out[f"{cls}_g"] + g, 2)
            per = out["per_filament"].setdefault(str(t + 1), {})
            per[cls] = round(per.get(cls, 0.0) + g, 2)
        out["waste_g"] = round(out["tower_g"] + out["flush_g"], 2)
        return out

    def summary(self, features: bool = False) -> dict:
        """The project as a dict. `features=True` adds `slice.features` per sliced plate from feature_usage(), which
        parses the whole plate G-code: ask for it only when support grams (or the G-code cross-check) are needed.
        A slice run's result.json gives seconds per feature and tower plus flush grams without that parse."""
        ps = self.project_settings
        order = self.build_order()
        index_of = {oid: i + 1 for i, oid in enumerate(order)}
        ranges = {}
        for o in self._layer_ranges_root().findall("object"):
            ranges[o.get("id") or ""] = [
                {
                    "min_z": float(r.get("min_z", 0)),
                    "max_z": float(r.get("max_z", 0)),
                    "options": {op.get("opt_key"): op.text for op in r.findall("option")},
                }
                for r in o.findall("range")
            ]
        objects = []
        for o in self._objects():
            oid = o.get("id")
            settings = {m.get("key"): m.get("value") for m in o.findall("metadata") if m.get("key") not in (None, "name", "extruder")}
            parts = []
            for p in o.findall("part"):
                parts.append(
                    {
                        "id": p.get("id"),
                        "subtype": p.get("subtype"),
                        "name": _meta(p, "name"),
                        "extruder": _meta(p, "extruder"),
                        "source_file": _meta(p, "source_file"),
                        "settings": {
                            m.get("key"): m.get("value")
                            for m in p.findall("metadata")
                            if m.get("key") not in ("name", "extruder", "matrix") and not (m.get("key") or "").startswith("source_")
                        },
                    }
                )
            idx = index_of.get(oid or "")
            objects.append(
                {
                    "id": oid,
                    "build_index": idx,
                    "name": _meta(o, "name"),
                    "extruder": _meta(o, "extruder"),
                    "settings": settings,
                    "parts": parts,
                    "height_ranges": ranges.get(str(idx), []),
                }
            )
        name_of = {o["id"]: o["name"] for o in objects}
        plates: list[dict[str, Any]] = []
        for p in self._plates():
            keys = {m.get("key"): m.get("value") for m in p.findall("metadata")}
            plates.append(
                {
                    "id": int(keys.get("plater_id") or 0),
                    "name": keys.get("plater_name", ""),
                    "locked": keys.get("locked"),
                    "bed_type": keys.get("bed_type"),
                    "print_sequence": keys.get("print_sequence"),
                    "filament_map_mode": keys.get("filament_map_mode"),
                    "filament_maps": keys.get("filament_maps"),
                    "gcode_file": keys.get("gcode_file"),
                    "objects": [name_of.get(_meta(mi, "object_id")) for mi in p.findall("model_instance")],
                }
            )
        custom = []
        for pl in self._custom_gcode_root().findall("plate"):
            info_el, mode_el = pl.find("plate_info"), pl.find("mode")
            pid = (info_el.get("id") if info_el is not None else None) or "1"
            mode = mode_el.get("value") if mode_el is not None else None
            for ly in pl.findall("layer"):
                t = int(ly.get("type") or -1)
                custom.append(
                    {
                        "plate": int(pid),
                        "top_z": float(ly.get("top_z") or 0),
                        "type": GCODE_TYPE_NAMES.get(t, t),
                        "extruder": ly.get("extruder"),
                        "color": ly.get("color"),
                        "extra": ly.get("extra"),
                        "mode": mode,
                    }
                )
        n_fil = len(ps.get("filament_settings_id", []))
        slices = self.slice_info()
        for plate_d in plates:
            plate_d["slice"] = slices.get(plate_d["id"])
            if features and plate_d["slice"] is not None:
                plate_d["slice"]["features"] = self.feature_usage(plate_d["id"])
        return {
            "file": self.path,
            "application": self.application(),
            "sliced": self.is_sliced(),
            "printer": ps.get("printer_settings_id"),
            "process": ps.get("print_settings_id"),
            "bed_type": ps.get("curr_bed_type"),
            "print_sequence": ps.get("print_sequence"),
            "filaments": [
                {
                    "slot": i + 1,
                    "preset": ps["filament_settings_id"][i],
                    "type": (ps.get("filament_type") or [None] * n_fil)[i] if i < len(ps.get("filament_type", [])) else None,
                    "colour": ps["filament_colour"][i] if i < len(ps.get("filament_colour", [])) else None,
                }
                for i in range(n_fil)
            ],
            "filament_map_mode": ps.get("filament_map_mode"),
            "filament_map": ps.get("filament_map"),
            "extruders": {
                "extruder_type": ps.get("extruder_type"),
                "nozzle_diameter": ps.get("nozzle_diameter"),
                "nozzle_volume_type": ps.get("nozzle_volume_type"),
                "master_extruder_id": ps.get("master_extruder_id"),
            },
            "plates": plates,
            "objects": objects,
            "custom_gcode": custom,
            "reslice_problems": self.reslice_problems(),
        }

    # ------------------------------------------------------------------ (b) patches
    def _touch_ms(self, invalidate: bool = True):
        self._dirty.add(MODEL_SETTINGS)
        if invalidate:
            self.invalidate_slices()

    def _touch_ps(self):
        self._dirty.add(PROJECT_SETTINGS)
        self.invalidate_slices()

    def set_bed_type(self, bed_type: str, plate: int | None = None) -> None:
        """Project-wide curr_bed_type, and the per-plate override (<metadata key="bed_type">) for
        one plate or, with plate=None, for every plate that already has an override."""
        if bed_type not in BED_TYPES:
            raise ProjectError(f"bed type must be one of {BED_TYPES}")
        if plate is None:
            self.project_settings["curr_bed_type"] = bed_type
            self._touch_ps()
            for p in self._plates():
                if _meta(p, "bed_type") is not None:
                    _set_meta(p, "bed_type", bed_type)
                    self._touch_ms()
        else:
            _set_meta(self._plate(plate), "bed_type", bed_type, after="locked")
            self._touch_ms()

    def set_print_sequence(self, sequence: str, plate: int | None = None) -> None:
        """Per-plate <metadata key="print_sequence"> ("by layer" | "by object"), or with plate=None the project-wide
        `print_sequence` plus every plate that already carries an override."""
        if sequence not in ("by layer", "by object"):
            raise ProjectError("print sequence must be 'by layer' or 'by object'")
        if plate is None:
            self.project_settings["print_sequence"] = sequence
            self._touch_ps()
            for p in self._plates():
                if _meta(p, "print_sequence") is not None:
                    _set_meta(p, "print_sequence", sequence)
                    self._touch_ms()
        else:
            _set_meta(
                self._plate(plate), "print_sequence", sequence, after="bed_type" if _meta(self._plate(plate), "bed_type") else "locked"
            )
            self._touch_ms()

    def n_filaments(self) -> int:
        return len(self.project_settings.get("filament_settings_id", []))

    def set_filament_colours(self, colours: list[str]) -> None:
        """One '#RRGGBB' (or '#RRGGBBAA') per filament slot. The CLI writes only one entry for a
        two-filament project; this writes the full list."""
        n = self.n_filaments()
        if len(colours) != n:
            raise ProjectError(f"need {n} colours, got {len(colours)}")
        for c in colours:
            if not re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", c):
                raise ProjectError(f"bad colour {c!r}")
        self.project_settings["filament_colour"] = [c.upper() for c in colours]
        self._touch_ps()

    def set_filament_map(self, nozzle_of_filament: list[int], plate: int | None = None, mode: str = "Manual") -> None:
        """Which extruder (1-based, order of extruder_type in the printer preset) prints each
        filament slot. On the X2D: 1 = extruder_type[0], 2 = extruder_type[1].
        plate=None sets the project default; plate=N writes the plate override that the GUI's
        per-plate 'filament grouping' dialog writes."""
        n = self.n_filaments()
        n_ext = len(self.project_settings.get("nozzle_diameter", []))
        if len(nozzle_of_filament) != n or any(not 1 <= v <= n_ext for v in nozzle_of_filament):
            raise ProjectError(f"need {n} values in 1..{n_ext}")
        if mode not in FILAMENT_MAP_MODES:
            raise ProjectError(f"mode must be one of {FILAMENT_MAP_MODES}")
        ps = self.project_settings
        if plate is None:
            ps["filament_map"] = [str(v) for v in nozzle_of_filament]
        # PrintConfig.cpp update_values_to_printer_extruders_for_multiple_filaments looks up, for filament i, the
        # variant '<extruder_type[map[i]-1]> <nozzle_volume_type[map[i]-1]>' among the filament_extruder_variant
        # entries whose filament_self_index is i (index by index, PrintConfig.cpp:8022-8052). The two lists stay in
        # block form (one block of variants per filament); collapsing the variants to one entry per filament made
        # filament 2 unfindable ('could not found extruder_type ... filament_index 2', PrintConfig.cpp:8935).
        if "filament_extruder_variant" in ps:
            self.repair_extruder_variants()
        if plate is None:
            ps["filament_map"] = [str(v) for v in nozzle_of_filament]
            ps["filament_map_mode"] = mode
            self._touch_ps()
        else:
            p = self._plate(plate)
            _set_meta(p, "filament_map_mode", mode, after="locked")
            _set_meta(p, "filament_maps", " ".join(str(v) for v in nozzle_of_filament), after="filament_map_mode")
            self._touch_ms()

    # -- the per-extruder keys a CLI re-slice needs (reference 03 section "Re-slicing a CLI export") ------------------
    def _variant_block(self, slot: int, given: Sequence[Sequence[str]] | None) -> list[str]:
        """The extruder variants of filament `slot` (1-based): the filament preset's own list when given, else the
        printer's variant list (the X2D filament presets list the same six variants in the same order)."""
        ps = self.project_settings
        if given and slot <= len(given) and given[slot - 1]:
            return [str(v) for v in given[slot - 1]]
        pev = ps.get("printer_extruder_variant")
        if isinstance(pev, list) and pev:
            return [str(v) for v in pev]
        evl = ps.get("extruder_variant_list")
        if isinstance(evl, list) and evl:
            return [v.strip() for e in evl for v in str(e).split(",") if v.strip()]
        et, nvt = ps.get("extruder_type") or [], ps.get("nozzle_volume_type") or []
        return [f"{t} {v}" for t, v in zip(et, nvt, strict=False)]

    def repair_extruder_variants(self, filament_variants: Sequence[Sequence[str]] | None = None) -> bool:
        """Rewrite `filament_extruder_variant` and `filament_self_index` in block form: per filament, its variant list,
        with its slot number once per variant. A block that already matches stays as it is. When the file already holds
        `filament_self_index` blocks, a rebuilt block must keep its length (the 6N value keys follow it). Returns
        True when something changed. Raises ProjectError when the blocks cannot be rebuilt consistently."""
        ps = self.project_settings
        n = self.n_filaments()
        if n == 0 or ("filament_extruder_variant" not in ps and "filament_self_index" not in ps):
            return False  # a project without the variant keys (a one-extruder printer) gets none added
        var = [str(v) for v in ps.get("filament_extruder_variant") or []]
        idx = [str(v) for v in ps.get("filament_self_index") or []]
        counts = {i: idx.count(str(i)) for i in range(1, n + 1)}
        if idx and len(var) == len(idx) and all(counts[i] for i in counts) and len(idx) == sum(counts.values()):
            return False  # already one consistent block per filament
        new_var: list[str] = []
        new_idx: list[str] = []
        for i in range(1, n + 1):
            block = self._variant_block(i, filament_variants)
            if idx and counts.get(i) and counts[i] != len(block):
                raise ProjectError(
                    f"filament {i} has {counts[i]} entries in filament_self_index but {len(block)} extruder variants: "
                    "cannot rebuild filament_extruder_variant; re-export the project"
                )
            new_var += block
            new_idx += [str(i)] * len(block)
        if new_var == var and new_idx == idx:
            return False
        ps["filament_extruder_variant"] = new_var
        ps["filament_self_index"] = new_idx
        self._touch_ps()
        return True

    def repair_nozzle_stats(self) -> bool:
        """`extruder_nozzle_stats` `[]` (what a CLI export without --slice writes) or a `#0` count makes every later CLI
        slice of the file exit -100 "No valid nozzle found" (BambuStudio.cpp:4101-4156, ToolOrdering.cpp:1477-1481).
        The key the CLI reads (`effective_stats`: a non-empty `_new` wins) gets
        `<nozzle_volume_type[i]>#<extruder_max_nozzle_count[i]>` for each empty or zero-count extruder entry only; a
        valid entry (High Flow included) stays as it is. A repaired `_new` is copied to the plain key so the two agree.
        Also resize filament_volume_map / filament_nozzle_map to the filament count as Studio does
        (PresetBundle.cpp:2384-2387). Returns True when something changed."""
        ps = self.project_settings
        changed = False
        nvt = [str(v) for v in ps.get("nozzle_volume_type") or []]
        eff = effective_stats(ps)
        if eff is not None and nvt and self.needs_nozzle_stats() and _stats_bad(eff[1], self.extruder_count()):
            key, cur = eff
            cnt = [str(v) for v in ps.get("extruder_max_nozzle_count") or []]

            def default(i: int) -> str:
                # Studio broadcasts the last nozzle_volume_type over the extruders (BambuStudio.cpp:4137-4142); the
                # nozzle count is the extruder's own (or the last one given)
                t = nvt[min(i, len(nvt) - 1)]
                c = cnt[min(i, len(cnt) - 1)] if cnt else "1"
                return f"{t}#{c if str(c) not in ('', '0') else '1'}"

            n_ext = max(self.extruder_count(), len(cur))
            want = [str(cur[i]) if i < len(cur) and _entry_count(cur[i]) > 0 else default(i) for i in range(n_ext)]
            ps[key] = want
            if key == STATS_NEW and STATS_OLD in ps:
                ps[STATS_OLD] = list(want)
            changed = True
        n = self.n_filaments()
        for key, fill in (("filament_volume_map", "0"), ("filament_nozzle_map", "0")):
            v = ps.get(key)
            if isinstance(v, list) and n and len(v) != n:
                ps[key] = (list(v) + [fill] * n)[:n]
                changed = True
        if changed:
            self._touch_ps()
        return changed

    def extruder_count(self) -> int:
        ps = self.project_settings
        return max(len(ps.get("nozzle_diameter") or []), len(ps.get("nozzle_volume_type") or []), 1)

    def needs_nozzle_stats(self, extruders: int | None = None) -> bool:
        """Studio reads the nozzle stats only on a multi-extruder or multi-nozzle machine (BambuStudio.cpp:6648): an
        empty list is fine on a one-extruder, one-nozzle printer (P1S, A1)."""
        counts = []
        for v in self.project_settings.get("extruder_max_nozzle_count") or []:
            try:
                counts.append(int(str(v)))
            except ValueError:
                continue
        return (extruders or self.extruder_count()) > 1 or any(c > 1 for c in counts)

    def reslice_problems(self, extruders: int | None = None) -> list[str]:
        """Why a later CLI run on this file (`--slice`) would fail or use the wrong per-filament values; [] when none.
        `extruders` is the machine's extruder count (machine facts); the file's own count is used without it."""
        ps = self.project_settings
        out = []
        eff = effective_stats(ps)
        n_ext = extruders or self.extruder_count()
        if eff is not None and self.needs_nozzle_stats(n_ext) and _stats_bad(eff[1], n_ext):
            out.append(f'{eff[0]} {eff[1]} (Studio exits -100 "No valid nozzle found")')
        var, idx = ps.get("filament_extruder_variant"), ps.get("filament_self_index")
        if isinstance(var, list) and isinstance(idx, list) and len(var) != len(idx):
            out.append(
                f"filament_extruder_variant has {len(var)} entries but filament_self_index {len(idx)} "
                "(Studio logs 'could not found extruder_type' and slices with the wrong filament values)"
            )
        return out

    def set_object_setting(self, object_name: str, key: str, value) -> None:
        """Per-object process override, e.g. enable_support, sparse_infill_density ('25%'),
        brim_type ('outer_only'|'no_brim'|'auto_brim'|'brim_ears'|...), wall_loops.
        Stored as <metadata key=... value=...> under <object>; the loader passes every key it
        does not know to ModelObject::config.set_deserialize. Value must be in the string
        form the JSON presets use."""
        o = self._object_by_name(object_name)
        if key in ("name", "module"):
            raise ProjectError(f"{key} is not a print setting")
        _set_meta(o, key, value, after="name")
        self._touch_ms()

    def clear_object_setting(self, object_name: str, key: str) -> bool:
        hit = _del_meta(self._object_by_name(object_name), key)
        if hit:
            self._touch_ms()
        return hit

    def set_extruder(self, object_name: str, extruder: int, part_name: str | None = None) -> None:
        """Filament slot (1-based) for a whole object or one of its parts."""
        if not 1 <= extruder <= self.n_filaments():
            raise ProjectError(f"extruder must be in 1..{self.n_filaments()}")
        o = self._object_by_name(object_name)
        if part_name is None:
            _set_meta(o, "extruder", extruder, after="name")
        else:
            parts = [p for p in o.findall("part") if _meta(p, "name") == part_name]
            if len(parts) != 1:
                raise ProjectError(f"{len(parts)} parts named {part_name!r} in {object_name!r}")
            _set_meta(parts[0], "extruder", extruder)
        self._touch_ms()

    def set_height_range(self, object_name: str, min_z: float, max_z: float, options: dict) -> None:
        """Add or replace the height range [min_z, max_z] on an object. options are config keys
        and string values, e.g. {'extruder': '2'} or {'sparse_infill_density': '40%'}.
        The range always gets a layer_height (the slicer segfaults without one): the caller's
        value, else the value of the range it replaces, else project_layer_height()."""
        o = self._object_by_name(object_name)
        order = self.build_order()
        if o.get("id") not in order:
            raise ProjectError(f"object {object_name!r} has no build item")
        idx = str(order.index(o.get("id", "")) + 1)
        root = self._layer_ranges_root()
        obj = next((x for x in root.findall("object") if x.get("id") == idx), None)
        if obj is None:
            obj = ET.SubElement(root, "object", {"id": idx})
        old_lh: str | None = None
        for r in list(obj.findall("range")):
            if abs(float(r.get("min_z", 0)) - min_z) < 1e-6 and abs(float(r.get("max_z", 0)) - max_z) < 1e-6:
                old_lh = next((op.text for op in r.findall("option") if op.get("opt_key") == "layer_height" and op.text), old_lh)
                obj.remove(r)
        opts = {str(k): str(v) for k, v in options.items()}
        if not opts.get("layer_height", "").strip():
            opts["layer_height"] = old_lh or self.project_layer_height()
        r = ET.SubElement(obj, "range", {"min_z": repr(float(min_z)), "max_z": repr(float(max_z))})
        for k, v in opts.items():
            ET.SubElement(r, "option", {"opt_key": k}).text = v
        obj[:] = sorted(obj, key=lambda e: float(e.get("min_z", 0)))
        self._put(LAYER_RANGES, _xml_bytes(root, " "), before=(PROJECT_SETTINGS, MODEL_SETTINGS))
        self.invalidate_slices()

    def add_custom_gcode(self, plate: int, top_z: float, kind: str = "pause", extruder: int = 1, colour: str = "", extra: str = "") -> None:
        """Insert a per-layer action. kind: pause | color_change | tool_change | custom | template.
        top_z is the print_z of the layer the action runs before (GUI: 'add pause at layer N'
        stores that layer's top z). For 'custom', extra is the G-code text. For 'color_change'
        the extruder is the filament slot and colour the new colour."""
        if kind not in GCODE_TYPES:
            raise ProjectError(f"kind must be one of {list(GCODE_TYPES)}")
        self._plate(plate)
        ps = self.project_settings
        root = self._custom_gcode_root()
        pl = None
        for x in root.findall("plate"):
            info = x.find("plate_info")
            if info is not None and info.get("id") == str(plate):
                pl = x
        if pl is None:
            pl = ET.SubElement(root, "plate")
            ET.SubElement(pl, "plate_info", {"id": str(plate)})
            mode = "MultiExtruder" if self.n_filaments() > 1 else "SingleExtruder"
            ET.SubElement(pl, "mode", {"value": mode})
        t = GCODE_TYPES[kind]
        gcode = {1: ps.get("machine_pause_gcode", "M400 U1"), 3: ps.get("template_custom_gcode", ""), 2: "tool_change"}.get(t, extra)
        layer = ET.Element(
            "layer",
            {"top_z": repr(float(top_z)), "type": str(t), "extruder": str(extruder), "color": colour, "extra": extra, "gcode": gcode},
        )
        for old in list(pl.findall("layer")):  # idempotent: one action of a kind per layer height
            if old.get("type") == str(t) and abs(float(old.get("top_z") or 0) - float(top_z)) < 1e-6:
                pl.remove(old)
        mode_el = pl.find("mode")
        layers = pl.findall("layer") + [layer]
        layers.sort(key=lambda e: float(e.get("top_z", 0)))
        for e in pl.findall("layer"):
            pl.remove(e)
        insert_at = list(pl).index(mode_el) if mode_el is not None else len(pl)
        for i, e in enumerate(layers):
            pl.insert(insert_at + i, e)
        self._put(CUSTOM_GCODE, _xml_bytes(root, "    "), before=(MODEL_SETTINGS,))
        self.invalidate_slices()

    def rename_plate(self, plate: int, name: str) -> bool:
        """Names do not change the G-code, so a sliced plate stays sliced. Idempotent: the plate
        is found by id. Returns False when the plate already has that name."""
        p = self._plate(plate)
        if _meta(p, "plater_name") == name:
            return False
        _set_meta(p, "plater_name", name)
        self._touch_ms(invalidate=False)
        return True

    def rename_object(self, old: str, new: str) -> bool:
        """Rename one object. Idempotent: when no object is named `old` and exactly one is named
        `new`, the rename already happened and this returns False. Raises when neither name
        exists or a name is ambiguous. Names do not change the G-code, so a slice stays valid."""
        names = [_meta(o, "name") for o in self._objects()]
        if old not in names and names.count(new) == 1:
            return False
        o = self._object_by_name(old)
        if old == new:
            return False
        _set_meta(o, "name", new)
        self._touch_ms(invalidate=False)
        return True

    def invalidate_slices(self) -> list[str]:
        """Drop G-code and per-plate slice results so Bambu Studio shows the plate as unsliced."""
        removed = [n for n in self.names() if _SLICE_ENTRY.match(n)]
        for n in removed:
            self._remove(n)
        if self._ms is not None or self.has(MODEL_SETTINGS):
            changed = False
            for p in self._plates():
                if _meta(p, "gcode_file"):
                    _set_meta(p, "gcode_file", "")
                    changed = True
                for k in _SLICE_PLATE_KEYS:
                    changed |= _del_meta(p, k)
            if changed:
                self._dirty.add(MODEL_SETTINGS)
        if self.has(SLICE_INFO):
            si = self._parse_xml(SLICE_INFO)
            plates = si.findall("plate")
            if plates:
                for p in plates:
                    si.remove(p)
                self._put(SLICE_INFO, _xml_bytes(si, "  "))
                removed.append(SLICE_INFO + " <plate> entries")
        return removed

    # ------------------------------------------------------------------ (c) write
    def save(self, out_path: str) -> None:
        raw: bytes | None = None
        if not self._dirty and not self._changed:
            # Nothing changed: keep the original bytes (a rewrite recompresses every entry). The same path needs no
            # write; another path gets those bytes through the same temp-file-and-replace path as a real save.
            if os.path.abspath(out_path) == os.path.abspath(self.path):
                return
            with open(self.path, "rb") as src:
                raw = src.read()
        if MODEL_SETTINGS in self._dirty:
            self._put(MODEL_SETTINGS, _xml_bytes(self.model_settings, "  "))
        if PROJECT_SETTINGS in self._dirty:
            s = json.dumps(self.project_settings, indent=4, ensure_ascii=False)
            self._put(PROJECT_SETTINGS, (s + ("\n" if self._ps_trailing_nl else "")).encode("utf-8"))
        names = self.names()
        if MODEL_SETTINGS in names:
            ms = names.index(MODEL_SETTINGS)
            for late in (SLICE_INFO, "Metadata/filament_sequence.json"):
                if late in names and names.index(late) < ms:
                    raise ProjectError(f"{late} must come after {MODEL_SETTINGS} in the zip")
        # Atomic: write a temporary file in the destination directory (same filesystem), check it,
        # then rename it over out_path. On any failure the original stays as it was.
        out_dir = os.path.dirname(os.path.abspath(out_path))
        fd, tmp = tempfile.mkstemp(dir=out_dir, prefix="." + os.path.basename(out_path) + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                if raw is not None:
                    f.write(raw)
                else:
                    with zipfile.ZipFile(f, "w") as z:
                        for e in self._entries:
                            info = zipfile.ZipInfo(e.info.filename, date_time=e.info.date_time)
                            info.compress_type = e.info.compress_type
                            info.external_attr = e.info.external_attr
                            z.writestr(info, e.data)
                f.flush()
                os.fsync(f.fileno())
            with zipfile.ZipFile(tmp) as z:
                bad = z.testzip()
            if bad is not None:
                raise ProjectError(f"{out_path}: the written zip fails its CRC check at {bad}; original kept")
            # mkstemp creates 0600: keep the original's mode, or give a new file the umask default.
            if os.path.exists(out_path):
                os.chmod(tmp, os.stat(out_path).st_mode & 0o7777)
            else:
                umask = os.umask(0)
                os.umask(umask)
                os.chmod(tmp, 0o666 & ~umask)
            os.replace(tmp, out_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise


# ---------------------------------------------------------------------------- streaming read (no Project load)
_PLATE_GCODE = re.compile(r"Metadata/plate_(\d+)\.gcode")


def plate_gcode_names(path: str) -> list[str]:
    """The `Metadata/plate_N.gcode` entries of a .gcode.3mf, from the zip directory only (nothing is decompressed)."""
    try:
        with zipfile.ZipFile(path) as z:
            return sorted((n for n in z.namelist() if _PLATE_GCODE.fullmatch(n)), key=lambda n: int(_PLATE_GCODE.fullmatch(n).group(1)))  # type: ignore[union-attr]
    except (zipfile.BadZipFile, OSError, EOFError) as e:
        raise ProjectError(f"{path}: not a readable 3MF zip ({e})") from e


@contextmanager
def open_text_entry(path: str, name: str) -> Iterator[io.TextIOWrapper]:
    """One zip entry as streamed text (UTF-8, bad bytes replaced): a 76 MB plate G-code is read line by line, and no
    other entry is decompressed. `Project` loads every entry; this does not."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, EOFError) as e:
        raise ProjectError(f"{path}: not a readable 3MF zip ({e})") from e
    try:
        try:
            raw = z.open(name)
        except KeyError as e:
            raise ProjectError(f"{path}: no entry {name!r} in the zip") from e
        with io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="") as f:
            yield f
    finally:
        z.close()
