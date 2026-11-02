"""The topo extra: the hex8 element, assembly, the filter, loads, the gradient, the update, the voxeliser, the Michell
cantilever and the run findings. Skipped without numpy and scipy (the base suite checks the CLI and the spec in
test_topo_cli.py); the CI `topo` job installs the extra and runs it. Every grid is tiny: the file runs in seconds."""

from __future__ import annotations

import json
import math

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from cadfix import run, stl  # noqa: E402
from meshes import box  # noqa: E402
from shambu.errors import TopoSpecError  # noqa: E402
from shambu.topo import checks, loads, michell  # noqa: E402
from shambu.topo.fe import VoxelFE, elasticity, hex8_ke  # noqa: E402
from shambu.topo.filter import DensityFilter  # noqa: E402
from shambu.topo.linsolve import DenseSolver, SuperLUSolver  # noqa: E402
from shambu.topo.optimise import Problem, optimise  # noqa: E402
from shambu.topo.service import Topo  # noqa: E402
from shambu.topo.spec import parse_spec  # noqa: E402
from shambu.topo.voxel import box_domain, voxelise  # noqa: E402

TIMING_KEYS = ("seconds", "timings", "peak_rss_mb", "elapsed_s", "versions")


def _problem(doc: dict, solver=None):
    spec, errors = parse_spec(doc)
    assert spec is not None, errors
    dom = box_domain(*spec.domain.box, spec.domain.pitch)
    centres = dom.centres()
    fe = VoxelFE(dom.mask, dom.pitch, spec.material.nu)
    nodes = fe.node_coords(dom.origin)
    from shambu.topo import select

    passive = np.zeros(fe.n_elements, dtype=bool)
    for r in spec.passive:
        passive |= select.mask(r.cells, centres)
    model = loads.build(spec, fe, centres, nodes)
    fe.set_fixed(model.fixed)
    filt = DensityFilter.build(centres, spec.simp.rmin_mm(dom.pitch))
    prob = Problem(fe, filt, model.cases, passive, spec.material.E, spec.simp.emin_rel, spec.simp.penal, solver or SuperLUSolver())
    full = prob.analyse(np.ones(fe.n_elements))
    prob.weights = (1.0 / len(model.cases)) / full.compliance
    return spec, dom, fe, nodes, prob, full


