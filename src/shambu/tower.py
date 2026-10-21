"""The prime tower of a two-filament plate: Studio's footprint estimate, a free spot both extruders reach, the rules.

Pure planning core: no subprocess, no zip, no network. `build.py` asks this module for a footprint per plate,
reserves it during packing, writes the chosen `wipe_tower_x/y` per plate, and adds each plate's `TowerClear` lines
to the other limit checks. It does not slice and cannot see Studio's real tower path: the
footprint is the estimate Studio itself uses before slicing, and the binding check is still the post-slice G-code
conflict test (exit -101).

Sources (Bambu Studio v02.08.02.61, `skills/shambu/references/06-slicer-rules.md` section 10):
- size: `PartPlate::estimate_wipe_tower_size`, PartPlate.cpp:2191-2265;
- minimum depth by height and the auto brim: `WipeTower::get_limit_depth_by_height` and `get_auto_brim_by_height`,
  WipeTower.cpp:1587-1630;
- the footprint `x - brim .. x + w + brim`, `y - brim .. y + d + brim`: PartPlate.cpp:2290-2296 and
  BambuStudio.cpp:4462-4488 (CLI log `wipe bbox: min {200, 200} - max {257.39, 257.39}` at (208, 208), H 218).

Limits: the GUI formula is an estimate. Studio recomputes the real tower at slice time from the actual tool changes
(`WipeTower::plan_tower` and the depth pass, WipeTower.cpp:4520-4585), so the sliced tower can differ. A tower is also
needed with ONE filament when the process uses Smooth timelapse (`need_wipe_tower`, PartPlate.cpp:2222-2223): build.py
then plans an "auto" tower on a one-filament plate too.

Design: pure functions for the estimate (`min_depth`, `auto_brim`, `TowerSettings.size`, `footprint`), a grid search
for a free spot (`find_spot`, `auto_place`) whose obstacle test is a parameter (the `TowerClearance` protocol:
`FootprintClearance` over the layout's part silhouettes and support spreads, strict first and then relaxed, or
`BoxClearance` over part rects), `plan_project` for ONE tower size per project over every plate, and one `Rule` for the
limit checker (`TowerClear`). It handles the tower's size and spot. It does not handle the post-slice tower, purge
volumes, the part layout, the support predicate or the extruder reach of the interface (layout.py: `needs_support`,
`ExtruderReach`).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import pairwise
from typing import Protocol

from . import layout as L
from .errors import BuildError
from .layout import Check

Rect = tuple[float, float, float, float]  # x0, y0, x1, y1

MIN_DEPTH_BY_HEIGHT = ((5.0, 5.0), (100.0, 20.0), (250.0, 40.0), (350.0, 60.0))  # WipeTower.cpp:1587-1589
FALLBACK_WIDTHS = (35.0, 30.0, 25.0, 22.0, 20.0)  # tried with the rib wall off and a 2 mm brim when the default does not fit
FALLBACK_BRIM = 2.0
PART_CLEARANCE = 3.0  # mm between the tower footprint and the rect (with brim and spread) of a part known by its box only
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


def _f(v, default: float, key: str = "value") -> float:
    """A number from a preset value ("150%" -> 150). Missing -> `default`; a value that does not parse raises
    BuildError naming the key, so the planned footprint never silently differs from Studio's."""
    if isinstance(v, list):
        v = v[0] if v else None
    if v is None or str(v).strip() == "":
        return default
    try:
        return float(str(v).strip().rstrip("%"))
    except (TypeError, ValueError):
        raise BuildError(f"{key} = {v!r} is not a number (the prime tower estimate needs it)") from None


def side(index: int) -> str:
    """ "extruder 1 (left nozzle)" / "extruder 2 (right nozzle)": one name for an extruder in every message."""
    return f"extruder {index} ({'left' if index == 1 else 'right'} nozzle)"


