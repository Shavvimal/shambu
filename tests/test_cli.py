"""cli.main: subcommands, --json output, exit codes. Bambu() is replaced by a facade over fakes."""

import json
import zipfile

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
    assert (code, one_json(out)) == (0, {"ok": True, "problems": []})


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
            "uv tool, git tag v0.4.0",
            "69519ac",
        ),
        ({"url": "file:///src/shambu", "dir_info": {"editable": True}}, "editable checkout", None),
        ({"url": "file:///src/shambu", "dir_info": {}}, "uv tool, local folder (snapshot)", None),
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
    assert (d["install"], d["expected"], d["match"]) == ("uv tool, local folder (snapshot)", "9.9.9", False)
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
    code, out, _ = run(capsys, "--json", "doctor", "--no-printer")
    j = one_json(out)
    assert code == 3 and "binary not found" in j["studio"]["error"] and j["shambu"]["version"]
