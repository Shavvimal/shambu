"""cli.main: subcommands, --json output, exit codes. Bambu() is replaced by a facade over fakes."""

import json
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from conftest import FakeStudio, make_3mf
from shambu import cli, recipes
from shambu import studio as studio_mod
from shambu.errors import StudioError
from shambu.facade import Bambu


@pytest.fixture(autouse=True)
def fake_app(monkeypatch, resolver):
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=FakeStudio(), resolver=resolver))


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def one_json(out: str):
    return json.loads(out)  # raises if stdout holds anything but one JSON document


def test_recipes_json(capsys):
    code, out, _ = run(capsys, "--json", "recipes")
    assert code == 0
    assert [r["name"] for r in one_json(out)] == [r.name for r in recipes.RECIPES]


def test_recipes_scope_text(capsys):
    code, out, _ = run(capsys, "recipes", "--scope", "object")
    assert code == 0
    assert len(out.strip().splitlines()) == sum(r.scope == "object" for r in recipes.RECIPES)


def test_spec_example(capsys):
    code, out, _ = run(capsys, "spec")
    assert code == 0
    assert '"printer"' in out


def test_spec_example_json(capsys):
    _, out, _ = run(capsys, "--json", "spec")
    one_json(out)


def write_spec(tmp_path, **over):
    d = {
        "name": "s",
        "outdir": str(tmp_path),
        "printer": "P",
        "process": {"base": "B"},
        "filaments": [{"preset": "F"}],
        "plates": [{"name": "p", "parts": [{"path": "/a.stl"}]}],
    }
    d.update(over)
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(d))
    return p


def test_spec_validate_ok(capsys, tmp_path):
    code, out, _ = run(capsys, "--json", "spec", str(write_spec(tmp_path)))
    assert (code, one_json(out)) == (0, {"ok": True, "problems": [], "warnings": []})


def test_spec_validate_problems_exit_1(capsys, tmp_path):
    code, out, _ = run(capsys, "--json", "spec", str(write_spec(tmp_path, bed_type="Glass")))
    assert code == 1
    assert one_json(out)["ok"] is False


def test_spec_unreadable_exit_1(capsys, tmp_path):
    """A bad spec is the user's input failing a check (1), not an environment error (3)."""
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    code, out, _ = run(capsys, "--json", "spec", str(p))
    assert code == 1
    assert "BuildError" in one_json(out)["error"]


def test_inspect_json(capsys, project_file):
    code, out, _ = run(capsys, "--json", "inspect", str(project_file))
    s = one_json(out)
    assert code == 0
    assert [o["name"] for o in s["objects"]] == ["body", "label"]


def test_inspect_text(capsys, project_file):
    code, out, _ = run(capsys, "inspect", str(project_file))
    assert code == 0
    assert "object label" in out and "unsliced" in out


def test_inspect_not_bambu_exit_3(capsys, tmp_path):
    p = make_3mf(tmp_path / "plain.3mf", with_model_settings=False)
    code, out, err = run(capsys, "inspect", str(p))
    assert code == 3
    assert "ProjectError" in err and out == ""


def test_inspect_not_a_zip_exit_3(capsys, tmp_path):
    p = tmp_path / "x.3mf"
    p.write_text("hello")
    assert run(capsys, "inspect", str(p))[0] == 3


def test_patch_dry_run_does_not_write(capsys, project_file):
    before = project_file.read_bytes()
    code, out, _ = run(capsys, "--json", "patch", str(project_file), "--bed-type", "Textured PEI Plate", "--pause", "1:4.2", "--dry-run")
    assert code == 0
    assert one_json(out)["would_patch"] == ["bed type Textured PEI Plate (project and every plate)", "plate 1: pause at z 4.2"]
    assert project_file.read_bytes() == before


def test_patch_writes_to_out(capsys, project_file, tmp_path):
    out_file = tmp_path / "o.3mf"
    code, out, _ = run(
        capsys,
        "--json",
        "patch",
        str(project_file),
        "-o",
        str(out_file),
        "--set",
        "body:wall_loops=6",
        "--extruder",
        "label/label_text:1",
        "--range",
        "body:0-0.6:extruder=2,wall_loops=3",
        "--rename-plate",
        "1:jaws",
    )
    assert code == 0
    assert len(one_json(out)["patched"]) == 4
    assert zipfile.ZipFile(out_file).testzip() is None


def test_patch_nothing_exit_2(capsys, project_file):
    assert run(capsys, "patch", str(project_file))[0] == 2


def test_patch_bad_value_exit_3(capsys, project_file):
    code, out, _ = run(capsys, "--json", "patch", str(project_file), "--bed-type", "Glass")
    assert code == 3
    assert "ProjectError" in one_json(out)["error"]


def test_usage_error_exit_2(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["no-such-command"])
    assert e.value.code == 2


