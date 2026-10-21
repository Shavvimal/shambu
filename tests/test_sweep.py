"""`shambu sweep --write-spec`: the chosen variant written as a full spec. Offline: a fake builder, no Studio."""

import json
from pathlib import Path

import pytest

from shambu import cli
from shambu import sweep as S
from shambu.spec import Spec

SECS = {"baseline": 22860, "fast": 20820, "slow": 25000, "by-object": 21000}  # per variant name; "broken" fails
SOURCES = [
    {"slot": 1, "preset": "Generic PETG @Test", "colour": "#FFFFFF", "source": "printer", "text": "left external spool"},
    {"slot": 2, "preset": "Generic PLA @Test", "colour": "#101010", "source": "printer", "text": "right external spool"},
]
MEASURED = [{k: s[k] for k in ("slot", "preset", "colour", "source")} for s in SOURCES]


class _Result:
    def __init__(self, ok, secs, grams):
        self.run = None
        self._j = {
            "ok": ok,
            "project": "/p.gcode.3mf",
            "problems": [] if ok else ["export failed: exit -102"],
            "verify_problems": [],
            "estimates": [{"plate": 1, "prediction_s": secs, "weight_g": grams, "filaments": [{"id": "1", "used_g": grams}]}] if ok else [],
            "report": "/r.md",
            "filaments": {"headline": "Filaments: 1 = Generic PETG @Test, 2 = Generic PLA @Test (printer)", "sources": SOURCES},
        }

    def to_json(self):
        return self._j


def fake_build(spec, do_slice):
    name = spec.outdir.name
    if name == "broken":
        return _Result(False, 0, 0.0)
    return _Result(True, SECS[name] if do_slice else 0, 142.0 if name == "fast" else 150.0)


@pytest.fixture
def sweep_files(tmp_path, monkeypatch):
    real_run = S.run
    monkeypatch.setattr(cli, "_studio_version", lambda app: "02.08.02.61")
    monkeypatch.setattr(S, "run", lambda spec_json, variants, build, **kw: real_run(spec_json, variants, fake_build, **kw))
    spec = {
        "name": "coupon",
        "outdir": str(tmp_path / "out"),
        "printer": "P",
        "process": {"base": "B", "set": {"wall_loops": "2"}},
        "filaments": [{"preset": "loaded"}],
        "plates": [{"name": "p", "parts": [{"path": str(tmp_path / "coupon.stl"), "name": "coupon"}]}],
    }
    sp = tmp_path / "plate.json"
    sp.write_text(json.dumps(spec))
    variants = [
        {"name": "fast", "process": {"set": {"wall_loops": "4"}}, "parts": {"coupon": {"lay_on_face": [0, 0, 1]}}},
        {"name": "slow", "process": {"set": {"layer_height": "0.12"}}},
    ]
    vp = tmp_path / "v.json"
    vp.write_text(json.dumps(variants))
    return tmp_path, sp, vp, spec


def sweep(capsys, *argv):
    code = cli.main(["sweep", *argv])
    out, err = capsys.readouterr()
    return code, out, err


def test_write_spec_writes_the_fastest_with_the_original_outdir(capsys, sweep_files):
    tmp, sp, vp, spec = sweep_files
    outp = tmp / "plate-best.json"
    code, out, _ = sweep(capsys, str(sp), "--variants", str(vp), "--write-spec", str(outp))
    assert code == 0
    assert out.splitlines()[-1] == (
        f"wrote {outp}: variant fast (Studio estimate 5 h 47 min, 142.0 g; baseline 6 h 21 min), "
        "measured with Generic PETG @Test / Generic PLA @Test"
    )
    w = json.loads(outp.read_text())
    assert w["outdir"] == spec["outdir"]  # not <outdir>/sweep/fast
    assert w["process"]["set"] == {"wall_loops": "4"}
    assert w["plates"][0]["parts"][0]["lay_on_face"] == [0, 0, 1]
    assert w["_sweep"] == {
        "from": str(sp.resolve()),
        "variant": "fast",
        "studio_estimate_s": 20820,
        "grams": 142.0,
        "baseline_s": 22860,
        "measured_with": MEASURED,
        "studio_version": "02.08.02.61",
        "command": ["shambu", "sweep", str(sp.resolve()), "--variants", str(vp.resolve())],
    }
    assert w["filaments"] == [{"preset": "loaded"}]  # the spec keeps the skill's rule: the next build reads the printer
    assert Spec.from_json(w).outdir == Spec.from_json(spec).outdir  # the provenance key does not break the spec
    assert json.loads(sp.read_text()) == spec  # the input is untouched


