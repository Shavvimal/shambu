"""Print-by-object rules, orientation in the pipeline, sweep variants and the new CLI commands. Offline."""

import json
import math
import zipfile

import pytest

from conftest import MACHINE, PROCESS, FakeStudio, mesh
from shambu import cli
from shambu import layout as L
from shambu import mesh as M
from shambu import sweep as S
from shambu.build import Pipeline
from shambu.errors import BuildError
from shambu.facade import Bambu
from shambu.spec import Spec

FACTS = {
    "bed": {"x": (0.0, 256.0), "y": (0.0, 256.0)},
    "z_max": 261.0,
    "extruders": [
        {"index": 1, "x": (0.0, 256.0), "y": (0.0, 256.0), "z_max": 261.0, "type": "Direct Drive", "nozzle": "0.4"},
        {"index": 2, "x": (20.5, 256.0), "y": (0.0, 256.0), "z_max": 256.0, "type": "Bowden", "nozzle": "0.4"},
    ],
    "clearance": {"radius": 60.0, "height_to_rod": 32.8, "height_to_lid": 133.0, "dist_to_rod": 33.0, "nozzle_height": 4.0},
}


def part(key, w=20.0, h=20.0, z=10.0, x0=0.0, y0=0.0, filament=1):
    return L.Placed(key, mesh(f"{key}.stl", (w, h, z)), filament, x0, y0)


def seq_failures(placed):
    return [c.text for c in L.SequentialClearance().check(placed, L.Context(FACTS, by_object=True)) if not c.ok]


# ---------------------------------------------------------------------------- the rule
def test_rule_is_silent_by_layer():
    assert list(L.SequentialClearance().check([part("a"), part("b", x0=25)], L.Context(FACTS))) == []


def test_gap_needs_the_clearance_radius():
    assert seq_failures([part("a", x0=2), part("b", x0=82)]) == []  # 20 wide at 2 ends at 22; 82 - 22 = 60
    fails = seq_failures([part("a", x0=2), part("b", x0=81)])
    assert len(fails) == 1 and "59.0 mm apart" in fails[0] and "60.0 mm" in fails[0]


def test_short_objects_need_only_the_nozzle_gap():
    assert seq_failures([part("a", z=3, x0=2), part("b", z=3.9, x0=27)]) == []  # 5 mm apart, all under 4 mm tall
    assert "4.0 mm" in seq_failures([part("a", z=3, x0=2), part("b", z=3.9, x0=25)])[0]


def test_tall_object_before_a_neighbour_in_its_y_band_fails():
    tall_first = [part("t", z=40, x0=2, y0=2), part("s", z=10, x0=82, y0=2)]
    fails = seq_failures(tall_first)
    assert len(fails) == 1 and fails[0].startswith("t is 40.0 mm tall") and "gantry rods" in fails[0]
    assert seq_failures(list(reversed(tall_first))) == []  # tall one last: fine
    # a later object 40 mm away in Y (and 60 in X) is outside the band (33 mm)
    assert seq_failures([part("t", z=40, x0=2, y0=2), part("s", z=10, x0=82, y0=62)]) == []
    fails = seq_failures([part("t", z=40, x0=2, y0=2), part("s", z=10, x0=82, y0=52)])  # 30 mm Y gap: in the band
    assert len(fails) == 1 and "gantry rods" in fails[0]


def test_lid_limit_applies_to_all_but_the_last():
    assert "133.0 mm" in seq_failures([part("t", z=140, x0=2, y0=2), part("s", z=10, x0=2, y0=100)])[0]
    assert seq_failures([part("s", z=10, x0=2, y0=100), part("t", z=200, x0=2, y0=2)]) == []


# ---------------------------------------------------------------------------- the packer
def test_packer_keeps_order_and_isolates_tall_rows():
    ps = [part("a", z=10), part("tall", z=40), part("b", z=10), part("c", z=10)]
    plate = L.ShelfPacker(gap=60, keep_order=True, solo_row_above=32.8).place(ps)[0]
    assert [p.key for p in plate] == ["a", "tall", "b", "c"]
    assert plate[0].y0 == plate[1].y0  # a and tall share a row: a prints first, so that is allowed
    assert plate[2].y0 == plate[1].y0 + 20 + 60  # b starts a new row after the tall one
    assert plate[3].y0 == plate[2].y0 and plate[3].x0 == plate[2].x1 + 60
    assert seq_failures(plate) == []


