"""Place parts on the bed with known positions and check every machine limit before the CLI runs.

Positions are computed here, never by Bambu's arrange, so the report can state where every part
sits. The CLI's pos_x/pos_y/pos_z are a translation added to the STL's own coordinates, so a part
whose bounding-box min corner is (x0, y0) on the bed gets pos = (x0 - min_x, y0 - min_y, -min_z).

Design: `Placer` is a Strategy (`BottomLeftPacker` for by-layer plates, `ShelfPacker` for print-by-object order and
filament groups). A placer never raises for a part that does not fit: it returns that part in `Placement.unplaced`
with the arithmetic, and the geometric rules skip it. `Footprint` is a Strategy too: `RasterFootprint` (the mesh's XY
silhouette after the orientation) or `BoxFootprint` (the bounding box). Limit checks are a list of `Rule`
strategies run by `LimitChecker`, so a new printer fact is one more rule. `ExtruderReach` is the one reach rule: it
also holds a slot that prints only the support interface to the interface spread (`Placed.interface`). The prime tower
is placed by `tower.py` against these footprints and spreads (`tower.FootprintClearance`).

What Studio enforces, by layer (Bambu Studio 02.08.02.61 source, reference 08 section 11):
- no clearance between objects before slicing (`Print.cpp:944-1050` checks hulls against the bed exclude area only);
- after slicing, exit -101 when the extrusion centrelines of two objects in one layer overlap collinearly by more than
  0.01 mm (any roles), or cross more than 0.01 mm from an endpoint; crossings of two supports only count past 100 mm,
  which almost switches them off (`ConflictChecker.cpp:291-310`). The check takes the first instance of each
  PrintObject (`ConflictChecker.cpp:226, 244-245`), and Studio groups the instances of one model object that differ
  only by an XY shift into one PrintObject (`PrintApply.cpp:140-160`). shambu's `count` copies are separate model
  objects (live: two `<object>` entries and two build items in the exported 3MF), so every copy is checked. A project
  with GUI instances (Ctrl+K copies) is not: there only the first copy is clash-tested;
- brims and skirts are not in that test; brims of different objects are clipped against each other, against support
  and against the bed and each nozzle's reach (`Brim.cpp:258-365, 1011-1138`), so a brim crossing is a WARN here;
- tree supports grow per object, against that object's outlines and the bed border only
  (`TreeSupport3D.cpp:5232-5246`, `TreeSupport.cpp:626-629, 2507-2522`): they do not avoid a neighbour, and they never
  leave the bed.
So the pair rule (`Clearance`) holds the support between models, not the brim, and takes the larger of the two facing
spreads, not their sum. A probed part brings its measured support mask, which the pair rule uses in place of the four
spreads (a support inside a U-shaped notch stays inside the box). An estimated spread that is short is a WARN: the
slice (exit -101) decides. "Studio has a support" is `needs_support` (`Print.hpp:492-494`).

Not here: Studio's own arrange, rotation trials, z-aware support (the spread is the widest band, z 0..5).
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol

from .errors import BambuError, LayoutError
from .studio import MeshInfo

BED = 256.0
EDGE = 2.0
GAP = 5.0
# The line allowance: shambu's choice, not a Studio limit. Studio has no clearance distance, it refuses lines that
# overlap or cross by more than 0.01 mm (ConflictChecker.cpp:291-310). Two unsupported models 0 mm apart sliced with
# exit 0 (walls 0.42 mm apart, reference 08 section 11.4). 1.0 mm, about two line widths, covers the slicer's own
# rounding; it is not a measured threshold.
LINE = 1.0
# Tree support spread past the model box, per side, as an estimate (reference 08 section 11.2, G-code of the printed
# plate 14): the housing, 218 mm tall, 20.9 mm on -x at z 0..5 (20.9 / 218 = 0.096); the bracket, 170 mm tall,
# 15.6 mm (0.092, section 3.6); the cradle, 28.9 mm tall, 7.1 mm. Studio's tree first-layer brim (auto) is 1..10 mm
# (TreeSupport.cpp:2476-2484, ExPolygon.cpp:416-430), so short parts get the 8 mm floor.
TREE_SPREAD_PER_MM = 0.10
TREE_SPREAD_FLOOR = 8.0
TREE_SPREAD_CAP = 22.0  # the largest measured spread, 20.9 mm, plus one line
# Normal (auto) support: 4.4 mm at z 0..5 and 2.5 mm above on the live clash test (reference 08 section 11.4). Studio's
# own arrange assumes 6 (ModelArrange.cpp:172-173).
NORMAL_SPREAD = 5.0
# The support interface on another extruder (`dual-material-interface`): it prints past the part box by the brim
# margin plus this, on every side (2.2 mm measured on the seed cradle, -0.2 mm on the housing; n=1 each). The seed
# cradle at x 9.2 exited -102 with the right nozzle's interface at x 14.3 (reference 06 section 5).
INTERFACE_SPREAD = 3.0
SIDES = ("-x", "+x", "-y", "+y")

Rect = tuple[float, float, float, float]  # x0, y0, x1, y1
Runs = tuple[tuple[float, float], ...]
Band = tuple[float, float, Runs]  # y0, y1, x runs: one row of a footprint
# Print-by-object head clearance (Bambu Lab X2D values; the machine preset overrides them). See SequentialClearance.
DEFAULT_CLEARANCE = {"radius": 60.0, "height_to_rod": 32.8, "height_to_lid": 133.0, "dist_to_rod": 33.0, "nozzle_height": 4.0}


# ---------------------------------------------------------------------------- support spread
@dataclass(frozen=True)
class Spread:
    """Support reach past the model's bounding box, mm, per bed side (-x, +x, -y, +y).

    `source`: "probe" (measured on a solo slice of the part), "estimate" (`support_spread`), "spec" (the part's
    `brim_margin` or `support_spread`) or "none". A probe or spec value is a hard limit between parts (FAIL). An
    estimate gives a WARN. `tree`: tree support, which Studio clips to the bed border, so the bed rule leaves it out.
    `note` says where the number came from, for the report."""

    mx: float = 0.0
    px: float = 0.0
    my: float = 0.0
    py: float = 0.0
    source: str = "none"
    tree: bool = False
    note: str = ""

    def sides(self) -> tuple[float, float, float, float]:
        return (self.mx, self.px, self.my, self.py)

    def most(self) -> float:
        return max(self.sides())

    @property
    def hard(self) -> bool:
        return self.source in ("probe", "spec")

    def facing(self, dx: float, dy: float) -> tuple[float, str]:
        """The spread on the side that faces a neighbour in direction (dx, dy), and that side's name. A diagonal
        neighbour faces two sides: the larger counts. No direction (the footprints meet): the largest side."""
        cands = []
        if dx > 0:
            cands.append((self.px, "+x"))
        elif dx < 0:
            cands.append((self.mx, "-x"))
        if dy > 0:
            cands.append((self.py, "+y"))
        elif dy < 0:
            cands.append((self.my, "-y"))
        if not cands:
            cands = list(zip(self.sides(), SIDES, strict=True))
        return max(cands, key=lambda c: c[0])

    def describe(self) -> str:
        v = self.sides()
        if max(v) == min(v):
            return f"{v[0]:.1f} mm each side"
        return ", ".join(f"{n} {x:.1f}" for n, x in zip(SIDES, v, strict=True)) + " mm"


NO_SPREAD = Spread()


def _int(v) -> int:
    """An optional integer setting (enforce_support_layers, raft_layers): a missing or malformed value is 0, Studio's
    default (a documented silent fallback, rubric 13)."""
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return 0


def needs_support(params: Mapping | None) -> bool:
    """Studio prints support material for an object when `enable_support`, or `enforce_support_layers > 0`, or
    `raft_layers > 0` (`Print.hpp:492-494`: has_support() || has_raft()). The one predicate for the layout and for
    the filament accounting (a raft prints with the support filaments too)."""
    p = params or {}
    return (
        str(p.get("enable_support", "0")).strip().lower() in ("1", "true")
        or _int(p.get("enforce_support_layers", 0)) > 0
        or _int(p.get("raft_layers", 0)) > 0
    )


# Object and region keys that choose a filament slot (0: the object's own filament). One classification for a spec
# part (build.effective_filaments) and an object of a saved project (edits), so `check` and `patch` agree.
FILAMENT_KEYS = (
    "extruder",
    "wall_filament",
    "sparse_infill_filament",
    "solid_infill_filament",
    "support_filament",
    "support_interface_filament",
)
SUPPORT_FILAMENT_KEYS = ("support_filament", "support_interface_filament")
WALL_INFILL_KEYS = ("wall_filament", "sparse_infill_filament", "solid_infill_filament")
INTERFACE_KEY = "support_interface_filament"
SlotParser = Callable[[object, str], int]


def filament_slots(
    base: Iterable[int],
    settings: Mapping,
    ranges: Iterable[tuple[str, Mapping]],
    process: Mapping | None,
    slot: SlotParser,
    *,
    skip: Sequence[str] = (),
    set_label: str = "set.",
) -> set[int]:
    """Every filament slot an object prints with: `base` (its own filament or its sub-parts'), the wall, infill,
    support and interface keys of its `settings`, every key of each height range (`(label, settings)`), and the
    process's wall and infill keys, and its support keys when Studio prints support for the object (`needs_support`
    on process plus settings: a support slot of an object without support is unused). The object's own value of a key
    wins over the process's. `skip` leaves out those keys of `settings` and the process. `slot(value, where)` parses one
    value (0: none) and raises its caller's error type."""
    out = set(base)
    support = needs_support({**(process or {}), **settings})
    for k in FILAMENT_KEYS[1:]:
        if k in skip or (k in SUPPORT_FILAMENT_KEYS and not support):
            continue
        if k in settings and (v := slot(settings[k], f"{set_label}{k}")):
            out.add(v)
    for label, r in ranges:
        for k in FILAMENT_KEYS:
            if k in r and (v := slot(r[k], f"{label} {k}")):
                out.add(v)
    if process is not None:
        for k in WALL_INFILL_KEYS + (SUPPORT_FILAMENT_KEYS if support else ()):
            if k not in skip and k not in settings and k in process and (v := slot(process[k], f"process {k}")):
                out.add(v)
    return out