def _cantilever(nx, ny, nz, volfrac=0.4, rmin_cells=1.5, maxit=40, **extra) -> dict:
    doc = michell.spec_doc("small")
    doc["domain"]["box"]["max"] = [nx, ny, nz]
    doc["simp"].update({"volfrac": [volfrac], "rmin_cells": rmin_cells, "maxit": maxit})
    doc["cases"][0]["loads"][0]["nodes"] = {"box": {"x": [nx - 0.01, nx + 0.01], "z": [nz // 2 - 0.01, nz // 2 + 0.01]}}
    doc["regions"] = []
    doc.update(extra)
    return doc


# ---------------------------------------------------------------------------- element and assembly
def test_ke_symmetric_with_six_rigid_modes():
    ke = hex8_ke(0.3)
    assert np.allclose(ke, ke.T)
    ev = np.linalg.eigvalsh(ke)
    assert np.sum(np.abs(ev) < 1e-10) == 6 and ev[6] > 1e-3
    assert np.allclose(ke.reshape(24, 8, 3).sum(axis=1), 0)  # a rigid translation makes no force


@pytest.mark.parametrize("nu", [0.3, 0.38])
def test_ke_pins_the_material_law_by_constant_strain_energies(nu):
    """A linear displacement field is exact on a hex8, so u^T ke u of a unit cube (E = 1, volume 1) equals the
    continuum energy 2 W = strain^T D strain. Each engineering shear gives G = 1 / (2 (1 + nu)), each axial strain
    lambda + 2 mu, and a pure dilatation 9 K. A wrong shear (or Lame) term in the material law changes these."""
    from shambu.topo.fe import CORNERS

    ke = hex8_ke(nu)
    assert np.allclose(ke, ke.T, atol=1e-14)
    ev = np.linalg.eigvalsh(ke)
    assert np.sum(np.abs(ev) < 1e-10) == 6 and ev[6] > 1e-3  # exactly the six rigid-body modes are free
    lam, mu = nu / ((1 + nu) * (1 - 2 * nu)), 1 / (2 * (1 + nu))
    xyz = CORNERS.astype(float)

    def energy(grad: np.ndarray) -> float:
        u = (xyz @ grad.T).ravel()  # u_i = grad[i, j] x_j at each corner
        return float(u @ ke @ u)

    eps = 1e-3
    for i, j in ((0, 1), (1, 2), (2, 0)):  # simple shear: gamma_ij = eps
        g = np.zeros((3, 3))
        g[i, j] = eps
        assert math.isclose(energy(g), mu * eps**2, rel_tol=1e-10), (i, j)
    for i in range(3):  # uniaxial strain
        g = np.zeros((3, 3))
        g[i, i] = eps
        assert math.isclose(energy(g), (lam + 2 * mu) * eps**2, rel_tol=1e-10)
    assert math.isclose(energy(eps * np.eye(3)), (9 * lam + 6 * mu) * eps**2, rel_tol=1e-10)  # 9 K eps^2, K = lam + 2 mu / 3
    assert np.allclose(elasticity(nu)[3:, 3:], mu * np.eye(3))


def test_patch_uniaxial_tension_is_exact():
    """A 2 x 2 x 2 block on rollers, pulled on x = L: u = F L / (E A), lateral strain -nu times axial."""
    e_mod, nu, force = 1000.0, 0.3, 50.0
    doc = {
        "schema": "shambu.topo/1",
        "units": "mm-N-MPa",
        "domain": {"box": {"min": [0, 0, 0], "max": [2, 2, 2]}, "pitch": 1.0},
        "material": {"E": e_mod, "nu": nu},
        "supports": [
            {"name": "x0", "nodes": {"box": {"x": [-0.01, 0.01]}}, "dofs": "x"},
            {"name": "y0", "nodes": {"box": {"y": [-0.01, 0.01]}}, "dofs": "y"},
            {"name": "z0", "nodes": {"box": {"z": [-0.01, 0.01]}}, "dofs": "z"},
        ],
        "cases": [{"name": "pull", "loads": [{"nodes": {"box": {"x": [1.99, 2.01]}}, "force": [force, 0, 0]}]}],
    }
    spec, errors = parse_spec(doc)
    assert spec is not None, errors
    dom = box_domain((0, 0, 0), (2, 2, 2), 1.0)
    fe = VoxelFE(dom.mask, 1.0, nu)
    nodes = fe.node_coords(dom.origin)
    model = loads.build(spec, fe, dom.centres(), nodes)
    # the rigid spread of a pure force over a symmetric face is uniform, but a uniform nodal split is not a uniform
    # traction on bilinear faces; load the consistent nodal forces instead (corner 1/16, edge 1/8, centre 1/4)
    on = np.flatnonzero(np.isclose(nodes[:, 0], 2.0))
    f = np.zeros(fe.n_dof)
    for i in on:
        edge = sum(np.isclose(nodes[i, k], 0.0) or np.isclose(nodes[i, k], 2.0) for k in (1, 2))
        f[3 * i] = force * {0: 0.25, 1: 0.125, 2: 0.0625}[edge]
    model.cases[0].f0[:] = f
    fe.set_fixed(model.fixed)
    u = np.zeros(fe.n_dof)
    k = fe.assemble(np.full(fe.n_elements, e_mod))
    u[fe.free] = SuperLUSolver().solve(k, f[fe.free])
    axial = force / (e_mod * 4.0)  # strain = F / (E A), A = 4 mm2
    assert np.allclose(u[3 * on], axial * 2.0, rtol=1e-9)
    far_y = np.flatnonzero(np.isclose(nodes[:, 1], 2.0))
    assert np.allclose(u[3 * far_y + 1], -nu * axial * 2.0, rtol=1e-9)


def test_equilibrium_at_full_density():
    _, _, fe, nodes, prob, full = _problem(_cantilever(6, 2, 3))
    rows, found = checks.equilibrium(fe, prob, full, nodes, ["tip"])
    assert found == [] and rows[0]["ok"]
    assert np.allclose(np.asarray(rows[0]["reaction"]), -np.asarray(rows[0]["load"]), atol=1e-9)


def test_dense_and_sparse_solvers_agree():
    *_, full_s = _problem(_cantilever(6, 2, 3))
    *_, full_d = _problem(_cantilever(6, 2, 3), DenseSolver())
    assert np.allclose(full_s.compliance, full_d.compliance, rtol=1e-10)


def test_rigid_distribute_is_the_least_norm_rigid_field():
    rng = np.random.default_rng(7)
    xyz = rng.uniform(-5, 5, (10, 3))
    force, moment = np.array([1.0, -2.0, 3.0]), np.array([4.0, 0.5, -1.0])
    f = loads.rigid_distribute(xyz, force, moment)
    r = xyz - xyz.mean(axis=0)
    assert np.allclose(f.sum(axis=0), force) and np.allclose(np.cross(r, f).sum(axis=0), moment)
    # the least-norm solution of the 6 x 30 equilibrium system
    a = np.zeros((6, 30))
    for i in range(10):
        a[:3, 3 * i : 3 * i + 3] = np.eye(3)
        x, y, z = r[i]
        a[3:, 3 * i : 3 * i + 3] = [[0, -z, y], [z, 0, -x], [-y, x, 0]]
    ref = np.linalg.lstsq(a, np.r_[force, moment], rcond=None)[0]
    assert np.allclose(f.ravel(), ref)


def test_a_moment_on_one_node_is_a_spec_error():
    from shambu.errors import TopoSpecError

    with pytest.raises(TopoSpecError, match="cannot be carried"):
        loads.rigid_distribute(np.zeros((1, 3)), np.zeros(3), np.array([0, 0, 1.0]))


# ---------------------------------------------------------------------------- filter, gradient, update
def test_filter_rows_sum_to_one_and_keep_a_constant():
    dom = box_domain((0, 0, 0), (5, 5, 1), 1.0)
    f = DensityFilter.build(dom.centres(), 1.5)
    w = f.h.toarray() / f.hs[:, None]
    assert np.allclose(w.sum(axis=1), 1.0)
    # the cone weights of the centre cell: itself at distance 0, four at 1, four at sqrt(2)
    assert math.isclose(w[12, 12], 1.5 / (1.5 + 4 * 0.5 + 4 * (1.5 - math.sqrt(2))), rel_tol=1e-12)
    assert np.allclose(f.forward(np.full(25, 0.37)), 0.37)
    x = np.random.default_rng(1).uniform(size=25)
    g = np.random.default_rng(2).uniform(size=25)
    assert np.isclose(f.forward(x) @ g, x @ f.backward(g))  # backward is the transpose of forward


def test_filter_radius_under_the_pitch_is_the_identity():
    dom = box_domain((0, 0, 0), (5, 5, 1), 1.0)
    f = DensityFilter.build(dom.centres(), 0.9)
    x = np.random.default_rng(3).uniform(size=25)
    assert np.allclose(f.forward(x), x)


@pytest.mark.parametrize("self_weight", [False, True])
def test_gradient_matches_central_differences(self_weight):
    doc = _cantilever(3, 2, 2, rmin_cells=1.5)
    doc["passive"] = [{"name": "clamp block", "cells": {"box": {"x": [0, 1]}}}]
    if self_weight:
        doc["cases"].append({"name": "weight", "body_force": {"accel_g": [0, 0, -2000.0]}})
    *_, prob, _ = _problem(doc)
    rng = np.random.default_rng(11)
    x = np.where(prob.design, rng.uniform(0.3, 0.9, prob.fe.n_elements), 1.0)
    g = prob.gradient(x)
    for j in rng.choice(np.flatnonzero(prob.design), 5, replace=False):
        step = 1e-6
        xp, xm = x.copy(), x.copy()
        xp[j] += step
        xm[j] -= step
        fd = (prob.objective(xp) - prob.objective(xm)) / (2 * step)
        assert math.isclose(g[j], fd, rel_tol=1e-5, abs_tol=1e-12), (j, g[j], fd)


def test_update_holds_the_volume_and_the_passive_cells():
    doc = _cantilever(8, 1, 4, volfrac=0.5, maxit=1)
    doc["passive"] = [{"name": "tip pad", "cells": {"box": {"x": [7, 8]}}}]
    *_, prob, _ = _problem(doc)
    seen = []
    for it in range(1, 6):
        run_ = optimise(prob, 0.5, it, 0.0, 0.2, 0.5, listener=lambda ev, d: seen.append(d["it"]))
        assert abs(run_.design_volume_fraction - 0.5) < 1e-3
        assert np.all(run_.rho[prob.passive] == 1.0)
    assert seen and seen[-1] == 5


# ---------------------------------------------------------------------------- the Michell cantilever
def test_michell_small_shape():
    rep = Topo().validate("small")
    doc = rep.to_json()
    checks_ = doc["metrics"]["checks"]
    assert doc["metrics"]["pass"], checks_
    assert checks_["top_chord"]["value"] > 0.6 and checks_["bottom_chord"]["value"] > 0.6 and checks_["clamp_middle"]["value"] < 0.1
    assert rep.exit_code == 0
    codes = {f["code"] for f in doc["findings"]}
    assert {"topo.map_not_proof", "topo.stiffness_not_strength", "topo.clamped_support", "topo.rigid_load_patch"} <= codes


def test_validate_cli_one_document(capsys):
    code, doc, out, _ = run(capsys, "topo", "validate")
    assert code == 0 and json.loads(out) == doc and doc["schema"] == "shambu.topo.validate/1"


def test_determinism(tmp_path):
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps(_cantilever(12, 1, 4, maxit=10)))
    docs = []
    for name in ("a", "b"):
        Topo().solve(str(spec), str(tmp_path / name), mesh="cubes")
        d = json.loads((tmp_path / name / "run.json").read_text())
        for k in TIMING_KEYS:
            d.pop(k, None)
            for r in d["runs"]:
                r.pop(k, None)
        docs.append(json.dumps(d, sort_keys=True))
    assert docs[0] == docs[1]
    assert (tmp_path / "a" / "density_v40.npy").read_bytes() == (tmp_path / "b" / "density_v40.npy").read_bytes()


# ---------------------------------------------------------------------------- run findings
def test_not_converged_at_five_iterations():
    rep = Topo()._solve(parse_spec(_cantilever(12, 1, 4, maxit=5))[0], False)[0]
    codes = {f.code for f in rep.findings}
    assert "topo.not_converged" in codes and rep.exit_code == 0


def test_unloaded_region_and_inert_case():
    doc = _cantilever(20, 1, 4, maxit=8)
    doc["cases"][0]["loads"][0]["nodes"] = {"box": {"x": [7.99, 8.01], "z": [1.99, 2.01]}}  # beyond x 8 nothing works
    doc["regions"] = [{"name": "idle end", "cells": {"box": {"x": [14, 20]}}}]
    doc["passive"] = [{"name": "pad", "cells": {"box": {"x": [0, 2], "z": [0, 2]}}}]
    doc["cases"].append({"name": "pad only", "loads": [{"nodes": {"box": {"x": [-0.01, 2.01], "z": [-0.01, 0.01]}}, "force": [0, 1.0, 0]}]})
    rep = Topo()._solve(parse_spec(doc)[0], False)[0]
    by = {(f.code, f.object) for f in rep.findings}
    assert ("topo.unloaded_region", "idle end") in by
    assert ("topo.inert_case", "pad only") in by


def test_equilibrium_guard_fails_the_run(monkeypatch, tmp_path, capsys):
    """A broken balance is a FAIL (exit 1) and no optimisation runs on that model."""
    monkeypatch.setattr(checks, "EQ_REL", -1.0)
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps(_cantilever(6, 1, 3, maxit=3)))
    code, doc, _, _ = run(capsys, "topo", "solve", str(spec), "--out", str(tmp_path / "o"))
    assert code == 1 and doc["runs"] == [] and "topo.equilibrium" in {f["code"] for f in doc["findings"]}


def test_passive_guard_fails_the_run(monkeypatch):
    doc = _cantilever(8, 1, 4, maxit=3)
    doc["passive"] = [{"name": "pad", "cells": {"box": {"x": [7, 8]}}}]
    real = Problem.physical

    def leaky(self, x):
        rho = real(self, x)
        rho[self.passive] = 0.5
        return rho

    monkeypatch.setattr(Problem, "physical", leaky)
    rep = Topo()._solve(parse_spec(doc)[0], False)[0]
    assert "topo.passive_not_one" in {f.code for f in rep.findings} and rep.exit_code == 1


def test_raw_weights_flag_a_dominant_case():
    doc = _cantilever(12, 1, 4, maxit=4)
    doc["weights"] = "raw"
    doc["cases"].append({"name": "small", "loads": [{"nodes": {"box": {"x": [11.99, 12.01], "z": [1.99, 2.01]}}, "force": [0.01, 0, 0]}]})
    rep = Topo()._solve(parse_spec(doc)[0], False)[0]
    assert ("topo.case_dominates", "tip") in {(f.code, f.object) for f in rep.findings}


def test_filter_small_and_load_fallback():
    doc = _cantilever(12, 1, 4, maxit=2, rmin_cells=1.0)
    doc["cases"][0]["loads"][0] = {"cells": {"ball": {"c": [14, 0.5, 2], "r": 0.5}}, "grow_if_empty": 3.0, "force": [0, 0, -1]}
    rep = Topo()._solve(parse_spec(doc)[0], False)[0]
    codes = {f.code for f in rep.findings}
    assert {"topo.filter_small", "topo.load_fallback"} <= codes


# ---------------------------------------------------------------------------- the voxeliser and solve on an STL
def test_voxelise_unit_cube_exactly():
    dom = voxelise(box((0, 0, 0), (1, 1, 1)), 0.25)
    assert dom.cells == 64 and math.isclose(dom.voxel_volume_mm3, 1.0) and dom.odd_columns == 0


def test_voxelise_rotated_box_volume():
    from shambu.mesh import transform

    c, s = math.cos(math.radians(30)), math.sin(math.radians(30))
    facets = transform(box((0, 0, 0), (20, 10, 6)), ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0)))
    dom = voxelise(facets, 1.0)
    assert abs(dom.voxel_volume_mm3 / 1200 - 1) < 0.03 and dom.odd_columns == 0


