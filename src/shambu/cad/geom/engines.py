"""Geometry engines for the clearance and sweep gates (numpy, scipy and manifold3d: the `geom` extra; import this
module only after `geom.require()`).

Two Protocols, each with two real implementations and a test fake (rubric 5):

- `IntersectionEngine.overlap(a, b)`:
  - `ManifoldBoolean`: the intersection volume from manifold3d and the bounds of its pieces. manifold3d signals a
    mesh it cannot use through its status, not an exception; any status other than NoError, and any error the binding
    raises, becomes `GeomError`, which the gate reports as `clash.boolean_failed` (FAIL, never exempt).
  - `SampledContainment`: for a pair manifold3d cannot take (a mesh that is not closed): surface samples of each mesh
    inside the other by ray parity. It fills `inside_points` and leaves `volume_mm3` None. A count of points is never
    reported as a volume.
- `DistanceEngine.distances(points, upper_mm)`: per point, the distance to a mesh and the closest point on it.
  - `ExactDistance`: every point against every triangle (the slow reference path).
  - `PrunedDistance`: triangle samples in a KD-tree. Each triangle is sampled on its own barycentric grid, so every
    point of a triangle lies within the covering radius h of a sample of the same triangle. For a query point p with
    nearest sample at q, the true distance D lies in [q - h, q]: so a point whose q - h exceeds the upper bound is
    beyond it, and the closest triangle of p owns a sample within D + h <= q + h of p. A ball query of radius q + h
    therefore returns every triangle that can be closest, and the exact point-triangle step on those gives D itself.
    The result equals `ExactDistance` exactly (same pairs, same arithmetic), not approximately; a property test
    checks it on random placements.

What it does not do: no file reading, no rule logic (clearance.py, sweep.py), no caches keyed by object identity,
no import-time work beyond importing the extra, no printing.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import manifold3d
import numpy as np
from scipy.spatial import cKDTree

from ... import meshgeom as G
from ...errors import GeomError
from ..judge import Overlap

PARITY_DIR = (0.5773502, 0.5773507, 0.5773497)  # off the axes and the diagonals, so a ray rarely grazes an edge
# Triangle samples for the KD-tree of PrunedDistance. Exactness does not depend on it (see the docstring); 0.5 mm keeps
# the tree small and the candidate sets short on parts of a few hundred millimetres.
TARGET_SPACING_MM = 0.5
PAIR_BUDGET = 2_000_000  # point-triangle pairs per vectorised chunk (memory, not accuracy)


@dataclass
class GMesh:
    """A triangle mesh as arrays: welded vertices V (n, 3), faces F (m, 3) and whether it is closed and wound."""

    name: str
    V: np.ndarray
    F: np.ndarray
    watertight: bool

    @classmethod
    def from_facets(cls, name: str, facets: Sequence[G.Facet]) -> GMesh:
        w = G.weld(facets)
        keep = [t for i, t in enumerate(w.tris) if not w.degenerate(i)]
        closed = not w.open_edges() and not w.nonmanifold_edges() and w.flipped_edges() == 0
        return cls(name, np.asarray(w.verts, dtype=np.float64), np.asarray(keep, dtype=np.int64).reshape(-1, 3), closed)

    @property
    def tris(self) -> np.ndarray:
        return self.V[self.F]

    @property
    def lo(self) -> np.ndarray:
        return self.V.min(axis=0)

    @property
    def hi(self) -> np.ndarray:
        return self.V.max(axis=0)

    def volume(self) -> float:
        t = self.tris
        return float(_dot(t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6) if len(t) else 0.0

    def moved(self, rot: np.ndarray, shift: np.ndarray) -> GMesh:
        """The mesh after x -> rot @ x + shift."""
        return GMesh(self.name, self.V @ rot.T + shift, self.F, self.watertight)

    def manifold(self) -> manifold3d.Manifold:
        try:
            m = manifold3d.Manifold(manifold3d.Mesh64(vert_properties=self.V, tri_verts=self.F.astype(np.uint64)))
        except (RuntimeError, ValueError, TypeError) as e:
            raise GeomError(f"{self.name}: manifold3d refused the mesh: {e}") from e
        if m.status() != manifold3d.Error.NoError:
            raise GeomError(f"{self.name}: manifold3d refused the mesh ({m.status().name})")
        return m


class IntersectionEngine(Protocol):
    method: str

    def overlap(self, a: GMesh, b: GMesh) -> Overlap: ...


class ManifoldBoolean:
    method = "boolean"

    def overlap(self, a: GMesh, b: GMesh) -> Overlap:
        ma, mb = a.manifold(), b.manifold()
        try:
            inter = ma ^ mb
        except (RuntimeError, ValueError) as e:
            raise GeomError(f"{a.name} x {b.name}: the Boolean failed: {e}") from e
        if inter.status() != manifold3d.Error.NoError:
            raise GeomError(f"{a.name} x {b.name}: the Boolean failed ({inter.status().name})")
        vol = float(inter.volume())
        if vol <= 0:
            return Overlap(self.method, 0.0, None)
        boxes = [p.bounding_box() for p in inter.decompose() if p.volume() > 0]
        lo = tuple(min(bx[i] for bx in boxes) for i in range(3))
        hi = tuple(max(bx[i + 3] for bx in boxes) for i in range(3))
        return Overlap(self.method, vol, (lo, hi))


def _dot(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return a[..., 0] * b[..., 0] + a[..., 1] * b[..., 1] + a[..., 2] * b[..., 2]


def closest_pairs(p: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(distance, closest point) for aligned rows of points and triangles (a, b, c), by the corner and edge Voronoi
    regions in the same order as `meshgeom.point_triangle`, so the two agree to rounding."""
    p, a, b, c = np.broadcast_arrays(p, a, b, c)
    ab, ac, ap = b - a, c - a, p - a
    d1, d2 = _dot(ab, ap), _dot(ac, ap)
    bp = p - b
    d3, d4 = _dot(ab, bp), _dot(ac, bp)
    cp = p - c
    d5, d6 = _dot(ab, cp), _dot(ac, cp)
    vc = d1 * d4 - d3 * d2
    vb = d5 * d2 - d1 * d6
    va = d3 * d6 - d5 * d4
    with np.errstate(divide="ignore", invalid="ignore"):
        den = 1.0 / (va + vb + vc)
        q = a + ab * (vb * den)[..., None] + ac * (vc * den)[..., None]
        t_ab = d1 / (d1 - d3)
        t_ac = d2 / (d2 - d6)
        t_bc = (d4 - d3) / ((d4 - d3) + (d5 - d6))
    done = np.zeros(d1.shape, dtype=bool)

    def put(mask: np.ndarray, value: np.ndarray) -> None:
        nonlocal done
        m = mask & ~done
        q[m] = value[m]
        done |= m

    put((d1 <= 0) & (d2 <= 0), a)
    put((d3 >= 0) & (d4 <= d3), b)
    put((vc <= 0) & (d1 >= 0) & (d3 <= 0), a + ab * t_ab[..., None])
    put((d6 >= 0) & (d5 <= d6), c)
    put((vb <= 0) & (d2 >= 0) & (d6 <= 0), a + ac * t_ac[..., None])
    put((va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0), b + (c - b) * t_bc[..., None])
    d = p - q
    return np.sqrt(d[..., 0] * d[..., 0] + d[..., 1] * d[..., 1] + d[..., 2] * d[..., 2]), q


