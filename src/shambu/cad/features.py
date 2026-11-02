"""`shambu cad features`: the hole and boss inventory of an exported part, read from the mesh in pure Python.

Detection:

1. Weld the facets (meshgeom) and grow smooth regions across edges whose dihedral angle is under the axis tolerance
   (default: the record's export `angle_tol_deg` + 1, else 7 degrees; ClassCAD's STL export default is 6 degrees, so
   two neighbours on a tessellated cylinder never differ by more).
2. Per region, the cylinder axis is the eigenvector of the smallest eigenvalue of the area-weighted normal covariance.
   A region whose smallest eigenvalue is above 0.02 is not a cylinder (the normals leave the plane normal to the axis).
   A region whose normals turn less than 270 degrees about the axis is a fillet, a rounded edge or a slot end, not a
   hole or a boss.
3. The region's vertices, projected onto the plane normal to the axis, get an algebraic circle fit. STL vertices lie
   on the CAD surface, so the fitted radius is the CAD radius to rounding, not the facet's inscribed radius; the RMS
   residual is reported.
4. Normals pointing at the axis: a hole; pointing away: a boss. Depth is the extent along the axis. For a hole, a ray
   along the axis each way from the mid-point finds the ends: no hit within half the depth (plus 5 % of the diameter,
   at least 0.1 mm) is an open end. Both open: `through`; one: `blind`.
5. Features of one kind with the same diameter (within 0.01 mm) and parallel axes form a pattern. Three or more get a
   pitch-circle diameter and their angles in the plane basis of `linalg.basis` (a mirrored part shows as other angles).

What it does not do: no cone, thread, slot or counterbore recognition (a counterbore reads as two coaxial holes), no
feature that turns less than 270 degrees, no file reading and no printing.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from .. import meshgeom as G
from ..findings import FAIL, INFO, MEASURED, OBSERVED, WARN, Finding, Fix, Location
from . import linalg as L
from .linalg import Facet, Vec
from .spec import ExpectedFeature

DEFAULT_AXIS_TOL_DEG = 7.0
CYLINDER_EIGEN_MAX = 0.02
MIN_TURN_DEG = 270.0
MIN_FACETS = 6
SAME_DIAMETER_MM = 0.01
PARALLEL_COS = 0.9999
HOLE_WARNING = "holes print undersize: about 0.1 mm vertical, 0.2 to 0.3 mm horizontal"


@dataclass(frozen=True)
class Feature:
    kind: str  # "hole" or "boss"
    diameter_mm: float
    axis: Vec
    centre: Vec
    depth_mm: float
    ends: str | None  # "through", "blind", "closed" (holes) or None (bosses)
    residual_mm: float
    facets: int
    print_axis: str = "unknown"  # "vertical", "horizontal", "inclined" against the bed face, else "unknown"
    pattern: str | None = None

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "diameter_mm": round(self.diameter_mm, 4),
            "axis": [round(x, 4) + 0.0 for x in self.axis],
            "centre": [round(x, 4) + 0.0 for x in self.centre],
            "depth_mm": round(self.depth_mm, 4),
            "ends": self.ends,
            "residual_mm": round(self.residual_mm, 5),
            "facets": self.facets,
            "print_axis": self.print_axis,
            "pattern": self.pattern,
        }


@dataclass(frozen=True)
class Pattern:
    id: str
    kind: str
    diameter_mm: float
    axis: Vec
    members: tuple[int, ...]
    pcd_mm: float | None = None
    centre: Vec | None = None
    angles_deg: tuple[float, ...] = ()
    spacing_mm: tuple[float, ...] = ()

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "diameter_mm": round(self.diameter_mm, 4),
            "count": len(self.members),
            "axis": [round(x, 4) + 0.0 for x in self.axis],
            "pcd_mm": round(self.pcd_mm, 4) if self.pcd_mm is not None else None,
            "centre": [round(x, 4) + 0.0 for x in self.centre] if self.centre is not None else None,
            "angles_deg": [round(a, 3) for a in self.angles_deg],
            "spacing_mm": [round(s, 4) for s in self.spacing_mm],
        }


@dataclass
class Inventory:
    features: list[Feature]
    patterns: list[Pattern] = field(default_factory=list)
    angle_tol_deg: float = DEFAULT_AXIS_TOL_DEG

    def to_json(self) -> dict:
        return {
            "angle_tol_deg": self.angle_tol_deg,
            "features": [f.to_json() for f in self.features],
            "patterns": [p.to_json() for p in self.patterns],
        }


def print_axis(axis: Vec, bed_face: Vec | None) -> str:
    if bed_face is None:
        return "unknown"
    c = abs(L.dot(axis, L.unit(bed_face)))
    if c > math.cos(math.radians(10)):
        return "vertical"
    if c < math.sin(math.radians(10)):
        return "horizontal"
    return "inclined"


def _regions(facets: Sequence[Facet], w: G.Welded, normals: list[Vec], tol_deg: float) -> list[list[int]]:
    cos_tol = math.cos(math.radians(tol_deg))
    links = []
    for users in w.edge_map().values():
        if len(users) == 2:
            a, b = users
            if L.dot(normals[a], normals[b]) >= cos_tol:
                links.append((a, b))
    # Union-find on the smooth links only: meshgeom.regions would join across every shared edge, sharp ones too.
    parent = list(range(len(facets)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in links:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    groups: dict[int, list[int]] = {}
    for f in range(len(facets)):
        if not w.degenerate(f):
            groups.setdefault(find(f), []).append(f)
    return sorted(groups.values(), key=lambda g: g[0])


def detect(
    facets: Sequence[Facet], angle_tol_deg: float = DEFAULT_AXIS_TOL_DEG, min_diameter_mm: float = 0.0, bed_face: Vec | None = None
) -> Inventory:
    """Every hole and boss of the mesh, sorted by kind, diameter and centre, with their patterns."""
    w = G.weld(facets)
    normals: list[Vec] = []
    areas: list[float] = []
    for a, b, c in facets:
        n = L.cross(L.sub(b, a), L.sub(c, a))
        ln = L.norm(n)
        normals.append(L.mul(n, 1 / ln) if ln > 0 else (0.0, 0.0, 0.0))
        areas.append(ln / 2)
    grid: G.RayGrid | None = None
    found: list[Feature] = []
    for reg in _regions(facets, w, normals, angle_tol_deg):
        if len(reg) < MIN_FACETS:
            continue
        total = sum(areas[f] for f in reg)
        if total <= 0:
            continue
        m = [[sum(areas[f] * normals[f][i] * normals[f][j] for f in reg) / total for j in range(3)] for i in range(3)]
        lam, axis = L.smallest_eigenvector(m)
        if lam > CYLINDER_EIGEN_MAX:
            continue
        axis = L.canonical_axis(axis)
        u, v = L.basis(axis)
        angs = []
        for f in reg:
            pu, pv = L.dot(normals[f], u), L.dot(normals[f], v)
            if math.hypot(pu, pv) > 0.5:
                angs.append(math.degrees(math.atan2(pv, pu)) % 360)
        if len(angs) < MIN_FACETS:
            continue
        angs.sort()
        gaps = [angs[i + 1] - angs[i] for i in range(len(angs) - 1)] + [angs[0] + 360 - angs[-1]]
        if 360 - max(gaps) < MIN_TURN_DEG:
            continue
        vids = sorted({vi for f in reg for vi in w.tris[f]})
        pts = [w.verts[i] for i in vids]
        try:
            cu, cv, r, rms = L.fit_circle([(L.dot(p, u), L.dot(p, v)) for p in pts])
        except ValueError:
            continue
        if 2 * r < min_diameter_mm:
            continue
        ts = [L.dot(p, axis) for p in pts]
        t0, t1 = min(ts), max(ts)
        centre = L.add(L.add(L.mul(u, cu), L.mul(v, cv)), L.mul(axis, (t0 + t1) / 2))
        side = 0.0
        for f in reg:
            cf = tuple(sum(facets[f][k][i] for k in range(3)) / 3 for i in range(3))
            radial = L.sub(cf, centre)  # type: ignore[arg-type]
            radial = L.sub(radial, L.mul(axis, L.dot(radial, axis)))
            side += areas[f] * L.dot(normals[f], radial)
        kind = "hole" if side < 0 else "boss"
        ends = None
        depth = t1 - t0
        if kind == "hole":
            if grid is None:
                grid = G.RayGrid(facets)
            reach = depth / 2 + max(0.1, 0.05 * 2 * r)
            closed = 0
            for d in (axis, L.mul(axis, -1.0)):
                hit = grid.first_hit(centre, d, skip=reg, t_min=1e-6)
                if hit is not None and hit[0] <= reach:
                    closed += 1
            ends = ("through", "blind", "closed")[closed]
        found.append(Feature(kind, 2 * r, axis, centre, depth, ends, rms, len(reg), print_axis(axis, bed_face)))
    found.sort(key=lambda f: (f.kind, round(f.diameter_mm, 3), tuple(round(x, 3) for x in f.centre)))
    patterns = _patterns(found)
    named = list(found)
    for p in patterns:
        for i in p.members:
            named[i] = Feature(**{**named[i].__dict__, "pattern": p.id})
    return Inventory(named, patterns, angle_tol_deg)


def _patterns(found: Sequence[Feature]) -> list[Pattern]:
    groups: list[list[int]] = []
    for i, f in enumerate(found):
        for g in groups:
            h = found[g[0]]
            if h.kind == f.kind and abs(h.diameter_mm - f.diameter_mm) <= SAME_DIAMETER_MM and abs(L.dot(h.axis, f.axis)) >= PARALLEL_COS:
                g.append(i)
                break
        else:
            groups.append([i])
    out = []
    for k, g in enumerate(x for x in groups if len(x) >= 2):
        h = found[g[0]]
        u, v = L.basis(h.axis)
        pts2 = [(L.dot(found[i].centre, u), L.dot(found[i].centre, v)) for i in g]
        pid = f"{h.kind}-{k + 1}"
        if len(g) >= 3:
            try:
                cu, cv, r, rms = L.fit_circle(pts2)
            except ValueError:
                r, rms = math.inf, math.inf
            if math.isfinite(r) and rms < 1e-3 * max(r, 1.0):
                angles = tuple(sorted(math.degrees(math.atan2(p[1] - cv, p[0] - cu)) % 360 for p in pts2))
                t = sum(L.dot(found[i].centre, h.axis) for i in g) / len(g)
                centre = L.add(L.add(L.mul(u, cu), L.mul(v, cv)), L.mul(h.axis, t))
                out.append(Pattern(pid, h.kind, h.diameter_mm, h.axis, tuple(g), 2 * r, centre, angles))
                continue
        spacing = tuple(math.dist(found[g[i]].centre, found[g[i + 1]].centre) for i in range(len(g) - 1))
        out.append(Pattern(pid, h.kind, h.diameter_mm, h.axis, tuple(g), spacing_mm=spacing))
    return out


def _angles_match(want: Sequence[float], got: Sequence[float], tol: float) -> bool:
    if len(want) != len(got):
        return False
    left = list(got)
    for a in want:
        best = min(left, key=lambda b: abs((a - b + 180) % 360 - 180), default=None)
        if best is None or abs((a - best + 180) % 360 - 180) > tol:
            return False
        left.remove(best)
    return True


def compare(name: str, inv: Inventory, expected: Sequence[ExpectedFeature], frame: str = "part") -> list[Finding]:
    """`feature.missing` (FAIL) and `feature.extra` (WARN) against the expected inventory, plus
    `feature.horizontal_bore` (INFO, observed) for every hole that prints horizontal."""
    fs: list[Finding] = []
    used: set[int] = set()
    for e in expected:
        cands = [
            i
            for i, f in enumerate(inv.features)
            if f.kind == e.kind
            and abs(f.diameter_mm - e.diameter_mm) <= e.diameter_tol_mm
            and (e.axis is None or abs(L.dot(f.axis, e.axis)) >= PARALLEL_COS)
        ]
        used |= set(cands)
        label = f"{e.count} x {e.kind} d {e.diameter_mm:g}"
        if len(cands) < e.count:
            fs.append(
                Finding(
                    "feature.missing",
                    FAIL,
                    "features",
                    f"expected {label}, found {len(cands)}",
                    MEASURED,
                    name,
                    measure={"expected_count": e.count, "found_count": len(cands), "diameter_mm": e.diameter_mm, "expected_from": "spec"},
                    fix=Fix("compare the model with the parameter table: a cut or a pattern instance did not take"),
                )
            )
        elif len(cands) > e.count:
            fs.append(
                Finding(
                    "feature.extra",
                    WARN,
                    "features",
                    f"expected {label}, found {len(cands)}",
                    MEASURED,
                    name,
                    measure={"expected_count": e.count, "found_count": len(cands), "diameter_mm": e.diameter_mm, "expected_from": "spec"},
                )
            )
        if (e.pcd_mm is not None or e.angles_deg) and len(cands) == e.count:
            pat = next((p for p in inv.patterns if set(p.members) == set(cands)), None)
            pcd_ok = pat is not None and pat.pcd_mm is not None and (e.pcd_mm is None or abs(pat.pcd_mm - e.pcd_mm) <= e.diameter_tol_mm)
            ang_ok = pat is not None and (not e.angles_deg or _angles_match(e.angles_deg, pat.angles_deg, e.angle_tol_deg))
            if not (pcd_ok and ang_ok):
                got = (
                    f"pcd {pat.pcd_mm:.3f}, angles {[round(a, 2) for a in pat.angles_deg]}"
                    if pat is not None and pat.pcd_mm is not None
                    else "no circular pattern"
                )
                fs.append(
                    Finding(
                        "feature.missing",
                        FAIL,
                        "features",
                        f"expected {label} on pcd {e.pcd_mm} at {list(e.angles_deg or [])} deg; found {got} (a mirrored or rotated pattern?)",
                        MEASURED,
                        name,
                        measure={"expected_pcd_mm": e.pcd_mm, "expected_angles_deg": list(e.angles_deg or []), "expected_from": "spec"},
                        fix=Fix("check the pattern's handedness and start angle against the drawing"),
                    )
                )
    if expected:
        for i, f in enumerate(inv.features):
            if i not in used:
                fs.append(
                    Finding(
                        "feature.extra",
                        WARN,
                        "features",
                        f"{f.kind} d {f.diameter_mm:.3f} is not in the expected inventory",
                        MEASURED,
                        name,
                        Location(frame, f.centre),
                        {"diameter_mm": f.diameter_mm, "expected_from": "spec"},
                    )
                )
    for f in inv.features:
        if f.kind == "hole" and f.print_axis == "horizontal":
            fs.append(
                Finding(
                    "feature.horizontal_bore",
                    INFO,
                    "features",
                    f"hole d {f.diameter_mm:.2f} prints horizontal: {HOLE_WARNING}",
                    OBSERVED,
                    name,
                    Location(frame, f.centre),
                    {"diameter_mm": f.diameter_mm},
                    Fix("give the bore its horizontal allowance in CAD (intent.hole_allowance_mm) or print a coupon"),
                )
            )
    return fs