def test_voxelise_open_mesh_warns(tmp_path, capsys):
    facets = box((0, 0, 0), (10, 10, 4))[1:]  # one bottom triangle missing
    stl(tmp_path / "open.stl", facets)
    spec = tmp_path / "s.json"
    doc = _cantilever(10, 10, 4)
    doc["domain"] = {"stl": "open.stl", "pitch": 1.0}
    spec.write_text(json.dumps(doc))
    code, out, _, _ = run(capsys, "topo", "voxelise", str(spec))
    assert code == 0 and "topo.odd_parity" in {f["code"] for f in out["findings"]} and out["domain"]["odd_columns"] > 0


def test_voxelise_reports_an_empty_selection(tmp_path, capsys):
    stl(tmp_path / "b.stl", box((0, 0, 0), (10, 4, 4)))
    doc = _cantilever(10, 4, 4)
    doc["domain"] = {"stl": "b.stl", "pitch": 1.0}
    doc["cases"][0]["loads"][0]["nodes"] = {"box": {"x": [50, 60]}}
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps(doc))
    code, out, _, _ = run(capsys, "topo", "voxelise", str(spec))
    assert code == 1 and out["errors"] and out["selections"]["loads"][0]["nodes"] == 0


def test_solve_an_stl_writes_the_run_files(tmp_path, capsys):
    stl(tmp_path / "bar.stl", box((0, 0, 0), (16, 2, 6)))
    doc = _cantilever(16, 2, 6, maxit=6)
    doc["domain"] = {"stl": "bar.stl", "pitch": 1.0}
    doc["regions"] = [{"name": "root", "cells": {"box": {"x": [0, 2]}}}]
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps(doc))
    out_dir = tmp_path / "run"
    code, out, raw, _ = run(capsys, "topo", "solve", str(spec), "--out", str(out_dir), "--mesh", "cubes", "--volfrac", "0.5")
    assert code == 0 and json.loads(raw) == out
    assert json.loads((out_dir / "run.json").read_text()) == out
    r = out["runs"][0]
    dens = np.load(out_dir / r["files"]["density"])
    assert dens.shape == tuple(out["grid"]["shape"]) and dens.dtype == np.float32
    assert r["files"]["iso_kind"] == "cubes" and (out_dir / r["files"]["iso"]).stat().st_size > 84
    assert (out_dir / "regions.md").read_text().startswith("# Topology run regions")
    assert out["domain"]["volume_ratio"] == 1.0 and out["full_density"][0]["ok"]
    assert out["not_checked"] and {"topo.map_not_proof", "topo.stiffness_not_strength"} <= {f["code"] for f in out["findings"]}


