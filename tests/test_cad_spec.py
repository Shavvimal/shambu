"""The CAD gate spec and runner: selectors by role, incomplete rules, spec errors, gate words, and the clearance and
sweep verdicts on plain numbers (no geom extra needed)."""

from __future__ import annotations

import pytest

from cadfix import codes, fails, run, spec, stl, write_record
from meshes import box, plate_with_holes
from shambu.cad import judge as J
from shambu.cad import rules as R
from shambu.cad import spec as S
from shambu.cad.report import RuleResult, status_of
from shambu.cad.run import run_rules
from shambu.errors import CadSpecError
from shambu.findings import FAIL, MEASURED, Finding

HOLE = {"name": "h1", "fit": "M4-clearance-medium", "mouth": [10, 10, 5], "axis": [0, 0, -1], "offset_max_mm": 0.1}


def _scene(tmp_path, hole_x=10.0, **part_over):
    """A clean scene: a plate with one M4 medium clearance hole, its record, and three stdlib rules."""
    mesh = stl(tmp_path / "out" / "base.stl", plate_with_holes((20, 20, 5), [{"x": hole_x, "y": 10, "stages": [(2.25, 0)]}]))
    write_record(mesh, "base")
    part = {
        "name": "base",
        "mesh": "out/base.stl",
        "record": "out/base.cad.json",
        "role": "rigid",
        "expected_size_mm": [20, 20, 5],
        "size_tol_mm": 0.05,
        "expected_features": [{"kind": "hole", "diameter_mm": 4.5, "count": 1}],
        "holes": [HOLE],
        **part_over,
    }
    return spec(
        tmp_path / "cad.json",
        {
            "parts": [part, {"name": "screw", "mesh": "ref/screw.stl", "role": "reference"}],
            "rules": [
                {"id": "solids", "type": "check", "parts": "role:!reference"},
                {"id": "inventory", "type": "features", "parts": "part:base"},
                {"id": "holes", "type": "fit", "parts": "part:base"},
            ],
        },
    )


def _failing_rules(doc) -> set[str]:
    return {f["rule"] for f in doc["findings"] if f["severity"] == "FAIL"}


def test_clean_scene_passes(tmp_path, capsys):
    code, doc, _, _ = run(capsys, "cad", "gate", str(_scene(tmp_path)))
    assert code == 0 and doc["gate"] == "pass", doc["findings"]
    assert [r["status"] for r in doc["rules"][1:]] == ["no-finding"] * 3


@pytest.mark.parametrize(
    ("defect", "rule"),
    [
        ({"expected_size_mm": [20, 20, 6]}, "solids"),
        ({"expected_features": [{"kind": "hole", "diameter_mm": 4.5, "count": 2}]}, "inventory"),
        ({"hole_x": 10.3}, "holes"),
    ],
)
def test_one_defect_fails_that_rule_and_no_other(tmp_path, capsys, defect, rule):
    hole_x = defect.pop("hole_x", 10.0)
    code, doc, _, _ = run(capsys, "cad", "gate", str(_scene(tmp_path, hole_x, **defect)))
    assert code == 1 and _failing_rules(doc) == {rule}


def test_fixed_part_by_role_not_name(tmp_path, capsys):
    """Rename the rigid part's mesh file: a rule that selects by role still finds and evaluates it."""
    sp = _scene(tmp_path)
    (tmp_path / "out" / "base.stl").rename(tmp_path / "out" / "base-mirrored.stl")
    write_record(tmp_path / "out" / "base-mirrored.stl", "base")
    sp.write_text(
        sp.read_text().replace("out/base.stl", "out/base-mirrored.stl").replace("out/base.cad.json", "out/base-mirrored.cad.json")
    )
    code, doc, _, _ = run(capsys, "cad", "gate", str(sp), "--only", "solids")
    solids = {r["id"]: r for r in doc["rules"]}["solids"]
    assert code == 0 and solids["status"] == "no-finding" and list(solids["parts"]) == ["base"]


