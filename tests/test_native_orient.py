"""Native orientation (Studio's `--orient 1`, pass 1) and `--convert-unit`: the stdout parser, the argv order, the
spec value `"orientation": "auto"`, the pipeline path, `sweep --source studio`, the CLI flags, and live checks
against the installed Bambu Studio (skipped when it is absent)."""

import json
import math
from pathlib import Path

import pytest

from conftest import MACHINE, PROCESS, PROCESS_NAME, FakeStudio, requires_studio
from shambu import cli
from shambu import mesh as M
from shambu import sweep as S
from shambu.build import Pipeline
from shambu.errors import BuildError, StudioError
from shambu.facade import Bambu
from shambu.spec import Spec
from shambu.studio import OrientCandidate, StudioCLI, orient_command, parse_orient
from test_mesh import box

DATA = Path(__file__).parent / "data"
REPO = Path(__file__).parent.parent
SAMPLE = DATA / "orient_stdout.txt"


# ---------------------------------------------------------------------------- the stdout parser
def test_parse_saved_bracket_sample():
    tables = parse_orient(SAMPLE.read_text())
    assert len(tables) == 1
    t = tables[0]
    # 32 `orientation:` rows plus the `best:` row, which repeats one of them (live, Studio 02.08.02.61)
    assert len(t.candidates) == 32
    assert t.best is not None
    assert t.best.vector == (-1.0, 0.0, 0.0)
    assert t.best.unprintability == pytest.approx(0.6)
    assert t.best.overhang == pytest.approx(3790.4)
    assert t.best.costs() == pytest.approx(
        {
            "overhang": 3790.4,
            "bottom": 4314.0,
            "bottom_hull": 9010.0,
            "contour": 262.7,
            "area_laf": 485.1,
            "area_projected": 0.0,
            "unprintability": 0.6,
        }
    )
    u = [c.unprintability for c in t.candidates]
    assert u == sorted(u)
    assert t.candidates[0].vector == t.best.vector
    assert t.best.lay_on_face == (1.0, 0.0, 0.0)
    assert all(math.copysign(1, v) == 1 for v in t.best.lay_on_face if v == 0)  # no -0.0 leaks into a spec
    # spacing varies: `1.0000 0.0000 0.0000` and `-0.0000  1.0000  0.0000` both parse
    vecs = {c.vector for c in t.candidates}
    assert (1.0, 0.0, 0.0) in vecs and (0.0, 1.0, 0.0) in vecs and (0.0, 0.3661, -0.9306) in vecs


def test_parse_several_tables_and_skips_noise():
    text = (
        "[2026-10-13 16:01:29] [trace] Initializing StaticPrintConfigs\n"
        "                    overhang, bottom, bothull, contour, A_laf, A_prj, unprintability\n"
        "orientation:0.0000 0.0000 1.0000, cost:1.0,\t2.0,\t3.0,\t4.0,\t5.0,\t0.0,\t9.0\n"
        "orientation:1.0000 0.0000 0.0000, cost:1.0,\t2.0,\t3.0\n"  # truncated row: skipped
        "orientation:0.0000 1.0000 0.0000, cost:1.0,\t2.0,\t3.0,\t4.0,\t5.0,\t0.0,\t2.0\n"
        "best:0.000000 1.000000 0.000000, costs:1.0,\t2.0,\t3.0,\t4.0,\t5.0,\t0.0,\t2.0\n"
        "orientation:-1.0000 0.0000 0.0000, cost:1.0,\t2.0,\t3.0,\t4.0,\t5.0,\t0.0,\t7.0\n"
    )
    tables = parse_orient(text)
    assert len(tables) == 2
    assert [c.unprintability for c in tables[0].candidates] == [2.0, 9.0]
    assert tables[0].best is not None and tables[0].best.vector == (0.0, 1.0, 0.0)
    assert tables[1].best is None and tables[1].candidates[0].lay_on_face == (1.0, 0.0, 0.0)
    assert parse_orient("no table here\n") == []


def test_from_studio_vector_negates():
    o = M.from_studio_vector((-1.0, -0.0, 0.0))
    assert o.lay_on_face == (1.0, 0.0, 0.0) and o.rotate == (0.0, 0.0, 0.0) and not o.auto
    with pytest.raises(BuildError):
        M.from_studio_vector((1.0, 0.0))


