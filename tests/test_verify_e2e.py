"""`shambu verify` on a project the pipeline built: plan, export through the fake Studio, patch, then verify the export
against its spec. The fake copies the geometry fixture and writes what the real CLI writes into an export (the loaded
presets, the object settings, the tower keys: `conftest.write_cli_presets`, `write_cli_tower`), so the path from the
spec to the file is the pipeline's own. Offline: no Studio binary, the presets come from the synthetic library."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import MACHINE, PROCESS, FakeStudio, make_geo_3mf, write_box_stl
from shambu import cli
from shambu.build import BuildResult, Pipeline
from shambu.facade import Bambu
from shambu.spec import Spec
from shambu.threemf import PROJECT_SETTINGS, Project

OBJECT_RECIPES = ["tall-outer-brim", "snug-normal-supports", "dual-material-interface"]


@pytest.fixture(autouse=True)
def fake_app(monkeypatch, resolver):
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=FakeStudio(), resolver=resolver))


def spec_json(tmp_path: Path) -> dict:
    """A process recipe and `set`, two filaments, a supported part with three object recipes, a plain part on filament 2.
    The STL boxes match the geometry fixture's objects (body 30 x 20 x 10, label 10 x 10 x 2)."""
    body = write_box_stl(tmp_path / "stl" / "body.stl", (0, 0, 0), (30, 20, 10))
    label = write_box_stl(tmp_path / "stl" / "label.stl", (-5, -5, 0), (5, 5, 2))
    return {
        "name": "e2e",
        "outdir": str(tmp_path / "out"),
        "printer": MACHINE,
        "process": {
            "base": PROCESS,
            "name": "Sweeper",
            "recipes": ["two-filament-plate"],
            "set": {"wall_loops": "4", "sparse_infill_density": "25%"},
        },
        "filaments": [{"preset": "Test PLA", "colour": "#00AE42"}, {"preset": "Test PLA", "colour": "#FFFFFF"}],
        "bed_type": "Cool Plate",
        "plates": [
            {
                "name": "p",
                "parts": [
                    {"path": str(body), "recipes": OBJECT_RECIPES},
                    {"path": str(label), "filament": 2, "set": {"brim_type": "no_brim"}},
                ],
            }
        ],
    }


def build(tmp_path: Path, resolver, *, sliced: bool = False, process_base: str = PROCESS) -> tuple[BuildResult, Path]:
    """Build the spec through the fake export; returns the result and the spec file `shambu verify` reads."""
    (tmp_path / "stl").mkdir()
    d = spec_json(tmp_path)
    d["process"]["base"] = process_base
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(d), encoding="utf-8")
    fixture = make_geo_3mf(tmp_path / "fixture.3mf", sliced=sliced)
    pr = Project(str(fixture))  # the CLI names copy 1 of a file <stem>_1; the pipeline renames it to the spec key
    pr.rename_object("body", "body_1")
    pr.rename_object("label", "label_1")
    pr.save(str(fixture))
    studio = FakeStudio(
        fixture,
        cli_presets=True,
        sizes={"body.stl": (30.0, 20.0, 10.0), "label.stl": (10.0, 10.0, 2.0)},
        los={"label.stl": (-5.0, -5.0, 0.0)},
    )
    r = Pipeline(studio, resolver).build(Spec.from_json(d), do_slice=sliced)
    assert r.plan.ok and r.run is not None and r.run.ok, (r.plan.problems, r.run)
    assert r.verify_problems == []
    return r, spec_path


def verify(capsys, project: Path, spec_path: Path) -> tuple[int, list[str]]:
    code = cli.main(["verify", str(project), "--against", str(spec_path)])
    out, _ = capsys.readouterr()
    return code, out.strip().splitlines()


@pytest.mark.parametrize("sliced", [False, True], ids=["unsliced", "sliced"])
def test_built_export_verifies_against_its_spec(capsys, tmp_path, resolver, sliced):
    r, spec_path = build(tmp_path, resolver, sliced=sliced)
    assert any("support filament pinned to the part's own filament for body:support_filament" in w for w in r.plan.warnings)
    code, lines = verify(capsys, r.project, spec_path)
    assert code == 0, lines
    assert not [x for x in lines if x.startswith(("FAIL", "WARN"))], lines
    assert lines[-1].startswith("verify: the file matches the spec")
    # The INFO lines: the tower keys from planning (the spec sets none) and, sliced, the plate bed type. A sliced build
    # keeps the G-code, so it skips the bed type patch and the plate carries no bed type (build.Pipeline.verify
    # allows that too): the project bed type applies.
    allowed = ["tower key from planning"] + (["INFO plate 1 bed type (not set on the plate"] if sliced else [])
    infos = [x for x in lines if x.startswith("INFO")]
    assert all(any(a in x for a in allowed) for x in infos), "\n".join(lines)
    assert len(infos) == 6 + sliced, "\n".join(lines)
    assert Project(str(r.project)).is_sliced() == sliced
    body = next(o for o in Project(str(r.project)).summary()["objects"] if o["name"] == "body")
    assert body["settings"]["support_filament"] == "1"  # the auto-pin, which verify expects too
    assert body["settings"]["support_interface_filament"] == "2"  # the recipe's value, not pinned
    assert body["settings"]["brim_type"] == "outer_only"


def test_gui_reset_of_a_process_key_fails_naming_it(capsys, tmp_path, resolver):
    """A GUI open that reset `wall_loops` to the base: the value back to 2 and the key gone from the process list."""
    r, spec_path = build(tmp_path, resolver)
    pr = Project(str(r.project))
    ps = pr.project_settings
    assert "wall_loops" in ps["different_settings_to_system"][0].split(";")
    ps["wall_loops"] = "2"
    ps["different_settings_to_system"][0] = ";".join(k for k in ps["different_settings_to_system"][0].split(";") if k != "wall_loops")
    pr.write_entry(PROJECT_SETTINGS, (json.dumps(ps, indent=4) + "\n").encode())
    pr.save(str(r.project))
    code, lines = verify(capsys, r.project, spec_path)
    assert code == 1, lines
    fails = [x for x in lines if x.startswith("FAIL")]
    assert any(x.startswith("FAIL process wall_loops: expected 4, got 2") for x in fails), fails
    assert all("wall_loops" in x for x in fails), fails


def test_export_of_a_user_process_preset_verifies(capsys, tmp_path, resolver, profiles):
    """A spec on an installed user preset: the export names its system base and lists the user preset's override
    (`build._bookkeeping`), and verify expects exactly that, so the pristine export passes."""
    (profiles / "process" / "My walls.json").write_text(
        json.dumps({"name": "My walls", "from": "User", "inherits": PROCESS, "brim_width": "8"}), encoding="utf-8"
    )
    r, spec_path = build(tmp_path, resolver, process_base="My walls")
    ps = Project(str(r.project)).project_settings
    assert ps["print_settings_id"] == PROCESS and "brim_width" in ps["different_settings_to_system"][0].split(";")
    code, lines = verify(capsys, r.project, spec_path)
    assert code == 0, lines
    assert not [x for x in lines if x.startswith(("FAIL", "WARN"))], lines