def test_help_exit_0(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"])
    assert e.value.code == 0


def test_presets_list_json(capsys):
    code, out, _ = run(capsys, "--json", "presets", "list", "filament", "--printer", "Test X2D 0.4 nozzle")
    assert code == 0
    assert [r["name"] for r in one_json(out)] == ["Generic PETG @Test X2D", "Test PETG Basic", "Test PLA", "Test TPU"]


def test_recipes_work_without_studio_binary(capsys, monkeypatch):
    def missing(self):
        raise StudioError("Bambu Studio binary not found")

    monkeypatch.setattr(cli, "Bambu", Bambu)  # the real factory
    monkeypatch.setattr(studio_mod.StudioCLI, "__post_init__", missing)
    assert run(capsys, "recipes")[0] == 0


# ---------------------------------------------------------------------------- batch 1 (real-STL test findings)
def test_json_flag_after_the_subcommand(capsys):
    for argv in (("--json", "recipes"), ("recipes", "--json")):
        code, out, _ = run(capsys, *argv)
        assert code == 0 and [r["name"] for r in one_json(out)] == [r.name for r in recipes.RECIPES]
    code, out, _ = run(capsys, "presets", "list", "--json", "filament", "--printer", "Test X2D 0.4 nozzle")
    assert code == 0 and [r["name"] for r in one_json(out)] == ["Generic PETG @Test X2D", "Test PETG Basic", "Test PLA", "Test TPU"]
    assert cli.build_parser().parse_args(["recipes"]).json is False  # the subcommand flag never resets the default


@pytest.mark.parametrize("argv", [("--json", "info"), ("info", "--json")])
def test_info_json_either_side(capsys, tmp_path, argv):
    f = tmp_path / "a.stl"
    f.write_bytes(b"solid a\nendsolid a\n")
    code, out, _ = run(capsys, *argv, str(f))
    assert code == 0
    (entry,) = one_json(out)
    assert entry["facets"] == 12 and entry["warnings"] == []


class _FlakyStudio(FakeStudio):
    def info(self, path):
        if "bad" in str(path):
            raise StudioError(f"--info failed for {path}: exit -11 (unknown)")
        return super().info(path)


def test_info_continues_past_a_bad_file(capsys, monkeypatch, resolver, tmp_path):
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=_FlakyStudio(), resolver=resolver))
    code, out, _ = run(capsys, "--json", "info", "bad.3mf", "good.stl")
    assert code == 1
    bad, good = one_json(out)
    assert bad["file"] == "bad.3mf" and "StudioError" in bad["error"] and "exit -11" in bad["error"]
    assert good["path"].endswith("good.stl") and good["manifold"] is True
    code, out, _ = run(capsys, "info", "bad.3mf", "good.stl")
    assert code == 1
    lines = out.strip().splitlines()
    assert lines[0].startswith("bad.3mf: ERROR StudioError") and lines[1].startswith("good.stl:")


def test_info_missing_binary_stays_an_environment_error(capsys, monkeypatch):
    def missing(self):
        raise StudioError("Bambu Studio binary not found")

    monkeypatch.setattr(cli, "Bambu", Bambu)
    monkeypatch.setattr(studio_mod.StudioCLI, "__post_init__", missing)
    code, out, _ = run(capsys, "--json", "info", "a.stl", "b.stl")
    assert code == 3 and "binary not found" in one_json(out)["error"]


def test_info_warns_on_a_mesh_in_metres(capsys, monkeypatch, resolver):
    studio = FakeStudio(sizes={"jaw.stl": (0.022, 0.092, 0.048)})
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=studio, resolver=resolver))
    code, out, _ = run(capsys, "--json", "info", "jaw.stl")
    (entry,) = one_json(out)
    assert code == 0 and "probably in metres" in entry["warnings"][0]
    code, out, _ = run(capsys, "info", "jaw.stl")
    assert "WARNING: the whole mesh is under 1 mm" in out


INFO_BLOCK = """[{name}]
size_x = {sx}
size_y = 10.000000
size_z = 15.000000
min_x = {lo}
min_y = -5.000000
min_z = -7.500000
max_x = {hi}
max_y = 5.000000
max_z = 7.500000
number_of_facets = {facets}
manifold = {manifold}
number_of_parts =  1
volume = 1500.000000
"""


def test_info_sums_the_objects_of_a_multi_object_3mf():
    """Studio's --info prints one block per object of a 3MF (live: four blocks for a four-cube plate)."""
    out = "[2026-10-13 15:47:58.652765] [0x00000001f34c2180] [trace]   Initializing StaticPrintConfigs\n"
    out += INFO_BLOCK.format(name="p.3mf", sx="10.0", lo="-5.0", hi="5.0", facets=724, manifold="yes")
    out += INFO_BLOCK.format(name="p.3mf", sx="30.0", lo="-15.0", hi="15.0", facets=1076, manifold="no")
    out += INFO_BLOCK.format(name="p.3mf", sx="10.0", lo="-5.0", hi="5.0", facets=970, manifold="yes")
    m = studio_mod.parse_info(studio_mod.Path("p.3mf"), out)
    assert (m.facets, m.parts, m.objects) == (724 + 1076 + 970, 3, 3)
    assert m.volume_mm3 == pytest.approx(4500)
    assert m.min == (-15.0, -5.0, -7.5) and m.max == (15.0, 5.0, 7.5) and m.size == (30.0, 10.0, 15.0)
    assert m.manifold is False  # one object is not manifold
    assert "3 objects" in m.describe()
    single = studio_mod.parse_info(
        studio_mod.Path("a.stl"), INFO_BLOCK.format(name="a.stl", sx="10", lo="-5", hi="5", facets=12, manifold="yes")
    )
    assert (single.objects, single.facets, single.size) == (1, 12, (10.0, 10.0, 15.0))
    assert "objects" not in single.describe()
    with pytest.raises(StudioError, match="missing"):
        studio_mod.parse_info(studio_mod.Path("x"), "[trace] nothing here\n")


def test_inspect_text_shows_the_print_sequence(capsys, project_file):
    from shambu import patches as X

    X.PatchSet().add(X.SetPrintSequence("by object", 1)).apply(str(project_file))
    code, out, _ = run(capsys, "inspect", str(project_file))
    assert code == 0
    assert "print sequence not set (by layer)" in out.splitlines()[1]
    assert "plate 1 '': bed project default, map project default, print sequence by object" in out
    X.PatchSet().add(X.SetPrintSequence("by layer")).apply(str(project_file))
    _, out, _ = run(capsys, "inspect", str(project_file))
    assert "; print sequence by layer" in out.splitlines()[1]
    _, out, _ = run(capsys, "--json", "inspect", str(project_file))
    assert one_json(out)["print_sequence"] == "by layer"


@pytest.mark.parametrize("command", ["check", "build", "spec"])
def test_bad_recipe_is_one_error_document_and_exit_1(capsys, tmp_path, command):
    p = write_spec(tmp_path, process={"base": "B", "recipes": ["no-such-recipe"]})
    code, out, err = run(capsys, "--json", command, str(p))
    assert code == 1 and err == ""
    assert one_json(out) == {"error": "BuildError: unknown recipe 'no-such-recipe'; run `shambu recipes`"}
    code, out, _ = run(capsys, command, str(p))
    assert code == 1 and out.strip().splitlines() == ["BuildError: unknown recipe 'no-such-recipe'; run `shambu recipes`"]


