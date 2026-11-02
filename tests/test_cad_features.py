"""shambu cad features and fit: hole and boss inventory, patterns, and the bore gauge against the fit table."""

from __future__ import annotations

import json

import pytest

from cadfix import codes, fails, run, spec, stl
from meshes import cylinder, mirror_y, pcd_holes, plate_with_holes, rounded_edge_bar
from shambu import mesh as M
from shambu.cad import features as FT
from shambu.cad import fits as F
from shambu.cad import gauge as GA
from shambu.cad.spec import ExpectedFeature

ANGLES = (10.0, 100.0, 190.0, 300.0)  # not symmetric about x, so a mirrored part reads other angles


def _plate():
    return plate_with_holes((50, 50, 5), pcd_holes(30, ANGLES, 2.0, (25, 25)))


def test_four_holes_on_a_pitch_circle():
    inv = FT.detect(_plate())
    assert [f.kind for f in inv.features] == ["hole"] * 4
    assert all(abs(f.diameter_mm - 4.0) <= 0.005 and f.ends == "through" for f in inv.features)
    (p,) = inv.patterns
    assert abs(p.pcd_mm - 30) < 1e-6 and [round(a, 3) for a in p.angles_deg] == list(ANGLES)


def test_mirrored_plate_reads_other_angles_and_fails_the_inventory():
    want = [ExpectedFeature("hole", 4.0, 4, pcd_mm=30.0, angles_deg=ANGLES)]
    assert FT.compare("plate", FT.detect(_plate()), want) == []
    inv = FT.detect(mirror_y(_plate(), 25))
    assert all(abs(f.diameter_mm - 4.0) <= 0.005 for f in inv.features)
    assert {f.code for f in FT.compare("plate", inv, want)} == {"feature.missing"}


def test_missing_and_extra_against_the_count():
    inv = FT.detect(_plate())
    assert {f.code for f in FT.compare("p", inv, [ExpectedFeature("hole", 4.0, 5)])} == {"feature.missing"}
    assert {f.code for f in FT.compare("p", inv, [ExpectedFeature("hole", 4.0, 3)])} == {"feature.extra"}


def test_blind_pocket_depth_and_ends():
    (f,) = FT.detect(plate_with_holes((20, 20, 10), [{"x": 10, "y": 10, "stages": [(1.9, 6)]}])).features
    assert f.kind == "hole" and abs(f.diameter_mm - 3.8) < 1e-6 and abs(f.depth_mm - 6.0) <= 0.02 and f.ends == "blind"


def test_boss_and_fillet_strip():
    (f,) = FT.detect(cylinder(4, 10)).features
    assert f.kind == "boss" and abs(f.diameter_mm - 8) < 1e-6
    assert FT.detect(rounded_edge_bar((20, 10, 10), 3)).features == []


def test_horizontal_bore_is_observed_info():
    plate = M.transform(plate_with_holes((20, 20, 10), [{"x": 10, "y": 10, "stages": [(2, 0)]}]), M.euler_matrix(0, 90, 0))
    inv = FT.detect(plate, bed_face=(0, 0, -1))
    (f,) = inv.features
    assert f.print_axis == "horizontal"
    (info,) = FT.compare("p", inv, [])
    assert info.code == "feature.horizontal_bore" and info.severity == "INFO" and info.evidence == "observed"


# ---------------------------------------------------------------------------- gauge
def _bore(stages, x=10.0, d_nom=None):
    plate = plate_with_holes((20, 20, 10), [{"x": x, "y": 10, "stages": stages}])
    return GA.Gauge(plate).bore((10, 10, 10), (0, 0, -1), d_nom or 2 * stages[0][0])


def test_gauge_blind_pocket():
    b = _bore([(1.9, 6)])
    assert abs(b.diameter_mm - 3.8) < 1e-6 and abs(b.depth_mm - 6.0) <= 0.02 and b.ends == "blind" and abs(b.floor_mm - 6.0) <= 0.06