# ---------------------------------------------------------------------------- pipeline
def spec_json(tmp_path, stls, **plate_extra):
    return {
        "name": "s",
        "outdir": str(tmp_path / "out"),
        "printer": MACHINE,
        "process": {"base": PROCESS},
        "filaments": [{"preset": "Test PLA", "colour": "#111111"}, {"preset": "Test PLA", "colour": "#222222"}],
        "plates": [
            {
                "name": "p",
                "parts": [{"path": str(stls["body"]), "filament": 1}, {"path": str(stls["base"]), "filament": 2, "name": "second"}],
                **plate_extra,
            }
        ],
    }


def test_by_object_plate_params_order_and_no_reserve(tmp_path, stls, resolver):
    studio = FakeStudio(sizes={"body.stl": (20, 20, 10), "base.stl": (20, 20, 40)})
    spec = Spec.from_json(spec_json(tmp_path, stls, print_sequence="by object"))
    plan = Pipeline(studio, resolver).plan(spec)
    assert plan.ok, (plan.problems, [c.text for p in plan.plates for c in p.checks if not c.ok])
    plate = plan.assemble.plates[0]
    assert plate.plate_params["print_sequence"] == "by object"
    assert plan.plates[0].print_sequence == "by object"
    keys = [x.key for x in plan.plates[0].parts]
    assert keys == ["body", "second"]  # filament 1 first, then filament 2
    a, b = (x.placed for x in plan.plates[0].parts)
    assert b.x0 - a.x1 >= 60
    assert any("no prime tower" in w for w in plan.warnings)
    assert any(c.text.startswith("print-by-object") and c.ok for c in plan.plates[0].checks)


def test_by_object_rejects_smooth_timelapse(tmp_path, stls, resolver):
    d = spec_json(tmp_path, stls, print_sequence="by object")
    d["process"]["set"] = {"timelapse_type": "1"}
    plan = Pipeline(FakeStudio(), resolver).plan(Spec.from_json(d))
    assert any("Smooth timelapse" in p for p in plan.problems)


def test_bad_print_sequence_is_a_spec_error(tmp_path, stls):
    errs = Spec.from_json(spec_json(tmp_path, stls, print_sequence="sideways")).validate()
    assert any("print_sequence" in e for e in errs)


def test_orientation_writes_a_rotated_copy(tmp_path, resolver):
    stl = M.write_stl(tmp_path / "slab.stl", [((0, 0, 0), (30, 0, 0), (30, 10, 0)), ((0, 0, 0), (30, 10, 0), (0, 10, 0))] + box_facets())
    d = {
        "name": "o",
        "outdir": str(tmp_path / "out"),
        "printer": MACHINE,
        "process": {"base": PROCESS},
        "filaments": [{"preset": "Test PLA"}],
        "plates": [{"name": "p", "parts": [{"path": str(stl), "lay_on_face": [1, 0, 0]}]}],
    }
    spec = Spec.from_json(d)
    assert spec.plates[0].parts[0].orientation.lay_on_face == (1.0, 0.0, 0.0)
    assert spec.to_json()["plates"][0]["parts"][0]["lay_on_face"] == [1.0, 0.0, 0.0]
    studio = FakeStudio()
    plan = Pipeline(studio, resolver).plan(spec)
    assert plan.ok, plan.problems
    used = plan.plates[0].parts[0].files[0][0]
    assert used.parent.name == "oriented" and used.name.startswith("slab__face")
    assert used.is_file()
    lo, hi = M.bbox(M.read_stl(used))
    assert lo == pytest.approx((0, 0, 0))
    assert hi[2] == pytest.approx(30)  # the 30 mm long edge now stands up
    assert plan.plates[0].parts[0].key == "slab"  # the object keeps the original name


