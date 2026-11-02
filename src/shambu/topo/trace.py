"""Trace a density map into window polygons on the plate-like walls of a part.

Per face (a rectangle on a wall whose normal is one grid axis, with the wall's extent along that normal), on a
raster of `rules.raster_mm`:

1. Class each raster column through the domain grid: OPEN (no wall), PURE (the wall and nothing attached within
   `rib_mm` behind or in front of it) or OTHER (a rib, a boss or another wall meets it).
2. Average the density over the cells inside the wall (outside counts as 0) and sample it bilinearly. A candidate is
   PURE and below `threshold`.
3. Keep-outs: every passive cell and every keep-out ball within `clearance + margin` of the wall, projected into the
   face plane and grown by `sqrt(grow^2 - gap^2)` (passive cells up to that reach outside the face rectangle count
   too); a rim along each closed face edge; OTHER grown by the rib (or the
   rim) width. An optional opening of `clip_r_mm` rounds where a keep-out clips.
4. Clean the raster: a closing of 0.6 mm, then an opening of 0.3 mm.
5. Minimum rib: material thinner than `rib_mm` between two windows grows to the rib; material that thin between a
   window and air becomes window. Then an opening of `corner_r_mm` rounds corners and deletes windows narrower than
   twice that radius.
6. Print rule, when the build axis lies in the face plane: scanning down from the top, a window cell stays only when
   the layer above has window or air within one raster cell to either side, so roofs rise at 45 degrees. A run with
   nothing above keeps an `apex_mm` apex, so a square hole becomes a pointed arch.
7. Polygons (shapely): a window around an island of material is cut by a strip `rib_mm` wide through each island,
   along the build axis, so no window holds an island and every island stays joined to the wall; simplify from 0.3 mm,
   raised until at most `max_vertices` (a bounded search); drop windows under `min_area_mm2` or narrower than
   `min_width_mm`; round to 0.05 mm. Area and mean density come from the emitted polygon.
8. Report each window's 3D clearance to every passive region and keep-out ball (`topo.clearance` FAIL at or under
   `clearance_mm`), its removed volume, mean density and the roof length flatter than `flat_report_deg`
   (`topo.roof_bridge` WARN past the apex), and the area left after each stage (`topo.trace_rules_dominate` WARN when
   the rules removed more than half the candidate area).

The run's `passive.npy` must be there when run.json lists passive regions (else TopoError, exit 3): without it the
keep-outs would silently vanish. A face raster over `spec.MAX_RASTER` cells is a TopoSpecError before allocation.

What it does not do: no CAD edit and no parameter file: the polygons are data for the user's own CAD script. No
printing. Needs numpy, scipy and shapely (the `topo-trace` extra).
"""

from __future__ import annotations

import itertools
import math
from pathlib import Path

import numpy as np
import shapely
from scipy import ndimage
from shapely import geometry, ops

from ..errors import TopoError, TopoSpecError
from ..findings import Finding
from .result import finding
from .spec import AXES, MAX_RASTER, Face, Faces, TraceRules, plane_axes

OPEN, PURE, OTHER = 0, 1, 2
CLOSE_MM, OPEN_MM = 0.6, 0.3  # raster clean-up: fill hairline gaps, then drop slivers
SIMPLIFY_MM = 0.3
SIMPLIFY_STEPS = 40  # the tolerance grows 1.5 x a step: 0.3 mm to about 3e6 mm
ROUND_MM = 0.05
DOMINATE = 0.5


def disc(radius_px: float) -> np.ndarray:
    r = max(0, math.floor(radius_px))
    y, x = np.mgrid[-r : r + 1, -r : r + 1]
    return x * x + y * y <= radius_px * radius_px + 1e-9


def opening(m: np.ndarray, radius_px: float) -> np.ndarray:
    if radius_px < 1:
        return m
    se = disc(radius_px)
    p = se.shape[0]
    return ndimage.binary_opening(np.pad(m, p), structure=se)[p:-p, p:-p]


def closing(m: np.ndarray, radius_px: float, pad_value: np.ndarray | None = None) -> np.ndarray:
    """A closing; `pad_value` (the padded array) lets cells beyond open edges count as window."""
    if radius_px < 1:
        return m
    se = disc(radius_px)
    p = se.shape[0]
    pm = np.pad(m, p) if pad_value is None else pad_value
    return ndimage.binary_closing(pm, structure=se)[p:-p, p:-p]