BOTH = "extruders 1 and 2 (left and right nozzle)"


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
        prime = [_f(v, 45.0, "filament_prime_volume") for f in filaments for v in (f.get("filament_prime_volume") or [])] or [45.0]
        if str(process.get("prime_volume_mode", "")).lower() == "saving":
            prime = [15.0]
        length = max([_f(v, 0.0, "filament_change_length") for f in filaments for v in (f.get("filament_change_length") or [])] or [0.0])
        dia = max(
            [_f(v, FILAMENT_DIAMETER, "filament_diameter") for f in filaments for v in (f.get("filament_diameter") or [])]
            or [FILAMENT_DIAMETER]
        )
        return cls(
            width=_f(process.get("prime_tower_width"), 35.0, "prime_tower_width"),
            rib_wall=_on(process.get("prime_tower_rib_wall"), True),
            brim=_f(process.get("prime_tower_brim_width"), -1.0, "prime_tower_brim_width"),
            rib_width=_f(process.get("prime_tower_rib_width"), 8.0, "prime_tower_rib_width"),
            extra_rib_length=_f(process.get("prime_tower_extra_rib_length"), 0.0, "prime_tower_extra_rib_length"),
            infill_gap=_f(process.get("prime_tower_infill_gap"), 150.0, "prime_tower_infill_gap") / 100.0,
            layer_height=_f(process.get("layer_height"), 0.2, "layer_height"),
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
class TowerFootprint:
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


def footprint(settings: TowerSettings, height: float, x: float, y: float) -> TowerFootprint:
    w, d = settings.size(height)
    return TowerFootprint(x, y, w, d, settings.brim_at(height), height, settings)


def shared_reach(extruders: Iterable[Mapping], bed: Mapping, aux_x_limit: float | None = None) -> Rect:
    """The area every given extruder reaches, inside the bed. Both extruders print the tower. `aux_x_limit` is
    a stricter right edge for extruder 2, when the user sets one."""
    x0, y0, x1, y1 = bed["x"][0], bed["y"][0], bed["x"][1], bed["y"][1]
    for e in extruders:
        lo, hi = L.extruder_x(e, aux_x_limit)
        x0, x1 = max(x0, lo), min(x1, hi)
        y0, y1 = max(y0, e["y"][0]), min(y1, e["y"][1])
    return (x0, y0, x1, y1)


def inside(r: Rect, area: Rect, eps: float = 1e-6) -> bool:
    return r[0] >= area[0] - eps and r[1] >= area[1] - eps and r[2] <= area[2] + eps and r[3] <= area[3] + eps


def _gap(a: Rect, b: Rect) -> float:
    """Shortest distance between two rectangles; 0 when they touch or overlap (`layout.rect_gap`)."""
    return L.rect_gap(a, b)[0]


def _area(a: Rect, b: Rect) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


class TowerClearance(Protocol):
    """The obstacle test of the placement, as a parameter: `FootprintClearance` tests the parts' silhouettes and
    support spreads (build.py binds it when the parts have outlines), `BoxClearance` tests part rects."""

    def clear(self, rect: Rect) -> bool:
        """True when a tower footprint `rect` may stand there."""
        ...

    def gap(self, rect: Rect) -> float:
        """The clearance of a free `rect` to the nearest obstacle (scoring only)."""
        ...

    def blocker(self, rect: Rect) -> tuple[float, str | None]:
        """For a blocked `rect`: (how much it is blocked, the name of the worst obstacle)."""
        ...

    def blocking(self, rect: Rect) -> list[tuple[str, Rect]]:
        """Every obstacle that blocks `rect`, with its box, for a FAIL line."""
        ...

    def describe(self) -> str:
        """What the test keeps clear, for a FAIL line: "3 mm clearance to every part's box"."""
        ...


@dataclass(frozen=True)
class BoxClearance:
    """Part rects (grown by brim and support spread, `Placed.rect()`) plus `margin` mm on every side. The grown rects
    are computed once; `narrow()` keeps only the rects within the scoring radius of one x band, so the grid scan stays
    fast with many parts and the clearance score (up to CLEARANCE_CAP) still sees every rect that can lower it."""

    obstacles: tuple[tuple[str, Rect], ...]
    margin: float = PART_CLEARANCE
    grown: tuple[tuple[str, Rect, Rect], ...] = ()  # (name, rect, rect grown by margin), filled in __post_init__

    def __post_init__(self) -> None:
        m = self.margin
        object.__setattr__(self, "grown", tuple((n, o, (o[0] - m, o[1] - m, o[2] + m, o[3] + m)) for n, o in self.obstacles))

    def narrow(self, x0: float, x1: float) -> BoxClearance:
        r = max(self.margin, CLEARANCE_CAP)
        return BoxClearance(tuple((n, o) for n, o in self.obstacles if o[2] + r > x0 and o[0] - r < x1), self.margin)

    def clear(self, rect: Rect) -> bool:
        # The grown rect is a quick reject; a corner needs the exact (Euclidean) gap
        return all(not L.overlaps(rect, g) or (not L.overlaps(rect, o) and _gap(rect, o) >= self.margin - 1e-9) for _, o, g in self.grown)

    def gap(self, rect: Rect) -> float:
        return min((_gap(rect, o) for _, o in self.obstacles), default=CLEARANCE_CAP)

    def blocker(self, rect: Rect) -> tuple[float, str | None]:
        hits = [(_area(rect, g), n) for n, _, g in self.grown]
        hits = [h for h in hits if h[0] > 0]
        return (sum(a for a, _ in hits), max(hits)[1]) if hits else (0.0, None)

    def blocking(self, rect: Rect) -> list[tuple[str, Rect]]:
        return [(n, o) for n, o, g in self.grown if L.overlaps(rect, g) and (L.overlaps(rect, o) or _gap(rect, o) < self.margin - 1e-9)]

    def describe(self) -> str:
        return f"{self.margin:g} mm clearance to every part's box (with brim and support spread)"


@dataclass(frozen=True)
class _Keep:
    """One area a tower must keep `need` mm from: a part's outline, its swept support spread or its probe mask."""

    name: str
    shape: L.Footprint
    need: float
    estimate: bool  # an estimated spread: the strict test only


@dataclass(frozen=True)
class FootprintClearance:
    """The tower against the parts' silhouettes (`layout.RasterFootprint`), as the pair rule tests two parts
    (`layout.pair_check`): the tower rect must stay `layout.LINE` (1 mm) from each part's outline and from that outline
    swept by the part's support spread per side (toward the tower, whichever side faces it), or from the probe's
    support mask when the part has one. Brims do not count: the T8 tower at (172, 150) sliced 2.4 mm from the motor
    cradle's box with its 5.2 mm brim (reference 06 section 10). A part with only a `BoxFootprint` falls back to the
    box test: its rect with brim and spread, plus PART_CLEARANCE.

    `strict` also holds the estimated spreads. The relaxed test (`relaxed()`) holds only outlines, probe masks and
    probe or spec spreads: on plate 14 T8 sliced inside the cradle's estimated 8 mm spread (measured: 0 mm on +y), so
    an estimate is a WARN for the tower, as between two parts. `plan_project` tries every size strict first."""

    keeps: tuple[_Keep, ...]
    strict: bool = True
    parts: tuple[L.Placed, ...] = ()

    @classmethod
    def of(cls, parts: Iterable[L.Placed], strict: bool = True) -> FootprintClearance:
        keeps: list[_Keep] = []
        ps = tuple(p for p in parts if p.placed)
        for p in ps:
            if p.footprint is None:
                keeps.append(_Keep(p.key, L.BoxFootprint.of(*_wh(p.rect())).translated(*p.rect()[:2]), PART_CLEARANCE, False))
                continue
            body = p.shape()
            keeps.append(_Keep(p.key, body, L.LINE, False))
            mask = p.mask_shape()
            if mask is not None:
                keeps.append(_Keep(p.key, mask, L.LINE, False))
            elif p.spread.most() > 0 and (strict or p.spread.hard):
                keeps.append(_Keep(p.key, body.grown(*p.spread.sides()), L.LINE, not p.spread.hard))
        return cls(tuple(keeps), strict, ps)

    def relaxed(self) -> FootprintClearance:
        return FootprintClearance.of(self.parts, strict=False)

    def narrow(self, x0: float, x1: float) -> FootprintClearance:
        r = CLEARANCE_CAP + PART_CLEARANCE
        return replace(self, keeps=tuple(k for k in self.keeps if k.shape.bbox()[2] + r > x0 and k.shape.bbox()[0] - r < x1))

    @staticmethod
    def _tower(rect: Rect) -> L.Footprint:
        return L.BoxFootprint.of(*_wh(rect)).translated(rect[0], rect[1])

    def _hits(self, rect: Rect) -> list[tuple[_Keep, float]]:
        t = self._tower(rect)
        out = []
        for k in self.keeps:
            g, _ = t.gap(k.shape, limit=k.need + 0.01)
            if g < k.need - 1e-9:
                out.append((k, g))
        return out

    def clear(self, rect: Rect) -> bool:
        t = self._tower(rect)
        return all(t.gap(k.shape, limit=k.need + 0.01)[0] >= k.need - 1e-9 for k in self.keeps)

    def gap(self, rect: Rect) -> float:
        t = self._tower(rect)
        return min((t.gap(k.shape, limit=CLEARANCE_CAP + k.need)[0] - (k.need - L.LINE) for k in self.keeps), default=CLEARANCE_CAP)

    def blocker(self, rect: Rect) -> tuple[float, str | None]:
        hits = [(_area(rect, k.shape.bbox()) + k.need - g, k.name) for k, g in self._hits(rect)]
        return (sum(a for a, _ in hits), max(hits)[1]) if hits else (0.0, None)

    def blocking(self, rect: Rect) -> list[tuple[str, Rect]]:
        out: dict[str, Rect] = {}
        for k, _ in self._hits(rect):
            out.setdefault(k.name, k.shape.bbox())
        return list(out.items())

    def estimates_within(self, rect: Rect) -> list[str]:
        """The parts whose estimated support spread `rect` comes within the line allowance of (WARN text)."""
        return list(dict.fromkeys(k.name for k, _ in self._hits(rect) if k.estimate))

    def describe(self) -> str:
        what = "outline and support spread" if self.strict else "outline and measured or spec support spread"
        return f"{L.LINE:g} mm (the line allowance) to every part's {what}"


def _wh(r: Rect) -> tuple[float, float]:
    return (r[2] - r[0], r[3] - r[1])


def as_clearance(obstacles: Sequence[tuple[str, Rect]] | TowerClearance, margin: float = PART_CLEARANCE) -> TowerClearance:
    if isinstance(obstacles, (list, tuple)):
        return BoxClearance(tuple(obstacles), margin)
    return obstacles  # type: ignore[return-value]


@dataclass(frozen=True)
class TowerPlacement:
    """The result of `auto_place`: a footprint, or None with the best blocked spot and the part that blocks it."""

    footprint: TowerFootprint | None
    tried: tuple[str, ...]  # one entry per settings tried, e.g. "41.4 mm (rib wall)"
    best: TowerFootprint | None = None
    blocker: str | None = None


def _origins(fw: float, fd: float, brim: float, area: Rect, step: float) -> Iterable[tuple[float, float]]:
    """Tower origins (x, y) on a `step` grid whose footprint lies inside `area`; none when the footprint is larger
    than the area (an inverted bound)."""
    x_lo, x_hi = math.ceil((area[0] + brim) / step - 1e-9) * step, area[2] - fw + brim
    y_lo, y_hi = math.ceil((area[1] + brim) / step - 1e-9) * step, area[3] - fd + brim
    if x_hi < x_lo - 1e-9 or y_hi < y_lo - 1e-9:
        return
    nx, ny = math.floor((x_hi - x_lo) / step + 1e-9), math.floor((y_hi - y_lo) / step + 1e-9)
    for i in range(nx + 1):
        for j in range(ny + 1):
            yield x_lo + i * step, y_lo + j * step


def find_spot(
    settings: TowerSettings,
    height: float,
    obstacles: Sequence[tuple[str, Rect]] | TowerClearance,
    area: Rect,
    clearance: float = PART_CLEARANCE,
    step: float = 1.0,
) -> tuple[TowerFootprint | None, TowerFootprint | None, str | None]:
    """(free footprint, best blocked footprint, its blocker). The footprint is recomputed from `settings` (a narrower
    tower is deeper, PartPlate.cpp:2255-2259). A spot is free when the footprint lies in `area` and `obstacles`
    (part rects, or any `TowerClearance`) clears it. Score: the clearance (capped at CLEARANCE_CAP, floored to 0.5 mm),
    then high y (Studio's own default sits at the back, y 250, PartPlate.cpp:69-70), then high x."""
    test = as_clearance(obstacles, clearance)
    fp0 = footprint(settings, height, 0.0, 0.0)
    fw, fd = fp0.outer
    best_free: tuple[tuple[float, float, float], TowerFootprint] | None = None
    best_blocked: tuple[tuple[float, float, float], TowerFootprint, str | None] | None = None
    column: tuple[float, TowerClearance] | None = None
    for x, y in _origins(fw, fd, fp0.brim, area, step):
        fp = replace(fp0, x=x, y=y)
        r = fp.rect
        if not inside(r, area):  # guard: never accept a footprint outside the reach or the bed
            continue
        if column is None or column[0] != x:
            narrow = getattr(test, "narrow", None)
            column = (x, narrow(r[0], r[2]) if narrow else test)
        local = column[1]
        if local.clear(r):
            key = (math.floor(min(local.gap(r), CLEARANCE_CAP) * 2) / 2, y, x)
            if best_free is None or key > best_free[0]:
                best_free = (key, fp)
        elif best_free is None:
            total, name = local.blocker(r)
            key_b = (-total, y, x)
            if best_blocked is None or key_b > best_blocked[0]:
                best_blocked = (key_b, fp, name)
    if best_free is not None:
        return best_free[1], None, None
    if best_blocked is not None:
        return None, best_blocked[1], best_blocked[2]
    return None, None, None


def candidates(settings: TowerSettings) -> list[TowerSettings]:
    """The process settings first, then the rib wall off with widths 35, 30, 25, 22 and 20 and a 2 mm brim."""
    out = [settings]
    for w in FALLBACK_WIDTHS:
        c = replace(settings, rib_wall=False, width=w, brim=FALLBACK_BRIM)
        if c not in out:
            out.append(c)
    return out


def _tried(s: TowerSettings, height: float) -> str:
    w, _ = s.size(height)
    return f"{w:.1f} mm" + (" (rib wall)" if s.rib_wall else " (no rib wall)")


def auto_place(
    settings: TowerSettings,
    height: float,
    obstacles: Sequence[tuple[str, Rect]] | TowerClearance,
    area: Rect,
    clearance: float = PART_CLEARANCE,
    step: float = 1.0,
    fallback: bool = True,
) -> TowerPlacement:
    """One plate on its own: the first settings in `candidates()` (or only `settings` without `fallback`) with a free
    spot. `plan_project` makes the choice for every plate together."""
    tried: list[str] = []
    best: TowerFootprint | None = None
    blocker: str | None = None
    for s in candidates(settings) if fallback else [settings]:
        fp, b, who = find_spot(s, height, obstacles, area, clearance, step)
        tried.append(_tried(s, height))
        if fp is not None:
            return TowerPlacement(fp, tuple(tried))
        if b is not None:
            best, blocker = b, who
    return TowerPlacement(None, tuple(tried), best, blocker)


# ---------------------------------------------------------------------------- the plan of one plate
@dataclass(frozen=True)
class TowerPlan:
    """What a plate's tower is: `mode` "auto" | "fixed" | "off". `footprint` is set for a placed tower; `problem`
    is a FAIL line for the plate's checks."""

    plate: int
    mode: str
    footprint: TowerFootprint | None = None
    problem: str | None = None
    warning: str | None = None

    @property
    def active(self) -> bool:
        return self.footprint is not None


def _fmt_rect(r: Rect) -> str:
    return f"x {r[0]:.1f}..{r[2]:.1f} y {r[1]:.1f}..{r[3]:.1f}"


def _alternative(settings: TowerSettings, height: float, test: TowerClearance, area: Rect, first_w: float) -> str:
    p = auto_place(settings, height, test, area)
    if p.footprint is None:
        return f'Free spots {BOTH} reach: none down to width {FALLBACK_WIDTHS[-1]:g}; set "wipe_tower": false or move parts'
    fp = p.footprint
    lead = "" if fp.settings == settings else f"none at {first_w:.1f} mm; "
    return f"Free spots {BOTH} reach: {lead}x {fp.x:.0f} y {fp.y:.0f} with {fp.settings.describe()}"


def check_fixed(
    plate: int, fp: TowerFootprint, obstacles: Sequence[tuple[str, Rect]] | TowerClearance, area: Rect, bed: Rect
) -> str | None:
    """The FAIL line for a tower at a fixed spot, or None when it is clear (the obstacle test clears it, inside the
    bed and inside the shared reach)."""
    test = as_clearance(obstacles)
    r = fp.rect
    why: list[str] = []
    hits = test.blocking(r)
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
        why.append(f"leaves the area {BOTH} reach ({_fmt_rect(area)})")
    if not why:
        return None
    first_w = fp.outer[0] - 2 * fp.brim
    alt = _alternative(fp.settings, fp.height, test, area, first_w)
    return f"plate {plate} {fp.describe()}: {' and '.join(why)} ({test.describe()}). {alt}. Set plates[{plate - 1}].wipe_tower."


def _fail_auto(
    plate: int, area: Rect, tried: Sequence[str], best: TowerFootprint | None, blocker: str | None, how: str, why: str = ""
) -> str:
    where = ""
    if best is not None:
        where = f"; the best spot ({best.x:.0f}, {best.y:.0f}) at {best.settings.describe()}" + (
            f" is blocked by {blocker}" if blocker else ""
        )
    return (
        f"plate {plate}: no free prime tower spot {BOTH} reach ({_fmt_rect(area)}), tried "
        + ", ".join(tried)
        + f" with {how}{where}{why}. Move parts, set plates[{plate - 1}].wipe_tower "
        '{"x", "y", "width", "rib_wall", "brim"}, or set "wipe_tower": false'
    )


# ---------------------------------------------------------------------------- every plate together
@dataclass(frozen=True)
class TowerRequest:
    """One plate's tower question. `settings` is the process tower with the plate's own filament count (the volume
    term); `obstacles` the plate's part rects (with brim and spread); `clearance` overrides the box test
    (`FootprintClearance`, the silhouettes)."""

    plate: int
    mode: str  # auto | fixed | off
    settings: TowerSettings
    height: float
    obstacles: tuple[tuple[str, Rect], ...]
    area: Rect
    bed: Rect
    x: float | None = None
    y: float | None = None
    clearance: TowerClearance | None = None

    def test(self) -> TowerClearance:
        return self.clearance if self.clearance is not None else as_clearance(self.obstacles)

    def relaxed(self) -> TowerClearance | None:
        """The test without estimated spreads (None: the test has no relaxed form)."""
        rel = getattr(self.test(), "relaxed", None)
        return rel() if rel is not None else None


def _size_key(s: TowerSettings) -> tuple[float, bool, float]:
    return (s.width, s.rib_wall, s.brim)


def _with_size(s: TowerSettings, key: tuple[float, bool, float]) -> TowerSettings:
    return replace(s, width=key[0], rib_wall=key[1], brim=key[2])


Found = dict[int, tuple[TowerFootprint | None, TowerFootprint | None, str | None]]


def _search(
    autos: Sequence[TowerRequest], keys: Sequence[tuple[float, bool, float]], tests: Mapping[int, TowerClearance]
) -> tuple[Found, list[str]]:
    """The first size key every plate finds a spot for, with each plate's own test; else the last key's results."""
    tried: list[str] = []
    found: Found = {}
    for key in keys:
        found = {r.plate: find_spot(_with_size(r.settings, key), r.height, tests[r.plate], r.area) for r in autos}
        tried.append(_tried(_with_size(autos[0].settings, key), max(r.height for r in autos)))
        if all(f[0] is not None for f in found.values()):
            break
    return found, tried


def plan_project(requests: Sequence[TowerRequest], fallback: bool = True) -> list[TowerPlan]:
    """The towers of every plate with ONE size configuration (width, rib wall and brim are process-global in Studio).
    A fixed tower pins the size. Otherwise the configurations of `candidates()` are tried in order, and the first one
    every "auto" plate finds a spot for wins; each step recomputes the footprint per plate (its height and filament
    count). Every size is tried with the strict test first, then (when no size fits) with the relaxed one: a tower
    placed inside an estimated support spread gets a WARN. When none fits every plate, the plates that block the last
    configuration FAIL and say so."""
    out: dict[int, TowerPlan] = {}
    for r in requests:
        if r.mode == "off":
            out[r.plate] = TowerPlan(r.plate, "off")
        elif r.mode == "fixed" and r.x is not None and r.y is not None:
            fp = footprint(r.settings, r.height, r.x, r.y)
            # The user's spot: the outlines and measured spreads decide (FAIL), an estimated spread is a WARN
            problem = check_fixed(r.plate, fp, r.relaxed() or r.test(), r.area, r.bed)
            out[r.plate] = TowerPlan(r.plate, "fixed", fp, problem, None if problem else _estimate_warning(r, fp))
    autos = [r for r in requests if r.plate not in out]
    if autos:
        fixed = [r for r in requests if r.mode == "fixed"]
        if fixed:
            keys = [_size_key(fixed[0].settings)]
        elif fallback:
            keys = [_size_key(s) for s in candidates(autos[0].settings)]
        else:
            keys = [_size_key(autos[0].settings)]
        strict = {r.plate: r.test() for r in autos}
        found, tried = _search(autos, keys, strict)
        tests = strict
        if not all(f[0] is not None for f in found.values()):
            relaxed = {r.plate: r.relaxed() or strict[r.plate] for r in autos}
            if any(relaxed[n] is not strict[n] for n in relaxed):
                found, tried = _search(autos, keys, relaxed)
                tests = relaxed
        ok = [n for n, f in found.items() if f[0] is not None]
        for r in autos:
            spot, best, who = found[r.plate]
            if spot is not None:
                out[r.plate] = TowerPlan(r.plate, "auto", spot, warning=_estimate_warning(r, spot))
                continue
            why = ""
            if fixed:
                why = f" (the size is pinned by plate {fixed[0].plate}'s wipe_tower: one size per project)"
            elif ok:
                why = f" (one tower size per project: plates {ok} fit, plate {r.plate} blocks every size)"
            how = tests[r.plate].describe()
            out[r.plate] = TowerPlan(r.plate, "auto", None, problem=_fail_auto(r.plate, r.area, tried, best, who, how, why))
    return [out[r.plate] for r in requests]


def _estimate_warning(r: TowerRequest, fp: TowerFootprint) -> str | None:
    """The WARN line for a tower inside an estimated support spread (the strict test blocks it, the relaxed one not)."""
    within = getattr(r.test(), "estimates_within", None)
    names = within(fp.rect) if within is not None else []
    if not names:
        return None
    return (
        f"plate {r.plate} prime tower at ({fp.x:.1f}, {fp.y:.1f}): within the estimated support spread of {', '.join(names)}, "
        "clear of the outlines; the slice decides (`--probe-supports` measures the spread)"
    )


def off_warning(plate: int) -> str:
    return (
        f"plate {plate} wipe_tower false: two filaments with no prime tower; Studio slices it (exit 0 measured) "
        "but the idle extruder is not wiped before the interface layers"
    )


@dataclass(frozen=True)
class TowerClear:
    """Rule: the plate's tower plan has no problem. One instance per plate (it carries that plate's plan); build.py
    runs it after the limit checker. Kind "tower"; a tower inside an estimated support spread adds a WARN line (kind
    "margin")."""

    plan: TowerPlan
    kind: str = "tower"

    def check(self, placed, ctx) -> Iterable:
        if self.plan.problem:
            yield Check(False, self.plan.problem, self.kind)
        elif self.plan.footprint is not None:
            fp = self.plan.footprint
            yield Check(True, f"plate {self.plan.plate} {fp.describe()}, clear of every part ({self.plan.mode})", self.kind)
            if self.plan.warning:
                yield Check(True, self.plan.warning, "margin")  # an estimate: a WARN, as between two parts


def smooth_timelapse(process: Mapping) -> bool:
    """`timelapse_type` 1 (Smooth) needs a prime tower even on a one-filament plate (PartPlate.cpp:2222-2223)."""
    return str(process.get("timelapse_type", "0")).strip().lower() in ("1", "smooth")
