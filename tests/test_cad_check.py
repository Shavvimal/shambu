"""shambu cad check: one closed solid, and volume, size and mass against the record and the spec."""

from __future__ import annotations

import math

import pytest

from cadfix import codes, fails, run, stl, write_record
from meshes import box, cylinder, without
from shambu.cad import solid as SO

R, H, N = 10.0, 20.0, 64


def _inward_box(lo, hi):
    return [(a, c, b) for a, b, c in box(lo, hi)]


def test_closed_box_is_one_solid():
    areas = SO.check("b", box((0, 0, 0), (10, 10, 10)), None, SO.Expect())
    assert areas["solid"].status == "no-finding" and areas["solid"].measures["bodies"] == 1


@pytest.mark.parametrize(
    ("facets", "code"),
    [
        (without(box((0, 0, 0), (10, 10, 10)), 3), "solid.open"),
        (box((0, 0, 0), (10, 10, 10)) + box((20, 0, 0), (30, 10, 10)), "solid.bodies"),
    ],
)
def test_not_one_solid_fails(facets, code):
    areas = SO.check("b", facets, None, SO.Expect())
    assert code in {f.code for f in areas["solid"].findings if f.severity == "FAIL"}


def test_void_is_info():
    areas = SO.check("b", box((0, 0, 0), (20, 20, 20)) + _inward_box((5, 5, 5), (10, 10, 10)), None, SO.Expect())
    void = [f for f in areas["solid"].findings if f.code == "solid.void"]
    assert void and void[0].severity == "INFO" and areas["solid"].measures["bodies"] == 1


def _cyl_record(tmp_path, cad_volume):
    part = stl(tmp_path / "cyl.stl", cylinder(R, H, N))
    rec = write_record(part, "cyl", stl={"faceting_tol_mm": 0.1}, cad={"volume_mm3": cad_volume, "from": "calculateMassProperties"})
    return part, rec


def test_stl_volume_within_the_faceting_band_of_the_cad(tmp_path, capsys):
    analytic = math.pi * R * R * H  # the CAD's exact volume; the 64-gon is 0.16 % smaller, inside A x 0.1 mm
    part, rec = _cyl_record(tmp_path, analytic)
    code, doc, _, _ = run(capsys, "cad", "check", str(part), "--record", str(rec))
    vol = doc["rules"][0]["parts"]["cyl"]["areas"]["volume"]
    assert code == 0 and vol["status"] == "no-finding"
    assert abs(vol["mesh_mm3"] - analytic) <= vol["tolerance_mm3"]


def test_stl_volume_far_from_the_cad_fails(tmp_path, capsys):
    # The band is A x 0.1 mm = 188 mm3 here (3 % of the volume); a CAD volume 4 % higher is outside it.
    part, rec = _cyl_record(tmp_path, math.pi * R * R * H * 1.04)
    code, doc, _, _ = run(capsys, "cad", "check", str(part), "--record", str(rec))
    assert code == 1 and "volume.vs_cad" in fails(doc)


@pytest.mark.parametrize(("rel", "ok"), [(1e-4, True), (1e-3, False)])
def test_step_reimport_band(tmp_path, capsys, rel, ok):
    v = 1000.0
    part = stl(tmp_path / "b.stl", box((0, 0, 0), (10, 10, 10)))
    rec = write_record(part, "b", cad={"volume_mm3": v}, step={"path": None, "sha256": None, "reimport_volume_mm3": v * (1 + rel)})
    code, doc, _, _ = run(capsys, "cad", "check", str(part), "--record", str(rec))
    assert (code == 0) is ok and ("volume.vs_step" in fails(doc)) is not ok


@pytest.mark.parametrize(("size_z", "ok"), [(10.04, True), (10.06, False)])
def test_size_band(tmp_path, capsys, size_z, ok):
    part = stl(tmp_path / "b.stl", box((0, 0, 0), (20, 10, size_z)))
    code, doc, _, _ = run(capsys, "cad", "check", str(part), "--expect-size", "20,10,10", "--size-tol", "0.05")
    assert (code == 0) is ok and ("size.vs_expected" in fails(doc)) is not ok


