"""`shambu printability`: each check on a mesh built in code, the 3MF path, the CLI contract and the evidence rule."""

from __future__ import annotations

import json
import math
import random

import pytest

from conftest import MACHINE, FakeStudio, make_geo_3mf
from meshes import box, cup, flipped, prism, rotate_z, without
from shambu import cli, findings, presets
from shambu import mesh as M
from shambu import printability as pa
from shambu.errors import PresetError
from shambu.facade import Bambu


@pytest.fixture(autouse=True)
def fake_app(monkeypatch, resolver):
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=FakeStudio(), resolver=resolver))


def run(facets, limits: pa.Limits | None = None, name: str = "part", **flags) -> pa.ObjectReport:
    lim = (limits or pa.Limits.default()).with_flags(**flags)
    return pa.analyse([pa.Subject(name, facets, "posed")], lim).objects[0]


def codes(o: pa.ObjectReport) -> list[str]:
    return [f.code for f in o.findings]


def only(o: pa.ObjectReport, code: str) -> findings.Finding:
    hits = [f for f in o.findings if f.code == code]
    assert len(hits) == 1, codes(o)
    return hits[0]


# Shapes (mm). Profiles are (x, z) and extrude 10 mm along y unless noted.
CANTILEVER = [(0, 0), (10, 0), (10, 20), (40, 20), (40, 30), (0, 30)]  # arm 30 mm out at z 20
GAMMA = [(0, 0), (10, 0), (10, 40), (80, 40), (80, 50), (0, 50)]  # short foot, long arm: centre of mass outside


def lintel(span: float) -> list:
    return prism([(0, 0), (5, 0), (5, 20), (5 + span, 20), (5 + span, 0), (10 + span, 0), (10 + span, 25), (0, 25)], 10)


def bed_fillet_box(r: float = 3.0, segments: int = 5) -> list:
    arc = [
        (r + r * math.cos(math.radians(180 + 90 * k / segments)), r + r * math.sin(math.radians(180 + 90 * k / segments)))
        for k in range(1, segments)
    ]
    return prism([(r, 0), (30, 0), (30, 10), (0, 10), (0, r), *arc], 10)


# ---------------------------------------------------------------------------- whole report
def test_cube_has_no_warning_and_names_its_limits():
    rep = pa.analyse([pa.Subject("cube", box((0, 0, 0), (20, 20, 20)), "posed")], pa.Limits.default())
    o = rep.objects[0]
    assert rep.counts["FAIL"] == 0 and rep.counts["WARN"] == 0 and rep.exit_code == 0
    assert {a: r.status for a, r in o.areas.items() if a != "fit"} == dict.fromkeys(
        ("mesh", "support", "bridges", "walls", "layers", "stability", "adhesion"), "no-finding"
    )
    assert o.areas["fit"].status == "not-checked" and "pass --printer" in (o.areas["fit"].reason or "")
    assert codes(o) == ["brim.not_needed"]  # 400 mm2 on the bed, slenderness 1
    doc = rep.to_json()
    assert doc["limits"]["overhang_deg_from_vertical"] == {"value": 60.0, "source": "default"}
    assert len(doc["not_checked"]) == len(pa.NOT_CHECKED) >= 10


def test_no_overall_score_anywhere():
    doc = json.dumps(pa.analyse([pa.Subject("c", box((0, 0, 0), (20, 20, 20)), "posed")], pa.Limits.default()).to_json())
    assert '"score"' not in doc and '"ready"' not in doc


def test_shuffled_facets_give_the_same_report():
    f = lintel(8) + [tuple((x + dx, y, z) for x, y, z in t) for dx in (60, 120) for t in prism(CANTILEVER, 10)]
    g = list(f)
    random.Random(3).shuffle(g)  # noqa: S311 (a fixed test order, not a secret)

    def doc(facets):
        d = pa.analyse([pa.Subject("p", facets, "posed")], pa.Limits.default()).to_json()
        d.pop("elapsed_s")
        return d

    assert doc(f) == doc(g) == doc(f[::-1])


