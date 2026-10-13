"""threemf.Project.save: atomic replace. A failed save leaves the original bytes and no temporary file."""

import builtins
import os
import stat
import zipfile

import pytest

from shambu import patches as X
from shambu.errors import ProjectError
from shambu.threemf import Project


def _fail_on_nth_writestr(monkeypatch, n: int) -> None:
    real = zipfile.ZipFile.writestr
    calls = {"n": 0}

    def writestr(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == n:
            raise OSError("disk full (simulated)")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "writestr", writestr)


def test_failed_in_place_save_keeps_original(project_file, monkeypatch):
    before = project_file.read_bytes()
    listing = sorted(os.listdir(project_file.parent))
    p = Project(str(project_file))
    p.set_bed_type("Textured PEI Plate")
    _fail_on_nth_writestr(monkeypatch, 4)  # midway through the entries
    with pytest.raises(OSError, match="disk full"):
        p.save(str(project_file))
    assert project_file.read_bytes() == before
    assert sorted(os.listdir(project_file.parent)) == listing  # no temporary file left behind


def test_save_never_truncates_destination_before_replace(project_file, monkeypatch):
    # The old save() opened out_path with 'wb' (truncating it) and then wrote: a full disk or a kill there
    # destroyed the original. Model it: opening the destination for writing truncates it and then fails.
    before = project_file.read_bytes()
    real_open = builtins.open

    def fake_open(file, mode="r", *args, **kwargs):
        if "w" in mode and os.path.abspath(file) == str(project_file):
            real_open(file, "wb").close()
            raise OSError("short write (simulated)")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    p = Project(str(project_file))
    p.set_bed_type("Textured PEI Plate")
    p.save(str(project_file))
    monkeypatch.undo()
    assert project_file.read_bytes() != before
    assert Project(str(project_file)).summary()["bed_type"] == "Textured PEI Plate"
    assert os.listdir(project_file.parent) == [project_file.name]


def test_failed_patchset_keeps_original(project_file, monkeypatch):
    before = project_file.read_bytes()
    _fail_on_nth_writestr(monkeypatch, 2)
    with pytest.raises(OSError):
        X.PatchSet().add(X.SetBedType("Textured PEI Plate")).apply(str(project_file))
    assert project_file.read_bytes() == before
    assert os.listdir(project_file.parent) == [project_file.name]


def test_crc_failure_keeps_original(project_file, monkeypatch):
    before = project_file.read_bytes()
    monkeypatch.setattr(zipfile.ZipFile, "testzip", lambda self: "Metadata/model_settings.config")
    with pytest.raises(ProjectError, match="CRC"):
        Project(str(project_file)).save(str(project_file))
    assert project_file.read_bytes() == before
    assert os.listdir(project_file.parent) == [project_file.name]


def test_save_to_new_path_and_mode_kept(project_file, tmp_path):
    os.chmod(project_file, 0o640)
    p = Project(str(project_file))
    p.set_bed_type("Textured PEI Plate")
    p.save(str(project_file))
    assert stat.S_IMODE(os.stat(project_file).st_mode) == 0o640
    out = tmp_path / "copy.3mf"
    p.save(str(out))
    assert stat.S_IMODE(os.stat(out).st_mode) & 0o600 == 0o600
    with zipfile.ZipFile(out) as z:
        assert z.testzip() is None
    assert sorted(os.listdir(tmp_path)) == ["copy.3mf", "fixture.3mf"]