def test_cube_iso_surface_is_closed():
    from shambu import meshgeom
    from shambu.topo import report as R

    dom = box_domain((0, 0, 0), (4, 3, 2), 1.0)
    rho = np.ones(dom.cells)
    rho[5] = 0.0
    facets = R.cube_facets(dom, rho)
    w = meshgeom.weld(facets)
    assert w.open_edges() == [] and math.isclose(sum(meshgeom_volume(f) for f in facets), 23.0)


def meshgeom_volume(f) -> float:
    a, b, c = (np.asarray(v) for v in f)
    return float(np.dot(a, np.cross(b, c)) / 6)


def test_smooth_iso_surface_when_scikit_image_is_present():
    pytest.importorskip("skimage")
    from shambu.topo import report as R

    dom = box_domain((0, 0, 0), (6, 6, 6), 1.0)
    rho = np.ones(dom.cells)
    facets = R.smooth_facets(dom, rho)
    vol = sum(meshgeom_volume(f) for f in facets)
    assert facets and 150 < vol < 230  # the 216 mm3 block, its edges cut by the 0.5 level


# ---------------------------------------------------------------------------- guards from the review
def _michell(**over) -> dict:
    doc = michell.spec_doc("small")
    doc.update(over)
    return doc


def _write(tmp_path, doc) -> str:
    p = tmp_path / "s.json"
    p.write_text(json.dumps(doc))
    return str(p)