# ---------------------------------------------------------------------------- mesh
def test_open_mesh_fails_and_skips_the_inside_checks():
    o = run(without(box((0, 0, 0), (20, 20, 20)), 4))
    assert only(o, "mesh.open_edges").severity == "FAIL"
    for a in ("walls", "layers", "stability"):
        assert o.areas[a].status == "not-checked" and "open mesh" in (o.areas[a].reason or "")


def test_mesh_in_metres_fails():
    assert only(run(box((0, 0, 0), (0.02, 0.02, 0.02))), "mesh.metres").severity == "FAIL"


def test_reversed_facet_warns():
    o = run(flipped(box((0, 0, 0), (20, 20, 20)), 2))
    assert only(o, "mesh.flipped").severity == "WARN" and not o.watertight


def test_two_bodies_are_info():
    o = run(box((0, 0, 0), (10, 10, 10)) + box((20, 0, 0), (30, 10, 10)))
    assert only(o, "mesh.bodies").measure == {"bodies": 2}


# ---------------------------------------------------------------------------- support
def test_cantilever_is_one_overhang_region_and_no_bridge():
    o = run(prism(CANTILEVER, 10))
    f = only(o, "overhang.region")
    assert f.measure["area_mm2"] == pytest.approx(300, rel=0.01)
    assert f.location is not None and f.location.z == pytest.approx((20, 20))
    assert f.fix is not None and f.fix.spec == {"recipes": ["tree-supports"]}
    assert o.areas["bridges"].measures["bridges"] == 0


def test_threshold_flag_flags_a_50_degree_face():
    slope = prism([(0, 0), (10, 0), (10 + 10 * math.tan(math.radians(50)), 10), (0, 10)], 10)
    at60 = run(slope)
    assert "overhang.region" not in codes(at60)
    assert at60.areas["support"].measures["overhang_mm2_45_to_threshold"] > 0
    at45 = pa.analyse([pa.Subject("s", slope, "posed")], pa.Limits.default().with_flags(overhang_deg=45.0))
    assert "overhang.region" in codes(at45.objects[0])
    assert at45.to_json()["limits"]["overhang_deg_from_vertical"] == {"value": 45.0, "source": "flag"}


def test_overhang_meeting_the_bed_is_a_bed_fillet():
    o = run(bed_fillet_box())
    assert "overhang.bed_fillet" in codes(o) and "overhang.region" not in codes(o)


# ---------------------------------------------------------------------------- bridges
def test_long_lintel_is_a_bridge_studio_supports():
    o = run(lintel(30))
    f = only(o, "bridge.long")
    assert f.severity == "INFO" and f.measure["span_mm"] == pytest.approx(30, abs=0.5)
    assert "bridge.studio_supports" not in codes(o) and "overhang.region" not in codes(o)


def test_short_lintel_gets_the_bridge_recipe():
    f = only(run(lintel(8)), "bridge.studio_supports")
    assert f.severity == "WARN" and f.measure["span_mm"] == pytest.approx(8, abs=0.5)
    assert f.fix is not None and f.fix.spec == {"recipes": ["bridge-short-spans"]}


def test_short_lintel_within_max_bridge_is_free():
    o = run(lintel(8), max_bridge_mm=10.0)
    assert only(o, "bridge.free").severity == "INFO" and "bridge.studio_supports" not in codes(o)


def test_ceiling_beside_a_down_slope_is_not_a_bridge():
    # The arm's flat underside ends in a drip edge that slopes down and outwards: the slope goes below the ceiling
    # but faces away from it, so it is no anchor and the ceiling stays a cantilever.
    o = run(prism([(0, 0), (10, 0), (10, 20), (30, 20), (40, 15), (40, 30), (0, 30)], 10))
    assert o.areas["bridges"].measures["bridges"] == 0 and "overhang.region" in codes(o)