def grow(m: np.ndarray, mm: float, px_mm: float) -> np.ndarray:
    """Cells within `mm` of `m` (Euclidean, on the raster)."""
    if not m.any():
        return m.copy()
    return ndimage.distance_transform_edt(~m) * px_mm <= mm + 1e-9


def print_rule(win: np.ndarray, air: np.ndarray, up: int, top_open: bool, apex_px: int, side_px: int = 1) -> np.ndarray:
    """Roofs at 45 degrees or steeper. `up` is the raster axis (0 or 1) of the build direction (+). Rows are scanned from
    the top; a window cell stays when the kept row above has window or air within `side_px` cells. A run with no
    such support keeps an apex of `apex_px` cells at its centre."""
    w = np.moveaxis(win, up, 0)
    a = np.moveaxis(air, up, 0)
    out = np.zeros_like(w)
    prev = np.ones(w.shape[1], dtype=bool) if top_open else np.zeros(w.shape[1], dtype=bool)
    for r in range(w.shape[0] - 1, -1, -1):
        cur = w[r]
        support = ndimage.binary_dilation(prev, structure=np.ones(2 * side_px + 1, dtype=bool)) if prev.any() else prev
        ok = cur & support
        labels, n = ndimage.label(cur)
        for k in range(1, n + 1):
            run = np.flatnonzero(labels == k)
            if not ok[run].any():
                mid = (run[0] + run[-1]) / 2
                half = (apex_px - 1) / 2
                ok[run[(run >= mid - half - 1e-9) & (run <= mid + half + 1e-9)]] = True
        out[r] = ok
        prev = ok | a[r]
    return np.moveaxis(out, 0, up)


def _run_entry(run_doc: dict, keep: float | None) -> dict:
    runs = run_doc.get("runs") or []
    if not runs:
        raise TopoSpecError(["run.json holds no optimised run (was it solved with --full-only?)"])
    if keep is None:
        return runs[0]
    for r in runs:
        if abs(r["volfrac"] - keep) < 1e-9:
            return r
    raise TopoSpecError([f"no run at volfrac {keep:g}; the run file has " + ", ".join(f"{r['volfrac']:g}" for r in runs)])


def trace(run_doc: dict, run_dir: Path, faces: Faces, rules: TraceRules) -> tuple[dict, list[Finding], list[str]]:
    entry = _run_entry(run_doc, rules.keep_volfrac)
    g = run_doc["grid"]
    origin, pitch = np.asarray(g["origin"], dtype=float), float(g["pitch"])
    names = list(run_doc.get("passive", []))
    try:
        dens = np.load(run_dir / entry["files"]["density"])
        ppath = run_dir / "passive.npy"
        if ppath.exists():
            passive = np.load(ppath)
        elif names:
            raise TopoError(f"{ppath}: missing, and run.json lists passive regions ({', '.join(names)}): without it no keep-out holds")
        else:
            passive = np.zeros(dens.shape, dtype=np.int16)
    except (OSError, ValueError) as e:
        raise TopoError(f"{run_dir}: cannot read the density map: {e}") from e
    if passive.shape != dens.shape:
        raise TopoError(f"{run_dir}: passive.npy has shape {passive.shape}, the density map {dens.shape}")
    grid = _Grid(dens, passive, origin, pitch, names)
    out_faces, findings, text = [], [], []
    for face in faces.faces:
        res, ff = _face(grid, face, faces, rules)
        out_faces.append(res)
        findings += ff
        text.append(
            f"face {face.name}: {len(res['windows'])} window(s), "
            + ", ".join(f"{w['name']} {w['area_mm2']:.0f} mm2 clearance {w['clearance']['mm']}" for w in res["windows"])
            + (f"; dropped {len(res['dropped'])}" if res["dropped"] else "")
        )
    data = {
        "volfrac": entry["volfrac"],
        "rules": {k: getattr(rules, k) for k in rules.__dataclass_fields__},
        "build_axis": faces.build_axis,
        "faces": out_faces,
    }
    return data, findings, text


class _Grid:
    def __init__(self, dens: np.ndarray, passive: np.ndarray, origin: np.ndarray, pitch: float, names: list[str]):
        self.dens, self.passive, self.origin, self.pitch, self.names = dens, passive, origin, pitch, names
        self.dom = ~np.isnan(dens)

    def centres(self, axis: int) -> np.ndarray:
        return self.origin[axis] + self.pitch * (np.arange(self.dens.shape[axis]) + 0.5)