@dataclass(frozen=True)
class Distances:
    """Per query point: distance (inf beyond the upper bound), closest point (nan beyond) and triangle (-1 beyond)."""

    d: np.ndarray
    q: np.ndarray
    tri: np.ndarray

    def best(self) -> tuple[float, int, int] | None:
        """(distance, point index, triangle index) of the minimum; ties go to the lower point index."""
        if not len(self.d) or not np.isfinite(self.d).any():
            return None
        i = int(np.argmin(self.d))
        return float(self.d[i]), i, int(self.tri[i])


@dataclass(frozen=True)
class Nearest:
    """The minimum over a point set: distance, the query point's index, the triangle and the closest point on it."""

    distance_mm: float
    index: int
    tri: int
    point: np.ndarray


class DistanceEngine(Protocol):
    mesh: GMesh

    def distances(self, points: np.ndarray, upper_mm: float = math.inf) -> Distances: ...
    def nearest(self, points: np.ndarray, upper_mm: float = math.inf) -> Nearest | None: ...


def _nearest(res: Distances, index: np.ndarray | None = None) -> Nearest | None:
    top = res.best()
    if top is None:
        return None
    d, i, tri = top
    return Nearest(d, int(index[i]) if index is not None else i, tri, res.q[i])


def _empty(n: int) -> Distances:
    return Distances(np.full(n, np.inf), np.full((n, 3), np.nan), np.full(n, -1, dtype=np.int64))


