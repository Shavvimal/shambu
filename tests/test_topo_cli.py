"""shambu topo on a base install: the spec check, help, usage errors, the missing-extra path and the finding table.
Nothing here needs numpy; the solver tests are in test_topo_solver.py and test_topo_trace.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cadfix import run
from shambu import cli
from shambu.findings import CEILING, FAIL, Finding
from shambu.topo import michell
from shambu.topo.result import ALWAYS, CODES, NOT_CHECKED, TopoReport, finding
from shambu.topo.spec import PRESETS, parse_faces, parse_spec, rules_from

HEAVY = ("numpy", "scipy", "shapely", "skimage")


def _spec(tmp_path: Path, **over) -> Path:
    doc = michell.spec_doc("small")
    doc.update(over)
    p = tmp_path / "topo.json"
    p.write_text(json.dumps(doc))
    return p


def _hide(monkeypatch, mods=HEAVY):
    for m in mods:
        monkeypatch.setitem(sys.modules, m, None)


def test_check_valid_spec_exit_0(tmp_path, capsys):
    code, doc, _, _ = run(capsys, "topo", "check", str(_spec(tmp_path)))
    assert code == 0 and doc["schema"] == "shambu.topo.check/1" and doc["errors"] == []
    assert doc["summary"]["cases"][0]["name"] == "tip"


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"colour": "red"}, "unknown key 'colour'"),
        ({"cases": []}, "spec.cases: must be a non-empty list"),
        ({"simp": {"volfrac": [1.2]}}, "spec.simp.volfrac[0]"),
        ({"regions": [{"name": "r", "cells": {"blob": {}}}]}, "unknown selection primitive 'blob'"),
        ({"units": "in-lbf-psi"}, "spec.units"),
    ],
)
def test_check_bad_spec_exit_1_one_line_per_error(tmp_path, capsys, over, needle):
    code, doc, _, _ = run(capsys, "topo", "check", str(_spec(tmp_path, **over)))
    assert code == 1 and any(needle in e for e in doc["errors"]), doc["errors"]
    assert len(doc["errors"]) == len(set(doc["errors"]))


def test_check_text_lists_each_error(tmp_path, capsys):
    p = _spec(tmp_path, colour="red", cases=[])
    code = cli.main(["topo", "check", str(p)])
    out, _ = capsys.readouterr()
    lines = out.strip().splitlines()
    assert code == 1 and sum("colour" in x for x in lines) == 1 and sum("spec.cases" in x for x in lines) == 1
    assert lines[-1].startswith("topo check: fail")


def test_check_missing_stl_is_an_error(tmp_path, capsys):
    p = _spec(tmp_path, domain={"stl": "nowhere.stl", "pitch": 2})
    code, doc, _, _ = run(capsys, "topo", "check", str(p))
    assert code == 1 and "is not a file" in doc["errors"][0]


def test_unreadable_spec_exit_3(tmp_path, capsys):
    code, doc, _, _ = run(capsys, "topo", "check", str(tmp_path / "missing.json"))
    assert code == 3 and "cannot read" in doc["error"]


def test_topo_help_without_extras(capsys, monkeypatch):
    _hide(monkeypatch)
    with pytest.raises(SystemExit) as e:
        cli.main(["topo", "--help"])
    out, _ = capsys.readouterr()
    assert e.value.code == 0 and "map_not_proof" in out and "voxelise" in out


@pytest.mark.parametrize(
    ("argv", "extra"),
    [
        (["solve", "SPEC", "--out", "OUT"], "shambu[topo]"),
        (["voxelise", "SPEC"], "shambu[topo]"),
        (["validate"], "shambu[topo]"),
    ],
)
def test_missing_topo_extra_exit_3(tmp_path, capsys, monkeypatch, argv, extra):
    _hide(monkeypatch)
    spec = str(_spec(tmp_path))
    argv = [spec if x == "SPEC" else str(tmp_path / "out") if x == "OUT" else x for x in argv]
    code, doc, _, _ = run(capsys, "topo", *argv)
    assert code == 3 and extra in doc["error"] and "TopoDependencyError" in doc["error"]


def test_missing_trace_extra_exit_3(tmp_path, capsys, monkeypatch):
    _hide(monkeypatch, ("shapely",))
    faces = tmp_path / "faces.json"
    faces.write_text(
        json.dumps(
            {
                "schema": "shambu.topo.faces/1",
                "build_axis": "z",
                "faces": [{"name": "f", "axis": "z", "slab": [0, 2], "u": [0, 1], "v": [0, 1]}],
            }
        )
    )
    code, doc, _, _ = run(capsys, "topo", "trace", str(tmp_path / "run.json"), "--faces", str(faces))
    assert code == 3 and "shambu[topo-trace]" in doc["error"]


def test_spec_error_beats_missing_extra(tmp_path, capsys, monkeypatch):
    """A bad spec is the user's input (exit 1) whatever is installed."""
    _hide(monkeypatch)
    code, doc, _, _ = run(capsys, "topo", "solve", str(_spec(tmp_path, cases=[])), "--out", str(tmp_path / "o"))
    assert code == 1 and any("spec.cases" in e for e in doc["errors"])


