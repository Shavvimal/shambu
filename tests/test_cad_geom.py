"""The geom extra: Boolean overlap, exact and pruned distances, clearance and sweeps. Skipped without numpy, scipy and
manifold3d (the base suite checks the verdicts on plain numbers in test_cad_spec.py); the CI `geom` job runs it."""

from __future__ import annotations

import math
import random

import pytest

pytest.importorskip("manifold3d")
np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from cadfix import codes, fails, run, spec, stl  # noqa: E402
from meshes import box, cylinder, plate_with_holes, rotate_z, tube, without  # noqa: E402
from shambu import meshgeom as G  # noqa: E402
from shambu.cad import spec as S  # noqa: E402
from shambu.cad.geom import clearance as C  # noqa: E402
from shambu.cad.geom.engines import ExactDistance, GMesh, ManifoldBoolean, PrunedDistance, SampledContainment  # noqa: E402
from shambu.cad.geom.sweep import SweepRunner  # noqa: E402
from shambu.errors import GeomError  # noqa: E402


def _scene(tmp_path, meshes: dict, rules: list, roles: dict | None = None, **top):
    parts = []
    for name, facets in meshes.items():
        stl(tmp_path / f"{name}.stl", facets)
        parts.append({"name": name, "mesh": f"{name}.stl", "role": (roles or {}).get(name, "rigid")})
    return spec(tmp_path / "cad.json", {"parts": parts, "rules": rules, **top})


def _clash(**kw):
    return {"id": "clash", "type": "clearance", "a": "part:a", "b": "part:b", **kw}


# ---------------------------------------------------------------------------- overlap
def test_cubes_overlapping_one_mm(tmp_path, capsys):
    sp = _scene(tmp_path, {"a": box((0, 0, 0), (10, 10, 10)), "b": box((9, 0, 0), (19, 10, 10))}, [_clash()])
    code, doc, _, _ = run(capsys, "cad", "clearance", "--spec", str(sp))
    (pair,) = doc["rules"][1]["pairs"]
    assert code == 1 and fails(doc) == {"clash.overlap"} and abs(pair["overlap"]["volume_mm3"] - 100) < 1e-6


def test_face_touching_cubes_are_within_the_floor(tmp_path, capsys):
    sp = _scene(tmp_path, {"a": box((0, 0, 0), (10, 10, 10)), "b": box((10, 0, 0), (20, 10, 10))}, [_clash()])
    code, doc, _, _ = run(capsys, "cad", "clearance", "--spec", str(sp))
    assert code == 0 and doc["rules"][1]["pairs"][0]["overlap"]["volume_mm3"] <= 1e-3


@pytest.mark.parametrize(("cap", "code"), [(150, "clash.exempt"), (50, "clash.over_cap")])
def test_exemption_cap(tmp_path, capsys, cap, code):
    ex = [{"a": "part:a", "b": "part:b", "reason": "press fit", "max_overlap_mm3": cap}]
    sp = _scene(tmp_path, {"a": box((0, 0, 0), (10, 10, 10)), "b": box((9, 0, 0), (19, 10, 10))}, [_clash(exempt=ex)])
    _, doc, _, _ = run(capsys, "cad", "gate", str(sp))
    assert code in codes(doc) and "clash.overlap" not in codes(doc)


@pytest.mark.parametrize(("gap", "fail"), [(0.25, True), (0.35, False)])
def test_margin_edge(tmp_path, capsys, gap, fail):
    sp = _scene(tmp_path, {"a": box((0, 0, 0), (10, 10, 10)), "b": box((10 + gap, 0, 0), (20, 10, 10))}, [_clash(margin_mm=0.3)])
    code, doc, _, _ = run(capsys, "cad", "gate", str(sp))
    assert (code == 1) is fail and ("clash.margin" in fails(doc)) is fail
    assert abs(doc["rules"][1]["pairs"][0]["distance_mm"] - gap) < 1e-5  # the STL stores float32


