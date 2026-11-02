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