def interface_only(
    settings: Mapping, process: Mapping, others: set[int], spread: float, slot: SlotParser, set_label: str = "set."
) -> dict[int, tuple[float, str]]:
    """The slot that prints only the object's support interface, as {slot: (spread, source)} for `Placed.interface`:
    empty when Studio prints no support for it, the slot is 0, or the object prints with that slot anyway (`others`:
    `filament_slots` with the interface key skipped)."""
    if not needs_support({**process, **settings}):
        return {}
    src = "part" if INTERFACE_KEY in settings else "process"
    raw = settings.get(INTERFACE_KEY, process.get(INTERFACE_KEY, "0"))
    v = slot(str(raw).strip() or "0", f"{set_label}{INTERFACE_KEY}" if src == "part" else f"process {INTERFACE_KEY}")
    if not v or v in others:
        return {}
    return {v: (spread, f"{src} {INTERFACE_KEY}")}


def is_tree(p: Mapping) -> bool:
    """The params ask for tree support (Studio's default support_type is tree(auto) on the X2D presets)."""
    return str(p.get("support_type", "tree(auto)")).startswith("tree")


def clipped_at_bed(params: Mapping | None) -> bool:
    """Studio clips this part's support to the bed border: tree support (TreeSupport.cpp:2507-2522) and no raft. Raft
    paths are not clipped by the tree code (TreeSupport.cpp:1591-1617), so a raft keeps its bed margin. The one
    classification for the estimate, the probe and the spec's support_spread."""
    p = params or {}
    supported = str(p.get("enable_support", "0")).strip().lower() in ("1", "true") or _int(p.get("enforce_support_layers", 0)) > 0
    return supported and is_tree(p) and _int(p.get("raft_layers", 0)) <= 0


def support_spread(params: Mapping | None, height: float, probe: Spread | None = None) -> Spread:
    """How far a part's support reaches past its bounding box, per side.

    No support: nothing. A probe (a solo slice of the part, measured per side): returned as it is. Otherwise an
    estimate on all four sides, because the mesh does not tell which side the trees grow on (reference 08 section
    11.2: the housing's -y and +y overhangs had 0.3 and 2.9 mm of spread, its -x overhang 20.9 mm):
    tree `min(22, max(8, 0.10 x height))`, normal 5 mm. A raft without support (raft_layers > 0) grows like normal
    support: 5 mm, or `raft_first_layer_expansion` when that is larger."""
    p = params or {}
    if not needs_support(p):
        return NO_SPREAD
    if probe is not None:
        return probe
    if str(p.get("enable_support", "0")).strip().lower() not in ("1", "true") and _int(p.get("enforce_support_layers", 0)) <= 0:
        try:
            exp = float(p.get("raft_first_layer_expansion", -1))
        except (TypeError, ValueError):
            exp = -1.0
        s = max(NORMAL_SPREAD, exp)
        return Spread(s, s, s, s, "estimate", False, "raft, estimated")
    if is_tree(p):
        raw = TREE_SPREAD_PER_MM * height
        s = min(TREE_SPREAD_CAP, max(TREE_SPREAD_FLOOR, raw))
        if raw < TREE_SPREAD_FLOOR:
            note = f"support {s:.1f} mm, the tree minimum ({TREE_SPREAD_PER_MM:.2f} x {height:.1f} mm height is less), estimated"
        elif raw > TREE_SPREAD_CAP:
            note = f"support {s:.1f} mm, the tree cap ({TREE_SPREAD_PER_MM:.2f} x {height:.1f} mm height is more), estimated"
        else:
            note = f"support {TREE_SPREAD_PER_MM:.2f} x {height:.1f} mm height, estimated"
        return Spread(s, s, s, s, "estimate", clipped_at_bed(p), note)
    return Spread(NORMAL_SPREAD, NORMAL_SPREAD, NORMAL_SPREAD, NORMAL_SPREAD, "estimate", False, "normal support, estimated")


def spec_spread(margin: float) -> Spread:
    """The part's `brim_margin` from the spec: it replaces brim and support spread together, on every side, and at the
    bed edge too (it is the user's number, not a tree spread that Studio clips)."""
    return Spread(margin, margin, margin, margin, "spec", False, "spec brim_margin")


def brim_margin(params: dict | None, default_brim_width: float = 5.0) -> float:
    """Footprint growth from a brim: outer_only/auto grow by brim_width, no_brim by 0, ears by 3."""
    p = params or {}
    bt = str(p.get("brim_type", "auto_brim"))
    if bt == "no_brim":
        return 0.0
    if bt == "brim_ears":
        return 3.0
    try:
        return float(p.get("brim_width", default_brim_width)) + 0.2
    except (TypeError, ValueError):
        return default_brim_width + 0.2


# ---------------------------------------------------------------------------- footprints
def _runs_gap(ra: Runs, ax: float, rb: Runs, bx: float) -> float:
    """Signed horizontal gap between two sorted run lists: + when B lies to the right, - to the left, 0 when a run of
    A meets a run of B."""
    i = j = 0
    best, sign = math.inf, 1.0
    while i < len(ra) and j < len(rb):
        a0, a1 = ra[i][0] + ax, ra[i][1] + ax
        b0, b1 = rb[j][0] + bx, rb[j][1] + bx
        if a1 < b0:
            if b0 - a1 < best:
                best, sign = b0 - a1, 1.0
            i += 1
        elif b1 < a0:
            if a0 - b1 < best:
                best, sign = a0 - b1, -1.0
            j += 1
        else:
            return 0.0
    return sign * best


def _bands_gap(
    a: Sequence[Band], ax: float, ay: float, b: Sequence[Band], b_y0: Sequence[float], bx: float, by: float, limit: float
) -> tuple[float, tuple[float, float]]:
    """Shortest distance between two band sets (local coordinates plus offsets) and its direction A -> B. For every
    band of A it walks the bands of B outward in y until the vertical distance alone reaches the best so far."""
    best, best_dir = limit, (0.0, 0.0)
    for ya0, ya1, ra in a:
        ya0, ya1 = ya0 + ay, ya1 + ay
        k = bisect.bisect_left(b_y0, ya0 - by)
        for step, start in ((-1, k - 1), (1, k)):
            j = start
            while 0 <= j < len(b):
                yb0, yb1, rb = b[j]
                yb0, yb1 = yb0 + by, yb1 + by
                dy = yb0 - ya1 if yb0 > ya1 else (yb1 - ya0 if yb1 < ya0 else 0.0)
                if abs(dy) >= best:
                    break
                dx = _runs_gap(ra, ax, rb, bx)
                d = math.hypot(dx, dy)
                if d < best:
                    best, best_dir = d, (dx, dy)
                    if d == 0.0:
                        return 0.0, best_dir
                j += step
    return best, best_dir


def rect_gap(a: Rect, b: Rect) -> tuple[float, tuple[float, float]]:
    """Euclidean gap between two rectangles and its signed direction A -> B (0 on an axis where they overlap)."""
    dx = b[0] - a[2] if b[0] > a[2] else (b[2] - a[0] if b[2] < a[0] else 0.0)
    dy = b[1] - a[3] if b[1] > a[3] else (b[3] - a[1] if b[3] < a[1] else 0.0)
    return math.hypot(dx, dy), (dx, dy)