def test_boolean_failure_is_fail(tmp_path, capsys, monkeypatch):
    """A Boolean that raises is one clash.boolean_failed FAIL for that pair; the run goes on; no exemption covers it."""
    real = ManifoldBoolean.overlap

    def flaky(self, a, b):
        if "c" in (a.name, b.name):
            raise GeomError(f"{a.name} x {b.name}: the Boolean failed (test)")
        return real(self, a, b)

    monkeypatch.setattr(ManifoldBoolean, "overlap", flaky)
    meshes = {"a": box((0, 0, 0), (10, 10, 10)), "b": box((9, 0, 0), (19, 10, 10)), "c": box((0, 0, 9), (10, 10, 19))}
    ex = [{"a": "all", "b": "all", "reason": "anything goes", "max_overlap_mm3": 1000}]
    sp = _scene(tmp_path, meshes, [{"id": "clash", "type": "clearance", "a": "all", "b": "all", "exempt": ex}])
    code, doc, _, _ = run(capsys, "cad", "gate", str(sp))
    failed = [f for f in doc["findings"] if f["code"] == "clash.boolean_failed"]
    assert code == 1 and len(failed) == 2 and "clash.exempt" in codes(doc)  # a x b still ran, and is exempt


def test_containment_has_no_volume(tmp_path, capsys):
    a = GMesh.from_facets("a", without(box((0, 0, 0), (10, 10, 10)), 0))
    b = GMesh.from_facets("b", box((9, 1, 1), (19, 9, 9)))
    ov = SampledContainment(0.5).overlap(a, b)
    assert not a.watertight and ov.volume_mm3 is None and ov.inside_points
    with pytest.raises(GeomError):
        ManifoldBoolean().overlap(a, b)
    ex = [{"a": "part:a", "b": "part:b", "reason": "press fit", "max_overlap_mm3": 5}]
    sp = _scene(tmp_path, {"a": without(box((0, 0, 0), (10, 10, 10)), 0), "b": box((9, 1, 1), (19, 9, 9))}, [_clash(exempt=ex)])
    code, doc, _, _ = run(capsys, "cad", "gate", str(sp))
    assert code == 1 and doc["gate"] == "incomplete" and fails(doc) == {"cad.incomplete.no_volume"}


def test_a_missing_partner_is_incomplete_not_inf(tmp_path, capsys):
    sp = _scene(tmp_path, {"a": box((0, 0, 0), (10, 10, 10)), "b": box((20, 0, 0), (30, 10, 10))}, [_clash()])
    (tmp_path / "b.stl").unlink()
    code, doc, out, _ = run(capsys, "cad", "gate", str(sp))
    assert code == 1 and doc["gate"] == "incomplete" and "inf" not in out.lower().replace("info", "")


# ---------------------------------------------------------------------------- fast path equals slow path
def _random_pose(rng: random.Random):
    ax = np.array([rng.gauss(0, 1) for _ in range(3)])
    ax /= np.linalg.norm(ax)
    ang = rng.uniform(0, 2 * math.pi)
    k = np.array([[0, -ax[2], ax[1]], [ax[2], 0, -ax[0]], [-ax[1], ax[0], 0]])
    rot = np.eye(3) + math.sin(ang) * k + (1 - math.cos(ang)) * k @ k
    return rot, np.array([rng.uniform(-12, 12) for _ in range(3)])


def test_pruned_distance_equals_exact_distance():
    """50 seeded random rigid placements of a box against a tube: the same minimum to 1e-9 and the same closest pair,
    and a third, independent oracle (meshgeom.point_triangle, pure Python) agrees."""
    rng = random.Random(20261102)  # noqa: S311 (a seeded test sequence)
    tube_mesh = GMesh.from_facets("tube", tube(3, 5, 10, 24))
    pts = PrunedDistance.build(tube_mesh, 0.7).samples
    base = GMesh.from_facets("box", box((-2, -3, -1), (2, 3, 1)))
    for _ in range(50):
        rot, shift = _random_pose(rng)
        target = base.moved(rot, shift)
        fast = PrunedDistance.build(target, 0.4).nearest(pts)
        slow = ExactDistance(target).nearest(pts)
        assert fast is not None and slow is not None
        assert abs(fast.distance_mm - slow.distance_mm) <= 1e-9
        assert (fast.index, fast.tri) == (slow.index, slow.tri)
        p = tuple(float(x) for x in pts[fast.index])
        oracle = min(G.point_triangle(p, tuple(tuple(float(x) for x in v) for v in tri))[0] for tri in target.tris)
        assert abs(oracle - fast.distance_mm) <= 1e-9