def test_write_spec_json_document(capsys, sweep_files):
    tmp, sp, vp, _ = sweep_files
    outp = tmp / "best.json"
    code = cli.main(["--json", "sweep", str(sp), "--variants", str(vp), "--write-spec", str(outp), "--pick", "slow"])
    j = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [r["name"] for r in j["results"]] == ["baseline", "fast", "slow"]
    assert j["written"] == {
        "path": str(outp),
        "variant": "slow",
        "studio_estimate_s": 25000,
        "grams": 150.0,
        "baseline_s": 22860,
        "measured_with": MEASURED,
        "studio_version": "02.08.02.61",
        "command": ["shambu", "sweep", str(sp.resolve()), "--variants", str(vp.resolve())],
    }
    assert json.loads(outp.read_text())["process"]["set"]["layer_height"] == "0.12"


def test_without_the_flag_the_json_stays_a_bare_list(capsys, sweep_files):
    _, sp, vp, _ = sweep_files
    assert cli.main(["--json", "sweep", str(sp), "--variants", str(vp)]) == 0
    assert isinstance(json.loads(capsys.readouterr().out), list)


def test_pick_baseline_writes_the_spec_back_with_provenance(capsys, sweep_files):
    tmp, sp, vp, spec = sweep_files
    outp = tmp / "again.json"
    assert sweep(capsys, str(sp), "--variants", str(vp), "--write-spec", str(outp), "--pick", "baseline")[0] == 0
    w = json.loads(outp.read_text())
    assert w.pop("_sweep")["variant"] == "baseline" and w == spec


def test_write_spec_refuses_the_input_path(capsys, sweep_files):
    _, sp, vp, spec = sweep_files
    code, out, _ = sweep(capsys, str(sp), "--variants", str(vp), "--write-spec", str(sp))
    assert code == 2 and "input spec" in out
    assert json.loads(sp.read_text()) == spec


def test_pick_naming_no_variant_is_exit_2(capsys, sweep_files):
    tmp, sp, vp, _ = sweep_files
    code, out, _ = sweep(capsys, str(sp), "--variants", str(vp), "--write-spec", str(tmp / "o.json"), "--pick", "nope")
    assert code == 2 and "no such variant" in out and "baseline, fast, slow" in out
    assert not (tmp / "o.json").exists()


def test_no_slice_needs_a_pick(capsys, sweep_files):
    tmp, sp, vp, _ = sweep_files
    code, out, _ = sweep(capsys, str(sp), "--variants", str(vp), "--no-slice", "--write-spec", str(tmp / "o.json"))
    assert code == 2 and "--pick" in out
    code, out, _ = sweep(capsys, str(sp), "--variants", str(vp), "--no-slice", "--write-spec", str(tmp / "o.json"), "--pick", "fast")
    assert code == 0 and out.splitlines()[-1].endswith(
        "variant fast (not sliced; no baseline numbers), measured with Generic PETG @Test / Generic PLA @Test"
    )


def test_pick_without_write_spec_is_exit_2(capsys, sweep_files):
    _, sp, vp, _ = sweep_files
    assert sweep(capsys, str(sp), "--variants", str(vp), "--pick", "fast")[0] == 2


def test_a_failed_variant_still_writes_the_winner_and_exits_1(capsys, sweep_files):
    tmp, sp, vp, _ = sweep_files
    vp.write_text(json.dumps([{"name": "fast"}, {"name": "broken"}]))
    outp = tmp / "o.json"
    code, out, _ = sweep(capsys, str(sp), "--variants", str(vp), "--write-spec", str(outp))
    assert code == 1 and outp.exists()
    assert out.splitlines()[-1].endswith("; failed: broken")


def test_nothing_passed_writes_nothing(capsys, sweep_files):
    tmp, sp, vp, _ = sweep_files
    vp.write_text(json.dumps([{"name": "broken"}]))
    outp = tmp / "o.json"
    code, out, _ = sweep(capsys, str(sp), "--variants", str(vp), "--no-baseline", "--write-spec", str(outp))
    assert code == 1 and not outp.exists() and out.splitlines()[-1] == "nothing written: no variant passed"
    code, out, _ = sweep(capsys, str(sp), "--variants", str(vp), "--write-spec", str(outp), "--pick", "broken")
    assert code == 1 and not outp.exists() and "nothing written: variant broken failed (export failed: exit -102)" in out