def box_facets():
    from test_mesh import box

    return box((0, 0, 0), (30, 10, 5))


# ---------------------------------------------------------------------------- sweep
BASE = {
    "name": "n",
    "outdir": "/tmp/x",
    "printer": "P",
    "process": {"base": "B", "set": {"wall_loops": "2"}},
    "filaments": [{"preset": "F"}],
    "plates": [{"name": "jaws", "parts": [{"path": "/a/jaw.stl", "set": {"k": "1"}}, {"path": "/a/other.stl", "name": "o"}]}],
}


def test_apply_variant_process_plates_parts_merge():
    v = {
        "name": "v",
        "process": {"set": {"layer_height": "0.28"}, "recipes": ["coarse-fast"]},
        "plates": {"jaws": {"print_sequence": "by object", "parts": {"o": {"count": 2}}}},
        "parts": {"jaw": {"lay_on_face": [0, 0, 1], "set": {"enable_support": "0"}}},
        "merge": {"bed_type": "Cool Plate"},
    }
    d = S.apply_variant(BASE, v)
    assert BASE["process"]["set"] == {"wall_loops": "2"}  # untouched
    assert d["process"]["set"] == {"wall_loops": "2", "layer_height": "0.28"}
    assert d["process"]["recipes"] == ["coarse-fast"]
    pl = d["plates"][0]
    assert pl["print_sequence"] == "by object"
    assert pl["parts"][0] == {"path": "/a/jaw.stl", "set": {"k": "1", "enable_support": "0"}, "lay_on_face": [0, 0, 1]}
    assert pl["parts"][1]["count"] == 2
    assert d["bed_type"] == "Cool Plate"


def test_apply_variant_unknown_targets():
    with pytest.raises(BuildError):
        S.apply_variant(BASE, {"name": "v", "plates": {"nope": {}}})
    with pytest.raises(BuildError):
        S.apply_variant(BASE, {"name": "v", "parts": {"nope": {}}})


def test_merge_patch_deletes_with_none():
    assert S.merge_patch({"a": 1, "b": {"c": 2, "d": 3}}, {"a": None, "b": {"c": 9}}) == {"b": {"c": 9, "d": 3}}


def test_by_object_variant_covers_every_plate():
    d = {**BASE, "plates": [{"name": "a", "parts": []}, {"name": "b", "parts": []}]}
    assert S.by_object_variant(d)["plates"] == {"1": {"print_sequence": "by object"}, "2": {"print_sequence": "by object"}}


def test_orientation_variants_from_candidates(tmp_path):
    from test_mesh import table

    stl = M.write_stl(tmp_path / "table.stl", table())
    d = {**BASE, "plates": [{"name": "p", "parts": [{"path": str(stl)}]}]}
    vs = S.orientation_variants(d, "table", top=3)
    assert len(vs) == 3
    assert vs[0]["parts"]["table"]["lay_on_face"] == pytest.approx([0, 0, 1])
    assert vs[0]["orientation"]["shadow_mm2"] == 0
    with pytest.raises(BuildError):
        S.orientation_variants(d, "nope")


class _Result:
    def __init__(self, ok, secs, grams, problems=()):
        self.run = None
        self._j = {
            "ok": ok,
            "project": "/p.gcode.3mf",
            "problems": list(problems),
            "verify_problems": [],
            "estimates": [
                {
                    "plate": 1,
                    "prediction_s": secs,
                    "weight_g": grams,
                    "filaments": [{"id": "1", "used_g": grams}],
                    "features": {"support_g": 1.5, "waste_g": 2.0, "filament_changes": 3},
                }
            ]
            if ok
            else [],
            "report": "/r.md",
        }

    def to_json(self):
        return self._j


