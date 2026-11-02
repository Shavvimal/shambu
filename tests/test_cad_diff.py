"""shambu cad diff: measures, sections, feature changes, plates to reslice, coupons and the expected-change gate."""

from __future__ import annotations

import json

from cadfix import codes, fails, run, spec, stl
from meshes import box, plate_with_holes
from shambu import meshgeom as G


def _plate(t=5.0, holes=((10, 8, 2.0), (30, 12, 2.0))):
    return plate_with_holes((40, 20, t), [{"x": x, "y": y, "stages": [(r, 0)]} for x, y, r in holes])


def _pair(tmp_path, old, new):
    return stl(tmp_path / "old" / "bracket.stl", old), stl(tmp_path / "new" / "bracket.stl", new)


def test_section_area_is_exact_on_a_box():
    assert abs(G.section_area(box((0, 0, 0), (10, 20, 5)), 2, 2.5) - 200) < 1e-9
    assert abs(G.section_area(box((0, 0, 0), (10, 20, 5)), 0, 3.0) - 100) < 1e-9


def test_same_file_twice_has_no_change(tmp_path, capsys):
    a, b = _pair(tmp_path, _plate(), _plate())
    code, doc, _, _ = run(capsys, "cad", "diff", str(a), str(b))
    assert code == 0 and doc["findings"] == [] and len(doc["features"]["same"]) == 2


def test_thicker_plate(tmp_path, capsys):
    a, b = _pair(tmp_path, _plate(5), _plate(7))
    code, doc, _, _ = run(capsys, "cad", "diff", str(a), str(b))
    assert code == 0 and {"diff.size", "diff.volume", "diff.section"} <= codes(doc)
    z = next(s for s in doc["sections"] if s["axis"] == "z")
    assert z["changed_between_mm"] and z["changed_between_mm"][0][0] >= 4.5  # the change sits above the old top
    assert doc["measures"]["size_z"]["delta"] == 2.0


def test_moved_and_resized_holes(tmp_path, capsys):
    a, b = _pair(tmp_path, _plate(), _plate(holes=((12, 8, 2.0), (30, 12, 1.75))))
    code, doc, _, _ = run(capsys, "cad", "diff", str(a), str(b))
    moved, resized = doc["features"]["moved"], doc["features"]["resized"]
    assert code == 0 and len(moved) == 1 and len(resized) == 1
    assert moved[0]["delta_mm"] == [2.0, 0.0, 0.0]
    assert abs(resized[0]["delta_diameter_mm"] + 0.5) < 1e-6
    assert {"diff.feature_moved", "diff.feature_resized"} <= codes(doc)


def _spec(tmp_path, kind="insert"):
    return spec(
        tmp_path / "cad.json",
        {
            "fits": [{"name": "pocket", "kind": kind, "hole_mm": 4.0, "basis": "insert datasheet"}],
            "parts": [
                {
                    "name": "bracket",
                    "mesh": "new/bracket.stl",
                    "holes": [{"name": "h2", "fit": "pocket", "mouth": [30, 12, 5], "axis": [0, 0, -1]}],
                }
            ],
            "rules": [{"id": "holes", "type": "fit"}],
        },
    )


def test_a_changed_fit_needs_a_coupon(tmp_path, capsys):
    a, b = _pair(tmp_path, _plate(), _plate(holes=((10, 8, 2.0), (30, 12, 2.1))))
    code, doc, _, _ = run(capsys, "cad", "diff", str(a), str(b), "--spec", str(_spec(tmp_path)))
    (c,) = [f for f in doc["findings"] if f["code"] == "diff.coupon"]
    assert code == 0 and c["severity"] == "WARN" and "h2" in c["message"]


def test_expect_names_the_intended_changes(tmp_path, capsys):
    a, b = _pair(tmp_path, _plate(5), _plate(7, holes=((12, 8, 2.0), (30, 12, 2.0))))
    exp = tmp_path / "expect.json"
    exp.write_text(json.dumps({"changes": [{"measure": "size_z", "delta_mm": 2}]}))
    code, doc, _, _ = run(capsys, "cad", "diff", str(a), str(b), "--expect", str(exp))
    assert code == 1 and fails(doc) == {"diff.unexpected"}
    (u,) = [f for f in doc["findings"] if f["code"] == "diff.unexpected"]
    assert "moved" in u["message"]


def test_expected_change_that_did_not_happen_and_coupon_decision(tmp_path, capsys):
    a, b = _pair(tmp_path, _plate(), _plate(holes=((10, 8, 2.0), (30, 12, 2.1))))
    exp = tmp_path / "expect.json"
    exp.write_text(json.dumps({"changes": [{"feature": "h2", "diameter_mm": 4.2}, {"measure": "size_z", "delta_mm": 1}]}))
    code, doc, _, _ = run(capsys, "cad", "diff", str(a), str(b), "--spec", str(_spec(tmp_path)), "--expect", str(exp))
    assert code == 1 and fails(doc) == {"diff.coupon_undecided", "diff.expected_missing"}
    exp.write_text(json.dumps({"changes": [{"feature": "h2", "diameter_mm": 4.2, "coupon": True}]}))
    code, doc, _, _ = run(capsys, "cad", "diff", str(a), str(b), "--spec", str(_spec(tmp_path)), "--expect", str(exp))
    assert code == 0 and fails(doc) == set()