def test_expected_volume_needs_a_tolerance(tmp_path, capsys):
    part = stl(tmp_path / "b.stl", box((0, 0, 0), (10, 10, 10)))
    code, doc, _, _ = run(capsys, "cad", "check", str(part), "--expect-volume", "1000")
    assert code == 1 and "volume-tol" in doc["error"]


def test_mass_is_information_only(tmp_path, capsys):
    part = stl(tmp_path / "b.stl", box((0, 0, 0), (10, 10, 10)))
    code, doc, _, _ = run(capsys, "cad", "check", str(part), "--density", "1.27")
    mass = doc["rules"][0]["parts"]["b"]["areas"]["mass"]
    assert code == 0 and mass["mass_g_solid"] == 1.27


def test_cog_far_from_the_cad_warns(tmp_path, capsys):
    part = stl(tmp_path / "b.stl", box((0, 0, 0), (10, 10, 10)))
    rec = write_record(part, "b", cad={"volume_mm3": 1000, "cog": [5, 5, 7]})
    code, doc, _, _ = run(capsys, "cad", "check", str(part), "--record", str(rec))
    assert code == 0 and "mass.cog_vs_cad" in codes(doc)


def test_no_record_lists_provenance_not_checked(tmp_path, capsys):
    part = stl(tmp_path / "b.stl", box((0, 0, 0), (10, 10, 10)))
    code, doc, _, _ = run(capsys, "cad", "check", str(part))
    prov = doc["rules"][0]["parts"]["b"]["areas"]["provenance"]
    assert code == 0 and prov["status"] == "not-checked" and "stamp" in prov["reason"]


def test_metres_mesh_fails(tmp_path, capsys):
    part = stl(tmp_path / "b.stl", box((0, 0, 0), (0.02, 0.01, 0.005)))
    code, doc, _, _ = run(capsys, "cad", "check", str(part))
    assert code == 1 and "mesh.metres" in fails(doc)


def test_an_inward_shell_outside_every_body_is_not_a_void():
    """An inside-out box beside a box is not a cavity: it once read as one body, watertight, 875 mm3."""
    areas = SO.check("b", box((0, 0, 0), (10, 10, 10)) + _inward_box((20, 20, 20), (25, 25, 25)), None, SO.Expect())
    codes_ = {f.code: f.severity for f in areas["solid"].findings}
    assert codes_.get("solid.inverted") == "FAIL" and "solid.void" not in codes_


def _ascii_stl(path, first_vertex):
    path.write_text(
        "solid b\nfacet normal 0 0 0\nouter loop\n"
        f"vertex {first_vertex}\nvertex 1 0 0\nvertex 0 1 0\n"
        "endloop\nendfacet\nendsolid b\n"
    )
    return path


def test_a_bad_stl_coordinate_is_a_named_error(tmp_path, capsys, vertex):
    """A non-numeric or non-finite vertex once crashed every cad verb with a traceback."""
    p = _ascii_stl(tmp_path / "bad.stl", vertex)
    code, doc, _, _ = run(capsys, "cad", "check", str(p))
    assert code == 3 and "bad.stl" in doc["error"] and "vertex" in doc["error"]


def test_a_non_finite_binary_stl_coordinate_is_a_named_error(tmp_path, capsys):
    import struct

    tri = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, math.nan, 0.0)]
    data = b"\0" * 80 + struct.pack("<I", 1) + struct.pack("<12f", 0, 0, 0, *[x for v in tri for x in v]) + b"\0\0"
    p = tmp_path / "nan.stl"
    p.write_bytes(data)
    code, doc, _, _ = run(capsys, "cad", "check", str(p))
    assert code == 3 and "nan.stl" in doc["error"] and "finite" in doc["error"]


def test_a_truncated_binary_stl_names_the_size_mismatch(tmp_path, capsys):
    """A binary STL whose header starts with `solid` and lost its tail once read as an empty ASCII STL."""
    full = stl(tmp_path / "full.stl", box((0, 0, 0), (10, 10, 10))).read_bytes()
    p = tmp_path / "cut.stl"
    p.write_bytes(b"solid cube" + full[10:-30])
    code, doc, _, _ = run(capsys, "cad", "check", str(p))
    assert code == 3 and "12 facets" in doc["error"] and str(len(full) - 30) in doc["error"]