def test_ceiling_anchored_all_round_spans_its_short_side():
    o = run(cup((24, 16), (20, 12), 10, 2))
    assert only(o, "bridge.long").measure["span_mm"] == pytest.approx(12, abs=0.5)


def test_diagonal_ceiling_spans_the_same():
    o = run(rotate_z(cup((24, 16), (20, 12), 10, 2), 45))
    assert only(o, "bridge.long").measure["span_mm"] == pytest.approx(12, abs=0.5)


# ---------------------------------------------------------------------------- walls and layers
@pytest.mark.parametrize(("t", "code"), [(0.3, "wall.below_line"), (0.6, "wall.one_loop"), (2.0, None)])
def test_fin_thickness(t, code):
    o = run(box((0, 0, 0), (20, t, 10)))
    walls = [c for c in codes(o) if c.startswith("wall.")]
    assert walls == ([code] if code else [])
    assert o.areas["walls"].measures["thinnest_wall_mm_sampled"] == pytest.approx(t)


def test_slab_below_one_layer():
    assert only(run(box((0, 0, 0), (20, 20, 0.15))), "slab.below_layer").severity == "WARN"


def test_slab_off_the_layer_grid():
    f = only(run(box((0, 0, 0), (20, 20, 0.5))), "slab.off_grid")
    assert f.severity == "INFO" and "2 or 3 layers" in f.message


def test_no_wall_from_a_chord_across_a_sharp_edge():
    # A 60-degree knife edge with the floor and the slope cut into strips near the edge: a ray from the lowest slope
    # strip hits a floor strip 0.3 mm away, but the two faces are 60 degrees apart, not a wall pair.
    k = math.tan(math.radians(60))
    pts = [(0.0, 0.0), (0.2, 0.0), (1.0, 0.0), (5.0, 0.0), (10.0, 0.0), (10.0, 10 * k), (5.0, 5 * k), (1.0, k), (0.2, 0.2 * k)]
    o = run(prism(pts, 10))
    assert "wall.below_line" not in codes(o)


# ---------------------------------------------------------------------------- stability and adhesion
def test_mast_wants_a_brim_and_has_a_low_tip_margin():
    o = run(box((0, 0, 0), (5, 5, 100)))
    assert only(o, "brim.suggest").severity == "WARN"
    assert only(o, "tip.low_margin").severity == "INFO"


def test_long_narrow_contact_wants_a_brim():
    o = run(box((0, 0, 0), (120, 10, 4)))
    assert "long" in only(o, "brim.suggest").message


def test_brim_in_the_file_silences_the_suggestion():
    lim = pa.Limits.default().from_settings({"brim_type": "outer_only"}, "file")
    assert "brim.suggest" not in codes(run(box((0, 0, 0), (5, 5, 100)), lim))


def test_centre_of_mass_outside_the_contact():
    f = only(run(prism(GAMMA, 10)), "tip.com_outside")
    assert f.severity == "WARN" and f.measure["com_margin_mm"] < 0


def test_cube_on_an_edge_has_no_contact():
    cube = M.transform(box((0, 0, 0), (20, 20, 20)), M.euler_matrix(45, 0, 0))
    lo, _ = M.bbox(cube)
    rep = pa.analyse([pa.Subject("edge", M.translate(cube, (0, 0, -lo[2])), "posed")], pa.Limits.default())
    assert only(rep.objects[0], "contact.none").severity == "FAIL" and rep.exit_code == 1


# ---------------------------------------------------------------------------- fit
@pytest.fixture
def x2d(resolver) -> pa.Limits:
    return pa.Limits.default().with_machine(presets.machine_facts(resolver.resolve("machine", MACHINE)), f"preset {MACHINE}")


def test_too_long_for_the_bed(x2d):
    assert only(run(box((0, 0, 0), (300, 10, 10)), x2d), "fit.bed").severity == "FAIL"