def _strict(raw: str) -> dict:
    def bad(token):
        raise AssertionError(f"non-JSON constant {token} in the output")

    return json.loads(raw, parse_constant=bad)


@pytest.mark.parametrize(
    ("support", "free"),
    [
        ({"name": "clamp", "nodes": {"box": {"x": [-0.01, 0.01]}}, "dofs": "x"}, 3),  # y, z and rotation about x free
        ({"name": "pin", "nodes": {"ball": {"c": [0, 0, 0], "r": 0.01}}}, 3),  # one node: the three rotations free
    ],
)
def test_an_under_constrained_model_fails_before_the_solve(tmp_path, capsys, support, free):
    doc = _michell(supports=[support])
    code, out, raw, _ = run(capsys, "topo", "solve", _write(tmp_path, doc), "--out", str(tmp_path / "o"))
    _strict(raw)
    (f,) = [f for f in out["findings"] if f["code"] == "topo.under_constrained"]
    assert code == 1 and out["ok"] is False and out["runs"] == [] and f["measure"]["free_modes"] == free
    for weights in ("raw", "normalised"):  # both weight modes, never a wrong "zero compliance" diagnosis
        rep = Topo()._solve(parse_spec({**doc, "weights": weights})[0], False)[0]
        assert {x.code for x in rep.findings if x.severity == "FAIL"} == {"topo.under_constrained"}


