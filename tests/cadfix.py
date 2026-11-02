"""Helpers for the `shambu cad` tests: write meshes, records and specs in a tmp folder, and run the CLI once."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from shambu import cli
from shambu.cad import verbs
from shambu.mesh import write_stl


def stl(path: Path, facets: Sequence) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_stl(path, facets)


def write_record(stl_path: Path, part: str = "bracket", rev: str = "A", inputs: Sequence[Path] = (), **over) -> Path:
    """Stamp `stl_path` with `shambu cad stamp` (through the verb), then patch the record's sections with `over`
    (e.g. cad={"volume_mm3": ...}). Returns the record path."""
    verbs.stamp(verbs.StampArgs(str(stl_path), part, rev, tuple(str(p) for p in inputs)))
    path = stl_path.with_name(stl_path.stem + ".cad.json")
    data = json.loads(path.read_text())
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(data.get(k), dict):
            data[k].update(v)
        else:
            data[k] = v
    path.write_text(json.dumps(data))
    return path


def spec(path: Path, doc: dict) -> Path:
    path.write_text(json.dumps({"schema": "shambu.cad/1", **doc}))
    return path


def run(capsys, *argv: str) -> tuple[int, dict | None, str, str]:
    """(exit code, the JSON document or None, stdout, stderr) of `shambu --json ARGV`."""
    code = cli.main(["--json", *argv])
    out, err = capsys.readouterr()
    doc = json.loads(out) if out.strip() else None
    return code, doc, out, err


def codes(doc: dict) -> set[str]:
    return {f["code"] for f in doc["findings"]}


def fails(doc: dict) -> set[str]:
    return {f["code"] for f in doc["findings"] if f["severity"] == "FAIL"}
