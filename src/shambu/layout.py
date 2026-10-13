"""Place parts on the bed with known positions and check every machine limit before the CLI runs.

Positions are computed here, never by Bambu's arrange, so the report can state where every part
sits. The CLI's pos_x/pos_y/pos_z are a translation added to the STL's own coordinates, so a part
whose bounding box should start at (x0, y0) on the bed gets pos = (x0 - min_x, y0 - min_y, -min_z).

Design: `Placer` is a Strategy (ShelfPacker today; an explicit or grid placer drops in). Limit checks
are a list of `Rule` strategies run by `LimitChecker`, so a new printer fact is one more rule.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from .errors import LayoutError
from .studio import MeshInfo

BED = 256.0
EDGE = 2.0
GAP = 5.0
WIPE_TOWER = (15.0, 220.0, 35.0, 35.0)  # x, y, w, h reserved when a plate uses two filaments (CLI default position)

Rect = tuple[float, float, float, float]  # x0, y0, x1, y1


@dataclass
class Placed:
    """A part with its mesh info and the bed rectangle its bounding box will occupy."""

    key: str
    info: MeshInfo
    filament: int
    x0: float = 0.0
    y0: float = 0.0
    margin: float = 0.0  # brim or extra clearance around the footprint
    fixed: bool = False  # placed by the spec, not by the packer
    also_filaments: frozenset[int] = frozenset()  # filaments used by overrides, height ranges or sub-parts

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

    def rect(self, with_margin: bool = True) -> Rect:
        m = self.margin if with_margin else 0.0
        return (self.x0 - m, self.y0 - m, self.x1 + m, self.y1 + m)

    def pos(self) -> tuple[float, float, float]:
        """Translation for the assemble list: bbox starts at (x0, y0) and the part sits on the bed."""
        return (self.x0 - self.info.min[0], self.y0 - self.info.min[1], -self.info.min[2])


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


class Placer(Protocol):
    """Sets x0/y0 on each free part and returns them split into plates. `x_min_of` gives, per part key,
    a stricter left limit (the reach of the leftmost-limited extruder the part uses)."""

    def place(
        self, parts: list[Placed], obstacles: Sequence[Rect] = (), x_min_of: Mapping[str, float] | None = None
    ) -> list[list[Placed]]: ...


@dataclass
class ShelfPacker:
    """Rows along x from the bottom left, deepest parts first. Returns one or more plates."""

    x_range: tuple[float, float] = (EDGE, BED - EDGE)
    y_range: tuple[float, float] = (EDGE, BED - EDGE)
    gap: float = GAP

    def place(self, parts: list[Placed], obstacles: Sequence[Rect] = (), x_min_of: Mapping[str, float] | None = None) -> list[list[Placed]]:
        x_min = dict(x_min_of or {})
        left = sorted(parts, key=lambda p: (-(p.h + 2 * p.margin), -(p.w + 2 * p.margin)))
        plates: list[list[Placed]] = []
        plate: list[Placed] = []
        y = self.y_range[0]
        while left:
            row = [left.pop(0)]
            x_after = max(self.x_range[0], x_min.get(row[0].key, self.x_range[0])) + row[0].w + 2 * row[0].margin + self.gap
            for o in list(left):
                start = max(x_after, x_min.get(o.key, self.x_range[0]))
                if start + o.w + 2 * o.margin <= self.x_range[1]:
                    row.append(o)
                    left.remove(o)
                    x_after = start + o.w + 2 * o.margin + self.gap
            row_h = max(r.h + 2 * r.margin for r in row)
            if y + row_h > self.y_range[1] and plate:
                plates.append(plate)
                plate, y = [], self.y_range[0]
            if y + row_h > self.y_range[1]:
                raise LayoutError(f"{row[0].key} is {row[0].h:.1f} mm deep (+{row[0].margin:.1f} margin): deeper than the bed area")
            x = self.x_range[0]
            for o in row:
                if o.w + 2 * o.margin > self.x_range[1] - self.x_range[0]:
                    raise LayoutError(f"{o.key} is {o.w:.1f} mm wide (+{o.margin:.1f} margin): wider than the x range {self.x_range}")
                o.x0, o.y0 = max(x, x_min.get(o.key, self.x_range[0])) + o.margin, y + o.margin
                for ob in obstacles:
                    while overlaps(o.rect(), ob) and o.x1 + o.margin <= self.x_range[1]:
                        o.x0 = ob[2] + self.gap + o.margin
                x = o.x1 + o.margin + self.gap
                plate.append(o)
            y += row_h + self.gap
        if plate:
            plates.append(plate)
        return plates


@dataclass
class Check:
    ok: bool
    text: str


@dataclass
class Context:
    """What the rules need: machine facts, the filament->extruder map, TPU slots, extra limits, and the
    prime-tower reserve of the plate being checked (None when the plate uses one filament)."""

    facts: dict
    filament_extruder: dict[int, int] = field(default_factory=dict)
    tpu_filaments: set[int] = field(default_factory=set)
    aux_x_limit: float | None = None
    reserve: Rect | None = None

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


class NoOverlap:
    def check(self, placed, ctx):
        for i, a in enumerate(placed):
            for b in placed[i + 1 :]:
                if overlaps(a.rect(), b.rect()):
                    yield Check(False, f"{a.key} and {b.key} overlap (footprints with brim margins)")


class InsideBed:
    def check(self, placed, ctx):
        bed = ctx.facts["bed"]
        for p in placed:
            x0, y0, x1, y1 = p.rect(with_margin=True)
            if x0 < bed["x"][0] or x1 > bed["x"][1] or y0 < bed["y"][0] or y1 > bed["y"][1]:
                yield Check(
                    False,
                    f"{p.key} footprint with brim margin x {x0:.1f}..{x1:.1f} y {y0:.1f}..{y1:.1f} "
                    f"leaves the bed {bed['x'][1]:.0f} x {bed['y'][1]:.0f}",
                )


class ReserveClear:
    """Nothing (packed or fixed) may cover the prime-tower reserve of a plate that uses two or more filaments."""

    def check(self, placed, ctx):
        if ctx.reserve is None:
            return
        for p in placed:
            if overlaps(p.rect(with_margin=True), ctx.reserve):
                r = ctx.reserve
                yield Check(
                    False,
                    f"{p.key} overlaps the prime-tower reserve x {r[0]:.0f}..{r[2]:.0f} y {r[1]:.0f}..{r[3]:.0f} "
                    f"(the plate uses more than one filament)",
                )


class ExtruderReach:
    """Every extruder a part uses (its filament, height ranges, sub-parts) must reach the whole part."""

    def check(self, placed, ctx):
        for p in placed:
            for fil in sorted(p.filaments):
                if not ctx.known(fil):
                    yield Check(False, f"{p.key} uses filament {fil}, which is not a loaded filament slot")
                    continue
                e = ctx.extruder_of(fil)
                if p.info.size[2] > e["z_max"]:
                    yield Check(False, f"{p.key} is {p.info.size[2]:.1f} mm tall; extruder {e['index']} prints up to {e['z_max']:.0f} mm")
                if p.x0 < e["x"][0] or p.x1 > e["x"][1] or p.y0 < e["y"][0] or p.y1 > e["y"][1]:
                    yield Check(
                        False,
                        f"{p.key} uses filament {fil} (extruder {e['index']}, {e['type']}) and spans x {p.x0:.1f}..{p.x1:.1f}; "
                        f"that extruder reaches x {e['x'][0]:.1f}..{e['x'][1]:.1f}",
                    )
                if e["index"] == 2 and ctx.aux_x_limit is not None and p.x1 > ctx.aux_x_limit:
                    yield Check(
                        False,
                        f"{p.key} uses extruder 2 and ends at x {p.x1:.1f}; the configured aux_x_limit for the auxiliary nozzle is {ctx.aux_x_limit} mm",
                    )


class TpuOnMain:
    def check(self, placed, ctx):
        for p in placed:
            for fil in sorted(p.filaments):
                if fil in ctx.tpu_filaments and ctx.extruder_of(fil)["index"] != 1:
                    yield Check(
                        False,
                        f"{p.key} uses TPU filament {fil} on extruder {ctx.extruder_of(fil)['index']}; TPU prints only through extruder 1 (main)",
                    )


class MeshSane:
    def check(self, placed, ctx):
        for p in placed:
            if not p.info.manifold:
                yield Check(False, f"{p.key} is not manifold")
            if not p.info.on_bed():
                yield Check(True, f"{p.key}: STL min z is {p.info.min[2]:.2f}; the placement drops it onto the bed")


DEFAULT_RULES: tuple[Rule, ...] = (NoOverlap(), InsideBed(), ReserveClear(), ExtruderReach(), TpuOnMain(), MeshSane())


@dataclass
class LimitChecker:
    rules: tuple[Rule, ...] = DEFAULT_RULES

    def check(self, placed: list[Placed], ctx: Context) -> list[Check]:
        out: list[Check] = []
        for r in self.rules:
            out += list(r.check(placed, ctx))
        if not any(not c.ok for c in out):
            bed = ctx.facts["bed"]
            out.append(
                Check(True, f"{len(placed)} part(s) fit: bed {bed['x'][1]:.0f} x {bed['y'][1]:.0f}, every reach and height limit holds")
            )
        return out


def describe(placed: list[Placed]) -> list[str]:
    return [
        f"{p.key:28s} filament {'+'.join(str(f) for f in sorted(p.filaments))}  bbox x {p.x0:6.1f}..{p.x1:6.1f}  y {p.y0:6.1f}..{p.y1:6.1f}  "
        f"z 0..{p.info.size[2]:5.1f}  ({p.w:.1f} x {p.h:.1f} x {p.info.size[2]:.1f} mm)"
        for p in placed
    ]