def _face(grid: _Grid, face: Face, faces: Faces, rules: TraceRules) -> tuple[dict, list[Finding]]:
    n = AXES.index(face.axis)
    a, b = plane_axes(face.axis)
    h, H = grid.pitch, rules.raster_mm
    s0, s1 = face.slab
    kc = grid.centres(n)
    slab_k = np.flatnonzero((kc >= s0) & (kc <= s1))
    base: dict = {"name": face.name, "axis": face.axis, "plane": AXES[a] + AXES[b], "windows": [], "dropped": [], "stage_areas_mm2": {}}
    if not len(slab_k):
        base["dropped"].append({"reason": "the slab holds no cell centre"})
        return base, []
    dom = np.moveaxis(grid.dom, n, -1)  # (Na, Nb, Nn)
    dens = np.moveaxis(np.nan_to_num(grid.dens, nan=0.0), n, -1)
    pas = np.moveaxis(grid.passive, n, -1)
    near = np.flatnonzero(((kc < s0) & (kc >= s0 - rules.rib_mm)) | ((kc > s1) & (kc <= s1 + rules.rib_mm)))
    in_slab = dom[:, :, slab_k]
    cls = np.full(dom.shape[:2], OTHER, dtype=np.int8)
    cls[~in_slab.any(axis=2)] = OPEN
    attached = dom[:, :, near].any(axis=2) if len(near) else np.zeros(dom.shape[:2], dtype=bool)
    cls[in_slab.all(axis=2) & ~attached] = PURE
    slab_mean = dens[:, :, slab_k].mean(axis=2)

    nu = max(1, math.ceil((face.u[1] - face.u[0]) / H - 1e-9))
    nv = max(1, math.ceil((face.v[1] - face.v[0]) / H - 1e-9))
    reach = rules.clearance_mm + rules.margin_mm
    pad = math.ceil(reach / H) + 1  # the keep-out raster reaches this far past the face rectangle
    if (nu + 2 * pad) * (nv + 2 * pad) > MAX_RASTER:
        raise TopoSpecError(
            [f"face {face.name}: a {nu} x {nv} raster at raster_mm {H:g} is over the cap of {MAX_RASTER} cells; raise raster_mm"]
        )
    uu = face.u[0] + H * (np.arange(nu) + 0.5)
    vv = face.v[0] + H * (np.arange(nv) + 0.5)
    A, B = np.meshgrid((uu - grid.origin[a]) / h - 0.5, (vv - grid.origin[b]) / h - 0.5, indexing="ij")
    d2 = ndimage.map_coordinates(slab_mean, [A, B], order=1, mode="nearest")
    ia, ib, inside = _cell_index(A, B, cls.shape)
    c2 = np.full((nu, nv), OPEN, dtype=np.int8)
    c2[inside] = cls[ia[inside], ib[inside]]
    air = c2 == OPEN
    px_area = H * H

    stages: dict[str, float] = {}
    win = (c2 == PURE) & (d2 < rules.threshold)
    stages["candidate"] = float(win.sum() * px_area)

    keep = np.zeros_like(win)
    # passive cells near the wall, one gap level at a time, on a raster padded by the reach
    up_ = face.u[0] + H * (np.arange(-pad, nu + pad) + 0.5)
    vp_ = face.v[0] + H * (np.arange(-pad, nv + pad) + 0.5)
    Ap, Bp = np.meshgrid((up_ - grid.origin[a]) / h - 0.5, (vp_ - grid.origin[b]) / h - 0.5, indexing="ij")
    iap, ibp, inside_p = _cell_index(Ap, Bp, cls.shape)
    for k in range(dom.shape[2]):
        lo, hi = grid.origin[n] + h * k, grid.origin[n] + h * (k + 1)
        gap = max(0.0, s0 - hi, lo - s1)
        if gap >= reach or not (pas[:, :, k] > 0).any():
            continue
        foot = np.zeros(Ap.shape, dtype=bool)
        foot[inside_p] = pas[:, :, k][iap[inside_p], ibp[inside_p]] > 0
        keep |= grow(foot, math.sqrt(reach**2 - gap**2), H)[pad:-pad, pad:-pad]
    balls = list(faces.keepouts) + list(face.keepouts)
    U, V = np.meshgrid(uu, vv, indexing="ij")
    for ball in balls:
        c = ball["c"]
        gap = max(0.0, s0 - c[n], c[n] - s1)
        rr = ball["r"] + reach
        if gap < rr:
            keep |= np.hypot(U - c[a], V - c[b]) <= math.sqrt(rr**2 - gap**2)
    rim = rules.rim_mm
    edges = {"u_lo": U - face.u[0], "u_hi": face.u[1] - U, "v_lo": V - face.v[0], "v_hi": face.v[1] - V}
    for name, dist in edges.items():
        if name not in face.open_edges:
            keep |= dist < rim
    other_w = rules.rib_mm if rules.other_grow == "rib" else rules.rim_mm
    keep |= grow(c2 == OTHER, other_w, H)
    win &= ~keep
    if rules.clip_r_mm:
        win = opening(win, rules.clip_r_mm / H)
    stages["after_keepouts"] = float(win.sum() * px_area)

    win = closing(win, CLOSE_MM / H) & ~keep
    win = opening(win, OPEN_MM / H)
    stages["after_cleanup"] = float(win.sum() * px_area)

    win = _rib_rule(win, air, keep, face, rules)
    stages["after_rib"] = float(win.sum() * px_area)
    win = opening(win, rules.corner_r_mm / H)
    stages["after_round"] = float(win.sum() * px_area)

    build = AXES.index(faces.build_axis)
    rule_applies = build != n
    if rule_applies:
        up = 0 if build == a else 1
        top_edge = "u_hi" if up == 0 else "v_hi"
        side = max(1, round(1 / math.tan(math.radians(rules.roof_deg))))
        win = print_rule(win, air, up, top_edge in face.open_edges, max(1, round(rules.apex_mm / H)), side)
    stages["after_print_rule"] = float(win.sum() * px_area)

    windows, dropped = _polygons(win, uu, vv, H, face, rules, (0 if build == a else 1) if rule_applies else 1)
    findings: list[Finding] = []
    thick = s1 - s0
    for i, (poly, tol) in enumerate(windows):
        cells = shapely.contains_xy(poly, U, V)
        if not cells.any():  # a polygon thinner than the raster: the nearest raster cell
            cells = np.zeros_like(win)
            cells[np.unravel_index(int(np.argmin(np.hypot(U - poly.centroid.x, V - poly.centroid.y))), U.shape)] = True
        wname = f"{face.name}-w{i + 1}"
        clr, nearest = _clearance(poly, grid, face, n, balls)
        roof = _roof_flat(poly, build, a, b, rules.flat_report_deg) if rule_applies else 0.0
        w = {
            "name": wname,
            "face": face.name,
            "axis": face.axis,
            "range": [s0, s1],
            "plane": AXES[a] + AXES[b],
            "poly": [[round(x, 2), round(y, 2)] for x, y in list(poly.exterior.coords)[:-1]],
            "area_mm2": round(poly.area, 2),
            "removed_mm3": round(poly.area * thick, 2),
            "mean_density": round(float(d2[cells].mean()), 4),
            "roof_flat_mm": round(roof, 2) if rule_applies else None,
            "clearance": {"mm": None if clr is None else round(clr, 3), "nearest": nearest},
            "n_vertices": len(poly.exterior.coords) - 1,
            "simplify_tol": round(tol, 3),
        }
        base["windows"].append(w)
        findings += window_findings(w, rules)
    base["dropped"] = dropped
    final = sum(w["area_mm2"] for w in base["windows"])
    stages["final"] = round(final, 2)
    base["stage_areas_mm2"] = {k: round(v, 2) for k, v in stages.items()}
    base["print_rule"] = rule_applies
    cand = stages["candidate"]
    if cand > 0 and (cand - final) / cand > DOMINATE:
        findings.append(
            finding(
                "topo.trace_rules_dominate",
                f"face {face.name}: the rules removed {100 * (cand - final) / cand:.0f} % of the {cand:.0f} mm2 the map offered",
                {"candidate_mm2": cand, "final_mm2": final},
                face.name,
            )
        )
    return base, findings


