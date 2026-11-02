"""Mesh geometry for the printability pre-flight, in pure Python with `math` only: vertex welding and edge use,
connected regions of facets, a 2D convex hull with its signed margin and minimum width, volume moments, and a uniform
grid for ray casts.

What it handles: triangle soups in mm (a list of facets, each three (x, y, z) tuples, as `mesh.py` reads them). The
weld joins vertices on a 1e-4 mm grid, so an STL whose facets carry their own vertex copies gets an edge topology.
Every function is deterministic for a given facet order; callers sort their output where order must not matter.

What it does not do: no mesh repair, no booleans, no self-intersection test, no file I/O and no printing. A ray cast
reports the first facet hit; it does not decide whether that hit makes a wall (printability.py does).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

Vec = tuple[float, float, float]
Facet = tuple[Vec, Vec, Vec]
P2 = tuple[float, float]
Edge = tuple[int, int]

WELD_TOL = 1e-4  # mm: vertices closer than this on every axis share one index
GRID_MAX_ENTRIES = 1_000_000  # a RayGrid lists at most this many (cell, facet) pairs; the cell grows to stay under it


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


# ---------------------------------------------------------------------------- welding and edges
@dataclass
class Welded:
    """Indexed form of a facet list: `verts` (welded) and `tris` (one index triple per input facet, same order)."""

    verts: list[Vec]
    tris: list[tuple[int, int, int]]
    _edges: dict[Edge, list[int]] | None = field(default=None, init=False, repr=False)
    _by_vertex: dict[int, list[int]] | None = field(default=None, init=False, repr=False)

    def degenerate(self, fid: int) -> bool:
        """True when two corners of the facet weld to one vertex (a sliver with no area)."""
        a, b, c = self.tris[fid]
        return len({a, b, c}) < 3

    def edges_of(self, fid: int) -> list[Edge]:
        a, b, c = self.tris[fid]
        return [(min(a, b), max(a, b)), (min(b, c), max(b, c)), (min(c, a), max(c, a))]

    def edge_map(self) -> dict[Edge, list[int]]:
        """Each undirected edge and the facets that use it (degenerate facets left out)."""
        if self._edges is None:
            m: dict[Edge, list[int]] = {}
            for fid in range(len(self.tris)):
                if self.degenerate(fid):
                    continue
                for e in self.edges_of(fid):
                    m.setdefault(e, []).append(fid)
            self._edges = m
        return self._edges

    def open_edges(self) -> list[Edge]:
        """Edges used by one facet: a hole in the surface."""
        return sorted(e for e, fs in self.edge_map().items() if len(fs) == 1)

    def nonmanifold_edges(self) -> list[Edge]:
        """Edges used by more than two facets."""
        return sorted(e for e, fs in self.edge_map().items() if len(fs) > 2)

    def flipped_edges(self) -> int:
        """Directed edges used twice in the same direction. In a consistently wound closed mesh every edge runs once
        each way, so one reversed facet in a closed mesh gives three."""
        seen: dict[Edge, int] = {}
        for fid, (a, b, c) in enumerate(self.tris):
            if self.degenerate(fid):
                continue
            for e in ((a, b), (b, c), (c, a)):
                seen[e] = seen.get(e, 0) + 1
        return sum(1 for n in seen.values() if n >= 2)

    def neighbours(self, fid: int) -> list[int]:
        """Facets that share an edge with `fid`."""
        if self.degenerate(fid):
            return []
        m = self.edge_map()
        return sorted({g for e in self.edges_of(fid) for g in m.get(e, []) if g != fid})

    def facets_at(self, vid: int) -> list[int]:
        """Facets that use vertex `vid`."""
        if self._by_vertex is None:
            bv: dict[int, list[int]] = {}
            for fid, t in enumerate(self.tris):
                for v in set(t):
                    bv.setdefault(v, []).append(fid)
            self._by_vertex = bv
        return self._by_vertex.get(vid, [])


def weld(facets: Sequence[Facet], tol: float = WELD_TOL) -> Welded:
    """Join vertices that fall in the same `tol` grid cell. Facet order is kept: tris[i] is facets[i]."""
    index: dict[tuple[int, int, int], int] = {}
    verts: list[Vec] = []
    tris: list[tuple[int, int, int]] = []
    for f in facets:
        t = []
        for v in f:
            key = (round(v[0] / tol), round(v[1] / tol), round(v[2] / tol))
            i = index.get(key)
            if i is None:
                i = index[key] = len(verts)
                verts.append(v)
            t.append(i)
        tris.append((t[0], t[1], t[2]))
    return Welded(verts, tris)


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def add(self, x: int) -> None:
        self.parent.setdefault(x, x)

    def find(self, x: int) -> int:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def groups(self) -> list[list[int]]:
        out: dict[int, list[int]] = {}
        for x in self.parent:
            out.setdefault(self.find(x), []).append(x)
        return sorted((sorted(g) for g in out.values()), key=lambda g: g[0])


def regions(fids: Iterable[int], welded: Welded, links: Iterable[tuple[int, int]] = ()) -> list[list[int]]:
    """The facets `fids` grouped into connected regions: two facets join when they share a welded edge, or when a
    pair in `links` names them both. Each region is sorted; regions are ordered by their first facet id."""
    keep = set(fids)
    uf = _UnionFind()
    for f in keep:
        uf.add(f)
    m = welded.edge_map()
    for f in keep:
        if welded.degenerate(f):
            continue
        for e in welded.edges_of(f):
            for g in m.get(e, []):
                if g in keep and g != f:
                    uf.union(f, g)
    for a, b in links:
        if a in keep and b in keep:
            uf.union(a, b)
    return uf.groups()


def edge_chains(edges: Sequence[Edge]) -> list[tuple[list[Edge], bool]]:
    """Edges grouped into chains that share a vertex. Each chain comes with `closed`: True when every vertex in it
    joins exactly two of its edges (a loop)."""
    uf = _UnionFind()
    for a, b in edges:
        uf.add(a)
        uf.add(b)
        uf.union(a, b)
    chains: dict[int, list[Edge]] = {}
    for e in edges:
        chains.setdefault(uf.find(e[0]), []).append(e)
    out = []
    for root in sorted(chains):
        es = chains[root]
        degree: dict[int, int] = {}
        for a, b in es:
            degree[a] = degree.get(a, 0) + 1
            degree[b] = degree.get(b, 0) + 1
        out.append((sorted(es), len(es) >= 3 and all(d == 2 for d in degree.values())))
    return out


# ---------------------------------------------------------------------------- 2D hull
def _cross2(o: P2, a: P2, b: P2) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def hull_2d(points: Iterable[P2]) -> list[P2]:
    """Convex hull, counter-clockwise, no collinear points (Andrew's monotone chain)."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts
    lower: list[P2] = []
    for p in pts:
        while len(lower) >= 2 and _cross2(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[P2] = []
    for p in reversed(pts):
        while len(upper) >= 2 and _cross2(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _seg_dist(p: P2, a: P2, b: P2) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    ln2 = dx * dx + dy * dy
    t = 0.0 if ln2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / ln2))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def convex_margin(p: P2, hull: Sequence[P2]) -> float:
    """Signed distance from `p` to the boundary of a counter-clockwise convex hull: positive inside, negative
    outside. A hull of one or two points has no inside, so the result is never positive."""
    if not hull:
        raise ValueError("empty hull")
    if len(hull) == 1:
        return -math.hypot(p[0] - hull[0][0], p[1] - hull[0][1])
    if len(hull) == 2:
        return -_seg_dist(p, hull[0], hull[1])
    n = len(hull)
    d = min(_seg_dist(p, hull[i], hull[(i + 1) % n]) for i in range(n))
    inside = all(_cross2(hull[i], hull[(i + 1) % n], p) >= 0 for i in range(n))
    return d if inside else -d


def min_width(hull: Sequence[P2]) -> tuple[float, float, float]:
    """(width, length, angle_deg) of the narrowest strip that holds the hull: the minimum over hull edges of the
    farthest point from the edge's line (rotating calipers), the extent along that edge, and the edge's angle."""
    if len(hull) < 3:
        if len(hull) < 2:
            return 0.0, 0.0, 0.0
        (ax, ay), (bx, by) = hull[0], hull[1]
        return 0.0, math.hypot(bx - ax, by - ay), math.degrees(math.atan2(by - ay, bx - ax))
    best: tuple[float, float, float] | None = None
    n = len(hull)
    for i in range(n):
        a, b = hull[i], hull[(i + 1) % n]
        ln = math.hypot(b[0] - a[0], b[1] - a[1])
        if ln == 0:
            continue
        ex, ey = (b[0] - a[0]) / ln, (b[1] - a[1]) / ln
        width = max(abs((p[0] - a[0]) * -ey + (p[1] - a[1]) * ex) for p in hull)
        along = [(p[0] - a[0]) * ex + (p[1] - a[1]) * ey for p in hull]
        cand = (width, max(along) - min(along), math.degrees(math.atan2(ey, ex)))
        if best is None or cand[0] < best[0] - 1e-12:
            best = cand
    return best if best is not None else (0.0, 0.0, 0.0)


def farthest_in(
    tris: Sequence[tuple[P2, P2, P2]], segments: Sequence[tuple[P2, P2]], max_cells: int = 90_000, min_cell: float = 0.25
) -> float:
    """The largest distance from a point of the triangles `tris` (2D) to its nearest segment: 0 with no segments.

    A raster over the triangles and segments (cell `min_cell` mm, coarser when the box would pass `max_cells`) holds
    each cell's nearest segment, propagated in two sweeps from the cells the segments cross (a nearest-feature
    distance transform). The distance itself is exact to that segment, and the error of the maximum is under one
    cell. Triangle corners, edge midpoints and centroids are measured as well, so a region smaller than a cell still
    counts (the midline of a strip narrower than a cell passes through its diagonal's midpoint)."""
    if not segments or not tris:
        return 0.0
    xs = [p[0] for t in tris for p in t] + [p[0] for s in segments for p in s]
    ys = [p[1] for t in tris for p in t] + [p[1] for s in segments for p in s]
    x0, y0 = min(xs), min(ys)
    w, h = max(xs) - x0, max(ys) - y0
    cell = max(min_cell, math.sqrt(max(w * h, 1e-9) / max_cells))
    nx, ny = int(w / cell) + 2, int(h / cell) + 2
    near = [-1] * (nx * ny)
    dist = [math.inf] * (nx * ny)

    def centre(i: int, j: int) -> P2:
        return (x0 + (i + 0.5) * cell, y0 + (j + 0.5) * cell)

    def cell_of(p: P2) -> tuple[int, int]:
        return min(nx - 1, max(0, int((p[0] - x0) / cell))), min(ny - 1, max(0, int((p[1] - y0) / cell)))

    def offer(i: int, j: int, k: int) -> None:
        c = i + j * nx
        if near[c] == k:
            return
        d = _seg_dist(centre(i, j), *segments[k])
        if d < dist[c]:
            dist[c], near[c] = d, k

    crossing: dict[int, set[int]] = {}  # cell -> every segment that crosses it (a cell holds only its nearest in `near`)
    for k, (a, b) in enumerate(segments):  # seed the cells each segment crosses
        n = max(1, math.ceil(math.hypot(b[0] - a[0], b[1] - a[1]) / (cell / 2)))
        for m in range(n + 1):
            i, j = cell_of((a[0] + (b[0] - a[0]) * m / n, a[1] + (b[1] - a[1]) * m / n))
            offer(i, j, k)
            crossing.setdefault(i + j * nx, set()).add(k)
    sweeps = (
        (range(ny), range(nx), ((-1, 0), (-1, -1), (0, -1), (1, -1))),
        (range(ny - 1, -1, -1), range(nx - 1, -1, -1), ((1, 0), (1, 1), (0, 1), (-1, 1))),
    )
    for rows, cols, nbrs in sweeps:
        for j in rows:
            for i in cols:
                for di, dj in nbrs:
                    ni, nj = i + di, j + dj
                    if 0 <= ni < nx and 0 <= nj < ny and near[ni + nj * nx] >= 0:
                        offer(i, j, near[ni + nj * nx])

    def exact(p: P2) -> float:  # the nearest of the candidates held around p's cell and the segments that cross them
        i, j = cell_of(p)
        around = [ci + cj * nx for ci in range(i - 1, i + 2) for cj in range(j - 1, j + 2) if 0 <= ci < nx and 0 <= cj < ny]
        cands = {near[c] for c in around} | {k for c in around for k in crossing.get(c, ())}
        return min((_seg_dist(p, *segments[k]) for k in cands if k >= 0), default=0.0)

    best = 0.0
    for t in tris:
        (ax, ay), (bx, by), (cx, cy) = t
        mids = (((ax + bx) / 2, (ay + by) / 2), ((bx + cx) / 2, (by + cy) / 2), ((cx + ax) / 2, (cy + ay) / 2))
        best = max(best, exact(((ax + bx + cx) / 3, (ay + by + cy) / 3)), *(exact(p) for p in (*t, *mids)))
        det = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(det) < 1e-12:
            continue
        i0, j0 = cell_of((min(ax, bx, cx), min(ay, by, cy)))
        i1, j1 = cell_of((max(ax, bx, cx), max(ay, by, cy)))
        for j in range(j0, j1 + 1):
            for i in range(i0, i1 + 1):
                px, py = centre(i, j)
                u = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / det
                v = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / det
                if u >= -1e-9 and v >= -1e-9 and u + v <= 1 + 1e-9 and dist[i + j * nx] > best:
                    best = dist[i + j * nx]
    return best


# ---------------------------------------------------------------------------- moments
def moments(facets: Iterable[Facet]) -> tuple[float, Vec]:
    """(volume mm3, centroid) of a closed, outward-wound mesh from signed tetrahedra against the origin. A zero
    volume gives the origin as the centroid."""
    v6 = cx = cy = cz = 0.0
    for a, b, c in facets:
        v = _dot(a, _cross(b, c))
        v6 += v
        cx += v * (a[0] + b[0] + c[0])
        cy += v * (a[1] + b[1] + c[1])
        cz += v * (a[2] + b[2] + c[2])
    if v6 == 0:
        return 0.0, (0.0, 0.0, 0.0)
    return v6 / 6, (cx / (4 * v6), cy / (4 * v6), cz / (4 * v6))


# ---------------------------------------------------------------------------- rays
def ray_triangle(o: Vec, d: Vec, tri: Facet, eps: float = 1e-12) -> float | None:
    """Distance along `d` (a unit vector) from `o` to the triangle, or None (Moller-Trumbore, both faces)."""
    a, b, c = tri
    e1, e2 = _sub(b, a), _sub(c, a)
    p = _cross(d, e2)
    det = _dot(e1, p)
    if abs(det) < eps:
        return None
    inv = 1.0 / det
    s = _sub(o, a)
    u = _dot(s, p) * inv
    if u < -1e-9 or u > 1 + 1e-9:
        return None
    q = _cross(s, e1)
    v = _dot(d, q) * inv
    if v < -1e-9 or u + v > 1 + 1e-9:
        return None
    return _dot(e2, q) * inv


class RayGrid:
    """A uniform grid over a mesh that lists the facets touching each cell (by the facet's bounding box), for
    first-hit ray casts with a 3D DDA walk. The cell edge defaults to the cube root of the bounding volume per facet,
    clamped to 0.5..10 mm, so a cell holds a few facets on a typical part. The cell then grows until the grid lists at
    most `max_entries` (cell, facet) pairs: a metre-scale mesh or one large slanted facet would otherwise fill millions
    of cells. `tests` counts the ray-triangle tests, so a caller can stop a run that grows too long."""

    def __init__(self, facets: Sequence[Facet], cell: float | None = None, max_entries: int = GRID_MAX_ENTRIES):
        self.facets = facets
        self.tests = 0
        xs = [v[0] for f in facets for v in f]
        ys = [v[1] for f in facets for v in f]
        zs = [v[2] for f in facets for v in f]
        pad = 1e-6
        self.lo: Vec = (min(xs) - pad, min(ys) - pad, min(zs) - pad)
        self.hi: Vec = (max(xs) + pad, max(ys) + pad, max(zs) + pad)
        ext = [max(self.hi[i] - self.lo[i], 1e-3) for i in range(3)]
        if cell is None:
            cell = (ext[0] * ext[1] * ext[2] / max(len(facets), 1)) ** (1 / 3)
            cell = min(10.0, max(0.5, cell))
        boxes = [(tuple(min(v[i] for v in f) for i in range(3)), tuple(max(v[i] for v in f) for i in range(3))) for f in facets]
        while True:  # counted before any cell is filled, so the bound holds for memory as well as time
            self.cell = cell
            self.dims = tuple(max(1, math.ceil(e / cell)) for e in ext)
            entries = 0
            for lo_, hi_ in boxes:
                n = 1
                for i in range(3):
                    n *= self._idx(hi_[i], i) - self._idx(lo_[i], i) + 1
                entries += n
            if entries <= max_entries or self.dims == (1, 1, 1):
                break
            cell *= 1.5
        self.cells: dict[tuple[int, int, int], list[int]] = {}
        for fid, (blo, bhi) in enumerate(boxes):
            lo = [self._idx(blo[i], i) for i in range(3)]
            hi = [self._idx(bhi[i], i) for i in range(3)]
            for i in range(lo[0], hi[0] + 1):
                for j in range(lo[1], hi[1] + 1):
                    for k in range(lo[2], hi[2] + 1):
                        self.cells.setdefault((i, j, k), []).append(fid)

    def _key(self, fid: int) -> tuple:
        return tuple(sorted(tuple(round(x, 6) for x in v) for v in self.facets[fid]))

    def _idx(self, x: float, axis: int) -> int:
        return min(self.dims[axis] - 1, max(0, int((x - self.lo[axis]) / self.cell)))

    def first_hit(self, origin: Vec, direction: Vec, skip: Iterable[int] = (), t_min: float = 1e-3) -> tuple[float, int] | None:
        """(distance, facet id) of the nearest facet hit beyond `t_min` along the unit `direction`, ignoring the facets
        in `skip`; None when the ray leaves the grid without a hit. A tie goes to the facet with the lowest sorted
        corners, so the answer does not depend on facet order."""
        skipped = set(skip)
        t0, t1 = 0.0, math.inf
        for i in range(3):  # clip the ray to the grid box (slab test)
            if abs(direction[i]) < 1e-15:
                if not self.lo[i] <= origin[i] <= self.hi[i]:
                    return None
                continue
            ta = (self.lo[i] - origin[i]) / direction[i]
            tb = (self.hi[i] - origin[i]) / direction[i]
            t0, t1 = max(t0, min(ta, tb)), min(t1, max(ta, tb))
        if t0 > t1:
            return None
        start = tuple(origin[i] + direction[i] * t0 for i in range(3))
        cell = [self._idx(start[i], i) for i in range(3)]
        step, t_next, t_delta = [0, 0, 0], [math.inf] * 3, [math.inf] * 3
        for i in range(3):
            if direction[i] > 0:
                step[i] = 1
                t_next[i] = (self.lo[i] + (cell[i] + 1) * self.cell - origin[i]) / direction[i]
                t_delta[i] = self.cell / direction[i]
            elif direction[i] < 0:
                step[i] = -1
                t_next[i] = (self.lo[i] + cell[i] * self.cell - origin[i]) / direction[i]
                t_delta[i] = -self.cell / direction[i]
        tested: set[int] = set()
        best: tuple[float, int] | None = None
        while True:
            for fid in self.cells.get((cell[0], cell[1], cell[2]), ()):
                if fid in tested or fid in skipped:
                    continue
                tested.add(fid)
                self.tests += 1
                t = ray_triangle(origin, direction, self.facets[fid])
                if t is None or t <= t_min:
                    continue
                if best is None or t < best[0] - 1e-9 or (t <= best[0] + 1e-9 and self._key(fid) < self._key(best[1])):
                    best = (t, fid)  # equal distances (a ray along a shared edge): the facet with the lower corners
            exit_t = min(t_next)
            if best is not None and best[0] <= exit_t + 1e-9:
                return best
            axis = t_next.index(exit_t)
            cell[axis] += step[axis]
            if not 0 <= cell[axis] < self.dims[axis] or exit_t > t1:
                return best
            t_next[axis] += t_delta[axis]
