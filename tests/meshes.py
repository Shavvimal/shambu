"""Closed test meshes built in code (outward-wound, watertight by construction) for the printability tests."""

from __future__ import annotations

import math
from collections.abc import Sequence

from conftest import box_facets

Vec = tuple[float, float, float]
Facet = tuple[Vec, Vec, Vec]
P2 = tuple[float, float]


def box(lo: Sequence[float], hi: Sequence[float]) -> list[Facet]:
    return box_facets(tuple(lo), tuple(hi))


def _area2(poly: Sequence[P2]) -> float:
    return sum(poly[i][0] * poly[(i + 1) % len(poly)][1] - poly[(i + 1) % len(poly)][0] * poly[i][1] for i in range(len(poly))) / 2


def _inside(p: P2, a: P2, b: P2, c: P2) -> bool:
    def side(u: P2, v: P2, w: P2) -> float:
        return (v[0] - u[0]) * (w[1] - u[1]) - (v[1] - u[1]) * (w[0] - u[0])

    d1, d2, d3 = side(a, b, p), side(b, c, p), side(c, a, p)
    return d1 >= -1e-12 and d2 >= -1e-12 and d3 >= -1e-12


def ear_clip(poly: Sequence[P2]) -> list[tuple[int, int, int]]:
    """Triangles (index triples, counter-clockwise) of a simple counter-clockwise polygon. Every polygon vertex is a
    corner of some triangle, so the side quads of a prism meet the caps without T-junctions."""
    idx = list(range(len(poly)))
    out: list[tuple[int, int, int]] = []
    guard = 0
    while len(idx) > 3:
        guard += 1
        if guard > 10000:
            raise ValueError("ear clipping did not finish")
        for k in range(len(idx)):
            i, j, m = idx[k - 1], idx[k], idx[(k + 1) % len(idx)]
            a, b, c = poly[i], poly[j], poly[m]
            cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
            if cross <= 1e-12:
                continue  # reflex or collinear: not an ear
            if any(_inside(poly[q], a, b, c) for q in idx if q not in (i, j, m)):
                continue
            out.append((i, j, m))
            idx.pop(k)
            break
        else:
            raise ValueError("no ear found: the polygon is not simple")
    out.append((idx[0], idx[1], idx[2]))
    return out


def prism(poly: Sequence[P2], depth: float, plane: str = "xz") -> list[Facet]:
    """A polygon extruded into a closed solid. `plane` "xz": the polygon is (x, z), extruded along +y from 0 to
    `depth`; "xy": (x, y), extruded along +z from 0 to `depth`."""
    pts = list(poly)
    if _area2(pts) < 0:
        pts.reverse()

    def at(p: P2, d: float) -> Vec:
        return (p[0], d, p[1]) if plane == "xz" else (p[0], p[1], d)

    out: list[Facet] = []
    tris = ear_clip(pts)
    # In "xz" the polygon's counter-clockwise (x, z) order faces -y; in "xy" it faces +z.
    front, back = (0.0, depth) if plane == "xz" else (depth, 0.0)
    for i, j, k in tris:
        out.append((at(pts[i], front), at(pts[j], front), at(pts[k], front)))
        out.append((at(pts[k], back), at(pts[j], back), at(pts[i], back)))
    n = len(pts)
    for i in range(n):
        p, q = pts[i], pts[(i + 1) % n]
        a, b, c, d = at(p, front), at(q, front), at(q, back), at(p, back)
        out.append((a, d, c))
        out.append((a, c, b))
    if plane == "xy":  # the side winding above suits "xz"; mirror it for "xy"
        out = [f if idx < 2 * len(tris) else (f[0], f[2], f[1]) for idx, f in enumerate(out)]
    return out