def test_empty_selector_incomplete(tmp_path, capsys):
    sp = _scene(tmp_path)
    sp.write_text(sp.read_text().replace('"parts": "part:base"}, {"id": "holes"', '"parts": "group:nothing"}, {"id": "holes"'))
    code, doc, _, _ = run(capsys, "cad", "gate", str(sp))
    assert code == 1 and doc["gate"] == "incomplete"
    assert fails(doc) == {"cad.incomplete.selector_empty"} and _failing_rules(doc) == {"inventory"}
    assert {r["id"]: r["status"] for r in doc["rules"]}["holes"] == "no-finding"


def test_missing_mesh_and_missing_record_are_incomplete(tmp_path, capsys):
    sp = _scene(tmp_path)
    (tmp_path / "out" / "base.cad.json").unlink()
    code, doc, _, _ = run(capsys, "cad", "gate", str(sp), "--only", "solids")
    assert code == 1 and fails(doc) == {"cad.incomplete.no_record"}
    (tmp_path / "out" / "base.stl").unlink()
    code, doc, _, _ = run(capsys, "cad", "gate", str(sp), "--only", "inventory")
    assert code == 1 and fails(doc) == {"cad.incomplete.mesh_missing", "cad.incomplete.no_record"} and doc["gate"] == "incomplete"


@pytest.mark.parametrize(
    ("edit", "words"),
    [
        ({"exempt": [{"a": "part:a", "b": "part:b", "reason": " "}]}, "reason"),
        ({"type": "teleport"}, "type"),
    ],
)
def test_spec_errors_exit_1(tmp_path, capsys, edit, words):
    rule = {"id": "clash", "type": "clearance", "a": "part:a", "b": "part:b", **edit}
    sp = spec(tmp_path / "cad.json", {"parts": [{"name": "a", "mesh": "a.stl"}, {"name": "b", "mesh": "b.stl"}], "rules": [rule]})
    code, doc, _, _ = run(capsys, "cad", "gate", str(sp))
    assert code == 1 and words in doc["error"]


def test_non_orthonormal_placement_is_a_spec_error():
    with pytest.raises(CadSpecError, match="orthonormal"):
        S.placement({"origin": [0, 0, 0], "x": [1, 0, 0], "y": [0.1, 1, 0]}, "parts[0].placement")


def test_unknown_key_warns_and_default_role_is_info(tmp_path, capsys):
    stl(tmp_path / "a.stl", box((0, 0, 0), (10, 10, 10)))
    sp = spec(tmp_path / "cad.json", {"parts": [{"name": "a", "mesh": "a.stl", "colour": "red"}], "rules": [{"id": "s", "type": "check"}]})
    code, doc, _, _ = run(capsys, "cad", "gate", str(sp))
    assert code == 0 and {"cad.spec.unknown_key", "cad.default_role"} <= codes(doc)
    assert doc["spec_notes"]["unknown_keys"] == ["parts[0].colour"]


def test_skip_and_only_are_not_checked(tmp_path, capsys):
    code, doc, _, _ = run(capsys, "cad", "gate", str(_scene(tmp_path)), "--skip", "holes")
    st = {r["id"]: (r["status"], r.get("reason")) for r in doc["rules"]}
    assert code == 0 and st["holes"] == ("not-checked", "skipped: --skip holes")
    code, doc, _, _ = run(capsys, "cad", "gate", str(_scene(tmp_path)), "--only", "nope")
    assert code == 1 and "no rule" in doc["error"]


# ---------------------------------------------------------------------------- the runner with a fake rule
class FakeScene:
    def __init__(self, spec):
        self.spec = spec

    def subject(self, part):
        raise AssertionError("the fake rule reads no mesh")

    def record(self, part):
        return None

    def loaded(self):
        return {}


class FakeRule:
    def __init__(self, rs, codes):
        self.spec = rs
        self.codes = codes

    def run(self, ctx):
        fs = [Finding(c, FAIL, "x", c, MEASURED) for c in self.codes]
        return RuleResult(self.spec.id, self.spec.type, status_of(fs), findings=fs)


def _fake_spec():
    return S.parse(
        {
            "schema": S.SCHEMA,
            "parts": [{"name": "a", "mesh": "a.stl", "role": "rigid"}],
            "rules": [{"id": "r1", "type": "check"}, {"id": "r2", "type": "check"}],
        }
    )