@pytest.mark.parametrize(
    ("size", "extruder", "fails"),
    [((240, 10, 10), 2, True), ((240, 10, 10), 1, False), ((10, 10, 322), 1, True), ((10, 10, 322), 2, False)],
)
def test_extruder_reach(x2d, size, extruder, fails):
    # Test X2D: extruder 1 x 0..256, z 320; extruder 2 x 20.5..256 (235.5 wide), z 325
    o = pa.analyse([pa.Subject("p", box((0, 0, 0), size), "posed", extruder)], x2d).objects[0]
    assert ("fit.extruder_reach" in codes(o)) is fails
    assert "fit.bed" not in codes(o)


def test_explicit_brim_past_the_reach_is_info(x2d):
    lim = x2d.from_settings({"brim_type": "outer_only", "brim_width": "5"}, "file")
    assert only(run(box((0, 0, 0), (250, 30, 10)), lim), "fit.brim_edge").severity == "INFO"


# ---------------------------------------------------------------------------- limits
def test_settings_override_the_defaults_with_their_source():
    lim = pa.Limits.default().from_settings({"support_threshold_angle": "40", "max_bridge_length": ["10"], "layer_height": "0.16"}, "file")
    assert lim.overhang_deg == pa.Limit(50.0, "file")  # 40 from horizontal is 50 from vertical
    assert lim.max_bridge_mm == pa.Limit(10.0, "file") and lim.layer_height_mm == pa.Limit(0.16, "file")
    assert lim.outer_line_mm == pa.Limit(0.42, "default")  # an unset key keeps its default


def test_defaults_match_the_x2d_preset_library():
    try:
        proc = presets.PresetResolver().resolve("process", "0.20mm Standard @BBL X2D")
    except PresetError as e:
        pytest.skip(f"no Bambu Studio preset library here: {e}")
    lib = pa.Limits.default().from_settings(proc, "preset")
    d = pa.Limits.default()
    for attr in (
        "overhang_deg",
        "max_bridge_mm",
        "layer_height_mm",
        "initial_layer_mm",
        "outer_line_mm",
        "inner_line_mm",
        "wall_generator",
    ):
        assert getattr(lib, attr).value == getattr(d, attr).value, attr


# ---------------------------------------------------------------------------- evidence
def test_every_finding_has_evidence():
    shapes = [
        prism(CANTILEVER, 10),
        lintel(8),
        lintel(30),
        box((0, 0, 0), (20, 0.3, 10)),
        box((0, 0, 0), (5, 5, 100)),
        without(box((0, 0, 0), (9, 9, 9)), 0),
    ]
    seen = [f for s in shapes for f in run(s).findings]
    assert seen and all(f.evidence in findings.EVIDENCE for f in seen)
    assert all(f.severity != "FAIL" or f.evidence == "measured" for f in seen)


@pytest.mark.parametrize("evidence", ["external", "heuristic", "observed"])
def test_only_measured_evidence_can_fail(evidence):
    with pytest.raises(ValueError, match="cannot carry FAIL"):
        findings.Finding("x.y", "FAIL", "mesh", "m", evidence)
    findings.Finding("x.y", "WARN", "mesh", "m", evidence)  # WARN is the ceiling


def test_unknown_evidence_is_refused():
    with pytest.raises(ValueError, match="evidence"):
        findings.Finding("x.y", "INFO", "mesh", "m", "source")


# ---------------------------------------------------------------------------- 3MF
def test_3mf_objects_in_plate_frame(tmp_path):
    rep = Bambu().printability(make_geo_3mf(tmp_path / "g.3mf"))
    assert [o.name for o in rep.objects] == ["body", "label"]
    body = rep.objects[0]
    assert body.frame == "plate" and body.size_mm == pytest.approx([30, 20, 10])
    assert rep.limits.machine is not None and rep.limits.machine_source == "file"
    assert rep.objects[1].extruder == 2  # the label is filament 2, mapped to extruder 2