def cup(outer: tuple[float, float], inner: tuple[float, float], height: float, roof: float) -> list[Facet]:
    """An upside-down cup: an outer box `outer` (x, y) by `height` with a centred cavity `inner` (x, y), open at the
    bed, whose ceiling is at z = height - roof. The cavity ceiling is anchored on all four sides."""
    ox, oy = outer
    ix, iy = inner
    x0, x1 = (ox - ix) / 2, (ox + ix) / 2
    y0, y1 = (oy - iy) / 2, (oy + iy) / 2
    zc = height - roof
    o = [(0.0, 0.0), (ox, 0.0), (ox, oy), (0.0, oy)]
    i = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    out: list[Facet] = []

    def quad(a: Vec, b: Vec, c: Vec, d: Vec) -> None:  # counter-clockwise seen from outside
        out.append((a, b, c))
        out.append((a, c, d))

    for k in range(4):  # bottom ring at z 0, facing down
        a, b = o[k], o[(k + 1) % 4]
        c, d = i[(k + 1) % 4], i[k]
        quad((a[0], a[1], 0.0), (d[0], d[1], 0.0), (c[0], c[1], 0.0), (b[0], b[1], 0.0))
    quad((0.0, 0.0, height), (ox, 0.0, height), (ox, oy, height), (0.0, oy, height))  # top
    for k in range(4):  # outer walls
        a, b = o[k], o[(k + 1) % 4]
        quad((a[0], a[1], 0.0), (b[0], b[1], 0.0), (b[0], b[1], height), (a[0], a[1], height))
    for k in range(4):  # inner walls, facing into the cavity
        a, b = i[k], i[(k + 1) % 4]
        quad((b[0], b[1], 0.0), (a[0], a[1], 0.0), (a[0], a[1], zc), (b[0], b[1], zc))
    quad((x0, y0, zc), (x0, y1, zc), (x1, y1, zc), (x1, y0, zc))  # cavity ceiling, facing down
    return out


def rotate_z(facets: Sequence[Facet], deg: float) -> list[Facet]:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [tuple((x * c - y * s, x * s + y * c, z) for x, y, z in f) for f in facets]  # type: ignore[misc]


def without(facets: Sequence[Facet], i: int) -> list[Facet]:
    """The mesh with facet `i` removed: three open edges."""
    return [f for k, f in enumerate(facets) if k != i]


def flipped(facets: Sequence[Facet], i: int) -> list[Facet]:
    """The mesh with facet `i` wound the other way."""
    return [(f[0], f[2], f[1]) if k == i else f for k, f in enumerate(facets)]