def test_sweep_run_and_table(tmp_path):
    seen = []

    def build(spec, do_slice):
        seen.append((spec.name, str(spec.outdir), do_slice, spec.plates[0].print_sequence))
        secs = 1000 if spec.plates[0].by_object else 2000
        return _Result(True, secs, 10.0)

    d = {**BASE, "outdir": str(tmp_path)}
    res = S.run(d, [S.by_object_variant(d)], build)
    assert [r.name for r in res] == ["baseline", "by-object"]
    assert seen[0][1].endswith("/sweep/baseline") and seen[1][1].endswith("/sweep/by-object")
    assert seen[1][3] == "by object"
    lines = S.table(res)
    assert lines[1].startswith("by-object") and "best" in lines[1]
    assert lines[2].startswith("baseline") and "+0h16" in lines[2]
    assert res[0].to_json()["support_g"] == 1.5 and res[0].to_json()["waste_g"] == 2.0 and res[0].to_json()["filament_changes"] == 3
    assert "1.5" in lines[1] and "2.0" in lines[1] and "    3" in lines[1]


def test_sweep_failed_variant_sinks(tmp_path):
    res = S.run({**BASE, "outdir": str(tmp_path)}, [{"name": "bad", "parts": {"nope": {}}}], lambda s, d: _Result(True, 10, 1.0))
    assert res[1].ok is False and "no part" in res[1].problems[0]
    assert S.table(res)[-1].startswith("bad") and "FAIL" in S.table(res)[-1]


# ---------------------------------------------------------------------------- CLI
@pytest.fixture(autouse=True)
def fake_app(monkeypatch, resolver):
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=FakeStudio(), resolver=resolver))


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_cli_orient_rotate_split(capsys, tmp_path):
    from test_mesh import table

    stl = M.write_stl(tmp_path / "table.stl", table())
    code, out, _ = run(capsys, "--json", "orient", str(stl), "--write", str(tmp_path / "o"))
    assert code == 0
    j = json.loads(out)
    assert j["candidates"][0]["shadow_mm2"] == 0 and j["written"].endswith(".stl")
    code, out, _ = run(capsys, "orient", str(stl))
    assert code == 0 and "orientation" in out and "lay_on_face" in out
    code, out, _ = run(capsys, "--json", "rotate", str(stl), "-o", str(tmp_path / "r"), "--rotate", "90,0,0")
    assert code == 0 and json.loads(out)["size"][2] == pytest.approx(20)
    assert run(capsys, "rotate", str(stl), "-o", str(tmp_path / "r"))[0] == 2
    two = M.write_stl(tmp_path / "two.stl", box_facets() + [tuple((x + 100, y, z) for x, y, z in f) for f in box_facets()])
    code, out, _ = run(capsys, "--json", "split", str(two), "-o", str(tmp_path / "s"))
    assert code == 0 and json.loads(out)["bodies"] == 2


def test_cli_patch_print_sequence(capsys, tmp_path):
    from conftest import make_3mf

    f = make_3mf(tmp_path / "p.3mf")
    code, out, _ = run(capsys, "patch", str(f), "--print-sequence", "1:by object", "--print-sequence", "by_object")
    assert code == 0, out
    code, out, _ = run(capsys, "--json", "inspect", str(f))
    j = json.loads(out)
    assert j["plates"][0]["print_sequence"] == "by object"


# ---------------------------------------------------------------------------- G-code feature usage
GCODE = """; HEADER_BLOCK_START
; filament_density: 1.26,1.24
; filament_diameter: 1.75,1.75
; HEADER_BLOCK_END
M83
T0
; FEATURE: Outer wall
G1 X1 Y1 E10
G1 X2 Y2 E-0.8
G1 X2 Y2 E0.8
G2 X3 Y3 I1 J1 E5
; FEATURE: Support
G1 X4 E100
; FEATURE: Support interface
G1 X5 E50
; FEATURE: Prime tower
G1 X6 E20
; FLUSH_START
G1 E30
; FLUSH_END
T1
; FEATURE: Sparse infill
G1 X7 E1000
T255
G1 X8 E1
"""