def window_findings(w: dict, rules: TraceRules) -> list[Finding]:
    """`topo.clearance` (FAIL) when the window is at or under the clearance rule from a keep-out, and `topo.roof_bridge`
    (WARN) when its flat roof is longer than the apex the print rule leaves."""
    out = []
    clr, wname = w["clearance"]["mm"], w["name"]
    if clr is not None and clr <= rules.clearance_mm:
        out.append(
            finding(
                "topo.clearance",
                f"window {wname} is {clr:.2f} mm from {w['clearance']['nearest']}; the rule asks for more than {rules.clearance_mm:g} mm",
                {"clearance_mm": clr, "rule_mm": rules.clearance_mm},
                wname,
            )
        )
    roof = w["roof_flat_mm"]
    if roof is not None and roof > rules.apex_mm + rules.raster_mm:
        out.append(
            finding(
                "topo.roof_bridge",
                f"window {wname} has {roof:.1f} mm of roof flatter than {rules.flat_report_deg:g} degrees; check the bridge in the slicer",
                {"roof_flat_mm": roof},
                wname,
            )
        )
    return out


def _rib_rule(win: np.ndarray, air: np.ndarray, keep: np.ndarray, face: Face, rules: TraceRules) -> np.ndarray:
    H = rules.raster_mm
    r_px = rules.rib_mm / 2 / H
    p = disc(r_px).shape[0]
    for _ in range(rules.rib_passes):
        padded_air = np.pad(air, p)
        # beyond an open edge is air; beyond a closed edge is wall
        if "u_lo" in face.open_edges:
            padded_air[:p, :] = True
        if "u_hi" in face.open_edges:
            padded_air[-p:, :] = True
        if "v_lo" in face.open_edges:
            padded_air[:, :p] = True
        if "v_hi" in face.open_edges:
            padded_air[:, -p:] = True
        void = win | air
        padded = np.pad(void, p) | padded_air
        strip = closing(void, r_px, padded) & ~void
        if not strip.any():
            break
        labels, nlab = ndimage.label(strip)
        wlab, _ = ndimage.label(win)
        touch_air = ndimage.binary_dilation(padded_air)[p:-p, p:-p]
        new = win.copy()
        for k in range(1, nlab + 1):
            comp = labels == k
            ring = ndimage.binary_dilation(comp, structure=np.ones((3, 3), dtype=bool)) & ~comp
            wins = set(np.unique(wlab[ring])) - {0}
            on_air = bool((comp & touch_air).any())
            if on_air and wins and not (comp & keep).any():
                new |= comp  # thin material between a window and air: it becomes window
            elif len(wins) + on_air >= 2:
                new &= ~grow(comp, rules.rib_mm / 2, H)  # thin material between windows: grow it to the rib
            # a strip that touches one window only is a notch in its outline, not a rib
        if np.array_equal(new, win):
            break
        win = new
    return win


