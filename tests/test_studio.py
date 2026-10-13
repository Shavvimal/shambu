"""studio.py: the CliCommand builder rules, return codes and RunResult. No binary is run."""

from pathlib import Path

import pytest

from shambu.errors import StudioError
from shambu.studio import CliCommand, MeshInfo, RunResult, signed


def test_no_action_raises():
    cmd = CliCommand().load_settings("/tmp/m.json").setting("curr_bed_type", "Cool Plate")
    with pytest.raises(StudioError, match="no action"):
        cmd.args()


def test_no_check_goes_before_slice():
    args = CliCommand().option("slice", 0).option("no-check").option("outputdir", "/tmp/o").args()
    assert args.index("--no-check") < args.index("--slice=0")
    assert args[-1] == "--slice=0"


def test_slice_helper_orders_pre_slice_actions():
    args = CliCommand().slice(plate=2, max_seconds=60).args()
    assert args == ["--no-check", "--mstpp=60", "--slice=2"]


def test_bool_flags_use_equals():
    args = CliCommand().flag("allow-rotations", False).flag("min_save").args()
    assert "--allow-rotations=0" in args
    assert "--min-save=1" in args
    assert "0" not in args and "1" not in args


def test_export_3mf_with_path_raises():
    with pytest.raises(StudioError, match="bare file name"):
        CliCommand().export_3mf("out/plate.3mf", "/tmp")


def test_export_3mf_joins_outputdir_as_separate_values(tmp_path):
    args = CliCommand().export_3mf("plate one.3mf", tmp_path).args()
    i = args.index("--export-3mf")
    assert args[i + 1] == "plate one.3mf"
    assert args[args.index("--outputdir") + 1] == str(tmp_path.resolve())


def test_vector_setting_joins_with_comma():
    args = CliCommand().setting("nozzle_volume_type", ["Standard", "Standard"]).option("info").args()
    assert "--nozzle-volume-type=Standard,Standard" in args


def test_load_settings_joins_absolute_paths_with_semicolon(tmp_path):
    args = CliCommand().load_settings(tmp_path / "m.json", tmp_path / "p.json").option("info").args()
    i = args.index("--load-settings")
    assert args[i + 1] == f"{tmp_path.resolve() / 'm.json'};{tmp_path.resolve() / 'p.json'}"


def test_inputs_come_last():
    cmd = CliCommand(inputs=["/tmp/a.stl"]).option("info")
    assert cmd.args()[-1] == "/tmp/a.stl"


@pytest.mark.parametrize(("status", "code"), [(0, 0), (239, -17), (251, -5), (127, 127), (124, 124)])
def test_signed(status, code):
    assert signed(status) == code


def test_run_result_ok_needs_output(tmp_path):
    out = tmp_path / "x.3mf"
    rr = RunResult(["b"], 0, 0, 1.0, tmp_path / "l.log", None, out)
    assert not rr.ok
    out.write_bytes(b"x")
    assert rr.ok
    assert "written" in rr.summary()


def test_run_result_meaning():
    rr = RunResult(["b"], 239, -17, 1.0, Path("l.log"), None)
    assert "compatible_printers" in rr.meaning()
    assert not rr.ok
    assert RunResult(["b"], 1, -999, 0, Path("l"), None).meaning() == "return code -999"


def test_mesh_info_helpers():
    m = MeshInfo(Path("a.stl"), (10, 20, 5), (0, 0, 1), (10, 20, 6), True, 12, 1, 1000.0)
    assert m.centre_xy == (5, 10)
    assert not m.on_bed()
    assert "manifold yes" in m.describe()


# ---------------------------------------------------------------------------- StudioCLI against a fake binary
FAKE_BINARY = """#!/bin/sh
INFO='size_x = 10
size_y = 20
size_z = 5
min_x = 0
max_x = 10
min_y = 0
max_y = 20
min_z = 0
max_z = 5
manifold = yes
number_of_facets = 12'
prev=""
for a in "$@"; do
  if [ "$prev" = "--export-settings" ]; then OUT="$a"; fi
  prev="$a"
done
case "$FAKE_MODE" in
  sleep) exec sleep 10 ;;
  info_ok) echo "$INFO" ;;
  info_fail) echo "$INFO"; echo "boom: model unreadable"; exit 250 ;;
  info_bad) echo "$INFO" | sed 's/size_x = 10/size_x = ten/' ;;
  settings_ok) echo '{"layer_height": "0.2"}' > "$OUT" ;;
  settings_fail) exit 239 ;;
  settings_list) echo '[1]' > "$OUT" ;;
esac
"""


@pytest.fixture
def cli(tmp_path):
    from shambu.studio import StudioCLI

    b = tmp_path / "BambuStudio"
    b.write_text(FAKE_BINARY)
    b.chmod(0o755)
    return StudioCLI(binary=b, scratch=tmp_path / "scratch")


@pytest.fixture
def stl(tmp_path):
    p = tmp_path / "a.stl"
    p.write_bytes(b"solid a\nendsolid a\n")
    return p


def test_info_parses(cli, stl, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "info_ok")
    m = cli.info(stl)
    assert (m.size, m.manifold, m.facets) == ((10.0, 20.0, 5.0), True, 12)


def test_info_timeout_is_studio_error(cli, stl, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "sleep")
    with pytest.raises(StudioError, match=r"timed out after 1 s.*--info"):
        cli.info(stl, timeout=1)


def test_info_nonzero_exit_is_studio_error_even_with_fields(cli, stl, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "info_fail")
    with pytest.raises(StudioError, match=r"(?s)exit -6.*boom: model unreadable"):
        cli.info(stl)


def test_info_bad_number_is_studio_error(cli, stl, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "info_bad")
    with pytest.raises(StudioError, match="bad number"):
        cli.info(stl)


def test_run_timeout_is_code_124(cli, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_MODE", "sleep")
    rr = cli.run(CliCommand().option("info"), name="t", outputdir=tmp_path / "o", timeout=1)
    assert (rr.code, rr.ok) == (124, False)
    assert "[timeout after 1 s]" in rr.log.read_text()


def test_export_settings_reads_fresh_file(cli, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_MODE", "settings_ok")
    assert cli.export_settings(CliCommand(), tmp_path / "s.json") == {"layer_height": "0.2"}


def test_export_settings_failure_never_returns_stale_file(cli, monkeypatch, tmp_path):
    out = tmp_path / "s.json"
    out.write_text('{"stale": true}')
    monkeypatch.setenv("FAKE_MODE", "settings_fail")
    with pytest.raises(StudioError, match=r"export-settings failed.*exit -17"):
        cli.export_settings(CliCommand(), out)
    assert not out.exists()


def test_export_settings_not_an_object(cli, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_MODE", "settings_list")
    with pytest.raises(StudioError, match="not a JSON object"):
        cli.export_settings(CliCommand(), tmp_path / "s.json")