def test_3mf_threshold_from_the_file(tmp_path):
    rep = Bambu().printability(make_geo_3mf(tmp_path / "g.3mf", support_threshold_angle="45"), objects=["body"])
    assert rep.to_json()["limits"]["overhang_deg_from_vertical"] == {"value": 45.0, "source": "file"}


# ---------------------------------------------------------------------------- CLI
def test_cli_json_is_one_document_and_exit_0(tmp_path, capsys):
    stl = M.write_stl(tmp_path / "cube.stl", box((0, 0, 0), (20, 20, 20)))
    code = cli.main(["--json", "printability", str(stl)])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0 and doc["schema"] == 1 and doc["kind"] == "stl" and doc["objects"][0]["pose"]["from"] == "as-is"


def test_cli_exit_1_on_a_fail(tmp_path, capsys):
    stl = M.write_stl(tmp_path / "cube.stl", box((0, 0, 0), (20, 20, 20)))
    code = cli.main(["printability", str(stl), "--rotate", "45,0,0"])
    out = capsys.readouterr().out
    assert code == 1 and "FAIL contact.none" in out and "printability: 1 FAIL" in out


def test_cli_lay_on_face_poses_the_part(tmp_path, capsys):
    stl = M.write_stl(tmp_path / "plank.stl", box((0, 0, 0), (5, 5, 100)))
    code = cli.main(["--json", "printability", str(stl), "--lay-on-face", "1,0,0"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0 and doc["objects"][0]["size_mm"][2] == pytest.approx(5)


def test_cli_printer_preset_runs_the_fit(tmp_path, capsys):
    stl = M.write_stl(tmp_path / "rail.stl", box((0, 0, 0), (300, 10, 10)))
    code = cli.main(["--json", "printability", str(stl), "--printer", MACHINE])
    doc = json.loads(capsys.readouterr().out)
    assert code == 1 and any(f["code"] == "fit.bed" for f in doc["objects"][0]["findings"])


def test_cli_both_pose_flags_are_usage(tmp_path):
    stl = M.write_stl(tmp_path / "c.stl", box((0, 0, 0), (5, 5, 5)))
    with pytest.raises(SystemExit) as e:
        cli.main(["printability", str(stl), "--rotate", "1,0,0", "--lay-on-face", "0,0,-1"])
    assert e.value.code == 2


@pytest.mark.parametrize("extra", [["--plate", "1"], ["--object", "x"]])
def test_cli_3mf_flags_on_an_stl_are_usage(tmp_path, capsys, extra):
    stl = M.write_stl(tmp_path / "c.stl", box((0, 0, 0), (5, 5, 5)))
    assert cli.main(["--json", "printability", str(stl), *extra]) == 2
    assert "error" in json.loads(capsys.readouterr().out)


def test_cli_unknown_object_is_usage(tmp_path, capsys):
    assert cli.main(["--json", "printability", str(make_geo_3mf(tmp_path / "g.3mf")), "--object", "nope"]) == 2
    assert "no object named" in json.loads(capsys.readouterr().out)["error"]


def test_cli_missing_file_is_environment(tmp_path, capsys):
    assert cli.main(["--json", "printability", str(tmp_path / "gone.stl")]) == 3


def test_cli_unknown_preset_is_environment(tmp_path, capsys):
    stl = M.write_stl(tmp_path / "c.stl", box((0, 0, 0), (5, 5, 5)))
    assert cli.main(["--json", "printability", str(stl), "--process", "no such process"]) == 3


def test_cli_text_lists_what_it_did_not_check(tmp_path, capsys):
    stl = M.write_stl(tmp_path / "cube.stl", box((0, 0, 0), (20, 20, 20)))
    cli.main(["printability", str(stl)])
    out = capsys.readouterr().out
    assert "not checked: support volume" in out and "pose as modelled" in out
