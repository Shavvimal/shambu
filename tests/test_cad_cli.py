"""shambu cad at the command line: one JSON document per call, exit codes 0 to 3, help and the optional extra."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from cadfix import run, spec, stl
from meshes import box
from shambu import cli


def _box(tmp_path):
    return stl(tmp_path / "b.stl", box((0, 0, 0), (10, 10, 10)))


def test_cad_help_without_the_extra(capsys, monkeypatch):
    for mod in ("numpy", "scipy", "manifold3d"):
        monkeypatch.setitem(sys.modules, mod, None)
    with pytest.raises(SystemExit) as e:
        cli.main(["cad", "--help"])
    out, _ = capsys.readouterr()
    assert e.value.code == 0 and "slow reference path" in " ".join(out.split()) and "stamp" in out


@pytest.mark.parametrize(
    "argv",
    [
        ["cad", "check"],  # no file
        ["cad", "check", "a.stl", "--no-cull"],  # a sweep flag on a stdlib verb
        ["cad", "fit", "a.stl", "--hole", "h1:M3:1,2:0,0,1"],  # a malformed tuple
        ["cad", "clearance"],  # no spec
        ["cad", "stamp", "a.stl"],  # no --part
    ],
)
def test_usage_errors_exit_2(capsys, argv):
    code = cli.main(["--json", *argv])
    out, _ = capsys.readouterr()
    assert code == 2 and "error" in json.loads(out)


def test_unreadable_file_exit_3(tmp_path, capsys):
    bad = tmp_path / "bad.stl"
    bad.write_bytes(b"\x00" * 10)
    code, doc, _, _ = run(capsys, "cad", "check", str(bad))
    assert code == 3 and "error" in doc


def test_every_stdlib_verb_prints_one_document(tmp_path, capsys):
    part = _box(tmp_path)
    for argv in (
        ["cad", "stamp", str(part), "--part", "b"],
        ["cad", "check", str(part)],
        ["cad", "features", str(part)],
        ["cad", "fit", str(part), "--hole", "h1:M3-clearance-medium:5,5,10:0,0,-1"],
        ["cad", "diff", str(part), str(part)],
    ):
        code, doc, out, _ = run(capsys, *argv)
        assert json.loads(out) == doc and doc["schema"] == "shambu.cad.result/1" and code in (0, 1)


def test_text_output_ends_with_the_gate_line(tmp_path, capsys):
    code = cli.main(["cad", "check", str(_box(tmp_path))])
    out, _ = capsys.readouterr()
    assert code == 0 and out.strip().splitlines()[-1].startswith("cad check: pass (0 FAIL")


def test_geom_missing_exit_3(tmp_path, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "manifold3d", None)
    stl(tmp_path / "a.stl", box((0, 0, 0), (10, 10, 10)))
    stl(tmp_path / "b.stl", box((20, 0, 0), (30, 10, 10)))
    sp = spec(
        tmp_path / "cad.json",
        {
            "parts": [{"name": "a", "mesh": "a.stl"}, {"name": "b", "mesh": "b.stl"}],
            "rules": [{"id": "c", "type": "clearance", "a": "part:a", "b": "part:b"}],
        },
    )
    code, doc, out, _ = run(capsys, "cad", "clearance", "--spec", str(sp))
    assert code == 3 and "GeomMissing" in doc["error"] and "shambu[geom]" in doc["error"] and out.count("\n") == 1
    code = cli.main(["cad", "gate", str(sp)])
    _, err = capsys.readouterr()
    assert code == 3 and "uv tool install --force 'shambu[geom] @ git+https://github.com/Shavvimal/shambu@v" in err


def test_geom_not_imported_at_collection(tmp_path):
    """`cad check` and `--help` must not import the extra (run in a fresh interpreter: other tests may load it)."""
    part = _box(tmp_path)
    code = (
        "import sys\n"
        "from shambu import cli\n"
        f"assert cli.main(['--json', 'cad', 'check', {str(part)!r}]) == 0\n"
        "bad = [m for m in ('numpy', 'scipy', 'manifold3d') if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr


def test_cad_package_stays_off_cad_programs():
    """The gates read files; they never start Node, an MCP server or a CAD kernel (rubric 2 and 22)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "shambu" / "cad"
    hits = [
        f"{p.name}:{i}"
        for p in root.rglob("*.py")
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if any(w in line for w in ("subprocess", "socket", "npx", "import mcp"))
    ]
    assert hits == []
