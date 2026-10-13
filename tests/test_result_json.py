"""result.json numbers (PLAN-native-cli §5): RunResult.plates(), build estimates, the sweep table, and summary()
parsing the G-code only on request. Offline."""

import copy
import zipfile

import pytest

from conftest import SLICED_PLATES, FakeStudio, make_3mf
from shambu import sweep as S
from shambu.build import Pipeline, summarise
from shambu.studio import RunResult
from shambu.threemf import Project
from test_optimise import GCODE
from test_pipeline import make_spec

SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <header/>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="prediction" value="1066"/>
    <metadata key="weight" value="4.74"/>
    <metadata key="outside" value="false"/>
    <metadata key="support_used" value="{support}"/>
    <filament id="1" type="PLA" color="#000000" used_m="0.7" used_g="2.12"/>
{fil2}  </plate>
</config>
"""


FIL2 = '    <filament id="2" type="PLA" color="#FFFFFF" used_m="0.86" used_g="2.62"/>\n'


def sliced_project(path, support="true", two=True):
    """A project with a real slice_info plate (index 1, one or two filaments used) and plate_1.gcode holding GCODE."""
    src = make_3mf(path.with_suffix(".src.3mf"), sliced=True)
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            if info.filename == "Metadata/slice_info.config":
                zout.writestr(info, SLICE_INFO.format(support=support, fil2=FIL2 if two else ""))
            elif info.filename == "Metadata/plate_1.gcode":
                zout.writestr(info, GCODE)
            else:
                zout.writestr(info, zin.read(info.filename))
    return path


def run_result(result):
    return RunResult(["BambuStudio"], 0, 0, 0.1, None, result)


# ---------------------------------------------------------------------------- RunResult.plates()
def test_plates_types_the_sliced_plates():
    (p,) = run_result({"return_code": 0, "sliced_plates": copy.deepcopy(SLICED_PLATES)}).plates()
    assert p.id == 1
    assert p.total_s == pytest.approx(1065.91, abs=0.01) and p.main_s == pytest.approx(694.36, abs=0.01)
    assert p.filament_change_times == 0
    assert p.feature_times_s["Travel"] == pytest.approx(57.98, abs=0.01) and p.feature_times_s["Flush"] == 63.0
    assert [o["name"] for o in p.objects] == ["a_1", "b_1"]
    assert [(f.id, f.filament_id) for f in p.filaments] == [(1, "GFA00"), (2, "GFA00")]
    assert p.filaments[0].waste_g == 0.0 and p.filaments[1].waste_g == pytest.approx(0.5)
    assert p.warning_message == ""


@pytest.mark.parametrize(
    "result",
    [None, {"return_code": 0, "error_string": "Success."}, {"sliced_plates": "junk"}, {"sliced_plates": [3, None]}],
)
def test_plates_empty_without_slice_data(result):
    assert run_result(result).plates() == []


def test_plates_tolerates_missing_and_bad_fields():
    (p,) = run_result({"sliced_plates": [{"id": "2", "total_predication": None, "filaments": [{"id": 1, "main_used_g": "x"}]}]}).plates()
    assert p.id == 2 and p.total_s == 0.0 and p.feature_times_s == {} and p.objects == []
    assert p.filaments[0].main_g == 0.0 and p.filaments[0].total_g == 0.0


# ---------------------------------------------------------------------------- summary(features=...)
def test_summary_parses_gcode_only_on_request(tmp_path, monkeypatch):
    f = sliced_project(tmp_path / "s.gcode.3mf")
    calls = []
    real = Project.feature_usage

    def spy(self, plate):
        calls.append(plate)
        return real(self, plate)

    monkeypatch.setattr(Project, "feature_usage", spy)
    s = Project(str(f)).summary()
    assert calls == [] and "features" not in s["plates"][0]["slice"]
    s = Project(str(f)).summary(features=True)
    assert calls == [1] and s["plates"][0]["slice"]["features"]["support_g"] > 0


def test_summarise_parses_only_where_the_parser_adds_something(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(Project, "feature_usage", lambda self, plate: calls.append(plate) or {"support_g": 1.0})
    rr = run_result({"sliced_plates": copy.deepcopy(SLICED_PLATES)})
    summarise(sliced_project(tmp_path / "no.gcode.3mf", support="false", two=False), rr)
    assert calls == []  # one filament, no support, result.json present: nothing to parse for
    summarise(sliced_project(tmp_path / "yes.gcode.3mf", support="true", two=False), rr)
    assert calls == [1]  # support grams
    summarise(sliced_project(tmp_path / "two.gcode.3mf", support="false", two=True), rr)
    assert calls == [1, 1]  # two filaments: the tool-switch count
    summarise(sliced_project(tmp_path / "noresult.gcode.3mf", support="false", two=False), run_result({"return_code": 0}))
    assert calls == [1, 1, 1]  # no result.json: the parser is the only source of tower and flush grams


def test_inspect_keeps_feature_grams(tmp_path):
    from shambu.facade import Bambu

    f = sliced_project(tmp_path / "s.gcode.3mf")
    assert Bambu(studio=FakeStudio()).inspect(f)["plates"][0]["slice"]["features"]["support_g"] > 0
    assert "features" not in Bambu(studio=FakeStudio()).inspect(f, features=False)["plates"][0]["slice"]


# ---------------------------------------------------------------------------- build estimates
def build(tmp_path, stls, resolver, support="true", sliced_plates=SLICED_PLATES, two=True):
    fixture = sliced_project(tmp_path / "fixture.gcode.3mf", support=support, two=two)
    studio = FakeStudio(fixture, sliced_plates=sliced_plates)
    return Pipeline(studio, resolver).build(make_spec(tmp_path, stls), do_slice=True, keep_going=True)


def test_to_json_estimates_merge_result_json(tmp_path, stls, resolver):
    r = build(tmp_path, stls, resolver)
    (e,) = r.to_json()["estimates"]
    assert e["plate"] == 1 and e["prediction_s"] == 1066 and e["weight_g"] == pytest.approx(4.74)  # slice_info keys stay
    assert e["main_prediction_s"] == 694
    assert e["filament_changes"] == 1 and e["studio_filament_change_times"] == 0  # swaps from the G-code T lines
    assert e["feature_times_s"]["Travel"] == 58.0 and e["feature_times_s"]["Outer wall"] == 147.9
    f1, f2 = e["filaments"]
    assert f1["used_g"] == 2.12 and f1["type"] == "PLA"  # slice_info keys stay
    assert (f1["total_g"], f1["main_g"], f1["waste_g"]) == (2.12, 2.12, 0.0)
    assert (f2["total_g"], f2["main_g"], f2["waste_g"]) == (2.62, 2.12, 0.5)
    assert e["features"]["support_g"] > 0  # support was used: support grams still come from the parser
    assert "prime tower + flush 0.5 g (result.json)" in r.report and "(G-code parser)" in r.report
    assert "time by feature (result.json)" in r.report and "travel 1.0 min" in r.report
    assert "1 filament change(s) (G-code parser)" in r.report


def test_to_json_without_result_json_keeps_old_shape(tmp_path, stls, resolver):
    r = build(tmp_path, stls, resolver, sliced_plates=None)
    (e,) = r.to_json()["estimates"]
    assert "feature_times_s" not in e and "main_g" not in e["filaments"][0]
    assert e["features"]["tower_g"] > 0
    assert "(G-code parser, no result.json)" in r.report


def test_one_filament_without_support_skips_the_parser(tmp_path, stls, resolver):
    r = build(tmp_path, stls, resolver, support="false", two=False)
    (e,) = r.to_json()["estimates"]
    assert "features" not in e and e["filament_changes"] == 0 and e["studio_filament_change_times"] == 0
    assert "support 0.0 g (the slicer used none)" in r.report and "0 filament change(s) (one filament)" in r.report


def test_two_filaments_without_support_parse_for_swaps(tmp_path, stls, resolver):
    r = build(tmp_path, stls, resolver, support="false")
    (e,) = r.to_json()["estimates"]
    assert e["features"]["filament_changes"] == 1 and e["filament_changes"] == 1


# ---------------------------------------------------------------------------- sweep
class _Result:
    def __init__(self, estimates):
        self.run = None
        self._j = {"ok": True, "project": "/p.gcode.3mf", "problems": [], "verify_problems": [], "estimates": estimates, "report": "/r.md"}

    def to_json(self):
        return self._j


def _estimate(support_used="true", features=None, times=None, changes=None):
    e = {
        "plate": 1,
        "prediction_s": 1066,
        "weight_g": 4.74,
        "support_used": support_used,
        "filaments": [{"id": "1", "used_g": 2.12, "waste_g": 0.0}, {"id": "2", "used_g": 2.62, "waste_g": 0.5}],
        "features": features,
    }
    if features:
        e["filament_changes"] = features["filament_changes"]
    elif changes is not None:
        e["filament_changes"] = changes
    if times is not None:
        e.update(feature_times_s=times, main_prediction_s=694, studio_filament_change_times=0)
    return e


def test_sweep_reads_result_json_columns(tmp_path):
    from test_optimise import BASE

    times = {"Support": 120.0, "Prime tower": 90.0, "Flush": 63.0, "Travel": 57.98, "Outer wall": 147.9}
    est = [
        _estimate("true", {"support_g": 1.5, "waste_g": 9.9, "filament_changes": 7}, times),
        _estimate("false", None, times, changes=0),
    ]
    builds = iter(est)
    d = {**BASE, "outdir": str(tmp_path)}
    res = S.run(d, [S.by_object_variant(d)], lambda spec, do_slice: _Result([next(builds)]))
    j = res[0].to_json()
    assert j["support_g"] == 1.5 and j["waste_g"] == 0.5  # waste from result.json, not the parser's 9.9
    assert j["filament_changes"] == 7  # swaps from the parser, not result.json's 0
    assert res[1].to_json()["filament_changes"] == 0
    assert j["feature_times_s"] == {"Support": 120.0, "Prime tower": 90.0, "Flush": 63.0, "Travel": 58.0}
    assert res[1].to_json()["support_g"] == 0.0  # no support used: known zero without the parser
    lines = S.table(res)
    for head in ("supp min", "tower min", "flush min", "travel min"):
        assert head in lines[0]
    row = lines[1].split()
    # name, time, grams, vs best, support g, waste g, swaps, support/tower/flush/travel minutes
    assert row[4:11] == ["1.5", "0.5", "7", "2.0", "1.5", "1.1", "1.0"]


def test_sweep_without_result_json_falls_back_to_the_parser(tmp_path):
    from test_optimise import BASE

    d = {**BASE, "outdir": str(tmp_path)}
    feats = {"support_g": 1.5, "waste_g": 2.0, "filament_changes": 3}
    res = S.run(d, [], lambda spec, do_slice: _Result([_estimate("true", feats)]))
    j = res[0].to_json()
    assert (j["support_g"], j["waste_g"], j["filament_changes"], j["feature_times_s"]) == (1.5, 2.0, 3, {})
    row = S.table(res)[1].split()
    assert row[4:11] == ["1.5", "2.0", "3", "-", "-", "-", "-"]


def test_sweep_unknown_support_shows_a_dash(tmp_path):
    from test_optimise import BASE

    d = {**BASE, "outdir": str(tmp_path)}
    res = S.run(d, [], lambda spec, do_slice: _Result([_estimate("true", None, {"Travel": 60.0})]))
    assert res[0].to_json()["support_g"] is None
    assert S.table(res)[1].split()[4] == "-"