@pytest.mark.parametrize("command", ["check", "build", "sweep"])
def test_unreadable_spec_is_exit_1(capsys, tmp_path, command):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    code, out, _ = run(capsys, "--json", command, str(p))
    assert code == 1 and "BuildError" in one_json(out)["error"]


def test_spec_missing_key_is_exit_1(capsys, tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"name": "s", "outdir": str(tmp_path), "printer": "P", "process": {}, "filaments": [], "plates": []}))
    code, out, _ = run(capsys, "--json", "check", str(p))
    assert code == 1 and "bad spec" in one_json(out)["error"]


def test_spec_short_at_is_exit_1(capsys, tmp_path):
    """`"at": [20]` raises IndexError in Spec.from_json: still one error document and exit 1."""
    p = write_spec(tmp_path, plates=[{"name": "p", "parts": [{"path": "/a.stl", "at": [20]}]}])
    code, out, err = run(capsys, "--json", "check", str(p))
    assert code == 1 and err == ""
    assert "bad spec (IndexError" in one_json(out)["error"]


def test_sweep_bad_plates_before_the_run_is_exit_1(capsys, tmp_path):
    """--by-object reads the plate list before any variant builds; a non-list there is one error document, exit 1."""
    p = write_spec(tmp_path, plates=5)
    code, out, err = run(capsys, "--json", "sweep", str(p), "--by-object", "--no-slice")
    assert code == 1 and err == ""
    assert "bad spec (TypeError" in one_json(out)["error"]


def test_sweep_structurally_bad_spec_fails_each_variant(capsys, tmp_path):
    """`"process": {}` fails Spec.from_json in every variant: the sweep records each as failed, prints one document."""
    p = write_spec(tmp_path, process={})
    code, out, _ = run(capsys, "--json", "sweep", str(p), "--by-object", "--no-slice")
    j = one_json(out)
    assert code == 1 and [r["name"] for r in j] == ["baseline", "by-object"]
    assert all(r["ok"] is False and "bad spec for variant" in r["problems"][0] for r in j)


def test_check_without_the_binary_stays_exit_3(capsys, tmp_path, monkeypatch):
    def missing(self):
        raise StudioError("Bambu Studio binary not found")

    monkeypatch.setattr(cli, "Bambu", Bambu)
    monkeypatch.setattr(studio_mod.StudioCLI, "__post_init__", missing)
    code, out, _ = run(capsys, "--json", "check", str(write_spec(tmp_path)))
    assert code == 3 and "binary not found" in one_json(out)["error"]


def test_threshold_and_top_defaults_and_help(capsys):
    from shambu import mesh as M

    p = cli.build_parser()
    assert p.parse_args(["orient", "f.stl"]).threshold == M.DEFAULT_THRESHOLD == 60
    assert p.parse_args(["sweep", "s.json"]).threshold == 60
    with pytest.raises(SystemExit):
        cli.main(["orient", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "default 60, Studio's own; 45 is the cautious rule" in help_text
    assert "largest flat faces to try, on top of as-is and the six axis faces (default 12)" in help_text


# ---------------------------------------------------------------------------- doctor: version, timings, exit codes
@pytest.mark.parametrize(
    "direct_url, install, commit",
    [
        (
            {"url": "https://github.com/x/shambu", "vcs_info": {"vcs": "git", "requested_revision": "v0.4.0", "commit_id": "69519ac0123"}},
            "git tag v0.4.0",
            "69519ac",
        ),
        ({"url": "file:///src/shambu", "dir_info": {"editable": True}}, "editable checkout", None),
        ({"url": "file:///src/shambu", "dir_info": {}}, "local folder (snapshot)", None),
        ({"url": "https://example.org/shambu.whl", "archive_info": {}}, "other", None),
        (None, "other", None),
    ],
)
def test_install_kind_from_direct_url(direct_url, install, commit):
    from shambu.facade import install_kind

    assert install_kind(direct_url) == (install, commit)


def test_shambu_section_takes_the_package_version_and_compares():
    import shambu
    from shambu.facade import shambu_section

    d = shambu_section("9.9.9", {"url": "file:///x", "dir_info": {}})
    assert d["version"] == shambu.__version__
    assert (d["install"], d["expected"], d["match"]) == ("local folder (snapshot)", "9.9.9", False)
    assert shambu_section(None, {})["match"] is None
    assert shambu_section(shambu.__version__, {})["match"] is True


def test_doctor_json_starts_with_the_shambu_section(capsys):
    import shambu

    code, out, err = run(capsys, "--json", "doctor", "--no-printer", "--expect-version", shambu.__version__)
    j = one_json(out)
    assert code == 0
    assert list(j)[:2] == ["shambu", "studio"]
    assert set(j["shambu"]) == {"version", "package_path", "install", "commit", "expected", "match"}
    assert j["shambu"]["match"] is True and j["shambu"]["version"] == shambu.__version__
    assert j["studio"]["tested"] == "02.08.02.61" and j["studio"]["match"] is True
    assert set(j["timings_s"]) == {"studio", "profiles", "config", "machine"}
    assert "doctor: studio" in err and "doctor: machine" in err  # one progress line per step, on stderr


def test_doctor_version_mismatch_is_exit_3_after_every_section(capsys):
    code, out, _ = run(capsys, "doctor", "--no-printer", "--expect-version", "9.9.9")
    lines = out.splitlines()
    assert code == 3
    assert lines[0].endswith("; skill expects 9.9.9: MISMATCH")
    assert (
        lines[1] == 'Fix: uv tool install --reinstall "shambu @ git+https://github.com/Shavvimal/shambu@v9.9.9"   (or /shambu:setup update)'
    )
    assert lines[2].startswith("Bambu Studio 02.08.02.61 (tested 02.08.02.61) at ")
    assert any(ln.startswith("recipes: ") for ln in lines)  # the rest still printed
    code, out, _ = run(capsys, "--json", "doctor", "--no-printer", "--expect-version", "9.9.9")
    assert code == 3 and one_json(out)["shambu"]["match"] is False


def test_doctor_ok_line_and_no_expectation(capsys):
    import shambu

    code, out, _ = run(capsys, "doctor", "--no-printer", "--expect-version", shambu.__version__)
    assert code == 0 and out.splitlines()[0].endswith(f"; skill expects {shambu.__version__}: OK")
    code, out, _ = run(capsys, "doctor", "--no-printer")
    assert code == 0 and out.splitlines()[0].startswith(f"shambu {shambu.__version__} (")


@pytest.mark.parametrize("bad", ["0.4", "v0.4.0", "0.4.0-rc1", "latest"])
def test_doctor_malformed_expect_version_is_exit_2(capsys, bad):
    code, out, _ = run(capsys, "--json", "doctor", "--no-printer", "--expect-version", bad)
    assert code == 2 and "X.Y.Z" in one_json(out)["error"]


def test_doctor_redacts_the_ip_and_passes_the_budget(capsys, monkeypatch):
    from shambu.errors import PrinterError

    seen = {}

    def unreachable(self, *a, **k):
        seen["timeout"] = self.printer_timeout
        raise PrinterError("cannot connect to 10.1.2.3:8883: timed out")

    monkeypatch.setattr(Bambu, "loaded_filaments", unreachable)
    code, out, err = run(capsys, "--json", "doctor", "--ip", "10.1.2.3", "--printer-timeout", "4")
    j = one_json(out)
    assert code == 0  # an unread printer is a WARN, not an environment error
    assert j["printer"]["error"] == "PrinterError: cannot connect to <ip>:8883: timed out"
    assert "10.1.2.3" not in out
    assert seen["timeout"] == 4.0 and "printer" in j["timings_s"] and "doctor: printer" in err


def test_doctor_without_the_binary_still_prints_and_exits_3(capsys, monkeypatch, resolver):
    def missing(self):
        raise StudioError("Bambu Studio binary not found")

    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(resolver=resolver))
    monkeypatch.setattr(studio_mod.StudioCLI, "__post_init__", missing)
    code, out, _ = run(capsys, "--json", "doctor", "--no-printer", "--expect-version", "9.9.9")
    j = one_json(out)
    assert code == 3 and "binary not found" in j["studio"]["error"]
    assert next(iter(j)) == "shambu" and (j["shambu"]["expected"], j["shambu"]["match"]) == ("9.9.9", False)
    code, out, _ = run(capsys, "doctor", "--no-printer", "--expect-version", "9.9.9")
    assert code == 3 and out.splitlines()[0].endswith("MISMATCH") and out.splitlines()[1].startswith("Fix: ")


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "0", "-1", "1e300"])
def test_doctor_printer_timeout_must_be_finite_and_positive(capsys, bad):
    code, out, _ = run(capsys, "--json", "doctor", "--no-printer", f"--printer-timeout={bad}")
    assert code == 2 and "finite number of seconds" in one_json(out)["error"]


