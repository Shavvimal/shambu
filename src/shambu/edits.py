"""Geometry and filament edits of a Bambu Studio project: replace a mesh, move an object, move the prime tower, add a
filament slot. Each edit is a `Patch` (Command) with a `touches` declaration, so `Project.save()` can refuse a change
to any entry the edit did not declare.

Format facts (reference 03 section 2, and `03-3mf-format.md` "Project edits"):
- An object's position is `<build><item transform>` composed with `<component transform>`. The 12 numbers are
  column order `r00 r10 r20 r01 r11 r21 r02 r12 r22 tx ty tz`, in mm. Studio writes each sub-mesh re-centred on its
  bounding-box centre (bbs_3mf.cpp:5366), and `source_offset_*` of the part is that centre in the STL's own frame.
- The GUI's "replace with STL" keeps the volume transform and shifts it by `R * (c_new - c_old)`, so the two STL
  frames coincide (Plater.cpp:11302-11411). `--anchor frame` is that rule.
- `face_count` and `mesh_stat face_count` are written but never read (bbs_3mf.cpp:8218, 8306): they are kept right.
- `wipe_tower_x/y` are per-plate vectors of the tower's MIN corner, plate-local; a short vector means "plate k uses
  values[0]" (Config.hpp:681-685), so one plate's value is written after padding.
- Plates after the first sit at world offsets (col * W * 1.2, -row * D * 1.2), cols = compute_colum_count(n)
  (PartPlate.cpp:4536-4548, PartPlate.hpp:38-50).
- `--load-filaments` on a 3MF replaces slots, it never adds one (BambuStudio.cpp:2036), so AddFilament appends slot
  N+1 itself and leaves slots 1..N byte-identical. Studio pairs `filament_self_index[i]` with
  `filament_extruder_variant[i]` (PrintConfig.cpp:8022-8052); a short vector silently gives filament N+1 the values
  of filament 1, so every per-filament vector is extended and checked.

Design: each edit is a frozen dataclass Command (`apply`, `describe`, `touches`, `verify`) that the `EditSet`
Composite runs, like `patches.py`. Reading is ElementTree; writing is targeted text substitution of the one `<mesh>`
or `transform` attribute, so every other byte of an entry stays. The STL and the preset are read through `mesh.py`
and the caller; the clearance test is one injectable function (`CLEARANCE`).

This module is planning core: no zipfile, no subprocess, and it writes no file itself. It computes the new bytes;
`threemf.Project.save()` writes the temporary archive, runs the verify hook on it and renames it (its atomic writer
owns every os.replace, chmod and unlink). The Studio runs that normalise a new filament slot happen in `runs.py`,
which hands a `FilamentSlot` in.

Not here: multi-instance objects (a GUI clone), painting, mesh repair, adding or removing objects.
"""

from __future__ import annotations

import copy
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import layout as L
from . import mesh as M
from .errors import BambuError, ProjectError
from .patches import Patch, PatchSet
from .presets import machine_facts
from .studio import MeshInfo
from .threemf import DEFAULT_TOUCHES, LAYER_RANGES, MODEL, MODEL_SETTINGS, NS_CORE, NS_PROD, PROJECT_SETTINGS, Project

Vec = tuple[float, float, float]
# invalidate_slices() drops these: an edit that changes geometry or settings declares them
SLICE_TOUCHES = frozenset({"Metadata/plate_*.gcode", "Metadata/plate_*.gcode.md5", "Metadata/plate_*.json", "Metadata/slice_info.config"})
SIZE_TOL = 0.05  # mm per axis before a replacement counts as a size change
FRAME_TOL = 1.0  # mm the world centre may move under --anchor frame with an unchanged size
PLATE_GAP = 1.2  # 1 + LOGICAL_PART_PLATE_GAP (PartPlate.cpp:56)
TOWER_BRIM = 8.0  # the brim around the tower in the CLI's footprint estimate (BambuStudio.cpp:4481-4486)
TOWER_MIN_WIDTH = 35.0
PLACEHOLDER_FLUSH = "600"  # mm3 per change when Studio did not compute the matrix (only without Studio)
# Per-filament project keys that the CLI does not grow on --load-filaments (D report, Q2 table)
PROJECT_ONLY_KEYS = (
    "filament_colour_type",
    "filament_multi_colour",
    "filament_is_mixed",
    "filament_mixed_components",
    "filament_mixed_gradient",
    "filament_mixed_gradient_curve",
    "filament_mixed_gradient_per_part",
    "filament_mixed_gradient_range",
    "filament_mixed_sublayer_ratios",
    "pressure_advance",
    "enable_pressure_advance",
    "ironing_fan_speed",
    "first_x_layer_part_fan_speed",
    "pre_start_fan_time",
    "overhang_threshold_participating_cooling",
    "filament_change_length_nc",
)
MAP_KEYS = ("filament_map", "filament_nozzle_map", "filament_volume_map", "filament_map_2")
_SPECIAL_KEYS = {
    "filament_extruder_variant",
    "filament_self_index",
    "flush_volumes_matrix",
    "flush_volumes_vector",
    "different_settings_to_system",
    "inherits_group",
    "filament_colour",
    *MAP_KEYS,
}


class EditError(ProjectError):
    """An edit was refused or its self-check failed (unknown object, a plate out of range, a size change, a no-op,
    paint, off the bed, malformed project data). The input file is unchanged. The CLI maps it to exit 1."""


