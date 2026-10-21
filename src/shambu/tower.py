"""The prime tower of a two-filament plate: Studio's footprint estimate, a free spot both nozzles reach, and the rules.

Pure planning core: no subprocess, no zip, no network. `build.py` asks this module for a footprint per plate,
reserves it during packing, writes the chosen `wipe_tower_x/y` per plate, and runs `TowerClear` and
`InterfaceReach` with the other limit rules. It does not slice and cannot see Studio's real tower path: the
footprint is the estimate Studio itself uses before slicing, and the binding check is still the post-slice G-code
conflict test (exit -101).

Sources (Bambu Studio v02.08.02.61, `skills/shambu/references/06-slicer-rules.md` section 10):
- size: `PartPlate::estimate_wipe_tower_size`, PartPlate.cpp:2191-2265;
- minimum depth by height and the auto brim: `WipeTower::get_limit_depth_by_height` and `get_auto_brim_by_height`,
  WipeTower.cpp:1587-1630;
- the footprint `x - brim .. x + w + brim`, `y - brim .. y + d + brim`: PartPlate.cpp:2290-2296 and
  BambuStudio.cpp:4462-4488 (CLI log `wipe bbox: min {200, 200} - max {257.39, 257.39}` at (208, 208), H 218).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import pairwise

from .layout import Check

Rect = tuple[float, float, float, float]  # x0, y0, x1, y1

MIN_DEPTH_BY_HEIGHT = ((5.0, 5.0), (100.0, 20.0), (250.0, 40.0), (350.0, 60.0))  # WipeTower.cpp:1587-1589
FALLBACK_WIDTHS = (35.0, 30.0, 25.0, 22.0, 20.0)  # tried with the rib wall off and a 2 mm brim when the default does not fit
FALLBACK_BRIM = 2.0
PART_CLEARANCE = 3.0  # mm between the tower footprint and a part's margin-grown rect
CLEARANCE_CAP = 10.0  # more clearance than this scores the same, so the tie-breakers (high y, then high x) decide
WIDTH_RANGE = (10.0, 60.0)
FILAMENT_DIAMETER = 1.75


def min_depth(height: float) -> float:
    """Studio's minimum tower depth for a plate whose tallest object is `height` mm (WipeTower.cpp:1591-1625)."""
    pts = MIN_DEPTH_BY_HEIGHT
    if height <= pts[0][0]:
        return pts[0][1]
    for (h0, d0), (h1, d1) in pairwise(pts):
        if height < h1:
            return d0 + (height - h0) / (h1 - h0) * (d1 - d0)
    return pts[-1][1]


def auto_brim(height: float) -> float:
    """`prime_tower_brim_width -1`: 8 mm from 100 mm up, else 8 % of the height (WipeTower.cpp:1627-1630)."""
    return 8.0 if height >= 100 else height / 100.0 * 8.0


def _f(v, default: float) -> float:
    if isinstance(v, list):
        v = v[0] if v else default
    try:
        return float(str(v).rstrip("%"))
    except (TypeError, ValueError):
        return default


def _on(v, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, list):
        v = v[0] if v else default
    return str(v).strip().lower() in ("1", "true")