def test_gauge_stops_at_a_step_into_a_relief_bore():
    b = _bore([(2.9, 6), (2.25, 0)])
    assert abs(b.depth_mm - 6.0) <= 0.02 and abs(b.diameter_mm - 5.8) < 1e-6


def test_gauge_offset_hole():
    b = _bore([(2.0, 0)], x=10.3)
    assert abs(b.offset_mm - 0.3) <= 0.01
    fit = F.Fit("M4-test", "clearance", 4.0, offset_max_mm=0.1, basis="test")
    assert {f.code for f in GA.judge("p", "h1", fit, b, (0, 0, -1), (10, 10, 10), None, None, {})} == {"fit.offset"}


def test_gauge_no_hole_at_the_mouth():
    plate = plate_with_holes((20, 20, 10), [{"x": 10, "y": 10, "stages": [(2.0, 0)]}])
    assert GA.Gauge(plate).bore((3, 3, 10), (0, 0, -1), 4.0) is None
    (f,) = GA.judge("p", "h1", F.FITS[0], None, (0, 0, -1), (3, 3, 10), None, None, {})
    assert f.code == "fit.not_found" and f.severity == "FAIL"


@pytest.mark.parametrize(("r", "ok"), [(1.7, True), (1.8, False)])
def test_iso_273_m3_medium(tmp_path, capsys, r, ok):
    part = stl(tmp_path / "p.stl", plate_with_holes((20, 20, 5), [{"x": 10, "y": 10, "stages": [(r, 0)]}]))
    code, doc, _, _ = run(capsys, "cad", "fit", str(part), "--hole", "h1:M3-clearance-medium:10,10,5:0,0,-1")
    assert (code == 0) is ok and ("fit.diameter" in fails(doc)) is not ok


def test_insert_fit_from_a_spec_needs_a_coupon_and_its_depth(tmp_path, capsys):
    stl(tmp_path / "lid.stl", plate_with_holes((20, 20, 10), [{"x": 10, "y": 10, "stages": [(2.0, 5)]}]))
    sp = spec(
        tmp_path / "cad.json",
        {
            "fits": [{"name": "insert-M3", "kind": "insert", "hole_mm": 4.0, "depth_min_mm": 6.0, "basis": "insert datasheet"}],
            "parts": [
                {"name": "lid", "mesh": "lid.stl", "holes": [{"name": "h1", "fit": "insert-M3", "mouth": [10, 10, 10], "axis": [0, 0, -1]}]}
            ],
            "rules": [{"id": "holes", "type": "fit", "parts": "part:lid"}],
        },
    )
    code, doc, _, _ = run(capsys, "cad", "gate", str(sp))
    assert code == 1 and fails(doc) == {"fit.depth"} and "fit.coupon" in codes(doc)


def test_a_spec_fit_that_shadows_a_built_in_warns(tmp_path, capsys):
    stl(tmp_path / "p.stl", plate_with_holes((20, 20, 5), [{"x": 10, "y": 10, "stages": [(1.7, 0)]}]))
    sp = spec(
        tmp_path / "cad.json",
        {
            "fits": [{"name": "M3-clearance-medium", "kind": "clearance", "hole_mm": 3.5, "band_mm": [-0.2, 0.2], "basis": "my printer"}],
            "parts": [
                {
                    "name": "p",
                    "mesh": "p.stl",
                    "holes": [{"name": "h1", "fit": "M3-clearance-medium", "mouth": [10, 10, 5], "axis": [0, 0, -1]}],
                }
            ],
            "rules": [{"id": "holes", "type": "fit"}],
        },
    )
    code, doc, _, _ = run(capsys, "cad", "gate", str(sp))
    (w,) = [f for f in doc["findings"] if f["code"] == "fit.shadowed"]
    assert code == 0 and "3.5" in w["message"] and "ISO 273" in w["message"] and w["severity"] == "WARN"