class Footprint(Protocol):
    """A part's outline on the bed (XY). Two implementations: RasterFootprint and BoxFootprint."""

    def bbox(self) -> Rect: ...

    def translated(self, dx: float, dy: float) -> Footprint: ...

    def grown(self, mx: float, px: float, my: float, py: float) -> Footprint: ...

    def area(self) -> float: ...

    def gap(self, other: Footprint, limit: float = math.inf) -> tuple[float, tuple[float, float]]:
        """Shortest distance to `other` and its direction (dx, dy) from self to other. 0 when they meet. A result at or
        above `limit` is only a lower bound."""
        ...


@dataclass(frozen=True)
class _Bands:
    """Shared gap logic: bands in a local frame plus an offset on the bed. The local bbox starts at (lx, ly): 0, 0 for
    a part's outline (its bbox minimum), anywhere for a support mask, which can reach past the part's box."""

    rows: tuple[Band, ...]
    w: float
    h: float
    ox: float = 0.0
    oy: float = 0.0
    lx: float = 0.0
    ly: float = 0.0

    def bbox(self) -> Rect:
        return (self.ox + self.lx, self.oy + self.ly, self.ox + self.lx + self.w, self.oy + self.ly + self.h)

    def to_json(self) -> dict:
        """The local bands (no offset), for the probe cache."""
        return {
            "rows": [[y0, y1, [list(r) for r in runs]] for y0, y1, runs in self.rows],
            "w": self.w,
            "h": self.h,
            "lx": self.lx,
            "ly": self.ly,
        }

    def translated(self, dx: float, dy: float) -> Footprint:
        return replace(self, ox=self.ox + dx, oy=self.oy + dy)

    def grown(self, mx: float, px: float, my: float, py: float) -> Footprint:
        """The outline swept by mx/px/my/py mm toward -x/+x/-y/+y (each band widens and stretches), for the gap test
        of a support spread. Grown bands may overlap in y; their y0 and y1 stay in the same order, which the gap walk
        needs. Not for `area`."""
        rows = []
        for y0, y1, runs in self.rows:
            merged: list[list[float]] = []
            for a, b in runs:
                if merged and a - mx <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], b + px)
                else:
                    merged.append([a - mx, b + px])
            rows.append((y0 - my, y1 + py, tuple((a, b) for a, b in merged)))
        return replace(self, rows=tuple(rows), w=self.w + mx + px, h=self.h + my + py, lx=self.lx - mx, ly=self.ly - my)

    def area(self) -> float:
        return sum((y1 - y0) * sum(b - a for a, b in runs) for y0, y1, runs in self.rows)

    def gap(self, other: Footprint, limit: float = math.inf) -> tuple[float, tuple[float, float]]:
        g, d = rect_gap(self.bbox(), other.bbox())
        if g >= limit or not isinstance(other, _Bands):
            return g, d
        a, b, flip = (self, other, 1.0) if len(self.rows) <= len(other.rows) else (other, self, -1.0)
        best, (dx, dy) = _bands_gap(a.rows, a.ox, a.oy, b.rows, [r[0] for r in b.rows], b.ox, b.oy, limit)
        if best >= limit:
            return max(limit, g), d
        return best, (flip * dx, flip * dy)


@dataclass(frozen=True)
class BoxFootprint(_Bands):
    """The bounding box as a footprint: for a part without a mesh (a filament group, a test MeshInfo)."""

    @classmethod
    def of(cls, w: float, h: float) -> BoxFootprint:
        return cls(((0.0, h, ((0.0, w),)),), w, h)


@dataclass(frozen=True)
class RasterFootprint(_Bands):
    """The mesh's XY silhouette (`mesh.silhouette`, after the orientation) as row runs. On the printed plate 14 it
    sees the gap between the feeler's end blocks and the cradle, where the boxes overlap by 3.7 mm and the convex
    hulls touch (reference 08 section 11.2)."""

    @classmethod
    def from_silhouette(cls, sil) -> RasterFootprint:
        """From a `mesh.Silhouette`: the frame moves to the silhouette's bbox minimum, and empty rows are dropped."""
        rows = []
        for r, runs in enumerate(sil.rows):
            if not runs:
                continue
            lo, hi = sil.row_span(r)
            rows.append((lo - sil.y0, hi - sil.y0, tuple((a - sil.x0, b - sil.x0) for a, b in runs)))
        return cls(tuple(rows), sil.x1 - sil.x0, sil.y1 - sil.y0)

    @classmethod
    def from_json(cls, d: dict) -> RasterFootprint:
        rows = tuple((float(y0), float(y1), tuple((float(a), float(b)) for a, b in runs)) for y0, y1, runs in d["rows"])
        return cls(rows, float(d["w"]), float(d["h"]), lx=float(d.get("lx", 0.0)), ly=float(d.get("ly", 0.0)))

    @classmethod
    def from_cells(cls, cells: Mapping[int, Iterable[int]], cell: float) -> RasterFootprint | None:
        """From occupied grid cells {row: columns} of size `cell` in the local frame (cell (0, 0) starts at 0, 0)."""
        rows: list[Band] = []
        for r in sorted(cells):
            cs = sorted(set(cells[r]))
            if not cs:
                continue
            runs = []
            start = prev = cs[0]
            for c in cs[1:]:
                if c != prev + 1:
                    runs.append((start * cell, (prev + 1) * cell))
                    start = c
                prev = c
            runs.append((start * cell, (prev + 1) * cell))
            rows.append((r * cell, (r + 1) * cell, tuple(runs)))
        if not rows:
            return None
        lx = min(rr[0][0] for _, _, rr in rows)
        hx = max(rr[-1][1] for _, _, rr in rows)
        return cls(tuple(rows), hx - lx, rows[-1][1] - rows[0][0], lx=lx, ly=rows[0][0])


OUTLINE_BOX_TOL = 0.5  # mm: an outline whose box differs more from the expected box is not trusted (the box stands in)


def outline_of(facets: Sequence, box: tuple[float, float, float, float]) -> tuple[Footprint | None, str]:
    """The XY outline of `facets` (mesh.silhouette, after the orientation) as a RasterFootprint, and "". None (the
    bounding box) plus the reason when there are no facets, the mesh does not rasterise, or the outline's box differs
    from `box` (x0, y0, x1, y1: Studio's box for a spec part, the world box for a project object) by more than
    OUTLINE_BOX_TOL (a unit conversion, a 3MF input). The one outline rule for `check` and `patch`."""
    from . import mesh as M  # mesh is a sibling planning module; imported here to keep layout's import light

    if not facets:
        return None, ""
    try:
        sil = M.silhouette(facets)
    except BambuError as e:  # mesh raises BuildError for an empty mesh: the box stands in, with the reason
        return None, str(e)
    off = max(abs(a - b) for a, b in zip((sil.x0, sil.y0, sil.x1, sil.y1), box, strict=True))
    if off > OUTLINE_BOX_TOL:
        return None, f"the mesh's box differs from Studio's by {off:.1f} mm (a unit conversion or a 3MF input?)"
    return RasterFootprint.from_silhouette(sil), ""


SUPPORT_MASK_CELL = 0.5  # the same cell as mesh.SILHOUETTE_CELL


def mask_from_points(points: Iterable[tuple[float, float]], cell: float = SUPPORT_MASK_CELL) -> RasterFootprint | None:
    """The cells that sampled support centrelines touch (local frame of the part), as a footprint. probe.py samples
    every extrusion at least every `cell` mm, so a line marks every cell it crosses."""
    cells: dict[int, set[int]] = {}
    for x, y in points:
        cells.setdefault(math.floor(y / cell), set()).add(math.floor(x / cell))
    return RasterFootprint.from_cells(cells, cell)


def union_footprint(shapes: Iterable[Footprint], origin: tuple[float, float], cell: float = SUPPORT_MASK_CELL) -> Footprint | None:
    """One outline for several placed shapes (a filament group), in the frame that starts at `origin`: every band of
    every shape, rasterised outward to `cell`. None when a shape has no bands."""
    cells: dict[int, set[int]] = {}
    for s in shapes:
        if not isinstance(s, _Bands):
            return None
        for y0, y1, runs in s.rows:
            r0, r1 = math.floor((y0 + s.oy - origin[1]) / cell + 1e-9), math.ceil((y1 + s.oy - origin[1]) / cell - 1e-9)
            for a, b in runs:
                c0, c1 = math.floor((a + s.ox - origin[0]) / cell + 1e-9), math.ceil((b + s.ox - origin[0]) / cell - 1e-9)
                for r in range(r0, max(r1, r0 + 1)):
                    cells.setdefault(r, set()).update(range(c0, max(c1, c0 + 1)))
    return RasterFootprint.from_cells(cells, cell)