@dataclass(frozen=True)
class TowerSettings:
    """The inputs of Studio's estimate. Width, rib wall and brim are process-global in Studio (one value per project)."""

    width: float = 35.0  # prime_tower_width
    rib_wall: bool = True  # prime_tower_rib_wall (default on, PrintConfig.cpp:6281-6286)
    brim: float = -1.0  # prime_tower_brim_width; -1 = auto by height
    rib_width: float = 8.0  # prime_tower_rib_width
    extra_rib_length: float = 0.0  # prime_tower_extra_rib_length
    infill_gap: float = 1.5  # prime_tower_infill_gap 150 %
    layer_height: float = 0.2
    wipe_volume: float = 45.0  # the largest filament_prime_volume (15 in prime_volume_mode "Saving")
    change_volume: float = 0.0  # the largest filament_change_length x the filament cross-section
    plate_filaments: int = 2  # filaments the plate prints with
    extruders: int = 2

    @classmethod
    def from_config(
        cls, process: Mapping, filaments: Sequence[Mapping] = (), plate_filaments: int = 2, extruders: int = 2
    ) -> TowerSettings:
        """Read the estimate inputs from a resolved process preset and the filament presets (defaults where absent)."""
        prime = [_f(v, 45.0) for f in filaments for v in (f.get("filament_prime_volume") or [])] or [45.0]
        if str(process.get("prime_volume_mode", "")).lower() == "saving":
            prime = [15.0]
        length = max([_f(v, 0.0) for f in filaments for v in (f.get("filament_change_length") or [])] or [0.0])
        dia = max([_f(v, FILAMENT_DIAMETER) for f in filaments for v in (f.get("filament_diameter") or [])] or [FILAMENT_DIAMETER])
        return cls(
            width=_f(process.get("prime_tower_width"), 35.0),
            rib_wall=_on(process.get("prime_tower_rib_wall"), True),
            brim=_f(process.get("prime_tower_brim_width"), -1.0),
            rib_width=_f(process.get("prime_tower_rib_width"), 8.0),
            extra_rib_length=_f(process.get("prime_tower_extra_rib_length"), 0.0),
            infill_gap=_f(process.get("prime_tower_infill_gap"), 150.0) / 100.0,
            layer_height=_f(process.get("layer_height"), 0.2),
            wipe_volume=max(prime),
            change_volume=length * math.pi * dia * dia / 4.0,
            plate_filaments=plate_filaments,
            extruders=extruders,
        )

    def volume(self) -> float:
        """PartPlate.cpp:2238-2239: the prime volume per filament, plus one filament change per pair on two extruders."""
        n = self.plate_filaments
        v = self.wipe_volume * (n if self.extruders == 2 else n - 1)
        if self.extruders == 2:
            v += self.change_volume * (n // 2)
        return v

    def size(self, height: float) -> tuple[float, float]:
        """(width, depth) of the tower body without its brim (PartPlate.cpp:2240-2262)."""
        vol = self.volume()
        if self.rib_wall:
            depth = math.sqrt(vol / self.layer_height * self.infill_gap)
            volume_depth = depth
            depth = max(min_depth(height), depth)
            rib = min(self.rib_width, depth / 2)
            side = rib / math.sqrt(2) + max(depth + self.extra_rib_length, volume_depth)
            return side, side
        depth = vol / (self.layer_height * self.width) * self.infill_gap
        return self.width, max(min_depth(height), depth)

    def brim_at(self, height: float) -> float:
        return auto_brim(height) if self.brim < 0 else self.brim

    def describe(self) -> str:
        return f"width {self.width:g}, rib_wall {'true' if self.rib_wall else 'false'}, brim {self.brim:g}"


@dataclass(frozen=True)
class Footprint:
    """A tower at (x, y): its body w x d and its brim. `rect` includes the brim (the area nothing else may use)."""

    x: float
    y: float
    w: float
    d: float
    brim: float
    height: float
    settings: TowerSettings

    @property
    def rect(self) -> Rect:
        b = self.brim
        return (self.x - b, self.y - b, self.x + self.w + b, self.y + self.d + b)

    @property
    def outer(self) -> tuple[float, float]:
        r = self.rect
        return (r[2] - r[0], r[3] - r[1])

    def describe(self) -> str:
        ow, od = self.outer
        kind = "rib wall" if self.settings.rib_wall else f"width {self.w:g}, no rib wall"
        return (
            f"prime tower at ({self.x:.1f}, {self.y:.1f}) is {ow:.1f} x {od:.1f} mm with its {round(self.brim, 1):g} mm brim "
            f"({kind}, tallest part {self.height:.0f} mm)"
        )


def footprint(settings: TowerSettings, height: float, x: float, y: float) -> Footprint:
    w, d = settings.size(height)
    return Footprint(x, y, w, d, settings.brim_at(height), height, settings)


def shared_reach(extruders: Iterable[Mapping], bed: Mapping, aux_x_limit: float | None = None) -> Rect:
    """The area every given extruder reaches, inside the bed. The tower is printed by both nozzles. `aux_x_limit` is
    the user's stricter right edge for extruder 2."""
    x0, y0, x1, y1 = bed["x"][0], bed["y"][0], bed["x"][1], bed["y"][1]
    for e in extruders:
        x0, x1 = max(x0, e["x"][0]), min(x1, e["x"][1])
        y0, y1 = max(y0, e["y"][0]), min(y1, e["y"][1])
        if e.get("index") == 2 and aux_x_limit is not None:
            x1 = min(x1, aux_x_limit)
    return (x0, y0, x1, y1)


def inside(r: Rect, area: Rect, eps: float = 1e-6) -> bool:
    return r[0] >= area[0] - eps and r[1] >= area[1] - eps and r[2] <= area[2] + eps and r[3] <= area[3] + eps


def _overlaps(a: Rect, b: Rect) -> bool:
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


def _gap(a: Rect, b: Rect) -> float:
    """Shortest distance between two rectangles; 0 when they touch or overlap."""
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return math.hypot(dx, dy)


def _area(a: Rect, b: Rect) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


@dataclass(frozen=True)
class Placement:
    """The result of `auto_place`: a footprint, or None with the best blocked spot and the part that blocks it."""

    footprint: Footprint | None
    tried: tuple[str, ...]  # one entry per settings tried, e.g. "width 41.4 (rib wall)"
    best: Footprint | None = None
    blocker: str | None = None


def _origins(fw: float, fd: float, brim: float, area: Rect, step: float) -> Iterable[tuple[float, float]]:
    """Tower origins (x, y) on a `step` grid whose footprint lies inside `area`."""
    x_lo, x_hi = math.ceil((area[0] + brim) / step) * step, area[2] - fw + brim
    y_lo, y_hi = math.ceil((area[1] + brim) / step) * step, area[3] - fd + brim
    nx, ny = int((x_hi - x_lo) / step + 1e-9), int((y_hi - y_lo) / step + 1e-9)
    for i in range(nx + 1):
        for j in range(ny + 1):
            yield x_lo + i * step, y_lo + j * step


def find_spot(
    settings: TowerSettings,
    height: float,
    obstacles: Sequence[tuple[str, Rect]],
    area: Rect,
    clearance: float = PART_CLEARANCE,
    step: float = 1.0,
) -> tuple[Footprint | None, Footprint | None, str | None]:
    """(free footprint, best blocked footprint, its blocker). A spot is free when the footprint lies in `area` and
    keeps `clearance` from every obstacle rect. Score: the clearance (capped at CLEARANCE_CAP and floored to 0.5 mm),
    then high y (Studio's own default sits at the back, y 250, PartPlate.cpp:69-70), then high x."""
    fp0 = footprint(settings, height, 0.0, 0.0)
    fw, fd = fp0.outer
    best_free: tuple[tuple[float, float, float], Footprint] | None = None
    best_blocked: tuple[tuple[float, float, float], Footprint, str] | None = None
    for x, y in _origins(fw, fd, fp0.brim, area, step):
        fp = replace(fp0, x=x, y=y)
        r = fp.rect
        gaps = [(_gap(r, o), name, o) for name, o in obstacles]
        g = min((v for v, _, _ in gaps), default=CLEARANCE_CAP)
        if g >= clearance - 1e-9 and not any(_overlaps(r, o) for _, _, o in gaps):
            key = (math.floor(min(g, CLEARANCE_CAP) * 2) / 2, y, x)
            if best_free is None or key > best_free[0]:
                best_free = (key, fp)
        elif best_free is None:
            grown = [(name, (o[0] - clearance, o[1] - clearance, o[2] + clearance, o[3] + clearance)) for _, name, o in gaps]
            hits = [(_area(r, o), name) for name, o in grown if _overlaps(r, o)]
            total = sum(a for a, _ in hits)
            key_b = (-total, y, x)
            if best_blocked is None or key_b > best_blocked[0]:
                best_blocked = (key_b, fp, max(hits)[1] if hits else "")
    if best_free is not None:
        return best_free[1], None, None
    if best_blocked is not None:
        return None, best_blocked[1], best_blocked[2] or None
    return None, None, None


def candidates(settings: TowerSettings) -> list[TowerSettings]:
    """The process settings first, then the rib wall off with widths 35, 30, 25, 22 and 20 and a 2 mm brim."""
    out = [settings]
    for w in FALLBACK_WIDTHS:
        c = replace(settings, rib_wall=False, width=w, brim=FALLBACK_BRIM)
        if c not in out:
            out.append(c)
    return out


def auto_place(
    settings: TowerSettings,
    height: float,
    obstacles: Sequence[tuple[str, Rect]],
    area: Rect,
    clearance: float = PART_CLEARANCE,
    step: float = 1.0,
    fallback: bool = True,
) -> Placement:
    """The first settings in `candidates()` (or only `settings` without `fallback`) with a free spot."""
    tried: list[str] = []
    best: Footprint | None = None
    blocker: str | None = None
    for s in candidates(settings) if fallback else [settings]:
        fp, b, who = find_spot(s, height, obstacles, area, clearance, step)
        w, _ = s.size(height)
        tried.append(f"{w:.1f} mm" + (" (rib wall)" if s.rib_wall else " (no rib wall)"))
        if fp is not None:
            return Placement(fp, tuple(tried))
        if b is not None:
            best, blocker = b, who
    return Placement(None, tuple(tried), best, blocker)


# ---------------------------------------------------------------------------- the plan of one plate
@dataclass(frozen=True)
class TowerPlan:
    """What a plate's tower is: `mode` "auto" | "fixed" | "off" | "none" (one filament, or print-by-object).
    `footprint` is set for auto and fixed; `problem` is a FAIL line for the plate's checks."""

    plate: int
    mode: str
    footprint: Footprint | None = None
    problem: str | None = None
    warning: str | None = None

    @property
    def active(self) -> bool:
        return self.footprint is not None


def _fmt_rect(r: Rect) -> str:
    return f"x {r[0]:.1f}..{r[2]:.1f} y {r[1]:.1f}..{r[3]:.1f}"


def _alternative(settings: TowerSettings, height: float, obstacles: Sequence[tuple[str, Rect]], area: Rect, first_w: float) -> str:
    p = auto_place(settings, height, obstacles, area)
    if p.footprint is None:
        return f'Free spots both nozzles reach: none down to width {FALLBACK_WIDTHS[-1]:g}; set "wipe_tower": false or move parts'
    fp = p.footprint
    lead = "" if fp.settings == settings else f"none at {first_w:.1f} mm; "
    return f"Free spots both nozzles reach: {lead}x {fp.x:.0f} y {fp.y:.0f} with {fp.settings.describe()}"


def check_fixed(plate: int, fp: Footprint, obstacles: Sequence[tuple[str, Rect]], area: Rect, bed: Rect) -> str | None:
    """The FAIL line for a tower at a fixed spot, or None when it is clear (no overlap with a margin-grown part rect,
    inside the bed and inside the shared reach)."""
    r = fp.rect
    why: list[str] = []
    hits = [(name, o) for name, o in obstacles if _overlaps(r, o)]
    if hits:
        why.append("it overlaps " + ", ".join(f"{n} ({_fmt_rect(o)})" for n, o in hits))
    if not inside(r, bed):
        edge = []
        if r[2] > bed[2]:
            edge.append(f"x {r[2]:.1f}")
        if r[0] < bed[0]:
            edge.append(f"x {r[0]:.1f}")
        if r[3] > bed[3]:
            edge.append(f"y {r[3]:.1f}")
        if r[1] < bed[1]:
            edge.append(f"y {r[1]:.1f}")
        why.append("leaves the bed at " + " and ".join(edge))
    elif not inside(r, area):
        why.append(f"leaves the area both nozzles reach ({_fmt_rect(area)})")
    if not why:
        return None
    first_w = fp.outer[0] - 2 * fp.brim
    alt = _alternative(fp.settings, fp.height, obstacles, area, first_w)
    return f"plate {plate} {fp.describe()}: {' and '.join(why)}. {alt}. Set plates[{plate - 1}].wipe_tower."


def plan_auto(
    plate: int, settings: TowerSettings, height: float, obstacles: Sequence[tuple[str, Rect]], area: Rect, fallback: bool = True
) -> TowerPlan:
    """The "auto" tower of a plate: the first free spot (`auto_place`), or a FAIL line naming the best blocked spot."""
    p = auto_place(settings, height, obstacles, area, fallback=fallback)
    if p.footprint is not None:
        return TowerPlan(plate, "auto", p.footprint)
    where = ""
    if p.best is not None:
        where = f"; the best spot ({p.best.x:.0f}, {p.best.y:.0f}) at {p.best.settings.describe()}" + (
            f" is blocked by {p.blocker}" if p.blocker else ""
        )
    msg = (
        f"plate {plate}: no free prime tower spot both nozzles reach ({_fmt_rect(area)}), tried "
        + ", ".join(p.tried)
        + f" with {PART_CLEARANCE:g} mm clearance to every part{where}. Move parts, set plates[{plate - 1}].wipe_tower "
        '{"x", "y", "width", "rib_wall", "brim"}, or set "wipe_tower": false'
    )
    return TowerPlan(plate, "auto", None, problem=msg)


def off_warning(plate: int) -> str:
    return (
        f"plate {plate} wipe_tower false: two filaments with no prime tower; Studio slices it (exit 0 measured) "
        "but the idle nozzle is not wiped before the interface layers"
    )


@dataclass(frozen=True)
class TowerClear:
    """Rule: the plate's tower plan has no problem. One instance per plate (it carries that plate's plan); build.py
    runs it after the limit checker. Kind "tower"."""

    plan: TowerPlan
    kind: str = "tower"

    def check(self, placed, ctx) -> Iterable:
        if self.plan.problem:
            yield Check(False, self.plan.problem)
        elif self.plan.footprint is not None:
            fp = self.plan.footprint
            yield Check(True, f"plate {self.plan.plate} {fp.describe()}, clear of every part ({self.plan.mode})")


@dataclass(frozen=True)
class InterfaceUse:
    """A supported part whose support or support interface prints with another slot set at process level."""

    key: str
    key_name: str  # support_interface_filament | support_filament
    slot: int
    margin: float  # brim margin + 3 mm: the interface overshoot past the part box (2.2 mm measured, n=1)
    source: str = "process"


INTERFACE_OVERSHOOT = 3.0


@dataclass(frozen=True)
class InterfaceReach:
    """Rule: the extruder that prints a part's support interface (or support) reaches the part's box grown by its
    brim margin plus 3 mm. The seed build exited -102 with the right nozzle's interface at x 14.3 (reference
    06-slicer-rules.md section 5). One instance per plate."""

    uses: tuple[InterfaceUse, ...]

    def check(self, placed, ctx) -> Iterable:
        by_key = {p.key: p for p in placed}
        for u in self.uses:
            p = by_key.get(u.key)
            if p is None or not ctx.known(u.slot):
                continue
            e = ctx.extruder_of(u.slot)
            x0, y0, x1, y1 = p.x0 - u.margin, p.y0 - u.margin, p.x1 + u.margin, p.y1 + u.margin
            lim_x1 = min(e["x"][1], ctx.aux_x_limit) if (e["index"] == 2 and ctx.aux_x_limit is not None) else e["x"][1]
            if x0 < e["x"][0] or x1 > lim_x1 or y0 < e["y"][0] or y1 > e["y"][1]:
                yield Check(
                    False,
                    f"{p.key} has support with the {u.source} {u.key_name} {u.slot} (extruder {e['index']}, {e['type']}) and spans "
                    f"x {x0:.1f}..{x1:.1f} with its interface margin; extruder {e['index']} reaches x {e['x'][0]:.1f}..{lim_x1:.1f}",
                )