def test_doctor_expect_version_with_a_trailing_newline_is_exit_2(capsys):
    import shambu

    code, out, _ = run(capsys, "--json", "doctor", "--no-printer", "--expect-version", shambu.__version__ + "\n")
    assert code == 2 and "X.Y.Z" in one_json(out)["error"]


def test_install_kind_names_a_branch_as_a_revision():
    from shambu.facade import install_kind

    assert install_kind({"vcs_info": {"vcs": "git", "requested_revision": "main"}})[0] == "git revision main"


def test_malformed_info_plist_is_a_studio_error(tmp_path, capsys, monkeypatch, resolver):
    app = tmp_path / "BambuStudio.app" / "Contents"
    (app / "MacOS").mkdir(parents=True)
    binary = app / "MacOS" / "BambuStudio"
    binary.write_text("")
    st = studio_mod.StudioCLI.__new__(studio_mod.StudioCLI)
    st.binary = binary
    (app / "Info.plist").write_bytes(b"not a plist at all")
    with pytest.raises(StudioError, match="cannot read the Studio version"):
        st.version()
    (app / "Info.plist").write_bytes(b'<?xml version="1.0"?><plist version="1.0"><dict></dict></plist>')
    with pytest.raises(StudioError, match="KeyError"):
        st.version()
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=st, resolver=resolver))
    code, out, _ = run(capsys, "--json", "doctor", "--no-printer")
    j = one_json(out)
    assert code == 3 and "cannot read the Studio version" in j["studio"]["error"] and "recipes" in j


@pytest.mark.parametrize(
    "result, pids",
    [
        (lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "12 34\n", ""), [12, 34]),
        (lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", ""), []),  # no process: an ordinary negative
        (lambda argv, **kw: subprocess.CompletedProcess(argv, 3, "", "pgrep: bad"), None),
        (lambda argv, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, 2)), None),
        (lambda argv, **kw: (_ for _ in ()).throw(FileNotFoundError("pgrep")), None),
    ],
)
def test_running_pids_unknown_is_none(monkeypatch, caplog, result, pids):
    st = studio_mod.StudioCLI.__new__(studio_mod.StudioCLI)
    st.binary = Path("/Applications/BambuStudio.app/Contents/MacOS/BambuStudio")
    monkeypatch.setattr(studio_mod.subprocess, "run", result)
    with caplog.at_level("WARNING", logger="shambu.studio"):
        assert st.running_pids() == pids
    assert bool(caplog.records) == (pids is None)


def test_doctor_shows_unknown_pids(capsys, monkeypatch):
    from conftest import FakeStudio

    monkeypatch.setattr(FakeStudio, "running_pids", lambda self: None)
    code, out, _ = run(capsys, "doctor", "--no-printer")
    assert code == 0 and "(running pids: unknown (pgrep failed))" in out.splitlines()[1]