# ---------------------------------------------------------------------------- placed parts
@dataclass
class Placed:
    """A part with its mesh info and where its bounding box sits on the bed.

    `brim` is the brim margin (0 when the spec's brim_margin is the spread): it counts at the bed edge and in the
    extruder reach, never between two parts (Studio clips brims against neighbours, Brim.cpp:258-365). `spread` is the
    support reach past the box per side. `footprint` is the outline in the part's own frame (bbox minimum at 0, 0);
    None means the box. `placed` is False when the placer found no position: no geometric rule runs on the part, and
    `why_not` says why. `interface` holds the slots that print only the part's support interface, each with how far
    that interface reaches past the box on every side and where the slot comes from: `ExtruderReach` holds that slot's
    extruder to this spread instead of the support spread."""

    key: str
    info: MeshInfo
    filament: int
    x0: float = 0.0
    y0: float = 0.0
    brim: float = 0.0
    fixed: bool = False  # placed by the spec, not by the packer
    also_filaments: frozenset[int] = frozenset()  # filaments used by overrides, height ranges or sub-parts
    spread: Spread = NO_SPREAD
    footprint: Footprint | None = None
    placed: bool = True
    why_not: str = ""
    support_mask: Footprint | None = None  # measured support centrelines (probe), same frame as `footprint`
    interface: Mapping[int, tuple[float, str]] = field(default_factory=dict)  # slot -> (spread mm, "process support_interface_filament")

    @property
    def filaments(self) -> set[int]:
        return {self.filament, *self.also_filaments}

    @property
    def w(self) -> float:
        return self.info.size[0]

    @property
    def h(self) -> float:
        return self.info.size[1]

    @property
    def x1(self) -> float:
        return self.x0 + self.w

    @property
    def y1(self) -> float:
        return self.y0 + self.h

    def reach_growth(self, filament: int) -> tuple[tuple[float, float, float, float], str]:
        """How far the prints of `filament` reach past the box (-x, +x, -y, +y) and what reaches that far: the support
        spread, or the interface spread for a slot that prints only the support interface (it holds the brim)."""
        if filament in self.interface:
            v, _ = self.interface[filament]
            return (v, v, v, v), "support interface"
        return self.spread.sides(), "support"

    @property
    def keep_out(self) -> float:
        """The keep-out the row packer holds on every side: the larger of brim and support spread."""
        return max(self.brim, self.spread.most())

    def bed_margin(self, with_brim: bool = True) -> tuple[float, float, float, float]:
        """Growth per side (-x, +x, -y, +y) that must stay on the bed: the support spread unless it is tree support
        (Studio clips trees to the bed border, TreeSupport.cpp:2507-2522), plus the brim when `with_brim` (Studio
        clips a brim too, Brim.cpp:1011-1138, so a brim off the bed is a WARN, not a FAIL)."""
        s = (0.0, 0.0, 0.0, 0.0) if self.spread.tree else self.spread.sides()
        m = self.brim if with_brim else 0.0
        return (m + s[0], m + s[1], m + s[2], m + s[3])

    def rect(self, with_margin: bool = True) -> Rect:
        """The bounding box, grown by brim plus support spread on each side when `with_margin`."""
        if not with_margin:
            return (self.x0, self.y0, self.x1, self.y1)
        m, s = self.brim, self.spread
        return (self.x0 - m - s.mx, self.y0 - m - s.my, self.x1 + m + s.px, self.y1 + m + s.py)

    def shape(self) -> Footprint:
        fp = self.footprint if self.footprint is not None else BoxFootprint.of(self.w, self.h)
        return fp.translated(self.x0, self.y0)

    def mask_shape(self) -> Footprint | None:
        return self.support_mask.translated(self.x0, self.y0) if self.support_mask is not None else None

    def pos(self) -> tuple[float, float, float]:
        """Translation for the assemble list: bbox starts at (x0, y0) and the part sits on the bed."""
        return (self.x0 - self.info.min[0], self.y0 - self.info.min[1], -self.info.min[2])

    def margin_text(self) -> str:
        """Where the brim and the spread come from, for a report line: `spread 8.0 mm = support 0.10 x 64.0 mm height,
        estimated; brim 5.2 mm`."""
        parts = []
        if self.spread.most() > 0:
            v = self.spread.describe() if self.spread.source != "estimate" else f"{self.spread.most():.1f} mm"
            parts.append(f"spread {v} = {self.spread.note or self.spread.source}")
        if self.brim > 0:
            parts.append(f"brim {self.brim:.1f} mm")
        return "; ".join(parts) or "no brim or spread"


def group_spread(members: Sequence[Placed]) -> Spread:
    """The support spread of several placed parts as one object (a filament group): per side, how far any member's
    support reaches past the union box. Each side is its own maximum (a member at the left edge with 20 mm on -x and one
    at the right edge with 20 mm on +x give 20 on both), not one member's whole spread. An estimate in any member makes
    the result an estimate; it is tree (clipped at the bed) only when every supported member's is."""
    if not members:
        return NO_SPREAD
    gx0, gy0 = min(m.x0 for m in members), min(m.y0 for m in members)
    gx1, gy1 = max(m.x1 for m in members), max(m.y1 for m in members)
    sup = [m for m in members if m.spread.most() > 0]
    if not sup:
        return NO_SPREAD
    mx = max(max(0.0, gx0 - (m.x0 - m.spread.mx)) for m in sup)
    px = max(max(0.0, m.x1 + m.spread.px - gx1) for m in sup)
    my = max(max(0.0, gy0 - (m.y0 - m.spread.my)) for m in sup)
    py = max(max(0.0, m.y1 + m.spread.py - gy1) for m in sup)
    sources = {m.spread.source for m in sup}
    source = "estimate" if "estimate" in sources else ("probe" if "probe" in sources else next(iter(sources)))
    return Spread(mx, px, my, py, source, all(m.spread.tree for m in sup), f"group of {len(members)}: per-side maxima")


def group_mask(members: Sequence[Placed], origin: tuple[float, float]) -> Footprint | None:
    """The members' measured support masks as one mask in the group frame that starts at `origin`, or None when a
    supported member has no mask (the pair rule then uses the group spread for all of them)."""
    sup = [m for m in members if m.spread.most() > 0 or m.support_mask is not None]
    shapes = [m.mask_shape() for m in sup]
    if not sup or any(s is None for s in shapes):
        return None
    return union_footprint([s for s in shapes if s is not None], origin)


def union_info(infos: Sequence[MeshInfo]) -> MeshInfo:
    """One MeshInfo for several meshes that share one translation (the sub-parts of a merged object):
    the union bounding box in the shared STL frame, manifold only if every mesh is."""
    if not infos:
        raise LayoutError("union_info needs at least one mesh")
    if len(infos) == 1:
        return infos[0]
    lo = (min(i.min[0] for i in infos), min(i.min[1] for i in infos), min(i.min[2] for i in infos))
    hi = (max(i.max[0] for i in infos), max(i.max[1] for i in infos), max(i.max[2] for i in infos))
    return MeshInfo(
        infos[0].path,
        (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]),
        lo,
        hi,
        all(i.manifold for i in infos),
        sum(i.facets for i in infos),
        sum(i.parts for i in infos),
        sum(i.volume_mm3 for i in infos),
    )


def overlaps(a: Rect, b: Rect) -> bool:
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


# ---------------------------------------------------------------------------- checks
@dataclass
class Check:
    """One line of the plate's check. `kind` sorts it: body (the parts themselves meet), margin (the margin rule: a
    support spread reaches another part or the bed edge, the bodies are apart), placement (the packer could not place a part), bed, reach, height, spool, tower,
    sequence, mesh, summary, brim (a brim Studio will clip), outline (the bounding box stands in for a mesh that did not
    rasterise). An ok Check of kind margin, brim or outline is a WARN."""

    ok: bool
    text: str
    kind: str = ""

    @property
    def warn(self) -> bool:
        return self.ok and self.kind in ("margin", "brim", "outline")