def test_feature_usage_from_gcode(tmp_path):
    from conftest import make_3mf
    from shambu.threemf import Project

    f = make_3mf(tmp_path / "s.3mf")
    with zipfile.ZipFile(f, "a") as z:
        z.writestr("Metadata/plate_1.gcode", GCODE)
    ft = Project(str(f)).feature_usage(1)
    area = math.pi * 0.875**2
    assert ft["model_g"] == pytest.approx(15 * area * 1.26 / 1000 + 1001 * area * 1.24 / 1000, abs=0.01)
    assert ft["support_g"] == pytest.approx(150 * area * 1.26 / 1000, abs=0.01)
    assert ft["tower_g"] == pytest.approx(20 * area * 1.26 / 1000, abs=0.01)
    assert ft["flush_g"] == pytest.approx(30 * area * 1.26 / 1000, abs=0.01)
    assert ft["waste_g"] == pytest.approx(ft["tower_g"] + ft["flush_g"], abs=0.01)
    assert ft["filament_changes"] == 1  # the T0 line repeats the loaded tool; T0 -> T1 counts; T255 is not a tool
    assert set(ft["per_filament"]) == {"1", "2"}
    assert Project(str(f)).feature_usage(2) is None


def test_cli_sweep_json_one_document(capsys, tmp_path, stls, resolver, monkeypatch):
    from conftest import PROCESS_NAME, make_3mf

    fixture = make_3mf(tmp_path / "cli-export.3mf", object_names=("body_1", "assemble_1"), change_filament_gcode="M600")
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=FakeStudio(fixture=fixture), resolver=resolver))
    spec = {
        "name": "plate-t",
        "outdir": str(tmp_path / "out"),
        "printer": MACHINE,
        "process": {"base": PROCESS, "name": PROCESS_NAME},
        "filaments": [{"preset": "Test PLA", "colour": "#000000"}, {"preset": "Test PLA", "colour": "#FFFFFF"}],
        "plates": [
            {
                "name": "p",
                "parts": [
                    {"path": str(stls["body"]), "name": "body"},
                    {"name": "label", "parts": [{"path": str(stls["base"])}, {"path": str(stls["text"]), "filament": 2}]},
                ],
            }
        ],
    }
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    code, out, _ = run(capsys, "--json", "sweep", str(tmp_path / "spec.json"), "--by-object", "--no-slice")
    j = json.loads(out)  # exactly one document
    assert [r["name"] for r in j] == ["baseline", "by-object"]
    assert j[0]["ok"] is True and j[0]["project"].endswith("/sweep/baseline/plate-t.3mf")
    # the fake export cannot write the plate's print_sequence, so verify flags it: the sweep reports it, not hides it,
    # and one failed variant makes the whole sweep exit 1 (a failed check), as `build` would
    assert j[1]["ok"] is False and any("print_sequence" in p for p in j[1]["problems"])
    assert code == 1
    code, out, _ = run(capsys, "sweep", str(tmp_path / "spec.json"), "--no-baseline")
    assert code == 2 and "give --variants" in out


# ---------------------------------------------------------------------------- review follow-ups (Codex, 2026-10-13)
def test_oriented_copies_keep_a_merged_object_aligned(tmp_path):
    from test_mesh import box

    a = M.write_stl(tmp_path / "a.stl", box((0, 0, 0), (10, 10, 10)))
    b = M.write_stl(tmp_path / "b.stl", box((0, 0, 10), (10, 10, 12)))  # a lid on top of a
    o = M.Orientation(rotate=(90, 0, 0))
    ta, tb = M.oriented_copies([a, b], o, tmp_path / "o")
    la, _ = M.bbox(M.read_stl(ta))
    lb, hb = M.bbox(M.read_stl(tb))
    assert min(la[2], lb[2]) == pytest.approx(0) and min(la[0], lb[0]) == pytest.approx(0)
    assert hb[1] - lb[1] == pytest.approx(2) and hb[1] == pytest.approx(la[1], abs=1e-6)  # the lid still touches the box
    assert ta.name != tb.name and ta.name.endswith(".stl")


def test_oriented_copy_names_never_collide(tmp_path):
    from test_mesh import box

    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    a = M.write_stl(tmp_path / "x" / "part.stl", box((0, 0, 0), (1, 1, 1)))
    b = M.write_stl(tmp_path / "y" / "part.stl", box((0, 0, 0), (2, 2, 2)))
    o = M.Orientation(rotate=(90, 0, 0))
    pa = M.oriented_copy(a, o, tmp_path / "o")
    pb = M.oriented_copy(b, o, tmp_path / "o")
    assert pa != pb
    p12 = M.oriented_copy(a, M.Orientation(rotate=(12, 0, 0)), tmp_path / "o")
    p1_2 = M.oriented_copy(a, M.Orientation(rotate=(1.2, 0, 0)), tmp_path / "o")
    assert p12 != p1_2