def test_free_rigid_modes_of_a_full_clamp_is_empty():
    _, _, fe, nodes, _, _ = _problem(_cantilever(6, 2, 3))
    assert checks.free_rigid_modes(nodes, fe.fixed) == []
    assert len(checks.free_rigid_modes(nodes, np.zeros(0, dtype=int))) == 6


def test_raw_weights_with_a_case_on_fixed_nodes_is_a_spec_error_not_nan(tmp_path, capsys):
    doc = _michell(weights="raw")
    doc["cases"].append({"name": "on clamp", "loads": [{"nodes": {"box": {"x": [-0.01, 0.01]}}, "force": [0, 0, 1]}]})
    code, out, raw, _ = run(capsys, "topo", "solve", _write(tmp_path, doc), "--out", str(tmp_path / "o"))
    _strict(raw)
    assert code == 1 and any("'on clamp': zero compliance" in e for e in out["errors"])


def test_an_all_passive_domain_is_a_spec_error_not_nan(tmp_path, capsys):
    doc = _michell(passive=[{"name": "all", "cells": {"box": {"x": [-1, 100]}}}])
    code, out, raw, _ = run(capsys, "topo", "solve", _write(tmp_path, doc), "--out", str(tmp_path / "o"))
    _strict(raw)
    assert code == 1 and any("cover every domain cell" in e for e in out["errors"])