def test_iso_273_rows_are_data():
    by = {f.name: f for f in F.FITS}
    assert by["M3-clearance-medium"].hole_mm == 3.4 and by["M8-clearance-coarse"].hole_mm == 10.0
    assert all(f.basis.startswith("ISO 273") and f.kind == "clearance" for f in F.FITS)
    assert len(F.FITS) == 21


def test_features_cli_one_document(tmp_path, capsys):
    part = stl(tmp_path / "p.stl", _plate())
    code, doc, out, _ = run(capsys, "cad", "features", str(part))
    assert code == 0 and json.loads(out) == doc and len(doc["rules"][0]["parts"]["p"]["features"]) == 4


def test_pocket_depth_counts_from_the_material_not_the_declared_mouth():
    """A 6 mm pocket whose mouth is declared 0.8 mm above the surface once read 6.8 mm deep."""
    plate = plate_with_holes((20, 20, 10), [{"x": 10, "y": 10, "stages": [(2.0, 6)]}])
    b = GA.Gauge(plate).bore((10, 10, 10.8), (0, 0, -1), 4.0)
    assert b is not None and abs(b.depth_mm - 6.0) <= 0.02
    fit = F.Fit("insert-test", "insert", 4.0, (-0.1, 0.1), depth_min_mm=6.5, basis="test")
    assert "fit.depth" in {f.code for f in GA.judge("p", "h1", fit, b, (0, 0, -1), (10, 10, 10.8), None, None, {})}


def test_an_oval_bore_fails_a_round_fit():
    """A 3.9 x 4.1 mm bore averages 4.0 mm; its narrow side is what a 4.00 mm pin meets."""
    plate = plate_with_holes((20, 20, 5), [{"x": 10, "y": 10, "stages": [(2.0, 0)]}])
    oval = [tuple((x * 1.025, y * 0.975, z) for x, y, z in f) for f in plate]
    b = GA.Gauge(oval).bore((10.25, 9.75, 5), (0, 0, -1), 4.0)
    assert b is not None
    fit = F.Fit("slip-test", "slip", 4.0, basis="test")
    assert "fit.diameter" in {f.code for f in GA.judge("p", "h1", fit, b, (0, 0, -1), (10.25, 9.75, 5), None, None, {})}


def test_an_unmeasured_wall_is_incomplete_not_a_pass():
    b = GA.Bore(0.05, 5.0, 4.0, (10.0, 10.0, 5.0), 0.0, None, None, "through", 0.0, 64)
    fit = F.Fit("wall-test", "clearance", 4.0, wall_min_mm=2.0, basis="test")
    (f,) = [f for f in GA.judge("p", "h1", fit, b, (0, 0, -1), (10, 10, 5), None, None, {}) if f.severity == "FAIL"]
    assert f.code.startswith("cad.incomplete.")


def test_a_failed_band_fit_reports_no_residual_and_still_prints(tmp_path, capsys, monkeypatch):
    """A failed refit of the walled band once set the residual to NaN, and the strict JSON writer crashed on it."""
    import sys

    from shambu.cad import linalg as L

    real = L.fit_circle

    def fit(points):
        if sys._getframe(1).f_code.co_name == "bore":
            raise ValueError("degenerate band")
        return real(points)

    monkeypatch.setattr(L, "fit_circle", fit)
    part = stl(tmp_path / "p.stl", plate_with_holes((20, 20, 5), [{"x": 10, "y": 10, "stages": [(2.25, 0)]}]))
    _, doc, _, _ = run(capsys, "cad", "fit", str(part), "--hole", "h1:M4-clearance-medium:10,10,5:0,0,-1")
    assert doc is not None and "error" not in doc
    (row,) = doc["rules"][0]["parts"]["p"]["holes"]
    assert row["bore"]["residual_mm"] is None