# ---------------------------------------------------------------------------- argv order
def test_orient_command_argv_order(tmp_path):
    stl = tmp_path / "a.stl"
    args = orient_command([stl], tmp_path / "o", [tmp_path / "m.json", tmp_path / "p.json"]).args()
    out = str((tmp_path / "o").resolve())
    assert args[0] == "--load-settings" and args[1].endswith("m.json;" + str((tmp_path / "p.json").resolve()))
    assert args[2:] == ["--convert-unit", "--orient=1", "--export-stls", out, "--outputdir", out, str(stl.resolve())]
    assert args.index("--convert-unit") < args.index("--orient=1") < args.index("--export-stls")


def test_orient_command_variants(tmp_path):
    a = orient_command([tmp_path / "a.stl", tmp_path / "b.stl"], tmp_path, convert_unit=False).args()
    assert a[0] == "--orient=1" and "--convert-unit" not in a and a[-2:] == [str(tmp_path / "a.stl"), str(tmp_path / "b.stl")]
    n = orient_command([tmp_path / "a.stl"], tmp_path, orient=False).args()
    assert n[0] == "--convert-unit" and not any(x.startswith("--orient") for x in n)


# ---------------------------------------------------------------------------- StudioCLI.orient against a fake binary
FAKE_ORIENT = """#!/bin/sh
prev=""
for a in "$@"; do
  if [ "$prev" = "--export-stls" ]; then OUT="$a"; fi
  prev="$a"
  last="$a"
done
stem=$(basename "$last" .stl)
case "$FAKE_MODE" in
  ok) cp "$last" "$OUT/obj_1_$stem.stl"; cat "$FAKE_TABLE"; echo '{"return_code": 0, "error_string": "Success."}' > "$OUT/result.json" ;;
  missing) cat "$FAKE_TABLE" ;;
  fail) exit 234 ;;
esac
"""


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    b = tmp_path / "BambuStudio"
    b.write_text(FAKE_ORIENT)
    b.chmod(0o755)
    monkeypatch.setenv("FAKE_TABLE", str(SAMPLE))
    return StudioCLI(binary=b, scratch=tmp_path / "scratch")


def test_orient_runs_parses_and_maps_files(fake_cli, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "ok")
    src = M.write_stl(tmp_path / "part.stl", box((0, 0, 0), (10, 10, 10)))
    stale = tmp_path / "o" / "obj_1_part.stl"
    stale.parent.mkdir()
    stale.write_text("stale")
    r = fake_cli.orient([src], tmp_path / "o")
    assert r.files == [(tmp_path / "o" / "obj_1_part.stl").resolve()]
    assert r.files[0].read_bytes() == src.read_bytes()
    assert r.best is not None and r.best.vector == (-1.0, 0.0, 0.0) and len(r.candidates) == 32
    assert "--debug=1" in r.run.cmd and "--orient=1" in r.run.cmd


def test_orient_missing_export_or_failure_is_studio_error(fake_cli, tmp_path, monkeypatch):
    src = M.write_stl(tmp_path / "part.stl", box((0, 0, 0), (10, 10, 10)))
    monkeypatch.setenv("FAKE_MODE", "missing")
    with pytest.raises(StudioError, match="missing"):
        fake_cli.orient([src], tmp_path / "o")
    monkeypatch.setenv("FAKE_MODE", "fail")
    with pytest.raises(StudioError, match="orient failed: exit -22"):
        fake_cli.orient([src], tmp_path / "o")
    with pytest.raises(StudioError, match="not found"):
        fake_cli.orient([tmp_path / "nope.stl"], tmp_path / "o")


# ---------------------------------------------------------------------------- spec
def spec_dict(tmp_path, *parts) -> dict:
    return {
        "name": "auto",
        "outdir": str(tmp_path / "out"),
        "printer": MACHINE,
        "process": {"base": PROCESS, "name": PROCESS_NAME},
        "filaments": [{"preset": "Test PLA"}],
        "plates": [{"name": "p", "parts": list(parts)}],
    }


def test_spec_auto_round_trip(tmp_path):
    s = Spec.from_json(spec_dict(tmp_path, {"path": "/a.stl", "orientation": "auto"}))
    o = s.plates[0].parts[0].orientation
    assert o.auto and not o.is_identity and o.lay_on_face is None
    assert s.to_json()["plates"][0]["parts"][0]["orientation"] == "auto"
    assert Spec.from_json(s.to_json()) == s
    assert s.validate() == []
    assert "orientation" not in Spec.from_json(spec_dict(tmp_path, {"path": "/a.stl"})).to_json()["plates"][0]["parts"][0]