def _cell_index(A: np.ndarray, B: np.ndarray, shape: tuple[int, ...]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The grid cell under each raster point (fractional cell coordinates A, B) and whether it lies on the grid."""
    ia = np.clip(np.floor(A + 0.5).astype(int), -1, shape[0])
    ib = np.clip(np.floor(B + 0.5).astype(int), -1, shape[1])
    return ia, ib, (ia >= 0) & (ia < shape[0]) & (ib >= 0) & (ib < shape[1])


def _without_islands(poly, rib: float, up: int) -> list:
    """Pieces of `poly` with no interior ring: a strip `rib` wide through each island, running along the raster axis
    `up` across the whole polygon, joins the island to the wall on both sides. Cut edges run along the build axis."""
    todo, out = [poly], []
    for _ in range(10_000):  # each cut removes at least one ring; the bound only guards a shapely surprise
        if not todo:
            break
        p = todo.pop()
        if not p.interiors:
            out.append(p)
            continue
        c = geometry.Polygon(p.interiors[0]).representative_point()
        x0, y0, x1, y1 = p.bounds
        if up == 0:
            strip = geometry.box(x0 - 1, c.y - rib / 2, x1 + 1, c.y + rib / 2)
        else:
            strip = geometry.box(c.x - rib / 2, y0 - 1, c.x + rib / 2, y1 + 1)
        rest = p.difference(strip)
        todo += [g for g in getattr(rest, "geoms", [rest]) if isinstance(g, geometry.Polygon) and not g.is_empty]
    return out


def _polygons(win: np.ndarray, uu: np.ndarray, vv: np.ndarray, H: float, face: Face, rules: TraceRules, up: int = 1):
    labels, n = ndimage.label(win)
    found, dropped = [], []
    for k in range(1, n + 1):
        cells = labels == k
        boxes = []
        for i in np.unique(np.nonzero(cells)[0]):
            js = np.flatnonzero(cells[i])
            breaks = np.flatnonzero(np.diff(js) > 1)
            for s, e in zip(np.r_[0, breaks + 1], np.r_[breaks, len(js) - 1], strict=True):
                u0, v0 = uu[i] - H / 2, vv[js[s]] - H / 2
                boxes.append(geometry.box(u0, v0, u0 + H, vv[js[e]] + H / 2))
        shape = ops.unary_union(boxes)
        polys = list(shape.geoms) if hasattr(shape, "geoms") else [shape]
        for poly in (piece for whole in polys for piece in _without_islands(whole, rules.rib_mm, up)):
            tol = SIMPLIFY_MM
            simple = poly.simplify(tol, preserve_topology=True)
            for _ in range(SIMPLIFY_STEPS):
                if len(simple.exterior.coords) - 1 <= rules.max_vertices:
                    break
                tol *= 1.5
                simple = poly.simplify(tol, preserve_topology=True)
            simple = geometry.polygon.orient(
                geometry.Polygon([(_r(x), _r(y)) for x, y in simple.exterior.coords]),
                sign=1.0,
            )
            c = simple.centroid
            if simple.area < rules.min_area_mm2:
                dropped.append({"reason": "area", "area_mm2": round(simple.area, 2), "at": [round(c.x, 2), round(c.y, 2)]})
            elif simple.buffer(-rules.min_width_mm / 2).is_empty:
                dropped.append({"reason": "width", "area_mm2": round(simple.area, 2), "at": [round(c.x, 2), round(c.y, 2)]})
            else:
                found.append((simple, tol))
    found.sort(key=lambda t: (round(t[0].centroid.x, 3), round(t[0].centroid.y, 3)))
    return found, dropped


def _r(x: float) -> float:
    return round(round(x / ROUND_MM) * ROUND_MM, 2)


def _clearance(poly, grid: _Grid, face: Face, n: int, balls: list[dict]) -> tuple[float | None, str | None]:
    a, b = plane_axes(face.axis)
    s0, s1 = face.slab
    h = grid.pitch
    best: tuple[float | None, str | None] = (None, None)

    def take(d: float, name: str) -> None:
        nonlocal best
        if best[0] is None or d < best[0]:
            best = (d, name)

    pas = np.moveaxis(grid.passive, n, -1)
    for k in range(pas.shape[2]):
        layer = pas[:, :, k]
        if not (layer > 0).any():
            continue
        lo, hi = grid.origin[n] + h * k, grid.origin[n] + h * (k + 1)
        gap = max(0.0, s0 - hi, lo - s1)
        for lab in np.unique(layer[layer > 0]):
            idx = np.argwhere(layer == lab)
            squares = [
                geometry.box(grid.origin[a] + h * i, grid.origin[b] + h * j, grid.origin[a] + h * (i + 1), grid.origin[b] + h * (j + 1))
                for i, j in idx
            ]
            d2 = poly.distance(ops.unary_union(squares))
            name = grid.names[lab - 1] if 0 < lab <= len(grid.names) else f"passive {lab}"
            take(math.hypot(d2, gap), name)
    for ball in balls:
        c = ball["c"]
        gap = max(0.0, s0 - c[n], c[n] - s1)
        d2 = poly.distance(geometry.Point(c[a], c[b]))
        take(max(0.0, math.hypot(d2, gap) - ball["r"]), str(ball.get("name", "keep-out")))
    return best


def _roof_flat(poly, build: int, a: int, b: int, flat_deg: float) -> float:
    """Length of the edges whose outward normal points up and that are flatter than `flat_deg` from horizontal."""
    up = 0 if build == a else 1
    pts = list(poly.exterior.coords)
    total = 0.0
    for (x0, y0), (x1, y1) in itertools.pairwise(pts):
        d = (x1 - x0, y1 - y0)
        normal = (d[1], -d[0])  # outward for a counter-clockwise ring
        if normal[up] <= 0:
            continue
        rise, run = abs(d[up]), abs(d[1 - up])
        if math.degrees(math.atan2(rise, run)) < flat_deg:
            total += math.hypot(*d)
    return total