def test_a_nan_in_a_result_is_a_fail_and_strict_json(tmp_path, capsys, monkeypatch):
    real = checks.equilibrium

    def nan_row(*a, **k):
        rows, found = real(*a, **k)
        rows[0]["umax_mm"] = float("nan")
        return rows, found

    monkeypatch.setattr(checks, "equilibrium", nan_row)
    out_dir = tmp_path / "o"
    code, out, raw, _ = run(capsys, "topo", "solve", _write(tmp_path, _michell()), "--out", str(out_dir), "--maxit", "2")
    assert code == 1 and out["ok"] is False and "topo.non_finite" in {f["code"] for f in out["findings"]}
    assert _strict(raw)["full_density"][0]["umax_mm"] is None
    assert _strict((out_dir / "run.json").read_text()) == out


def test_two_close_volume_fractions_write_two_maps(tmp_path):
    doc = _michell()
    doc["simp"].update({"volfrac": [0.401, 0.404], "maxit": 3})
    Topo().solve(_write(tmp_path, doc), str(tmp_path / "o"), mesh="none")
    out = json.loads((tmp_path / "o" / "run.json").read_text())
    assert [r["files"]["density"] for r in out["runs"]] == ["density_v401.npy", "density_v404.npy"]
    assert (tmp_path / "o" / "density_v401.npy").read_bytes() != (tmp_path / "o" / "density_v404.npy").read_bytes()


def test_raw_weights_and_a_large_force_still_hold_the_volume():
    """The bisection bound follows the sensitivity scale: a 1e6 N load must not saturate it."""
    doc = _michell(weights="raw")
    doc["simp"]["maxit"] = 10
    doc["cases"][0]["loads"][0]["force"] = [0, 0, -1e6]
    rep = Topo()._solve(parse_spec(doc)[0], False)[0]
    run_ = rep.data["runs"][0]
    assert abs(run_["design_volume_fraction"] - 0.4) < 0.005
    assert "topo.volume_missed" not in {f.code for f in rep.findings}


def test_an_unreachable_volume_is_a_finding_not_a_convergence():
    """Five passive slices and a wide filter hold the design volume above 0.4: say so."""
    doc = {
        "schema": "shambu.topo/1",
        "units": "mm-N-MPa",
        "domain": {"box": {"min": [0, 0, 0], "max": [6, 1, 4]}, "pitch": 1},
        "simp": {"volfrac": [0.4], "rmin_cells": 2.5, "maxit": 40},
        "supports": [{"name": "c", "nodes": {"box": {"x": [-0.01, 0.01]}}}],
        "passive": [{"name": f"p{i}", "cells": {"box": {"x": [i + 0.1, i + 0.9]}}} for i in range(5)],
        "cases": [{"name": "t", "loads": [{"nodes": {"box": {"x": [5.99, 6.01]}}, "force": [0, 0, -1]}]}],
    }
    rep = Topo()._solve(parse_spec(doc)[0], False)[0]
    run_ = rep.data["runs"][0]
    (f,) = [f for f in rep.findings if f.code == "topo.volume_missed"]
    assert run_["converged"] is False and run_["design_volume_fraction"] > 0.405
    assert f.severity == "WARN" and "out of reach" in f.message