def test_plate_impact(tmp_path, capsys):
    a, b = _pair(tmp_path, _plate(5), _plate(7))
    plate = tmp_path / "plate.json"
    plate.write_text(
        json.dumps(
            {
                "plates": [
                    {"name": "one", "parts": [{"path": "other.stl"}]},
                    {"name": "two", "parts": [{"name": "g", "parts": [{"path": "new/bracket.stl"}]}]},
                ]
            }
        )
    )
    code, doc, _, _ = run(capsys, "cad", "diff", str(a), str(b), "--plate", str(plate))
    imp = doc["impact"]
    assert code == 0 and imp["plates"] == [2] and imp["replace_mesh"].startswith("needs --allow-size-change")
    (r,) = [f for f in doc["findings"] if f["code"] == "diff.reslice"]
    assert r["plate"] == 2 and r["fix"]["command"].startswith("shambu build")


def test_diff_takes_two_files(tmp_path, capsys):
    a, _ = _pair(tmp_path, _plate(), _plate())
    code, doc, _, _ = run(capsys, "cad", "diff", str(a))
    assert code == 2 and "two files" in doc["error"]


def _inward_box(lo, hi):
    return [(a, c, b) for a, b, c in box(lo, hi)]


def test_expect_explains_a_volume_and_section_change(tmp_path, capsys):
    """An internal cavity changes no outside size and no cylindrical feature; an empty expectation once passed it."""
    a, b = _pair(tmp_path, box((0, 0, 0), (20, 20, 20)), box((0, 0, 0), (20, 20, 20)) + _inward_box((5, 5, 5), (17, 17, 17)))
    exp = tmp_path / "expect.json"
    exp.write_text(json.dumps({"changes": []}))
    code, doc, _, _ = run(capsys, "cad", "diff", str(a), str(b), "--expect", str(exp))
    assert code == 1 and "diff.unexpected" in fails(doc)
    exp.write_text(json.dumps({"changes": [{"measure": "volume_mm3", "delta_mm3": -1728}]}))
    code, doc, _, _ = run(capsys, "cad", "diff", str(a), str(b), "--expect", str(exp))
    assert code == 0 and fails(doc) == set()


def test_an_old_record_stays_usable_after_the_inputs_move_on(tmp_path, capsys):
    """The revision workflow edits the script and parameters in place: the old record still names the old export
    (its STL hash), but its build id no longer recomputes. diff judges the artifact, not the current build."""
    from cadfix import write_record

    params = tmp_path / "params.json"
    params.write_text('{"w": 20}')
    a, b = _pair(tmp_path, _plate(5), _plate(7))
    rec = write_record(a, inputs=[params])
    params.write_text('{"w": 21}')
    code, doc, _, _ = run(capsys, "cad", "diff", str(rec), str(b))
    assert code == 0 and not any(c.startswith("cad.stale.") for c in codes(doc))
    a.write_bytes(b.read_bytes())  # the old export itself replaced: that is still stale
    code, doc, _, _ = run(capsys, "cad", "diff", str(rec), str(b))
    assert code == 1 and "cad.stale.stl_sha" in fails(doc)


def test_a_new_spec_hole_keeps_its_name(tmp_path, capsys):
    a, b = _pair(tmp_path, _plate(holes=((30, 12, 2.0),)), _plate(holes=((10, 8, 2.15), (30, 12, 2.0))))
    sp = spec(
        tmp_path / "cad.json",
        {
            "fits": [{"name": "pocket", "kind": "insert", "hole_mm": 4.3, "basis": "insert datasheet"}],
            "parts": [
                {
                    "name": "bracket",
                    "mesh": "new/bracket.stl",
                    "holes": [{"name": "h1", "fit": "pocket", "mouth": [10, 8, 5], "axis": [0, 0, -1]}],
                }
            ],
        },
    )
    exp = tmp_path / "expect.json"
    exp.write_text(json.dumps({"changes": [{"feature": "h1", "diameter_mm": 4.3, "coupon": True}]}))
    code, doc, _, _ = run(capsys, "cad", "diff", str(a), str(b), "--spec", str(sp), "--part", "bracket", "--expect", str(exp))
    assert code == 0 and fails(doc) == set() and doc["features"]["new"][0]["id"] == "h1"
    assert "diff.coupon" in codes(doc)


def test_through_to_blind_is_a_change():
    from shambu.cad import diff as D
    from shambu.cad.features import Feature

    old = Feature("hole", 4.0, (0.0, 0.0, 1.0), (0.0, 0.0, 5.0), 10.0, "through", 0.0, 64)
    new = Feature("hole", 4.0, (0.0, 0.0, 1.0), (0.0, 0.0, 5.0), 10.0, "blind", 0.0, 64)
    (c,) = D.match_features([old], [new])
    assert c.kind == "resized" and D.match_features([old], [old])[0].kind == "same"
