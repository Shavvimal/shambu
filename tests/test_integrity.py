"""File integrity checks (integrity.py) and how `inspect` and `verify` show them. Every fixture is built in code."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from conftest import SLICED_GCODE, FakeStudio, gcode_text, make_3mf, make_geo_3mf, md5_entry, png_bytes
from shambu import cli, integrity
from shambu import verify as verify_mod
from shambu.facade import Bambu
from shambu.findings import EXTERNAL, Finding
from shambu.threemf import Project


@pytest.fixture(autouse=True)
def fake_app(monkeypatch, resolver):
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=FakeStudio(), resolver=resolver))


def rewrite(path: Path, changes: dict[str, bytes | None]) -> Path:
    """The zip with each named entry replaced (bytes), removed (None) or appended (a new name)."""
    with zipfile.ZipFile(path) as z:
        entries = [(i, z.read(i.filename)) for i in z.infolist()]
    with zipfile.ZipFile(path, "w") as z:
        for info, data in entries:
            if info.filename in changes:
                new = changes[info.filename]
                if new is None:
                    continue
                data = new
            z.writestr(info, data)
        for name, data in changes.items():
            if data is not None and name not in {i.filename for i, _ in entries}:
                z.writestr(zipfile.ZipInfo(name, date_time=(2026, 10, 12, 0, 0, 0)), data)
    return path


def run(path: Path) -> integrity.IntegrityReport:
    return integrity.check(Project(str(path)))


def codes(rep: integrity.IntegrityReport) -> list[str]:
    return [f.code for f in rep.findings]


def only(rep: integrity.IntegrityReport, code: str) -> Finding:
    hits = [f for f in rep.findings if f.code == code]
    assert len(hits) == 1, codes(rep)
    return hits[0]


@pytest.fixture
def sliced(tmp_path) -> Path:
    return make_geo_3mf(tmp_path / "s.gcode.3mf", sliced=True)


# ---------------------------------------------------------------------------- clean file
def test_clean_sliced_file_has_no_finding(sliced):
    rep = run(sliced)
    assert rep.findings == [] and rep.not_applicable == []
    assert rep.plates[0]["md5_ok"] and rep.plates[0]["header_layers"] == 2 and rep.plates[0]["thumbnail"]["colour_type"] == 6


def test_inspect_leaves_the_file_unchanged_and_exits_0(sliced, capsys):
    before = sliced.read_bytes()
    assert cli.main(["--json", "inspect", str(sliced)]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["integrity"]["counts"] == {"FAIL": 0, "WARN": 0, "INFO": 0}
    assert sliced.read_bytes() == before


# ---------------------------------------------------------------------------- MD5
def test_md5_mismatch_is_warn(tmp_path):
    f = only(run(make_geo_3mf(tmp_path / "m.3mf", sliced=True, md5=b"abc")), "gcode.md5_mismatch")
    assert f.severity == "WARN" and f.evidence == "measured" and f.measure["got"] == "abc"


def test_lowercase_md5_is_format_info_only(tmp_path):
    rep = run(make_geo_3mf(tmp_path / "m.3mf", sliced=True, md5=md5_entry(SLICED_GCODE).lower()))
    f = only(rep, "gcode.md5_format")
    assert codes(rep) == ["gcode.md5_format"] and f.severity == "INFO" and f.evidence == "observed"


def test_md5_with_a_newline_is_format_info(tmp_path):
    f = only(run(make_geo_3mf(tmp_path / "m.3mf", sliced=True, md5=md5_entry(SLICED_GCODE) + b"\n")), "gcode.md5_format")
    assert f.evidence == EXTERNAL


def test_missing_md5_is_warn(sliced):
    rewrite(sliced, {"Metadata/plate_1.gcode.md5": None})
    assert only(run(sliced), "gcode.md5_missing").severity == "WARN"


# ---------------------------------------------------------------------------- G-code markers and time
def test_no_header_is_warn(tmp_path):
    g = gcode_text(header=False)
    assert only(run(make_geo_3mf(tmp_path / "g.3mf", sliced=True, gcode=g)), "gcode.no_header").severity == "WARN"


def test_layer_markers_short_of_the_header(tmp_path):
    g = gcode_text(layers=10, markers=9)
    f = only(run(make_geo_3mf(tmp_path / "g.3mf", sliced=True, gcode=g)), "gcode.layer_markers")
    assert f.measure == {"expected": 10, "got": 9}


def test_no_layer_notice_is_external_info(tmp_path):
    f = only(run(make_geo_3mf(tmp_path / "g.3mf", sliced=True, gcode=gcode_text(m991=False))), "gcode.no_layer_notice")
    assert f.severity == "INFO" and f.evidence == EXTERNAL


@pytest.mark.parametrize(("r_min", "drift"), [(21, False), (25, True)])
def test_prediction_against_the_first_m73_r(tmp_path, r_min, drift):
    # slice_info predicts 1234 s; the band is max(120 s, 5 %) = 120 s
    rep = run(make_geo_3mf(tmp_path / "g.3mf", sliced=True, gcode=gcode_text(r_min=r_min)))
    assert ("gcode.prediction_drift" in codes(rep)) is drift


# ---------------------------------------------------------------------------- thumbnails
@pytest.mark.parametrize(
    ("png", "code"),
    [
        (png_bytes(1, 1), "thumb.placeholder"),
        (png_bytes(64, 64, 2), "thumb.not_rgba"),
        (b"\x89PNG fake", "thumb.unreadable"),
        (None, "thumb.missing"),
    ],
)
def test_thumbnail_checks(tmp_path, png, code):
    rep = run(make_geo_3mf(tmp_path / "t.3mf", sliced=True, png=png))
    assert codes(rep) == [code] and rep.findings[0].severity == "INFO"


# ---------------------------------------------------------------------------- bed, slice warnings, flush
def test_plate_json_bed_against_the_project(tmp_path):
    f = make_geo_3mf(tmp_path / "b.3mf", sliced=True, plate_json=b'{"bed_type": "cool_plate"}', curr_bed_type="Textured PEI Plate")
    hit = only(run(f), "bed.plate_vs_project")
    assert hit.measure == {"expected": "textured_plate", "got": "cool_plate"} and hit.evidence == EXTERNAL


def test_unknown_plate_json_bed_id_is_info(tmp_path):
    f = make_geo_3mf(tmp_path / "b.3mf", sliced=True, plate_json=b'{"bed_type": "moon_plate"}')
    hit = only(run(f), "bed.plate_vs_project")
    assert hit.severity == "INFO" and "not a known plate id" in hit.message and hit.measure == {"got": "moon_plate"}


def test_slice_warning_keeps_level_and_code(tmp_path):
    extra = b'    <warning msg="filament overlaps" level="3" error_code="1000C001"/>\n'
    f = make_geo_3mf(tmp_path / "w.3mf", sliced=True, slice_extra=extra)
    hit = only(run(f), "slice.warning")
    assert hit.severity == "WARN" and hit.measure == {"msg": "filament overlaps", "level": "3", "error_code": "1000C001"}
    assert Project(str(f)).slice_info()[1]["warnings"] == ["filament overlaps"]


@pytest.mark.parametrize(
    ("filaments", "entries", "flagged"),
    [(3, 18, False), (3, 9, True), (2, 8, False), (2, 4, True)],  # 2 x 2 x 2 = 8 is right and not a perfect square
)
def test_flush_matrix_is_extruders_by_n_by_n(tmp_path, filaments, entries, flagged):
    f = make_geo_3mf(tmp_path / "f.3mf", filaments=filaments, flush_volumes_matrix=["0"] * entries)
    rep = run(f)
    assert ("flush.matrix_shape" in codes(rep)) is flagged


# ---------------------------------------------------------------------------- plates, unsliced
def test_second_plate_is_checked_and_named(tmp_path):
    f = make_geo_3mf(tmp_path / "p.3mf", sliced=True, plates=2)
    rewrite(f, {"Metadata/plate_2.gcode": gcode_text(), "Metadata/plate_2.gcode.md5": b"0" * 32, "Metadata/plate_2.png": png_bytes()})
    rep = run(f)
    assert [x["plate"] for x in rep.plates] == [1, 2]
    assert only(rep, "gcode.md5_mismatch").plate == 2


def test_unsliced_file_lists_the_gcode_checks_as_not_applicable(tmp_path):
    rep = run(make_geo_3mf(tmp_path / "u.3mf"))
    assert set(rep.not_applicable) == set(integrity.GCODE_CHECKS) and "flush" in rep.checked


# ---------------------------------------------------------------------------- verify and inspect surfaces
def test_verify_lines_never_fail(tmp_path):
    rep = verify_mod.Report("f.3mf", "spec.json")
    rep.lines += verify_mod.integrity_lines(run(make_geo_3mf(tmp_path / "m.3mf", sliced=True, md5=b"abc", png=None)))
    assert {x.level for x in rep.lines} == {"WARN", "INFO"} and rep.ok
    assert "1 other warning(s)" in rep.last_line()
    md5 = next(x for x in rep.lines if x.item == "integrity gcode.md5_mismatch")
    assert md5.got == "abc" and "[measured]" in md5.note


def test_inspect_text_shows_the_findings(tmp_path, capsys):
    f = make_geo_3mf(tmp_path / "m.3mf", sliced=True, md5=b"abc")
    assert cli.main(["inspect", str(f)]) == 0  # integrity never fails inspect
    out = capsys.readouterr().out
    assert "integrity: 1 WARN, 0 INFO" in out and "WARN gcode.md5_mismatch plate 1" in out


def test_no_integrity_skips_the_checks(tmp_path, capsys, monkeypatch):
    def boom(project):
        raise AssertionError("integrity.check ran")

    monkeypatch.setattr(integrity, "check", boom)
    f = make_geo_3mf(tmp_path / "m.3mf", sliced=True, md5=b"abc")
    assert cli.main(["--json", "inspect", str(f), "--no-integrity"]) == 0
    assert "integrity" not in json.loads(capsys.readouterr().out)


def test_external_evidence_never_fails(tmp_path):
    files = [
        make_geo_3mf(tmp_path / "a.3mf", sliced=True, md5=md5_entry(SLICED_GCODE) + b"\n", gcode=SLICED_GCODE),
        make_geo_3mf(tmp_path / "b.3mf", sliced=True, gcode=gcode_text(m991=False), png=png_bytes(1, 1, 2)),
        make_geo_3mf(tmp_path / "c.3mf", sliced=True, plate_json=b'{"bed_type": "moon_plate"}'),
        make_3mf(tmp_path / "d.3mf", sliced=True, md5=b"abc"),
    ]
    seen = [f for p in files for f in run(p).findings]
    external = [f for f in seen if f.evidence == EXTERNAL]
    assert external and all(f.severity in ("INFO", "WARN") for f in external)
    assert all(f.severity != "FAIL" for f in seen)


# ---------------------------------------------------------------------------- review fixes
def test_declared_sliced_plate_without_gcode_is_warn(sliced):
    rewrite(sliced, {"Metadata/plate_1.gcode": None, "Metadata/plate_1.gcode.md5": None})
    hit = only(run(sliced), "gcode.missing")
    assert hit.severity == "WARN" and hit.plate == 1


def test_truncated_ihdr_is_unreadable(tmp_path):
    png = png_bytes(64, 64)
    assert integrity.png_header(png) == (64, 64, 6)
    assert integrity.png_header(png[:26]) is None
    assert integrity.png_header(png[:8] + b"\x00\x00\x00\x0c" + png[12:]) is None