@pytest.mark.parametrize("argv, timeout", [([], 15.0), (["--wait", "20"], 27.0)])
def test_printer_command_read_timeout_follows_wait(capsys, monkeypatch, argv, timeout):
    from shambu.errors import PrinterError

    seen = {}

    class Svc:
        def status(self):
            raise PrinterError("no printer in tests")

    def fake_printer(self, *a, **k):
        seen["timeout"] = self.printer_timeout
        return Svc()

    monkeypatch.setattr(Bambu, "printer", fake_printer)
    assert cli.main(["printer", "status", "--ip", "10.0.0.9", *argv]) == 3
    assert seen["timeout"] == timeout


def test_direct_url_without_a_distribution_or_with_bad_json(monkeypatch):
    from importlib import metadata

    from shambu import facade

    def missing(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(facade.metadata, "distribution", missing)
    assert facade._direct_url() is None

    class Dist:
        def read_text(self, name):
            return "{not json"

    monkeypatch.setattr(facade.metadata, "distribution", lambda name: Dist())
    assert facade._direct_url() is None
    assert facade.shambu_section(None)["install"] == "other"


def test_install_kind_names_the_installer_only_with_proof(monkeypatch, tmp_path):
    from shambu import facade

    vcs = {"vcs_info": {"vcs": "git", "requested_revision": "v0.4.0"}}
    assert facade.install_kind(vcs, "uv tool")[0] == "uv tool, git tag v0.4.0"
    assert facade.install_kind(vcs)[0] == "git tag v0.4.0"
    monkeypatch.setattr(facade.sys, "prefix", str(tmp_path))
    assert facade._installer() is None
    (tmp_path / "uv-receipt.toml").write_text("[tool]\n")
    assert facade._installer() == "uv tool"


def test_inspect_flags_an_export_the_cli_cannot_reslice(capsys, tmp_path):
    f = make_3mf(tmp_path / "u.3mf", extruder_nozzle_stats=[])
    code, out, _ = run(capsys, "inspect", str(f))
    assert code == 0
    assert 'not re-sliceable by the CLI: extruder_nozzle_stats [] (Studio exits -100 "No valid nozzle found")' in out


def test_patch_repair_nozzle_stats_flag(capsys, tmp_path):
    f = make_3mf(tmp_path / "u.3mf", extruder_nozzle_stats=[], extruder_max_nozzle_count=["1", "1"])
    code, out, _ = run(capsys, "--json", "patch", str(f), "--repair-nozzle-stats")
    assert code == 0 and one_json(out)["patched"][0].startswith("extruder_nozzle_stats")
    code, out, _ = run(capsys, "inspect", str(f))
    assert "not re-sliceable" not in out


def test_spec_lists_unknown_keys_as_warnings(capsys, tmp_path, stls):
    spec = {
        "name": "s",
        "outdir": str(tmp_path),
        "printer": "P",
        "process": {"base": "B"},
        "filaments": [{"preset": "F"}],
        "plates": [{"name": "p", "parts": [{"path": str(stls["body"]), "brim_margn": 2}]}],
    }
    f = tmp_path / "s.json"
    f.write_text(json.dumps(spec))
    code, out, _ = run(capsys, "--json", "spec", str(f))
    assert code == 0 and one_json(out)["warnings"][0].startswith("plate 1 part 1: unknown key 'brim_margn'")


# ---------------------------------------------------------------------------- project edits, slice, studio (edits.py)
def _geo(tmp_path, **kw):
    from conftest import make_geo_3mf

    return make_geo_3mf(tmp_path / "geo.3mf", **kw)


def test_patch_replace_mesh_prints_the_box_report(capsys, tmp_path):
    from conftest import write_box_stl

    path = _geo(tmp_path)
    stl = write_box_stl(tmp_path / "body-v2.stl", (0, 0, 0), (30, 20, 10), fine=True)
    code, out, _ = run(capsys, "patch", str(path), "-o", str(tmp_path / "o.3mf"), "--replace-mesh", f"body={stl}")
    assert code == 0
    assert "size (30.000, 20.000, 10.000) -> (30.000, 20.000, 10.000) mm" in out and "triangles 12 -> 24" in out
    assert "world min corner (40.000, 50.000, 0.000) -> (40.000, 50.000, 0.000)" in out


def test_patch_replace_mesh_size_change_is_exit_1_and_keeps_the_file(capsys, tmp_path):
    from conftest import write_box_stl

    path = _geo(tmp_path)
    raw = path.read_bytes()
    stl = write_box_stl(tmp_path / "wide.stl", (0, 0, 0), (32, 20, 10))
    code, out, _ = run(capsys, "--json", "patch", str(path), "--replace-mesh", f"body={stl}")
    assert code == 1 and "delta (2.000" in one_json(out)["error"]
    assert path.read_bytes() == raw


def test_patch_move_dry_run_shows_corner_and_centre(capsys, tmp_path):
    path = _geo(tmp_path)
    raw = path.read_bytes()
    code, out, _ = run(capsys, "--json", "patch", str(path), "--dry-run", "--move", "body=+5,-5")
    doc = one_json(out)
    assert code == 0 and path.read_bytes() == raw
    assert any("(45.000, 45.000) plate 1" in n and "centre (55.000, 60.000) -> (60.000, 55.000)" in n for n in doc["notes"])


def test_patch_move_needs_both_signs(capsys, tmp_path):
    code, out, _ = run(capsys, "patch", str(_geo(tmp_path)), "--move", "body=+5,5")
    assert code == 2 and "sign on both values" in out


def test_patch_tower_one_plate(capsys, tmp_path):
    path = _geo(tmp_path, plates=3)
    code, _out, _ = run(capsys, "--json", "patch", str(path), "--tower", "90,100@2")
    assert code == 0
    from shambu.threemf import Project

    assert Project(str(path)).project_settings["wipe_tower_x"] == ["208", "90", "208"]


def _with_settings(src, dest, ps):
    with zipfile.ZipFile(src) as z, zipfile.ZipFile(dest, "w") as w:
        for i in z.infolist():
            data = z.read(i.filename)
            if i.filename == "Metadata/project_settings.config":
                data = json.dumps(ps, indent=4).encode()
            w.writestr(i, data)
    return dest


def _scratch_3mf(tmp_path, src=None):
    """The canned scratch exports of a one-filament project: the new preset alone (normalised), then all slots."""
    from shambu.threemf import Project
    from test_edits import normalised_settings, scratch_settings

    src = src or _geo(tmp_path, filaments=1)
    ps = Project(str(src)).project_settings
    norm = _with_settings(src, tmp_path / "normalised.3mf", normalised_settings(ps))
    flush = _with_settings(src, tmp_path / "flush.3mf", scratch_settings(ps, 1))
    return src, [norm, flush, norm]


@dataclass
class FixturesStudio(FakeStudio):
    """FakeStudio that copies fixtures[i] on call i."""

    fixtures: list = field(default_factory=list)

    def run(self, cmd, **kw):
        i = len([c for c in self.calls if c[0] == "run"])
        self.fixture = self.fixtures[min(i, len(self.fixtures) - 1)]
        return super().run(cmd, **kw)


def test_patch_add_filament_with_studio(capsys, tmp_path, monkeypatch, resolver):
    src, scratch = _scratch_3mf(tmp_path)
    studio = FixturesStudio(fixtures=scratch)
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=studio, resolver=resolver))
    out_path = tmp_path / "two.3mf"
    code, out, _ = run(capsys, "--json", "patch", str(src), "-o", str(out_path), "--add-filament", "Test PETG Basic:#FCECD6@2")
    doc = one_json(out)
    assert code == 0, doc
    from shambu.threemf import Project

    ps = Project(str(out_path)).project_settings
    assert ps["filament_settings_id"] == ["Test PLA", "Test PETG Basic"] and ps["filament_map"] == ["1", "2"]
    assert ps["filament_self_index"] == ["1"] * 6 + ["2"] * 6
    assert any(n.startswith("verify slice: Studio sliced plate 1") for n in doc["notes"])
    runs = [c[1] for c in studio.calls if c[0] == "run"]
    assert len(runs) == 3 and all("--load-filaments" in r for r in runs[:2]) and "--slice=1" in runs[2]
    assert runs[0][runs[0].index("--load-filaments") + 1].endswith("filament-new.json")  # the new preset alone