def test_process_level_by_object_is_honoured(tmp_path, stls, resolver):
    d = spec_json(tmp_path, stls)
    d["process"]["set"] = {"print_sequence": "by object"}
    studio = FakeStudio(sizes={"body.stl": (20, 20, 10), "base.stl": (20, 20, 40)})
    plan = Pipeline(studio, resolver).plan(Spec.from_json(d))
    assert plan.plates[0].print_sequence == "by object"
    assert plan.assemble.plates[0].plate_params["print_sequence"] == "by object"
    a, b = (x.placed for x in plan.plates[0].parts)
    assert b.x0 - a.x1 >= 60
    # a plate that says "by layer" against a by-object process writes its own value
    d["plates"][0]["print_sequence"] = "by layer"
    plan = Pipeline(studio, resolver).plan(Spec.from_json(d))
    assert plan.assemble.plates[0].plate_params["print_sequence"] == "by layer"
    assert plan.plates[0].print_sequence == "by layer"


def test_recipe_keys_pass_the_unknown_key_check(tmp_path, stls, resolver):
    from shambu import recipes as R

    d = spec_json(tmp_path, stls)
    d["plates"][0]["parts"][0]["recipes"] = ["scarf-seam", "counterbore-bridging", "minimal-supports", "precise-fit", "show-walls"]
    plan = Pipeline(FakeStudio(), resolver).plan(Spec.from_json(d))
    assert not [p for p in plan.problems if "unknown setting key" in p], plan.problems
    assert "seam_slope_conditional" in R.all_keys()


def test_single_two_filament_object_keeps_the_tower_reserve(tmp_path, stls, resolver):
    d = spec_json(tmp_path, stls, print_sequence="by object")
    d["plates"][0]["parts"] = [
        {"name": "label", "parts": [{"path": str(stls["base"])}, {"path": str(stls["text"]), "filament": 2}], "at": [220, 220]}
    ]
    plan = Pipeline(FakeStudio(), resolver).plan(Spec.from_json(d))
    assert any("prime-tower reserve" in c.text for c in plan.plates[0].checks if not c.ok)
    assert not any("no prime tower" in w for w in plan.warnings)
    assert not any(c.text.startswith("print-by-object") for c in plan.plates[0].checks)  # one object: no sequential rules


def test_short_objects_pack_with_the_nozzle_gap(tmp_path, stls, resolver):
    d = spec_json(tmp_path, stls, print_sequence="by object")
    d["plates"][0]["parts"] = [{"path": str(stls["body"]), "count": 16, "recipes": ["flat-no-brim"]}]
    d["layout"] = {"gap": 2, "edge": 2}
    studio = FakeStudio(sizes={"body.stl": (20, 20, 3)})
    plan = Pipeline(studio, resolver).plan(Spec.from_json(d))
    assert plan.ok, (plan.problems, [c.text for c in plan.plates[0].checks if not c.ok])
    xs = sorted({x.placed.x0 for x in plan.plates[0].parts})
    assert xs[1] - xs[0] == pytest.approx(24)  # 20 wide + the 4 mm nozzle gap, not 60


def test_sequential_gap_adds_a_per_object_skirt():
    ctx = L.Context(FACTS, by_object=True, skirt_extra=10.0)
    assert ctx.sequential_gap(False) == pytest.approx(2 * (30 + 5))
    assert ctx.sequential_gap(True) == pytest.approx(2 * 10)  # max(2 + 5, 10)
    assert L.Context(FACTS, by_object=True).sequential_gap(False) == 60


