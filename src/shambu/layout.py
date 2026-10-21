"""Place parts on the bed with known positions and check every machine limit before the CLI runs.

Positions are computed here, never by Bambu's arrange, so the report can state where every part
sits. The CLI's pos_x/pos_y/pos_z are a translation added to the STL's own coordinates, so a part
whose bounding box should start at (x0, y0) on the bed gets pos = (x0 - min_x, y0 - min_y, -min_z).

Design: `Placer` is a Strategy (`BottomLeftPacker` for by-layer plates, `ShelfPacker` for print-by-object order and
filament groups). A placer never raises for a part that does not fit: it returns that part in `Placement.unplaced`
with the arithmetic, and the geometric rules skip it. `Footprint` is a Strategy too: `RasterFootprint` (the mesh's XY
silhouette after the orientation) or `BoxFootprint` (the bounding box). Limit checks are a list of `Rule`
strategies run by `LimitChecker`, so a new printer fact is one more rule.

What Studio enforces, by layer (Bambu Studio 02.08.02.61 source, reference 08 section 11):
- no clearance between objects before slicing (`Print.cpp:944-1050` checks hulls against the bed exclude area only);
- after slicing, exit -101 when extrusion centrelines of two objects cross in one layer (`ConflictChecker.cpp:203-314`);
  brims and skirts are not in that test, and brims of different objects are clipped against each other
  (`Brim.cpp:258-365`);
- tree supports grow per object, against that object's outlines and the bed border only
  (`TreeSupport3D.cpp:5232-5246`, `TreeSupport.cpp:2507-2522`): they do not avoid a neighbour, and they never leave
  the bed.
So the pair rule (`Clearance`) holds the support spread between models, not the brim, and takes the larger of the two
facing spreads, not their sum. An estimated spread that is short is a WARN: the slice (exit -101) decides.

Not here: Studio's own arrange, rotation trials, z-aware support (the spread is the widest band, z 0..5).
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol

from .errors import LayoutError
from .studio import MeshInfo

BED = 256.0
EDGE = 2.0
GAP = 5.0
WIPE_TOWER = (15.0, 220.0, 35.0, 35.0)  # x, y, w, h reserved when a plate uses two filaments (CLI default position)
# Two models may touch (no support, gap 0 sliced with exit 0) but their lines may not cross (exit -101); 1.0 mm is two
# line widths (reference 08 section 11.4, live clash test).
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

    def toward(self, dx: float, dy: float) -> float:
        return self.facing(dx, dy)[0]

    def turned(self, quarter_turns: int) -> Spread:
        """The spread after the part turns by quarter_turns x +90 degrees about z: (mx, px, my, py) -> (py, my, mx, px)."""
        s = self
        for _ in range(quarter_turns % 4):
            s = replace(s, mx=s.py, px=s.my, my=s.mx, py=s.px)
        return s

    def describe(self) -> str:
        v = self.sides()
        if max(v) == min(v):
            return f"{v[0]:.1f} mm each side"
        return ", ".join(f"{n} {x:.1f}" for n, x in zip(SIDES, v, strict=True)) + " mm"


NO_SPREAD = Spread()


def _supported(p: Mapping) -> bool:
    return str(p.get("enable_support", "0")) in ("1", "true")


def is_tree(p: Mapping) -> bool:
    """The params ask for tree support (Studio's default support_type is tree(auto) on the X2D presets)."""
    return str(p.get("support_type", "tree(auto)")).startswith("tree")


def support_spread(params: Mapping | None, height: float, probe: Spread | None = None) -> Spread:
    """How far a part's support reaches past its bounding box, per side.

    No support: nothing. A probe (a solo slice of the part, measured per side): returned as it is. Otherwise an
    estimate on all four sides, because the mesh does not tell which side the trees grow on (reference 08 section
    11.2: the housing's -y and +y overhangs had 0.3 and 2.9 mm of spread, its -x overhang 20.9 mm):
    tree `min(22, max(8, 0.10 x height))`, normal 5 mm."""
    p = params or {}
    if not _supported(p):
        return NO_SPREAD
    if probe is not None:
        return probe
    if is_tree(p):
        raw = TREE_SPREAD_PER_MM * height
        s = min(TREE_SPREAD_CAP, max(TREE_SPREAD_FLOOR, raw))
        if raw < TREE_SPREAD_FLOOR:
            note = f"support {s:.1f} mm, the tree minimum ({TREE_SPREAD_PER_MM:.2f} x {height:.1f} mm height is less), estimated"
        elif raw > TREE_SPREAD_CAP:
            note = f"support {s:.1f} mm, the tree cap ({TREE_SPREAD_PER_MM:.2f} x {height:.1f} mm height is more), estimated"
        else:
            note = f"support {TREE_SPREAD_PER_MM:.2f} x {height:.1f} mm height, estimated"
        return Spread(s, s, s, s, "estimate", True, note)
    return Spread(NORMAL_SPREAD, NORMAL_SPREAD, NORMAL_SPREAD, NORMAL_SPREAD, "estimate", False, "normal support, estimated")


def spec_spread(margin: float) -> Spread:
    """The part's `brim_margin` from the spec: it replaces brim and support margin together, on every side, and at the
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

    def area(self) -> float: ...

    def gap(self, other: Footprint, limit: float = math.inf) -> tuple[float, tuple[float, float]]:
        """Shortest distance to `other` and its direction (dx, dy) from self to other. 0 when they meet. A result at or
        above `limit` is only a lower bound."""
        ...


@dataclass(frozen=True)
class _Bands:
    """Shared gap logic: bands in a local frame (bbox minimum at 0, 0) plus an offset on the bed."""

    rows: tuple[Band, ...]
    w: float
    h: float
    ox: float = 0.0
    oy: float = 0.0

    def bbox(self) -> Rect:
        return (self.ox, self.oy, self.ox + self.w, self.oy + self.h)

    def translated(self, dx: float, dy: float) -> Footprint:
        return replace(self, ox=self.ox + dx, oy=self.oy + dy)

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


# ---------------------------------------------------------------------------- placed parts
@dataclass
class Placed:
    """A part with its mesh info and where its bounding box sits on the bed.

    `margin` is the brim (or nothing when the spec's brim_margin is the spread): it counts at the bed edge and in the
    extruder reach, never between two parts (Studio clips brims against neighbours, Brim.cpp:258-365). `spread` is the
    support reach past the box per side. `footprint` is the outline in the part's own frame (bbox minimum at 0, 0);
    None means the box. `placed` is False when the placer found no position: no geometric rule runs on the part, and
    `why_not` says why."""

    key: str
    info: MeshInfo
    filament: int
    x0: float = 0.0
    y0: float = 0.0
    margin: float = 0.0
    fixed: bool = False  # placed by the spec, not by the packer
    also_filaments: frozenset[int] = frozenset()  # filaments used by overrides, height ranges or sub-parts
    spread: Spread = NO_SPREAD
    footprint: Footprint | None = None
    placed: bool = True
    why_not: str = ""

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

    @property
    def reserve(self) -> float:
        """One margin for every side, for the row packer: the larger of brim and support spread."""
        return max(self.margin, self.spread.most())

    def bed_margin(self) -> tuple[float, float, float, float]:
        """Growth per side (-x, +x, -y, +y) that must stay on the bed: the brim, plus the support spread unless it is
        tree support (Studio clips trees to the bed border, TreeSupport.cpp:2507-2522)."""
        s = (0.0, 0.0, 0.0, 0.0) if self.spread.tree else self.spread.sides()
        return (self.margin + s[0], self.margin + s[1], self.margin + s[2], self.margin + s[3])

    def rect(self, with_margin: bool = True) -> Rect:
        """The bounding box, grown by brim plus support spread on each side when `with_margin`."""
        if not with_margin:
            return (self.x0, self.y0, self.x1, self.y1)
        m, s = self.margin, self.spread
        return (self.x0 - m - s.mx, self.y0 - m - s.my, self.x1 + m + s.px, self.y1 + m + s.py)

    def shape(self) -> Footprint:
        fp = self.footprint if self.footprint is not None else BoxFootprint.of(self.w, self.h)
        return fp.translated(self.x0, self.y0)

    def pos(self) -> tuple[float, float, float]:
        """Translation for the assemble list: bbox starts at (x0, y0) and the part sits on the bed."""
        return (self.x0 - self.info.min[0], self.y0 - self.info.min[1], -self.info.min[2])

    def margin_text(self) -> str:
        """Where the margins come from, for a report line: `margin 8.0 mm = support 0.10 x 64.0 mm height, estimated`."""
        parts = []
        if self.spread.most() > 0:
            v = self.spread.describe() if self.spread.source != "estimate" else f"{self.spread.most():.1f} mm"
            parts.append(f"margin {v} = {self.spread.note or self.spread.source}")
        if self.margin > 0:
            parts.append(f"brim {self.margin:.1f} mm")
        return "; ".join(parts) or "no margin"


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
    """One line of the plate's check. `kind` sorts it: body (the parts themselves meet), margin (a support or brim
    margin, not the bodies), placement (the packer could not place a part), bed, reach, height, spool, tower,
    sequence, mesh, summary. An ok Check of kind margin is a WARN."""

    ok: bool
    text: str
    kind: str = ""

    @property
    def warn(self) -> bool:
        return self.ok and self.kind == "margin"


def pair_check(a: Placed, b: Placed) -> Check | None:
    """The pair rule (fix 2 of the plate-14 post-mortem). None when the pair is clear.

    - footprints closer than LINE (1 mm): FAIL body, because their lines would cross (exit -101);
    - closer than max(a's spread toward b, b's spread toward a) + LINE: the support of one could reach the other's
      model. A probe or spec spread that is short is a FAIL. An estimate that is short is a WARN: the slice decides.
    Support-on-support crossings do not fail Studio (ConflictChecker.cpp:291, 306-311), so the max counts, not the sum."""
    sa, sb = a.shape(), b.shape()
    g, (dx, dy) = sa.gap(sb, limit=max(a.spread.most(), b.spread.most()) + LINE + 0.01)
    if g < LINE:
        ra, rb = sa.bbox(), sb.bbox()
        iw, ih = min(ra[2], rb[2]) - max(ra[0], rb[0]), min(ra[3], rb[3]) - max(ra[1], rb[1])
        where_a = f"{a.key} (x {ra[0]:.1f}..{ra[2]:.1f}, y {ra[1]:.1f}..{ra[3]:.1f})"
        where_b = f"{b.key} (x {rb[0]:.1f}..{rb[2]:.1f}, y {rb[1]:.1f}..{rb[3]:.1f})"
        if g <= 0.0 and iw > 0 and ih > 0:
            text = f"{where_a} and {where_b} intersect by {iw:.1f} x {ih:.1f} mm. The parts themselves overlap: move one."
        else:
            text = (
                f"{where_a} and {where_b}: the footprints touch ({g:.1f} mm apart, {LINE:.1f} mm needed). "
                "Studio refuses crossing paths (exit -101): move one."
            )
        return Check(False, text, "body")
    fa, side_a = a.spread.facing(dx, dy)
    fb, side_b = b.spread.facing(-dx, -dy)
    need = max(fa, fb) + LINE
    if g >= need:
        return None
    hard = [(p, f, sd) for p, f, sd in ((a, fa, side_a), (b, fb, side_b)) if p.spread.hard and g < f + LINE]
    if hard:
        p, f, sd = max(hard, key=lambda h: h[1])
        how = "measured by a solo slice" if p.spread.source == "probe" else "set in the spec"
        return Check(
            False,
            f"{a.key} and {b.key} are {g:.1f} mm apart; {p.key}'s support reaches {f:.1f} mm on {sd} ({how}); "
            f"they need {f + LINE:.1f} mm: move them apart",
            "margin",
        )
    return Check(
        True, f"{a.key} and {b.key} are {g:.1f} mm apart; support reaches {need - LINE:.1f} mm (estimated); the slice decides", "margin"
    )


@dataclass
class Context:
    """What the rules need: machine facts, the filament->extruder map, TPU slots, extra limits, and the
    prime-tower reserve of the plate being checked (None when the plate uses one filament)."""

    facts: dict
    filament_extruder: dict[int, int] = field(default_factory=dict)
    tpu_filaments: set[int] = field(default_factory=set)
    aux_x_limit: float | None = None
    reserve: Rect | None = None
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
    """The body, its brim and any normal-support spread stay on the bed. Tree spread does not count: Studio clips
    tree support to the bed border (TreeSupport.cpp:2507-2522)."""

    def check(self, placed, ctx):
        bed = ctx.facts["bed"]
        size = f"{bed['x'][1]:.0f} x {bed['y'][1]:.0f}"
        for p in _on_bed(placed):
            body = p.rect(with_margin=False)
            where = f"x {body[0]:.1f}..{body[2]:.1f} y {body[1]:.1f}..{body[3]:.1f}"
            if body[0] < bed["x"][0] or body[2] > bed["x"][1] or body[1] < bed["y"][0] or body[3] > bed["y"][1]:
                yield Check(False, f"{p.key} body {where} leaves the bed {size}", "bed")
                continue
            mx, px, my, py = p.bed_margin()
            g = (body[0] - mx, body[1] - my, body[2] + px, body[3] + py)
            if g[0] < bed["x"][0] or g[2] > bed["x"][1] or g[1] < bed["y"][0] or g[3] > bed["y"][1]:
                what = f"brim {p.margin:.1f} mm" if p.margin else "no brim"
                if not p.spread.tree and p.spread.most() > 0:
                    what += f" and {p.spread.source} margin {p.spread.describe()}"
                yield Check(
                    False,
                    f"{p.key} body {where} is on the bed, but with its {what} it reaches x {g[0]:.1f}..{g[2]:.1f} "
                    f"y {g[1]:.1f}..{g[3]:.1f}: that leaves the bed {size}",
                    "margin",
                )


class ReserveClear:
    """Nothing (packed or fixed) may cover the prime-tower reserve of a plate that uses two or more filaments."""

    def check(self, placed, ctx):
        if ctx.reserve is None:
            return
        for p in _on_bed(placed):
            if overlaps(p.rect(with_margin=True), ctx.reserve):
                r = ctx.reserve
                yield Check(
                    False,
                    f"{p.key} overlaps the prime-tower reserve x {r[0]:.0f}..{r[2]:.0f} y {r[1]:.0f}..{r[3]:.0f} "
                    f"(the plate uses more than one filament)",
                    "tower",
                )


class ExtruderReach:
    """Every extruder a part uses (its filament, height ranges, sub-parts) must reach the whole part with its brim
    and support. Only the sides where the extruder's reach is narrower than the bed are checked (InsideBed covers
    the rest). On those sides the support spread counts in full, trees too: a jaw's support reached 5.5 mm into the
    right nozzle's dead strip, exit -102 (reference 08 section 3.3b)."""

    def check(self, placed, ctx):
        bed = ctx.facts["bed"]
        for p in placed:
            for fil in sorted(p.filaments):
                if not ctx.known(fil):
                    yield Check(False, f"{p.key} uses filament {fil}, which is not a loaded filament slot", "spool")
                    continue
                e = ctx.extruder_of(fil)
                if p.info.size[2] > e["z_max"]:
                    yield Check(
                        False, f"{p.key} is {p.info.size[2]:.1f} mm tall; extruder {e['index']} prints up to {e['z_max']:.0f} mm", "height"
                    )
                if not p.placed:
                    continue
                s, m = p.spread, p.margin
                g = (p.x0 - m - s.mx, p.y0 - m - s.my, p.x1 + m + s.px, p.y1 + m + s.py)
                limits = (
                    ("-x", g[0] < e["x"][0] and e["x"][0] > bed["x"][0], s.mx, "x", g[0]),
                    ("+x", g[2] > e["x"][1] and e["x"][1] < bed["x"][1], s.px, "x", g[2]),
                    ("-y", g[1] < e["y"][0] and e["y"][0] > bed["y"][0], s.my, "y", g[1]),
                    ("+y", g[3] > e["y"][1] and e["y"][1] < bed["y"][1], s.py, "y", g[3]),
                )
                for side, bad, sv, axis, at in limits:
                    if bad:
                        yield Check(
                            False,
                            f"{p.key} uses filament {fil} (extruder {e['index']}, {e['type']}) and reaches {axis} {at:.1f} on {side} "
                            f"with its brim {m:.1f} mm and support {sv:.1f} mm; that extruder reaches {axis} "
                            f"{e[axis][0]:.1f}..{e[axis][1]:.1f}",
                            "reach",
                        )
                if e["index"] == 2 and ctx.aux_x_limit is not None and g[2] > ctx.aux_x_limit:
                    yield Check(
                        False,
                        f"{p.key} uses extruder 2 and ends at x {g[2]:.1f} with its brim {m:.1f} mm and support {s.px:.1f} mm on +x; "
                        f"the configured aux_x_limit for the auxiliary nozzle is {ctx.aux_x_limit} mm",
                        "reach",
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


def gap_between(a: Rect, b: Rect) -> float:
    """Shortest axis-aligned distance between two rectangles (0 when they touch or overlap)."""
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return max(dx, dy) if (dx == 0 or dy == 0) else (dx * dx + dy * dy) ** 0.5


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
                g = gap_between(a.rect(with_margin=False), b.rect(with_margin=False))
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
    ReserveClear(),
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
        if not any(not c.ok for c in out):
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
    stricter x limits (the reach of the extruders the part uses). `fixed` are the parts the spec placed with `at`:
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


def _too_big(p: Placed, x_range: tuple[float, float], y_range: tuple[float, float], m: tuple[float, float, float, float]) -> str | None:
    """The arithmetic for a part that cannot fit an empty plate with margins m (-x, +x, -y, +y), else None."""
    for size, lo, hi, axis, word in ((p.h, m[2], m[3], y_range, "deep"), (p.w, m[0], m[1], x_range, "wide")):
        span = axis[1] - axis[0]
        if size + lo + hi > span + 1e-9:
            marg = f"2 x {lo:.1f} mm margin" if abs(lo - hi) < 1e-9 else f"{lo:.1f} + {hi:.1f} mm margin"
            return (
                f"{size:.1f} mm {word} + {marg} = {size + lo + hi:.1f} mm, the bed area is {span:.1f} mm {word} "
                f"({axis[0]:.1f}..{axis[1]:.1f}, inside the layout edges)"
            )
    return None


@dataclass
class ShelfPacker:
    """Rows along x from the bottom left, deepest parts first. Returns one or more plates. Kept for print-by-object
    plates (`keep_order`, `solo_row_above`) and filament groups. It holds `Placed.reserve` on every side."""

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
        x_min = dict(x_min_of or {})
        obstacles = list(obstacles) + [f.rect() for f in fixed]
        unplaced: dict[str, str] = {}
        fits = []
        for p in parts:
            m = p.reserve
            why = _too_big(p, (max(self.x_range[0], x_min.get(p.key, self.x_range[0])), self.x_range[1]), self.y_range, (m, m, m, m))
            if why:
                _unplace(p, f"the row packer could not place {p.key}: {why}", unplaced)
            else:
                fits.append(p)
        left = list(fits) if self.keep_order else sorted(fits, key=lambda p: (-(p.h + 2 * p.reserve), -(p.w + 2 * p.reserve)))
        plates: list[list[Placed]] = []
        plate: list[Placed] = []
        y = self.y_range[0]
        while left:
            row = [left.pop(0)]
            x_after = max(self.x_range[0], x_min.get(row[0].key, self.x_range[0])) + row[0].w + 2 * row[0].reserve + self.gap
            closed = self.solo_row_above is not None and row[0].info.size[2] > self.solo_row_above
            for o in list(left):
                if closed:
                    break
                start = max(x_after, x_min.get(o.key, self.x_range[0]))
                if start + o.w + 2 * o.reserve <= self.x_range[1]:
                    row.append(o)
                    left.remove(o)
                    x_after = start + o.w + 2 * o.reserve + self.gap
                    closed = self.solo_row_above is not None and o.info.size[2] > self.solo_row_above
                elif self.keep_order:
                    break  # the next object in print order starts the next row
            row_h = max(r.h + 2 * r.reserve for r in row)
            if y + row_h > self.y_range[1] and plate:
                plates.append(plate)
                plate, y = [], self.y_range[0]
            x = self.x_range[0]
            for o in row:
                o.x0, o.y0 = max(x, x_min.get(o.key, self.x_range[0])) + o.reserve, y + o.reserve
                for ob in obstacles:
                    while overlaps(o.rect(), ob) and o.x1 + o.reserve <= self.x_range[1]:
                        o.x0 = ob[2] + self.gap + o.reserve
                x = o.x1 + o.reserve + self.gap
                plate.append(o)
            y += row_h + self.gap
        if plate:
            plates.append(plate)
        return Placement(plates, unplaced)


@dataclass
class BottomLeftPacker:
    """Candidate points, bottom left first, for by-layer plates (fix 4 of the plate-14 post-mortem). 0 degrees only.

    Parts go largest box area first. The candidates for a part are the bed corner (inside the layout edge and the
    part's bed margin) and, for every part already on the plate, its left and bottom edges and its right and top
    edges moved out by the larger facing spread + LINE + `gap` (only `gap` when neither side has support). A
    candidate is legal when `pair_check` finds nothing
    (no WARN either) against every part on the plate and every fixed part, the part with its brim (and normal-support
    spread) stays inside the layout edges, its brim and spread stay inside the x reach of its extruders, and it keeps
    `gap` from every obstacle. Score: the lowest top edge, then the lowest right edge. First fit over plates, so the
    objective is the fewest plates. On the five plate-14 parts it uses 1 plate where the row packer needed 2
    (reference 08 section 11.5)."""

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
            why = _too_big(p, self.x_range, self.y_range, p.bed_margin())
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
                    f"the packer could not place {p.key}: no position on an empty plate keeps it with its margins "
                    f"({p.margin_text()}) inside the bed area and its extruder reach (x {lo:.1f}..{hi:.1f})",
                    unplaced,
                )
        return Placement([pl for pl in plates if pl], unplaced)

    def _step(self, spread: float) -> float:
        """Distance from a placed part's edge to the next candidate: spread + LINE + gap, or only the layout gap (at
        least LINE) when neither facing side has support, so unsupported parts sit `gap` apart as with the row packer."""
        return spread + LINE + self.gap if spread > 0 else max(self.gap, LINE)

    def _put(self, p: Placed, others: Sequence[Placed], obstacles: Sequence[Rect], x_lo: float | None, x_hi: float | None) -> bool:
        bm = p.bed_margin()
        s = p.spread
        lo_x = self.x_range[0] + bm[0]
        if x_lo is not None and x_lo > self.x_range[0]:  # a reach narrower than the bed: brim and all spread inside it
            lo_x = max(lo_x, x_lo + p.margin + s.mx)
        hi_x = self.x_range[1] - bm[1]
        if x_hi is not None and x_hi < self.x_range[1]:
            hi_x = min(hi_x, x_hi - p.margin - s.px)
        lo_y, hi_y = self.y_range[0] + bm[2], self.y_range[1] - bm[3]
        xs = {lo_x}
        ys = {lo_y}
        for q in others:
            xs |= {q.x0, q.x1 + self._step(max(q.spread.px, s.mx))}
            ys |= {q.y0, q.y1 + self._step(max(q.spread.py, s.my))}
        for ob in obstacles:
            xs.add(ob[2] + self.gap + p.margin + s.mx)
            ys.add(ob[3] + self.gap + p.margin + s.my)
        grown = [(ob[0] - self.gap, ob[1] - self.gap, ob[2] + self.gap, ob[3] + self.gap) for ob in obstacles]
        best: tuple[tuple[float, float], float, float] | None = None
        for y in sorted(ys):
            if y < lo_y - 1e-9 or y + p.h > hi_y + 1e-9:
                continue
            for x in sorted(xs):
                if x < lo_x - 1e-9 or x + p.w > hi_x + 1e-9:
                    continue
                score = (y + p.h, x + p.w)
                if best is not None and score >= best[0]:
                    continue
                p.x0, p.y0 = x, y
                if any(overlaps(p.rect(), ob) for ob in grown):
                    continue
                if any(pair_check(p, q) is not None for q in others):
                    continue
                best = (score, x, y)
        if best is None:
            return False
        p.x0, p.y0 = best[1], best[2]
        p.placed, p.why_not = True, ""
        return True


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