def test_patch_add_filament_without_studio_warns(capsys, tmp_path, monkeypatch, resolver):
    src = _geo(tmp_path, filaments=1)

    def no_studio(self):
        raise StudioError("Bambu Studio binary not found")

    monkeypatch.setattr(Bambu, "studio_cli", no_studio)
    code, out, _ = run(capsys, "--json", "patch", str(src), "--add-filament", "Test PETG Basic:#FCECD6@2")
    doc = one_json(out)
    assert code == 0
    assert any("values not normalised by Studio" in n for n in doc["notes"])


def test_patch_add_filament_one_per_call(capsys, tmp_path):
    code, out, _ = run(capsys, "patch", str(_geo(tmp_path)), "--add-filament", "Test PLA", "--add-filament", "Test PLA")
    assert code == 2 and "one --add-filament per call" in out


def test_patch_add_filament_unknown_preset_is_exit_1(capsys, tmp_path):
    code, out, _ = run(capsys, "patch", str(_geo(tmp_path)), "--add-filament", "No Such Filament:#FFFFFF")
    assert code == 1 and "not found" in out


def test_build_no_check_without_slice_is_exit_2(capsys, tmp_path):
    code, out, _ = run(capsys, "build", str(write_spec(tmp_path)), "--no-check")
    assert code == 2 and "--no-check needs --slice" in out


def test_slice_verb_two_pass(capsys, tmp_path, monkeypatch, resolver):
    from test_edits import CONFLICT_LOG, SequenceStudio

    src = make_3mf(tmp_path / "in.3mf")
    sliced = make_3mf(tmp_path / "fixture.gcode.3mf", sliced=True)
    studio = SequenceStudio(sliced, codes=[-101, 0], logs=[CONFLICT_LOG, ""])
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=studio, resolver=resolver))
    code, out, _ = run(capsys, "--json", "slice", str(src), "--no-check")
    doc = one_json(out)
    assert code == 0 and doc["ok"]
    assert doc["output"] == str(tmp_path / "in.gcode.3mf")
    assert doc["checks_skipped"]["pair"] == ["a", "b"]
    assert "G-code path conflict check (exit -101)" in doc["checks_skipped"]["checks"]
    runs = [c[1] for c in studio.calls if c[0] == "run"]
    assert "--no-check" not in runs[0] and runs[1].index("--no-check") < runs[1].index("--slice=0")


def test_slice_verb_without_no_check_fails_on_a_conflict(capsys, tmp_path, monkeypatch, resolver):
    from test_edits import CONFLICT_LOG, SequenceStudio

    studio = SequenceStudio(None, codes=[-101], logs=[CONFLICT_LOG])
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=studio, resolver=resolver))
    code, out, _ = run(capsys, "slice", str(make_3mf(tmp_path / "in.3mf")), "--plate", "1")
    assert code == 1 and "exit -101 (G-code path conflicts)" in out
    assert len(studio.calls) == 1


def test_slice_verb_tower_leaves_the_input(capsys, tmp_path, monkeypatch, resolver):
    from test_edits import SequenceStudio

    src = _geo(tmp_path)
    raw = src.read_bytes()
    studio = SequenceStudio(make_3mf(tmp_path / "f.gcode.3mf", sliced=True, wipe_tower_x=["90"], wipe_tower_y=["100"]), codes=[0])
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=studio, resolver=resolver))
    code, out, _ = run(capsys, "--json", "slice", str(src), "--tower", "90,100", "-o", str(tmp_path / "o.gcode.3mf"))
    assert code == 0 and src.read_bytes() == raw
    assert any("wipe_tower_x ['90']" in n for n in one_json(out)["notes"])
    assert not list(tmp_path.glob(".o.gcode.3mf.shambu"))