@pytest.mark.parametrize("extra", [{"lay_on_face": [1, 0, 0]}, {"rotate": [0, 0, 45]}])
def test_spec_auto_with_explicit_turn_is_rejected(tmp_path, extra):
    s = Spec.from_json(spec_dict(tmp_path, {"path": "/a.stl", "orientation": "auto", **extra}))
    assert any("orientation 'auto'" in e for e in s.validate())


def test_spec_unknown_orientation_value_raises(tmp_path):
    with pytest.raises(BuildError, match="only value is"):
        Spec.from_json(spec_dict(tmp_path, {"path": "/a.stl", "orientation": "best"}))


def test_oriented_copies_refuse_auto(tmp_path):
    src = M.write_stl(tmp_path / "a.stl", box((0, 0, 0), (1, 1, 1)))
    with pytest.raises(BuildError, match="auto"):
        M.oriented_copies([src], M.Orientation(auto=True), tmp_path)


# ---------------------------------------------------------------------------- pipeline
def test_pipeline_auto_part_uses_studio_export(tmp_path, resolver):
    src = M.write_stl(tmp_path / "block.stl", box((0, 0, 0), (30, 20, 10)))
    studio = FakeStudio()
    spec = Spec.from_json(spec_dict(tmp_path, {"path": str(src), "orientation": "auto"}))
    r = Pipeline(studio, resolver).build(spec, export=False)
    assert r.plan.problems == [], r.plan.problems
    calls = [c for c in studio.calls if c[0] == "orient"]
    assert len(calls) == 1
    paths, settings, convert = calls[0][1]
    assert paths == [src.resolve()] and convert is True
    workdir = r.plan.workdir
    assert [p.name for p in settings] == ["machine.json", "process.json"] and all(p.is_file() for p in settings)
    item = json.loads(r.inputs["assemble"].read_text())["plates"][0]["objects"][0]
    used = Path(item["path"])
    assert used.is_relative_to(workdir / "oriented") and used.name == "obj_1_block.stl"
    part = r.plan.plates[0].parts[0]
    assert part.orient is not None and part.key == "block"
    assert "oriented by Studio: best [-1, 0, 0], unprintability 0.6 (lay_on_face [1, 0, 0]" in r.report


def test_pipeline_auto_output_that_cannot_stand_is_a_problem(tmp_path, resolver):
    from test_optimise import uv_sphere

    src = M.write_stl(tmp_path / "ball.stl", uv_sphere())
    plan = Pipeline(FakeStudio(), resolver).plan(Spec.from_json(spec_dict(tmp_path, {"path": str(src), "orientation": "auto"})))
    assert any("no flat face on the bed" in p and "auto-orient found nothing better" in p for p in plan.problems)


def test_pipeline_merged_auto_part_keeps_the_own_path(tmp_path, resolver):
    a = M.write_stl(tmp_path / "a.stl", box((0, 0, 0), (30, 20, 5)))
    b = M.write_stl(tmp_path / "b.stl", box((0, 0, 5), (10, 10, 6)))
    studio = FakeStudio()
    spec = Spec.from_json(spec_dict(tmp_path, {"name": "m", "orientation": "auto", "parts": [{"path": str(a)}, {"path": str(b)}]}))
    r = Pipeline(studio, resolver).build(spec, export=False)
    assert r.plan.problems == [], r.plan.problems
    assert not [c for c in studio.calls if c[0] == "orient"]
    part = r.plan.plates[0].parts[0]
    assert part.orient is None and part.auto_face is not None
    assert all(fp.parent.name == "oriented" for fp, _, _ in part.files)
    assert "oriented by shambu's own score (orientation auto on a merged part)" in r.report


def test_explicit_orientation_never_calls_studio_orient(tmp_path, resolver):
    src = M.write_stl(tmp_path / "block.stl", box((0, 0, 0), (30, 20, 10)))
    studio = FakeStudio()
    Pipeline(studio, resolver).plan(Spec.from_json(spec_dict(tmp_path, {"path": str(src), "lay_on_face": [1, 0, 0]})))
    assert not [c for c in studio.calls if c[0] == "orient"]


