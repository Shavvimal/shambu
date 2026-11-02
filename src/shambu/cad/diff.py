"""`shambu cad diff`: what changed between two exports of a part, and what that means for printing.

1. Measures: volume, area, box, centre of mass and the inertia diagonal (meshgeom.inertia), with deltas.
2. Section profile: the section area at N planes along each axis. Two exports of an unchanged part can tessellate
   differently: a section of perimeter P moves by at most P x t in area (t the faceting tolerance), so a plane counts
   as changed only beyond 2 x P x t (both exports). Runs of changed planes are reported as bands ("changed between
   z 18.0 and 24.0") so a person can find the change without a picture.
3. Features (features.py on both): matched by kind and axis with the centre within max(0.5 mm, d / 4): `same` or
   `resized` (a through hole whose depth follows the part's thickness stays `same`); the rest paired by kind, diameter and axis: `moved`; the remainder `gone` and `new`.
4. Impact, with a shambu plate spec: the plates that hold the changed STL (reslice them), whether
   `shambu patch --replace-mesh` applies (a size change needs `--allow-size-change`; a changed bed face needs a new
   orientation), and which changed features carry a fit in the CAD spec (`diff.coupon`: print a coupon first).
5. Intent, with an expectation file (`{"changes": [{"feature": "h1", "diameter_mm": 3.5, "coupon": true},
   {"measure": "size_z", "delta_mm": 2}]}`): a change not listed is `diff.unexpected` (FAIL), a listed change that did
   not happen is `diff.expected_missing` (FAIL), and a changed fit whose entry makes no coupon decision is
   `diff.coupon_undecided` (FAIL).

The frame is assumed shared (the exports come from one CAD frame). What it does not do: no alignment of a rotated or
moved part, no change below the section spacing, nothing a STEP B-rep carries that a mesh does not, no file reading
(load.py), no printing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import meshgeom as G
from ..errors import CadSpecError
from ..findings import FAIL, INFO, MEASURED, WARN, Finding, Fix, Location
from ..mesh import bbox
from . import features as FT
from . import linalg as L
from .fits import COUPON_KINDS
from .linalg import Facet
from .solid import DEFAULT_FACETING_MM, surface_area
from .spec import CadSpec, PartSpec

SAME_MM = 0.01
SIZE_AXES = ("size_x", "size_y", "size_z")

NOT_CHECKED: tuple[dict[str, str], ...] = (
    {
        "what": "a rotation or move of the whole part",
        "why": "the exports are assumed to share one frame",
        "use": "export both in the same CAD frame",
    },
    {
        "what": "changes thinner than the section spacing",
        "why": "sections are sampled",
        "use": "--sections N, or --surface with the geom extra",
    },
    {"what": "face types, exact edges and tolerances of the STEP", "why": "diff reads meshes", "use": "the CAD session"},
)


def measures(facets: Sequence[Facet]) -> dict:
    vol, com = G.moments(facets)
    lo, hi = bbox(facets)
    ixx, iyy, izz = G.inertia(facets)
    return {
        "volume_mm3": vol,
        "area_mm2": surface_area(facets),
        "bbox_min": list(lo),
        "bbox_max": list(hi),
        "size_x": hi[0] - lo[0],
        "size_y": hi[1] - lo[1],
        "size_z": hi[2] - lo[2],
        "cog_mm": list(com),
        "inertia_mm5": [ixx, iyy, izz],
    }


def measure_deltas(old: dict, new: dict) -> dict:
    out = {}
    for k, a in old.items():
        b = new[k]
        if isinstance(a, list):
            out[k] = {
                "old": [round(x, 4) for x in a],
                "new": [round(x, 4) for x in b],
                "delta": [round(y - x, 4) for x, y in zip(a, b, strict=True)],
            }
        else:
            out[k] = {"old": round(a, 4), "new": round(b, 4), "delta": round(b - a, 4)}
    return out


def sections(old: Sequence[Facet], new: Sequence[Facet], n: int, tol_mm: float) -> tuple[list[dict], list[Finding]]:
    lo_a, hi_a = bbox(old)
    lo_b, hi_b = bbox(new)
    out, fs = [], []
    for ax in range(3):
        lo, hi = min(lo_a[ax], lo_b[ax]), max(hi_a[ax], hi_b[ax])
        span = hi - lo
        pos = [lo + (k + 0.5) * span / n for k in range(n)]
        olds, news, changed = [], [], []
        for v in pos:
            a_area, a_per = G.section(old, ax, v)
            b_area, b_per = G.section(new, ax, v)
            olds.append(a_area)
            news.append(b_area)
            changed.append(abs(b_area - a_area) > 2 * max(a_per, b_per) * tol_mm + 1e-9)
        bands = []
        k = 0
        while k < n:
            if changed[k]:
                j = k
                while j + 1 < n and changed[j + 1]:
                    j += 1
                a = pos[k - 1] if k > 0 else lo
                b = pos[j + 1] if j + 1 < n else hi
                bands.append([round(a, 3), round(b, 3)])
                k = j + 1
            else:
                k += 1
        name = "xyz"[ax]
        out.append(
            {
                "axis": name,
                "positions_mm": [round(p, 3) for p in pos],
                "old_mm2": [round(x, 3) for x in olds],
                "new_mm2": [round(x, 3) for x in news],
                "changed_between_mm": bands,
            }
        )
        for a, b in bands:
            fs.append(
                Finding(
                    "diff.section",
                    INFO,
                    "diff",
                    f"section area changed between {name} {a:.1f} and {b:.1f}",
                    MEASURED,
                    measure={"axis": name, "from_mm": a, "to_mm": b},
                )
            )
    return out, fs


@dataclass(frozen=True)
class Change:
    kind: str  # same, resized, moved, gone, new
    feature: str  # "hole" or "boss"
    old: FT.Feature | None
    new: FT.Feature | None
    id: str

    @property
    def diameter_delta(self) -> float:
        return (self.new.diameter_mm - self.old.diameter_mm) if self.old and self.new else 0.0

    @property
    def move(self) -> tuple[float, float, float]:
        if self.old and self.new:
            return shift(self.old, self.new)
        return (0.0, 0.0, 0.0)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "change": self.kind,
            "kind": self.feature,
            "old": self.old.to_json() if self.old else None,
            "new": self.new.to_json() if self.new else None,
            "delta_diameter_mm": round(self.diameter_delta, 4),
            "delta_mm": [round(x, 4) + 0.0 for x in self.move],
        }


def shift(old: FT.Feature, new: FT.Feature) -> tuple[float, float, float]:
    """How far the feature moved. Two through holes are compared by their axis lines (the mid-depth point follows the
    part's thickness, not the hole)."""
    d = L.sub(new.centre, old.centre)
    if old.ends == new.ends == "through":
        d = L.sub(d, L.mul(old.axis, L.dot(d, old.axis)))
    return (d[0] + 0.0, d[1] + 0.0, d[2] + 0.0)


def _fid(f: FT.Feature) -> str:
    return f"{f.kind} d{f.diameter_mm:.2f} at ({', '.join(f'{x:.1f}' for x in f.centre)})"


def _parallel(a: FT.Feature, b: FT.Feature) -> bool:
    return abs(L.dot(a.axis, b.axis)) >= FT.PARALLEL_COS


def _off_axis(f: FT.Feature, p: Sequence[float]) -> float:
    d = L.sub((float(p[0]), float(p[1]), float(p[2])), f.centre)
    return L.norm(L.sub(d, L.mul(f.axis, L.dot(d, f.axis))))


def match_features(old: Sequence[FT.Feature], new: Sequence[FT.Feature], names: Mapping[int, str] | None = None) -> list[Change]:
    """Changes between two inventories. `names` maps an old feature index to a name (a spec hole)."""
    names = names or {}
    left_new = list(range(len(new)))
    out: list[Change] = []
    left_old = []
    for i, f in enumerate(old):
        cands = [
            j
            for j in left_new
            if new[j].kind == f.kind and _parallel(f, new[j]) and L.norm(shift(f, new[j])) <= max(0.5, f.diameter_mm / 4)
        ]
        if not cands:
            left_old.append(i)
            continue
        j = min(cands, key=lambda k: (L.norm(shift(f, new[k])), k))
        left_new.remove(j)
        g = new[j]
        same = (
            abs(g.diameter_mm - f.diameter_mm) <= SAME_MM
            and L.norm(shift(f, g)) <= SAME_MM
            and (abs(g.depth_mm - f.depth_mm) <= SAME_MM or f.ends == g.ends == "through")
        )
        out.append(Change("same" if same else "resized", f.kind, f, g, names.get(i, _fid(f))))
    for i in list(left_old):
        f = old[i]
        cands = [
            j for j in left_new if new[j].kind == f.kind and _parallel(f, new[j]) and abs(new[j].diameter_mm - f.diameter_mm) <= SAME_MM
        ]
        if not cands:
            continue
        j = min(cands, key=lambda k: (L.norm(shift(f, new[k])), k))
        left_new.remove(j)
        left_old.remove(i)
        out.append(Change("moved", f.kind, f, new[j], names.get(i, _fid(f))))
    out += [Change("gone", old[i].kind, old[i], None, names.get(i, _fid(old[i]))) for i in left_old]
    out += [Change("new", new[j].kind, None, new[j], _fid(new[j])) for j in left_new]
    return out


def hole_names(part: PartSpec | None, inv: Sequence[FT.Feature]) -> dict[int, str]:
    """Old feature index -> the spec hole whose mouth lies on that hole's axis (within its radius)."""
    out: dict[int, str] = {}
    if part is None:
        return out
    for h in part.holes:
        for i, f in enumerate(inv):
            if f.kind == "hole" and abs(L.dot(f.axis, h.axis)) >= FT.PARALLEL_COS and _off_axis(f, h.mouth) <= f.diameter_mm / 2:
                out.setdefault(i, h.name)
    return out


def feature_findings(changes: Sequence[Change], spec: CadSpec | None, part: PartSpec | None) -> list[Finding]:
    fs = []
    fit_of = {h.name: spec.fits[h.fit] for h in part.holes} if spec is not None and part is not None else {}
    for c in changes:
        if c.kind == "same":
            continue
        f = c.new or c.old
        if f is None:
            continue
        loc = Location("part", f.centre)
        msg = {
            "resized": f"{c.id}: d {c.old.diameter_mm if c.old else 0:.3f} to {c.new.diameter_mm if c.new else 0:.3f} mm",
            "moved": f"{c.id}: moved by ({', '.join(f'{x:+.3f}' for x in c.move)}) mm",
            "gone": f"{c.id}: gone",
            "new": f"{c.id}: new",
        }[c.kind]
        fs.append(
            Finding(
                f"diff.feature_{c.kind}",
                INFO,
                "diff",
                msg,
                MEASURED,
                None,
                loc,
                {"delta_mm": list(c.move), "delta_diameter_mm": c.diameter_delta},
            )
        )
        fit = fit_of.get(c.id)
        if fit is not None and fit.kind in COUPON_KINDS and c.kind in ("resized", "moved", "new"):
            fs.append(
                Finding(
                    "diff.coupon",
                    WARN,
                    "diff",
                    f"{c.id} carries the {fit.kind} fit {fit.name} and changed: print a coupon of it before the part",
                    MEASURED,
                    None,
                    loc,
                    {"fit": fit.name, "change": c.kind},
                    Fix("print a coupon of the changed fit in its own orientation"),
                )
            )
    return fs


def _plate_paths(doc: Mapping) -> list[tuple[int, str, str]]:
    """(plate number, plate name, part path) of every part in a plate spec, groups included."""
    out = []
    for i, pl in enumerate(doc.get("plates") or [], 1):
        stack = list(pl.get("parts") or [])
        while stack:
            p = stack.pop(0)
            if isinstance(p, Mapping):
                if p.get("path"):
                    out.append((i, str(pl.get("name") or f"plate {i}"), str(p["path"])))
                stack += list(p.get("parts") or [])
    return out


def impact(
    plate_doc: Mapping, plate_path: str, old_path: str, new_path: str, size_changed: bool, bed_changed: bool
) -> tuple[dict, list[Finding]]:
    base = Path(plate_path).parent
    targets = {Path(old_path).resolve(), Path(new_path).resolve()}
    names = {Path(old_path).name, Path(new_path).name}
    hits: list[dict] = []
    plates_set: set[int] = set()
    for num, name, path in _plate_paths(plate_doc):
        rp = (base / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        if rp in targets or Path(path).name in names:
            hits.append({"plate": num, "name": name, "path": path})
            plates_set.add(num)
    plates = sorted(plates_set)
    replace = "needs --allow-size-change" if size_changed else "applies"
    if bed_changed:
        replace += "; the bed face changed: orient again (shambu orient, lay_on_face)"
    block = {"reslice": hits, "plates": plates, "replace_mesh": replace}
    fs = []
    for num in plates:
        fs.append(
            Finding(
                "diff.reslice",
                INFO,
                "diff",
                f"plate {num} of {Path(plate_path).name} holds this part: build and slice it again",
                MEASURED,
                fix=Fix("rebuild the plate", command=f"shambu build {plate_path} --slice"),
                plate=num,
            )
        )
    return block, fs


def expect_findings(expect: Mapping, deltas: dict, changes: Sequence[Change], size_tol_mm: float, fit_names: set[str]) -> list[Finding]:
    """Changes the expectation file does not list, listed changes that did not happen, and changed fits with no
    coupon decision. Raises CadSpecError when the file breaks its shape."""
    items = expect.get("changes")
    if not isinstance(items, list):
        raise CadSpecError('the expectation file is {"changes": [{"feature": ...} | {"measure": ...}]}')
    fs: list[Finding] = []
    listed_measures: dict[str, Mapping] = {}
    listed_features: dict[str, Mapping] = {}
    for k, it in enumerate(items):
        if not isinstance(it, Mapping) or ("feature" in it) == ("measure" in it):
            raise CadSpecError(f"changes[{k}]: one of 'feature' or 'measure'")
        if "measure" in it:
            if it["measure"] not in SIZE_AXES:
                raise CadSpecError(f"changes[{k}].measure: one of {SIZE_AXES}")
            listed_measures[str(it["measure"])] = it
        else:
            listed_features[str(it["feature"])] = it
    for ax in SIZE_AXES:
        d = deltas[ax]["delta"]
        want = listed_measures.get(ax)
        moved = abs(d) > size_tol_mm
        if want is None and moved:
            fs.append(
                Finding(
                    "diff.unexpected",
                    FAIL,
                    "diff",
                    f"{ax} changed by {d:+.3f} mm and the expectation does not list it",
                    MEASURED,
                    measure={"measure": ax, "delta_mm": d, "expected_from": "expect"},
                )
            )
        elif want is not None:
            target = want.get("delta_mm")
            if target is not None and abs(d - float(target)) > size_tol_mm:
                fs.append(
                    Finding(
                        "diff.unexpected" if moved else "diff.expected_missing",
                        FAIL,
                        "diff",
                        f"{ax} changed by {d:+.3f} mm, the expectation says {float(target):+g}",
                        MEASURED,
                        measure={"measure": ax, "delta_mm": d, "expected_delta_mm": float(target), "expected_from": "expect"},
                    )
                )
    seen = set()
    for c in changes:
        if c.kind == "same":
            continue
        want = listed_features.get(c.id)
        seen.add(c.id)
        if want is None:
            fs.append(
                Finding(
                    "diff.unexpected",
                    FAIL,
                    "diff",
                    f"{c.id}: {c.kind}, and the expectation does not list it",
                    MEASURED,
                    measure={"feature": c.id, "change": c.kind, "expected_from": "expect"},
                )
            )
            continue
        if want.get("change") is not None and want["change"] != c.kind:
            fs.append(
                Finding(
                    "diff.unexpected",
                    FAIL,
                    "diff",
                    f"{c.id}: {c.kind}, the expectation says {want['change']}",
                    MEASURED,
                    measure={"feature": c.id, "change": c.kind},
                )
            )
        if want.get("diameter_mm") is not None and (c.new is None or abs(c.new.diameter_mm - float(want["diameter_mm"])) > SAME_MM * 2):
            got = f"{c.new.diameter_mm:.3f}" if c.new else "none"
            fs.append(
                Finding(
                    "diff.unexpected",
                    FAIL,
                    "diff",
                    f"{c.id}: d {got} mm, the expectation says {want['diameter_mm']}",
                    MEASURED,
                    measure={"feature": c.id},
                )
            )
        if c.id in fit_names and "coupon" not in want:
            fs.append(
                Finding(
                    "diff.coupon_undecided",
                    FAIL,
                    "diff",
                    f"{c.id} carries a fit and changed; its expectation entry makes no coupon decision",
                    MEASURED,
                    measure={"feature": c.id},
                    fix=Fix('add "coupon": true (print one first) or false (with the reason in the change note) to the entry'),
                )
            )
    for name in listed_features:
        if name not in seen:
            fs.append(
                Finding(
                    "diff.expected_missing",
                    FAIL,
                    "diff",
                    f"{name}: the expectation lists a change that did not happen",
                    MEASURED,
                    measure={"feature": name, "expected_from": "expect"},
                )
            )
    return fs


def faceting(records: Sequence[dict | None]) -> tuple[float, str]:
    vals = [
        float(r["stl"]["faceting_tol_mm"])
        for r in records
        if r and isinstance(r.get("stl"), dict) and r["stl"].get("faceting_tol_mm") is not None
    ]
    if vals:
        return max(vals), "records"
    return DEFAULT_FACETING_MM, f"default {DEFAULT_FACETING_MM} mm faceting (no record gives one)"