def _side_reaches(src: Placed, dst: Footprint) -> list[tuple[float, str]]:
    """The sides of src's support spread that reach within the line allowance of dst's model. First src's outline
    grown by all four spreads at once is tested against the whole of dst (an L-shaped neighbour can be nearest on one
    side and still be crossed on another); then each side alone, to name the side. When only the corner of two sides
    reaches, the facing side with the larger spread is named."""
    base = src.shape()
    if src.spread.most() <= 0:
        return []
    g_all, (dx, dy) = base.grown(*src.spread.sides()).gap(dst, limit=LINE + 0.01)
    if g_all >= LINE:  # all four sides at once (the box model: a corner can hold two sides' spread)
        return []
    out = []
    for i, (v, side) in enumerate(zip(src.spread.sides(), SIDES, strict=True)):
        if v <= 0:
            continue
        grow = [0.0, 0.0, 0.0, 0.0]
        grow[i] = v
        g, _ = base.grown(*grow).gap(dst, limit=LINE + 0.01)
        if g < LINE:
            out.append((v, side))
    if not out:  # only the corner region of two sides reaches: name the facing side with the larger spread
        out.append(src.spread.facing(dx, dy))
    return out


def pair_check(a: Placed, b: Placed) -> Check | None:
    """The pair rule (fix 2 of the plate-14 post-mortem). None when the pair is clear.

    - footprints closer than the line allowance (LINE, 1 mm): FAIL body, because their lines could cross (exit -101);
    - a part with a measured support mask (probe): its support cells closer than LINE to the other's model is a FAIL.
      The mask, not the four spreads, because support inside a U-shaped notch stays inside the box (all spreads 0)
      and still meets a part nested in the notch;
    - otherwise each side's spread, swept from the part's outline, closer than LINE to the other's model: its support
      could reach that model. Short against a probe or spec spread is a FAIL. Short against an estimate is a WARN: the
      slice decides.
    Each part is tested against the other's model only: Studio counts a crossing of two supports only past 100 mm
    (ConflictChecker.cpp:291-310), so the larger of the two spreads counts, not their sum."""
    sa, sb = a.shape(), b.shape()
    g, _ = sa.gap(sb, limit=max(a.spread.most(), b.spread.most()) + LINE + 0.01)
    if g < LINE:
        ra, rb = sa.bbox(), sb.bbox()
        iw, ih = min(ra[2], rb[2]) - max(ra[0], rb[0]), min(ra[3], rb[3]) - max(ra[1], rb[1])
        where_a = f"{a.key} (x {ra[0]:.1f}..{ra[2]:.1f}, y {ra[1]:.1f}..{ra[3]:.1f})"
        where_b = f"{b.key} (x {rb[0]:.1f}..{rb[2]:.1f}, y {rb[1]:.1f}..{rb[3]:.1f})"
        if g <= 0.0 and iw > 0 and ih > 0:
            text = f"{where_a} and {where_b} intersect by {iw:.1f} x {ih:.1f} mm. The parts themselves overlap: move one."
        else:
            text = (
                f"{where_a} and {where_b}: the outlines touch ({g:.1f} mm apart; the line allowance is {LINE:.1f} mm). "
                "Studio refuses crossing paths (exit -101): move one."
            )
        return Check(False, text, "body")
    masked: list[tuple[Placed, Placed, float]] = []
    reached: list[tuple[Placed, float, str]] = []
    for src, dst in ((a, b), (b, a)):
        m = src.mask_shape()
        if m is not None:
            gm, _ = m.gap(dst.shape(), limit=LINE + 0.01)
            if gm < LINE:
                masked.append((src, dst, gm))
            continue
        reached += [(src, v, side) for v, side in _side_reaches(src, dst.shape())]
    if not masked and not reached:
        return None
    g, _ = sa.gap(sb)  # the exact gap for the message, not the cut-off value above
    if masked:
        src, dst, gm = masked[0]
        return Check(
            False,
            f"{a.key} and {b.key} are {g:.1f} mm apart; {src.key}'s support (measured by a solo slice) comes within "
            f"{max(gm, 0.0):.1f} mm of {dst.key}: move them apart",
            "margin",
        )
    hard = [s for s in reached if s[0].spread.hard]
    if hard:
        p, f, sd = max(hard, key=lambda h: h[1])
        how = "measured by a solo slice" if p.spread.source == "probe" else "set in the spec"
        return Check(
            False,
            f"{a.key} and {b.key} are {g:.1f} mm apart; {p.key}'s support reaches {f:.1f} mm on {sd} ({how}), within the "
            f"{LINE:.1f} mm line allowance of {b.key if p is a else a.key}: move them apart",
            "margin",
        )
    reach = max(f for _, f, _ in reached)
    return Check(True, f"{a.key} and {b.key} are {g:.1f} mm apart; support reaches {reach:.1f} mm (estimated); the slice decides", "margin")


@dataclass
class Context:
    """What the rules need: machine facts, the filament->extruder map, TPU slots and extra limits. The prime tower
    of a plate is checked by `tower.TowerClear`, which build.py adds to the rule list."""

    facts: dict
    filament_extruder: dict[int, int] = field(default_factory=dict)
    tpu_filaments: set[int] = field(default_factory=set)
    aux_x_limit: float | None = None
    by_object: bool = False  # the plate prints one object at a time: head clearance and height limits apply
    skirt_extra: float = 0.0  # per-object skirt width (distance + loops x line), added to the by-object gap

    def sequential_gap(self, all_short: bool) -> float:
        """The bounding-box gap print-by-object needs (reference 08 section 2): Studio grows each hull by
        `0.5 * radius - 0.1` (or `0.5 * nozzle_height - 0.1` when every object is under `nozzle_height`), and a
        per-object skirt turns that into `max(d + skirt / 2, skirt)`. Two grown hulls may not touch, so the gap is
        twice the growth; the radius itself (not radius - 0.2) is used as the safe round-up."""
        c = self.clearance
        d = (c["nozzle_height"] if all_short else c["radius"]) / 2
        if self.skirt_extra > 0:
            d = max(d + self.skirt_extra / 2, self.skirt_extra)
        return 2 * d

    @property
    def clearance(self) -> dict:
        return {**DEFAULT_CLEARANCE, **self.facts.get("clearance", {})}

    def known(self, filament: int) -> bool:
        """A loaded filament slot. With no map (bare rule tests) every slot that names an extruder counts."""
        if self.filament_extruder:
            return filament in self.filament_extruder
        return any(e["index"] == filament for e in self.facts["extruders"])

    def extruder_of(self, filament: int) -> dict:
        exts = {e["index"]: e for e in self.facts["extruders"]}
        return exts.get(self.filament_extruder.get(filament, filament if filament in exts else 1), exts[1])


def extruder_x(e: Mapping, aux_x_limit: float | None = None) -> tuple[float, float]:
    """The x range extruder `e` reaches: its own, narrowed on the right by `aux_x_limit` for extruder 2. The one
    x-reach rule for ExtruderReach, the packers' bounds, filament groups, sub-parts and the tower area."""
    lo, hi = e["x"]
    if e.get("index") == 2 and aux_x_limit is not None:
        hi = min(hi, aux_x_limit)
    return lo, hi


def reach_bounds(p: Placed, ctx: Context) -> tuple[float, float]:
    """The smallest box min x (x0) and the largest box max x (x1) that keep every filament's prints inside its
    extruder's x reach, each filament with its own growth (`Placed.reach_growth`: an interface-only slot grows by the
    interface spread, not by the support spread). Only a reach narrower than the bed constrains (as ExtruderReach).
    The brim is not in it: a brim past the reach is clipped (a WARN)."""
    bed = ctx.facts["bed"]
    x0_min, x1_max = bed["x"][0], bed["x"][1]
    for f in p.filaments:
        if not ctx.known(f):
            continue
        lo, hi = extruder_x(ctx.extruder_of(f), ctx.aux_x_limit)
        g = p.reach_growth(f)[0]
        if lo > bed["x"][0]:
            x0_min = max(x0_min, lo + g[0])
        if hi < bed["x"][1]:
            x1_max = min(x1_max, hi - g[1])
    return x0_min, x1_max


class Rule(Protocol):
    def check(self, placed: list[Placed], ctx: Context) -> Iterable[Check]: ...


def _on_bed(placed: Iterable[Placed]) -> list[Placed]:
    return [p for p in placed if p.placed]


class Clearance:
    """Every pair of placed parts: see `pair_check`. It replaces the old box-plus-margins overlap rule, which summed
    two support margins (25 + 12 mm) and refused the plate-14 layout that printed (reference 08 section 11)."""

    def check(self, placed, ctx):
        ps = _on_bed(placed)
        for i, a in enumerate(ps):
            for b in ps[i + 1 :]:
                c = pair_check(a, b)
                if c is not None:
                    yield c