def test_a_box_over_the_cell_cap_is_refused_before_allocation(tmp_path, capsys, monkeypatch):
    from shambu.topo import voxel

    def boom(*a, **k):
        raise AssertionError("allocated a grid over the cap")

    monkeypatch.setattr(voxel, "box_domain", boom)
    code, out, _, _ = run(capsys, "topo", "solve", _write(tmp_path, _michell()), "--out", str(tmp_path / "o"), "--max-cells", "100")
    (err,) = out["errors"]
    assert code == 1 and "192 is over the cap of 100" in err and " MB" in err


def test_max_cells_raises_the_cap(tmp_path, capsys):
    doc = _michell()
    doc["simp"]["maxit"] = 2
    code, out, _, _ = run(capsys, "topo", "voxelise", _write(tmp_path, doc), "--max-cells", "192")
    assert code == 0 and out["fe"]["elements"] == 192


def test_an_stl_grid_over_the_cap_is_refused_before_voxelising(tmp_path, capsys, monkeypatch):
    from shambu.topo import voxel

    monkeypatch.setattr(voxel, "voxelise", lambda *a, **k: (_ for _ in ()).throw(AssertionError("voxelised")))
    stl(tmp_path / "big.stl", box((0, 0, 0), (100, 100, 100)))
    doc = _cantilever(10, 10, 4)
    doc["domain"] = {"stl": "big.stl", "pitch": 0.1}
    code, out, _, _ = run(capsys, "topo", "voxelise", _write(tmp_path, doc))
    assert code == 1 and "bounding grid" in out["errors"][0] and "over the cap" in out["errors"][0]


def test_override_errors_reach_library_callers(tmp_path):
    with pytest.raises(TopoSpecError, match="--maxit"):
        Topo().solve(_write(tmp_path, _michell()), str(tmp_path / "o"), maxit=0)


def test_budget_projection_counts_the_final_analysis(monkeypatch):
    """With a clock that ticks at a fixed rate, a budget stop keeps the whole run, final solve included, inside it."""
    from shambu.topo import optimise as O

    class Clock:
        t = 0.0

        def perf_counter(self):
            self.t += 1.0
            return self.t

    monkeypatch.setattr(O, "time", Clock())
    *_, prob, _ = _problem(_cantilever(8, 1, 4, maxit=50))
    for budget in (20.0, 30.0, 45.0):
        r = optimise(prob, 0.4, 50, 0.0, 0.2, 0.5, budget_s=budget)
        assert r.budget_stop and r.seconds <= budget, (budget, r.seconds, r.iterations)


def test_an_open_side_wall_is_reported():
    """Facets edge-on to the +z rays leave every column even: the welded edges still show the hole."""
    facets = [f for f in box((0, 0, 0), (10, 10, 4)) if not all(abs(v[0]) < 1e-9 for v in f)]
    dom = voxelise(facets, 1.0)
    found, _ = checks.domain_findings(dom, dom.mask)
    assert dom.odd_columns == 0 and dom.open_edges > 0 and "topo.open_mesh" in {f.code for f in found}
    closed = voxelise(box((0, 0, 0), (10, 10, 4)), 1.0)
    assert closed.open_edges == 0 and "topo.open_mesh" not in {f.code for f in checks.domain_findings(closed, closed.mask)[0]}


def test_an_inward_wound_mesh_keeps_a_positive_volume():
    facets = [(a, c, b) for a, b, c in box((0, 0, 0), (10, 10, 4))]
    dom = voxelise(facets, 1.0)
    found, _ = checks.domain_findings(dom, dom.mask)
    assert dom.stats()["volume_ratio"] == 1.0 and dom.inverted
    assert {f.code for f in found} >= {"topo.inverted_mesh"} and not any("-100" in f.message for f in found)