@dataclass
class ExactDistance:
    mesh: GMesh

    def distances(self, points: np.ndarray, upper_mm: float = math.inf) -> Distances:
        pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
        out = _empty(len(pts))
        t = self.mesh.tris
        if not len(t):
            return out
        chunk = max(1, PAIR_BUDGET // len(t))
        for s in range(0, len(pts), chunk):
            p = pts[s : s + chunk][:, None, :]
            d, q = closest_pairs(p, t[None, :, 0], t[None, :, 1], t[None, :, 2])
            j = np.argmin(d, axis=1)  # first index of the minimum: the lowest triangle id on a tie
            rows = np.arange(len(j))
            dd = d[rows, j]
            ok = dd <= upper_mm
            idx = np.arange(s, s + len(j))[ok]
            out.d[idx] = dd[ok]
            out.q[idx] = q[rows, j][ok]
            out.tri[idx] = j[ok]
        return out

    def nearest(self, points: np.ndarray, upper_mm: float = math.inf) -> Nearest | None:
        return _nearest(self.distances(points, upper_mm))


def surface_samples(mesh: GMesh, spacing: float) -> tuple[np.ndarray, np.ndarray, float]:
    """(points, owning triangle, covering radius h). Each triangle is split into k x k similar sub-triangles
    (k = ceil(longest edge / spacing)) and sampled at their corners. Any point of a triangle is within L / sqrt(3) of
    a corner of its sub-triangle of longest edge L (the circumradius bound for an acute triangle; half the longest
    edge for an obtuse one, which is smaller), so h = max(longest edge / k) / sqrt(3)."""
    t = mesh.tris
    if not len(t):
        return np.zeros((0, 3)), np.zeros(0, dtype=np.int64), 0.0
    e = np.stack(
        [np.linalg.norm(t[:, 1] - t[:, 0], axis=1), np.linalg.norm(t[:, 2] - t[:, 1], axis=1), np.linalg.norm(t[:, 0] - t[:, 2], axis=1)],
        axis=1,
    )
    longest = e.max(axis=1)
    ks = np.maximum(1, np.ceil(longest / spacing)).astype(np.int64)
    pts, owner = [], []
    for k in np.unique(ks):
        sel = np.nonzero(ks == k)[0]
        bary = np.array([(i / k, j / k) for i in range(k + 1) for j in range(k + 1 - i)], dtype=np.float64)
        a, b, c = t[sel, 0], t[sel, 1], t[sel, 2]
        p = a[:, None, :] + (b - a)[:, None, :] * bary[None, :, 0:1] + (c - a)[:, None, :] * bary[None, :, 1:2]
        pts.append(p.reshape(-1, 3))
        owner.append(np.repeat(sel, len(bary)))
    h = float(np.max(longest / ks) / math.sqrt(3))
    return np.concatenate(pts), np.concatenate(owner), h


@dataclass
class PrunedDistance:
    mesh: GMesh
    samples: np.ndarray
    owner: np.ndarray
    h: float
    tree: cKDTree

    @classmethod
    def build(cls, mesh: GMesh, spacing: float = TARGET_SPACING_MM) -> PrunedDistance:
        pts, owner, h = surface_samples(mesh, spacing)
        return cls(mesh, pts, owner, h, cKDTree(pts))

    def distances(self, points: np.ndarray, upper_mm: float = math.inf) -> Distances:
        pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
        out = _empty(len(pts))
        if not len(pts) or not len(self.samples):
            return out
        near, _ = self.tree.query(pts)
        t = self.mesh.tris
        live = np.nonzero(near - self.h <= upper_mm)[0]
        if not len(live):
            return out
        balls = self.tree.query_ball_point(pts[live], near[live] + self.h * (1 + 1e-12) + 1e-12)
        sizes = np.fromiter((len(b) for b in balls), dtype=np.int64, count=len(balls))
        pi = np.repeat(live, sizes)
        si = np.fromiter((j for b in balls for j in b), dtype=np.int64, count=int(sizes.sum()))
        pairs = np.unique(np.stack([pi, self.owner[si]], axis=1), axis=0)  # (point, triangle), sorted
        for s0 in range(0, len(pairs), PAIR_BUDGET):
            pr = pairs[s0 : s0 + PAIR_BUDGET]
            d, q = closest_pairs(pts[pr[:, 0]], t[pr[:, 1], 0], t[pr[:, 1], 1], t[pr[:, 1], 2])
            order = np.lexsort((pr[:, 1], d, pr[:, 0]))  # by point, then distance, then the lower triangle id
            first = order[np.r_[True, pr[order[1:], 0] != pr[order[:-1], 0]]]
            for k in first:  # a point's pairs never straddle two chunks unless it has more than PAIR_BUDGET of them
                i = pr[k, 0]
                if d[k] <= upper_mm and (d[k] < out.d[i] or (d[k] == out.d[i] and pr[k, 1] < out.tri[i])):
                    out.d[i], out.q[i], out.tri[i] = d[k], q[k], pr[k, 1]
        return out

    def nearest(self, points: np.ndarray, upper_mm: float = math.inf) -> Nearest | None:
        """The minimum over `points`. With d0 the smallest nearest-sample distance, the true minimum D* is at most d0,
        and a point's own distance is at least its nearest-sample distance minus h; so only points within d0 + h can
        hold D*, and only they go to the exact step. Ties keep the lowest point index, as ExactDistance does."""
        pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
        if not len(pts) or not len(self.samples):
            return None
        near, _ = self.tree.query(pts)
        d0 = float(near.min())
        if d0 - self.h > upper_mm:
            return None
        keep = np.nonzero(near <= d0 + self.h * (1 + 1e-12) + 1e-12)[0]
        return _nearest(self.distances(pts[keep], upper_mm), keep)

    def crosses(self, p0: np.ndarray, p1: np.ndarray) -> np.ndarray:
        """Per segment p0[i] -> p1[i]: True when it passes through a triangle of the mesh. A crossed triangle owns a
        sample within (half the segment + h) of the segment's middle, so the ball query finds every candidate."""
        a0 = np.asarray(p0, dtype=np.float64).reshape(-1, 3)
        a1 = np.asarray(p1, dtype=np.float64).reshape(-1, 3)
        out = np.zeros(len(a0), dtype=bool)
        if not len(a0) or not len(self.samples):
            return out
        mid = (a0 + a1) / 2
        half = np.linalg.norm(a1 - a0, axis=1) / 2
        t = self.mesh.tris
        balls = self.tree.query_ball_point(mid, half + self.h * (1 + 1e-12) + 1e-12)
        for i, ids in enumerate(balls):
            if not ids or half[i] == 0:
                continue
            tris = np.unique(self.owner[np.asarray(ids, dtype=np.int64)])
            out[i] = bool(_segment_hits(a0[i], a1[i], t[tris]).any())
        return out


def edges(mesh: GMesh) -> np.ndarray:
    """The unique undirected edges of the mesh as vertex index pairs (k, 2)."""
    f = mesh.F
    e = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    return np.unique(np.sort(e, axis=1), axis=0)


def edge_samples(mesh: GMesh, spacing: float) -> np.ndarray:
    """The vertices plus points along every edge at most `spacing` apart. Between two meshes the closest pair is a
    vertex against a triangle or a point on one edge against a point on another, so these points miss the true
    distance by at most spacing / 2 (the edge-against-edge case) and never read below it."""
    e = edges(mesh)
    a, b = mesh.V[e[:, 0]], mesh.V[e[:, 1]]
    n = np.maximum(1, np.ceil(np.linalg.norm(b - a, axis=1) / spacing)).astype(np.int64)
    pts = [mesh.V]
    for k in np.unique(n):
        if k < 2:
            continue
        sel = n == k
        t = (np.arange(1, k) / k)[None, :, None]
        pts.append((a[sel][:, None, :] + (b[sel] - a[sel])[:, None, :] * t).reshape(-1, 3))
    return np.concatenate(pts)


def _segment_pairs(p1: np.ndarray, q1: np.ndarray, p2: np.ndarray, q2: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(distance, point on 1, point on 2) for aligned rows of segments (the clamped closest-point solution)."""
    d1, d2, r = q1 - p1, q2 - p2, p1 - p2
    a, e, f = _dot(d1, d1), _dot(d2, d2), _dot(d2, r)
    c, b = _dot(d1, r), _dot(d1, d2)
    den = a * e - b * b
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.where(den > 1e-18, np.clip((b * f - c * e) / den, 0, 1), 0.0)
        t = np.where(e > 1e-18, (b * s + f) / e, 0.0)
        s = np.where(
            t < 0, np.where(a > 1e-18, np.clip(-c / a, 0, 1), 0.0), np.where(t > 1, np.where(a > 1e-18, np.clip((b - c) / a, 0, 1), 0.0), s)
        )
        t = np.clip(t, 0, 1)
    c1 = p1 + d1 * s[:, None]
    c2 = p2 + d2 * t[:, None]
    d = c1 - c2
    return np.sqrt(_dot(d, d)), c1, c2


@dataclass
class EdgeSet:
    """A mesh's unique edges with a KD-tree over their midpoints, for exact edge-against-edge distances."""

    p: np.ndarray
    q: np.ndarray
    half: np.ndarray
    tree: cKDTree

    @classmethod
    def build(cls, mesh: GMesh) -> EdgeSet:
        e = edges(mesh)
        p, q = mesh.V[e[:, 0]], mesh.V[e[:, 1]]
        return cls(p, q, np.linalg.norm(q - p, axis=1) / 2, cKDTree((p + q) / 2))

    def closest(self, p2: np.ndarray, q2: np.ndarray, bound: float) -> tuple[float, np.ndarray, np.ndarray] | None:
        """(distance, point on this set, point on the other) of the closest edge pair within `bound`, else None. Two
        segments within `bound` have midpoints within bound + both half lengths, so the ball query misses none."""
        if not len(self.p) or not len(p2):
            return None
        h2 = np.linalg.norm(q2 - p2, axis=1) / 2
        lists = self.tree.query_ball_point((p2 + q2) / 2, bound + h2 + self.half.max() + 1e-9)
        sizes = np.fromiter((len(x) for x in lists), dtype=np.int64, count=len(lists))
        if not sizes.sum():
            return None
        i2 = np.repeat(np.arange(len(p2)), sizes)
        i1 = np.fromiter((j for x in lists for j in x), dtype=np.int64, count=int(sizes.sum()))
        d, c1, c2 = _segment_pairs(self.p[i1], self.q[i1], p2[i2], q2[i2])
        k = int(np.argmin(d))
        return (float(d[k]), c1[k], c2[k]) if d[k] <= bound else None


def mesh_distance(a: PrunedDistance, b: PrunedDistance, upper_mm: float) -> tuple[float, np.ndarray, np.ndarray] | None:
    """The exact minimum distance between two meshes that do not intersect, with the closest points (on a, on b), or
    None beyond `upper_mm`. Every vertex of each against the other's triangles (PrunedDistance, exact), then every
    edge pair whose midpoints are close enough to beat that bound (a KD-tree over b's edge midpoints)."""
    best: tuple[float, np.ndarray, np.ndarray] | None = None
    for src, dst, flip in ((a, b, False), (b, a, True)):
        top = dst.nearest(src.mesh.V, upper_mm)
        if top is not None and (best is None or top.distance_mm < best[0]):
            p, q = src.mesh.V[top.index], top.point
            best = (top.distance_mm, q, p) if flip else (top.distance_mm, p, q)
    bound = best[0] if best is not None else upper_mm
    eb = EdgeSet.build(b.mesh)
    ea = edges(a.mesh)
    hit = eb.closest(a.mesh.V[ea[:, 0]], a.mesh.V[ea[:, 1]], bound)
    if hit is not None and (best is None or hit[0] < best[0]):
        best = (hit[0], hit[2], hit[1])
    return best


def _segment_hits(p0: np.ndarray, p1: np.ndarray, tris: np.ndarray) -> np.ndarray:
    d = p1 - p0
    e1 = tris[:, 1] - tris[:, 0]
    e2 = tris[:, 2] - tris[:, 0]
    h = np.cross(d, e2)
    det = _dot(e1, h)
    with np.errstate(divide="ignore", invalid="ignore"):
        inv = 1.0 / det
        s = p0 - tris[:, 0]
        u = _dot(s, h) * inv
        q = np.cross(s, e1)
        v = _dot(np.broadcast_to(d, q.shape), q) * inv
        tt = _dot(e2, q) * inv
        return (np.abs(det) > 1e-15) & (u >= 0) & (v >= 0) & (u + v <= 1) & (tt >= 0) & (tt <= 1)


def inside(points: np.ndarray, mesh: GMesh) -> np.ndarray:
    """Per point: True when a ray from it crosses the mesh an odd number of times (inside a closed mesh)."""
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    t = mesh.tris
    out = np.zeros(len(pts), dtype=bool)
    if not len(t) or not len(pts):
        return out
    d = np.asarray(PARITY_DIR) / np.linalg.norm(PARITY_DIR)
    e1 = t[:, 1] - t[:, 0]
    e2 = t[:, 2] - t[:, 0]
    h = np.cross(d, e2)
    det = _dot(e1, h)
    ok = np.abs(det) > 1e-15
    inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
    chunk = max(1, PAIR_BUDGET // len(t))
    for s in range(0, len(pts), chunk):
        p = pts[s : s + chunk][:, None, :]
        sv = p - t[None, :, 0]
        u = _dot(sv, h[None]) * inv[None]
        q = np.cross(sv, e1[None])
        v = _dot(np.broadcast_to(d, q.shape), q) * inv[None]
        tt = _dot(e2[None], q) * inv[None]
        hit = ok[None] & (u >= 0) & (v >= 0) & (u + v <= 1) & (tt > 1e-9)
        out[s : s + chunk] = (hit.sum(axis=1) % 2) == 1
    return out


@dataclass
class SampledContainment:
    spacing: float = 0.5
    method: str = "sampled"

    def overlap(self, a: GMesh, b: GMesh) -> Overlap:
        pa, _, _ = surface_samples(a, self.spacing)
        pb, _, _ = surface_samples(b, self.spacing)
        hits = np.concatenate([pa[inside(pa, b)], pb[inside(pb, a)]])
        pts = tuple(tuple(float(x) for x in p) for p in hits[:200])
        if not len(hits):
            return Overlap(self.method, None, None, ())
        lo = tuple(float(x) for x in hits.min(axis=0))
        hi = tuple(float(x) for x in hits.max(axis=0))
        return Overlap(self.method, None, (lo, hi), pts)  # type: ignore[arg-type]