def test_feature_usage_repays_retraction_across_features_and_absolute_mode(tmp_path):
    from conftest import make_3mf
    from shambu.threemf import Project

    gcode = """; filament_density: 1.26
; filament_diameter: 1.75
M83
; FEATURE: Outer wall
G1 X1 E10
G1 X2 E-0.8
   
; FEATURE: Support
G1 X3 E0.8
G1 X4 E5
M82
G92 E0
G1 X5 E10
G1 X6 E20
"""
    f = make_3mf(tmp_path / "s.3mf")
    with zipfile.ZipFile(f, "a") as z:
        z.writestr("Metadata/plate_1.gcode", gcode)
    ft = Project(str(f)).feature_usage(1)
    area = math.pi * 0.875**2 * 1.26 / 1000
    assert ft["model_g"] == pytest.approx(10 * area, abs=0.006)  # the deretraction is a repayment, not model material (2 dp)
    assert ft["support_g"] == pytest.approx((5 + 20) * area, abs=0.006)  # absolute: 10 then 20 = 20 mm, not 30


def test_sweep_names_are_checked():
    with pytest.raises(BuildError):
        S.check_names([{"name": "../escape"}])
    with pytest.raises(BuildError):
        S.check_names([{"name": "baseline"}])
    with pytest.raises(BuildError):
        S.check_names([{"name": "a"}, {"name": "a"}])
    S.check_names([{"name": "a.b-c_1"}])
    with pytest.raises(BuildError):
        S.run(BASE, [{"name": "x/y"}], lambda s, d: _Result(True, 1, 1.0))


def test_variant_plate_name_beats_index_and_nested_parts_must_exist():
    d = {**BASE, "plates": [{"name": "first", "parts": [{"path": "/a/p.stl"}]}, {"name": "1", "parts": [{"path": "/a/q.stl"}]}]}
    out = S.apply_variant(d, {"name": "v", "plates": {"1": {"print_sequence": "by object"}}})
    assert out["plates"][1]["print_sequence"] == "by object" and "print_sequence" not in out["plates"][0]
    with pytest.raises(BuildError):
        S.apply_variant(d, {"name": "v", "plates": {"first": {"parts": {"nope": {"count": 2}}}}})


def test_sweep_records_any_bambu_error_and_continues():
    from shambu.errors import PresetError

    def build(spec, do_slice):
        if spec.plates[0].by_object:
            raise PresetError("no such preset")
        return _Result(True, 10, 1.0)

    res = S.run(BASE, [S.by_object_variant(BASE), {"name": "ok2"}], build)
    assert [r.ok for r in res] == [True, False, True] and "no such preset" in res[1].problems[0]


def test_print_sequence_patch_integrity(tmp_path):
    from conftest import make_3mf
    from shambu import patches as X
    from shambu.threemf import Project

    f = make_3mf(tmp_path / "p.3mf", sliced=True)
    assert Project(str(f)).summary()["sliced"] is True
    X.PatchSet().add(X.SetPrintSequence("by object", 1)).apply(str(f))
    s1 = Project(str(f)).summary()
    assert s1["plates"][0]["print_sequence"] == "by object" and s1["sliced"] is False  # a slicing setting drops the G-code
    X.PatchSet().add(X.SetPrintSequence("by object", 1)).apply(str(f))
    s2 = Project(str(f)).summary()
    assert {k: v for k, v in s1.items() if k != "file"} == {k: v for k, v in s2.items() if k != "file"}  # idempotent
    X.PatchSet().add(X.SetPrintSequence("by layer")).apply(str(f))  # project default also rewrites the plate override
    s3 = Project(str(f)).summary()
    assert s3["plates"][0]["print_sequence"] == "by layer"
    assert zipfile.ZipFile(f).testzip() is None
    with pytest.raises(Exception, match="by layer"):
        X.PatchSet().add(X.SetPrintSequence("sideways")).apply(str(f))


def test_cli_rotate_and_orient_error_contracts(capsys, tmp_path):
    from test_mesh import box

    stl = M.write_stl(tmp_path / "b.stl", box((0, 0, 0), (1, 1, 1)))
    code, out, _ = run(capsys, "--json", "rotate", str(stl), "-o", str(tmp_path / "r"), "--rotate", "a,0,0")
    assert code == 2 and "error" in json.loads(out)
    code, out, _ = run(capsys, "--json", "rotate", str(stl), "-o", str(tmp_path / "r"), "--rotate", "1,2")
    assert code == 2 and "error" in json.loads(out)
    code, out, _ = run(capsys, "--json", "orient", str(tmp_path / "missing.stl"))
    assert code == 3 and "error" in json.loads(out)