# ---------------------------------------------------------------------------- sweep --source studio
def test_sweep_studio_variants_filter_rank_and_negate(tmp_path):
    src = M.write_stl(tmp_path / "block.stl", box((0, 0, 0), (30, 20, 10)))
    r2 = 1 / math.sqrt(2)
    studio = FakeStudio(
        orient_table=[
            OrientCandidate((r2, r2, 0.0), 1, 0, 0, 0, 0, 0, 0.1),  # an edge down: does not stand
            OrientCandidate((0.0, 0.0, 1.0), 1, 0, 0, 0, 0, 0, 0.5),  # as is
            OrientCandidate((-1.0, 0.0, 0.0), 1, 0, 0, 0, 0, 0, 0.3),  # +x face down
            OrientCandidate((-1.0, 0.0, 0.0), 1, 0, 0, 0, 0, 0, 0.4),  # a duplicate direction
        ]
    )
    d = {"name": "n", "outdir": str(tmp_path), "plates": [{"name": "p", "parts": [{"path": str(src), "orientation": "auto"}]}]}
    vs = S.orientation_variants(d, "block", top=4, source="studio", studio=studio)
    assert [v["parts"]["block"]["lay_on_face"] for v in vs] == [[1.0, 0.0, 0.0], [0.0, 0.0, -1.0]]
    assert [v["orientation"]["unprintability"] for v in vs] == [0.3, 0.5]
    assert all(v["orientation"]["stands"] and v["orientation"]["source"] == "studio" for v in vs)
    S.check_names(vs)
    patched = S.apply_variant(d, vs[0])["plates"][0]["parts"][0]
    assert "orientation" not in patched and patched["lay_on_face"] == [1.0, 0.0, 0.0]  # auto dropped: no conflict
    with pytest.raises(BuildError, match="not in"):
        S.orientation_variants(d, "block", source="theirs")


def test_sweep_studio_variants_raise_when_nothing_stands(tmp_path):
    src = M.write_stl(tmp_path / "block.stl", box((0, 0, 0), (30, 20, 10)))
    r2 = 1 / math.sqrt(2)
    studio = FakeStudio(orient_table=[OrientCandidate((r2, r2, 0.0), 1, 0, 0, 0, 0, 0, 0.1)])
    d = {"name": "n", "plates": [{"name": "p", "parts": [{"path": str(src)}]}]}
    with pytest.raises(BuildError, match="none of Studio's 1 orientations"):
        S.orientation_variants(d, "block", source="studio", studio=studio)


def test_own_variants_drop_auto_too(tmp_path):
    src = M.write_stl(tmp_path / "block.stl", box((0, 0, 0), (30, 20, 10)))
    d = {"name": "n", "plates": [{"name": "p", "parts": [{"path": str(src), "orientation": "auto"}]}]}
    vs = S.orientation_variants(d, "block", top=1)
    assert "orientation" not in S.apply_variant(d, vs[0])["plates"][0]["parts"][0]


# ---------------------------------------------------------------------------- CLI
@pytest.fixture
def app(monkeypatch, resolver):
    studio = FakeStudio()
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=studio, resolver=resolver))
    return studio


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, _ = capsys.readouterr()
    return code, out


def test_cli_orient_native(app, capsys, tmp_path):
    src = M.write_stl(tmp_path / "block.stl", box((0, 0, 0), (30, 20, 10)))
    code, out = run(capsys, "orient", "--native", str(src))
    assert code == 0 and "Studio auto-orient" in out and "BEST" in out and "[1.0000, 0.0000, 0.0000]" in out
    code, out = run(capsys, "--json", "orient", "--native", str(src), "--write", str(tmp_path / "w"))
    j = json.loads(out)
    assert code == 0 and j["source"] == "studio" and j["best"]["lay_on_face"] == [1.0, 0.0, 0.0]
    assert j["written"].endswith("obj_1_block.stl") and Path(j["written"]).is_file()
    assert run(capsys, "orient", "--native", str(tmp_path / "missing.stl"))[0] == 2