def test_slice_verb_strict_tower_verify_refuses_an_export_without_the_spot(capsys, tmp_path, monkeypatch, resolver):
    from test_edits import SequenceStudio

    src = _geo(tmp_path)
    studio = SequenceStudio(make_3mf(tmp_path / "f.gcode.3mf", sliced=True), codes=[0])  # Studio "lost" the tower
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=studio, resolver=resolver))
    dest = tmp_path / "o.gcode.3mf"
    code, out, _ = run(capsys, "--json", "slice", str(src), "--tower", "90,100", "-o", str(dest))
    doc = one_json(out)
    assert code == 1 and not dest.exists()
    assert any(v.startswith("wipe_tower_x in file is") for v in doc["verify_problems"]), doc["verify_problems"]


def test_slice_verb_repairs_an_unsliced_cli_export_first(capsys, tmp_path, monkeypatch, resolver):
    """`slice --settings-from` and a bare `slice` take the RepairNozzleStats path: an export with extruder_nozzle_stats []
    would exit -100 on every CLI slice."""
    from test_edits import SequenceStudio

    src = make_3mf(tmp_path / "u.3mf", extruder_nozzle_stats=[], extruder_max_nozzle_count=["1", "1"])
    raw = src.read_bytes()
    studio = SequenceStudio(make_3mf(tmp_path / "f.gcode.3mf", sliced=True), codes=[0])
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=studio, resolver=resolver))
    from conftest import MACHINE, PROCESS

    spec = write_spec(tmp_path, printer=MACHINE, process={"base": PROCESS})
    code, out, _ = run(capsys, "--json", "slice", str(src), "--settings-from", str(spec), "-o", str(tmp_path / "o.gcode.3mf"))
    doc = one_json(out)
    assert code == 0 and src.read_bytes() == raw, doc
    assert any(n.startswith("repaired before slicing (extruder_nozzle_stats []") for n in doc["notes"]), doc["notes"]
    sliced_input = studio.calls[0][1][-1] if studio.calls else ""
    assert sliced_input.endswith("repaired.3mf"), studio.calls


def test_slice_verb_pins_prime_volume_in_both_passes(capsys, tmp_path, monkeypatch, resolver):
    from test_edits import CONFLICT_LOG, SequenceStudio

    src = make_3mf(tmp_path / "in.3mf", filament_prime_volume=["90", "120"])
    studio = SequenceStudio(make_3mf(tmp_path / "f.gcode.3mf", sliced=True), codes=[-101, 0], logs=[CONFLICT_LOG, ""])
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=studio, resolver=resolver))
    code, _, _ = run(capsys, "slice", str(src), "--no-check")
    assert code == 0
    runs = [c[1] for c in studio.calls if c[0] == "run"]
    assert len(runs) == 2
    for argv in runs:
        assert "--filament-prime-volume=90,120" in argv


def test_patch_add_filament_with_an_absent_old_preset(capsys, tmp_path, monkeypatch, resolver):
    from conftest import make_geo_3mf

    src = make_geo_3mf(tmp_path / "gone.3mf", filaments=1, filament_settings_id=["My Deleted PLA"])
    _, scratch = _scratch_3mf(tmp_path, src)
    studio = FixturesStudio(fixtures=scratch)
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=studio, resolver=resolver))
    code, out, _ = run(capsys, "--json", "patch", str(src), "-o", str(tmp_path / "o.3mf"), "--add-filament", "Test PETG Basic:#FCECD6@2")
    assert code == 0, out
    argv = studio.calls[1][1]
    loads = argv[argv.index("--load-filaments") + 1]
    assert loads.endswith("filament-1.json;" + loads.split(";")[1]) and loads.split(";")[1].endswith("filament-new.json")


def test_slice_refuses_the_input_as_output(capsys, tmp_path, monkeypatch, resolver):
    from test_edits import SequenceStudio

    src = make_3mf(tmp_path / "in.3mf")
    raw = src.read_bytes()
    (tmp_path / "alias.3mf").symlink_to(src)
    studio = SequenceStudio(make_3mf(tmp_path / "f.gcode.3mf", sliced=True), codes=[0])
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=studio, resolver=resolver))
    for out_arg in (str(src), str(tmp_path / "alias.3mf")):
        code, out, _ = run(capsys, "--json", "slice", str(src), "-o", out_arg)
        assert code == 2 and "is the input file" in one_json(out)["error"]
    assert src.read_bytes() == raw and studio.calls == []


def test_slice_replaces_the_output_only_after_success(capsys, tmp_path, monkeypatch, resolver):
    from test_edits import SequenceStudio

    src = make_3mf(tmp_path / "in.3mf")
    old = tmp_path / "out.gcode.3mf"
    old.write_bytes(b"previous output")
    studio = SequenceStudio(None, codes=[-102])
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=studio, resolver=resolver))
    code, _, _ = run(capsys, "slice", str(src), "-o", str(old))
    assert code == 1 and old.read_bytes() == b"previous output"


def _verify_studio(tmp_path, code: int, log: str | None):
    """FixturesStudio whose third call (the verify slice) exits `code` and leaves `log` (None: no log file)."""
    _, fixtures = _scratch_3mf(tmp_path)

    @dataclass
    class VerifyStudio(FixturesStudio):
        def run(self, cmd, **kw):
            rr = super().run(cmd, **kw)
            if len(self.calls) == 3:
                if log is None:
                    rr.log.unlink()
                else:
                    rr.log.write_text(log)
                rr = type(rr)(rr.cmd, code % 256, code, rr.seconds, rr.log, {"return_code": code}, rr.output)
            return rr

    return VerifyStudio(fixtures=fixtures)


@pytest.mark.parametrize("code, log", [(124, "$ fake\n[timeout after 1200 s]\n"), (0, None)], ids=["timeout", "no-log"])
def test_verify_slice_failure_leaves_the_destination(capsys, tmp_path, monkeypatch, resolver, code, log):
    src = _geo(tmp_path, filaments=1)
    raw = src.read_bytes()
    studio = _verify_studio(tmp_path, code, log)
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=studio, resolver=resolver))
    rc, out, _ = run(capsys, "--json", "patch", str(src), "--add-filament", "Test PETG Basic:#FCECD6@2")
    assert rc == 1 and "verify slice" in one_json(out)["error"]
    assert src.read_bytes() == raw
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


