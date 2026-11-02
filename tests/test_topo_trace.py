"""The topo-trace extra: density map to window polygons on synthetic walls. Skipped without numpy, scipy and shapely;
the CI `topo` job installs the extras and runs it."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
pytest.importorskip("shapely")

from cadfix import run  # noqa: E402
from shambu.topo import trace as T  # noqa: E402
from shambu.topo.spec import PRESETS  # noqa: E402

PITCH = 2.0


def _run_dir(tmp: Path, dens: np.ndarray, passive: np.ndarray | None = None, names=()) -> Path:
    np.save(tmp / "density_v50.npy", dens.astype(np.float32))
    np.save(tmp / "passive.npy", np.zeros(dens.shape, dtype=np.int16) if passive is None else passive.astype(np.int16))
    doc = {
        "grid": {"origin": [0, 0, 0], "pitch": PITCH, "shape": list(dens.shape)},
        "passive": list(names),
        "runs": [{"volfrac": 0.5, "files": {"density": "density_v50.npy"}}],
    }
    (tmp / "run.json").write_text(json.dumps(doc))
    return tmp / "run.json"


def _faces(tmp: Path, faces: list[dict], build_axis: str = "z", keepouts=()) -> Path:
    p = tmp / "faces.json"
    p.write_text(json.dumps({"schema": "shambu.topo.faces/1", "build_axis": build_axis, "faces": faces, "keepouts": list(keepouts)}))
    return p


def _plate_with_disc(r: float = 8.0, centres=((20.0, 15.0),)) -> np.ndarray:
    """A 40 x 30 mm plate one 2 mm cell thick (normal z), solid except low-density discs."""
    nx, ny = 20, 15
    dens = np.ones((nx, ny, 1))
    x, y = np.meshgrid((np.arange(nx) + 0.5) * PITCH, (np.arange(ny) + 0.5) * PITCH, indexing="ij")
    for cx, cy in centres:
        dens[..., 0][np.hypot(x - cx, y - cy) < r] = 0.0
    return dens


PLATE = {"name": "plate", "axis": "z", "slab": [0, 2], "u": [0, 40], "v": [0, 30]}


def test_one_disc_gives_one_round_window(tmp_path, capsys):
    run_json = _run_dir(tmp_path, _plate_with_disc())
    code, doc, _, _ = run(capsys, "topo", "trace", str(run_json), "--faces", str(_faces(tmp_path, [PLATE])))
    assert code == 0 and doc["schema"] == "shambu.topo.trace/1"
    face = doc["faces"][0]
    (w,) = face["windows"]
    disc = math.pi * 8.0**2
    assert 0.8 * disc < w["area_mm2"] < 1.05 * disc, w["area_mm2"]
    assert w["removed_mm3"] == pytest.approx(w["area_mm2"] * 2.0, rel=1e-6) and w["mean_density"] < 0.2
    assert w["n_vertices"] <= PRESETS["conservative"].max_vertices and face["print_rule"] is False
    assert face["stage_areas_mm2"]["final"] <= face["stage_areas_mm2"]["candidate"]
    assert "topo.map_not_proof" in {f["code"] for f in doc["findings"]} and doc["not_checked"]


def test_a_keepout_ball_cuts_the_window_with_clearance(tmp_path, capsys):
    run_json = _run_dir(tmp_path, _plate_with_disc())
    ball = {"name": "probe", "c": [28.0, 15.0, 1.0], "r": 2.0}
    code, doc, _, _ = run(capsys, "topo", "trace", str(run_json), "--faces", str(_faces(tmp_path, [PLATE], keepouts=[ball])))
    (w,) = doc["faces"][0]["windows"]
    rules = PRESETS["conservative"]
    assert code == 0 and w["clearance"]["nearest"] == "probe" and w["clearance"]["mm"] > rules.clearance_mm
    assert w["area_mm2"] < 0.9 * math.pi * 64


def test_a_passive_region_is_kept_clear(tmp_path, capsys):
    dens = _plate_with_disc()
    passive = np.zeros(dens.shape)
    passive[13, 7, 0] = 1  # a boss cell inside the disc at (27, 15)
    run_json = _run_dir(tmp_path, dens, passive, ["boss"])
    code, doc, _, _ = run(capsys, "topo", "trace", str(run_json), "--faces", str(_faces(tmp_path, [PLATE])))
    (w,) = doc["faces"][0]["windows"]
    assert code == 0 and w["clearance"]["nearest"] == "boss" and w["clearance"]["mm"] > 3.0


def test_clearance_at_the_rule_is_a_fail():
    rules = PRESETS["conservative"]
    w = {"name": "f-w1", "clearance": {"mm": rules.clearance_mm - 0.5, "nearest": "boss"}, "roof_flat_mm": None}
    (f,) = T.window_findings(w, rules)
    assert f.code == "topo.clearance" and f.severity == "FAIL"
    assert T.window_findings({**w, "clearance": {"mm": rules.clearance_mm + 0.5, "nearest": "boss"}}, rules) == []


def test_print_rule_turns_a_square_hole_into_a_pointed_arch():
    win = np.zeros((40, 40), dtype=bool)
    win[10:30, 10:30] = True
    air = np.zeros_like(win)
    out = T.print_rule(win, air, up=1, top_open=False, apex_px=4)
    rows = [int(out[:, j].sum()) for j in range(29, 9, -1)]  # top row first
    assert rows[0] == 4 and rows[1] == 6 and rows[2] == 8  # widens one cell a side per row: 45 degrees
    assert out[:, 10].sum() == 20 and out.sum() < win.sum()  # the floor stays; the roof corners go
    assert not (out & ~win).any()


def test_print_rule_needs_the_build_axis_in_the_face(tmp_path, capsys):
    """A square low-density patch: on a wall that holds the build axis the roof is cut to 45 degrees; on a wall
    normal to it the window keeps its square corners (apart from the corner radius)."""
    n = 20
    dens = np.ones((1, n, n))
    dens[0, 5:15, 5:15] = 0.0  # a 20 mm square in the y-z plane
    run_json = _run_dir(tmp_path, dens)
    face_x = {"name": "side", "axis": "x", "slab": [0, 2], "u": [0, 40], "v": [0, 40]}
    code, doc, _, _ = run(capsys, "topo", "trace", str(run_json), "--faces", str(_faces(tmp_path, [face_x], build_axis="z")))
    (arch,) = doc["faces"][0]["windows"]
    code2, doc2, _, _ = run(capsys, "topo", "trace", str(run_json), "--faces", str(_faces(tmp_path, [face_x], build_axis="x")))
    (square,) = doc2["faces"][0]["windows"]
    assert code == code2 == 0 and doc["faces"][0]["print_rule"] and not doc2["faces"][0]["print_rule"]
    assert arch["area_mm2"] < square["area_mm2"] - 20
    top = max(p[1] for p in arch["poly"])
    assert max(p[0] for p in arch["poly"] if p[1] > top - 0.5) - min(p[0] for p in arch["poly"] if p[1] > top - 0.5) < 3.0
    assert arch["roof_flat_mm"] is not None and square["roof_flat_mm"] is None


def test_rib_rule_widens_a_thin_rib():
    rules = PRESETS["conservative"]
    h = rules.raster_mm
    n = int(40 / h)
    yy, xx = np.meshgrid(np.arange(n) * h, np.arange(n) * h)
    win = (np.hypot(xx - 12.5, yy - 20) < 6) | (np.hypot(xx - 27.5, yy - 20) < 6)  # 3 mm of material between
    face = T.Face("f", "z", (0, 2), (0, 40), (0, 40))
    out = T._rib_rule(win, np.zeros_like(win), np.zeros_like(win), face, rules)
    row = out[:, int(20 / h)]
    gap = (np.flatnonzero(row[int(20 / h) :])[0]) * h + (int(20 / h) - np.flatnonzero(row[: int(20 / h)])[-1] - 1) * h
    assert gap >= rules.rib_mm - h


def test_rib_rule_opens_a_thin_strip_to_air():
    rules = PRESETS["conservative"]
    h = rules.raster_mm
    n = int(40 / h)
    win = np.zeros((n, n), dtype=bool)
    air = np.zeros_like(win)
    win[40:80, 40:120] = True
    air[88:, :] = True  # 2 mm of material between the window and air
    face = T.Face("f", "z", (0, 2), (0, 40), (0, 40))
    out = T._rib_rule(win, air, np.zeros_like(win), face, rules)
    assert out[80:88, 60:100].all()


def test_trace_rules_dominate(tmp_path, capsys):
    """A disc that sits under the rim: the rules, not the map, remove the window."""
    run_json = _run_dir(tmp_path, _plate_with_disc(r=6.0, centres=((4.0, 15.0),)))
    code, doc, _, _ = run(capsys, "topo", "trace", str(run_json), "--faces", str(_faces(tmp_path, [PLATE])))
    assert code == 0 and "topo.trace_rules_dominate" in {f["code"] for f in doc["findings"]}


def test_unknown_volfrac_is_a_spec_error(tmp_path, capsys):
    run_json = _run_dir(tmp_path, _plate_with_disc())
    code, doc, _, _ = run(capsys, "topo", "trace", str(run_json), "--faces", str(_faces(tmp_path, [PLATE])), "--volfrac", "0.7")
    assert code == 1 and "no run at volfrac 0.7" in doc["errors"][0]


# ---------------------------------------------------------------------------- guards from the review
def _annulus() -> np.ndarray:
    """Low density in a ring (r 4 to 11 mm) round a solid island at (20, 15)."""
    nx, ny = 20, 15
    dens = np.ones((nx, ny, 1))
    x, y = np.meshgrid((np.arange(nx) + 0.5) * PITCH, (np.arange(ny) + 0.5) * PITCH, indexing="ij")
    r = np.hypot(x - 20, y - 15)
    dens[..., 0][(r > 4) & (r < 11)] = 0.0
    return dens


def test_a_window_never_swallows_an_island(tmp_path, capsys):
    from shapely import geometry

    run_json = _run_dir(tmp_path, _annulus())
    code, doc, _, _ = run(capsys, "topo", "trace", str(run_json), "--faces", str(_faces(tmp_path, [PLATE])))
    face = doc["faces"][0]
    polys = [geometry.Polygon(w["poly"]) for w in face["windows"]]
    assert code == 0 and polys
    assert not any(p.contains(geometry.Point(20, 15)) for p in polys)  # the solid island stays
    assert face["stage_areas_mm2"]["final"] <= face["stage_areas_mm2"]["after_print_rule"] + 1.0
    assert all(w["mean_density"] < 0.3 for w in face["windows"])


def test_a_missing_passive_file_is_an_error_when_the_run_has_passive_regions(tmp_path, capsys):
    dens = _plate_with_disc()
    passive = np.zeros(dens.shape)
    passive[13, 7, 0] = 1
    run_json = _run_dir(tmp_path, dens, passive, ["boss"])
    (tmp_path / "passive.npy").unlink()
    code, doc, _, _ = run(capsys, "topo", "trace", str(run_json), "--faces", str(_faces(tmp_path, [PLATE])))
    assert code == 3 and "passive.npy" in doc["error"] and "boss" in doc["error"]


def test_a_passive_cell_just_past_an_open_edge_is_kept_clear(tmp_path, capsys):
    dens = _plate_with_disc()
    x, y = np.meshgrid((np.arange(20) + 0.5) * PITCH, (np.arange(15) + 0.5) * PITCH, indexing="ij")
    dens[..., 0][(x > 26) & (x < 37) & (np.abs(y - 15) < 6)] = 0.0  # low density up to the face edge at u = 37
    passive = np.zeros(dens.shape)
    passive[19, 7, 0] = 1  # a boss cell at u 38 to 40: outside the face rectangle, 1 mm past its open edge
    run_json = _run_dir(tmp_path, dens, passive, ["boss"])
    face = {"name": "plate", "axis": "z", "slab": [0, 2], "u": [0, 37], "v": [0, 30], "open_edges": ["u_hi"]}
    code, doc, _, _ = run(capsys, "topo", "trace", str(run_json), "--faces", str(_faces(tmp_path, [face])))
    assert code == 0, doc["findings"]
    assert all(w["clearance"]["mm"] > PRESETS["conservative"].clearance_mm for w in doc["faces"][0]["windows"])


@pytest.mark.parametrize(
    ("rule", "needle"),
    [
        ({"max_vertices": 2}, "rules.max_vertices"),
        ({"raster_mm": 0}, "rules.raster_mm"),
        ({"roof_deg": 0}, "rules.roof_deg"),
        ({"threshold": 1.5}, "rules.threshold"),
    ],
)
def test_rules_that_would_hang_or_divide_by_zero_are_errors(tmp_path, capsys, rule, needle):
    run_json = _run_dir(tmp_path, _plate_with_disc())
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"schema": "shambu.topo.rules/1", **rule}))
    code, doc, _, _ = run(capsys, "topo", "trace", str(run_json), "--faces", str(_faces(tmp_path, [PLATE])), "--rules", str(rules))
    assert code == 1 and any(needle in e for e in doc["errors"]), doc


def test_a_raster_over_the_cap_is_refused(tmp_path, capsys):
    run_json = _run_dir(tmp_path, _plate_with_disc())
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"schema": "shambu.topo.rules/1", "raster_mm": 0.001}))
    code, doc, _, _ = run(capsys, "topo", "trace", str(run_json), "--faces", str(_faces(tmp_path, [PLATE])), "--rules", str(rules))
    assert code == 1 and "over the cap" in doc["errors"][0]