def test_pruned_distances_match_per_point_with_a_bound():
    rng = random.Random(7)  # noqa: S311 (a seeded test sequence)
    target = GMesh.from_facets("box", box((0, 0, 0), (4, 4, 4)))
    pts = np.array([[rng.uniform(-6, 10) for _ in range(3)] for _ in range(300)])
    a = PrunedDistance.build(target, 0.5).distances(pts, 2.0)
    b = ExactDistance(target).distances(pts, 2.0)
    assert np.array_equal(np.isfinite(a.d), np.isfinite(b.d))
    assert np.allclose(a.d[np.isfinite(a.d)], b.d[np.isfinite(b.d)], atol=1e-12, rtol=0)


# ---------------------------------------------------------------------------- sweeps
BAR = box((0, -1, 0), (50, 1, 2))


def _post(radius: float, deg: float):
    """A 2 x 2 mm post, z 0.5 to 1.5: inside the bar's height, so a post corner meets the bar's face (a vertex
    against a triangle) before any edge pair comes within the margin."""
    cx, cy = radius * math.cos(math.radians(deg)), radius * math.sin(math.radians(deg))
    return box((cx - 1, cy - 1, 0.5), (cx + 1, cy + 1, 1.5))


def _rotation_rule(**kw):
    rule = {
        "id": "swing",
        "type": "sweep",
        "moving": "part:bar",
        "fixed": "role:rigid",
        "motion": "rotate",
        "point": [0, 0, 0],
        "axis": [0, 0, 1],
    }
    return {**rule, "from_deg": 0, "to_deg": 90, "step_deg": 1, "resolution_deg": 0.05, "margin_mm": 0.1, **kw}


def _analytic_contact(radius: float, deg: float, margin: float) -> float:
    """The bar's leading face (y = 1 in its frame) comes within `margin` of the post's nearest corner."""
    cx, cy = radius * math.cos(math.radians(deg)), radius * math.sin(math.radians(deg))
    best = math.inf
    for dx in (-1, 1):
        for dy in (-1, 1):
            rho, phi = math.hypot(cx + dx, cy + dy), math.degrees(math.atan2(cy + dy, cx + dx))
            best = min(best, phi - math.degrees(math.asin((1 + margin) / rho)))
    return best


def test_rotation_meets_a_post_at_the_analytic_angle(tmp_path, capsys):
    sp = _scene(tmp_path, {"bar": BAR, "post": _post(40, 30)}, [_rotation_rule(first_contact_after_deg=10)], {"bar": "moving"})
    code, doc, _, _ = run(capsys, "cad", "sweep", "--spec", str(sp))
    got = doc["rules"][1]["first_contact"]
    assert code == 0 and got["partner"] == "post"
    assert abs(got["at_deg"] - _analytic_contact(40, 30, 0.1)) <= 0.05
    assert "sweep.first_contact" in codes(doc)


def test_early_contact_fails(tmp_path, capsys):
    sp = _scene(tmp_path, {"bar": BAR, "post": _post(40, 30)}, [_rotation_rule(first_contact_after_deg=60)], {"bar": "moving"})
    code, doc, _, _ = run(capsys, "cad", "gate", str(sp))
    assert code == 1 and fails(doc) == {"sweep.early_contact"}


