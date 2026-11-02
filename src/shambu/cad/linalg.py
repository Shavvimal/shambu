"""Small vector helpers for the CAD gates, `math` only: unit vectors, a plane basis for an axis, the eigenvector of
the smallest eigenvalue of a symmetric 3x3 matrix, an algebraic circle fit, and rigid placements.

What it handles: the numbers `features.py`, `gauge.py`, `spec.py` and `diff.py` share. What it does not do: no
mesh topology (meshgeom.py), no file I/O, no printing.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

Vec = tuple[float, float, float]
Facet = tuple[Vec, Vec, Vec]
Mat = tuple[Vec, Vec, Vec]


def sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def mul(a: Vec, k: float) -> Vec:
    return (a[0] * k, a[1] * k, a[2] * k)


def dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def norm(a: Vec) -> float:
    return math.sqrt(dot(a, a))


def unit(a: Sequence[float]) -> Vec:
    v = (float(a[0]), float(a[1]), float(a[2]))
    n = norm(v)
    if n == 0:
        raise ValueError("zero-length vector")
    return (v[0] / n, v[1] / n, v[2] / n)


def canonical_axis(a: Vec) -> Vec:
    """The axis with its largest component positive, so a bore read from either end gets one direction."""
    k = max(range(3), key=lambda i: abs(a[i]))
    return a if a[k] > 0 else (-a[0], -a[1], -a[2])


def basis(axis: Vec) -> tuple[Vec, Vec]:
    """(u, v): a right-handed basis of the plane normal to `axis`. u is the world axis least aligned with `axis`,
    made orthogonal, so the basis (and the pattern angles measured in it) is the same on every run."""
    k = min(range(3), key=lambda i: abs(axis[i]))
    e = tuple(1.0 if i == k else 0.0 for i in range(3))
    u = unit(sub(e, mul(axis, dot(e, axis))))  # type: ignore[arg-type]
    return u, cross(axis, u)


def smallest_eigenvector(m: Sequence[Sequence[float]]) -> tuple[float, Vec]:
    """(eigenvalue, unit eigenvector) of the smallest eigenvalue of a symmetric 3x3 matrix, in closed form (the
    trigonometric solution of the characteristic cubic, then the largest cross product of two rows of m - lambda I)."""
    a11, a12, a13 = m[0][0], m[0][1], m[0][2]
    a22, a23, a33 = m[1][1], m[1][2], m[2][2]
    p1 = a12 * a12 + a13 * a13 + a23 * a23
    q = (a11 + a22 + a33) / 3
    if p1 < 1e-30:
        lam = min(a11, a22, a33)
    else:
        p2 = (a11 - q) ** 2 + (a22 - q) ** 2 + (a33 - q) ** 2 + 2 * p1
        p = math.sqrt(p2 / 6)
        b = [[(m[i][j] - (q if i == j else 0.0)) / p for j in range(3)] for i in range(3)]
        det_b = (
            b[0][0] * (b[1][1] * b[2][2] - b[1][2] * b[2][1])
            - b[0][1] * (b[1][0] * b[2][2] - b[1][2] * b[2][0])
            + b[0][2] * (b[1][0] * b[2][1] - b[1][1] * b[2][0])
        )
        r = max(-1.0, min(1.0, det_b / 2))
        phi = math.acos(r) / 3
        lam = q + 2 * p * math.cos(phi + 2 * math.pi / 3)  # the smallest of the three roots
    rows: list[Vec] = [
        (m[i][0] - (lam if i == 0 else 0.0), m[i][1] - (lam if i == 1 else 0.0), m[i][2] - (lam if i == 2 else 0.0)) for i in range(3)
    ]
    cands: list[Vec] = [cross(rows[0], rows[1]), cross(rows[0], rows[2]), cross(rows[1], rows[2])]
    best = max(cands, key=norm)
    if norm(best) < 1e-12:  # a repeated smallest root: any vector normal to the one distinct row will do
        big = max(rows, key=norm)
        if norm(big) < 1e-12:
            return lam, (0.0, 0.0, 1.0)
        best = cross(big, basis(unit(big))[0])
    return lam, unit(best)


def fit_circle(points: Sequence[tuple[float, float]]) -> tuple[float, float, float, float]:
    """(cx, cy, r, rms residual) of the algebraic least-squares circle x2 + y2 = 2 a x + 2 b y + c through 2D points
    (a 3x3 normal-equation solve). Points on an exact circle give it to rounding."""
    n = len(points)
    if n < 3:
        raise ValueError("a circle needs three points")
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    pts = [(p[0] - mx, p[1] - my) for p in points]  # centred for conditioning
    sxx = sum(x * x for x, _ in pts)
    syy = sum(y * y for _, y in pts)
    sxy = sum(x * y for x, y in pts)
    sx = sum(x for x, _ in pts)
    sy = sum(y for _, y in pts)
    zz = [x * x + y * y for x, y in pts]
    sxz = sum(x * z for (x, _), z in zip(pts, zz, strict=True))
    syz = sum(y * z for (_, y), z in zip(pts, zz, strict=True))
    sz = sum(zz)
    a = [[2 * sxx, 2 * sxy, sx], [2 * sxy, 2 * syy, sy], [2 * sx, 2 * sy, float(n)]]
    rhs = [sxz, syz, sz]
    sol = solve3(a, rhs)
    cx, cy, c = sol
    r = math.sqrt(max(c + cx * cx + cy * cy, 0.0))
    rms = math.sqrt(sum((math.hypot(x - cx, y - cy) - r) ** 2 for x, y in pts) / n)
    return cx + mx, cy + my, r, rms


def solve3(a: Sequence[Sequence[float]], b: Sequence[float]) -> tuple[float, float, float]:
    """x with a x = b for a 3x3 system (Cramer's rule). Raises ValueError on a singular system."""

    def det(m: Sequence[Sequence[float]]) -> float:
        return (
            m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
        )

    d = det(a)
    scale = max(abs(x) for row in a for x in row) or 1.0
    if abs(d) < 1e-12 * scale**3:
        raise ValueError("singular system")
    out = []
    for k in range(3):
        mk = [[b[i] if j == k else a[i][j] for j in range(3)] for i in range(3)]
        out.append(det(mk) / d)
    return out[0], out[1], out[2]


@dataclass(frozen=True)
class Placement:
    """A rigid placement: the part's own x and y axes in the shared frame and its origin (z = x cross y)."""

    origin: Vec = (0.0, 0.0, 0.0)
    x: Vec = (1.0, 0.0, 0.0)
    y: Vec = (0.0, 1.0, 0.0)

    @property
    def z(self) -> Vec:
        return cross(self.x, self.y)

    def is_identity(self) -> bool:
        return self == Placement()

    def apply(self, p: Vec) -> Vec:
        z = self.z
        return (
            self.origin[0] + p[0] * self.x[0] + p[1] * self.y[0] + p[2] * z[0],
            self.origin[1] + p[0] * self.x[1] + p[1] * self.y[1] + p[2] * z[1],
            self.origin[2] + p[0] * self.x[2] + p[1] * self.y[2] + p[2] * z[2],
        )

    def facets(self, facets: Sequence[Facet]) -> list[Facet]:
        if self.is_identity():
            return list(facets)
        return [(self.apply(a), self.apply(b), self.apply(c)) for a, b, c in facets]

    def to_json(self) -> dict:
        return {"origin": list(self.origin), "x": list(self.x), "y": list(self.y)}


def rotation(axis: Vec, deg: float) -> Mat:
    """Rotation matrix (rows) about the unit `axis` by `deg` degrees, right-handed (Rodrigues)."""
    x, y, z = unit(axis)
    t = math.radians(deg)
    c, s = math.cos(t), math.sin(t)
    k = 1 - c
    return (
        (c + x * x * k, x * y * k - z * s, x * z * k + y * s),
        (y * x * k + z * s, c + y * y * k, y * z * k - x * s),
        (z * x * k - y * s, z * y * k + x * s, c + z * z * k),
    )


def rotate_about(p: Vec, m: Mat, point: Vec) -> Vec:
    d = sub(p, point)
    return add(point, (dot(m[0], d), dot(m[1], d), dot(m[2], d)))