def voxels(xs: Sequence[float], ys: Sequence[float], zs: Sequence[float], solid) -> list[Facet]:
    """The closed, outward-wound surface of the grid cells `solid(i, j, k)` holds, where cell (i, j, k) spans
    xs[i]..xs[i+1], ys[j]..ys[j+1], zs[k]..zs[k+1]. Every face lies on the grid, so neighbouring faces share edges.
    The solid must not touch itself along an edge only (that edge would be non-manifold)."""
    nx, ny, nz = len(xs) - 1, len(ys) - 1, len(zs) - 1

    def full(i: int, j: int, k: int) -> bool:
        return 0 <= i < nx and 0 <= j < ny and 0 <= k < nz and bool(solid(i, j, k))

    out: list[Facet] = []

    def quad(a: Vec, b: Vec, c: Vec, d: Vec) -> None:  # counter-clockwise seen from outside
        out.append((a, b, c))
        out.append((a, c, d))

    for i in range(nx):
        for j in range(ny):
            for k in range(nz):
                if not full(i, j, k):
                    continue
                x0, x1, y0, y1, z0, z1 = xs[i], xs[i + 1], ys[j], ys[j + 1], zs[k], zs[k + 1]
                if not full(i - 1, j, k):
                    quad((x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (x0, y1, z0))
                if not full(i + 1, j, k):
                    quad((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1))
                if not full(i, j - 1, k):
                    quad((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1))
                if not full(i, j + 1, k):
                    quad((x0, y1, z0), (x0, y1, z1), (x1, y1, z1), (x1, y1, z0))
                if not full(i, j, k - 1):
                    quad((x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (x1, y0, z0))
                if not full(i, j, k + 1):
                    quad((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1))
    return out


# ---------------------------------------------------------------------------- CAD gate fixtures (shambu cad)
def _orient(tri: Facet, want: Vec) -> Facet:
    """`tri` wound so its normal points along `want` (flipped when it does not)."""
    a, b, c = tri
    u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
    return tri if n[0] * want[0] + n[1] * want[1] + n[2] * want[2] >= 0 else (a, c, b)


def _ring(cx: float, cy: float, r: float, n: int, phase: float = 0.0) -> list[P2]:
    return [(cx + r * math.cos(phase + 2 * math.pi * i / n), cy + r * math.sin(phase + 2 * math.pi * i / n)) for i in range(n)]


def _zipper(outer: Sequence[P2], inner: Sequence[P2], centre: P2) -> list[tuple[P2, P2, P2]]:
    """Triangles of the band between a convex outer loop and a convex inner loop around `centre`. Each outer corner
    fans to the inner points nearer its direction than the next corner's; then the outer edge closes on the last of
    them. Every inner point used by a corner is within half the angle between corners of its direction, so it sees it."""
    two_pi = 2 * math.pi

    def ang(p: P2) -> float:
        return math.atan2(p[1] - centre[1], p[0] - centre[0]) % two_pi

    def gap(x: float, y: float) -> float:
        d = (x - y) % two_pi
        return min(d, two_pi - d)

    o = sorted(outer, key=ang)
    i = sorted(inner, key=ang)
    no, ni = len(o), len(i)
    m0 = min(range(ni), key=lambda j: gap(ang(i[j]), ang(o[0])))
    out: list[tuple[P2, P2, P2]] = []
    k = m = 0
    while k < no or m < ni:
        here, nxt = o[k % no], o[(k + 1) % no]
        cur, ahead = i[(m0 + m) % ni], i[(m0 + m + 1) % ni]
        if m < ni and (k == no or gap(ang(ahead), ang(here)) <= gap(ang(ahead), ang(nxt))):
            out.append((here, ahead, cur))
            m += 1
        else:
            out.append((here, nxt, cur))
            k += 1
    return out


def cylinder(r: float, h: float, n: int = 64, centre: Sequence[float] = (0.0, 0.0, 0.0)) -> list[Facet]:
    """A closed n-gon prism about the z axis, from centre z to z + h."""
    cx, cy, z0 = centre
    ring = _ring(cx, cy, r, n)
    out: list[Facet] = []
    for k in range(n):
        p, q = ring[k], ring[(k + 1) % n]
        out.append(_orient(((cx, cy, z0 + h), (p[0], p[1], z0 + h), (q[0], q[1], z0 + h)), (0, 0, 1)))
        out.append(_orient(((cx, cy, z0), (p[0], p[1], z0), (q[0], q[1], z0)), (0, 0, -1)))
        mid = ((p[0] + q[0]) / 2 - cx, (p[1] + q[1]) / 2 - cy, 0.0)
        out.append(_orient(((p[0], p[1], z0), (q[0], q[1], z0), (q[0], q[1], z0 + h)), mid))
        out.append(_orient(((p[0], p[1], z0), (q[0], q[1], z0 + h), (p[0], p[1], z0 + h)), mid))
    return out


def tube(r_in: float, r_out: float, h: float, n: int = 32) -> list[Facet]:
    """A closed annular prism about the z axis, z 0 to h."""
    a, b = _ring(0, 0, r_out, n), _ring(0, 0, r_in, n)
    out: list[Facet] = []
    for k in range(n):
        p, q = a[k], a[(k + 1) % n]
        s, t = b[k], b[(k + 1) % n]
        for z, nz in ((h, 1), (0.0, -1)):
            out.append(_orient(((p[0], p[1], z), (q[0], q[1], z), (t[0], t[1], z)), (0, 0, nz)))
            out.append(_orient(((p[0], p[1], z), (t[0], t[1], z), (s[0], s[1], z)), (0, 0, nz)))
        mo = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2, 0.0)
        out.append(_orient(((p[0], p[1], 0.0), (q[0], q[1], 0.0), (q[0], q[1], h)), mo))
        out.append(_orient(((p[0], p[1], 0.0), (q[0], q[1], h), (p[0], p[1], h)), mo))
        mi = (-(s[0] + t[0]) / 2, -(s[1] + t[1]) / 2, 0.0)
        out.append(_orient(((s[0], s[1], 0.0), (t[0], t[1], 0.0), (t[0], t[1], h)), mi))
        out.append(_orient(((s[0], s[1], 0.0), (t[0], t[1], h), (s[0], s[1], h)), mi))
    return out


def plate_with_holes(size: Sequence[float], holes: Sequence[dict], n: int = 64) -> list[Facet]:
    """A closed plate (0..sx, 0..sy, 0..t) with round holes from the top face, watertight by construction.

    Each hole is {"x", "y", "stages": [(radius, depth), ...]} (depth 0 or more than the plate: through), the radii
    shrinking from the top; the last stage is through when its depth reaches the bottom, else it ends in a floor.
    The plate is cut into cells between the holes (each hole alone in its cell, its circle inside it), so no two holes
    may share an x or a y coordinate."""
    sx, sy, t = size
    xs = sorted(h["x"] for h in holes)
    ys = sorted(h["y"] for h in holes)
    cut_x = [0.0] + [(xs[i] + xs[i + 1]) / 2 for i in range(len(xs) - 1)] + [sx]
    cut_y = [0.0] + [(ys[i] + ys[i + 1]) / 2 for i in range(len(ys) - 1)] + [sy]
    out: list[Facet] = []

    def flat(tri2: tuple[P2, P2, P2], z: float, nz: float) -> None:
        out.append(_orient(tuple((p[0], p[1], z) for p in tri2), (0.0, 0.0, nz)))  # type: ignore[arg-type]

    for i in range(len(cut_x) - 1):
        for j in range(len(cut_y) - 1):
            x0, x1, y0, y1 = cut_x[i], cut_x[i + 1], cut_y[j], cut_y[j + 1]
            corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            inside = [h for h in holes if x0 < h["x"] < x1 and y0 < h["y"] < y1]
            if not inside:
                for z, nz in ((t, 1.0), (0.0, -1.0)):
                    flat((corners[0], corners[1], corners[2]), z, nz)
                    flat((corners[0], corners[2], corners[3]), z, nz)
                continue
            h = inside[0]
            cx, cy = h["x"], h["y"]
            stages = list(h["stages"])
            top_ring = _ring(cx, cy, stages[0][0], n, 0.01)
            for tri in _zipper(corners, top_ring, (cx, cy)):
                flat(tri, t, 1.0)
            z = t
            for s, (r, depth) in enumerate(stages):
                ring = _ring(cx, cy, r, n, 0.01)
                z_bot = 0.0 if depth <= 0 or z - depth <= 1e-9 else z - depth
                for k in range(n):
                    p, q = ring[k], ring[(k + 1) % n]
                    inward = (cx - (p[0] + q[0]) / 2, cy - (p[1] + q[1]) / 2, 0.0)
                    out.append(_orient(((p[0], p[1], z_bot), (q[0], q[1], z_bot), (q[0], q[1], z)), inward))
                    out.append(_orient(((p[0], p[1], z_bot), (q[0], q[1], z), (p[0], p[1], z)), inward))
                z = z_bot
                if z == 0.0:
                    for tri in _zipper(corners, ring, (cx, cy)):
                        flat(tri, 0.0, -1.0)
                    break
                if s + 1 < len(stages):
                    nxt = _ring(cx, cy, stages[s + 1][0], n, 0.01)
                    for k in range(n):
                        p, q, u, v = ring[k], ring[(k + 1) % n], nxt[k], nxt[(k + 1) % n]
                        flat((p, q, v), z, 1.0)
                        flat((p, v, u), z, 1.0)
                else:
                    for k in range(n):
                        flat(((cx, cy), ring[k], ring[(k + 1) % n]), z, 1.0)
                    flat((corners[0], corners[1], corners[2]), 0.0, -1.0)
                    flat((corners[0], corners[2], corners[3]), 0.0, -1.0)
    for cuts, fixed, axis, sign in ((cut_x, 0.0, "x", -1.0), (cut_x, sy, "x", 1.0), (cut_y, 0.0, "y", -1.0), (cut_y, sx, "y", 1.0)):
        for k in range(len(cuts) - 1):
            u0, u1 = cuts[k], cuts[k + 1]
            if axis == "x":
                p, q, nrm = (u0, fixed), (u1, fixed), (0.0, sign, 0.0)
            else:
                p, q, nrm = (fixed, u0), (fixed, u1), (sign, 0.0, 0.0)
            out.append(_orient(((p[0], p[1], 0.0), (q[0], q[1], 0.0), (q[0], q[1], t)), nrm))
            out.append(_orient(((p[0], p[1], 0.0), (q[0], q[1], t), (p[0], p[1], t)), nrm))
    return out


def pcd_holes(pcd: float, angles: Sequence[float], r: float, centre: P2, depth: float = 0.0) -> list[dict]:
    return [
        {"x": centre[0] + pcd / 2 * math.cos(math.radians(a)), "y": centre[1] + pcd / 2 * math.sin(math.radians(a)), "stages": [(r, depth)]}
        for a in angles
    ]


def mirror_y(facets: Sequence[Facet], about: float = 0.0) -> list[Facet]:
    """The mirror image in the plane y = about, wound outward again."""
    return [tuple((x, 2 * about - y, z) for x, y, z in (f[0], f[2], f[1])) for f in facets]  # type: ignore[misc]


def moved(facets: Sequence[Facet], d: Sequence[float]) -> list[Facet]:
    return [tuple((x + d[0], y + d[1], z + d[2]) for x, y, z in f) for f in facets]  # type: ignore[misc]


def rounded_edge_bar(size: Sequence[float], r: float, n: int = 16) -> list[Facet]:
    """A bar 0..sx along x, 0..sy along y, 0..sz along z with its +x/+z edge rounded to radius r (a fillet strip)."""
    sx, sy, sz = size
    arc = [(sx - r + r * math.cos(math.pi / 2 * k / n), sz - r + r * math.sin(math.pi / 2 * k / n)) for k in range(n + 1)]
    poly = [(0.0, 0.0), (sx, 0.0), *arc, (0.0, sz)]
    return prism(poly, sy, "xz")