@pytest.mark.parametrize(
    ("codes", "word"),
    [
        ((), "pass"),
        (("clash.overlap",), "fail"),
        (("cad.incomplete.mesh_missing",), "incomplete"),
        (("cad.incomplete.mesh_missing", "clash.overlap"), "fail"),
        (("clash.overlap", "cad.stale.stl_sha"), "stale"),
    ],
)
def test_gate_word_is_derived(codes, word):
    sp = _fake_spec()
    rep = run_rules(sp, FakeScene(sp), sp.rules, "gate", factory=lambda rs: FakeRule(rs, codes if rs.id == "r2" else ()))
    assert rep.gate == word and rep.exit_code == (0 if word == "pass" else 1)
    assert rep.lines()[-1].startswith(f"cad gate: {word} (")
    assert "ready" not in " ".join(rep.lines()).lower()


def test_rule_build_has_a_class_per_type():
    sp = _fake_spec()
    assert isinstance(R.build(sp.rules[0]), R.CheckRule)


# ---------------------------------------------------------------------------- clearance and sweep verdicts
def _rule(**kw):
    sp = S.parse(
        {
            "schema": S.SCHEMA,
            "parts": [{"name": "lid", "mesh": "l.stl", "role": "moving"}, {"name": "base", "mesh": "b.stl", "role": "rigid"}],
            "zones": {"seat": {"z": [9.9, 10.1]}},
            "rules": [{"id": "clash", "type": "clearance", "a": "role:moving", "b": "role:rigid", "margin_mm": 0.3, **kw}],
        }
    )
    return sp, sp.rules[0], sp.part("lid"), sp.part("base")


OV = J.Overlap("boolean", 100.0, ((9.0, 0.0, 0.0), (10.0, 10.0, 10.0)))


@pytest.mark.parametrize(
    ("exempt", "code"),
    [
        ([], "clash.overlap"),
        ([{"a": "part:lid", "b": "part:base", "reason": "press fit", "max_overlap_mm3": 150}], "clash.exempt"),
        ([{"a": "part:lid", "b": "part:base", "reason": "press fit", "max_overlap_mm3": 50}], "clash.over_cap"),
        ([{"a": "part:lid", "b": "part:base", "reason": "rests on the rim", "zone": "seat"}], "clash.overlap"),
    ],
)
def test_overlap_verdicts(exempt, code):
    sp, rule, a, b = _rule(exempt=exempt)
    _, fs = J.clearance_verdict(rule, a, b, sp.zones, OV, 1e-3)
    assert [f.code for f in fs] == [code]


def test_boolean_failure_is_fail_and_never_exempt():
    sp, rule, a, b = _rule(exempt=[{"a": "all", "b": "all", "reason": "anything"}])
    row, fs = J.clearance_verdict(rule, a, b, sp.zones, error="manifold3d refused the mesh")
    assert [(f.code, f.severity) for f in fs] == [("clash.boolean_failed", "FAIL")] and "exemption" not in row


def test_sampled_overlap_has_no_volume_and_a_cap_is_incomplete():
    sp, rule, a, b = _rule(exempt=[{"a": "part:lid", "b": "part:base", "reason": "press fit", "max_overlap_mm3": 1}])
    ov = J.Overlap("sampled", None, ((0, 0, 0), (1, 1, 1)), ((0.5, 0.5, 0.5),))
    row, fs = J.clearance_verdict(rule, a, b, sp.zones, ov)
    assert [f.code for f in fs] == ["cad.incomplete.no_volume"] and row["overlap"]["volume_mm3"] is None


@pytest.mark.parametrize(("d", "fail"), [(0.25, True), (0.35, False)])
def test_margin_edge(d, fail):
    sp, rule, a, b = _rule()
    row, fs = J.clearance_verdict(rule, a, b, sp.zones, J.Overlap("boolean", 0.0, None), distance=(d, (0, 0, 0), (d, 0, 0)))
    assert ([f.code for f in fs] == ["clash.margin"]) is fail and row["distance_mm"] == d