def _runner(cull: bool, meshes: dict, rule: dict) -> SweepRunner:
    sp = S.parse(
        {
            "schema": S.SCHEMA,
            "parts": [{"name": n, "mesh": n, "role": "moving" if n == "bar" else "rigid"} for n in meshes],
            "rules": [rule],
        }
    )
    placed = {n: C.Placed(sp.part(n), GMesh.from_facets(n, f)) for n, f in meshes.items()}
    return SweepRunner(sp.rules[0], [placed["bar"]], [p for n, p in placed.items() if n != "bar"], sp.zones, cull)


def test_culled_sweep_equals_unculled_sweep():
    meshes = {"bar": BAR, "post": _post(40, 30), "far": _post(30, 75)}
    rule = _rotation_rule(margin_mm=2.0)  # a wide margin: the margin is met steps before the parts overlap
    fast = _runner(True, meshes, rule).run()
    slow = _runner(False, meshes, rule).run()
    assert fast.first is not None and slow.first is not None
    assert (fast.first.tau, fast.first.partner, fast.first.kind, fast.steps) == (
        slow.first.tau,
        slow.first.partner,
        slow.first.kind,
        slow.steps,
    )
    assert fast.first.kind == "distance" and fast.evaluated_points < slow.evaluated_points  # the culling did skip points


def test_a_plate_thinner_than_one_step_is_caught():
    plate = rotate_z(box((30, -0.1, 0), (45, 0.1, 2)), 35)
    rule = _rotation_rule(step_deg=10, margin_mm=0.0)
    run_ = _runner(True, {"bar": BAR, "plate": plate}, rule).run()
    assert run_.first is not None and run_.first.partner == "plate" and 30 < run_.first.tau < 36


def _peg_scene(tmp_path, r_peg: float):
    plate = plate_with_holes((20, 20, 10), [{"x": 10, "y": 10, "stages": [(3.0, 0)]}])
    peg = cylinder(r_peg, 15, 64, (10, 10, 1))
    rule = {"id": "insert", "type": "sweep", "moving": "part:peg", "fixed": "part:plate", "motion": "translate", "axis": [0, 0, -1]}
    rule.update({"from_mm": 20, "step_mm": 1, "resolution_mm": 0.02, "margin_mm": 0.05})
    return _scene(tmp_path, {"peg": peg, "plate": plate}, [rule], {"peg": "moving"})


def test_peg_seats_in_its_bore(tmp_path, capsys):
    code, doc, _, _ = run(capsys, "cad", "sweep", "--spec", str(_peg_scene(tmp_path, 2.9)))
    assert code == 0 and "sweep.no_contact" in codes(doc)


def test_oversize_peg_fails_at_the_entry(tmp_path, capsys):
    code, doc, _, _ = run(capsys, "cad", "sweep", "--spec", str(_peg_scene(tmp_path, 3.1)))
    got = doc["rules"][1]["first_contact"]
    assert code == 1 and fails(doc) & {"sweep.margin", "sweep.overlap"}
    assert abs(got["travel_mm"] - (20 - 9 - 0.05)) <= 0.03  # the peg's foot reaches the plate top (z 10) plus the margin


def test_step_chord_bounds_every_point():
    """The cull bound: a point moves at most its chord in one step, and the bound is tight (a rotation moves each
    point by exactly 2 rho sin(step / 2))."""
    from shambu.cad.geom.sweep import Motion

    rng = random.Random(11)  # noqa: S311 (a seeded test sequence)
    axis = np.array([1.0, 2.0, 2.0]) / 3.0
    m = Motion("rotate", axis, np.array([1.0, -2.0, 0.5]), 0.0, 90.0)
    pts = np.array([[rng.uniform(-30, 30) for _ in range(3)] for _ in range(200)])
    for tau, step in ((0.0, 1.0), (10.0, 7.5), (40.0, 0.05)):
        r0, t0 = m.pose(tau)
        r1, t1 = m.pose(tau + step)
        moved = np.linalg.norm((pts @ r1.T + t1) - (pts @ r0.T + t0), axis=1)
        bound = m.chord(m.radius(pts @ r0.T + t0), step)
        assert np.all(moved <= bound + 1e-9) and np.allclose(moved, bound, atol=1e-9)