# ---------------------------------------------------------------------------- transforms
def parse_transform(text: str | None) -> list[float]:
    """The 12 numbers of a 3MF transform; identity when absent."""
    if not text:
        return [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
    try:
        vals = [float(x) for x in text.split()]
    except ValueError as e:
        raise EditError(f"transform {text!r} holds a value that is not a number ({e})") from e
    if len(vals) != 12 or not all(math.isfinite(v) for v in vals):
        raise EditError(f"transform {text!r} does not hold 12 finite numbers")
    return vals


def _lin(t: Sequence[float]) -> list[list[float]]:
    return [[t[0], t[3], t[6]], [t[1], t[4], t[7]], [t[2], t[5], t[8]]]


def _mv(m: Sequence[Sequence[float]], v: Sequence[float]) -> Vec:
    return (
        m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
        m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
        m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
    )


def _apply(t: Sequence[float], v: Sequence[float]) -> Vec:
    x, y, z = _mv(_lin(t), v)
    return (x + t[9], y + t[10], z + t[11])


def _compose(a: Sequence[float], b: Sequence[float]) -> list[float]:
    """a after b (b applied first)."""
    la, lb = _lin(a), _lin(b)
    m = [[sum(la[i][k] * lb[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    tx, ty, tz = _apply(a, b[9:12])
    return [m[0][0], m[1][0], m[2][0], m[0][1], m[1][1], m[2][1], m[0][2], m[1][2], m[2][2], tx, ty, tz]


def _inverse3(m: list[list[float]]) -> list[list[float]]:
    a, b, c = m[0]
    d, e, f = m[1]
    g, h, i = m[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        raise EditError("a transform is singular")
    return [
        [(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
        [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
        [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det],
    ]


def _num(x: float) -> str:
    return f"{x:.9g}"


def _fmt_transform(old_text: str | None, new: Sequence[float]) -> str:
    """Keep the nine rotation tokens exactly as written and rewrite the translation, so only three numbers change."""
    toks = old_text.split() if old_text and len(old_text.split()) == 12 else [_num(v) for v in new[:9]]
    return " ".join(toks[:9] + [_num(v) for v in new[9:12]])


def _bbox(points: Iterable[Vec]) -> tuple[Vec, Vec]:
    xs, ys, zs = [], [], []
    for p in points:
        xs.append(p[0])
        ys.append(p[1])
        zs.append(p[2])
    if not xs:
        raise EditError("a mesh has no vertices")
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def _centre(lo: Vec, hi: Vec) -> Vec:
    return ((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2)


def _size(lo: Vec, hi: Vec) -> Vec:
    return (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])


def _v3(v: Sequence[float]) -> str:
    return "(" + ", ".join(f"{x:.3f}" for x in v) + ")"


# ---------------------------------------------------------------------------- model reading
def _meta(el: ET.Element, key: str) -> str | None:
    for m in el.findall("metadata"):
        if m.get("key") == key:
            return m.get("value")
    return None


def _set_meta(el: ET.Element, key: str, value: str) -> None:
    for m in el.findall("metadata"):
        if m.get("key") == key:
            m.set("value", value)
            return
    kids = list(el)
    pos = max([i + 1 for i, k in enumerate(kids) if k.tag == "metadata"] or [0])
    el.insert(pos, ET.Element("metadata", {"key": key, "value": value}))


@dataclass
class _Comp:
    path: str  # zip entry name, no leading slash
    sub_id: str
    transform_text: str | None

    @property
    def transform(self) -> list[float]:
        return parse_transform(self.transform_text)


@dataclass
class _Obj:
    oid: str
    name: str
    ms: ET.Element  # <object> in model_settings.config
    item_text: str | None  # the first build item's transform attribute
    n_items: int
    comps: list[_Comp]
    item_texts: list[str | None] = field(default_factory=list)  # every build item, in build order (instance ids)

    @property
    def item(self) -> list[float]:
        return parse_transform(self.item_text)


@dataclass
class _Mesh:
    vertices: list[Vec]
    triangles: list[tuple[int, int, int]]
    paint: bool
    span: tuple[int, int]  # the <mesh>...</mesh> character span in the entry text


_ATTR = re.compile(r'([\w:]+)="([^"]*)"')


def _root(project: Project) -> ET.Element:
    try:
        return ET.fromstring(project.read(MODEL))
    except ET.ParseError as e:
        raise EditError(f"{project.path}: {MODEL} is not valid XML ({e})") from e


def find_object(project: Project, name: str) -> _Obj:
    hits = [o for o in project.model_settings.findall("object") if _meta(o, "name") == name]
    if not hits:
        names = [_meta(o, "name") for o in project.model_settings.findall("object")]
        raise EditError(f"no object named {name!r}; objects: {names}")
    if len(hits) > 1:
        raise EditError(f"{len(hits)} objects named {name!r}; rename them first (shambu patch --rename-object)")
    return _object(project, hits[0], _root(project))


def _object(project: Project, ms: ET.Element, root: ET.Element) -> _Obj:
    oid = ms.get("id") or ""
    res = next((o for o in root.iter(f"{{{NS_CORE}}}object") if o.get("id") == oid), None)
    if res is None:
        raise EditError(f"object id {oid} is in model_settings.config but not in {MODEL}")
    comps = [
        _Comp((c.get(f"{{{NS_PROD}}}path") or "").lstrip("/"), c.get("objectid") or "", c.get("transform"))
        for c in res.iter(f"{{{NS_CORE}}}component")
    ]
    if not comps or any(not c.path for c in comps):
        raise EditError(f"object {_meta(ms, 'name')!r} holds its mesh in {MODEL}, not in a sub-model: not a Bambu Studio layout")
    items = [i for i in root.iter(f"{{{NS_CORE}}}item") if i.get("objectid") == oid]
    texts = [i.get("transform") for i in items]
    return _Obj(oid, _meta(ms, "name") or "", ms, texts[0] if texts else None, len(items), comps, texts)


def read_mesh(text: str, sub_id: str) -> _Mesh:
    """The <mesh> of sub-object `sub_id` in a sub-model's text, parsed with regular expressions (the file stays
    byte-identical outside the span)."""
    m = re.search(rf'<object\b[^>]*\bid="{re.escape(sub_id)}"[^>]*>', text)
    if m is None:
        raise EditError(f"sub-object {sub_id} not found in its sub-model")
    start = text.find("<mesh>", m.end())
    end = text.find("</mesh>", start)
    nxt = text.find("<object", m.end())
    if start < 0 or end < 0 or (0 <= nxt < start):
        raise EditError(f"sub-object {sub_id} has no <mesh>")
    end += len("</mesh>")
    body = text[start:end]
    verts: list[Vec] = []
    tris: list[tuple[int, int, int]] = []
    paint = False
    try:
        for vm in re.finditer(r"<vertex\b([^>]*)/>", body):
            a = dict(_ATTR.findall(vm.group(1)))
            verts.append((float(a["x"]), float(a["y"]), float(a["z"])))
        for tm in re.finditer(r"<triangle\b([^>]*)/>", body):
            a = dict(_ATTR.findall(tm.group(1)))
            tris.append((int(a["v1"]), int(a["v2"]), int(a["v3"])))
            paint = paint or any(k.startswith("paint_") or k == "face_property" for k in a)
    except (KeyError, ValueError) as e:
        raise EditError(f"sub-object {sub_id}: a <vertex> or <triangle> lacks a coordinate or index, or holds a non-number ({e!r})") from e
    if any(not 0 <= i < len(verts) for t in tris for i in t):
        raise EditError(f"sub-object {sub_id}: a triangle names a vertex index out of range (Studio: 'invalid vertex id')")
    return _Mesh(verts, tris, paint, (start, end))


def _mesh_xml(verts: Sequence[Vec], tris: Sequence[tuple[int, int, int]]) -> str:
    """Studio's own layout of a <mesh> (one element per line, the indentation of a CLI export)."""
    out = ["<mesh>\n    <vertices>\n"]
    out += [f'     <vertex x="{_num(x)}" y="{_num(y)}" z="{_num(z)}"/>\n' for x, y, z in verts]
    out.append("    </vertices>\n    <triangles>\n")
    out += [f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>\n' for a, b, c in tris]
    out.append("    </triangles>\n   </mesh>")
    return "".join(out)


def _text(project: Project, name: str) -> str:
    try:
        return project.read(name).decode("utf-8")
    except UnicodeDecodeError as e:
        raise EditError(f"{project.path}: {name} is not UTF-8 ({e})") from e


def _sub_item_transform(text: str, oid: str, new_text: str) -> str:
    """Rewrite the transform attribute of the one build item of object `oid` (or add one), nothing else."""
    pat = re.compile(rf'<item\b[^>]*\bobjectid="{re.escape(oid)}"[^>]*?/?>')
    hits = pat.findall(text)
    if len(hits) != 1:
        raise EditError(f"{len(hits)} build items for object id {oid}: a moved or replaced object needs exactly one")

    def fix(m: re.Match) -> str:
        tag = m.group(0)
        if re.search(r'\btransform="[^"]*"', tag):
            return re.sub(r'\btransform="[^"]*"', f'transform="{new_text}"', tag, count=1)
        return tag.replace(f'objectid="{oid}"', f'objectid="{oid}" transform="{new_text}"', 1)

    return pat.sub(fix, text, count=1)


def _sub_component_transform(text: str, oid: str, sub_id: str, new_text: str) -> str:
    om = re.search(rf'<object\b[^>]*\bid="{re.escape(oid)}"[^>]*>.*?</object>', text, re.S)
    if om is None:
        raise EditError(f"object id {oid} not found in {MODEL}")
    block = om.group(0)
    pat = re.compile(rf'<component\b[^>]*\bobjectid="{re.escape(sub_id)}"[^>]*?/>')

    def fix(m: re.Match) -> str:
        tag = m.group(0)
        if re.search(r'\btransform="[^"]*"', tag):
            return re.sub(r'\btransform="[^"]*"', f'transform="{new_text}"', tag, count=1)
        return tag.replace("/>", f' transform="{new_text}"/>', 1)

    new_block, n = pat.subn(fix, block, count=1)
    if n != 1:
        raise EditError(f"component {sub_id} of object {oid} not found")
    return text[: om.start()] + new_block + text[om.end() :]


class _Geometry:
    """Cached sub-meshes of one project, for world boxes."""

    def __init__(self, project: Project):
        self.project = project
        self._texts: dict[str, str] = {}
        self._meshes: dict[tuple[str, str], _Mesh] = {}

    def mesh(self, comp: _Comp) -> _Mesh:
        key = (comp.path, comp.sub_id)
        if key not in self._meshes:
            if comp.path not in self._texts:
                self._texts[comp.path] = _text(self.project, comp.path)
            self._meshes[key] = read_mesh(self._texts[comp.path], comp.sub_id)
        return self._meshes[key]

    def world_box(
        self, obj: _Obj, item: Sequence[float] | None = None, replace_verts: dict[str, list[Vec]] | None = None
    ) -> tuple[Vec, Vec]:
        """World bbox of an object: every component's vertices through component then item. `replace_verts` replaces the
        vertices of a sub-object id, `item` the item transform."""
        t_item = obj.item if item is None else list(item)
        pts: list[Vec] = []
        for c in obj.comps:
            verts = (replace_verts or {}).get(c.sub_id) or self.mesh(c).vertices
            full = _compose(t_item, c.transform)
            pts += [_apply(full, v) for v in verts]
        return _bbox(pts)


def world_box(project: Project, name: str) -> tuple[Vec, Vec]:
    """World bbox (min, max) of the named object, in mm: every part's vertices through component then item."""
    return _Geometry(project).world_box(find_object(project, name))


def plate_origin(index: int, count: int, width: float, depth: float) -> tuple[float, float]:
    """World offset of plate `index` (0-based) of `count` (PartPlate.cpp compute_shape_position)."""
    v = math.sqrt(count)
    r = round(v)
    cols = r + 1 if v > r else r
    cols = max(cols, 1)
    row, col = divmod(index, cols)
    return (col * width * PLATE_GAP, -row * depth * PLATE_GAP)


def _plates(project: Project) -> list[ET.Element]:
    return project.model_settings.findall("plate")


def _instance_plates(project: Project, oid: str) -> dict[int, int]:
    """instance_id -> 1-based plate position, from the plates' <model_instance> elements."""
    out: dict[int, int] = {}
    for i, p in enumerate(_plates(project), 1):
        for mi in p.findall("model_instance"):
            if _meta(mi, "object_id") == oid:
                try:
                    out[int(_meta(mi, "instance_id") or 0)] = i
                except ValueError as e:
                    raise EditError(f"plate {i}: model_instance of object {oid} has instance_id {_meta(mi, 'instance_id')!r}") from e
    return out


def _plate_of(project: Project, oid: str) -> int:
    """1-based position of the plate holding object `oid` (plate order in model_settings.config)."""
    for i, p in enumerate(_plates(project), 1):
        for mi in p.findall("model_instance"):
            if _meta(mi, "object_id") == oid:
                return i
    return 1


def _bed(project: Project) -> dict:
    return machine_facts(project.project_settings)


def _origin(project: Project, plate: int) -> tuple[float, float]:
    facts = _bed(project)
    w = facts["bed"]["x"][1] - facts["bed"]["x"][0]
    d = facts["bed"]["y"][1] - facts["bed"]["y"][0]
    return plate_origin(plate - 1, max(len(_plates(project)), 1), w, d)


def _object_filaments(project: Project, obj: _Obj) -> tuple[int, set[int]]:
    """The object's filament and the others it uses: part extruders, support filaments, height-range extruders."""

    def slot(v: str | None, where: str) -> int:
        try:
            return int(float(v or 0))
        except ValueError as e:
            raise EditError(f"object {obj.name!r}: {where} is {v!r}, not a filament slot number") from e

    main = slot(_meta(obj.ms, "extruder"), "extruder") or 1
    also: set[int] = set()
    for part in obj.ms.findall("part"):
        if part.get("subtype") in (None, "normal_part", "modifier_part"):
            e = slot(_meta(part, "extruder"), f"part {_meta(part, 'name')!r} extruder")
            if e:
                also.add(e)
    for k in ("support_filament", "support_interface_filament", "wall_filament", "sparse_infill_filament", "solid_infill_filament"):
        e = slot(_meta(obj.ms, k) or str(project.project_settings.get(k, "0")), k)
        if e:
            also.add(e)
    idx = _build_index(project, obj.oid)
    for r in _ranges(project, idx):
        e = slot(r.get("extruder"), f"height range {r['min_z']}..{r['max_z']} extruder")
        if e:
            also.add(e)
    return main, also - {main}


def _build_index(project: Project, oid: str) -> int:
    order = project.build_order()
    return order.index(oid) + 1 if oid in order else 0


def _ranges(project: Project, build_index: int) -> list[dict]:
    if not project.has(LAYER_RANGES) or not build_index:
        return []
    try:
        root = ET.fromstring(project.read(LAYER_RANGES))
    except ET.ParseError as e:
        raise EditError(f"{project.path}: {LAYER_RANGES} is not valid XML ({e})") from e
    out = []
    for o in root.findall("object"):
        if o.get("id") == str(build_index):
            for r in o.findall("range"):
                d = {op.get("opt_key") or "": op.text or "" for op in r.findall("option")}
                d["min_z"], d["max_z"] = r.get("min_z", "0"), r.get("max_z", "0")
                out.append(d)
    return out


def plate_filament_map(project: Project, plate: int | None) -> list[int]:
    """The filament -> extruder map that slices plate `plate`: the plate's own `filament_maps` when it has one, else the
    project's `filament_map` (PartPlate.cpp:288 get_real_filament_maps)."""
    ps = project.project_settings
    raw = None
    if plate is not None and 1 <= plate <= len(_plates(project)):
        raw = (_meta(_plates(project)[plate - 1], "filament_maps") or "").split() or None
    try:
        return [int(v) for v in (raw or ps.get("filament_map") or [])]
    except ValueError as e:
        raise EditError(f"plate {plate}: the filament map {raw or ps.get('filament_map')} holds a non-number") from e


def _context(project: Project, plate: int | None = None) -> L.Context:
    ps = project.project_settings
    fmap = plate_filament_map(project, plate)
    tpu = {i for i, t in enumerate(ps.get("filament_type") or [], 1) if "TPU" in str(t)}
    return L.Context(_bed(project), {i + 1: e for i, e in enumerate(fmap)}, tpu)


def _placed(key: str, lo: Vec, hi: Vec, origin: tuple[float, float], filament: int, also: set[int], margin: float) -> L.Placed:
    size = _size(lo, hi)
    x0, y0 = lo[0] - origin[0], lo[1] - origin[1]
    info = MeshInfo(Path(key), size, (x0, y0, lo[2]), (x0 + size[0], y0 + size[1], hi[2]), True, 0, 1, 0.0)
    return L.Placed(key, info, filament, x0, y0, margin, True, frozenset(also))


def _margin(project: Project, obj: _Obj) -> float:
    ps = project.project_settings
    params = {
        "brim_type": _meta(obj.ms, "brim_type") or ps.get("brim_type", "auto_brim"),
        "brim_width": _meta(obj.ms, "brim_width") or ps.get("brim_width", "5"),
    }
    return L.brim_margin(params)


def bbox_clear(a: L.Placed, b: L.Placed) -> bool:
    """The default clearance test of two placed objects: their bounding boxes with brim margins do not overlap."""
    return not L.overlaps(a.rect(), b.rect())


# The clearance test that --move (and replace-mesh's WARN lines) use. ONE function of two L.Placed, so the merge can bind
# it to the layout work's footprint (silhouette) test without touching the callers: edits.CLEARANCE = <footprint test>.
CLEARANCE: Callable[[L.Placed, L.Placed], bool] = bbox_clear


def layout_checks(
    project: Project, obj: _Obj, lo: Vec, hi: Vec, plate: int, geo: _Geometry, clear: Callable[[L.Placed, L.Placed], bool] | None = None
) -> list[L.Check]:
    """Bed edges, extruder reach (every filament the object uses, through the plate's own filament map) and clearance
    to every instance of every other object on the plate (`clear`, default CLEARANCE: bounding boxes with brim margins).
    The slice is the real clash test; these lines say what to look at."""
    clear = clear or CLEARANCE
    origin = _origin(project, plate)
    main, also = _object_filaments(project, obj)
    me = _placed(obj.name, lo, hi, origin, main, also, _margin(project, obj))
    ctx = _context(project, plate)
    checks = list(L.InsideBed().check([me], ctx)) + list(L.ExtruderReach().check([me], ctx))
    root = _root(project)
    for o in project.model_settings.findall("object"):
        if o.get("id") == obj.oid:
            continue
        other = _object(project, o, root)
        where = _instance_plates(project, other.oid)
        om, oa = _object_filaments(project, other)
        for k, text in enumerate(other.item_texts or [other.item_text]):
            if where.get(k, _plate_of(project, other.oid)) != plate:
                continue
            olo, ohi = geo.world_box(other, parse_transform(text))
            label = other.name if other.n_items <= 1 else f"{other.name} (instance {k})"
            them = _placed(label, olo, ohi, origin, om, oa, _margin(project, other))
            if not clear(me, them):
                checks.append(L.Check(False, f"{me.key} and {them.key} overlap (bounding boxes with brim margins)"))
    return checks


# ---------------------------------------------------------------------------- the STL side of a replacement
@dataclass(frozen=True)
class NewMesh:
    """An STL indexed the way Studio stores it: vertices re-centred on the bbox centre."""

    local: tuple[Vec, ...]
    triangles: tuple[tuple[int, int, int], ...]
    centre: Vec  # bbox centre in the (rotated) STL frame = the new source_offset
    size: Vec


def load_stl(path: str | Path, rotate: Vec = (0.0, 0.0, 0.0), lay_on_face: Vec | None = None) -> NewMesh:
    try:
        facets = M.read_stl(path)
    except BambuError as e:
        raise EditError(f"replacement mesh: {e}") from e
    o = M.Orientation(lay_on_face, rotate)
    if not o.is_identity:
        facets = M.transform(facets, o.matrix())
    index: dict[Vec, int] = {}
    verts: list[Vec] = []
    tris: list[tuple[int, int, int]] = []
    for f in facets:
        t = []
        for v in f:
            k = (round(v[0], 6), round(v[1], 6), round(v[2], 6))
            if k not in index:
                index[k] = len(verts)
                verts.append(k)
            t.append(index[k])
        if len(set(t)) == 3:
            tris.append((t[0], t[1], t[2]))
    if not tris:
        raise EditError(f"{path}: no non-degenerate facets (Studio refuses it: 'found no triangles')")
    used = sorted({i for t in tris for i in t})
    if len(used) != len(verts):  # vertices only degenerate facets used
        remap = {old: new for new, old in enumerate(used)}
        verts = [verts[i] for i in used]
        tris = [(remap[a], remap[b], remap[c]) for a, b, c in tris]
    lo, hi = _bbox(verts)
    c = _centre(lo, hi)
    local = tuple((v[0] - c[0], v[1] - c[1], v[2] - c[2]) for v in verts)
    return NewMesh(local, tuple(tris), c, _size(lo, hi))


def canonical_triangles(verts: Sequence[Vec], tris: Iterable[tuple[int, int, int]]) -> list[tuple]:
    """Triangles as vertex positions on a 0.001 mm grid in the bbox-centred frame, each rotated to start at its
    smallest corner (winding kept), sorted: equal lists mean the same surface, connectivity and winding."""
    c = _centre(*_bbox(verts))
    pts = [(round((v[0] - c[0]) * 1000), round((v[1] - c[1]) * 1000), round((v[2] - c[2]) * 1000)) for v in verts]
    out = []
    for a, b, cc in tris:
        t = (pts[a], pts[b], pts[cc])
        i = t.index(min(t))
        out.append(t[i:] + t[:i])
    return sorted(out)


# ---------------------------------------------------------------------------- the edits
@dataclass(frozen=True)
class ReplaceMesh:
    """Swap one part's mesh for an STL and keep the object: its name, settings, transform and plate.

    anchor: `frame` (default) keeps the STL frames coincident, as the GUI does; `centre` keeps the old world bbox
    centre; `min` keeps the old world min corner (the convention of spec `at` and --move). The object is then
    dropped onto z = 0, unless it was sunk below the bed on purpose (the GUI calls ensure_on_bed only for an object
    that is not sinking, Plater.cpp:11375, 11397). A mesh that another object shares is refused."""

    object_name: str
    stl: str
    part_name: str | None = None
    rotate: Vec = (0.0, 0.0, 0.0)
    lay_on_face: Vec | None = None
    anchor: str = "frame"
    allow_size_change: bool = False
    drop_paint: bool = False
    touches: frozenset[str] = frozenset({MODEL, "3D/Objects/*", MODEL_SETTINGS, LAYER_RANGES}) | SLICE_TOUCHES

    def describe(self) -> str:
        what = f"'{self.object_name}'" + (f" part '{self.part_name}'" if self.part_name else "")
        turn = ""
        bits = ([f"lay on face {self.lay_on_face}"] if self.lay_on_face is not None else []) + (
            [f"rotate {self.rotate}"] if any(self.rotate) else []
        )
        if bits:
            turn = " (turned first: " + ", ".join(bits) + ")"
        return f"object {what}: mesh replaced by {Path(self.stl).name}{turn}, anchor {self.anchor}"

    def _target(self, p: Project) -> tuple[_Obj, ET.Element, _Comp]:
        if self.anchor not in ("frame", "centre", "min"):
            raise EditError(f"--anchor must be frame, centre or min, not {self.anchor!r}")
        obj = find_object(p, self.object_name)
        if obj.n_items != 1:
            raise EditError(f"object {self.object_name!r} has {obj.n_items} build items: multi-instance objects are not supported")
        parts = obj.ms.findall("part")
        if self.part_name is not None:
            parts = [x for x in parts if _meta(x, "name") == self.part_name]
            if len(parts) != 1:
                raise EditError(f"{len(parts)} parts named {self.part_name!r} in {self.object_name!r}")
        elif len(parts) != 1:
            names = [_meta(x, "name") for x in parts]
            raise EditError(f"object {self.object_name!r} has {len(parts)} parts {names}: name one as OBJECT/PART")
        part = parts[0]
        comp = next((c for c in obj.comps if c.sub_id == part.get("id")), None)
        if comp is None:
            raise EditError(f"part id {part.get('id')} of {self.object_name!r} has no component in {MODEL}")
        # Studio shares one mesh resource between identical volumes (bbs_3mf.cpp:7353-7367, loaded at 5271-5313): two
        # components (of two objects, or two parts of one object) name the same sub-object, or a part carries the
        # `mesh_shared` metadata the reader follows (bbs_3mf.cpp:5282-5313). Rewriting the mesh would change every
        # volume that references it. (`source_object_id` is not the sharing mechanism, bbs_3mf.cpp:5450.)
        if _meta(part, "mesh_shared") is not None:
            raise EditError(
                f"part of {self.object_name!r} carries mesh_shared {_meta(part, 'mesh_shared')!r}: its mesh is shared; replacing it is "
                "not supported in this release"
            )
        refs: list[str] = []
        root = _root(p)
        for o in root.iter(f"{{{NS_CORE}}}object"):
            for c in o.iter(f"{{{NS_CORE}}}component"):
                if (c.get(f"{{{NS_PROD}}}path") or "").lstrip("/") == comp.path and c.get("objectid") == comp.sub_id:
                    refs.append(o.get("id") or "")
        sharers = [m for m in p.model_settings.findall("object") for x in m.findall("part") if _meta(x, "mesh_shared") == comp.sub_id]
        if len(refs) > 1 or sharers:
            others = sorted(
                {_meta(o, "name") or "" for o in p.model_settings.findall("object") if o.get("id") in refs and o.get("id") != obj.oid}
            )
            others += sorted({_meta(m, "name") or "" for m in sharers})
            who = others or [f"another part of {self.object_name!r}"]
            raise EditError(f"object {self.object_name!r} shares its mesh with {who}; replacing it is not supported in this release")
        return obj, part, comp

    def resolve_touches(self, p: Project) -> frozenset[str]:
        """The pattern 3D/Objects/* narrowed to the one sub-model of the named object."""
        _, _, comp = self._target(p)
        return frozenset({MODEL, comp.path, MODEL_SETTINGS}) | SLICE_TOUCHES

    def apply(self, p: Project) -> None:
        obj, part, comp = self._target(p)
        geo = _Geometry(p)
        old = geo.mesh(comp)
        new = load_stl(self.stl, self.rotate, self.lay_on_face)
        stl_name = Path(self.stl).name
        old_lo, old_hi = _bbox(old.vertices)
        old_size = _size(old_lo, old_hi)
        if len(old.triangles) == len(new.triangles) and canonical_triangles(old.vertices, old.triangles) == canonical_triangles(
            new.local, new.triangles
        ):
            if _meta(part, "source_file") == stl_name:
                p.notes.append(
                    f"replace-mesh '{self.object_name}': already applied ({stl_name}, {len(new.triangles)} triangles); no change"
                )
                return
            raise EditError(
                f"replacement is a no-op: the STL {stl_name} equals the current mesh of '{self.object_name}' "
                f"({len(new.triangles)} triangles, the same surface and winding to 0.001 mm)"
            )
        if old.paint and not self.drop_paint:
            raise EditError(
                f"'{self.object_name}' holds painted triangles (supports, seam or colour); a new triangle list makes the paint "
                "meaningless. Pass --drop-paint to replace the mesh without it."
            )
        delta = tuple(new.size[i] - old_size[i] for i in range(3))
        size_changed = any(abs(d) > SIZE_TOL for d in delta)
        if size_changed and not self.allow_size_change:
            msg = (
                f"the new mesh of '{self.object_name}' is {_v3(new.size)} mm, the old one {_v3(old_size)} mm "
                f"(delta {_v3(delta)} mm, more than {SIZE_TOL} mm on an axis)"
            )
            if abs(new.size[0] - old_size[1]) <= SIZE_TOL and abs(new.size[1] - old_size[0]) <= SIZE_TOL and abs(delta[2]) <= SIZE_TOL:
                msg += "; the new mesh is turned 90 degrees about z relative to the old one; pass --rotate-mesh 0,0,90"
            elif sorted(new.size) == sorted(old_size) or all(
                abs(a - b) <= SIZE_TOL for a, b in zip(sorted(new.size), sorted(old_size), strict=True)
            ):
                msg += "; the sizes match after swapping axes: the mesh is turned 90 degrees; pass --rotate-mesh or --lay-on-face"
            raise EditError(msg + ". Pass --allow-size-change if the new size is intended.")

        item, ctf = obj.item, comp.transform
        full = _compose(item, ctf)
        lin = _lin(full)
        world_old = geo.world_box(obj)
        part_old = _bbox([_apply(full, v) for v in old.vertices])
        part_new0 = _bbox([_apply(full, v) for v in new.local])
        if self.anchor == "frame":
            offs = [_meta(part, f"source_offset_{a}") for a in "xyz"]
            if any(v is None for v in offs):
                raise EditError(
                    f"part of '{self.object_name}' has no source_offset_*: the STL frame is unknown; pass --anchor centre or min"
                )
            try:
                s_old = tuple(float(v) for v in offs if v is not None)
            except ValueError as e:
                raise EditError(f"part of '{self.object_name}': source_offset_* {offs} is not numeric") from e
            d = (new.centre[0] - s_old[0], new.centre[1] - s_old[1], new.centre[2] - s_old[2])
            shift = _mv(lin, d)
        elif self.anchor == "centre":
            co, cn = _centre(*part_old), _centre(*part_new0)
            shift = (co[0] - cn[0], co[1] - cn[1], co[2] - cn[2])
        else:
            shift = (part_old[0][0] - part_new0[0][0], part_old[0][1] - part_new0[0][1], part_old[0][2] - part_new0[0][2])
        new_item, new_ctf = list(item), list(ctf)
        single = len(obj.comps) == 1
        if single:
            for i in range(3):
                new_item[9 + i] += shift[i]
        else:
            local_shift = _mv(_inverse3(_lin(item)), shift)
            for i in range(3):
                new_ctf[9 + i] += local_shift[i]
        tmp = copy.copy(obj)
        tmp.comps = [(_Comp(c.path, c.sub_id, _fmt_transform(c.transform_text, new_ctf)) if c is comp else c) for c in obj.comps]
        world_new = geo.world_box(tmp, new_item, {comp.sub_id: list(new.local)})
        if world_old[0][2] >= -1e-3:  # ensure_on_bed, unless the object was sunk on purpose
            new_item[11] -= world_new[0][2]
        world_new = geo.world_box(tmp, new_item, {comp.sub_id: list(new.local)})
        moved = math.dist(_centre(*world_old), _centre(*world_new))
        if self.anchor == "frame" and not size_changed and moved > FRAME_TOL:
            s_old = tuple(float(_meta(part, f"source_offset_{a}") or 0) for a in "xyz")
            raise EditError(
                f"the STL frames differ (old source_offset {','.join(f'{v:g}' for v in s_old)}, new "
                f"{','.join(f'{v:g}' for v in new.centre)}): --anchor frame moves '{self.object_name}' {moved:.2f} mm with an "
                "unchanged size; pass --anchor centre"
            )
        top = world_new[1][2]
        for r in _ranges(p, _build_index(p, obj.oid)):
            if float(r["min_z"]) >= top:
                raise EditError(f"height range {r['min_z']}..{r['max_z']} of '{self.object_name}' starts above the new top z {top:.2f}")

        text = _text(p, comp.path)
        p.write_entry(comp.path, (text[: old.span[0]] + _mesh_xml(new.local, new.triangles) + text[old.span[1] :]).encode("utf-8"))
        root_text = _text(p, MODEL)
        if single:
            root_text = _sub_item_transform(root_text, obj.oid, _fmt_transform(obj.item_text, new_item))
        else:
            root_text = _sub_component_transform(root_text, obj.oid, comp.sub_id, _fmt_transform(comp.transform_text, new_ctf))
            root_text = _sub_item_transform(root_text, obj.oid, _fmt_transform(obj.item_text, new_item))
        p.write_entry(MODEL, root_text.encode("utf-8"))

        stat = part.find("mesh_stat")
        old_part_faces = int(stat.get("face_count") or len(old.triangles)) if stat is not None else len(old.triangles)
        if stat is not None:
            stat.set("face_count", str(len(new.triangles)))
            for k in ("edges_fixed", "degenerate_facets", "facets_removed", "facets_reversed", "backwards_edges"):
                if stat.get(k) is not None:
                    stat.set(k, "0")
        for m in obj.ms.findall("metadata"):
            if m.get("face_count") is not None:
                total = int(m.get("face_count") or 0)
                m.set("face_count", str(max(total - old_part_faces, 0) + len(new.triangles)) if not single else str(len(new.triangles)))
        _set_meta(part, "source_file", stl_name)
        for i, a in enumerate("xyz"):
            _set_meta(part, f"source_offset_{a}", _num(new.centre[i]))
        for k in ("source_object_id", "source_volume_id"):
            if _meta(part, k) is not None:
                _set_meta(part, k, "0")
        p.touch_model_settings()

        lo0, hi0 = world_old
        lo1, hi1 = world_new
        p.notes += [
            f"replace-mesh '{self.object_name}': size {_v3(old_size)} -> {_v3(new.size)} mm (delta {_v3(delta)}), "
            f"triangles {len(old.triangles)} -> {len(new.triangles)}",
            f"  world min corner {_v3(lo0)} -> {_v3(lo1)}, centre {_v3(_centre(lo0, hi0))} -> {_v3(_centre(lo1, hi1))} "
            f"(anchor {self.anchor}, moved {moved:.3f} mm)",
            "  thumbnails are stale until Studio saves or exports the project; the slice (if any) was dropped",
        ]
        plate = _plate_of(p, obj.oid)
        for c in layout_checks(p, obj, lo1, hi1, plate, geo):
            if not c.ok:
                p.notes.append(f"  WARN: {c.text}")

    def verify(self, p: Project, before: Project | None = None) -> None:
        """Re-open check: the object's sub-mesh has the STL's triangle count, its centred bbox and its triangles."""
        _, _, comp = self._target(p)
        new = load_stl(self.stl, self.rotate, self.lay_on_face)
        got = read_mesh(_text(p, comp.path), comp.sub_id)
        lo, hi = _bbox(got.vertices)
        if len(got.triangles) != len(new.triangles):
            raise EditError(f"re-open check: '{self.object_name}' has {len(got.triangles)} triangles, the STL {len(new.triangles)}")
        if any(abs(a - b) > 1e-3 for a, b in zip(_size(lo, hi), new.size, strict=True)) or any(abs(c) > 1e-3 for c in _centre(lo, hi)):
            raise EditError(f"re-open check: '{self.object_name}' local bbox {_v3(lo)}..{_v3(hi)} is not the STL's {_v3(new.size)} centred")
        if canonical_triangles(got.vertices, got.triangles) != canonical_triangles(new.local, new.triangles):
            raise EditError(f"re-open check: the triangles written for '{self.object_name}' are not the STL's (surface or winding differs)")


@dataclass(frozen=True)
class MoveObject:
    """Move an object so its world bbox MIN corner is at (x, y) on its plate (plate-local mm, the convention of spec
    `at`), or by (+dx, +dy) with `relative`. `plate` moves it to another plate. Only the build item's translation
    changes; layout rules run as FAIL lines and raise only with `strict`."""

    object_name: str
    x: float
    y: float
    relative: bool = False
    plate: int | None = None
    strict: bool = False
    touches: frozenset[str] = frozenset({MODEL, MODEL_SETTINGS}) | SLICE_TOUCHES

    def describe(self) -> str:
        to = f"by ({self.x:+g}, {self.y:+g}) mm" if self.relative else f"to min corner ({self.x:g}, {self.y:g})"
        return f"object '{self.object_name}': moved {to}" + (f" on plate {self.plate}" if self.plate else "")

    def absolute(self, p: Project) -> MoveObject:
        """The same move as an absolute min-corner target on `p` (EditSet resolves a relative move against its input
        once, so the PatchSet records the target and applying it again is a no-op)."""
        if not self.relative:
            return self
        _, _, dst, _, _, (tx, ty) = self.target(p)
        return MoveObject(self.object_name, round(tx, 6), round(ty, 6), False, self.plate or dst, self.strict)

    def target(self, p: Project) -> tuple[_Obj, int, int, Vec, Vec, tuple[float, float]]:
        if not (math.isfinite(self.x) and math.isfinite(self.y)):
            raise EditError(f"--move {self.object_name}: {self.x}, {self.y} are not finite numbers")
        obj = find_object(p, self.object_name)
        if obj.n_items != 1:
            raise EditError(f"object {self.object_name!r} has {obj.n_items} build items: multi-instance objects are not supported")
        n_plates = len(_plates(p))
        if self.plate is not None and not 1 <= self.plate <= max(n_plates, 1):
            raise EditError(f"plate {self.plate} does not exist: the project has {n_plates} plate(s)")
        geo = _Geometry(p)
        lo, hi = geo.world_box(obj)
        cur = _plate_of(p, obj.oid)
        dst = self.plate or cur
        o_cur = _origin(p, cur)
        if self.relative:
            tx, ty = lo[0] - o_cur[0] + self.x, lo[1] - o_cur[1] + self.y
        else:
            tx, ty = self.x, self.y
        return obj, cur, dst, lo, hi, (tx, ty)

    def apply(self, p: Project) -> None:
        obj, cur, dst, lo, hi, (tx, ty) = self.target(p)
        o_dst = _origin(p, dst)
        dx, dy = tx + o_dst[0] - lo[0], ty + o_dst[1] - lo[1]
        size = _size(lo, hi)
        if abs(dx) > 5e-6 or abs(dy) > 5e-6:  # below the 1e-6 rounding of a resolved relative target: no move
            t = obj.item
            t[9] += dx
            t[10] += dy
            p.write_entry(MODEL, _sub_item_transform(_text(p, MODEL), obj.oid, _fmt_transform(obj.item_text, t)).encode("utf-8"))
            p.invalidate_slices()
        if dst != cur:
            plates = _plates(p)
            mi = next((m for m in plates[cur - 1].findall("model_instance") if _meta(m, "object_id") == obj.oid), None)
            if mi is None:
                raise EditError(f"plate {cur} holds no model_instance of '{self.object_name}': cannot move it to plate {dst}")
            plates[cur - 1].remove(mi)
            plates[dst - 1].append(mi)
            p.touch_model_settings()
        nlo = (tx, ty, lo[2])
        nhi = (tx + size[0], ty + size[1], hi[2])
        c_old = _centre(
            (lo[0] - _origin(p, cur)[0], lo[1] - _origin(p, cur)[1], lo[2]), (hi[0] - _origin(p, cur)[0], hi[1] - _origin(p, cur)[1], hi[2])
        )
        p.notes.append(
            f"move '{self.object_name}': min corner ({lo[0] - _origin(p, cur)[0]:.3f}, {lo[1] - _origin(p, cur)[1]:.3f}) plate {cur} -> "
            f"({tx:.3f}, {ty:.3f}) plate {dst}; centre ({c_old[0]:.3f}, {c_old[1]:.3f}) -> ({tx + size[0] / 2:.3f}, {ty + size[1] / 2:.3f}) "
            "(Studio's position field shows the centre)"
        )
        world_lo = (nlo[0] + o_dst[0], nlo[1] + o_dst[1], nlo[2])
        world_hi = (nhi[0] + o_dst[0], nhi[1] + o_dst[1], nhi[2])
        fails = [c for c in layout_checks(p, obj, world_lo, world_hi, dst, _Geometry(p)) if not c.ok]
        for c in fails:
            p.notes.append(f"  FAIL: {c.text}")
        if fails and self.strict:
            raise EditError(f"--strict: the move of '{self.object_name}' breaks a layout rule: " + "; ".join(c.text for c in fails))

    def verify(self, p: Project, before: Project | None = None) -> None:
        """Re-open check: the min corner is the target, and the size and z range are those of `before`."""
        if self.relative:
            raise EditError("a relative move reached verify(): EditSet resolves it to an absolute target first")
        obj = find_object(p, self.object_name)
        lo, hi = _Geometry(p).world_box(obj)
        o = _origin(p, _plate_of(p, obj.oid))
        if abs(lo[0] - o[0] - self.x) > 1e-3 or abs(lo[1] - o[1] - self.y) > 1e-3:
            raise EditError(
                f"re-open check: '{self.object_name}' min corner is ({lo[0] - o[0]:.4f}, {lo[1] - o[1]:.4f}), not ({self.x}, {self.y})"
            )
        if before is not None:
            blo, bhi = world_box(before, self.object_name)
            if any(abs(a - b) > 1e-3 for a, b in zip(_size(lo, hi), _size(blo, bhi), strict=True)) or abs(lo[2] - blo[2]) > 1e-3:
                raise EditError(
                    f"re-open check: '{self.object_name}' is {_v3(_size(lo, hi))} mm from z {lo[2]:.3f}, it was "
                    f"{_v3(_size(blo, bhi))} mm from z {blo[2]:.3f}"
                )


@dataclass(frozen=True)
class SetTower:
    """The prime tower's min corner (plate-local mm) on one plate, or on every plate when `plate` is None."""

    x: float
    y: float
    plate: int | None = None
    touches: frozenset[str] = frozenset({PROJECT_SETTINGS, MODEL_SETTINGS}) | SLICE_TOUCHES

    def describe(self) -> str:
        return f"prime tower min corner ({self.x:g}, {self.y:g})" + (f" on plate {self.plate}" if self.plate else " on every plate")

    def apply(self, p: Project) -> None:
        ps = p.project_settings
        if not (math.isfinite(self.x) and math.isfinite(self.y)):
            raise EditError(f"--tower {self.x},{self.y}: not finite numbers")
        n_plates = max(len(_plates(p)), 1)
        if self.plate is not None and not 1 <= self.plate <= n_plates:
            raise EditError(f"plate {self.plate} does not exist: the project has {n_plates} plate(s)")
        if p.n_filaments() < 2 or str(ps.get("enable_prime_tower", "1")) in ("0", "false"):
            raise EditError(
                f"the project uses {p.n_filaments()} filament(s) with enable_prime_tower {ps.get('enable_prime_tower', '1')!r}: "
                "Studio prints no prime tower, so there is nothing to place"
            )
        facts = _bed(p)
        bed = facts["bed"]
        try:
            w = max(float(ps.get("prime_tower_width", TOWER_MIN_WIDTH)), TOWER_MIN_WIDTH)
        except (TypeError, ValueError):
            w = TOWER_MIN_WIDTH
        x0, x1, y0, y1 = self.x - TOWER_BRIM, self.x + w + TOWER_BRIM, self.y - TOWER_BRIM, self.y + w + TOWER_BRIM
        foot = (
            f"the tower at ({self.x:g}, {self.y:g}) with width {w:g} and a {TOWER_BRIM:g} mm brim spans x {x0:g}..{x1:g} y {y0:g}..{y1:g}"
        )
        if x0 < bed["x"][0] or x1 > bed["x"][1] or y0 < bed["y"][0] or y1 > bed["y"][1]:
            raise EditError(f"{foot}, off the bed x {bed['x'][0]:g}..{bed['x'][1]:g} y {bed['y'][0]:g}..{bed['y'][1]:g}")
        plates = [self.plate] if self.plate else list(range(1, n_plates + 1))
        used = {e for pl in plates for e in plate_filament_map(p, pl)}
        for e in sorted(used):
            if not 1 <= e <= len(facts["extruders"]):
                continue
            r = facts["extruders"][e - 1]
            if x0 < r["x"][0] or x1 > r["x"][1] or y0 < r["y"][0] or y1 > r["y"][1]:
                raise EditError(
                    f"{foot}, outside extruder {e}'s reach x {r['x'][0]:g}..{r['x'][1]:g} y {r['y'][0]:g}..{r['y'][1]:g}, and a "
                    f"filament on {'plate ' + str(self.plate) if self.plate else 'the project'} maps to extruder {e}"
                )
        for key, v in (("wipe_tower_x", self.x), ("wipe_tower_y", self.y)):
            old = ps.get(key)
            vals = [str(x) for x in old] if isinstance(old, list) else ([str(old)] if old not in (None, "") else [])
            first = vals[0] if vals else _num(v)
            vals = (vals + [first] * n_plates)[:n_plates] if len(vals) < n_plates else vals
            if self.plate is None:
                vals = [_num(v)] * len(vals)
            else:
                vals[self.plate - 1] = _num(v)
            if vals != (old if isinstance(old, list) else None):
                ps[key] = vals
                p.touch_project_settings()
        p.notes.append(
            f"tower: wipe_tower_x {ps['wipe_tower_x']}, wipe_tower_y {ps['wipe_tower_y']} (estimated footprint "
            f"x {x0:g}..{x1:g} y {y0:g}..{y1:g}; Studio sizes the depth from the flush volumes at slice time)"
        )

    def verify(self, p: Project, before: Project | None = None) -> None:
        ps = p.project_settings
        n = max(len(_plates(p)), 1)
        for key, v in (("wipe_tower_x", self.x), ("wipe_tower_y", self.y)):
            vals = ps.get(key) or []
            idx = [self.plate - 1] if self.plate else list(range(n))
            if len(vals) < n or any(abs(float(vals[i]) - v) > 1e-6 for i in idx):
                raise EditError(f"re-open check: {key} is {vals}")


# ---------------------------------------------------------------------------- add a filament
@dataclass(frozen=True)
class FilamentSlot:
    """The values of a new slot N+1: one entry per per-filament key (`single`), one block per per-variant key
    (`block`, len(variants) entries), and Studio's flush matrix for N+1 filaments when it computed one."""

    preset: str
    variants: tuple[str, ...]
    single: tuple[tuple[str, str], ...]
    block: tuple[tuple[str, tuple[str, ...]], ...]
    flush: tuple[str, ...] | None = None
    source: str = "studio"  # "studio": normalised by a CLI export; "preset": the resolved preset JSON only
    notes: tuple[str, ...] = ()


def _as_list(v) -> list[str] | None:
    return [str(x) for x in v] if isinstance(v, list) else None


def check_preset(project_ps: dict, preset: dict, colour: str, extruder: int) -> None:
    """Raise ProjectError for a bad colour, an extruder out of range, or a preset whose compatible_printers excludes the
    project's printer."""
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", colour):
        raise EditError(f"bad colour {colour!r}: use #RRGGBB")
    n_ext = len(project_ps.get("nozzle_diameter") or ["0.4"])
    if not 1 <= extruder <= n_ext:
        raise EditError(f"extruder {extruder} is not in 1..{n_ext} (the printer has {n_ext} extruder(s))")
    if preset.get("type") not in (None, "filament"):
        raise EditError(f"preset {preset.get('name')!r} is a {preset.get('type')} preset, not a filament")
    printer = str(project_ps.get("printer_settings_id") or "")
    parent = str(project_ps.get("inherits") or "")
    cp = preset.get("compatible_printers") or []
    if cp and printer not in cp and parent not in cp:
        raise EditError(f"filament {preset.get('name')!r} compatible_printers {cp} does not list the project printer {printer!r}")


# The shapes of the per-filament keys, explicit (never guessed from a length):
# - per variant: one entry per extruder variant of each filament, in the blocks that filament_self_index numbers
#   (PrintConfig.cpp:7464 filament_options_with_variant); a block's length comes from its preset's
#   filament_extruder_variant, so two filaments may have blocks of different lengths;
# - per filament: every other list key of a filament preset, plus PROJECT_ONLY_KEYS and MAP_KEYS (one entry each);
# - per extruder (nozzle_diameter, extruder_type, ...), per plate (model_settings filament_maps) and the flush matrix
#   (E x N x N) are handled by name in AddFilament;
# - ODD_FILAMENT_KEYS: AMS drying tables a preset carries in neither shape; they are kept as they are.
FILAMENT_VARIANT_KEYS = frozenset(
    {
        "filament_flow_ratio", "filament_max_volumetric_speed", "filament_ramming_volumetric_speed", "filament_pre_cooling_temperature",
        "filament_ramming_travel_time", "filament_ramming_volumetric_speed_nc", "filament_pre_cooling_temperature_nc",
        "filament_ramming_travel_time_nc", "filament_extruder_variant", "filament_retraction_length", "filament_retract_length_nc",
        "filament_z_hop", "filament_z_hop_types", "filament_retract_restart_extra", "filament_retraction_speed",
        "filament_deretraction_speed", "filament_retraction_minimum_travel", "filament_retract_when_changing_layer", "filament_wipe",
        "filament_wipe_distance", "filament_retract_before_wipe", "filament_long_retractions_when_cut",
        "filament_retraction_distances_when_cut", "long_retractions_when_ec", "retraction_distances_when_ec",
        "nozzle_temperature_initial_layer", "nozzle_temperature", "filament_flush_volumetric_speed", "filament_flush_temp",
        "filament_flush_temp_fast", "filament_enable_overhang_speed", "filament_bridge_speed", "filament_overhang_1_4_speed",
        "filament_overhang_2_4_speed", "filament_overhang_3_4_speed", "filament_overhang_4_4_speed", "filament_overhang_totally_speed",
        "override_process_overhang_speed", "volumetric_speed_coefficients", "filament_adaptive_volumetric_speed",
        "filament_preheat_temperature_delta", "filament_cooling_before_tower", "slow_down_min_speed",
    }
)  # fmt: skip
ODD_FILAMENT_KEYS = frozenset(
    {"filament_dev_ams_drying_temperature", "filament_dev_ams_drying_time", "filament_dev_ams_drying_ams_limitations"}
)
_PRESET_META = {"name", "type", "from", "inherits", "instantiation", "setting_id", "filament_id", "compatible_printers", "compatible_printers_condition",
                "compatible_prints", "compatible_prints_condition", "is_custom_defined", "version", "description"}  # fmt: skip


@dataclass(frozen=True)
class FilamentShape:
    """The project's filament layout: N filaments, the variant block of each (index ranges of filament_self_index),
    E extruders, and which keys are per filament or per variant."""

    n: int
    blocks: tuple[tuple[int, ...], ...]  # per filament, the indexes of its variant entries
    e: int
    per_filament: tuple[str, ...]
    per_variant: tuple[str, ...]

    @property
    def v(self) -> int:
        return sum(len(b) for b in self.blocks)


def filament_shape(ps: dict, preset: dict) -> FilamentShape:
    """Lengths from filament_settings_id, filament_self_index and nozzle_diameter; key kinds from the schema above and
    the new preset's keys. Raises EditError for a project whose variant blocks do not line up."""
    n = len(ps.get("filament_settings_id") or [])
    fsi = [str(x) for x in ps.get("filament_self_index") or []]
    fev = ps.get("filament_extruder_variant") or []
    if len(fsi) != len(fev):
        raise EditError(
            f"filament_extruder_variant ({len(fev)}) and filament_self_index ({len(fsi)}) differ in length: the variant blocks are "
            "collapsed (an old `shambu patch --map`). Re-export the project in Studio, then add the filament."
        )
    blocks = tuple(tuple(j for j, x in enumerate(fsi) if x == str(i)) for i in range(1, n + 1))
    if (
        any(not b for b in blocks)
        or sorted(j for b in blocks for j in b) != list(range(len(fsi)))
        or [int(x) for x in fsi] != sorted(int(x) for x in fsi)
    ):
        raise EditError(f"filament_self_index {fsi} does not number {n} filament blocks in order; the project is not a Studio layout")
    per_variant = sorted(k for k in FILAMENT_VARIANT_KEYS if isinstance(ps.get(k), list) and k != "filament_extruder_variant")
    keys = {k for k, v in preset.items() if isinstance(v, list)} | set(PROJECT_ONLY_KEYS) | {"filament_ids"}
    per_filament = sorted(
        k for k in keys - set(FILAMENT_VARIANT_KEYS) - ODD_FILAMENT_KEYS - _PRESET_META - _SPECIAL_KEYS if isinstance(ps.get(k), list)
    )
    return FilamentShape(n, blocks, len(ps.get("nozzle_diameter") or ["0.4"]), tuple(per_filament), tuple(per_variant))


def _short(key: str, cur: list, want: int, what: str) -> bool:
    """True for a one-entry vector that means 'every slot uses entry 0' (Config.hpp:681-685); EditError for any other
    length that is not `want`."""
    if len(cur) == want:
        return False
    if len(cur) == 1:
        return True
    raise EditError(f"{key} has {len(cur)} entries; a {what} key needs {want} (or 1): an ambiguous shape, not extended")


def _variants_of(preset: dict, ps: dict, shape: FilamentShape) -> tuple[tuple[str, ...], tuple[str, ...]]:
    pv = _as_list(preset.get("filament_extruder_variant"))
    if pv:
        return tuple(pv), ()
    fev = ps.get("filament_extruder_variant") or []
    first = tuple(str(fev[j]) for j in shape.blocks[0]) if shape.blocks else ("",)
    return first, (f"the preset names no filament_extruder_variant: slot {shape.n + 1} takes slot 1's variant list {list(first)}",)


def slot_presets(ps: dict, preset: dict) -> list[dict]:
    """One filament preset JSON per existing slot, built from the project's own settings (a GUI project can hold a
    custom or deleted preset, so the slot names are never re-resolved). Only slot N+1 of the export that loads these
    is read back."""
    shape = filament_shape(ps, preset)
    fsid = [str(x) for x in ps.get("filament_settings_id") or []]
    cols = [str(x) for x in ps.get("filament_colour") or []]
    ids = [str(x) for x in ps.get("filament_ids") or []]
    fev = ps.get("filament_extruder_variant") or []
    out = []
    for i in range(shape.n):
        d: dict = {"type": "filament", "name": fsid[i], "from": "User", "inherits": "", "filament_settings_id": [fsid[i]]}
        d["filament_id"] = ids[i] if i < len(ids) else (ids[0] if ids else "")
        d["filament_colour"] = [cols[i] if i < len(cols) else (cols[0] if cols else "#FFFFFF")]
        for k in shape.per_filament:
            cur = ps[k]
            if k in PROJECT_ONLY_KEYS or k in MAP_KEYS:
                continue
            d[k] = [str(cur[i] if len(cur) == shape.n else cur[0])]
        for k in shape.per_variant:
            cur = ps[k]
            d[k] = [str(cur[j] if len(cur) == shape.v else cur[0]) for j in shape.blocks[i]]
        d["filament_extruder_variant"] = [str(fev[j]) for j in shape.blocks[i]]
        out.append(d)
    return out


def slot_from_preset(project_ps: dict, preset: dict) -> FilamentSlot:
    """Slot values straight from a resolved preset JSON (no Studio): not normalised (`"100"` stays `"100"`)."""
    shape = filament_shape(project_ps, preset)
    variants, notes = _variants_of(preset, project_ps, shape)
    b = len(variants)
    single: dict[str, str] = {}
    block: dict[str, tuple[str, ...]] = {}
    for k in shape.per_variant:
        v = preset.get(k)
        if not isinstance(v, list) or not v:
            continue
        if len(v) not in (1, b):
            raise EditError(f"preset {preset.get('name')!r}: {k} has {len(v)} entries for {b} variants: an ambiguous shape")
        block[k] = tuple(str(x) for x in (v if len(v) == b else v * b))
    for k in shape.per_filament:
        v = preset.get(k)
        if isinstance(v, list) and v:
            single[k] = str(v[0])
    single["filament_settings_id"] = str(preset.get("name", ""))
    if "filament_id" in preset:
        single["filament_ids"] = str(preset["filament_id"])
    notes += _project_only(project_ps, preset, single)
    return FilamentSlot(
        str(preset.get("name", "")), variants, tuple(sorted(single.items())), tuple(sorted(block.items())), None, "preset", notes
    )


def _project_only(project_ps: dict, preset: dict, single: dict[str, str]) -> tuple[str, ...]:
    n = len(project_ps.get("filament_settings_id") or [])
    copied = []
    for k in PROJECT_ONLY_KEYS:
        cur = project_ps.get(k)
        if k in single or not isinstance(cur, list) or len(cur) != n:
            continue
        pv = preset.get(k)
        if isinstance(pv, list) and pv:
            single[k] = str(pv[0])
        elif pv is not None and not isinstance(pv, list):
            single[k] = str(pv)
        else:
            single[k] = str(cur[0]) if cur else ""
            if k != "filament_multi_colour":  # AddFilament writes the new colour there
                copied.append(k)
    return (f"copied from slot 1 (no preset value): {', '.join(copied)}",) if copied else ()


def slot_from_export(project_ps: dict, normalised_ps: dict, preset: dict, flush_ps: dict | None = None) -> FilamentSlot:
    """Slot values from two scratch CLI exports. `normalised_ps`: a scratch copy loaded with the new preset ALONE, so it
    is slot 1 there; entry 0 of every per-filament key and the first block of every per-variant key are Studio's
    normalised values. (An export loaded with N+1 filaments does not do: it writes the per-filament keys in slot order
    but the loaded preset's variant block FIRST and filament_self_index all "1", live on 02.08.02.61.) `flush_ps`: the
    export loaded with slot_presets() plus the new preset and N+1 colours, read only for its E x (N+1)^2 flush matrix.
    A key the export lacks or holds in another shape takes the preset's value, and the notes say so."""
    shape = filament_shape(project_ps, preset)
    variants, notes = _variants_of(preset, project_ps, shape)
    b = len(variants)
    got = [str(x) for x in normalised_ps.get("filament_settings_id") or []]
    if not got or got[0] != str(preset.get("name", "")):
        raise EditError(f"the scratch export's slot 1 is {got[:1]}, not the new preset {preset.get('name')!r}: its values cannot be read")
    single: dict[str, str] = {}
    block: dict[str, tuple[str, ...]] = {}
    from_preset: list[str] = []
    for k in shape.per_variant:
        v = normalised_ps.get(k)
        if isinstance(v, list) and len(v) >= b and len(v) % b == 0 and b > 0:
            block[k] = tuple(str(x) for x in v[:b])
        elif isinstance(v, list) and len(v) == 1:
            block[k] = (str(v[0]),) * b
        elif isinstance(preset.get(k), list) and len(preset[k]) in (1, b):
            pv = [str(x) for x in preset[k]]
            block[k] = tuple(pv if len(pv) == b else pv * b)
            from_preset.append(k)
    for k in shape.per_filament:
        v = normalised_ps.get(k)
        if k in PROJECT_ONLY_KEYS:
            continue  # the CLI keeps the project's own values there: _project_only() decides, with a note
        if isinstance(v, list) and v:
            single[k] = str(v[0])
        elif isinstance(preset.get(k), list) and preset[k]:
            single[k] = str(preset[k][0])
            from_preset.append(k)
    single["filament_settings_id"] = str(preset.get("name", ""))
    notes += _project_only(project_ps, preset, single)
    if from_preset:
        notes += (f"from the preset JSON (the export holds them in another shape): {', '.join(sorted(from_preset))}",)
    fvm = (flush_ps or {}).get("flush_volumes_matrix")
    flush = tuple(str(x) for x in fvm) if isinstance(fvm, list) and len(fvm) == shape.e * (shape.n + 1) ** 2 else None
    return FilamentSlot(
        str(preset.get("name", "")), variants, tuple(sorted(single.items())), tuple(sorted(block.items())), flush, "studio", notes
    )


def variant_lookup(ps: dict) -> dict[int, int | None]:
    """Python port of get_index_for_extruder (PrintConfig.cpp:8022-8052): for each filament (1-based) the index in
    filament_extruder_variant of the variant its mapped extruder needs, or None (Studio logs 'could not found
    extruder_type' and falls back to the filament's first block)."""
    et, nvt = ps.get("extruder_type") or [], ps.get("nozzle_volume_type") or []
    fev, fsi = ps.get("filament_extruder_variant") or [], ps.get("filament_self_index") or []
    fmap = ps.get("filament_map") or []
    out: dict[int, int | None] = {}
    for i in range(1, len(ps.get("filament_settings_id") or []) + 1):
        e = int(fmap[i - 1]) if i - 1 < len(fmap) else (int(fmap[0]) if fmap else 1)
        volume = nvt[e - 1] if e - 1 < len(nvt) else "?"
        if volume == "Hybrid":  # not in presets: Studio matches Standard instead (PrintConfig.cpp:8031)
            volume = "Standard"
        want = f"{et[e - 1] if e - 1 < len(et) else '?'} {volume}"
        out[i] = next((k for k in range(min(len(fev), len(fsi))) if str(fsi[k]) == str(i) and fev[k] == want), None)
    return out


@dataclass(frozen=True)
class AddFilament:
    """Append filament slot N+1 (preset, colour, extruder). Slots 1..N stay byte-identical. A no-op when the last
    slot already is this preset, colour and extruder, so the same PatchSet applied twice adds one slot."""

    slot: FilamentSlot
    colour: str
    extruder: int = 1
    allow_duplicate: bool = False
    touches: frozenset[str] = frozenset({PROJECT_SETTINGS, MODEL_SETTINGS}) | SLICE_TOUCHES

    def describe(self) -> str:
        return f"filament added: {self.slot.preset} {self.colour.upper()} on extruder {self.extruder} ({self.slot.source} values)"

    def apply(self, p: Project) -> None:
        ps = p.project_settings
        n = p.n_filaments()
        colour = self.colour.upper()
        fsid = [str(x) for x in ps.get("filament_settings_id") or []]
        cols = [str(x).upper() for x in ps.get("filament_colour") or []]
        fmap = [str(x) for x in ps.get("filament_map") or []]
        check_preset(ps, {"type": "filament"}, colour, self.extruder)
        if (
            n
            and fsid[-1] == self.slot.preset
            and (cols[n - 1] if len(cols) >= n else "") == colour
            and (fmap[n - 1] if len(fmap) >= n else "1") == str(self.extruder)
        ):
            p.notes.append(f"add-filament: slot {n} already is {self.slot.preset} {colour} on extruder {self.extruder}; no change")
            return
        dup = [i + 1 for i in range(n) if fsid[i] == self.slot.preset and i < len(cols) and cols[i] == colour]
        if dup and not self.allow_duplicate:
            raise EditError(f"slot {dup[0]} already is {self.slot.preset} {colour}; pass --allow-duplicate to add a second one")
        fev = ps.get("filament_extruder_variant")
        fsi = ps.get("filament_self_index")
        if not isinstance(fev, list) or not isinstance(fsi, list):
            raise EditError("the project has no filament_extruder_variant / filament_self_index: not a Studio 2.x project")
        filament_shape(ps, {})  # raises for collapsed or unordered variant blocks
        shape = filament_shape(ps, {})
        before = copy.deepcopy(ps)
        lookup_before = variant_lookup(ps)
        v_old = shape.v
        changed: list[str] = []
        skipped: list[str] = []
        fallbacks: list[str] = []
        singles, blocks = dict(self.slot.single), dict(self.slot.block)
        named = {"filament_colour": colour, "filament_multi_colour": colour}
        named.update(
            filament_map=str(self.extruder),
            filament_nozzle_map=str(self.extruder),
            filament_volume_map="0",
            filament_map_2=str(self.extruder),
        )
        # every recognised per-filament key: the slot's own, the project-only ones, the colours and the maps. A
        # one-entry vector ("every slot uses entry 0") is written out first; a key with no value for the new slot
        # takes slot 1's value, and the report names it.
        for k in sorted(set(singles) | set(PROJECT_ONLY_KEYS) | set(named)):
            cur = ps.get(k)
            if not isinstance(cur, list):
                if k in ps and k in singles:
                    skipped.append(k)
                continue
            if not cur:
                raise EditError(f"{k} is empty; a per-filament key needs {n} entries: an ambiguous shape, not extended")
            if _short(k, cur, n, "per-filament"):
                cur[:] = [cur[0]] * n
            if k in named:
                v = named[k]
            elif k in singles:
                v = singles[k]
            else:
                v = str(cur[0])
                fallbacks.append(k)
            cur.append(v)
            changed.append(k)
        # every recognised per-variant key (Studio's list): the slot's block, else slot 1's first entry per variant
        b = len(self.slot.variants)
        for k in sorted(set(blocks) | (FILAMENT_VARIANT_KEYS - {"filament_extruder_variant"})):
            cur = ps.get(k)
            if not isinstance(cur, list) or not cur:
                if k in ps and k in blocks:
                    skipped.append(k)
                continue
            if _short(k, cur, v_old, "per-variant"):
                cur[:] = [cur[0]] * v_old
            if k in blocks:
                vals = blocks[k]
            else:
                vals = (str(cur[shape.blocks[0][0]]),) * b
                fallbacks.append(k)
            cur.extend(vals)
            changed.append(k)
        fev.extend(self.slot.variants)
        fsi.extend([str(n + 1)] * b)
        for k in ("different_settings_to_system", "inherits_group"):
            cur = ps.get(k)
            if isinstance(cur, list) and len(cur) == n + 2:
                cur.insert(n + 1, "")
        e = len(ps.get("nozzle_diameter") or ["0.4"])
        old_fvm = [str(x) for x in ps.get("flush_volumes_matrix") or []]
        new_fvm: list[str] = []
        placeholder = False
        if len(old_fvm) != e * n * n and self.slot.flush is None:
            raise EditError(f"flush_volumes_matrix has {len(old_fvm)} entries, not {e} x {n} x {n}, and Studio did not compute a new one")
        offdiag = [float(x) for i, x in enumerate(old_fvm) if (i % (n * n)) // n != (i % (n * n)) % n] if n else []
        for ex in range(e):
            for i in range(n + 1):
                for j in range(n + 1):
                    if i < n and j < n and len(old_fvm) == e * n * n:
                        new_fvm.append(old_fvm[ex * n * n + i * n + j])
                    elif self.slot.flush is not None:
                        new_fvm.append(self.slot.flush[ex * (n + 1) ** 2 + i * (n + 1) + j])
                    else:
                        placeholder = placeholder or i != j
                        new_fvm.append("0" if i == j else (_num(max(offdiag)) if offdiag else PLACEHOLDER_FLUSH))
        ps["flush_volumes_matrix"] = new_fvm
        p.touch_project_settings()
        for pl in _plates(p):
            fm = _meta(pl, "filament_maps")
            if fm is not None and len(fm.split()) == n:
                _set_meta(pl, "filament_maps", f"{fm} {self.extruder}".strip())
            fv = _meta(pl, "filament_volume_maps")
            if fv is not None and len(fv.split()) == n:
                _set_meta(pl, "filament_volume_maps", f"{fv} 0".strip())
        p.touch_model_settings()
        self._check(p, before, lookup_before, n, changed, e, set(blocks) | (FILAMENT_VARIANT_KEYS - {"filament_extruder_variant"}))
        p.notes.append(
            f"add-filament: slot {n + 1} = {self.slot.preset} {colour} on extruder {self.extruder}; {len(changed)} per-filament keys "
            f"extended, flush_volumes_matrix {e} x {n + 1} x {n + 1}, plate filament_maps extended; slots 1..{n} unchanged"
        )
        p.notes += [f"  {x}" for x in self.slot.notes]
        if skipped:
            p.notes.append(f"  kept (not one entry per filament in this project): {', '.join(sorted(skipped))}")
        if fallbacks:
            p.notes.append(f"  WARN: no value for slot {n + 1}, slot 1's value copied: {', '.join(sorted(fallbacks))}")
        if self.slot.source != "studio":
            p.notes.append(
                "  WARN: values not normalised by Studio (no Bambu Studio binary): the slot holds the resolved preset JSON as is"
            )
        if placeholder:
            p.notes.append(
                f"  WARN: flush volumes to and from slot {n + 1} are a placeholder ({PLACEHOLDER_FLUSH} mm3 or the largest existing "
                "value): Studio computes them from the colours when the project is opened or re-exported"
            )
        p.notes.append(f"  slot {n + 1} is unused until --extruder, --set OBJ:support_interface_filament={n + 1} or --range uses it")

    def _check(self, p: Project, before: dict, lookup_before: dict, n: int, changed: list[str], e: int, variant_keys: set[str]) -> None:
        """The whole recognised schema after the edit: lengths, the blocks, the variant lookup, the flush matrix, the
        plate maps, that the requested colour and extruder landed, and that slots 1..N are unchanged."""
        ps = p.project_settings
        bad: list[str] = []
        v = len(ps["filament_extruder_variant"])
        for k in (
            set(PROJECT_ONLY_KEYS) | set(MAP_KEYS) | {"filament_colour", "filament_multi_colour", "filament_settings_id"} | set(changed)
        ):
            cur = ps.get(k)
            if isinstance(cur, list) and k not in variant_keys and len(cur) != n + 1:
                bad.append(f"{k} has {len(cur)} entries, not {n + 1}")
        for k in variant_keys:
            cur = ps.get(k)
            if isinstance(cur, list) and cur and len(cur) != v:
                bad.append(f"{k} has {len(cur)} entries, not {v} (one per variant)")
        for k, want in (("filament_colour", self.colour.upper()), ("filament_map", str(self.extruder))):
            cur = ps.get(k)
            if isinstance(cur, list) and (len(cur) <= n or str(cur[n]).upper() != want):
                bad.append(f"{k} slot {n + 1} is {cur[n] if len(cur) > n else 'missing'}, not the requested {want}")
        fsi = [int(x) for x in ps["filament_self_index"]]
        if fsi != sorted(fsi) or sorted(set(fsi)) != list(range(1, n + 2)):
            bad.append(f"filament_self_index {ps['filament_self_index']} does not cover 1..{n + 1} in order")
        look = variant_lookup(ps)
        for i, hit in look.items():
            if hit is None and (i == n + 1 or lookup_before.get(i) is not None):
                bad.append(
                    f"filament {i}: its extruder's variant is not in filament_extruder_variant (Studio: 'could not found extruder_type')"
                )
        if len(ps["flush_volumes_matrix"]) != e * (n + 1) ** 2:
            bad.append(f"flush_volumes_matrix has {len(ps['flush_volumes_matrix'])} entries, not {e * (n + 1) ** 2}")
        for pl in _plates(p):
            fm = _meta(pl, "filament_maps")
            if fm is not None and len(fm.split()) != n + 1:
                bad.append(f"plate {_meta(pl, 'plater_id')} filament_maps {fm!r} has not {n + 1} entries")
        for k, old in before.items():
            got = ps.get(k)
            new: list = got if isinstance(got, list) else []
            if isinstance(old, list) and not isinstance(got, list):
                bad.append(f"{k} is no longer a list")
            elif k in ("different_settings_to_system", "inherits_group") and isinstance(old, list) and len(old) == n + 2:
                if new[: n + 1] != old[: n + 1] or new[-1] != old[-1]:
                    bad.append(f"{k}: an existing entry changed")
            elif k == "flush_volumes_matrix":
                continue
            elif isinstance(old, list):
                if new[: len(old)] != old or (len(old) == 1 and k not in variant_keys and any(x != old[0] for x in new[:n])):
                    bad.append(f"{k}: an existing slot's entry changed")
            elif got != old:
                bad.append(f"{k} changed")
        if bad:
            raise EditError("add-filament self-check failed: " + "; ".join(bad))


# ---------------------------------------------------------------------------- the composite
def touches_of(patches: Iterable[Patch], project: Project) -> frozenset[str]:
    """Union of what the patches declare; a patch without `touches` (every patch in patches.py) declares Metadata/*."""
    out: set[str] = set()
    for p in patches:
        resolve = getattr(p, "resolve_touches", None)
        out |= resolve(project) if callable(resolve) else getattr(p, "touches", DEFAULT_TOUCHES)
    return frozenset(out)


@dataclass
class EditSet(PatchSet):
    """A PatchSet that may hold edits. `apply()` resolves a relative move to its absolute target (recorded in the set,
    so applying the set again is a no-op), applies every patch, and saves with the union of their `touches`; the save
    writes a temporary archive, runs each edit's `verify(after, before)` and the caller's `check(tmp)` on it, and only
    then replaces the destination. `notes` holds the box reports, FAIL and WARN lines of the last apply or preview."""

    notes: list[str] = field(default_factory=list)

    def _run(self, project: Project) -> list[str]:
        self.patches = [x.absolute(project) if isinstance(x, MoveObject) else x for x in self.patches]
        done = []
        for patch in self.patches:
            try:
                patch.apply(project)
            except (ValueError, KeyError, IndexError, StopIteration, ET.ParseError) as e:
                raise EditError(f"{patch.describe()}: malformed project data ({type(e).__name__}: {e})") from e
            done.append(patch.describe())
        return done

    def preview(self, path: str) -> list[str]:
        """Apply in memory without writing (for --dry-run), with the same touches check as a save: the notes, including
        the move targets."""
        project = Project(path)
        self._run(project)
        project.check_touches(touches_of(self.patches, project))
        self.notes = list(project.notes)
        return self.notes

    def apply(self, path: str, out: str | None = None, check: Callable[[str], None] | None = None) -> list[str]:
        if not self.patches:
            return []
        before, project = Project(path), Project(path)
        done = self._run(project)

        def verify(tmp: str) -> None:
            after = Project(tmp)
            for patch in self.patches:
                v = getattr(patch, "verify", None)
                if callable(v):
                    v(after, before)
            if check is not None:
                check(tmp)

        project.save(out or path, touches_of(self.patches, project), verify=verify)
        self.notes = list(project.notes)
        return done
