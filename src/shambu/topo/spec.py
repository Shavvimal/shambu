"""The topology spec (`shambu.topo/1`), the faces file (`shambu.topo.faces/1`) and the trace rules, in pure Python.

What it handles: reading and validating the JSON documents `shambu topo` takes, with one error line per problem
(unknown key, wrong type, a value out of range, an unknown selection primitive, a case with nothing in it), and the
frozen dataclasses the solver reads. Units are mm, N and MPa throughout (`"units": "mm-N-MPa"`).

A selection is one primitive or a combination:

- `{"box": {"x": [lo, hi], "y": [...], "z": [...], "abs_x": [lo, hi], ...}}`: an axis left out is unbounded; `abs_*`
  bounds the absolute value (two mirrored boxes in one).
- `{"ball": {"c": [x, y, z], "r": r}}`
- `{"cyl": {"axis": "z", "c": [a, b], "r": r, "range": [lo, hi]}}`: `c` is the centre in the other two axes, in
  x, y, z order.
- `{"annulus": {"axis": "z", "c": [a, b], "r_in": r1, "r_out": r2, "range": [lo, hi]}}`
- `{"half": {"p": [x, y, z], "n": [nx, ny, nz]}}`: the side the normal points to.
- `{"union": [s, ...]}`, `{"intersect": [s, ...]}`, `{"minus": [a, b]}`.

What it does not do: no numpy, no geometry, no file other than the one JSON it is asked to read, no printing.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..errors import TopoError, TopoSpecError

SCHEMA = "shambu.topo/1"
FACES_SCHEMA = "shambu.topo.faces/1"
RULES_SCHEMA = "shambu.topo.rules/1"
UNITS = "mm-N-MPa"
AXES = ("x", "y", "z")
PRIMITIVES = ("box", "ball", "cyl", "annulus", "half", "union", "intersect", "minus")
EDGES = ("u_lo", "u_hi", "v_lo", "v_hi")

# Generic PETG-like defaults (a datasheet order of magnitude, not a measured filament). A spec should state its own.
DEFAULT_E_MPA = 2100.0
DEFAULT_NU = 0.38
DEFAULT_DENSITY_G_CM3 = 1.27


# ---------------------------------------------------------------------------- dataclasses
@dataclass(frozen=True)
class Material:
    E: float = DEFAULT_E_MPA
    nu: float = DEFAULT_NU
    density_g_cm3: float = DEFAULT_DENSITY_G_CM3
    fill: float = 1.0  # the share of the solid that is plastic, for self-weight only


@dataclass(frozen=True)
class Simp:
    penal: float = 3.0
    emin_rel: float = 1e-9
    rmin: float | None = None  # mm; None: rmin_cells x pitch
    rmin_cells: float = 2.5
    move: float = 0.2
    eta: float = 0.5
    volfrac: tuple[float, ...] = (0.5,)
    maxit: int = 80
    tol: float = 0.01
    budget_min: float = 40.0
    min_member_mm: float | None = None  # the thinnest member the printer makes as a member, for the filter warning

    def rmin_mm(self, pitch: float) -> float:
        return self.rmin if self.rmin is not None else self.rmin_cells * pitch


@dataclass(frozen=True)
class Domain:
    stl: str | None  # resolved path, or None for a box domain
    box: tuple[tuple[float, float, float], tuple[float, float, float]] | None
    pitch: float
    subsamples: int = 3


@dataclass(frozen=True)
class Support:
    name: str
    nodes: dict
    dofs: str = "xyz"


@dataclass(frozen=True)
class Load:
    selection: dict
    on: str  # "cells" (the nodes of the selected cells) or "nodes"
    force: tuple[float, float, float] = (0.0, 0.0, 0.0)
    moment: tuple[float, float, float] = (0.0, 0.0, 0.0)
    about: tuple[float, float, float] | None = None  # None: the centroid of the loaded nodes
    grow_if_empty: float = 0.0


@dataclass(frozen=True)
class BodyForce:
    accel_g: tuple[float, float, float]
    scale_with_density: bool = True


@dataclass(frozen=True)
class Case:
    name: str
    weight: float | None
    loads: tuple[Load, ...]
    body_force: BodyForce | None = None


@dataclass(frozen=True)
class Region:
    name: str
    cells: dict


@dataclass(frozen=True)
class TopoSpec:
    path: str
    sha256: str
    domain: Domain
    material: Material
    simp: Simp
    supports: tuple[Support, ...]
    passive: tuple[Region, ...]
    cases: tuple[Case, ...]
    weights: str  # "normalised" or "raw"
    regions: tuple[Region, ...] = ()


@dataclass(frozen=True)
class TraceRules:
    """The constants of `topo trace`. Two presets: `conservative` keeps more material, `aggressive` removes more."""

    name: str = "conservative"
    keep_volfrac: float | None = None  # the run to trace; None: the first run in run.json
    threshold: float = 0.5  # slab-mean density below this is a candidate
    raster_mm: float = 0.25
    rib_mm: float = 5.0
    rim_mm: float = 5.0
    corner_r_mm: float = 2.5
    clip_r_mm: float | None = None  # an extra rounding where a keep-out clips a window
    clearance_mm: float = 3.0
    margin_mm: float = 0.5  # what simplification may move an edge; keep-outs grow by clearance + margin
    min_area_mm2: float = 80.0
    min_width_mm: float = 6.0
    max_vertices: int = 60
    apex_mm: float = 1.0
    roof_deg: float = 45.0  # the steepest roof the rule makes, from horizontal
    flat_report_deg: float = 43.0  # a roof edge flatter than this is a bridge to check
    other_grow: str = "rib"  # grow attached material (ribs, bosses) by the rib ("rib") or the rim ("rim") width
    rib_passes: int = 4


PRESETS: dict[str, TraceRules] = {
    "conservative": TraceRules(),
    "aggressive": TraceRules(
        name="aggressive",
        rib_mm=4.0,
        rim_mm=3.0,
        corner_r_mm=2.0,
        clip_r_mm=3.0,
        clearance_mm=2.0,
        min_area_mm2=40.0,
        min_width_mm=5.0,
        other_grow="rim",
    ),
}


@dataclass(frozen=True)
class Face:
    name: str
    axis: str  # the wall normal axis
    slab: tuple[float, float]  # the wall's extent along the normal
    u: tuple[float, float]  # the face rectangle in the first other axis (x, y, z order)
    v: tuple[float, float]  # ... and in the second
    open_edges: tuple[str, ...] = ()
    keepouts: tuple[dict, ...] = ()  # balls: {"c": [x, y, z], "r": r}


@dataclass(frozen=True)
class Faces:
    build_axis: str  # "x", "y" or "z": the printer's up axis in the run's frame
    faces: tuple[Face, ...]
    keepouts: tuple[dict, ...] = ()


def plane_axes(axis: str) -> tuple[int, int]:
    """The two in-plane axis indexes of a face with this normal, in x, y, z order."""
    n = AXES.index(axis)
    a, b = (i for i in range(3) if i != n)
    return a, b


# ---------------------------------------------------------------------------- validation helpers
class _V:
    """Collects error lines; each helper returns a usable default after an error, so one pass finds every error."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def err(self, where: str, msg: str) -> None:
        self.errors.append(f"{where}: {msg}")

    def keys(self, d: Any, where: str, allowed: set[str], required: set[str] | None = None) -> dict:
        if not isinstance(d, dict):
            self.err(where, f"must be an object, got {type(d).__name__}")
            return {}
        for k in sorted(set(d) - allowed):
            self.err(where, f"unknown key {k!r} (allowed: {', '.join(sorted(allowed))})")
        for k in sorted((required or set()) - set(d)):
            self.err(where, f"missing key {k!r}")
        return d

    def num(
        self, v: Any, where: str, lo: float | None = None, hi: float | None = None, lo_open: bool = False, hi_open: bool = False
    ) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            self.err(where, f"must be a finite number, got {v!r}")
            return 0.0
        x = float(v)
        if lo is not None and (x < lo or (lo_open and x == lo)):
            self.err(
                where,
                f"{x:g} is below the range {'(' if lo_open else '['}{lo:g}, {'inf' if hi is None else f'{hi:g}'}{')' if hi_open or hi is None else ']'}",
            )
        if hi is not None and (x > hi or (hi_open and x == hi)):
            self.err(
                where,
                f"{x:g} is above the range {'(' if lo_open else '['}{lo if lo is not None else '-inf'}, {hi:g}{')' if hi_open else ']'}",
            )
        return x

    def vec(self, v: Any, where: str, n: int = 3) -> tuple[float, ...]:
        if not isinstance(v, list) or len(v) != n:
            self.err(where, f"must be a list of {n} numbers, got {v!r}")
            return (0.0,) * n
        return tuple(self.num(x, f"{where}[{i}]") for i, x in enumerate(v))

    def interval(self, v: Any, where: str) -> tuple[float, float]:
        lo, hi = self.vec(v, where, 2)
        if lo > hi:
            self.err(where, f"low {lo:g} is above high {hi:g}")
        return lo, hi

    def text(self, v: Any, where: str) -> str:
        if not isinstance(v, str) or not v.strip():
            self.err(where, f"must be a non-empty string, got {v!r}")
            return ""
        return v

    def axis(self, v: Any, where: str) -> str:
        if v not in AXES:
            self.err(where, f"must be one of x, y, z, got {v!r}")
            return "z"
        return v

    def selection(self, s: Any, where: str) -> dict:
        if not isinstance(s, dict) or len(s) != 1:
            self.err(where, f"a selection is an object with exactly one primitive ({', '.join(PRIMITIVES)}), got {s!r}")
            return {}
        (kind, body) = next(iter(s.items()))
        w = f"{where}.{kind}"
        if kind not in PRIMITIVES:
            self.err(where, f"unknown selection primitive {kind!r} (known: {', '.join(PRIMITIVES)})")
        elif kind == "box":
            d = self.keys(body, w, {*AXES, *(f"abs_{a}" for a in AXES)})
            if not d and isinstance(body, dict):
                self.err(w, "an empty box selects everything; bound at least one axis")
            for k, v in d.items():
                if k in AXES or k.startswith("abs_"):
                    self.interval(v, f"{w}.{k}")
        elif kind == "ball":
            d = self.keys(body, w, {"c", "r"}, {"c", "r"})
            if "c" in d:
                self.vec(d["c"], f"{w}.c")
            if "r" in d:
                self.num(d["r"], f"{w}.r", 0.0, lo_open=True)
        elif kind in ("cyl", "annulus"):
            radii = {"r"} if kind == "cyl" else {"r_in", "r_out"}
            d = self.keys(body, w, {"axis", "c", "range", *radii}, {"axis", "c", *radii})
            if "axis" in d:
                self.axis(d["axis"], f"{w}.axis")
            if "c" in d:
                self.vec(d["c"], f"{w}.c", 2)
            if "range" in d:
                self.interval(d["range"], f"{w}.range")
            for r in sorted(radii & set(d)):
                self.num(d[r], f"{w}.{r}", 0.0)
            if (
                kind == "annulus"
                and {"r_in", "r_out"} <= set(d)
                and self.ok_num(d["r_in"])
                and self.ok_num(d["r_out"])
                and d["r_in"] >= d["r_out"]
            ):
                self.err(w, "r_in must be below r_out")
        elif kind == "half":
            d = self.keys(body, w, {"p", "n"}, {"p", "n"})
            if "p" in d:
                self.vec(d["p"], f"{w}.p")
            if "n" in d and all(x == 0 for x in self.vec(d["n"], f"{w}.n")):
                self.err(f"{w}.n", "the normal is zero")
        elif not isinstance(body, list) or not body or (kind == "minus" and len(body) != 2):  # union, intersect, minus
            want = "a list of two selections" if kind == "minus" else "a non-empty list of selections"
            self.err(w, f"must be {want}")
        else:
            for i, sub in enumerate(body):
                self.selection(sub, f"{w}[{i}]")
        return s if isinstance(s, dict) else {}

    @staticmethod
    def ok_num(v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _read_json(path: str | Path, what: str) -> tuple[Any, bytes]:
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as e:
        raise TopoError(f"{p}: cannot read the {what}: {e}") from e
    try:
        return json.loads(raw), raw
    except json.JSONDecodeError as e:
        raise TopoSpecError([f"{p}: not JSON: {e}"]) from e


# ---------------------------------------------------------------------------- the spec
_TOP = {"schema", "units", "domain", "material", "simp", "supports", "passive", "cases", "weights", "regions", "notes"}


def parse_spec(doc: Any, path: str = "spec.json", raw: bytes = b"") -> tuple[TopoSpec | None, list[str]]:
    """(spec, []) for a valid document, else (None, error lines). Paths in the spec resolve against its folder."""
    v = _V()
    d = v.keys(doc, "spec", _TOP, {"schema", "units", "domain", "supports", "cases"})
    if d.get("schema", SCHEMA) != SCHEMA:
        v.err("spec.schema", f"must be {SCHEMA!r}, got {d.get('schema')!r}")
    if "units" in d and d["units"] != UNITS:
        v.err("spec.units", f"must be {UNITS!r} (mm, N, MPa), got {d['units']!r}")
    base = Path(path).parent

    dom = v.keys(d.get("domain", {}), "spec.domain", {"stl", "box", "pitch", "subsamples"}, {"pitch"})
    pitch = v.num(dom.get("pitch", 1.0), "spec.domain.pitch", 0.0, lo_open=True)
    sub = dom.get("subsamples", 3)
    if not isinstance(sub, int) or isinstance(sub, bool) or not 1 <= sub <= 7:
        v.err("spec.domain.subsamples", f"must be an integer from 1 to 7, got {sub!r}")
        sub = 3
    stl = box = None
    if ("stl" in dom) == ("box" in dom):
        v.err("spec.domain", "give exactly one of 'stl' (a closed mesh) or 'box' (a solid block)")
    elif "stl" in dom:
        stl = str(base / v.text(dom["stl"], "spec.domain.stl"))
    else:
        b = v.keys(dom["box"], "spec.domain.box", {"min", "max"}, {"min", "max"})
        lo, hi = v.vec(b.get("min", [0, 0, 0]), "spec.domain.box.min"), v.vec(b.get("max", [1, 1, 1]), "spec.domain.box.max")
        if any(h <= lo_ for lo_, h in zip(lo, hi, strict=True)):
            v.err("spec.domain.box", "max must be above min on every axis")
        box = ((lo[0], lo[1], lo[2]), (hi[0], hi[1], hi[2]))
    domain = Domain(stl, box, pitch, sub)

    m = v.keys(d.get("material", {}), "spec.material", {"E", "nu", "density_g_cm3", "fill"})
    material = Material(
        v.num(m.get("E", DEFAULT_E_MPA), "spec.material.E", 0.0, lo_open=True),
        v.num(m.get("nu", DEFAULT_NU), "spec.material.nu", 0.0, 0.5, hi_open=True),
        v.num(m.get("density_g_cm3", DEFAULT_DENSITY_G_CM3), "spec.material.density_g_cm3", 0.0),
        v.num(m.get("fill", 1.0), "spec.material.fill", 0.0, 1.0),
    )

    s = v.keys(
        d.get("simp", {}),
        "spec.simp",
        {"penal", "emin_rel", "rmin", "rmin_cells", "move", "eta", "volfrac", "maxit", "tol", "budget_min", "min_member_mm"},
    )
    vf = s.get("volfrac", [0.5])
    vf = [vf] if isinstance(vf, (int, float)) and not isinstance(vf, bool) else vf
    if not isinstance(vf, list) or not vf:
        v.err("spec.simp.volfrac", f"must be a number or a non-empty list of numbers, got {vf!r}")
        vf = [0.5]
    volfrac = tuple(v.num(x, f"spec.simp.volfrac[{i}]", 0.0, 1.0, lo_open=True, hi_open=True) for i, x in enumerate(vf))
    maxit = s.get("maxit", 80)
    if not isinstance(maxit, int) or isinstance(maxit, bool) or maxit < 1:
        v.err("spec.simp.maxit", f"must be a positive integer, got {maxit!r}")
        maxit = 80
    simp = Simp(
        penal=v.num(s.get("penal", 3.0), "spec.simp.penal", 1.0),
        emin_rel=v.num(s.get("emin_rel", 1e-9), "spec.simp.emin_rel", 0.0, 1e-2, lo_open=True),
        rmin=None if s.get("rmin") is None else v.num(s["rmin"], "spec.simp.rmin", 0.0, lo_open=True),
        rmin_cells=v.num(s.get("rmin_cells", 2.5), "spec.simp.rmin_cells", 0.0, lo_open=True),
        move=v.num(s.get("move", 0.2), "spec.simp.move", 0.0, 1.0, lo_open=True),
        eta=v.num(s.get("eta", 0.5), "spec.simp.eta", 0.0, 1.0, lo_open=True),
        volfrac=volfrac,
        maxit=maxit,
        tol=v.num(s.get("tol", 0.01), "spec.simp.tol", 0.0, lo_open=True),
        budget_min=v.num(s.get("budget_min", 40.0), "spec.simp.budget_min", 0.0, lo_open=True),
        min_member_mm=None if s.get("min_member_mm") is None else v.num(s["min_member_mm"], "spec.simp.min_member_mm", 0.0),
    )

    supports = []
    sup = d.get("supports", [])
    if not isinstance(sup, list) or not sup:
        v.err("spec.supports", "must be a non-empty list: a model with no support has no unique displacement")
        sup = []
    for i, x in enumerate(sup):
        w = f"spec.supports[{i}]"
        e = v.keys(x, w, {"name", "nodes", "dofs"}, {"name", "nodes"})
        dofs = e.get("dofs", "xyz")
        if not isinstance(dofs, str) or not dofs or set(dofs) - set(AXES) or len(set(dofs)) != len(dofs):
            v.err(f"{w}.dofs", f"must be a string of distinct letters from 'xyz', got {dofs!r}")
            dofs = "xyz"
        supports.append(Support(v.text(e.get("name", "?"), f"{w}.name"), v.selection(e.get("nodes", {}), f"{w}.nodes"), dofs))

    passive = _regions(v, d.get("passive", []), "spec.passive")
    regions = _regions(v, d.get("regions", []), "spec.regions")

    weights = d.get("weights", "normalised")
    if weights not in ("normalised", "raw"):
        v.err("spec.weights", f"must be 'normalised' or 'raw', got {weights!r}")
        weights = "normalised"
    cases = []
    cs = d.get("cases", [])
    if not isinstance(cs, list) or not cs:
        v.err("spec.cases", "must be a non-empty list of load cases")
        cs = []
    for i, x in enumerate(cs):
        cases.append(_case(v, x, f"spec.cases[{i}]", weights))
    names = [c.name for c in cases]
    for n in sorted({n for n in names if names.count(n) > 1}):
        v.err("spec.cases", f"case name {n!r} is used twice")

    if v.errors:
        return None, v.errors
    return TopoSpec(
        path=str(path),
        sha256=_sha256(raw) if raw else "",
        domain=domain,
        material=material,
        simp=simp,
        supports=tuple(supports),
        passive=tuple(passive),
        cases=tuple(cases),
        weights=weights,
        regions=tuple(regions),
    ), []


def _regions(v: _V, xs: Any, where: str) -> list[Region]:
    if not isinstance(xs, list):
        v.err(where, "must be a list")
        return []
    out = []
    for i, x in enumerate(xs):
        e = v.keys(x, f"{where}[{i}]", {"name", "cells"}, {"name", "cells"})
        out.append(Region(v.text(e.get("name", "?"), f"{where}[{i}].name"), v.selection(e.get("cells", {}), f"{where}[{i}].cells")))
    return out


def _case(v: _V, x: Any, w: str, weights: str) -> Case:
    e = v.keys(x, w, {"name", "weight", "loads", "body_force"}, {"name"})
    name = v.text(e.get("name", "?"), f"{w}.name")
    weight = e.get("weight")
    if weight is not None:
        weight = v.num(weight, f"{w}.weight", 0.0, lo_open=True)
    elif weights == "raw":
        weight = 1.0
    loads = []
    ls = e.get("loads", [])
    if not isinstance(ls, list):
        v.err(f"{w}.loads", "must be a list")
        ls = []
    for j, ld in enumerate(ls):
        wl = f"{w}.loads[{j}]"
        g = v.keys(ld, wl, {"cells", "nodes", "grow_if_empty", "force", "moment", "about"})
        if ("cells" in g) == ("nodes" in g):
            v.err(wl, "give exactly one of 'cells' or 'nodes'")
            on, sel = "cells", {}
        else:
            on = "cells" if "cells" in g else "nodes"
            sel = v.selection(g[on], f"{wl}.{on}")
        force = v.vec(g.get("force", [0, 0, 0]), f"{wl}.force")
        moment = v.vec(g.get("moment", [0, 0, 0]), f"{wl}.moment")
        about = g.get("about", "centroid")
        about_v = None if about == "centroid" else v.vec(about, f"{wl}.about")
        if all(c == 0 for c in force) and all(c == 0 for c in moment):
            v.err(wl, "has neither a force nor a moment")
        grow = v.num(g.get("grow_if_empty", 0.0), f"{wl}.grow_if_empty", 0.0)
        loads.append(Load(sel, on, force, moment, about_v, grow))  # type: ignore[arg-type]
    body = None
    if "body_force" in e:
        b = v.keys(e["body_force"], f"{w}.body_force", {"accel_g", "scale_with_density"}, {"accel_g"})
        acc = v.vec(b.get("accel_g", [0, 0, -1]), f"{w}.body_force.accel_g")
        swd = b.get("scale_with_density", True)
        if not isinstance(swd, bool):
            v.err(f"{w}.body_force.scale_with_density", "must be true or false")
            swd = True
        body = BodyForce(acc, swd)  # type: ignore[arg-type]
    if not loads and body is None:
        v.err(w, "has no load and no body force")
    return Case(name, weight, tuple(loads), body)


def load_spec(path: str | Path) -> TopoSpec:
    """Read and validate a spec file. TopoSpecError (exit 1) lists every error; TopoError (exit 3) if it does not read."""
    doc, raw = _read_json(path, "topology spec")
    spec, errors = parse_spec(doc, str(path), raw)
    if spec is None:
        raise TopoSpecError(errors)
    return spec


# ---------------------------------------------------------------------------- faces and rules
def parse_faces(doc: Any, where: str = "faces") -> tuple[Faces | None, list[str]]:
    v = _V()
    d = v.keys(doc, where, {"schema", "build_axis", "faces", "keepouts", "notes"}, {"schema", "build_axis", "faces"})
    if d.get("schema", FACES_SCHEMA) != FACES_SCHEMA:
        v.err(f"{where}.schema", f"must be {FACES_SCHEMA!r}")
    build = v.axis(d.get("build_axis", "z"), f"{where}.build_axis")
    keep = tuple(_ball(v, b, f"{where}.keepouts[{i}]") for i, b in enumerate(d.get("keepouts", []) or []))
    faces = []
    fs = d.get("faces", [])
    if not isinstance(fs, list) or not fs:
        v.err(f"{where}.faces", "must be a non-empty list")
        fs = []
    for i, f in enumerate(fs):
        w = f"{where}.faces[{i}]"
        e = v.keys(f, w, {"name", "axis", "slab", "u", "v", "open_edges", "keepouts"}, {"name", "axis", "slab", "u", "v"})
        oe = e.get("open_edges", [])
        if not isinstance(oe, list) or any(x not in EDGES for x in oe):
            v.err(f"{w}.open_edges", f"must be a list of {', '.join(EDGES)}")
            oe = []
        faces.append(
            Face(
                v.text(e.get("name", "?"), f"{w}.name"),
                v.axis(e.get("axis", "z"), f"{w}.axis"),
                v.interval(e.get("slab", [0, 1]), f"{w}.slab"),
                v.interval(e.get("u", [0, 1]), f"{w}.u"),
                v.interval(e.get("v", [0, 1]), f"{w}.v"),
                tuple(oe),
                tuple(_ball(v, b, f"{w}.keepouts[{j}]") for j, b in enumerate(e.get("keepouts", []) or [])),
            )
        )
    if v.errors:
        return None, v.errors
    return Faces(build, tuple(faces), keep), []


def _ball(v: _V, b: Any, w: str) -> dict:
    e = v.keys(b, w, {"name", "c", "r"}, {"c", "r"})
    return {
        "name": e.get("name", w.rsplit(".", 1)[-1]),
        "c": list(v.vec(e.get("c", [0, 0, 0]), f"{w}.c")),
        "r": v.num(e.get("r", 1), f"{w}.r", 0.0),
    }


def load_faces(path: str | Path) -> Faces:
    doc, _ = _read_json(path, "faces file")
    faces, errors = parse_faces(doc, str(path))
    if faces is None:
        raise TopoSpecError(errors)
    return faces


def rules_from(name_or_path: str) -> TraceRules:
    """A preset by name, or a rules file (`shambu.topo.rules/1`) whose `base` preset its keys override."""
    if name_or_path in PRESETS:
        return PRESETS[name_or_path]
    doc, _ = _read_json(name_or_path, "rules file")
    v = _V()
    fields = set(TraceRules.__dataclass_fields__) - {"name"}
    d = v.keys(doc, "rules", {"schema", "base", *fields})
    if d.get("schema", RULES_SCHEMA) != RULES_SCHEMA:
        v.err("rules.schema", f"must be {RULES_SCHEMA!r}")
    base = d.get("base", "conservative")
    if base not in PRESETS:
        v.err("rules.base", f"must be one of {', '.join(PRESETS)}")
        base = "conservative"
    over: dict[str, Any] = {}
    for k in sorted(fields & set(d)):
        if k == "other_grow":
            if d[k] not in ("rib", "rim"):
                v.err("rules.other_grow", "must be 'rib' or 'rim'")
            over[k] = d[k]
        elif k in ("max_vertices", "rib_passes"):
            if not isinstance(d[k], int) or isinstance(d[k], bool) or d[k] < 1:
                v.err(f"rules.{k}", "must be a positive integer")
            over[k] = d[k]
        elif d[k] is None and k in ("clip_r_mm", "keep_volfrac"):
            over[k] = None
        else:
            over[k] = v.num(d[k], f"rules.{k}", 0.0)
    if v.errors:
        raise TopoSpecError(v.errors)
    return replace(PRESETS[base], name=f"{base}+{Path(name_or_path).name}", **over)


def spec_summary(spec: TopoSpec) -> dict:
    """What `topo check` reports for a valid spec."""
    return {
        "domain": {"stl": spec.domain.stl, "box": spec.domain.box, "pitch": spec.domain.pitch, "subsamples": spec.domain.subsamples},
        "cases": [{"name": c.name, "loads": len(c.loads), "body_force": c.body_force is not None, "weight": c.weight} for c in spec.cases],
        "supports": [s.name for s in spec.supports],
        "passive": [r.name for r in spec.passive],
        "regions": [r.name for r in spec.regions],
        "volfrac": list(spec.simp.volfrac),
        "weights": spec.weights,
    }