def test_written_spec_lists_a_changed_path(tmp_path):
    spec = {"outdir": "/o", "plates": [{"parts": [{"path": "/a/p.stl", "name": "p"}]}]}
    v = {"name": "p-1-studio", "parts": {"p": {"path": "/o/sweep/_units/p/p.stl"}}, "orientation": {"label": "studio 1"}}
    r = S.VariantResult("p-1-studio", True, "", True, [{"plate": 1, "prediction_s": 60, "weight_g": 1.0, "filaments": []}])
    w = S.written_spec(spec, v, r, "/in.json")
    assert w["_sweep"]["paths_changed"] == ["/o/sweep/_units/p/p.stl"] and w["_sweep"]["orientation"] == {"label": "studio 1"}
    assert "orientation" not in w["plates"][0]["parts"][0]


def test_a_temp_name_can_never_clobber_the_input(capsys, sweep_files):
    """The old temp file was <OUT>.tmp: with the input named x.json.tmp the write truncated it and the rename removed it."""
    tmp, sp, vp, _ = sweep_files
    src = tmp / "x.json.tmp"
    src.write_bytes(sp.read_bytes())
    before = src.read_bytes()
    code, _, _ = sweep(capsys, str(src), "--variants", str(vp), "--write-spec", str(tmp / "x.json"))
    assert code == 0
    assert src.read_bytes() == before
    assert json.loads((tmp / "x.json").read_text())["_sweep"]["variant"] == "fast"
    assert sorted(p.name for p in tmp.iterdir() if p.name.endswith(".tmp")) == ["x.json.tmp"]  # no temp file left


def test_a_failed_write_is_one_error_document_and_exit_3(capsys, sweep_files, monkeypatch):
    from pathlib import Path

    tmp, sp, vp, _ = sweep_files

    def denied(self, target):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "replace", denied)
    code = cli.main(["--json", "sweep", str(sp), "--variants", str(vp), "--write-spec", str(tmp / "o.json")])
    j = json.loads(capsys.readouterr().out)  # exactly one document
    assert code == 3
    assert j["error"] == f"BuildError: cannot write the spec to {tmp / 'o.json'}: Permission denied"
    assert not (tmp / "o.json").exists() and not [p for p in tmp.iterdir() if p.name.endswith(".tmp")]


def test_reproduce_command_names_every_variant_flag(capsys, sweep_files):
    tmp, sp, vp, _ = sweep_files
    outp = tmp / "o.json"
    assert sweep(capsys, str(sp), "--variants", str(vp), "--by-object", "--no-baseline", "--write-spec", str(outp))[0] == 0
    cmd = json.loads(outp.read_text())["_sweep"]["command"]
    assert cmd == ["shambu", "sweep", str(sp.resolve()), "--variants", str(vp.resolve()), "--by-object", "--no-baseline"]


def test_relocated_mesh_spec_builds_without_the_sweep_folder(capsys, tmp_path, monkeypatch, stls, resolver):
    """A variant that points a part at a file under <outdir>/sweep/: the written spec gets its own copy, and a build of
    that spec still finds the mesh after the sweep folder is gone."""
    import shutil

    from conftest import MACHINE, PROCESS, PROCESS_NAME, FakeStudio
    from shambu.facade import Bambu

    real_run = S.run
    monkeypatch.setattr(cli, "_studio_version", lambda app: "02.08.02.61")
    monkeypatch.setattr(S, "run", lambda spec_json, variants, build, **kw: real_run(spec_json, variants, fake_build, **kw))
    out = tmp_path / "out"
    units = out / "sweep" / "_units" / "fast" / "body.stl"
    units.parent.mkdir(parents=True)
    shutil.copyfile(stls["body"], units)
    spec = {
        "name": "fast",
        "outdir": str(out),
        "printer": MACHINE,
        "process": {"base": PROCESS, "name": PROCESS_NAME},
        "filaments": [{"preset": "Test PLA", "colour": "#000000"}],
        "plates": [{"name": "p", "parts": [{"path": str(stls["body"]), "name": "body"}]}],
    }
    sp = tmp_path / "plate.json"
    sp.write_text(json.dumps(spec))
    vp = tmp_path / "v.json"
    vp.write_text(json.dumps([{"name": "fast", "parts": {"body": {"path": str(units)}}}]))
    outp = tmp_path / "best" / "plate-best.json"
    code, text, _ = sweep(capsys, str(sp), "--variants", str(vp), "--write-spec", str(outp), "--pick", "fast")
    assert code == 0, text
    w = json.loads(outp.read_text())
    copy = outp.parent / "plate-best_meshes" / "body.stl"
    assert w["plates"][0]["parts"][0]["path"] == str(copy.resolve())
    assert w["_sweep"]["paths_changed"] == [{"from": str(units), "to": str(copy.resolve())}]
    assert copy.read_bytes() == units.read_bytes()
    shutil.rmtree(out / "sweep")
    r = Bambu(studio=FakeStudio(), resolver=resolver).build(Spec.from_json(w), export=False, printer=False)
    assert r.plan.ok, r.plan.problems