def test_orientation_variant_names_pass_the_name_check(tmp_path):
    from test_mesh import table

    stl = M.write_stl(tmp_path / "table.stl", table())
    d = {**BASE, "plates": [{"name": "p", "parts": [{"path": str(stl)}]}]}
    vs = S.orientation_variants(d, "table", top=12)
    S.check_names(vs)  # "+z face down" and "-x face down" become plain names
    assert any(v["name"].endswith("pz-face-down") for v in vs) and any(v["name"].endswith("mx-face-down") for v in vs)


def test_rotate_scale_and_metre_warning(capsys, tmp_path):
    from test_mesh import box

    tiny = M.write_stl(tmp_path / "tiny.stl", box((0, 0, 0), (0.02, 0.01, 0.005)))  # a 20 x 10 x 5 mm part in metres
    code, out, _ = run(capsys, "orient", str(tiny))
    assert code == 0 and "probably in metres" in out
    code, out, _ = run(capsys, "--json", "rotate", str(tiny), "-o", str(tmp_path / "r"), "--scale", "1000")
    assert code == 0 and json.loads(out)["size"] == pytest.approx([20, 10, 5])
    assert run(capsys, "rotate", str(tiny), "-o", str(tmp_path / "r"), "--scale", "0")[0] == 2
    assert M.looks_like_metres(M.read_stl(tiny)) and not M.looks_like_metres(box((0, 0, 0), (2, 1, 1)))


def test_copies_are_positioned_relative_to_the_first_copy(tmp_path, stls, resolver):
    """Studio clones the translated first copy for copies 2..n, so their pos is relative (reference 01 section 2.4)."""
    d = spec_json(tmp_path, stls)
    d["filaments"] = d["filaments"][:1]
    d["plates"][0]["parts"] = [{"path": str(stls["body"]), "count": 2, "recipes": ["flat-no-brim"]}]
    studio = FakeStudio(sizes={"body.stl": (72, 20, 12)}, los={"body.stl": (-34.0, 64.0, -38.0)})  # the gripper link's frame
    plan = Pipeline(studio, resolver).plan(Spec.from_json(d))
    assert plan.ok, plan.problems
    a, b = (x.placed for x in plan.plates[0].parts)
    assert (a.x0, a.y0) == (2.0, 2.0) and (b.x0, b.y0) == (79.0, 2.0)
    item = plan.assemble.plates[0].items[0]
    assert item.count == 2
    assert [tuple(v) for v in item.pos] == [(36.0, -62.0, 38.0), (77.0, 0.0, 0.0)]


def test_support_filament_is_pinned_on_a_two_filament_plate(tmp_path, stls, resolver):
    d = spec_json(tmp_path, stls)
    d["plates"][0]["parts"][0]["recipes"] = ["tree-supports"]  # filament 1, support filaments left at Studio's default 0
    d["plates"][0]["parts"][1]["set"] = {"enable_support": "1", "support_interface_filament": "1"}  # explicit value kept
    plan = Pipeline(FakeStudio(), resolver).plan(Spec.from_json(d))
    items = plan.assemble.plates[0].items
    assert items[0].print_params["support_filament"] == "1" and items[0].print_params["support_interface_filament"] == "1"
    assert items[1].print_params["support_filament"] == "2" and items[1].print_params["support_interface_filament"] == "1"
    assert any("support filament pinned" in w and "body:support_filament" in w for w in plan.warnings)
    # one filament: nothing to pin; no supports: nothing to pin
    d["filaments"] = d["filaments"][:1]
    d["plates"][0]["parts"][1]["filament"] = 1
    plan = Pipeline(FakeStudio(), resolver).plan(Spec.from_json(d))
    assert "support_filament" not in plan.assemble.plates[0].items[0].print_params