@pytest.mark.parametrize(
    "argv",
    [
        ["topo", "solve", "s.json"],  # no --out
        ["topo", "trace", "run.json"],  # no --faces
        ["topo", "validate", "extra.json"],  # validate takes no file
        ["topo", "check"],  # no file
        ["topo", "check", "s.json", "--pitch", "2"],  # a solve flag on check
        ["topo", "trace", "r.json", "--faces", "f.json", "--volfrac", "0.5", "--volfrac", "0.6"],
    ],
)
def test_usage_errors_exit_2(capsys, argv):
    code = cli.main(["--json", *argv])
    out, _ = capsys.readouterr()
    assert code == 2 and "error" in json.loads(out)


def test_numpy_not_imported_at_collection(tmp_path):
    """`topo check` and `--help` must not import an extra (a fresh interpreter: other tests may have loaded them)."""
    spec = _spec(tmp_path)
    code = (
        "import sys\n"
        "from shambu import cli\n"
        f"assert cli.main(['--json', 'topo', 'check', {str(spec)!r}]) == 0\n"
        f"bad = [m for m in {HEAVY!r} if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr


def test_topo_package_never_prints():
    """Rubric 1: only cli.py prints. The optimiser reports progress through a listener."""
    root = Path(__file__).resolve().parents[1] / "src" / "shambu" / "topo"
    hits = [f"{p.name}:{i}" for p in sorted(root.glob("*.py")) for i, line in enumerate(p.read_text().splitlines(), 1) if "print(" in line]
    assert hits == []


# ---------------------------------------------------------------------------- the finding table
def test_twenty_seven_codes_and_five_fails():
    assert len(CODES) == 27
    assert {c for c, v in CODES.items() if v[0] == FAIL} == {
        "topo.equilibrium",
        "topo.passive_not_one",
        "topo.clearance",
        "topo.under_constrained",
        "topo.non_finite",
    }


def test_stiffness_text_is_scoped_to_fixed_loads():
    text = CODES["topo.stiffness_not_strength"][3]
    assert "under fixed loads" in text and "self-weight" in text


def test_seal_turns_a_nan_into_null_and_a_fail():
    from shambu.topo.result import seal

    rep = seal(TopoReport("solve", {"runs": [{"ratio": [1.5, float("nan")]}], "x": float("inf")}, [finding(c) for c in ALWAYS]))
    doc = rep.to_json()
    json.dumps(doc, allow_nan=False)  # strict JSON
    assert doc["runs"][0]["ratio"] == [1.5, None] and doc["x"] is None
    assert rep.exit_code == 1 and doc["ok"] is False
    (nf,) = [f for f in doc["findings"] if f["code"] == "topo.non_finite"]
    assert "runs[0].ratio[1]" in nf["message"] and nf["severity"] == "FAIL"


# ---------------------------------------------------------------------------- names, tags and flags
@pytest.mark.parametrize(
    ("key", "needle"),
    [
        ("passive", "spec.passive: passive region name 'pad' is used twice"),
        ("regions", "spec.regions: region name 'pad' is used twice"),
        ("supports", "spec.supports: support name 'pad' is used twice"),
    ],
)
def test_a_name_used_twice_is_a_spec_error(key, needle):
    """Two passive regions named alike would collapse into one dict entry and drop a constraint."""
    doc = michell.spec_doc("small")
    doc[key] = [
        {"name": "pad", "cells" if key != "supports" else "nodes": {"box": {"x": [0, 2]}}},
        {"name": "pad", "cells" if key != "supports" else "nodes": {"box": {"x": [22, 24]}}},
    ]
    spec, errors = parse_spec(doc)
    assert spec is None and needle in errors


def test_a_face_name_used_twice_and_a_bad_keepout_list_are_errors():
    face = {"name": "f", "axis": "z", "slab": [0, 2], "u": [0, 1], "v": [0, 1]}
    _, errors = parse_faces({"schema": "shambu.topo.faces/1", "build_axis": "z", "faces": [face, face], "keepouts": 5})
    assert any("face name 'f' is used twice" in e for e in errors)
    assert any("keepouts: must be a list" in e for e in errors)


def test_volfrac_tags_never_collide():
    from shambu.topo.spec import volfrac_tag

    assert [volfrac_tag(x) for x in (0.5, 0.4, 0.05, 0.401, 0.404, 1e-05)] == ["v50", "v40", "v05", "v401", "v404", "v00001"]
    doc = michell.spec_doc("small")
    doc["simp"]["volfrac"] = [0.4, 0.4]
    assert "spec.simp.volfrac: 0.4 is given twice" in parse_spec(doc)[1]


@pytest.mark.parametrize(
    "flags",
    [
        ["--pitch", "0"],
        ["--pitch", "nan"],
        ["--pitch", "-1"],
        ["--maxit", "0"],
        ["--tol", "nan"],
        ["--tol", "0"],
        ["--budget-min", "inf"],
        ["--volfrac", "1.5"],
        ["--volfrac", "0.4", "--volfrac", "0.4"],
        ["--max-cells", "0"],
    ],
)
def test_solve_flags_take_the_spec_ranges(tmp_path, capsys, flags):
    """A flag outside its range is a usage error before anything runs: no traceback, no empty run."""
    code, doc, _, _ = run(capsys, "topo", "solve", str(_spec(tmp_path)), "--out", str(tmp_path / "o"), *flags)
    assert code == 2 and flags[0] in doc["error"], doc
    assert not (tmp_path / "o").exists()


def test_check_refuses_a_grid_over_the_cap_with_a_memory_estimate(tmp_path, capsys):
    from shambu.topo.spec import MAX_CELLS

    p = _spec(tmp_path, domain={"box": {"min": [0, 0, 0], "max": [24, 1, 8]}, "pitch": 0.001})
    code, doc, _, _ = run(capsys, "topo", "check", str(p))
    assert code == 1 and doc["size"]["grid_cells"] == 24000 * 1000 * 8000
    (err,) = doc["errors"]
    assert f"over the cap of {MAX_CELLS}" in err and " MB" in err and "--max-cells" in err
    code, doc, _, _ = run(capsys, "topo", "check", str(_spec(tmp_path)))
    assert code == 0 and doc["size"]["grid_cells"] == 192 and doc["size"]["caps"]["cells"] == MAX_CELLS


def test_every_code_respects_its_evidence_ceiling():
    for code in CODES:
        f = finding(code)
        assert isinstance(f, Finding) and f.code == code
        assert {"INFO": 0, "WARN": 1, "FAIL": 2}[f.severity] <= {"INFO": 0, "WARN": 1, "FAIL": 2}[CEILING[f.evidence]]


def test_a_report_always_lists_what_it_did_not_check():
    rep = TopoReport("solve", {}, [finding(c) for c in ALWAYS])
    doc = rep.to_json()
    assert rep.exit_code == 0 and doc["not_checked"] == [dict(x) for x in NOT_CHECKED]
    assert any("strength" in x["what"] for x in doc["not_checked"])
    assert rep.lines()[-2].startswith("not checked: ")


def test_a_fail_finding_sets_exit_1():
    rep = TopoReport("solve", {}, [finding("topo.map_not_proof"), finding("topo.equilibrium")])
    assert rep.exit_code == 1 and rep.to_json()["ok"] is False


# ---------------------------------------------------------------------------- spec pieces
def test_michell_metrics_bands():
    good = {"top chord at the clamp": 0.9, "bottom chord at the clamp": 0.8, "middle of the clamp": 0.0}
    assert michell.metrics(good, [5, 4, 3, 2, 1.5, 1.4, 1.3])["pass"]
    assert not michell.metrics({**good, "middle of the clamp": 0.4}, [1.0])["pass"]
    rise = michell.metrics(good, [5, 4, 3, 2, 1.5, 1.4, 1.5])
    assert not rise["pass"] and rise["checks"]["objective_monotone"]["value"] == [7]


def test_rules_presets_and_file(tmp_path):
    assert rules_from("aggressive").rib_mm < rules_from("conservative").rib_mm
    f = tmp_path / "rules.json"
    f.write_text(json.dumps({"schema": "shambu.topo.rules/1", "base": "aggressive", "rib_mm": 6}))
    r = rules_from(str(f))
    assert r.rib_mm == 6 and r.rim_mm == PRESETS["aggressive"].rim_mm
    f.write_text(json.dumps({"base": "aggressive", "ribs": 6}))
    with pytest.raises(Exception, match="unknown key 'ribs'"):
        rules_from(str(f))


def test_raw_weights_default_to_one():
    doc = michell.spec_doc("small")
    doc["weights"] = "raw"
    spec, errors = parse_spec(doc)
    assert errors == [] and spec is not None and spec.cases[0].weight == 1.0


def test_a_moment_only_load_is_valid_and_an_empty_load_is_not():
    doc = michell.spec_doc("small")
    doc["cases"][0]["loads"][0]["force"] = [0, 0, 0]
    _, errors = parse_spec(doc)
    assert any("neither a force nor a moment" in e for e in errors)
    doc["cases"][0]["loads"][0]["moment"] = [0, 5, 0]
    assert parse_spec(doc)[1] == []