def test_beyond_the_bound_is_null_not_inf():
    sp, rule, a, b = _rule()
    row, fs = J.clearance_verdict(rule, a, b, sp.zones, beyond_mm=1.0)
    assert row["distance_mm"] is None and row["beyond_mm"] == 1.0 and fs == []


def _sweep(**kw):
    rule = {
        "id": "hinge",
        "type": "sweep",
        "moving": "part:lid",
        "fixed": "part:base",
        "motion": "rotate",
        "point": [0, 0, 0],
        "axis": [1, 0, 0],
    }
    rule.update({"from_deg": 0, "to_deg": 110, **kw})
    sp = S.parse({"schema": S.SCHEMA, "parts": [{"name": "lid", "mesh": "l.stl"}, {"name": "base", "mesh": "b.stl"}], "rules": [rule]})
    return sp.rules[0]


@pytest.mark.parametrize(("at", "code"), [(98.4, "sweep.early_contact"), (106.0, "sweep.first_contact")])
def test_rotation_contact_against_the_allowed_angle(at, code):
    rule = _sweep(first_contact_after_deg=105)
    run_ = J.SweepRun(steps=99, first=J.Contact(at, "distance", "base", "lid", (0.0, 1.0, 2.0), 0.4))
    block, fs = J.sweep_verdict(rule, run_, 110.0, lambda t: t)
    assert [f.code for f in fs] == [code] and block["first_contact"]["at_deg"] == at


def test_no_contact_and_end_cap():
    rule = _sweep(max_overlap_mm3=0.5)
    _, fs = J.sweep_verdict(rule, J.SweepRun(steps=111), 110.0, lambda t: t)
    assert [f.code for f in fs] == ["sweep.no_contact"]
    end = J.Contact(110.0, "overlap", "base", "lid", (0.0, 0.0, 0.0), None, 2.0)
    _, fs = J.sweep_verdict(rule, J.SweepRun(steps=111, first=end, end_overlap=end), 110.0, lambda t: t)
    assert sorted(f.code for f in fs) == ["sweep.first_contact", "sweep.overlap"]


def test_contact_before_the_end_without_a_bound_fails():
    rule = _sweep()
    c = J.Contact(40.0, "crossing", "base", "lid", (0.0, 0.0, 0.0))
    _, fs = J.sweep_verdict(rule, J.SweepRun(steps=41, first=c), 110.0, lambda t: t)
    assert [(f.code, f.severity) for f in fs] == [("sweep.margin", "FAIL")]


def test_pairs_skip_self_and_same_group():
    sp = S.parse(
        {
            "schema": S.SCHEMA,
            "parts": [{"name": "a", "mesh": "a", "group": "g"}, {"name": "b", "mesh": "b", "group": "g"}, {"name": "c", "mesh": "c"}],
            "rules": [{"id": "x", "type": "clearance", "a": "all", "b": "all", "skip_same_group": True}],
        }
    )
    assert {(p.name, q.name) for p, q in J.pairs_of(sp.rules[0], sp.parts)} == {("a", "c"), ("b", "c")}


def test_a_fit_rule_with_no_holes_reads_its_inputs_and_is_incomplete(tmp_path, capsys):
    """A fit rule on a part with no holes once passed without reading the mesh or the record, both missing."""
    part = {"name": "bracket", "mesh": "bracket.stl", "record": "bracket.cad.json"}
    sp = spec(tmp_path / "fit.json", {"parts": [part], "rules": [{"id": "holes", "type": "fit", "parts": "part:bracket"}]})
    code, doc, _, _ = run(capsys, "cad", "gate", str(sp))
    assert code == 1 and doc["gate"] == "incomplete"
    assert fails(doc) == {"cad.incomplete.mesh_missing", "cad.incomplete.no_record", "cad.incomplete.no_holes"}
    write_record(stl(tmp_path / "bracket.stl", box((0, 0, 0), (10, 10, 10))))
    code, doc, _, _ = run(capsys, "cad", "gate", str(sp))
    assert code == 1 and doc["gate"] == "incomplete" and fails(doc) == {"cad.incomplete.no_holes"}