def test_cli_rotate_convert_unit(app, capsys, tmp_path):
    tiny = M.write_stl(tmp_path / "tiny.stl", box((0, 0, 0), (0.02, 0.01, 0.005)))
    code, out = run(capsys, "--json", "rotate", str(tiny), "-o", str(tmp_path / "r"), "--convert-unit")
    j = json.loads(out)
    assert code == 0 and j["size"] == pytest.approx([20, 10, 5]) and "1000" in j["unit"]
    code, out = run(capsys, "--json", "rotate", str(tiny), "-o", str(tmp_path / "r"), "--convert-unit", "--lay-on-face", "1,0,0")
    assert code == 0 and json.loads(out)["size"] == pytest.approx([5, 10, 20])
    with pytest.raises(SystemExit) as e:
        cli.main(["rotate", str(tiny), "-o", str(tmp_path / "r"), "--convert-unit", "--scale", "1000"])
    assert e.value.code == 2


def test_cli_sweep_source_studio(app, capsys, tmp_path, monkeypatch):
    src = M.write_stl(tmp_path / "block.stl", box((0, 0, 0), (30, 20, 10)))
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps(spec_dict(tmp_path, {"path": str(src), "orientation": "auto"})))
    seen = {}

    def fake_variants(spec_json, key, top, threshold, source="own", studio=None):
        seen.update(source=source, studio=studio)
        return []

    monkeypatch.setattr(S, "orientation_variants", fake_variants)
    monkeypatch.setattr(S, "run", lambda *a, **k: [])
    code, _ = run(capsys, "--json", "sweep", str(spec), "--orientations", "block", "--source", "studio", "--no-slice")
    assert code == 0 and seen["source"] == "studio" and seen["studio"] is app


def test_metres_check_points_to_the_native_fix():
    from shambu import layout as L
    from shambu.studio import MeshInfo

    info = MeshInfo(Path("j.stl"), (0.02, 0.09, 0.05), (0, 0, 0), (0.02, 0.09, 0.05), True, 12, 1, 0.0)
    texts = [c.text for c in L.MeshSane().check([L.Placed("j", info, 1)], None)]
    assert any("orientation" in t and "--convert-unit" in t for t in texts)


# ---------------------------------------------------------------------------- live (the installed Bambu Studio)
@pytest.mark.integration
@requires_studio
@pytest.mark.parametrize("stem", ["bracket", "carrier", "jaw_posy", "tag_bridge", "bottle-funnel"])
def test_live_sign_rule(stem, tmp_path):
    """Studio's export lies exactly as shambu's copy with lay_on_face = -best: same contact and support numbers."""
    src = REPO / "data" / f"{stem}.stl"
    r = StudioCLI(scratch=tmp_path / "scratch").orient([src], tmp_path / "o")
    assert r.best is not None
    native = M.metrics(M.read_stl(r.files[0]))
    lof = M.from_studio_vector(r.best.vector).lay_on_face
    own = M.metrics(M.transform(M.read_stl(src), M.lay_on_face_matrix(lof)))  # type: ignore[arg-type]
    assert native.stands
    assert native.contact_mm2 == pytest.approx(own.contact_mm2, abs=1.0)
    assert native.shadow_mm2 == pytest.approx(own.shadow_mm2, abs=1.0)
    assert native.height == pytest.approx(own.height, abs=0.05)


@pytest.mark.integration
@requires_studio
def test_live_bracket(tmp_path):
    r = StudioCLI(scratch=tmp_path / "scratch").orient([REPO / "data" / "bracket.stl"], tmp_path / "o")
    assert r.files[0].is_file() and r.files[0].name == "obj_1_bracket.stl"
    m = M.metrics(M.read_stl(r.files[0]))
    assert m.stands
    assert m.contact_mm2 == pytest.approx(2876, abs=5)
    assert m.height == pytest.approx(95.0, abs=0.05)
    assert r.best is not None and r.best.vector == (-1.0, 0.0, 0.0)
    assert len(r.candidates) == 32


@pytest.mark.integration
@requires_studio
def test_live_metres_jaw_convert_unit(tmp_path):
    src = REPO / "data" / "so101_moving_jaw_metres.stl"
    st = StudioCLI(scratch=tmp_path / "scratch")
    mm = st.normalise([src], tmp_path / "n")[0]
    lo, hi = M.bbox(M.read_stl(mm))
    assert (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]) == pytest.approx((22.3, 92.0, 48.0), abs=0.05)
    assert st.info(mm).size[0] == pytest.approx(22.3, abs=0.05)
    r = st.orient([src], tmp_path / "o")  # pass 1 converts the unit before it orients
    assert max(st.info(r.files[0]).size) > 40