@pytest.mark.parametrize(
    "argv",
    [
        ["--move", "nope=1,1"],
        ["--move", "body=1,1@5"],
        ["--replace-mesh", "nope=x.stl"],
        ["--add-filament", "No Such Filament"],
        ["--add-filament", "Test PETG Basic:#FCECD6@5"],
        ["--tower", "120,30@9"],
    ],
    ids=["unknown-object", "plate-range", "replace-unknown", "unknown-preset", "bad-extruder", "tower-plate"],
)
def test_refused_edits_exit_1(capsys, tmp_path, argv):
    code, out, _ = run(capsys, "--json", "patch", str(_geo(tmp_path)), *argv)
    assert code == 1, out
    assert one_json(out)["error"].startswith("EditError")


@pytest.mark.parametrize(
    "argv",
    [
        ["patch", "{f}", "--tower", "nan,30"],
        ["patch", "{f}", "--tower", "120,30@oops"],
        ["patch", "{f}", "--move", "body=inf,3"],
        ["slice", "{f}", "--tower", "1,2@x"],
        ["studio", "--", "--slice", "oops", "x.3mf"],
    ],
    ids=["tower-nan", "tower-plate", "move-inf", "slice-tower", "studio-slice"],
)
def test_malformed_numbers_are_usage_errors(capsys, tmp_path, monkeypatch, argv):
    monkeypatch.chdir(tmp_path)
    f = str(_geo(tmp_path))
    code, out, _ = run(capsys, "--json", *[a.replace("{f}", f) for a in argv])
    assert code == 2
    assert "error" in one_json(out)


def test_slice_never_removes_a_folder_by_name(capsys, tmp_path, monkeypatch, resolver):
    from test_edits import SequenceStudio

    job = tmp_path / "job.slice"
    job.mkdir()
    src = make_3mf(job / "source.3mf")
    (job / "notes.txt").write_text("mine")
    studio = SequenceStudio(make_3mf(tmp_path / "f.gcode.3mf", sliced=True), codes=[0])
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=studio, resolver=resolver))
    code, _, _ = run(capsys, "slice", str(src), "-o", str(tmp_path / "job.gcode.3mf"))
    assert code == 0
    assert src.is_file() and (job / "notes.txt").read_text() == "mine"
    assert (tmp_path / "job.gcode.3mf").is_file()


def test_slice_keeps_the_old_output_when_the_export_does_not_read_back(capsys, tmp_path, monkeypatch, resolver):
    from test_edits import SequenceStudio

    src = make_3mf(tmp_path / "in.3mf")
    old = tmp_path / "out.gcode.3mf"
    old.write_bytes(b"previous good output")
    junk = tmp_path / "junk.3mf"
    junk.write_bytes(b"not a zip")
    studio = SequenceStudio(junk, codes=[0])  # exit 0, but the export is unreadable
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=studio, resolver=resolver))
    code, out, _ = run(capsys, "--json", "slice", str(src), "-o", str(old))
    assert code == 1 and "does not read back" in one_json(out)["error"]
    assert old.read_bytes() == b"previous good output"


def test_patch_add_filament_without_a_preset_colour_takes_the_next_default(capsys, tmp_path, monkeypatch):
    """BBL system presets carry no filament_colour: the next build.DEFAULT_COLOURS entry the slots do not use, and a WARN."""
    from shambu.build import DEFAULT_COLOURS
    from shambu.threemf import Project

    src = _geo(tmp_path, filaments=1, filament_colour=[DEFAULT_COLOURS[0]])

    def no_studio(self):
        raise StudioError("Bambu Studio binary not found")

    monkeypatch.setattr(Bambu, "studio_cli", no_studio)
    code, out, _ = run(capsys, "--json", "patch", str(src), "--add-filament", "Test PETG Basic@2")
    doc = one_json(out)
    assert code == 0, doc
    want = DEFAULT_COLOURS[1]  # DEFAULT_COLOURS[0] is slot 1's colour
    assert f"WARN: colour not in preset Test PETG Basic; using {want} (pass :#RRGGBB to choose)" in " ".join(doc["notes"])
    assert [c.upper() for c in Project(str(src)).project_settings["filament_colour"]] == [DEFAULT_COLOURS[0].upper(), want.upper()]


def test_inspect_prints_the_effective_nozzle_stats_per_filament(capsys, tmp_path):
    f = make_3mf(tmp_path / "s.3mf", extruder_nozzle_stats=["Standard#1", "Standard#1"])
    code, out, _ = run(capsys, "inspect", str(f))
    assert code == 0 and "extruder 1, extruder_nozzle_stats Standard#1" in out
    code, out, _ = run(capsys, "--json", "inspect", str(make_3mf(tmp_path / "u.3mf", extruder_nozzle_stats=[])))
    doc = one_json(out)
    assert doc["nozzle_stats"] == {"key": "extruder_nozzle_stats", "value": []} and doc["filaments"][0]["nozzle_stats"] is None


def test_slice_verb_tower_takes_the_planned_tower_size(capsys, tmp_path, monkeypatch, resolver):
    """`slice --tower` sizes the tower as `check` does: a 20 mm tower without rib wall fits at x 230."""
    from test_edits import SequenceStudio

    src = _geo(tmp_path, prime_tower_width="20", prime_tower_rib_wall="0", prime_tower_brim_width="2")
    studio = SequenceStudio(make_3mf(tmp_path / "f.gcode.3mf", sliced=True, wipe_tower_x=["230"], wipe_tower_y=["180"]), codes=[0])
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=studio, resolver=resolver))
    code, out, _ = run(capsys, "--json", "slice", str(src), "--tower", "230,180", "-o", str(tmp_path / "o.gcode.3mf"))
    doc = one_json(out)
    assert code == 0, doc
    assert any("x 228.0..252.0 y 178.0..215.8" in n for n in doc["notes"])