class InsideBed:
    """The body and any normal-support spread stay on the bed (FAIL). Tree spread does not count: Studio clips tree
    support to the bed border (TreeSupport.cpp:2507-2522). A brim that leaves the bed is a WARN: Studio clips the
    brim to the printable area (Brim.cpp:1011-1138)."""

    def check(self, placed, ctx):
        bed = ctx.facts["bed"]
        size = f"{bed['x'][1]:.0f} x {bed['y'][1]:.0f}"

        def off(r: Rect) -> bool:
            return r[0] < bed["x"][0] or r[2] > bed["x"][1] or r[1] < bed["y"][0] or r[3] > bed["y"][1]

        for p in _on_bed(placed):
            body = p.rect(with_margin=False)
            where = f"x {body[0]:.1f}..{body[2]:.1f} y {body[1]:.1f}..{body[3]:.1f}"
            if off(body):
                yield Check(False, f"{p.key} body {where} leaves the bed {size}", "bed")
                continue
            mx, px, my, py = p.bed_margin(with_brim=False)
            g = (body[0] - mx, body[1] - my, body[2] + px, body[3] + py)
            if off(g):
                yield Check(
                    False,
                    f"{p.key} body {where} is on the bed, but with its {p.spread.source} support spread {p.spread.describe()} "
                    f"it reaches x {g[0]:.1f}..{g[2]:.1f} y {g[1]:.1f}..{g[3]:.1f}: that leaves the bed {size}",
                    "margin",
                )
                continue
            m = p.brim
            gb = (g[0] - m, g[1] - m, g[2] + m, g[3] + m)
            if m > 0 and off(gb):
                clip = (max(gb[0], bed["x"][0]), max(gb[1], bed["y"][0]), min(gb[2], bed["x"][1]), min(gb[3], bed["y"][1]))
                yield Check(
                    True,
                    f"{p.key}: the {m:.1f} mm brim reaches x {gb[0]:.1f}..{gb[2]:.1f} y {gb[1]:.1f}..{gb[3]:.1f} and will be clipped "
                    f"at x {clip[0]:.1f}..{clip[2]:.1f} y {clip[1]:.1f}..{clip[3]:.1f} (the bed {size})",
                    "brim",
                )


class ExtruderReach:
    """Every extruder a part uses (its filament, height ranges, sub-parts, the process support and interface slots)
    must reach the whole part and its support. Only the sides where the extruder's reach is narrower than the bed are
    checked (InsideBed covers the rest). On those sides the support spread counts in full, trees too: a jaw's support
    reached 5.5 mm into the right nozzle's dead strip, exit -102 (reference 08 section 3.3b). A slot that prints only
    the support interface counts with the interface spread instead (`Placed.reach_growth`): the seed cradle at x 9.2
    exited -102 with the right nozzle's interface at x 14.3 (reference 06 section 5). One FAIL line per part and
    extruder, naming every side it crosses. A brim that crosses the reach is a WARN: Studio clips brims to each
    nozzle's printable area (Brim.cpp:1079-1095, 1129-1138)."""

    def check(self, placed, ctx):
        bed = ctx.facts["bed"]
        for p in placed:
            by_ext: dict[int, list[int]] = {}
            for fil in sorted(p.filaments):
                if not ctx.known(fil):
                    yield Check(False, f"{p.key} uses filament {fil}, which is not a loaded filament slot", "spool")
                    continue
                by_ext.setdefault(ctx.extruder_of(fil)["index"], []).append(fil)
            for fils in by_ext.values():
                yield from self._one(p, fils, ctx.extruder_of(fils[0]), ctx, bed)

    @staticmethod
    def _one(p: Placed, fils: list[int], e: dict, ctx: Context, bed: dict) -> Iterator[Check]:
        names = " and ".join(f"{f} as the {p.interface[f][1]}" if f in p.interface else str(f) for f in fils)
        if p.info.size[2] > e["z_max"]:
            yield Check(False, f"{p.key} is {p.info.size[2]:.1f} mm tall; extruder {e['index']} prints up to {e['z_max']:.0f} mm", "height")
        if not p.placed:
            return
        lo_x, hi_x = extruder_x(e, ctx.aux_x_limit)
        box = (p.x0, p.x1, p.y0, p.y1)
        sides = (
            ("-x", "x", 0, box[0], lo_x, lo_x > bed["x"][0], -1),
            ("+x", "x", 1, box[1], hi_x, hi_x < bed["x"][1], 1),
            ("-y", "y", 2, box[2], e["y"][0], e["y"][0] > bed["y"][0], -1),
            ("+y", "y", 3, box[3], e["y"][1], e["y"][1] < bed["y"][1], 1),
        )
        fails, limits, brims = [], [], []
        for side, axis, i, edge, limit, narrower, sign in sides:
            if not narrower:
                continue
            # The filament of this extruder whose prints reach farthest on this side
            grow, what = max((p.reach_growth(f) for f in fils), key=lambda r: r[0][i])
            at = edge + sign * grow[i]
            if sign * (at - limit) > 1e-9:
                fails.append(f"{axis} {at:.1f} on {side} with its {what} {grow[i]:.1f} mm")
                aux = side == "+x" and limit != e["x"][1]
                limits.append(
                    f"the configured aux_x_limit for the auxiliary nozzle is {limit} mm"
                    if aux
                    else f"that extruder reaches {axis} {e[axis][0]:.1f}..{e[axis][1]:.1f}"
                )
                continue
            m = max((p.brim for f in fils if f not in p.interface), default=0.0)  # the interface spread holds its brim
            s = max((p.reach_growth(f)[0][i] for f in fils if f not in p.interface), default=0.0)
            at_brim = edge + sign * (s + m)
            if m > 0 and sign * (at_brim - limit) > 1e-9:
                brims.append((axis, at_brim, side, limit))
        if fails:
            yield Check(
                False,
                f"{p.key} uses filament {names} (extruder {e['index']}, {e['type']}) and reaches "
                + " and ".join(fails)
                + "; "
                + "; ".join(dict.fromkeys(limits)),
                "reach",
            )
        elif brims:
            yield Check(
                True,
                f"{p.key}: the {p.brim:.1f} mm brim on filament {names} reaches "
                + " and ".join(f"{a} {at:.1f} on {sd}" for a, at, sd, _ in brims)
                + " and will be clipped at "
                + " and ".join(f"{a} {lim:.1f}" for a, _, _, lim in brims)
                + f" (extruder {e['index']} reach)",
                "brim",
            )


class TpuOnMain:
    def check(self, placed, ctx):
        for p in placed:
            for fil in sorted(p.filaments):
                if fil in ctx.tpu_filaments and ctx.extruder_of(fil)["index"] != 1:
                    yield Check(
                        False,
                        f"{p.key} uses TPU filament {fil} on extruder {ctx.extruder_of(fil)['index']}; TPU prints only through extruder 1 (main)",
                        "spool",
                    )


class MeshSane:
    def check(self, placed, ctx):
        for p in placed:
            if not p.info.manifold:
                yield Check(False, f"{p.key} is not manifold", "mesh")
            if max(p.info.size) < 1.0:
                yield Check(
                    False,
                    f"{p.key} is {p.info.size[0]:.3f} x {p.info.size[1]:.3f} x {p.info.size[2]:.3f} mm: probably a mesh in metres; "
                    f'add "orientation": "auto" to the part (Studio converts the unit first) or run '
                    f"`shambu rotate FILE -o DIR --convert-unit` for a mm copy",
                    "mesh",
                )
            if p.info.parts > 1:
                yield Check(True, f"{p.key}: the STL holds {p.info.parts} bodies; `shambu split` if they are separate parts", "mesh")
            if not p.info.on_bed():
                yield Check(True, f"{p.key}: STL min z is {p.info.min[2]:.2f}; the placement drops it onto the bed", "mesh")


