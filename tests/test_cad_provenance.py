"""shambu cad stamp and the stale-export refusal: build id, STL and STEP hashes, --promote."""

from __future__ import annotations

import json
from pathlib import Path

from cadfix import codes, fails, run, stl, write_record
from meshes import box
from shambu.cad import provenance as P


def _inputs(tmp_path: Path) -> list[Path]:
    script = tmp_path / "parts" / "bracket.js"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("// build\n")
    params = tmp_path / "params.json"
    params.write_text('{"w": 20}')
    return [script, params]


def test_build_id_stable_across_runs_and_order(tmp_path):
    a, b = _inputs(tmp_path)
    one = P.build_id([("parts/bracket.js", a), ("params.json", b)], "B")
    two = P.build_id([("params.json", b), ("parts/bracket.js", a)], "B")
    assert one == two and one.startswith("B@") and len(one) == 2 + 12


def test_build_id_changes_with_one_byte_and_missing_differs_from_empty(tmp_path):
    a, b = _inputs(tmp_path)
    before = P.build_id([("a", a), ("b", b)], "B")
    a.write_text("// build!\n")
    assert P.build_id([("a", a), ("b", b)], "B") != before
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    assert P.build_id([("x", empty)], "B") != P.build_id([("x", tmp_path / "gone.txt")], "B")


def test_fresh_record_passes(tmp_path, capsys):
    part = stl(tmp_path / "out" / "bracket.stl", box((0, 0, 0), (20, 10, 5)))
    rec = write_record(part, inputs=_inputs(tmp_path))
    code, doc, _, _ = run(capsys, "cad", "check", str(part), "--record", str(rec))
    assert code == 0 and doc["gate"] == "pass"
    prov = doc["rules"][0]["parts"]["bracket"]["areas"]["provenance"]
    assert prov["build_id_ok"] and prov["stl_sha_ok"]


def test_stale_build_id(tmp_path, capsys):
    part = stl(tmp_path / "out" / "bracket.stl", box((0, 0, 0), (20, 10, 5)))
    script, params = _inputs(tmp_path)
    rec = write_record(part, inputs=[script, params])
    params.write_text('{"w": 21}')  # the parameters changed after the export
    code, doc, _, _ = run(capsys, "cad", "check", str(part), "--record", str(rec))
    assert code == 1 and doc["gate"] == "stale" and "cad.stale.build_id" in fails(doc)


def test_stale_stl_sha(tmp_path, capsys):
    """Same build id, but the STL was replaced after stamping: the old report must not carry over."""
    part = stl(tmp_path / "out" / "bracket.stl", box((0, 0, 0), (20, 10, 5)))
    rec = write_record(part, inputs=_inputs(tmp_path))
    stl(part, box((0, 0, 0), (20, 10, 6)))
    code, doc, _, _ = run(capsys, "cad", "check", str(part), "--record", str(rec))
    assert code == 1 and doc["gate"] == "stale"
    assert fails(doc) == {"cad.stale.stl_sha"}


def test_stale_step_sha(tmp_path, capsys):
    part = stl(tmp_path / "bracket.stl", box((0, 0, 0), (20, 10, 5)))
    step = tmp_path / "bracket.stp"
    step.write_text("ISO-10303-21;\n")
    code, _, _, _ = run(capsys, "cad", "stamp", str(part), "--part", "bracket", "--step", str(step), "--step-volume", "1000")
    assert code == 0
    step.write_text("ISO-10303-21; changed\n")
    code, doc, _, _ = run(capsys, "cad", "check", str(part), "--record", str(tmp_path / "bracket.cad.json"))
    assert code == 1 and "cad.stale.step_sha" in fails(doc)


def test_record_path_mismatch_is_a_warning(tmp_path, capsys):
    part = stl(tmp_path / "a" / "bracket.stl", box((0, 0, 0), (20, 10, 5)))
    rec = write_record(part)
    copy = tmp_path / "b" / "bracket.stl"
    copy.parent.mkdir()
    copy.write_bytes(part.read_bytes())
    code, doc, _, _ = run(capsys, "cad", "check", str(copy), "--record", str(rec))
    assert code == 0 and "cad.record_path" in codes(doc)


def test_stamp_writes_record_fields(tmp_path, capsys):
    part = stl(tmp_path / "bracket.stl", box((0, 0, 0), (20, 10, 5)))
    script, params = _inputs(tmp_path)
    code, doc, _, _ = run(
        capsys,
        "cad", "stamp", str(part), "--part", "bracket", "--rev", "B", "--input", str(script), "--input", str(params),
        "--cad-volume", "1000", "--cad-cog", "10,5,2.5", "--faceting-tol", "0.05", "--angle-tol", "6", "--bed-face", "0,0,-1",
    )  # fmt: skip
    assert code == 0
    rec = json.loads((tmp_path / "bracket.cad.json").read_text())
    assert rec["schema"] == "shambu.cad.record/1" and rec["build_id"] == doc["build_id"]
    assert [i["path"] for i in rec["inputs"]] == ["params.json", "parts/bracket.js"]
    assert rec["mesh"]["volume_mm3"] == 1000 and rec["mesh"]["bodies"] == 1
    assert rec["stl"]["faceting_tol_mm"] == 0.05 and rec["print"]["bed_face"] == [0, 0, -1]


def test_stamp_missing_input_is_exit_3(tmp_path, capsys):
    part = stl(tmp_path / "bracket.stl", box((0, 0, 0), (20, 10, 5)))
    code, doc, _, _ = run(capsys, "cad", "stamp", str(part), "--part", "bracket", "--input", str(tmp_path / "nope.js"))
    assert code == 3 and "nope.js" in doc["error"]


def test_promote_copies_only_on_a_pass(tmp_path, capsys):
    part = stl(tmp_path / "out" / ".candidate" / "bracket.stl", box((0, 0, 0), (20, 10, 5)))
    rec = write_record(part, inputs=_inputs(tmp_path))
    dest = tmp_path / "out" / "bracket@A.stl"
    code, doc, _, _ = run(capsys, "cad", "check", str(part), "--record", str(rec), "--promote", str(dest))
    assert code == 0 and doc["promote"]["done"]
    assert dest.read_bytes() == part.read_bytes()
    new_rec = json.loads((tmp_path / "out" / "bracket@A.cad.json").read_text())
    assert new_rec["stl"]["path"] == "bracket@A.stl" and new_rec["stl"]["sha256"] == json.loads(rec.read_text())["stl"]["sha256"]
    code, doc, _, _ = run(capsys, "cad", "check", str(dest), "--record", str(tmp_path / "out" / "bracket@A.cad.json"))
    assert code == 0, doc["findings"]  # the promoted record recomputes the same build id from its new folder


def test_promote_on_a_fail_leaves_the_destination(tmp_path, capsys):
    part = stl(tmp_path / "bracket.stl", box((0, 0, 0), (20, 10, 5)))
    dest = tmp_path / "bracket@A.stl"
    code, doc, _, _ = run(capsys, "cad", "check", str(part), "--expect-volume", "999", "--volume-tol", "0.5", "--promote", str(dest))
    assert code == 1 and not doc["promote"]["done"] and not dest.exists()


def test_promote_never_overwrites(tmp_path, capsys):
    part = stl(tmp_path / "bracket.stl", box((0, 0, 0), (20, 10, 5)))
    dest = tmp_path / "bracket@A.stl"
    dest.write_bytes(b"last good export")
    code, doc, _, _ = run(capsys, "cad", "check", str(part), "--promote", str(dest))
    assert code == 3 and "new file" in doc["error"]
    assert dest.read_bytes() == b"last good export"