def test_written_spec_mode_follows_the_umask_or_the_replaced_file(tmp_path):
    import os
    import stat

    old = os.umask(0o022)
    try:
        new = S.write_spec(tmp_path / "new.json", {"a": 1})
        assert stat.S_IMODE(new.stat().st_mode) == 0o644  # not the temporary file's 0600
        kept = tmp_path / "kept.json"
        kept.write_text("{}")
        kept.chmod(0o640)
        S.write_spec(kept, {"a": 2})
        assert stat.S_IMODE(kept.stat().st_mode) == 0o640
    finally:
        os.umask(old)


def _count_builds(monkeypatch):
    calls = []
    real_run = S.run

    def counting(spec_json, variants, build, **kw):
        calls.append(1)
        return real_run(spec_json, variants, fake_build, **kw)

    monkeypatch.setattr(S, "run", counting)
    return calls


def test_an_unwritable_folder_is_refused_before_the_sweep(capsys, sweep_files, monkeypatch):
    tmp, sp, vp, _ = sweep_files
    calls = _count_builds(monkeypatch)
    locked = tmp / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        code = cli.main(["--json", "sweep", str(sp), "--variants", str(vp), "--write-spec", str(locked / "sub" / "o.json")])
        j = json.loads(capsys.readouterr().out)
    finally:
        locked.chmod(0o700)
    assert code == 3 and "cannot write in" in j["error"] and calls == []


@pytest.mark.parametrize("how", ["variants", "symlink", "hardlink"])
def test_write_spec_refuses_the_variants_file_and_links_of_the_input(capsys, sweep_files, monkeypatch, how):
    import os

    tmp, sp, vp, _ = sweep_files
    calls = _count_builds(monkeypatch)
    if how == "variants":
        target = vp
    else:
        target = tmp / f"{how}.json"
        (os.symlink if how == "symlink" else os.link)(sp, target)
    before = (sp.read_bytes(), vp.read_bytes())
    code, out, _ = sweep(capsys, str(sp), "--variants", str(vp), "--write-spec", str(target))
    assert code == 2 and ("the --variants file" if how == "variants" else "the input spec") in out
    assert calls == [] and (sp.read_bytes(), vp.read_bytes()) == before


def test_relocate_meshes_keeps_two_different_meshes_of_one_name_apart(tmp_path):
    """Two changed meshes with one basename and an absolute --write-spec path: each spec path gets its own copy with
    its own bytes (the second used to overwrite the first)."""
    from shambu.sweep import relocate_meshes

    a, b = tmp_path / "v1" / "body.stl", tmp_path / "v2" / "body.stl"
    for p, data in ((a, b"solid a\n"), (b, b"solid b\n")):
        p.parent.mkdir()
        p.write_bytes(data)
    spec = {
        "plates": [{"parts": [{"path": str(a)}, {"path": str(b)}]}],
        "_sweep": {"paths_changed": [str(a), str(b)]},
    }
    out = (tmp_path / "out" / "best.json").resolve()
    out.parent.mkdir()
    got = relocate_meshes(spec, out)
    pa, pb = (p["path"] for p in got["plates"][0]["parts"])
    assert pa != pb
    assert Path(pa).read_bytes() == b"solid a\n" and Path(pb).read_bytes() == b"solid b\n"


def test_write_spec_failure_still_prints_the_table(capsys, sweep_files, monkeypatch):
    from shambu.errors import BuildError

    tmp, sp, vp, _ = sweep_files

    def fail(path, spec):
        raise BuildError(f"cannot write {path}")

    monkeypatch.setattr(S, "write_spec", fail)
    code = cli.main(["sweep", str(sp), "--variants", str(vp), "--write-spec", str(tmp / "best.json"), "--pick", "slow"])
    out = capsys.readouterr().out
    assert code == 3 and "baseline" in out and "slow" in out and out.rstrip().endswith(f"BuildError: cannot write {tmp / 'best.json'}")