class SequentialClearance:
    """Print-by-object (Studio 02.08, Print.cpp `sequential_print_clearance_valid`, reference 08):
    - the gap between two objects' convex hulls must be at least `radius - 0.2` mm (59.8 on the X2D); a bounding-box
      gap of `radius` is the safe over-estimate used here. If every object is shorter than `nozzle_height`, 3.8 mm
      (the outer nozzle) is enough.
    - objects print in list order. An object taller than `height_to_rod` (32.8) collides with the gantry rods
      if a LATER object shares its Y band (raw Y gap under `dist_to_rod`, 33 mm). Otherwise the limit is
      `height_to_lid` (133). The last object only has to fit under the printable height."""

    def check(self, placed, ctx):
        placed = _on_bed(placed)
        if not ctx.by_object or not placed:
            return
        c = ctx.clearance
        need = ctx.sequential_gap(all(p.info.size[2] < c["nozzle_height"] for p in placed))
        fails: list[Check] = []
        for i, a in enumerate(placed):
            for b in placed[i + 1 :]:
                g, _ = rect_gap(a.rect(with_margin=False), b.rect(with_margin=False))
                if g < need - 1e-6:
                    fails.append(
                        Check(
                            False,
                            f"{a.key} and {b.key} are {g:.1f} mm apart; print-by-object needs {need:.1f} mm between objects "
                            f"(extruder_clearance_max_radius {c['radius']:.0f})",
                            "sequence",
                        )
                    )
        for i, a in enumerate(placed):
            later = placed[i + 1 :]
            if not later:
                limit, why = ctx.facts.get("z_max", c["height_to_lid"]), "the printable height"
            elif any(min(abs(b.y0 - a.y1), abs(a.y0 - b.y1)) < c["dist_to_rod"] or (b.y0 < a.y1 and a.y0 < b.y1) for b in later):
                limit, why = c["height_to_rod"], f"the gantry rods: a later object shares its Y band (within {c['dist_to_rod']:.0f} mm)"
            else:
                limit, why = c["height_to_lid"], "the lid"
            if a.info.size[2] > limit:
                fails.append(
                    Check(
                        False,
                        f"{a.key} is {a.info.size[2]:.1f} mm tall; print-by-object allows {limit:.1f} mm here because of {why}",
                        "sequence",
                    )
                )
        yield from fails
        if not fails:
            yield Check(
                True,
                f"print-by-object: {len(placed)} object(s) in print order, {need:.1f} mm apart, heights within the gantry limits",
                "sequence",
            )


DEFAULT_RULES: tuple[Rule, ...] = (
    Clearance(),
    InsideBed(),
    ExtruderReach(),
    TpuOnMain(),
    MeshSane(),
    SequentialClearance(),
)


@dataclass
class LimitChecker:
    rules: tuple[Rule, ...] = DEFAULT_RULES

    def check(self, placed: list[Placed], ctx: Context) -> list[Check]:
        out: list[Check] = []
        for r in self.rules:
            out += list(r.check(placed, ctx))
        if not any(not c.ok for c in out) and all(p.placed for p in placed):  # no "fit" line beside a part with no position
            bed = ctx.facts["bed"]
            n = sum(1 for p in placed if p.placed)
            out.append(
                Check(True, f"{n} part(s) fit: bed {bed['x'][1]:.0f} x {bed['y'][1]:.0f}, every reach and height limit holds", "summary")
            )
        return out


# ---------------------------------------------------------------------------- placers
@dataclass
class Placement:
    """What a placer returns: the free parts split into plates (plate 1 first; later plates are virtual, and the spec
    must move those parts), and the parts it could not place at all, with the reason. Iterating a Placement walks
    its plates."""

    plates: list[list[Placed]] = field(default_factory=list)
    unplaced: dict[str, str] = field(default_factory=dict)

    def __iter__(self) -> Iterator[list[Placed]]:
        return iter(self.plates)

    def __len__(self) -> int:
        return len(self.plates)

    def __getitem__(self, i: int) -> list[Placed]:
        return self.plates[i]


class Placer(Protocol):
    """Sets x0/y0 on each free part and returns them split into plates. `x_min_of` / `x_max_of` give, per part key,
    the smallest box min x and the largest box max x its extruders allow, growth included (`reach_bounds`). `fixed` are the parts the spec placed with `at`:
    the placer keeps clear of them. A part that fits nowhere comes back in `Placement.unplaced`: a placer never
    raises for it. Two implementations: BottomLeftPacker and ShelfPacker."""

    def place(
        self,
        parts: list[Placed],
        obstacles: Sequence[Rect] = (),
        x_min_of: Mapping[str, float] | None = None,
        *,
        fixed: Sequence[Placed] = (),
        x_max_of: Mapping[str, float] | None = None,
    ) -> Placement: ...


def _unplace(p: Placed, reason: str, out: dict[str, str]) -> None:
    p.placed, p.why_not = False, reason
    out[p.key] = reason


def _too_big(
    p: Placed, x_range: tuple[float, float], y_range: tuple[float, float], m: tuple[float, float, float, float], what: str
) -> str | None:
    """The arithmetic for a part that cannot fit an empty plate with `what` (spread or keep-out) m (-x, +x, -y, +y)
    around it, else None."""
    for size, lo, hi, axis, word in ((p.h, m[2], m[3], y_range, "deep"), (p.w, m[0], m[1], x_range, "wide")):
        span = axis[1] - axis[0]
        if size + lo + hi > span + 1e-9:
            grow = f"2 x {lo:.1f} mm {what}" if abs(lo - hi) < 1e-9 else f"{lo:.1f} + {hi:.1f} mm {what}"
            return (
                f"{size:.1f} mm {word} + {grow} = {size + lo + hi:.1f} mm, the bed area is {span:.1f} mm {word} "
                f"({axis[0]:.1f}..{axis[1]:.1f}, inside the layout edges and the extruder reach)"
            )
    return None


@dataclass
class ShelfPacker:
    """Rows along x from the bottom left, deepest parts first. Returns one or more plates. Kept for print-by-object
    plates (`keep_order`, `solo_row_above`) and filament groups. It holds `Placed.keep_out` on every side, and keeps
    every part inside `x_min_of` / `x_max_of`."""

    x_range: tuple[float, float] = (EDGE, BED - EDGE)
    y_range: tuple[float, float] = (EDGE, BED - EDGE)
    gap: float = GAP
    keep_order: bool = False  # print-by-object: the list order is the print order, so no re-sorting by depth
    solo_row_above: float | None = None  # print-by-object: an object taller than this ends its row (nothing later shares its Y band)

    def place(
        self,
        parts: list[Placed],
        obstacles: Sequence[Rect] = (),
        x_min_of: Mapping[str, float] | None = None,
        *,
        fixed: Sequence[Placed] = (),
        x_max_of: Mapping[str, float] | None = None,
    ) -> Placement:
        x_min, x_max = dict(x_min_of or {}), dict(x_max_of or {})

        def lo(p: Placed) -> float:  # the left edge of the keep-out rect: x0 - keep_out
            return max(self.x_range[0], x_min.get(p.key, -math.inf) - p.keep_out)

        def hi(p: Placed) -> float:
            return min(self.x_range[1], x_max.get(p.key, math.inf) + p.keep_out)

        obstacles = list(obstacles) + [f.rect() for f in fixed]
        unplaced: dict[str, str] = {}
        fits = []
        for p in parts:
            k = p.keep_out
            why = _too_big(p, (lo(p), hi(p)), self.y_range, (k, k, k, k), "keep-out")
            if why:
                _unplace(p, f"the row packer could not place {p.key}: {why}", unplaced)
            else:
                fits.append(p)
        left = list(fits) if self.keep_order else sorted(fits, key=lambda p: (-(p.h + 2 * p.keep_out), -(p.w + 2 * p.keep_out)))
        plates: list[list[Placed]] = []
        plate: list[Placed] = []
        y = self.y_range[0]
        while left:
            row = [left.pop(0)]
            x_after = lo(row[0]) + row[0].w + 2 * row[0].keep_out + self.gap
            closed = self.solo_row_above is not None and row[0].info.size[2] > self.solo_row_above
            for o in list(left):
                if closed:
                    break
                start = max(x_after, lo(o))
                if start + o.w + 2 * o.keep_out <= hi(o):
                    row.append(o)
                    left.remove(o)
                    x_after = start + o.w + 2 * o.keep_out + self.gap
                    closed = self.solo_row_above is not None and o.info.size[2] > self.solo_row_above
                elif self.keep_order:
                    break  # the next object in print order starts the next row
            row_h = max(r.h + 2 * r.keep_out for r in row)
            if y + row_h > self.y_range[1] and plate:
                plates.append(plate)
                plate, y = [], self.y_range[0]
            x = self.x_range[0]
            for o in row:
                o.x0, o.y0 = max(x, lo(o)) + o.keep_out, y + o.keep_out
                for ob in obstacles:
                    while overlaps(o.rect(), ob) and o.x1 + o.keep_out <= hi(o):
                        o.x0 = ob[2] + self.gap + o.keep_out
                if o.x1 + o.keep_out > hi(o) + 1e-9 or any(overlaps(o.rect(), ob) for ob in obstacles):
                    _unplace(
                        o,
                        f"the row packer could not place {o.key}: its row has no room for {o.w:.1f} mm + 2 x {o.keep_out:.1f} mm "
                        f"keep-out between the obstacles and x {hi(o):.1f} (the right limit of its extruder reach)",
                        unplaced,
                    )
                    continue
                x = o.x1 + o.keep_out + self.gap
                plate.append(o)
            y += row_h + self.gap
        if plate:
            plates.append(plate)
        return Placement(plates, unplaced)


