"""`shambu cad fit`: gauge a declared hole or insert pocket by ray casting, and judge it against its fit.

The gauge, for a hole declared by its mouth point, its axis (pointing into the material) and its nominal diameter:

1. Step along the axis from the mouth in 0.1 mm steps. At each depth cast 48 radial rays (`meshgeom.RayGrid`) from
   the axis point. A depth is "walled" when the point is in air (most first hits face back at it), at least 75 % of
   the rays hit within 5 % of a circle fitted to the hits, and that circle's diameter is near the reference. The hole
   is found at the first ring within 25 % of nominal (so a wrong-size hole reads as `fit.diameter`, not as missing);
   from there the reference is that ring's diameter and the band 5 %. The 5 % band is what stops the depth at a step
   into a smaller relief bore (a 4.5 mm relief under a 5.8 mm pocket is 22 % smaller); a wider band once let the
   relief count as pocket depth.
2. The first non-walled depth after the walled run is refined by bisection to 0.01 mm, and so is the entry (the
   first walled depth): a mouth point declared in the air above the surface adds no depth. The depth is from the
   entry to the end.
3. The diameter is refitted on the mesh vertices of the walled band (they lie on the CAD surface; ray hits on facet
   chords read small). The fitted centre against the declared axis gives the offset, and the nearest and farthest
   band vertex from that centre give the narrowest and widest diameters (an oval bore).
4. A ray along the axis from the last walled depth finds the floor (none: a through hole). Rays continued outward
   from the wall hits give the thinnest wall.

The judgement, from the fit (fits.py): the narrowest and the widest diameter within the band (a 3.9 x 4.1 mm bore
does not pass a 4.00 mm fit on its 4.0 mm average), full-diameter depth at least `depth_min_mm`, wall at least
`wall_min_mm` (a wall the rays could not measure is `cad.incomplete.wall_unmeasured`, never a pass), offset at most
`offset_max_mm`. `fit.coupon` (INFO) for an insert, press or slip fit: the first
print of a new fit is a coupon in the feature's own orientation. `fit.horizontal` (INFO, observed) for a bore that
prints horizontal, with the allowance from the spec's intent.

What it does not do: no automatic search for holes (features.py does that), no thread or taper gauge, no printed
size (a mesh is the CAD size), no file reading and no printing.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from .. import meshgeom as G
from ..findings import FAIL, INFO, MEASURED, OBSERVED, Finding, Fix, Location
from . import linalg as L
from .features import HOLE_WARNING, print_axis
from .fits import COUPON_KINDS, Fit
from .linalg import Facet, Vec

STEP_MM = 0.1
RESOLUTION_MM = 0.01
RAYS = 48
WALLED_SHARE = 0.75
RADIUS_BAND = 0.05
FIND_BAND = 0.25
START_SEARCH_MM = 1.0  # the mouth point may sit up to this far outside the hole (a chamfer, an export rounding)


@dataclass(frozen=True)
class Bore:
    walled_from_mm: float
    depth_mm: float  # full-diameter depth from the mouth
    diameter_mm: float
    centre: Vec  # the fitted axis point in the mouth plane
    offset_mm: float
    wall_mm: float | None
    floor_mm: float | None
    ends: str  # "through", "blind" or "step" (a narrower bore continues)
    residual_mm: float
    vertices: int
    diameter_min_mm: float | None = None  # the narrowest and widest across the band vertices (None: not measured)
    diameter_max_mm: float | None = None

    def to_json(self) -> dict:
        return {
            "walled_from_mm": round(self.walled_from_mm, 3),
            "depth_mm": round(self.depth_mm, 3),
            "diameter_mm": round(self.diameter_mm, 4),
            "centre": [round(x, 4) + 0.0 for x in self.centre],
            "offset_mm": round(self.offset_mm, 4),
            "wall_mm": round(self.wall_mm, 3) if self.wall_mm is not None else None,
            "floor_mm": round(self.floor_mm, 3) if self.floor_mm is not None else None,
            "ends": self.ends,
            "residual_mm": round(self.residual_mm, 5),
            "vertices": self.vertices,
            "diameter_min_mm": round(self.diameter_min_mm, 4) if self.diameter_min_mm is not None else None,
            "diameter_max_mm": round(self.diameter_max_mm, 4) if self.diameter_max_mm is not None else None,
        }


class Gauge:
    """The ray grid and welded vertices of one part, shared by every hole gauged on it."""

    def __init__(self, facets: Sequence[Facet]):
        self.facets = facets
        self.grid = G.RayGrid(facets)
        self.verts = G.weld(facets).verts
        lo, hi = self.grid.lo, self.grid.hi
        self.diagonal = math.dist(lo, hi)

    def _normal(self, fid: int) -> Vec:
        a, b, c = self.facets[fid]
        n = L.cross(L.sub(b, a), L.sub(c, a))
        ln = L.norm(n)
        return L.mul(n, 1 / ln) if ln > 0 else (0.0, 0.0, 0.0)

    def _ring(self, p: Vec, u: Vec, v: Vec) -> list[tuple[Vec, Vec, float, int]]:
        out = []
        for k in range(RAYS):
            t = 2 * math.pi * k / RAYS
            d = L.add(L.mul(u, math.cos(t)), L.mul(v, math.sin(t)))
            hit = self.grid.first_hit(p, d, t_min=1e-6)
            if hit is not None:
                out.append((d, L.add(p, L.mul(d, hit[0])), hit[0], hit[1]))
        return out

    def _walled(
        self, p: Vec, u: Vec, v: Vec, d_ref: float, band: float = RADIUS_BAND
    ) -> tuple[bool, tuple[float, float, float] | None, list]:
        hits = self._ring(p, u, v)
        if len(hits) < WALLED_SHARE * RAYS:
            return False, None, hits
        facing_back = sum(1 for d, _, _, fid in hits if L.dot(self._normal(fid), d) < 0)
        if facing_back <= len(hits) / 2:
            return False, None, hits  # the axis point is inside material
        try:
            cu, cv, r, _ = L.fit_circle([(L.dot(h, u), L.dot(h, v)) for _, h, _, _ in hits])
        except ValueError:
            return False, None, hits
        if abs(2 * r - d_ref) > band * d_ref:
            return False, (cu, cv, r), hits
        near = sum(1 for _, h, _, _ in hits if abs(math.hypot(L.dot(h, u) - cu, L.dot(h, v) - cv) - r) <= RADIUS_BAND * r)
        return near >= WALLED_SHARE * RAYS, (cu, cv, r), hits

    def bore(self, mouth: Vec, axis: Vec, d_nom: float) -> Bore | None:
        """The bore at `mouth` along `axis` (into the material), or None when no hole of about `d_nom` is there."""
        a = L.unit(axis)
        u, v = L.basis(a)

        def at(t: float) -> Vec:
            return L.add(mouth, L.mul(a, t))

        t = STEP_MM / 2
        start = None
        d_ref = d_nom
        while t <= START_SEARCH_MM:
            ok, found, _ = self._walled(at(t), u, v, d_nom, FIND_BAND)
            if ok and found is not None:
                start, d_ref = t, 2 * found[2]
                break
            t += STEP_MM
        if start is None:
            return None
        lo_t, hi_t = start - STEP_MM, start
        if self._walled(at(lo_t), u, v, d_ref)[0]:
            entry = 0.0  # walled before the first sample too: the mouth point sits inside the bore
        else:
            while hi_t - lo_t > RESOLUTION_MM:
                mid = (lo_t + hi_t) / 2
                if self._walled(at(mid), u, v, d_ref)[0]:
                    hi_t = mid
                else:
                    lo_t = mid
            entry = hi_t
        last = start
        t_max = self.diagonal + START_SEARCH_MM
        walls: list[float] = []
        fits: list[tuple[float, float, float]] = []
        t = start
        while t <= t_max:
            ok, fit, hits = self._walled(at(t), u, v, d_ref)
            if not ok:
                break
            last = t
            if fit is not None:
                fits.append(fit)
            if len(walls) < 2000 and round(t / STEP_MM) % 10 == 0:  # the wall every 1 mm of depth
                walls += self._walls(hits)
            t += STEP_MM
        lo_t, hi_t = last, min(t, t_max)
        while hi_t - lo_t > RESOLUTION_MM:
            mid = (lo_t + hi_t) / 2
            if self._walled(at(mid), u, v, d_ref)[0]:
                lo_t = mid
            else:
                hi_t = mid
        end = lo_t
        if not walls:
            walls += self._walls(self._ring(at((start + end) / 2), u, v))
        cu = sum(f[0] for f in fits) / len(fits)
        cv = sum(f[1] for f in fits) / len(fits)
        r_rays = sum(f[2] for f in fits) / len(fits)
        mouth_t = L.dot(mouth, a)
        band = [
            p
            for p in self.verts
            if entry - RESOLUTION_MM - 1e-6 <= L.dot(p, a) - mouth_t <= end + RESOLUTION_MM
            and abs(math.hypot(L.dot(p, u) - cu, L.dot(p, v) - cv) - r_rays) <= RADIUS_BAND * r_rays
        ]
        d_min: float | None
        d_max: float | None
        try:
            cu2, cv2, r2, rms = L.fit_circle([(L.dot(p, u), L.dot(p, v)) for p in band])
            radii = [math.hypot(L.dot(p, u) - cu2, L.dot(p, v) - cv2) for p in band]
            d_min, d_max = 2 * min(radii), 2 * max(radii)
        except ValueError:
            cu2, cv2, r2, rms = cu, cv, r_rays, math.nan
            d_min = d_max = None
        centre = L.add(L.add(L.mul(u, cu2), L.mul(v, cv2)), L.mul(a, mouth_t))
        offset = math.hypot(cu2 - L.dot(mouth, u), cv2 - L.dot(mouth, v))
        probe = max(start, end - STEP_MM / 2)  # a point inside the walled band, clear of a floor at `end`
        floor_hit = self.grid.first_hit(at(probe), a, t_min=1e-6)
        floor = probe + floor_hit[0] if floor_hit is not None else None
        ends = "through" if floor is None else ("blind" if floor - end <= 2 * RESOLUTION_MM else "step")
        return Bore(entry, end - entry, 2 * r2, centre, offset, min(walls) if walls else None, floor, ends, rms, len(band), d_min, d_max)

    def _walls(self, hits: Sequence[tuple[Vec, Vec, float, int]]) -> list[float]:
        out = []
        for d, h, _, fid in hits:
            nxt = self.grid.first_hit(h, d, skip=(fid,), t_min=1e-6)
            if nxt is not None:
                out.append(nxt[0])
        return out


def judge(
    part: str,
    hole: str,
    fit: Fit,
    bore: Bore | None,
    axis: Vec,
    mouth: Vec,
    offset_max_mm: float | None,
    bed_face: Vec | None,
    allowance: dict,
) -> list[Finding]:
    """The fit findings of one gauged hole."""
    where = Location("part", mouth)
    expected = f"fit table: {fit.basis}" if fit.basis else "fit table"
    if bore is None:
        return [
            Finding(
                "fit.not_found",
                FAIL,
                "fit",
                f"{hole}: no {fit.hole_mm:g} mm hole at the mouth along the declared axis",
                MEASURED,
                part,
                where,
                {"fit": fit.name, "hole_mm": fit.hole_mm, "expected_from": expected},
                Fix("check the mouth point and the axis in the spec, then the model"),
            )
        ]
    fs: list[Finding] = []
    lo, hi = fit.hole_mm + fit.band_mm[0], fit.hole_mm + fit.band_mm[1]
    m = {"fit": fit.name, "diameter_mm": bore.diameter_mm, "expected_from": expected}
    d_min = bore.diameter_min_mm if bore.diameter_min_mm is not None else bore.diameter_mm
    d_max = bore.diameter_max_mm if bore.diameter_max_mm is not None else bore.diameter_mm
    if not lo <= bore.diameter_mm <= hi or d_min < lo or d_max > hi:
        oval = f" (narrowest {d_min:.3f}, widest {d_max:.3f})" if d_max - d_min > 1e-3 else ""
        fs.append(
            Finding(
                "fit.diameter",
                FAIL,
                "fit",
                f"{hole}: d {bore.diameter_mm:.3f} mm{oval}, {fit.name} wants {lo:.3f} to {hi:.3f}",
                MEASURED,
                part,
                where,
                {**m, "diameter_min_mm": d_min, "diameter_max_mm": d_max, "band_mm": [lo, hi]},
                Fix(f"set the hole to {fit.hole_mm:g} mm in the parameter table"),
            )
        )
    if fit.depth_min_mm is not None and bore.depth_mm < fit.depth_min_mm:
        fs.append(
            Finding(
                "fit.depth",
                FAIL,
                "fit",
                f"{hole}: full-diameter depth {bore.depth_mm:.2f} mm, {fit.name} needs {fit.depth_min_mm:g}",
                MEASURED,
                part,
                where,
                {**m, "depth_mm": bore.depth_mm, "depth_min_mm": fit.depth_min_mm},
                Fix("deepen the pocket, or move the relief bore below it"),
            )
        )
    if fit.wall_min_mm is not None and bore.wall_mm is None:
        fs.append(
            Finding(
                "cad.incomplete.wall_unmeasured",
                FAIL,
                "fit",
                f"{hole}: {fit.name} needs a {fit.wall_min_mm:g} mm wall and no ray found the outside surface, so the wall is unknown",
                MEASURED,
                part,
                where,
                {**m, "wall_min_mm": fit.wall_min_mm},
                Fix("check that the part is a closed solid around the hole; a wall is never assumed"),
            )
        )
    if fit.wall_min_mm is not None and bore.wall_mm is not None and bore.wall_mm < fit.wall_min_mm:
        fs.append(
            Finding(
                "fit.wall",
                FAIL,
                "fit",
                f"{hole}: wall {bore.wall_mm:.2f} mm, {fit.name} needs {fit.wall_min_mm:g}",
                MEASURED,
                part,
                where,
                {**m, "wall_mm": bore.wall_mm, "wall_min_mm": fit.wall_min_mm},
                Fix("thicken the boss or move the hole away from the edge"),
            )
        )
    off_max = offset_max_mm if offset_max_mm is not None else fit.offset_max_mm
    if off_max is not None and bore.offset_mm > off_max:
        fs.append(
            Finding(
                "fit.offset",
                FAIL,
                "fit",
                f"{hole}: axis {bore.offset_mm:.3f} mm from the declared one (at most {off_max:g})",
                MEASURED,
                part,
                where,
                {**m, "offset_mm": bore.offset_mm, "offset_max_mm": off_max},
                Fix("move the hole to its declared position, or correct the spec's mouth point"),
            )
        )
    if fit.kind in COUPON_KINDS:
        fs.append(
            Finding(
                "fit.coupon",
                INFO,
                "fit",
                f"{hole}: a new {fit.kind} fit; print a coupon of it in the feature's own orientation before the part",
                MEASURED,
                part,
                where,
                {"fit": fit.name, "kind": fit.kind},
            )
        )
    if print_axis(L.canonical_axis(L.unit(axis)), bed_face) == "horizontal":
        allow = allowance.get("horizontal")
        fs.append(
            Finding(
                "fit.horizontal",
                INFO,
                "fit",
                f"{hole} prints horizontal: {HOLE_WARNING}"
                + (f"; the spec's horizontal allowance is {allow} mm" if allow is not None else "; the spec gives no horizontal allowance"),
                OBSERVED,
                part,
                where,
                {"fit": fit.name, "allowance_mm": allow},
            )
        )
    return fs
