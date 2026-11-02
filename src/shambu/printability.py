"""`shambu printability`: a pre-flight on the triangle mesh of an STL or a 3MF object in the pose it will print, in
pure Python. It reports findings by area (mesh, support, bridges, walls, layers, stability, adhesion, fit), never one
score, and every run lists what it did not check (`NOT_CHECKED`).

What it handles (reference 08 section 12 has the algorithms and the thresholds):

- mesh: open and non-manifold edges, flipped facets, bodies, a mesh in metres;
- support: overhang regions beyond Studio's support threshold, with area, shadow, steepest angle and location, and
  overhangs that start at the bed (a fillet to chamfer in CAD);
- bridges: flat ceilings with anchors on two sides or all round, and their span (twice the largest distance from the
  ceiling to its nearest anchor), against the file's `max_bridge_length`;
- walls and layers: sampled wall (XY) and slab (Z) thickness from ray casts along the inward normal, against the line
  widths and the layer height;
- stability: bed contact, and the centre of mass against the contact hull (tip-over);
- adhesion: when a brim helps (slenderness, long and narrow contact) and when it does not;
- fit: the posed size against the bed and the reach of the extruder that prints it.

Limits (honest, also in reference 08 section 12):

- An STL is chords of the CAD surface. A facet angle on a curved face can be off by up to half the angular step of the
  tessellation (a 64-segment bore: 2.8 degrees); regions on curves split or merge at that resolution.
- The bridge anchor test reads one facet across each boundary edge. A ceiling anchored on a slope or a fillet can read
  as unanchored, and an anchor on two adjacent sides reads as one chain: both stay overhangs (the conservative side).
- Wall and slab values are sampled, not a medial axis. The 30-degree pair filter drops chords across sharp edges but
  also skips walls whose faces are not near-parallel (tapers over 30 degrees). The JSON says how many facets were
  sampled.
- Tip-over uses the finished part, a uniform density and a free-standing body. It ignores adhesion, brims, supports
  and infill (infill puts the real centre of mass nearer the walls).
- The thresholds are Studio's defaults for one process (`Limits.default`, the X2D 0.20 Standard values); a 3MF's own
  process and per-object settings replace them, an STL takes `--process` and the flags.

What it does not do: no slicing (support grams and time come from `shambu build --slice` and `shambu sweep`), no mesh
repair, no file reading (facade.py loads the STL or the 3MF), no printing (cli.py prints `Report.lines()`), and no
overall score or "ready" verdict: a gate counts findings by code and severity.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from . import meshgeom as G
from .findings import FAIL, HEURISTIC, INFO, MEASURED, WARN, Finding, Fix, Location, tally
from .mesh import DEFAULT_THRESHOLD, MIN_CONTACT_MM2, Facet, Vec, bbox, looks_like_metres

SCHEMA = 1
AREAS = ("mesh", "support", "bridges", "walls", "layers", "stability", "adhesion", "fit")
ATTENTION, NO_FINDING, NOT_CHECKED_STATUS = "attention", "no-finding", "not-checked"

BED_TOL = 0.05  # mm: a facet whose three corners sit this close to the bed lies on it (mesh.metrics)
FLAT_DOWN = 0.999  # -n.z above this is a face flat on the bed (mesh.metrics)
BED_FILLET_MM = 1.0  # an overhang region within this of the bed meets it from above (reference 08 rule 13); above it, support
SMALL_SHADOW_MM2 = 1.0  # regions with less shadow are counted, not listed: a tessellated curve gives many slivers
BRIDGE_FLAT_DEG = 5.0  # heuristic: a ceiling within this of horizontal is a bridge candidate
ANCHOR_DROP_MM = 0.05  # the facet across a boundary edge goes down at least this far: a wall under the edge
ANCHOR_FACING = -0.5  # and faces back under the ceiling (dot with the outward edge direction below this)
BRIDGE_RECIPE_MM = 10.0  # `bridge-short-spans` frees spans to 10 mm (reference 06 section 4)
BRIDGE_TESTED_MM = 40.0  # 10 to 40 mm bridged clean in PLA on one measured plate (reference 08 rule 5)
PAIR_DEG = 30.0  # a wall pair: the hit facet faces the source within 30 degrees of opposite
WALL_NZ = 0.7  # |n.z| below this samples a wall (XY), at or above a slab (Z)
OFF_GRID_MM = 0.02  # a slab within this of a whole number of layers is on the grid
OFF_GRID_LAYERS = 10  # slabs thicker than this many layers lose little to rounding: not reported
ALWAYS_SAMPLE = 200  # the largest facets are always ray sources
DEFAULT_SAMPLES = 4000
TIP_DEG = 10.0  # heuristic: a static tip angle under this is a low margin
SLENDER = 3.0  # heuristic: the knee of `mesh.Metrics.score`; no published height-to-base ratio exists
LONG_CONTACT_MM, NARROW_CONTACT_MM = 100.0, 15.0  # heuristic: long, narrow contact (216 mm rails, 112 mm jaws, reference 06 s3)
BIG_CONTACT_MM2 = 400.0  # heuristic: contact this large and slenderness at most 1 needs no brim
EXPLICIT_BRIMS = ("outer_only", "inner_only", "outer_and_inner", "brim_ears", "painted")


class InputError(ValueError):
    """A request the file cannot answer: an unknown object name, a file with no mesh, a file type it does not read.
    The CLI reports it as a usage error (exit 2)."""


NOT_CHECKED: tuple[dict[str, str], ...] = (
    {
        "what": "support volume, grams, time and the real support footprint",
        "why": "needs the slicer",
        "use": "shambu build --slice; shambu sweep --orientations",
    },
    {"what": "load direction and strength", "why": "needs the user's intent", "use": "reference 08 rule 1; ask the user"},
    {"what": "show faces, surface finish and seam", "why": "intent", "use": "reference 08 rules 6 and 11"},
    {"what": "horizontal hole droop and roundness", "why": "not detected", "use": "reference 08 rule 7; print a coupon"},
    {"what": "mid-print stability (the part printed so far)", "why": "not computed", "use": "a brim when slenderness is high"},
    {"what": "warping, shrink, bed temperature and material", "why": "not geometry", "use": "reference 06 sections 3 and 7"},
    {"what": "painted supports, modifiers, enforcers, blockers and negative parts", "why": "not read", "use": "Bambu Studio"},
    {
        "what": "placement on the plate, prime tower and nozzle reach by position",
        "why": "`check` owns placement",
        "use": "shambu check; shambu build",
    },
    {"what": "bridge quality by material and cooling", "why": "not geometry", "use": "reference 08 rule 5 (PLA, one measured plate)"},
    {"what": "mesh accuracy against the CAD surface", "why": "an STL is a chord approximation", "use": "reference 08 section 12"},
)


# ---------------------------------------------------------------------------- limits
@dataclass(frozen=True)
class Limit:
    value: Any
    source: str  # "flag", "file", "file object", "preset <name>" or "default"

    def to_json(self) -> dict:
        return {"value": self.value, "source": self.source}


# Built-in defaults: the Bambu Lab X2D `0.20mm Standard @BBL X2D` process (tests/test_printability.py compares them
# with the preset library when it is installed). `support_threshold_angle` 30 counts from horizontal: 60 from vertical.
@dataclass(frozen=True)
class Limits:
    overhang_deg: Limit = Limit(DEFAULT_THRESHOLD, "default")  # from vertical
    max_bridge_mm: Limit = Limit(0.0, "default")
    layer_height_mm: Limit = Limit(0.2, "default")
    initial_layer_mm: Limit = Limit(0.2, "default")
    outer_line_mm: Limit = Limit(0.42, "default")
    inner_line_mm: Limit = Limit(0.45, "default")
    wall_generator: Limit = Limit("classic", "default")
    brim_type: Limit = Limit("auto_brim", "default")
    brim_width_mm: Limit = Limit(5.0, "default")
    machine: dict | None = None  # presets.machine_facts of the printer
    machine_source: str = "none"
    machine_reason: str = "no printer facts: pass --printer"

    @classmethod
    def default(cls) -> Limits:
        return cls()

    def from_settings(self, settings: Mapping[str, Any], source: str) -> Limits:
        """These limits with every key `settings` sets (a process preset, a 3MF's project or object settings).
        A value that does not parse as a number keeps the current limit."""
        kw: dict[str, Any] = {}

        def first(key: str) -> Any:
            v = settings.get(key)
            if isinstance(v, list):
                v = v[0] if v else None
            return v

        def num(key: str) -> float | None:
            v = first(key)
            if v is None or str(v).strip().endswith("%"):
                return None  # a percentage of the nozzle: keep the absolute default
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None  # a malformed optional value keeps the current limit (rubric 13's documented fallback)
            return f if math.isfinite(f) else None

        thr = num("support_threshold_angle")
        if thr is not None and thr > 0:  # counts from horizontal; 0 leaves the current limit
            kw["overhang_deg"] = Limit(90.0 - thr, source)
        for key, attr, positive in (
            ("max_bridge_length", "max_bridge_mm", False),
            ("layer_height", "layer_height_mm", True),
            ("initial_layer_print_height", "initial_layer_mm", True),
            ("outer_wall_line_width", "outer_line_mm", True),
            ("inner_wall_line_width", "inner_line_mm", True),
            ("brim_width", "brim_width_mm", False),
        ):
            v = num(key)
            if v is not None and (v > 0 or (not positive and v >= 0)):
                kw[attr] = Limit(v, source)
        for key, attr in (("wall_generator", "wall_generator"), ("brim_type", "brim_type")):
            s = first(key)
            if isinstance(s, str) and s:
                kw[attr] = Limit(s, source)
        return replace(self, **kw)

    def with_flags(self, **flags: float | None) -> Limits:
        """Each flag that is not None replaces its limit, with source `flag`."""
        kw: dict[str, Any] = {k: Limit(v, "flag") for k, v in flags.items() if v is not None}
        return replace(self, **kw)

    def with_machine(self, facts: dict | None, source: str, reason: str = "") -> Limits:
        return replace(self, machine=facts, machine_source=source if facts else "none", machine_reason=reason if not facts else "")

    def v(self, name: str) -> Any:
        return getattr(self, name).value

    def to_json(self) -> dict:
        out = {
            name: getattr(self, attr).to_json()
            for name, attr in (
                ("overhang_deg_from_vertical", "overhang_deg"),
                ("max_bridge_mm", "max_bridge_mm"),
                ("layer_height_mm", "layer_height_mm"),
                ("initial_layer_mm", "initial_layer_mm"),
                ("outer_line_mm", "outer_line_mm"),
                ("inner_line_mm", "inner_line_mm"),
                ("wall_generator", "wall_generator"),
                ("brim_type", "brim_type"),
                ("brim_width_mm", "brim_width_mm"),
            )
        }
        m: dict[str, Any] = {"value": None, "source": "none", "reason": self.machine_reason}
        if self.machine:
            m = {"value": _machine_json(self.machine), "source": self.machine_source}
        out["machine"] = m
        return out

    def text(self) -> list[str]:
        machine = f"{self.machine.get('name') or 'printer'} ({self.machine_source})" if self.machine else self.machine_reason
        return [
            f"limits: overhang {self.v('overhang_deg'):g} deg from vertical ({self.overhang_deg.source}), max bridge "
            f"{self.v('max_bridge_mm'):g} mm ({self.max_bridge_mm.source}), layer {self.v('layer_height_mm'):g} / first "
            f"{self.v('initial_layer_mm'):g} mm, lines {self.v('outer_line_mm'):g} outer / {self.v('inner_line_mm'):g} inner, "
            f"{self.v('wall_generator')} walls, brim {self.v('brim_type')}; machine: {machine}"
        ]


def _machine_json(facts: dict) -> dict:
    return {
        "name": facts.get("name"),
        "bed": {k: list(v) for k, v in facts["bed"].items()},
        "z_max": facts.get("z_max"),
        "extruders": [{"index": e["index"], "x": list(e["x"]), "y": list(e["y"]), "z_max": e["z_max"]} for e in facts.get("extruders", [])],
    }


# ---------------------------------------------------------------------------- subjects and geometry
@dataclass
class Subject:
    """One thing to analyse: an STL in its pose, or one 3MF object in its placed pose."""

    name: str
    facets: list[Facet]
    frame: str  # "posed" or "plate"
    extruder: int = 1
    pose: dict | None = None
    limits: Limits | None = None  # the object's own limits (3MF); None: the report's
    skipped: tuple[str, ...] = ()  # subtypes of the volumes that are not geometry (modifiers, enforcers, ...)
    negative: bool = False  # the object has a negative part (not subtracted here)
    command: str | None = None  # the shambu command a fix can name, e.g. "shambu orient bracket.stl"


class Geo:
    """Per-subject geometry shared by the checks: welded topology, normals, areas, the bed plane, contact facets and,
    built on first use, the ray grid and the bridge split."""

    def __init__(self, subject: Subject):
        self.facets = subject.facets
        self.frame = subject.frame
        self.welded = G.weld(self.facets)
        self.normals: list[Vec] = []
        self.areas: list[float] = []
        for a, b, c in self.facets:
            n = G._cross(G._sub(b, a), G._sub(c, a))
            ln = math.sqrt(G._dot(n, n))
            self.normals.append((n[0] / ln, n[1] / ln, n[2] / ln) if ln > 0 else (0.0, 0.0, 0.0))
            self.areas.append(ln / 2)
        self.lo, self.hi = bbox(self.facets)
        self.bed = self.lo[2]
        self.open_edges = self.welded.open_edges()
        self.nonmanifold = self.welded.nonmanifold_edges()
        self.flipped = self.welded.flipped_edges()
        self.closed = not self.open_edges and not self.nonmanifold
        self.contact = [f for f in range(len(self.facets)) if self.on_bed(f) and self.normals[f][2] < -FLAT_DOWN]
        self._grid: G.RayGrid | None = None
        self._bridges: tuple[list[Bridge], set[int]] | None = None

    def on_bed(self, f: int) -> bool:
        return max(v[2] for v in self.facets[f]) <= self.bed + BED_TOL

    def centroid(self, f: int) -> Vec:
        a, b, c = self.facets[f]
        return ((a[0] + b[0] + c[0]) / 3, (a[1] + b[1] + c[1]) / 3, (a[2] + b[2] + c[2]) / 3)

    def grid(self) -> G.RayGrid:
        if self._grid is None:
            self._grid = G.RayGrid(self.facets)
        return self._grid

    def region_box(self, fids: Sequence[int]) -> tuple[Vec, Vec]:
        return bbox([self.facets[f] for f in fids])

    def region_centroid(self, fids: Sequence[int]) -> Vec:
        total = sum(self.areas[f] for f in fids)
        if total <= 0:
            lo, hi = self.region_box(fids)
            return ((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2)
        cs = [(self.centroid(f), self.areas[f]) for f in fids]
        return tuple(sum(c[i] * w for c, w in cs) / total for i in range(3))  # type: ignore[return-value]

    def location(self, fids: Sequence[int], point: Vec | None = None) -> Location:
        lo, hi = self.region_box(fids)
        return Location(self.frame, point or self.region_centroid(fids), (lo[2], hi[2]), (lo, hi))

    def bridges(self) -> tuple[list[Bridge], set[int]]:
        """The flat ceilings that are bridges, and the facet ids they hold (left out of the overhang regions)."""
        if self._bridges is None:
            self._bridges = _find_bridges(self)
        return self._bridges


@dataclass(frozen=True)
class Bridge:
    fids: tuple[int, ...]
    span_mm: float
    chains: int
    closed: bool  # anchored all round (one closed chain)


def _find_bridges(geo: Geo) -> tuple[list[Bridge], set[int]]:
    w = geo.welded
    cos_flat = math.cos(math.radians(BRIDGE_FLAT_DEG))
    cands = [f for f in range(len(geo.facets)) if geo.normals[f][2] <= -cos_flat and min(v[2] for v in geo.facets[f]) > geo.bed + BED_TOL]
    out: list[Bridge] = []
    held: set[int] = set()
    em = w.edge_map()
    for reg in G.regions(cands, w):
        rs = set(reg)
        anchored: list[G.Edge] = []
        for f in reg:
            c = geo.centroid(f)
            for e in w.edges_of(f):
                users = em.get(e, [])
                others = [g for g in users if g not in rs]
                if len(users) != 2 or not others:
                    continue
                g = others[0]
                far = next(v for v in w.tris[g] if v not in e)
                a, b = w.verts[e[0]], w.verts[e[1]]
                ez = (a[2] + b[2]) / 2
                if w.verts[far][2] >= ez - ANCHOR_DROP_MM:
                    continue  # the face across goes up or stays level: no wall under this edge
                ex, ey = b[0] - a[0], b[1] - a[1]
                ln = math.hypot(ex, ey)
                if ln == 0:
                    continue
                ux, uy = -ey / ln, ex / ln  # perpendicular to the edge in the ceiling's plane, pointing out of the ceiling
                if ux * ((a[0] + b[0]) / 2 - c[0]) + uy * ((a[1] + b[1]) / 2 - c[1]) < 0:
                    ux, uy = -ux, -uy
                ng = geo.normals[g]
                if ng[0] * ux + ng[1] * uy < ANCHOR_FACING:
                    anchored.append(e)
        chains = G.edge_chains(anchored)
        closed = any(c for _, c in chains)
        if len(chains) < 2 and not closed:
            continue  # anchored on one side (or nowhere): a cantilever, it stays an overhang
        segs = [((w.verts[a][0], w.verts[a][1]), (w.verts[b][0], w.verts[b][1])) for a, b in anchored]
        tris = [tuple((v[0], v[1]) for v in geo.facets[f]) for f in reg]
        span = 2 * G.farthest_in(tris, segs)  # type: ignore[arg-type]
        out.append(Bridge(tuple(reg), span, len(chains), closed))
        held |= rs
    return out, held


# ---------------------------------------------------------------------------- results
@dataclass
class AreaResult:
    status: str  # attention | no-finding | not-checked
    measures: dict = field(default_factory=dict)
    reason: str | None = None
    findings: list[Finding] = field(default_factory=list)

    def to_json(self) -> dict:
        d: dict = {"status": self.status}
        if self.reason:
            d["reason"] = self.reason
        d.update({k: (round(v, 3) if isinstance(v, float) else v) for k, v in self.measures.items()})
        return d


def _result(measures: dict, findings: list[Finding]) -> AreaResult:
    status = ATTENTION if any(f.severity in (FAIL, WARN) for f in findings) else NO_FINDING
    return AreaResult(status, measures, None, findings)


def _skip(reason: str) -> AreaResult:
    return AreaResult(NOT_CHECKED_STATUS, {}, reason, [])


class PrintCheck(Protocol):
    """One printability check: the areas it reports and how it runs on one subject."""

    areas: tuple[str, ...]

    def run(self, subject: Subject, geo: Geo, limits: Limits) -> dict[str, AreaResult]: ...


# ---------------------------------------------------------------------------- checks
class MeshSound:
    areas: tuple[str, ...] = ("mesh",)

    def run(self, subject: Subject, geo: Geo, limits: Limits) -> dict[str, AreaResult]:
        name = subject.name
        bodies = len(G.regions(range(len(geo.facets)), geo.welded))
        fs: list[Finding] = []
        holes = geo.open_edges + geo.nonmanifold
        if holes:
            pts = [geo.welded.verts[v] for e in holes for v in e]
            lo = (min(p[0] for p in pts), min(p[1] for p in pts), min(p[2] for p in pts))
            hi = (max(p[0] for p in pts), max(p[1] for p in pts), max(p[2] for p in pts))
            centre = ((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2)
            fs.append(
                Finding(
                    "mesh.open_edges",
                    FAIL,
                    "mesh",
                    f"{len(geo.open_edges)} open and {len(geo.nonmanifold)} non-manifold edges: the mesh has no closed inside",
                    MEASURED,
                    name,
                    Location(geo.frame, centre, (lo[2], hi[2]), (lo, hi)),
                    {"open_edges": len(geo.open_edges), "nonmanifold_edges": len(geo.nonmanifold)},
                    Fix("re-export a closed solid from CAD; shambu does not repair meshes"),
                )
            )
        if geo.flipped:
            fs.append(
                Finding(
                    "mesh.flipped",
                    WARN,
                    "mesh",
                    f"{geo.flipped} edges run the same way in two facets: some facets face inwards",
                    MEASURED,
                    name,
                    measure={"flipped_edges": geo.flipped},
                    fix=Fix("re-export from CAD; Studio usually repairs the winding on load"),
                )
            )
        if looks_like_metres(geo.facets):
            fs.append(
                Finding(
                    "mesh.metres",
                    FAIL,
                    "mesh",
                    "the whole mesh is under 1 mm: it is probably in metres",
                    MEASURED,
                    name,
                    measure={"size_mm": [round(geo.hi[i] - geo.lo[i], 4) for i in range(3)]},
                    fix=Fix('`shambu rotate FILE -o DIR --convert-unit`, or "orientation": "auto" on the spec part'),
                )
            )
        if bodies > 1:
            fs.append(
                Finding(
                    "mesh.bodies",
                    INFO,
                    "mesh",
                    f"{bodies} separate bodies",
                    MEASURED,
                    name,
                    measure={"bodies": bodies},
                    fix=Fix("`shambu split` when the bodies are separate parts"),
                )
            )
        measures = {
            "open_edges": len(geo.open_edges),
            "flipped_edges": geo.flipped,
            "bodies": bodies,
            "watertight": geo.closed and not geo.flipped,
        }
        return {"mesh": _result(measures, fs)}


class Overhangs:
    areas: tuple[str, ...] = ("support",)

    def run(self, subject: Subject, geo: Geo, limits: Limits) -> dict[str, AreaResult]:
        thr = float(limits.v("overhang_deg"))
        sin_thr = math.cos(math.radians(90.0 - thr))
        _, bridged = geo.bridges()
        steep: list[int] = []
        band = 0.0
        steepest = 0.0
        for f, n in enumerate(geo.normals):
            if n[2] >= 0 or geo.on_bed(f):
                continue
            angle = math.degrees(math.asin(min(1.0, -n[2])))
            if -n[2] > sin_thr + 1e-6:  # a facet at exactly the threshold is printable (mesh.metrics)
                if f not in bridged:
                    steep.append(f)
                    steepest = max(steepest, angle)
            elif angle > 45.0 + 1e-6:
                band += geo.areas[f]
        contact = set(geo.contact)
        em = geo.welded.edge_map()
        fs: list[Finding] = []
        small = 0
        total = shadow_total = 0.0
        regions = 0
        for reg in G.regions(steep, geo.welded):
            area = sum(geo.areas[f] for f in reg)
            shadow = sum(geo.areas[f] * -geo.normals[f][2] for f in reg)
            total += area
            shadow_total += shadow
            if shadow < SMALL_SHADOW_MM2:
                small += 1
                continue
            regions += 1
            top = max(math.degrees(math.asin(min(1.0, -geo.normals[f][2]))) for f in reg)
            loc = geo.location(reg)
            low = loc.z[0] if loc.z else geo.bed
            meets_bed = low <= geo.bed + BED_FILLET_MM and any(
                g in contact for f in reg for e in geo.welded.edges_of(f) for g in em.get(e, [])
            )
            measure = {"area_mm2": area, "shadow_mm2": shadow, "steepest_deg": top}
            if meets_bed:  # a curve or slope that meets the bed from above: a CAD fix; it needs support only if it rises
                fs.append(
                    Finding(
                        "overhang.bed_fillet",
                        INFO,
                        "support",
                        f"{area:.0f} mm2 curves or slopes down onto the bed beyond {thr:g} deg from vertical",
                        MEASURED,
                        subject.name,
                        loc,
                        measure,
                        Fix("in CAD, chamfer the bed-facing edge instead of rounding it (reference 08 rule 13)"),
                    )
                )
                if loc.z is None or loc.z[1] <= geo.bed + BED_FILLET_MM:
                    continue
            flat = top >= 90.0 - BRIDGE_FLAT_DEG
            text = "support this region with tree supports, or try another pose" + (
                "; a flat area straight above the plate takes `snug-normal-supports`" if flat else ""
            )
            fs.append(
                Finding(
                    "overhang.region",
                    WARN,
                    "support",
                    f"{area:.0f} mm2 faces down beyond {thr:g} deg from vertical, shadow {shadow:.0f} mm2, steepest {top:.0f} deg",
                    MEASURED,
                    subject.name,
                    loc,
                    measure,
                    Fix(text, {"recipes": ["tree-supports"]}, subject.command),
                )
            )
        measures = {
            "overhang_mm2_beyond_threshold": total,
            "overhang_mm2_45_to_threshold": band,
            "shadow_mm2": shadow_total,
            "regions": regions,
            "small_regions": small,
            "steepest_deg": steepest,
        }
        return {"support": _result(measures, fs)}


class Bridges:
    areas: tuple[str, ...] = ("bridges",)

    def run(self, subject: Subject, geo: Geo, limits: Limits) -> dict[str, AreaResult]:
        bridges, _ = geo.bridges()
        max_free = float(limits.v("max_bridge_mm"))
        fs: list[Finding] = []
        supported = 0
        for b in bridges:
            loc = geo.location(b.fids)
            measure = {"span_mm": b.span_mm, "max_bridge_length_mm": max_free, "anchors": "all round" if b.closed else f"{b.chains} sides"}
            span = b.span_mm
            if max_free > 0 and span <= max_free + 1e-6:
                fs.append(
                    Finding(
                        "bridge.free",
                        INFO,
                        "bridges",
                        f"span {span:.1f} mm, within max_bridge_length {max_free:g}: Studio bridges it without support",
                        MEASURED,
                        subject.name,
                        loc,
                        measure,
                    )
                )
                continue
            supported += 1
            if span <= BRIDGE_RECIPE_MM + 1e-6:
                fs.append(
                    Finding(
                        "bridge.studio_supports",
                        WARN,
                        "bridges",
                        f"span {span:.1f} mm, max_bridge_length {max_free:g}: Studio puts support under it",
                        MEASURED,
                        subject.name,
                        loc,
                        measure,
                        Fix("bridge it: the recipe frees spans to 10 mm", {"recipes": ["bridge-short-spans"]}),
                    )
                )
            elif span <= BRIDGE_TESTED_MM + 1e-6:
                fs.append(
                    Finding(
                        "bridge.long",
                        INFO,
                        "bridges",
                        f"span {span:.1f} mm: Studio supports it; 10 to 40 mm bridged clean in PLA on one measured plate",
                        HEURISTIC,
                        subject.name,
                        loc,
                        measure,
                        Fix("test the span on a coupon before you remove its support"),
                    )
                )
            else:
                fs.append(
                    Finding(
                        "bridge.studio_supports",
                        WARN,
                        "bridges",
                        f"span {span:.1f} mm, over 40 mm: keep the support Studio puts under it",
                        MEASURED,
                        subject.name,
                        loc,
                        measure,
                        Fix("keep the support, or change the pose so the ceiling is not flat"),
                    )
                )
        measures = {
            "bridges": len(bridges),
            "longest_span_mm": max((b.span_mm for b in bridges), default=0.0),
            "spans_supported_by_studio": supported,
        }
        return {"bridges": _result(measures, fs)}


class Thickness:
    areas: tuple[str, ...] = ("walls", "layers")

    def __init__(self, samples: int = DEFAULT_SAMPLES):
        self.samples = samples

    def run(self, subject: Subject, geo: Geo, limits: Limits) -> dict[str, AreaResult]:
        why = _no_inside(subject, geo)
        if why:
            return {"walls": _skip(why), "layers": _skip(why)}
        sources = self._sources(geo)
        grid = geo.grid()
        w = geo.welded
        cos_pair = math.cos(math.radians(PAIR_DEG))
        walls: dict[int, tuple[float, int]] = {}
        slabs: dict[int, tuple[float, int]] = {}
        for f in sources:
            n = geo.normals[f]
            skip = {g for v in w.tris[f] for g in w.facets_at(v)}
            hit = grid.first_hit(geo.centroid(f), (-n[0], -n[1], -n[2]), skip)
            if hit is None:
                continue
            t, g = hit
            if G._dot(n, geo.normals[g]) >= -cos_pair:
                continue  # not a wall pair: a chord across a sharp edge or a taper
            (walls if abs(n[2]) < WALL_NZ else slabs)[f] = (t, g)
        outer, inner = float(limits.v("outer_line_mm")), float(limits.v("inner_line_mm"))
        lh = float(limits.v("layer_height_mm"))
        wall_fs = self._group(
            subject,
            geo,
            walls,
            [
                ("wall.below_line", WARN, lambda t: t < outer - 1e-9, "thinner than one {o:g} mm line: the wall may not print"),
                ("wall.one_loop", INFO, lambda t: outer - 1e-9 <= t < outer + inner - 1e-9, "prints as one loop at {o:g} + {i:g} mm lines"),
            ],
            {"o": outer, "i": inner},
        )
        slab_fs = self._group(
            subject,
            geo,
            slabs,
            [
                ("slab.below_layer", WARN, lambda t: t < lh - 1e-9, "thinner than one {lh:g} mm layer"),
                ("slab.off_grid", INFO, lambda t: lh - 1e-9 <= t < OFF_GRID_LAYERS * lh and _off_grid(t, lh), "{grid}"),
            ],
            {"lh": lh},
        )
        wall_m = {
            "thinnest_wall_mm_sampled": min((t for t, _ in walls.values()), default=None),
            "samples": len(sources),
            "facets": len(geo.facets),
            "wall_generator": limits.v("wall_generator"),
        }
        slab_m = {"thinnest_slab_mm_sampled": min((t for t, _ in slabs.values()), default=None), "layer_height_mm": lh}
        return {"walls": _result(wall_m, wall_fs), "layers": _result(slab_m, slab_fs)}

    def _sources(self, geo: Geo) -> list[int]:
        """Deterministic sample: the largest facets first (ties by position), the ALWAYS_SAMPLE largest always, then
        every k-th so that at most `samples` rays are cast."""

        def key(f: int) -> tuple:
            c = geo.centroid(f)
            return (-round(geo.areas[f], 9), round(c[0], 6), round(c[1], 6), round(c[2], 6))

        order = sorted((f for f in range(len(geo.facets)) if geo.areas[f] > 0), key=key)
        if len(order) <= self.samples:
            return order
        head, rest = order[:ALWAYS_SAMPLE], order[ALWAYS_SAMPLE:]
        k = math.ceil(len(rest) / max(1, self.samples - ALWAYS_SAMPLE))
        return head + rest[::k]

    @staticmethod
    def _group(
        subject: Subject,
        geo: Geo,
        hits: dict[int, tuple[float, int]],
        classes: list[tuple[str, str, Callable[[float], bool], str]],
        fmt: dict,
    ) -> list[Finding]:
        out: list[Finding] = []
        area = "walls" if classes[0][0].startswith("wall") else "layers"
        for code, sev, test, text in classes:
            thin = {f: th for f, th in hits.items() if test(th[0])}
            if not thin:
                continue
            nodes = set(thin) | {g for _, g in thin.values()}
            links = [(f, g) for f, (_, g) in thin.items()]
            for reg in G.regions(nodes, geo.welded, links):
                own = [f for f in reg if f in thin]
                if not own:
                    continue
                f_min = min(own, key=lambda f: (thin[f][0], f))
                t = thin[f_min][0]
                msg = text.format(grid=_grid_text(t, fmt.get("lh", 0.2)), **fmt)
                if code.startswith("wall"):
                    fix = (
                        Fix("in CAD, thicken the wall; or set Arachne walls or `detect_thin_wall` on the object")
                        if code == "wall.below_line"
                        else Fix("nothing to do unless the wall must be two loops; `wide-line-two-loop-walls` needs 0.84 mm or more")
                    )
                else:
                    fix = (
                        Fix("in CAD, thicken it; or print it alone with `shim-0.1mm` (0.1 mm layers), or `fine-0.16`")
                        if code == "slab.below_layer"
                        else Fix("adjust the CAD thickness, or a `height_ranges` entry with another layer height")
                    )
                out.append(
                    Finding(
                        code,
                        sev,
                        area,
                        f"{t:.2f} mm sampled: {msg}",
                        MEASURED,
                        subject.name,
                        geo.location(reg, geo.centroid(f_min)),
                        {"thickness_mm": t, "samples": len(own)},
                        fix,
                    )
                )
        return out


def _off_grid(t: float, lh: float) -> bool:
    return abs(t - round(t / lh) * lh) > OFF_GRID_MM


def _grid_text(t: float, lh: float) -> str:
    n = math.floor(t / lh + 1e-9)
    return f"prints as {n} or {n + 1} layers ({n * lh:.2f} or {(n + 1) * lh:.2f} mm at {lh:g} mm layers)"


def _no_inside(subject: Subject, geo: Geo) -> str:
    if not geo.closed:
        return "open mesh: no inside (mesh.open_edges)"
    if subject.negative:
        return "negative parts are not subtracted"
    return ""


class Stability:
    areas: tuple[str, ...] = ("stability",)

    def run(self, subject: Subject, geo: Geo, limits: Limits) -> dict[str, AreaResult]:
        contact = sum(geo.areas[f] for f in geo.contact)
        footprint = (geo.hi[0] - geo.lo[0]) * (geo.hi[1] - geo.lo[1])
        need = max(MIN_CONTACT_MM2, 0.01 * footprint)
        measures: dict[str, Any] = {"contact_mm2": contact}
        if contact < need:
            f = Finding(
                "contact.none",
                FAIL,
                "stability",
                f"{contact:.1f} mm2 flat on the bed, under the {need:.0f} mm2 a first layer needs",
                MEASURED,
                subject.name,
                fix=Fix("choose another pose", command=subject.command),
                measure={"contact_mm2": contact, "needed_mm2": need},
            )
            return {"stability": _result(measures, [f])}
        why = _no_inside(subject, geo)
        if why:
            return {"stability": AreaResult(NOT_CHECKED_STATUS, measures, f"tip-over not checked: {why}", [])}
        vol, com = G.moments(geo.facets)
        if vol <= 0:
            r = _result(measures, [])
            r.reason = "tip-over not checked: the mesh volume is not positive (inverted winding)"
            return {"stability": r}
        hull = G.hull_2d((v[0], v[1]) for f in geo.contact for v in geo.facets[f])
        margin = G.convex_margin((com[0], com[1]), hull)
        height = com[2] - geo.bed
        angle = math.degrees(math.atan2(margin, height)) if height > 0 else 90.0
        measures.update({"com_margin_mm": margin, "tip_angle_deg": angle, "com_height_mm": height})
        fs: list[Finding] = []
        loc = Location(geo.frame, com)
        if margin < 0:
            fs.append(
                Finding(
                    "tip.com_outside",
                    WARN,
                    "stability",
                    f"the centre of mass sits {-margin:.1f} mm outside the bed contact: the part can tip over as it grows",
                    MEASURED,
                    subject.name,
                    loc,
                    {"com_margin_mm": margin},
                    Fix("choose another pose, or add a brim and tell the user", {"recipes": ["tall-outer-brim"]}, subject.command),
                )
            )
        elif angle < TIP_DEG:
            fs.append(
                Finding(
                    "tip.low_margin",
                    INFO,
                    "stability",
                    f"tips over at {angle:.1f} deg: centre of mass {margin:.1f} mm inside the contact edge, {height:.1f} mm up",
                    HEURISTIC,
                    subject.name,
                    loc,
                    {"com_margin_mm": margin, "tip_angle_deg": angle},
                    Fix("an outer brim widens the base", {"recipes": ["tall-outer-brim"]}),
                )
            )
        return {"stability": _result(measures, fs)}


class Adhesion:
    areas: tuple[str, ...] = ("adhesion",)

    def run(self, subject: Subject, geo: Geo, limits: Limits) -> dict[str, AreaResult]:
        contact = sum(geo.areas[f] for f in geo.contact)
        if contact <= 0:
            return {"adhesion": _skip("no bed contact (stability: contact.none)")}
        height = geo.hi[2] - geo.bed
        slender = height / math.sqrt(max(contact, 1.0))
        hull = G.hull_2d((v[0], v[1]) for f in geo.contact for v in geo.facets[f])
        width, length, _ = G.min_width(hull)
        brim = str(limits.v("brim_type"))
        measures = {
            "slenderness": slender,
            "contact_width_mm": width,
            "contact_length_mm": length,
            "brim_type_in_file": brim,
        }
        fs: list[Finding] = []
        reasons = []
        if slender > SLENDER:
            reasons.append(f"slenderness {slender:.1f} (height over the square root of the contact) is over {SLENDER:g}")
        if length >= LONG_CONTACT_MM and width < NARROW_CONTACT_MM:
            reasons.append(f"the contact is {length:.0f} mm long and {width:.1f} mm wide")
        if reasons and brim not in EXPLICIT_BRIMS:
            fs.append(
                Finding(
                    "brim.suggest",
                    WARN,
                    "adhesion",
                    "; ".join(reasons) + f"; brim_type is {brim}",
                    HEURISTIC,
                    subject.name,
                    measure={"slenderness": slender, "contact_width_mm": width, "contact_length_mm": length},
                    fix=Fix("add an outer brim", {"recipes": ["tall-outer-brim"]}),
                )
            )
        elif not reasons and contact >= BIG_CONTACT_MM2 and slender <= 1.0 and brim == "auto_brim":
            fs.append(
                Finding(
                    "brim.not_needed",
                    INFO,
                    "adhesion",
                    f"{contact:.0f} mm2 flat on the bed and slenderness {slender:.1f}: no brim needed; the default is auto_brim",
                    HEURISTIC,
                    subject.name,
                    measure={"contact_mm2": contact, "slenderness": slender},
                    fix=Fix("say it explicitly", {"recipes": ["flat-no-brim"]}),
                )
            )
        return {"adhesion": _result(measures, fs)}


class BedFit:
    areas: tuple[str, ...] = ("fit",)

    def run(self, subject: Subject, geo: Geo, limits: Limits) -> dict[str, AreaResult]:
        facts = limits.machine
        if not facts:
            return {"fit": _skip(limits.machine_reason or "no printer facts")}
        size = tuple(geo.hi[i] - geo.lo[i] for i in range(3))
        bed = facts["bed"]
        bed_lim = (bed["x"][1] - bed["x"][0], bed["y"][1] - bed["y"][0], float(facts.get("z_max") or 0))
        exts = facts.get("extruders") or []
        measures: dict[str, Any] = {"size_mm": [round(s, 2) for s in size], "extruder": subject.extruder}
        fs: list[Finding] = []
        tol = 1e-6
        if any(size[i] > bed_lim[i] + tol for i in range(3)):
            fs.append(
                Finding(
                    "fit.bed",
                    FAIL,
                    "fit",
                    f"{_dims(size)} mm in this pose does not fit the {_dims(bed_lim)} mm build volume",
                    MEASURED,
                    subject.name,
                    measure={"size_mm": list(size), "limit_mm": list(bed_lim)},
                    fix=Fix("another pose that fits, `shambu split`, or a cut in CAD: the user decides", command=subject.command),
                )
            )
            measures["limit_mm"] = list(bed_lim)
            return {"fit": _result(measures, fs)}
        if not 1 <= subject.extruder <= len(exts):
            r = _result(measures, fs)
            r.reason = f"extruder {subject.extruder} is not in the printer facts ({len(exts)} extruders): reach not checked"
            return {"fit": r}
        e = exts[subject.extruder - 1]
        reach = (e["x"][1] - e["x"][0], e["y"][1] - e["y"][0], float(e["z_max"]))
        measures["limit_mm"] = [round(v, 2) for v in reach]
        if any(size[i] > reach[i] + tol for i in range(3)):
            fs.append(
                Finding(
                    "fit.extruder_reach",
                    FAIL,
                    "fit",
                    f"{_dims(size)} mm in this pose is beyond extruder {subject.extruder}'s reach of {_dims(reach)} mm",
                    MEASURED,
                    subject.name,
                    measure={"size_mm": list(size), "limit_mm": list(reach)},
                    fix=Fix("print the part on filament 1 (the left nozzle), or choose another pose"),
                )
            )
        brim = str(limits.v("brim_type"))
        bw = float(limits.v("brim_width_mm"))
        if not fs and brim in EXPLICIT_BRIMS and bw > 0 and any(size[i] + 2 * bw > reach[i] + tol for i in range(2)):
            fs.append(
                Finding(
                    "fit.brim_edge",
                    INFO,
                    "fit",
                    f"the {brim} brim adds 2 x {bw:g} mm: {size[0] + 2 * bw:.1f} x {size[1] + 2 * bw:.1f} mm passes the reach of {_dims(reach[:2])} mm",
                    MEASURED,
                    subject.name,
                    measure={"brim_width_mm": bw},
                    fix=Fix("a narrower brim, or no brim on that side"),
                )
            )
        return {"fit": _result(measures, fs)}


def _dims(v: Sequence[float]) -> str:
    return " x ".join(f"{x:.1f}" for x in v)


def default_checks(samples: int = DEFAULT_SAMPLES) -> tuple[PrintCheck, ...]:
    return (MeshSound(), Overhangs(), Bridges(), Thickness(samples), Stability(), Adhesion(), BedFit())


# ---------------------------------------------------------------------------- report
def _sort_key(f: Finding) -> tuple:
    p = f.location.point if f.location else (0.0, 0.0, 0.0)
    return (AREAS.index(f.area) if f.area in AREAS else len(AREAS), f.code, round(p[0], 3), round(p[1], 3), round(p[2], 3), f.message)


@dataclass
class ObjectReport:
    name: str
    frame: str
    pose: dict | None
    facets: int
    bodies: int
    watertight: bool
    size_mm: list[float]
    extruder: int
    areas: dict[str, AreaResult]
    findings: list[Finding]
    skipped: tuple[str, ...] = ()
    limits: Limits | None = None  # set when the object's limits differ from the report's

    def to_json(self) -> dict:
        d: dict = {
            "name": self.name,
            "frame": self.frame,
            "pose": self.pose,
            "facets": self.facets,
            "bodies": self.bodies,
            "watertight": self.watertight,
            "size_mm": [round(v, 2) for v in self.size_mm],
            "extruder": self.extruder,
            "areas": {a: self.areas[a].to_json() for a in AREAS if a in self.areas},
            "findings": [f.to_json() for f in self.findings],
        }
        if self.skipped:
            d["volumes_not_read"] = list(self.skipped)
        if self.limits is not None:
            d["limits"] = self.limits.to_json()
        return d


@dataclass
class Report:
    file: str
    kind: str  # "stl" or "3mf"
    limits: Limits
    objects: list[ObjectReport]
    elapsed_s: float = 0.0
    not_checked: tuple[dict[str, str], ...] = NOT_CHECKED

    @property
    def findings(self) -> list[Finding]:
        return [f for o in self.objects for f in o.findings]

    @property
    def counts(self) -> dict[str, int]:
        return tally(self.findings)

    @property
    def exit_code(self) -> int:
        """0 with no FAIL finding, 1 with one or more (as `shambu check`)."""
        return 1 if self.counts[FAIL] else 0

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA,
            "file": self.file,
            "kind": self.kind,
            "limits": self.limits.to_json(),
            "objects": [o.to_json() for o in self.objects],
            "not_checked": [dict(x) for x in self.not_checked],
            "counts": self.counts,
            "elapsed_s": round(self.elapsed_s, 2),
        }

    def lines(self) -> list[str]:
        out: list[str] = []
        for o in self.objects:
            pose = ""
            if o.pose is not None:
                bits = [f"{k} {v}" for k, v in o.pose.items() if k != "from" and v is not None]
                pose = f"; pose {', '.join(bits) or 'as modelled'} ({o.pose.get('from')})"
            out.append(
                f"{o.name}: {o.facets} facets, {o.bodies} body(ies), {'watertight' if o.watertight else 'NOT watertight'}, "
                f"{_dims(o.size_mm)} mm, extruder {o.extruder}, {o.frame} frame{pose}"
            )
            if o.pose is not None and o.pose.get("from") == "as-is":
                out.append("  pose as modelled: run `shambu orient` first if the CAD frame is not the print frame")
            if o.skipped:
                out.append(f"  volumes not read: {', '.join(o.skipped)}")
            for line in (o.limits or self.limits).text():
                out.append("  " + line)
            for a in AREAS:
                r = o.areas.get(a)
                if r is not None:
                    out.append(f"  {a:<10} {r.status:<12} {_area_text(a, r)}")
            out += ["  " + f.text() for f in o.findings]
        out.append("not checked: " + "; ".join(f"{x['what']} ({x['use']})" for x in self.not_checked))
        c = self.counts
        out.append(f"printability: {c[FAIL]} FAIL, {c[WARN]} WARN, {c[INFO]} INFO (no overall score: the slice decides)")
        return out


def _area_text(area: str, r: AreaResult) -> str:
    m = r.measures
    if r.status == NOT_CHECKED_STATUS:
        return r.reason or ""

    def mm(v: Any) -> str:
        return "none sampled" if v is None else f"{v:.2f} mm"

    texts = {
        "mesh": lambda: f"{m['open_edges']} open edges, {m['flipped_edges']} flipped, {m['bodies']} body(ies)",
        "support": lambda: (
            f"{m['overhang_mm2_beyond_threshold']:.0f} mm2 beyond the threshold in {m['regions']} region(s) "
            f"(+{m['small_regions']} small), shadow {m['shadow_mm2']:.0f} mm2; {m['overhang_mm2_45_to_threshold']:.0f} mm2 between 45 deg and the threshold"
        ),
        "bridges": lambda: (
            f"{m['bridges']} bridge(s), longest {m['longest_span_mm']:.1f} mm, {m['spans_supported_by_studio']} supported by Studio"
        ),
        "walls": lambda: f"thinnest {mm(m['thinnest_wall_mm_sampled'])} ({m['samples']} of {m['facets']} facets sampled)",
        "layers": lambda: f"thinnest slab {mm(m['thinnest_slab_mm_sampled'])} at {m['layer_height_mm']:g} mm layers",
        "stability": lambda: (
            f"contact {m['contact_mm2']:.0f} mm2"
            + (f", margin {m['com_margin_mm']:.1f} mm, tip angle {m['tip_angle_deg']:.0f} deg" if "com_margin_mm" in m else "")
        ),
        "adhesion": lambda: (
            f"slenderness {m['slenderness']:.1f}, contact {m['contact_length_mm']:.0f} x {m['contact_width_mm']:.1f} mm, brim {m['brim_type_in_file']}"
        ),
        "fit": lambda: (
            f"size {_dims(m['size_mm'])} mm, extruder {m['extruder']}" + (f", limit {_dims(m['limit_mm'])} mm" if "limit_mm" in m else "")
        ),
    }
    s = texts[area]() if m else ""
    return s + (f"; {r.reason}" if r.reason else "")


def analyse(
    subjects: Sequence[Subject],
    limits: Limits,
    file: str = "",
    kind: str = "stl",
    checks: Sequence[PrintCheck] | None = None,
    samples: int = DEFAULT_SAMPLES,
) -> Report:
    """Run every check on every subject. Findings are sorted by area, code and location, so the facet order of the
    input does not change the report."""
    start = time.monotonic()
    checks = tuple(checks) if checks is not None else default_checks(samples)
    objects: list[ObjectReport] = []
    for s in subjects:
        lim = s.limits or limits
        geo = Geo(s)
        areas: dict[str, AreaResult] = {}
        for c in checks:
            areas.update(c.run(s, geo, lim))
        findings = sorted((f for r in areas.values() for f in r.findings), key=_sort_key)
        mesh = areas.get("mesh")
        objects.append(
            ObjectReport(
                s.name,
                s.frame,
                s.pose,
                len(s.facets),
                mesh.measures.get("bodies", 1) if mesh else len(G.regions(range(len(s.facets)), geo.welded)),
                geo.closed and not geo.flipped,
                [geo.hi[i] - geo.lo[i] for i in range(3)],
                s.extruder,
                areas,
                findings,
                s.skipped,
                s.limits if s.limits is not None and s.limits != limits else None,
            )
        )
    return Report(file, kind, limits, objects, time.monotonic() - start)