CORNER_STEP = 1.0  # mm: run ends that move less than this between two rows are no corner (a curved wall)
CORNER_CAP = 48  # candidates per axis from one part's corners; more are thinned evenly


def _thin(vals: set[float], cap: int = CORNER_CAP) -> list[float]:
    out = sorted({round(v, 1) for v in vals})
    if len(out) <= cap:
        return out
    return [out[round(i * (len(out) - 1) / (cap - 1))] for i in range(cap)]


def _corners(shape: Footprint) -> tuple[set[float], set[float], set[float], set[float]]:
    """The internal corners of a placed outline (bed coordinates): run ends where two neighbouring rows differ by
    CORNER_STEP or more, and those rows' edges. Returns (right ends, left ends, row tops, row bottoms); a box
    outline gives its own edges."""
    if not isinstance(shape, _Bands):
        box = shape.bbox()
        return {box[2]}, {box[0]}, {box[3]}, {box[1]}
    rights, lefts, tops, bottoms = {shape.bbox()[2]}, {shape.bbox()[0]}, {shape.bbox()[3]}, {shape.bbox()[1]}
    rows = shape.rows
    for k in range(len(rows) - 1):
        (_, y1, ra), (y0n, _, rb) = rows[k], rows[k + 1]
        same = len(ra) == len(rb) and all(
            abs(a0 - b0) < CORNER_STEP and abs(a1 - b1) < CORNER_STEP for (a0, a1), (b0, b1) in zip(ra, rb, strict=True)
        )
        if same:
            continue
        tops.add(y1 + shape.oy)
        bottoms.add(y0n + shape.oy)
        for a, b in (*ra, *rb):
            lefts.add(a + shape.ox)
            rights.add(b + shape.ox)
    return rights, lefts, tops, bottoms


@dataclass
class BottomLeftPacker:
    """Candidate points, bottom left first, for by-layer plates (fix 4 of the plate-14 post-mortem). 0 degrees only.

    Parts go largest box area first. The candidates for a part are the bed corner (inside the layout edge and the
    part's bed margin) and, for every part already on the plate, its edges and the internal corners of its outline
    (a U's pocket), moved out by the larger facing spread + LINE + `gap` (only `gap` when neither side has support).
    They are tried in score order (the lowest top edge, then the lowest right edge), and the first legal one wins:
    `pair_check` finds nothing (no WARN either) against every part on the plate and every fixed part, the part with
    its brim (and normal-support spread) stays inside the layout edges, its spread stays inside the x reach of its
    extruders, and it keeps `gap` from every obstacle. First fit over plates, so the objective is the fewest plates.
    Plate 1 may hold only fixed parts: its index stays, so a part pushed to plate 2 is reported as such. On the five
    plate-14 parts it uses 1 plate where the row packer needed 2 (reference 08 section 11.5)."""

    x_range: tuple[float, float] = (EDGE, BED - EDGE)
    y_range: tuple[float, float] = (EDGE, BED - EDGE)
    gap: float = GAP

    def place(
        self,
        parts: list[Placed],
        obstacles: Sequence[Rect] = (),
        x_min_of: Mapping[str, float] | None = None,
        *,
        fixed: Sequence[Placed] = (),
        x_max_of: Mapping[str, float] | None = None,
    ) -> Placement:
        lo_of, hi_of = dict(x_min_of or {}), dict(x_max_of or {})
        unplaced: dict[str, str] = {}
        plates: list[list[Placed]] = [[]]
        for p in sorted(parts, key=lambda q: -(q.w * q.h)):
            why = _too_big(p, self.x_range, self.y_range, p.bed_margin(with_brim=False), "spread")  # a brim may be clipped
            if why:
                _unplace(p, f"the packer could not place {p.key}: {why}", unplaced)
                continue
            done = False
            for i, plate in enumerate(plates):
                others = plate + list(fixed) if i == 0 else plate
                if self._put(p, others, obstacles, lo_of.get(p.key), hi_of.get(p.key)):
                    plate.append(p)
                    done = True
                    break
            if done:
                continue
            if self._put(p, [], obstacles, lo_of.get(p.key), hi_of.get(p.key)):
                plates.append([p])
            else:
                lo, hi = lo_of.get(p.key, self.x_range[0]), hi_of.get(p.key, self.x_range[1])
                _unplace(
                    p,
                    f"the packer could not place {p.key}: no position on an empty plate keeps it with its brim and spread "
                    f"({p.margin_text()}) inside the bed area and its extruder reach (x {lo:.1f}..{hi:.1f})",
                    unplaced,
                )
        while len(plates) > 1 and not plates[-1]:
            plates.pop()
        return Placement(plates if any(plates) else [], unplaced)

    def _step(self, spread: float) -> float:
        """Distance from a placed part's edge to the next candidate: spread + LINE + gap, or only the layout gap (at
        least LINE) when neither facing side has support, so unsupported parts sit `gap` apart as with the row packer."""
        return spread + LINE + self.gap if spread > 0 else max(self.gap, LINE)

    def _put(self, p: Placed, others: Sequence[Placed], obstacles: Sequence[Rect], x_lo: float | None, x_hi: float | None) -> bool:
        """A position with the whole brim inside the bed and the reach if one exists, else one where only the brim is
        clipped (Studio clips brims, Brim.cpp:1011-1138; the rules then give a WARN brim line)."""
        return self._put_once(p, others, obstacles, x_lo, x_hi, True) or (
            p.brim > 0 and self._put_once(p, others, obstacles, x_lo, x_hi, False)
        )

    def _put_once(
        self, p: Placed, others: Sequence[Placed], obstacles: Sequence[Rect], x_lo: float | None, x_hi: float | None, brim: bool
    ) -> bool:
        bm = p.bed_margin(with_brim=brim)
        s = p.spread
        m = p.brim if brim else 0.0
        lo_x = self.x_range[0] + bm[0]
        if x_lo is not None and x_lo > self.x_range[0]:  # reach_bounds: every filament's growth is in it already
            lo_x = max(lo_x, x_lo + m)
        hi_x = self.x_range[1] - bm[1]
        if x_hi is not None and x_hi < self.x_range[1]:
            hi_x = min(hi_x, x_hi - m)
        lo_y, hi_y = self.y_range[0] + bm[2], self.y_range[1] - bm[3]
        xs: set[float] = {lo_x}
        ys: set[float] = {lo_y}
        for q in others:
            rights, lefts, tops, bottoms = _corners(q.shape())
            sx_r, sx_l = self._step(max(q.spread.px, s.mx)), self._step(max(q.spread.mx, s.px))
            sy_t, sy_b = self._step(max(q.spread.py, s.my)), self._step(max(q.spread.my, s.py))
            xs |= set(_thin({q.x0} | {r + sx_r for r in rights} | {a - sx_l - p.w for a in lefts}))
            ys |= set(_thin({q.y0} | {t + sy_t for t in tops} | {b - sy_b - p.h for b in bottoms}))
        for ob in obstacles:
            xs.add(ob[2] + self.gap + p.brim + s.mx)
            ys.add(ob[3] + self.gap + p.brim + s.my)
        grown = [(ob[0] - self.gap, ob[1] - self.gap, ob[2] + self.gap, ob[3] + self.gap) for ob in obstacles]
        cands = sorted(
            (y + p.h, x + p.w, x, y)
            for y in ys
            if lo_y - 1e-9 <= y and y + p.h <= hi_y + 1e-9
            for x in xs
            if lo_x - 1e-9 <= x and x + p.w <= hi_x + 1e-9
        )
        for _, _, x, y in cands:
            p.x0, p.y0 = x, y
            if any(overlaps(p.rect(), ob) for ob in grown):
                continue
            if any(pair_check(p, q) is not None for q in others):
                continue
            p.placed, p.why_not = True, ""
            return True
        return False


def describe(placed: list[Placed]) -> list[str]:
    out = []
    for p in placed:
        fil = "+".join(str(f) for f in sorted(p.filaments))
        size = f"{p.w:.1f} x {p.h:.1f} x {p.info.size[2]:.1f} mm"
        if not p.placed:
            why = f" ({p.why_not})" if p.why_not.startswith("would need plate") else ""
            out.append(f"{p.key:28s} filament {fil}  not placed{why}  ({size}; {p.margin_text()})")
            continue
        support = f"  support {p.spread.describe()} ({p.spread.note or p.spread.source})" if p.spread.most() > 0 else ""
        out.append(
            f"{p.key:28s} filament {fil}  bbox x {p.x0:6.1f}..{p.x1:6.1f}  y {p.y0:6.1f}..{p.y1:6.1f}  "
            f"z 0..{p.info.size[2]:5.1f}  ({size}){support}"
        )
    return out
