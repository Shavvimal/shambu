"""Triangle meshes without a dependency: read and write STL, rotate, lay a face on the bed, split bodies,
and score an orientation the way a printer cares about (bed contact, overhang, height).

Why here: Bambu Studio's CLI rotates every object at once (`--rotate*`) and the assemble list carries no
transform (reference 01), so a per-part orientation has to be baked into the STL before the CLI sees it.
The GUI's "Lay on face" and auto-orient do the same thing interactively; this module does it headless so an
agent can compare orientations by numbers and then slice the winner.

Conventions: a mesh is a list of facets, each a tuple of three vertices (x, y, z) in mm. Normals are always
recomputed from the winding (STL normals are unreliable). Angles are degrees. The bed is the plane z = min z
of the mesh, and `overhang` counts facets that face down more than `threshold` degrees FROM VERTICAL (0 = a
vertical wall, 90 = a flat ceiling), the usual "45 degree rule". Bambu's `support_threshold_angle` counts from
horizontal instead (its default 30 equals 60 here): convert with 90 - angle.

Not here: mesh repair, booleans or plane cuts (Studio's Cut tool, or CAD), convex hulls, painted supports, and
anything that needs the slicer (support volume, time): `shambu sweep` asks the slicer for those.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .errors import BuildError

Vec = tuple[float, float, float]
Facet = tuple[Vec, Vec, Vec]
Matrix = tuple[Vec, Vec, Vec]  # rows

IDENTITY: Matrix = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
AXES: dict[str, Vec] = {
    "-z": (0.0, 0.0, -1.0),
    "+z": (0.0, 0.0, 1.0),
    "-x": (-1.0, 0.0, 0.0),
    "+x": (1.0, 0.0, 0.0),
    "-y": (0.0, -1.0, 0.0),
    "+y": (0.0, 1.0, 0.0),
}


# ---------------------------------------------------------------------------- vectors
def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a: Vec) -> float:
    return math.sqrt(_dot(a, a))


def unit(a: Vec) -> Vec:
    n = _norm(a)
    if n == 0:
        raise BuildError("zero-length vector")
    return (a[0] / n, a[1] / n, a[2] / n)


def apply(m: Matrix, v: Vec) -> Vec:
    return (_dot(m[0], v), _dot(m[1], v), _dot(m[2], v))


def matmul(a: Matrix, b: Matrix) -> Matrix:
    cols = [(b[0][j], b[1][j], b[2][j]) for j in range(3)]
    return tuple(tuple(_dot(a[i], cols[j]) for j in range(3)) for i in range(3))  # type: ignore[return-value]


def euler_matrix(rx: float, ry: float, rz: float) -> Matrix:
    """Rotation by rx about X, then ry about Y, then rz about Z (degrees), about the origin. This is shambu's own
    convention for the spec's `rotate` key; it is not verified against Studio's rotate gizmo."""
    cx, sx = math.cos(math.radians(rx)), math.sin(math.radians(rx))
    cy, sy = math.cos(math.radians(ry)), math.sin(math.radians(ry))
    cz, sz = math.cos(math.radians(rz)), math.sin(math.radians(rz))
    mx: Matrix = ((1, 0, 0), (0, cx, -sx), (0, sx, cx))
    my: Matrix = ((cy, 0, sy), (0, 1, 0), (-sy, 0, cy))
    mz: Matrix = ((cz, -sz, 0), (sz, cz, 0), (0, 0, 1))
    return matmul(mz, matmul(my, mx))


def euler_angles(m: Matrix) -> tuple[float, float, float]:
    """The (rx, ry, rz) whose `euler_matrix` is `m`, degrees rounded to 0.01, for the report."""
    sy = -m[2][0]
    sy = max(-1.0, min(1.0, sy))
    ry = math.asin(sy)
    if abs(math.cos(ry)) > 1e-9:
        rx = math.atan2(m[2][1], m[2][2])
        rz = math.atan2(m[1][0], m[0][0])
    else:  # gimbal lock: put everything in rz
        rx = 0.0
        rz = math.atan2(-m[0][1], m[1][1])
    return tuple(round(math.degrees(a), 2) for a in (rx, ry, rz))  # type: ignore[return-value]


def rotation_to(src: Vec, dst: Vec) -> Matrix:
    """The smallest rotation that turns direction `src` into direction `dst` (Rodrigues)."""
    s, d = unit(src), unit(dst)
    c = max(-1.0, min(1.0, _dot(s, d)))
    axis = _cross(s, d)
    if _norm(axis) < 1e-12:
        if c > 0:
            return IDENTITY
        # opposite: 180 degrees about any axis perpendicular to s
        perp = _cross(s, (1.0, 0.0, 0.0))
        if _norm(perp) < 1e-6:
            perp = _cross(s, (0.0, 1.0, 0.0))
        axis, c = unit(perp), -1.0
        sn = 0.0
    else:
        sn = _norm(axis)
        axis = unit(axis)
    x, y, z = axis
    t = 1 - c
    return (
        (c + x * x * t, x * y * t - z * sn, x * z * t + y * sn),
        (y * x * t + z * sn, c + y * y * t, y * z * t - x * sn),
        (z * x * t - y * sn, z * y * t + x * sn, c + z * z * t),
    )


def lay_on_face_matrix(normal: Vec) -> Matrix:
    """The rotation that lays the face with outward normal `normal` (in the mesh's frame) flat on the bed."""
    return rotation_to(normal, AXES["-z"])


# ---------------------------------------------------------------------------- STL
def read_stl(path: str | Path) -> list[Facet]:
    """Binary or ASCII STL to facets. Raises BuildError on anything it cannot parse."""
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as e:
        raise BuildError(f"{p}: cannot read: {e}") from e
    if len(data) < 84 and not data.lstrip().lower().startswith(b"solid"):
        raise BuildError(f"{p}: not an STL file ({len(data)} bytes)")
    if len(data) >= 84:
        (n,) = struct.unpack_from("<I", data, 80)
        if 84 + 50 * n == len(data):
            return _read_binary(data, n)
    text = data.decode("utf-8", errors="replace")
    if "facet" in text or text.lstrip().lower().startswith("solid"):
        return _read_ascii(text)
    raise BuildError(f"{p}: neither a binary nor an ASCII STL")


def _read_binary(data: bytes, n: int) -> list[Facet]:
    out: list[Facet] = []
    for rec in struct.iter_unpack("<12xfffffffffH", data[84 : 84 + 50 * n]):
        out.append(((rec[0], rec[1], rec[2]), (rec[3], rec[4], rec[5]), (rec[6], rec[7], rec[8])))
    return out


def _read_ascii(text: str) -> list[Facet]:
    out: list[Facet] = []
    verts: list[Vec] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("vertex"):
            parts = s.split()
            if len(parts) != 4:
                raise BuildError(f"bad STL vertex line: {line!r}")
            verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
            if len(verts) == 3:
                out.append((verts[0], verts[1], verts[2]))
                verts = []
    if verts:
        raise BuildError("ASCII STL ends mid-facet")
    return out


def write_stl(path: str | Path, facets: Sequence[Facet], name: str = "shambu") -> Path:
    """Binary STL with recomputed normals. Returns the path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    header = name.encode("ascii", errors="replace")[:80].ljust(80, b"\0")
    chunks = [header, struct.pack("<I", len(facets))]
    for a, b, c in facets:
        n = facet_normal((a, b, c))
        chunks.append(struct.pack("<12f", *n, *a, *b, *c))
        chunks.append(b"\0\0")
    p.write_bytes(b"".join(chunks))
    return p


# ---------------------------------------------------------------------------- geometry
def facet_normal(f: Facet) -> Vec:
    n = _cross(_sub(f[1], f[0]), _sub(f[2], f[0]))
    ln = _norm(n)
    return (n[0] / ln, n[1] / ln, n[2] / ln) if ln > 0 else (0.0, 0.0, 0.0)


def facet_area(f: Facet) -> float:
    return _norm(_cross(_sub(f[1], f[0]), _sub(f[2], f[0]))) / 2


def transform(facets: Iterable[Facet], m: Matrix) -> list[Facet]:
    return [(apply(m, a), apply(m, b), apply(m, c)) for a, b, c in facets]


def translate(facets: Iterable[Facet], d: Vec) -> list[Facet]:
    return [tuple((v[0] + d[0], v[1] + d[1], v[2] + d[2]) for v in f) for f in facets]  # type: ignore[misc]


def bbox(facets: Sequence[Facet]) -> tuple[Vec, Vec]:
    if not facets:
        raise BuildError("empty mesh")
    xs = [v[0] for f in facets for v in f]
    ys = [v[1] for f in facets for v in f]
    zs = [v[2] for f in facets for v in f]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def volume(facets: Iterable[Facet]) -> float:
    """Signed volume in mm3 (positive for an outward-wound, closed mesh)."""
    return sum(_dot(a, _cross(b, c)) for a, b, c in facets) / 6


def split_bodies(facets: Sequence[Facet], tol: float = 1e-6) -> list[list[Facet]]:
    """Connected components by shared vertices (Studio's "Split to objects"), largest first."""
    key: dict[tuple[int, int, int], int] = {}

    def vid(v: Vec) -> int:
        k = (round(v[0] / tol), round(v[1] / tol), round(v[2] / tol))
        return key.setdefault(k, len(key))

    parent: list[int] = []

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    ids = []
    for f in facets:
        trio = []
        for v in f:
            i = vid(v)
            if i == len(parent):
                parent.append(i)
            trio.append(i)
        union(trio[0], trio[1])
        union(trio[0], trio[2])
        ids.append(trio[0])
    groups: dict[int, list[Facet]] = {}
    for f, i in zip(facets, ids, strict=True):
        groups.setdefault(find(i), []).append(f)
    return sorted(groups.values(), key=lambda g: -abs(volume(g)))


# ---------------------------------------------------------------------------- orientation metrics
@dataclass(frozen=True)
class Metrics:
    """What one orientation costs. Areas in mm2, lengths in mm, angles in degrees."""

    size: Vec
    height: float
    footprint_mm2: float  # bounding-box footprint
    contact_mm2: float  # facets lying flat on the bed plane
    overhang_mm2: float  # downward facets steeper than the threshold, off the bed
    shadow_mm2: float  # the same facets projected onto the bed, summed (overlaps count twice, nothing is occluded)
    steepest_deg: float  # the largest overhang angle present off the bed (0 = none, 90 = a flat ceiling)
    centroid_z: float  # volume centroid height above the bed
    threshold_deg: float
    volume_mm3: float

    @property
    def support_share(self) -> float:
        """shadow over footprint: 0 = nothing to support, about 1 = the whole footprint (stacked overhangs can exceed 1)."""
        return self.shadow_mm2 / self.footprint_mm2 if self.footprint_mm2 else 0.0

    @property
    def score(self) -> float:
        """Lower is better. The shadow area dominates (every mm2 is support filament and a scar), bed contact
        earns credit (adhesion, no brim), and height costs a little (a tall print is a slow, wobbly print).
        The weights are the tool's own, chosen so one mm2 of support outweighs one mm2 of contact. Bridges count
        as overhang here (as in Studio's auto-orient), so `shambu sweep` has the last word."""
        return self.shadow_mm2 - 0.25 * self.contact_mm2 + 2.0 * self.height

    def to_json(self) -> dict:
        return {
            "size": [round(v, 2) for v in self.size],
            "height": round(self.height, 2),
            "footprint_mm2": round(self.footprint_mm2, 1),
            "contact_mm2": round(self.contact_mm2, 1),
            "overhang_mm2": round(self.overhang_mm2, 1),
            "shadow_mm2": round(self.shadow_mm2, 1),
            "support_share": round(self.support_share, 3),
            "steepest_deg": round(self.steepest_deg, 1),
            "centroid_z": round(self.centroid_z, 2),
            "threshold_deg": self.threshold_deg,
            "volume_mm3": round(self.volume_mm3, 1),
            "score": round(self.score, 1),
        }


def metrics(facets: Sequence[Facet], threshold_deg: float = 45.0, bed_tol: float = 0.05) -> Metrics:
    lo, hi = bbox(facets)
    size = (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])
    cos_thr = math.cos(math.radians(90.0 - threshold_deg))  # -n.z above this = steeper than the threshold
    contact = overhang = shadow = 0.0
    steepest = 0.0
    vol6 = 0.0
    cz = 0.0
    for f in facets:
        a, b, c = f
        n = _cross(_sub(b, a), _sub(c, a))
        ln = _norm(n)
        if ln == 0:
            continue
        area = ln / 2
        nz = n[2] / ln
        v = _dot(a, _cross(b, c))
        vol6 += v
        cz += v * (a[2] + b[2] + c[2]) / 4
        if nz >= 0:
            continue
        on_bed = max(a[2], b[2], c[2]) <= lo[2] + bed_tol
        if on_bed and nz < -0.999:
            contact += area
            continue
        if on_bed:
            continue
        angle = math.degrees(math.asin(min(1.0, -nz)))
        steepest = max(steepest, angle)
        if -nz > cos_thr:
            overhang += area
            shadow += area * -nz
    vol = vol6 / 6
    centroid_z = (cz / vol6 if vol6 else (lo[2] + hi[2]) / 2) - lo[2]
    return Metrics(size, size[2], size[0] * size[1], contact, overhang, shadow, steepest, centroid_z, threshold_deg, vol)


@dataclass
class Candidate:
    label: str
    normal: Vec  # outward normal in the ORIGINAL mesh frame that this candidate lays on the bed
    matrix: Matrix
    metrics: Metrics
    face_mm2: float = 0.0  # area of the facets sharing that normal in the original mesh

    @property
    def rotate(self) -> tuple[float, float, float]:
        return euler_angles(self.matrix)

    def to_json(self) -> dict:
        return {
            "label": self.label,
            "lay_on_face": [round(v, 4) for v in self.normal],
            "rotate": list(self.rotate),
            "face_mm2": round(self.face_mm2, 1),
            **self.metrics.to_json(),
        }


def face_normals(facets: Sequence[Facet], top: int = 12, quantum: float = 0.02) -> list[tuple[Vec, float]]:
    """Distinct outward normals by total facet area, largest first. Normals within `quantum` collapse together."""
    acc: dict[tuple[int, int, int], tuple[list[float], float]] = {}
    for f in facets:
        n = _cross(_sub(f[1], f[0]), _sub(f[2], f[0]))
        ln = _norm(n)
        if ln == 0:
            continue
        u = (n[0] / ln, n[1] / ln, n[2] / ln)
        k = (round(u[0] / quantum), round(u[1] / quantum), round(u[2] / quantum))
        s, a = acc.get(k, ([0.0, 0.0, 0.0], 0.0))
        s[0] += u[0] * ln
        s[1] += u[1] * ln
        s[2] += u[2] * ln
        acc[k] = (s, a + ln / 2)
    out = []
    for s, a in acc.values():
        try:
            out.append((unit((s[0], s[1], s[2])), a))
        except BuildError:
            continue
    out.sort(key=lambda t: -t[1])
    return out[:top]


def candidates(facets: Sequence[Facet], threshold_deg: float = 45.0, top: int = 12) -> list[Candidate]:
    """Orientations worth comparing: as-is, the six axis faces, and the largest flat faces laid on the bed.
    Sorted by score, best first. Candidates whose rotations coincide (within 1 degree) are merged."""
    seen: list[Vec] = []
    out: list[Candidate] = []

    def add(label: str, normal: Vec, face_mm2: float) -> None:
        n = unit(normal)
        for s in seen:
            if _dot(s, n) > math.cos(math.radians(1.0)):
                return
        seen.append(n)
        m = lay_on_face_matrix(n)
        out.append(Candidate(label, n, m, metrics(transform(facets, m), threshold_deg), face_mm2))

    add("as is", AXES["-z"], 0.0)
    for name, axis in AXES.items():
        add(f"{name} face down", axis, 0.0)
    for n, area in face_normals(facets, top):
        add(f"largest face {len(out) - 6}", n, area)
    for c in out:  # fill face areas for the axis candidates from the same accumulation
        if c.face_mm2 == 0.0:
            c.face_mm2 = next((a for n, a in face_normals(facets, 200) if _dot(n, c.normal) > math.cos(math.radians(1.0))), 0.0)
    out.sort(key=lambda c: c.metrics.score)
    return out


def describe_candidates(cands: Sequence[Candidate]) -> list[str]:
    head = (
        f"{'#':>2} {'orientation':20s} {'score':>8} {'support mm2':>11} {'share':>6} {'contact':>8} {'height':>7} {'steepest':>8}  "
        f"lay_on_face [nx, ny, nz]        rotate [x, y, z]"
    )
    lines = [head]
    for i, c in enumerate(cands, 1):
        m = c.metrics
        n = ", ".join(f"{v:.4f}" for v in c.normal)
        lines.append(
            f"{i:>2} {c.label:20s} {m.score:8.1f} {m.shadow_mm2:11.1f} {m.support_share:6.2f} {m.contact_mm2:8.1f} {m.height:7.1f} {m.steepest_deg:8.1f}  "
            f"[{n}]  [{c.rotate[0]:.1f}, {c.rotate[1]:.1f}, {c.rotate[2]:.1f}]"
        )
    return lines


@dataclass(frozen=True)
class Orientation:
    """How a spec asks for a part to be turned: lay a face down first, then rotate by Euler angles."""

    lay_on_face: Vec | None = None
    rotate: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def is_identity(self) -> bool:
        return self.lay_on_face is None and all(abs(a) < 1e-9 for a in self.rotate)

    def matrix(self) -> Matrix:
        m = IDENTITY if self.lay_on_face is None else lay_on_face_matrix(self.lay_on_face)
        return matmul(euler_matrix(*self.rotate), m)

    def tag(self) -> str:
        """A short file-name suffix that identifies the orientation."""
        parts = []
        if self.lay_on_face is not None:
            parts.append("face" + "_".join(f"{v:+.2f}" for v in self.lay_on_face))
        if any(abs(a) > 1e-9 for a in self.rotate):
            parts.append("rot" + "_".join(f"{a:g}" for a in self.rotate))
        return "-".join(parts).replace("+", "p").replace("-", "m").replace(".", "")

    def to_json(self) -> dict:
        d: dict = {}
        if self.lay_on_face is not None:
            d["lay_on_face"] = list(self.lay_on_face)
        if any(abs(a) > 1e-9 for a in self.rotate):
            d["rotate"] = list(self.rotate)
        return d

    @classmethod
    def from_json(cls, d: dict) -> Orientation:
        lof = d.get("lay_on_face")
        rot = d.get("rotate") or (0.0, 0.0, 0.0)
        if lof is not None and len(lof) != 3:
            raise BuildError("lay_on_face needs three numbers [nx, ny, nz]")
        if len(rot) != 3:
            raise BuildError("rotate needs three angles [x, y, z] in degrees")
        return cls(tuple(float(v) for v in lof) if lof is not None else None, tuple(float(a) for a in rot))  # type: ignore[arg-type]


def _orientation_hash(srcs: Sequence[Path], orientation: Orientation) -> str:
    """Eight hex chars that identify the source files (resolved paths) and the exact orientation, so two parts with
    one stem in different folders, or rotations 1.2 and 12 degrees, never share an output file."""
    key = json.dumps([[str(p.resolve()) for p in srcs], orientation.to_json()], sort_keys=True)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


def oriented_copies(srcs: Sequence[str | Path], orientation: Orientation, outdir: str | Path) -> list[Path]:
    """Write `<outdir>/<stem>__<tag>-<hash>.stl` for every file with the orientation applied in ONE shared frame: the
    files of a merged object are rotated together and moved by one translation (from their combined bounding box)
    so they keep their alignment, and the group ends on z = 0 at x, y >= 0. Returns the new paths in order.
    Nothing is written for the identity orientation (the original paths come back)."""
    paths = [Path(p) for p in srcs]
    if orientation.is_identity or not paths:
        return paths
    m = orientation.matrix()
    meshes = [transform(read_stl(p), m) for p in paths]
    lo, _ = bbox([f for mesh in meshes for f in mesh])
    shift = (-lo[0], -lo[1], -lo[2])
    h = _orientation_hash(paths, orientation)
    out = []
    for p, mesh in zip(paths, meshes, strict=True):
        out.append(
            write_stl(Path(outdir) / f"{p.stem}__{orientation.tag()}-{h}.stl", translate(mesh, shift), f"{p.stem} {orientation.tag()}")
        )
    return out


def oriented_copy(src: str | Path, orientation: Orientation, outdir: str | Path) -> Path:
    """One file: see `oriented_copies`."""
    return oriented_copies([src], orientation, outdir)[0]


@dataclass
class Summary:
    """`shambu orient` output for one file."""

    path: Path
    facets: int
    bodies: int
    candidates: list[Candidate] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"path": str(self.path), "facets": self.facets, "bodies": self.bodies, "candidates": [c.to_json() for c in self.candidates]}
