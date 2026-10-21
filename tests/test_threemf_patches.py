"""threemf.Project summary and every patches.* command: round trip, idempotence, zip integrity."""

import os
import shutil
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from conftest import MACHINE, PROCESS_NAME, make_3mf
from shambu import patches as X
from shambu.errors import ProjectError
from shambu.threemf import CUSTOM_GCODE, LAYER_RANGES, PROJECT_SETTINGS, Project

REAL = Path(os.environ.get("SHAMBU_REAL_3MF", "/nonexistent"))


def apply(path, *patches) -> dict:
    X.PatchSet().add(*patches).apply(str(path))
    return Project(str(path)).summary()


def obj(summary, name):
    return next(o for o in summary["objects"] if o["name"] == name)


def without_file(s: dict) -> dict:
    return {k: v for k, v in s.items() if k != "file"}


# ---------------------------------------------------------------------------- summary
def test_summary_header(project_file):
    s = Project(str(project_file)).summary()
    assert s["application"] == "BambuStudio-02.08.02.61"
    assert (s["printer"], s["process"], s["bed_type"], s["sliced"]) == (MACHINE, PROCESS_NAME, "Cool Plate", False)
    assert s["filament_map"] == ["1"]
    assert s["extruders"]["extruder_type"] == ["Direct Drive", "Bowden"]


def test_summary_filaments_pad_missing_colour(project_file):
    fs = Project(str(project_file)).summary()["filaments"]
    assert [(f["slot"], f["type"], f["colour"]) for f in fs] == [(1, "PLA", "#00AE42"), (2, "PLA", None)]


def test_summary_objects_and_parts(project_file):
    s = Project(str(project_file)).summary()
    assert [o["name"] for o in s["objects"]] == ["body", "label"]
    label = obj(s, "label")
    assert [(p["name"], p["extruder"]) for p in label["parts"]] == [("label_base", "1"), ("label_text", "2")]
    assert label["parts"][0]["settings"] == {}  # name, extruder, source_* are not settings


def test_build_index_follows_build_order_not_id(project_file):
    s = Project(str(project_file)).summary()
    assert (obj(s, "label")["build_index"], obj(s, "body")["build_index"]) == (1, 2)


def test_summary_plate(project_file):
    (pl,) = Project(str(project_file)).summary()["plates"]
    assert (pl["id"], pl["bed_type"], pl["objects"]) == (1, None, ["body", "label"])


def test_not_a_bambu_project(tmp_path):
    p = make_3mf(tmp_path / "plain.3mf", with_model_settings=False)
    with pytest.raises(ProjectError):
        Project(str(p)).summary()


# ---------------------------------------------------------------------------- each patch
def test_set_bed_type_project(project_file):
    assert apply(project_file, X.SetBedType("Textured PEI Plate"))["bed_type"] == "Textured PEI Plate"


def test_set_bed_type_one_plate(project_file):
    s = apply(project_file, X.SetBedType("High Temp Plate", 1))
    assert (s["bed_type"], s["plates"][0]["bed_type"]) == ("Cool Plate", "High Temp Plate")


def test_set_bed_type_everywhere(project_file):
    s = apply(project_file, X.SetBedTypeEverywhere("Supertack Plate"))
    assert (s["bed_type"], s["plates"][0]["bed_type"]) == ("Supertack Plate", "Supertack Plate")


def test_bad_bed_type_raises(project_file):
    with pytest.raises(ProjectError):
        apply(project_file, X.SetBedType("Glass Plate"))


def test_set_filament_colours(project_file):
    s = apply(project_file, X.SetFilamentColours(("#000000", "#ffffff")))
    assert [f["colour"] for f in s["filaments"]] == ["#000000", "#FFFFFF"]


@pytest.mark.parametrize("colours", [("#000000",), ("#000000", "white")])
def test_bad_colours_raise(project_file, colours):
    with pytest.raises(ProjectError):
        apply(project_file, X.SetFilamentColours(colours))


def test_set_filament_map_project_writes_variant_blocks(project_file):
    """The conftest fixture holds the short form (one variant per filament, no filament_self_index). The map patch
    writes one block of extruder variants per filament with a matching filament_self_index, never one entry each."""
    s = apply(project_file, X.SetFilamentMap((1, 2)))
    assert (s["filament_map"], s["filament_map_mode"]) == (["1", "2"], "Manual")
    ps = Project(str(project_file)).project_settings
    block = ["Direct Drive Standard", "Bowden Standard"]
    assert ps["filament_extruder_variant"] == block + block
    assert ps["filament_self_index"] == ["1", "1", "2", "2"]


X2D_VARIANTS = [
    "Direct Drive Standard",
    "Direct Drive High Flow",
    "Direct Drive E3D High Flow",
    "Bowden Standard",
    "Bowden High Flow",
    "Bowden E3D High Flow",
]
# What an unsliced X2D export held after the old map patch (plate 14): 12 self-index entries, 2 variants, empty stats
UNSLICED_X2D = {
    "extruder_nozzle_stats": [],
    "extruder_max_nozzle_count": ["1", "1"],
    "printer_extruder_variant": X2D_VARIANTS,
    "filament_extruder_variant": ["Direct Drive Standard", "Bowden Standard"],
    "filament_self_index": ["1"] * 6 + ["2"] * 6,
    "filament_volume_map": ["0"],
    "filament_nozzle_map": ["1"],
    "filament_map": ["1", "2"],
}


def test_set_filament_map_keeps_the_x2d_blocks(tmp_path):
    """Studio pairs filament_extruder_variant with filament_self_index index by index (PrintConfig.cpp:8022-8052):
    a collapsed list left filament 2 unfindable ('could not found extruder_type ... filament_index 2')."""
    f = make_3mf(tmp_path / "x2d.3mf", **{**UNSLICED_X2D, "filament_extruder_variant": X2D_VARIANTS * 2})
    apply(f, X.SetFilamentMap((1, 2)))
    ps = Project(str(f)).project_settings
    assert ps["filament_extruder_variant"] == X2D_VARIANTS * 2
    assert ps["filament_self_index"] == ["1"] * 6 + ["2"] * 6
    f2 = make_3mf(tmp_path / "x2d-short.3mf", **UNSLICED_X2D)
    apply(f2, X.SetFilamentMap((1, 2)))  # a collapsed file is rebuilt from the printer's variant list
    assert Project(str(f2)).project_settings["filament_extruder_variant"] == X2D_VARIANTS * 2


def test_repair_nozzle_stats_makes_an_unsliced_export_resliceable(tmp_path):
    f = make_3mf(tmp_path / "u.3mf", **UNSLICED_X2D)
    pr = Project(str(f))
    assert pr.reslice_problems() and pr.summary()["reslice_problems"][0].startswith("extruder_nozzle_stats []")
    done = X.PatchSet().add(X.RepairNozzleStats()).apply(str(f))
    assert done == [X.RepairNozzleStats().describe()]
    ps = Project(str(f)).project_settings
    assert ps["extruder_nozzle_stats"] == ["Standard#1", "Standard#1"]
    assert ps["filament_extruder_variant"] == X2D_VARIANTS * 2 and len(ps["filament_self_index"]) == 12
    assert ps["filament_volume_map"] == ["0", "0"] and ps["filament_nozzle_map"] == ["1", "0"]
    assert Project(str(f)).reslice_problems() == []


def test_repair_nozzle_stats_takes_the_filament_preset_variants(tmp_path):
    f = make_3mf(tmp_path / "u.3mf", **UNSLICED_X2D)
    own = tuple(reversed(X2D_VARIANTS))
    X.PatchSet().add(X.RepairNozzleStats((tuple(X2D_VARIANTS), own))).apply(str(f))
    assert Project(str(f)).project_settings["filament_extruder_variant"] == X2D_VARIANTS + list(own)


def test_repair_nozzle_stats_is_idempotent_and_keeps_the_zip(tmp_path):
    f = make_3mf(tmp_path / "u.3mf", **{**UNSLICED_X2D, "extruder_nozzle_stats": ["Standard#0", "Standard#0"]})
    ps_set = X.PatchSet().add(X.RepairNozzleStats())
    first = ps_set.apply(str(f))
    once = Project(str(f)).read(PROJECT_SETTINGS)
    with zipfile.ZipFile(f) as z:
        order = [(i.filename, i.compress_type) for i in z.infolist()]
    assert ps_set.apply(str(f)) == first
    assert Project(str(f)).read(PROJECT_SETTINGS) == once
    with zipfile.ZipFile(f) as z:
        assert z.testzip() is None
        assert [(i.filename, i.compress_type) for i in z.infolist()] == order
    assert Project(str(f)).project_settings["extruder_nozzle_stats"] == ["Standard#1", "Standard#1"]


def test_repair_refuses_a_block_it_cannot_rebuild(tmp_path):
    f = make_3mf(tmp_path / "u.3mf", **{**UNSLICED_X2D, "filament_self_index": ["1"] * 6 + ["2"] * 4})
    with pytest.raises(ProjectError, match="filament 2 has 4 entries"):
        X.PatchSet().add(X.RepairNozzleStats()).apply(str(f))


def test_reslice_problems_flags_mismatched_variant_lengths(tmp_path):
    f = make_3mf(tmp_path / "u.3mf", **{**UNSLICED_X2D, "extruder_nozzle_stats": ["Standard#1", "Standard#1"]})
    (msg,) = Project(str(f)).reslice_problems()
    assert msg.startswith("filament_extruder_variant has 2 entries but filament_self_index 12")


def test_set_filament_map_plate(project_file):
    s = apply(project_file, X.SetFilamentMap((2, 1), plate=1))
    assert (s["plates"][0]["filament_maps"], s["plates"][0]["filament_map_mode"]) == ("2 1", "Manual")
    assert s["filament_map"] == ["1"]  # project default untouched


@pytest.mark.parametrize("m", [(1,), (1, 3)])
def test_bad_filament_map_raises(project_file, m):
    with pytest.raises(ProjectError):
        apply(project_file, X.SetFilamentMap(m))


def test_set_and_clear_object_setting(project_file):
    s = apply(project_file, X.SetObjectSetting("body", "wall_loops", "6"))
    assert obj(s, "body")["settings"] == {"wall_loops": "6"}
    s = apply(project_file, X.ClearObjectSetting("body", "wall_loops"))
    assert obj(s, "body")["settings"] == {}


def test_object_setting_refuses_name(project_file):
    with pytest.raises(ProjectError):
        apply(project_file, X.SetObjectSetting("body", "name", "x"))


def test_unknown_object_raises(project_file):
    with pytest.raises(ProjectError, match="no object named"):
        apply(project_file, X.SetObjectSetting("ghost", "wall_loops", "6"))


def test_set_extruder_object(project_file):
    assert obj(apply(project_file, X.SetExtruder("body", 2)), "body")["extruder"] == "2"


def test_set_extruder_part(project_file):
    s = apply(project_file, X.SetExtruder("label", 1, "label_text"))
    assert [p["extruder"] for p in obj(s, "label")["parts"]] == ["1", "1"]


def test_set_extruder_out_of_range(project_file):
    with pytest.raises(ProjectError):
        apply(project_file, X.SetExtruder("body", 3))


def test_height_range_keyed_by_build_order(project_file):
    s = apply(project_file, X.SetHeightRange("body", 0.0, 0.6, (("extruder", "2"),)))
    assert obj(s, "body")["height_ranges"] == [{"min_z": 0.0, "max_z": 0.6, "options": {"extruder": "2", "layer_height": "0.2"}}]
    root = ET.fromstring(Project(str(project_file)).read(LAYER_RANGES))
    assert [o.get("id") for o in root.findall("object")] == ["2"]  # body is the 2nd build item, its object id is 2 too
    s = apply(project_file, X.SetHeightRange("label", 1.0, 2.0, (("wall_loops", "4"),)))
    root = ET.fromstring(Project(str(project_file)).read(LAYER_RANGES))
    assert sorted(o.get("id") for o in root.findall("object")) == ["1", "2"]  # label (object id 4) is build item 1


def test_height_range_replaces_same_span(project_file):
    apply(project_file, X.SetHeightRange("body", 0.0, 0.6, (("extruder", "2"),)))
    s = apply(project_file, X.SetHeightRange("body", 0.0, 0.6, (("extruder", "1"),)))
    assert obj(s, "body")["height_ranges"] == [{"min_z": 0.0, "max_z": 0.6, "options": {"extruder": "1", "layer_height": "0.2"}}]


def test_height_range_always_has_layer_height(project_file):
    # A range without layer_height segfaults the slicer (02-binary-crash.md 1.3).
    apply(project_file, X.SetHeightRange("body", 0.0, 0.6, (("extruder", "2"),)))
    root = ET.fromstring(Project(str(project_file)).read(LAYER_RANGES))
    assert [op.get("opt_key") for op in root.iter("option")] == ["extruder", "layer_height"]
    assert root.find("object/range/option[@opt_key='layer_height']").text == "0.2"


def test_height_range_caller_layer_height_wins(project_file):
    s = apply(project_file, X.SetHeightRange("body", 0.0, 0.6, (("layer_height", "0.08"), ("extruder", "2"))))
    assert obj(s, "body")["height_ranges"][0]["options"] == {"layer_height": "0.08", "extruder": "2"}


def test_height_range_replace_keeps_existing_layer_height(project_file):
    apply(project_file, X.SetHeightRange("body", 0.0, 0.6, (("layer_height", "0.12"), ("extruder", "2"))))
    s = apply(project_file, X.SetHeightRange("body", 0.0, 0.6, (("extruder", "1"),)))
    assert obj(s, "body")["height_ranges"][0]["options"] == {"extruder": "1", "layer_height": "0.12"}


def test_height_range_without_any_layer_height_raises(tmp_path):
    p = make_3mf(tmp_path / "nolh.3mf", layer_height="")
    with pytest.raises(ProjectError, match="layer_height"):
        apply(p, X.SetHeightRange("body", 0.0, 0.6, (("extruder", "2"),)))


def test_project_layer_height(project_file):
    assert Project(str(project_file)).project_layer_height() == "0.2"


def test_custom_gcode_kinds(project_file):
    s = apply(
        project_file,
        X.AddCustomGcode(1, 4.2, "pause"),
        X.AddCustomGcode(1, 2.0, "color_change", extruder=2, colour="#FF0000"),
        X.AddCustomGcode(1, 6.0, "custom", extra="M117 hi"),
    )
    got = [(c["top_z"], c["type"], c["extruder"]) for c in s["custom_gcode"]]
    assert got == [(2.0, "color_change", "2"), (4.2, "pause", "1"), (6.0, "custom", "1")]
    assert {c["mode"] for c in s["custom_gcode"]} == {"MultiExtruder"}
    root = ET.fromstring(Project(str(project_file)).read(CUSTOM_GCODE))
    assert root.find("plate/layer[@type='1']").get("gcode") == "M400 U1"


def test_custom_gcode_bad_plate_and_kind(project_file):
    with pytest.raises(ProjectError):
        apply(project_file, X.AddCustomGcode(2, 1.0))
    with pytest.raises(ProjectError):
        apply(project_file, X.AddCustomGcode(1, 1.0, "explode"))


def test_rename_plate(project_file):
    assert apply(project_file, X.RenamePlate(1, "jaws"))["plates"][0]["name"] == "jaws"


def test_rename_object(project_file):
    s = apply(project_file, X.RenameObject("body", "jaw"))
    assert [o["name"] for o in s["objects"]] == ["jaw", "label"]
    assert s["plates"][0]["objects"] == ["jaw", "label"]


def test_rename_object_twice_is_a_noop(project_file):
    p = Project(str(project_file))
    assert p.rename_object("body", "jaw") is True
    assert p.rename_object("body", "jaw") is False
    assert [o["name"] for o in p.summary()["objects"]] == ["jaw", "label"]
    s = apply(project_file, X.RenameObject("body", "jaw"))
    s = apply(project_file, X.RenameObject("body", "jaw"))
    assert [o["name"] for o in s["objects"]] == ["jaw", "label"]


def test_rename_object_neither_name_raises(project_file):
    with pytest.raises(ProjectError, match="no object named"):
        apply(project_file, X.RenameObject("ghost", "spirit"))


def test_rename_object_ambiguous_raises(tmp_path):
    p = make_3mf(tmp_path / "dup.3mf", object_names=("part", "part"))
    with pytest.raises(ProjectError, match="2 objects named"):
        apply(p, X.RenameObject("part", "a"))
    with pytest.raises(ProjectError):
        apply(p, X.RenameObject("gone", "part"))  # 'new' exists twice: not a finished rename


def test_rename_plate_twice_is_a_noop(project_file):
    p = Project(str(project_file))
    assert p.rename_plate(1, "jaws") is True
    assert p.rename_plate(1, "jaws") is False


def test_empty_patchset_does_not_write(project_file):
    before = project_file.read_bytes()
    assert X.PatchSet().apply(str(project_file)) == []
    assert project_file.read_bytes() == before


def test_patchset_describe_and_out(project_file, tmp_path):
    out = tmp_path / "out.3mf"
    ps = X.PatchSet().add(X.RenamePlate(1, "a"), X.SetBedTypeEverywhere("Cool Plate"))
    assert ps.apply(str(project_file), str(out)) == ps.describe() == ["plate 1 named 'a'", "bed type Cool Plate (project and every plate)"]
    assert Project(str(out)).summary()["plates"][0]["name"] == "a"


# ---------------------------------------------------------------------------- integrity
IDEMPOTENT = (
    X.SetBedTypeEverywhere("Textured PEI Plate"),
    X.SetBedType("Engineering Plate", 1),
    X.SetFilamentColours(("#000000", "#FFFFFF")),
    X.SetFilamentMap((1, 2)),
    X.SetFilamentMap((1, 2), 1),
    X.SetObjectSetting("body", "wall_loops", "6"),
    X.ClearObjectSetting("label", "wall_loops"),
    X.SetExtruder("label", 2, "label_text"),
    X.SetHeightRange("body", 0, 0.6, (("extruder", "2"),)),
    X.RenamePlate(1, "p"),
)
# The rename comes first and later patches use the new name, as build.patch_set orders them.
FULL_SET = (
    X.RenameObject("body", "jaw"),
    X.SetBedTypeEverywhere("Textured PEI Plate"),
    X.SetFilamentColours(("#000000", "#FFFFFF")),
    X.SetFilamentMap((1, 2)),
    X.SetObjectSetting("jaw", "wall_loops", "6"),
    X.SetExtruder("label", 2, "label_text"),
    X.SetHeightRange("jaw", 0, 0.6, (("extruder", "2"),)),
    X.AddCustomGcode(1, 4.2, "pause"),
    X.RenamePlate(1, "p"),
)


def test_patchset_twice_same_summary(project_file):
    once = apply(project_file, *IDEMPOTENT)
    twice = apply(project_file, *IDEMPOTENT)
    assert without_file(once) == without_file(twice)


def test_full_patchset_with_rename_twice_same_summary(project_file):
    ps = X.PatchSet().add(*FULL_SET)
    ps.apply(str(project_file))
    once = Project(str(project_file)).summary()
    assert ps.apply(str(project_file)) == ps.describe()  # second run does not raise
    twice = Project(str(project_file)).summary()
    assert without_file(once) == without_file(twice)
    assert [o["name"] for o in twice["objects"]] == ["jaw", "label"]


def test_custom_gcode_twice_same_summary(project_file):
    once = apply(project_file, X.AddCustomGcode(1, 4.2, "pause"))
    twice = apply(project_file, X.AddCustomGcode(1, 4.2, "pause"))
    assert without_file(once) == without_file(twice)


def test_patch_keeps_order_and_compression(project_file):
    before = [(i.filename, i.compress_type) for i in zipfile.ZipFile(project_file).infolist()]
    apply(project_file, X.SetBedTypeEverywhere("Textured PEI Plate"), X.SetObjectSetting("body", "wall_loops", "6"))
    with zipfile.ZipFile(project_file) as z:
        assert [(i.filename, i.compress_type) for i in z.infolist()] == before
        assert z.testzip() is None


def test_patch_touches_only_metadata(project_file, tmp_path):
    orig = tmp_path / "orig.3mf"
    shutil.copyfile(project_file, orig)
    apply(project_file, *IDEMPOTENT, X.AddCustomGcode(1, 1.0))
    a, b = zipfile.ZipFile(orig), zipfile.ZipFile(project_file)
    for n in a.namelist():
        if not n.startswith("Metadata/"):
            assert a.read(n) == b.read(n), n


def test_new_entries_go_before_settings(project_file):
    apply(project_file, X.SetHeightRange("body", 0, 0.6, (("extruder", "2"),)), X.AddCustomGcode(1, 1.0))
    names = zipfile.ZipFile(project_file).namelist()
    assert names.index(LAYER_RANGES) < names.index(PROJECT_SETTINGS)
    assert names.index(CUSTOM_GCODE) < names.index("Metadata/model_settings.config")


def test_patch_on_sliced_project_drops_slice(sliced_file):
    assert Project(str(sliced_file)).summary()["sliced"] is True
    s = apply(sliced_file, X.SetObjectSetting("body", "wall_loops", "6"))
    assert s["sliced"] is False
    names = zipfile.ZipFile(sliced_file).namelist()
    assert "Metadata/plate_1.gcode" not in names and "Metadata/plate_1.gcode.md5" not in names
    pl = ET.fromstring(Project(str(sliced_file)).read("Metadata/model_settings.config")).find("plate")
    assert "prediction" not in [m.get("key") for m in pl.findall("metadata")]
    si = ET.fromstring(Project(str(sliced_file)).read("Metadata/slice_info.config"))
    assert si.findall("plate") == []


def test_rename_keeps_slice(sliced_file):
    s = apply(sliced_file, X.RenamePlate(1, "x"), X.RenameObject("body", "jaw"))
    assert s["sliced"] is True
    assert "Metadata/plate_1.gcode" in zipfile.ZipFile(sliced_file).namelist()


def test_rename_does_not_invalidate_but_settings_do(sliced_file):
    p = Project(str(sliced_file))
    p.rename_object("body", "jaw")
    p.rename_plate(1, "x")
    assert p.is_sliced() and "Metadata/plate_1.gcode" in p.names()
    p.add_custom_gcode(1, 1.0)
    assert not p.is_sliced() and "Metadata/plate_1.gcode" not in p.names()


# ---------------------------------------------------------------------------- malformed input
def test_read_missing_entry_raises_project_error(project_file):
    with pytest.raises(ProjectError, match=r"Metadata/nope\.xml"):
        Project(str(project_file)).read("Metadata/nope.xml")


def _replace_entry(path: Path, name: str, data: bytes) -> None:
    with zipfile.ZipFile(path) as z:
        entries = [(i, z.read(i.filename)) for i in z.infolist()]
    with zipfile.ZipFile(path, "w") as z:
        for i, d in entries:
            z.writestr(i, data if i.filename == name else d)


def test_malformed_project_settings_raises_project_error(project_file):
    _replace_entry(project_file, PROJECT_SETTINGS, b"{not json")
    with pytest.raises(ProjectError, match="JSON"):
        Project(str(project_file)).summary()


def test_project_settings_not_an_object_raises(project_file):
    _replace_entry(project_file, PROJECT_SETTINGS, b"[1, 2]")
    with pytest.raises(ProjectError, match="JSON object"):
        _ = Project(str(project_file)).project_settings


@pytest.mark.parametrize("entry", ["Metadata/model_settings.config", "3D/3dmodel.model", "Metadata/slice_info.config"])
def test_malformed_xml_raises_project_error(project_file, entry):
    _replace_entry(project_file, entry, b"<config><unclosed>")
    with pytest.raises(ProjectError, match="not valid XML"):
        Project(str(project_file)).summary()


def test_project_settings_trailing_newline_kept(project_file):
    apply(project_file, X.SetBedType("Textured PEI Plate"))
    assert Project(str(project_file)).read(PROJECT_SETTINGS).endswith(b"}\n")


# ---------------------------------------------------------------------------- real file (read-only, copied)
@pytest.mark.integration
@pytest.mark.skipif(not REAL.is_file(), reason="real CLI export not present")
def test_real_cli_export_round_trip(tmp_path):
    p = tmp_path / "real.3mf"
    shutil.copyfile(REAL, p)
    before = [(i.filename, i.compress_type) for i in zipfile.ZipFile(p).infolist()]
    s0 = Project(str(p)).summary()
    assert s0["application"].startswith("BambuStudio-")
    s1 = apply(p, X.SetBedTypeEverywhere("Textured PEI Plate"), X.SetFilamentColours(("#000000", "#FFFFFF")))
    assert s1["bed_type"] == "Textured PEI Plate"
    assert [o["name"] for o in s1["objects"]] == [o["name"] for o in s0["objects"]]
    with zipfile.ZipFile(p) as z:
        assert [(i.filename, i.compress_type) for i in z.infolist()] == before
        assert z.testzip() is None


# ---------------------------------------------------------------------------- stats precedence (_new wins when non-empty)
def test_conflicting_stats_keys_repair_the_one_studio_reads(tmp_path):
    """BambuStudio.cpp:4151-4155: a non-empty extruder_nozzle_stats_new wins over the plain key."""
    f = make_3mf(
        tmp_path / "c.3mf",
        **{
            **UNSLICED_X2D,
            "extruder_nozzle_stats": ["Standard#1", "Standard#1"],
            "extruder_nozzle_stats_new": ["Standard#0", "Standard#0"],
        },
    )
    (msg,) = [m for m in Project(str(f)).reslice_problems() if "nozzle_stats" in m]
    assert msg.startswith("extruder_nozzle_stats_new ['Standard#0', 'Standard#0']")
    X.PatchSet().add(X.RepairNozzleStats()).apply(str(f))
    ps = Project(str(f)).project_settings
    assert ps["extruder_nozzle_stats_new"] == ["Standard#1", "Standard#1"] == ps["extruder_nozzle_stats"]
    assert Project(str(f)).reslice_problems() == []


def test_valid_new_key_wins_over_an_empty_plain_key(tmp_path):
    f = make_3mf(tmp_path / "n.3mf", **{**UNSLICED_X2D, "extruder_nozzle_stats_new": ["Standard#1", "Standard#1"]})
    assert not [m for m in Project(str(f)).reslice_problems() if "nozzle_stats" in m]  # Studio reads _new


def test_valid_high_flow_stats_are_byte_identical_after_the_patch(tmp_path):
    hf = {
        **UNSLICED_X2D,
        "nozzle_volume_type": ["High Flow", "Standard"],
        "extruder_nozzle_stats": ["High Flow#1", "Standard#1|High Flow#1"],
        "filament_extruder_variant": X2D_VARIANTS * 2,
        "filament_volume_map": ["0", "0"],
        "filament_nozzle_map": ["1", "0"],
    }
    f = make_3mf(tmp_path / "hf.3mf", **hf)
    with zipfile.ZipFile(f) as z:
        before = z.read(PROJECT_SETTINGS)
    assert X.PatchSet().add(X.RepairNozzleStats()).apply(str(f)) == [X.RepairNozzleStats().describe()]
    assert Project(str(f)).project_settings["extruder_nozzle_stats"] == ["High Flow#1", "Standard#1|High Flow#1"]
    assert Project(str(f)).read(PROJECT_SETTINGS) == before


def test_repair_keeps_a_valid_entry_and_fixes_only_the_zero_one(tmp_path):
    f = make_3mf(
        tmp_path / "z.3mf",
        **{**UNSLICED_X2D, "nozzle_volume_type": ["High Flow", "Standard"], "extruder_nozzle_stats": ["High Flow#1", "Standard#0"]},
    )
    X.PatchSet().add(X.RepairNozzleStats()).apply(str(f))
    assert Project(str(f)).project_settings["extruder_nozzle_stats"] == ["High Flow#1", "Standard#1"]


def test_repair_then_map_leaves_no_reslice_problem(tmp_path):
    f = make_3mf(tmp_path / "m.3mf", **UNSLICED_X2D)
    X.PatchSet().add(X.RepairNozzleStats()).apply(str(f))
    X.PatchSet().add(X.SetFilamentMap((2, 1))).apply(str(f))
    assert Project(str(f)).reslice_problems() == []
    assert Project(str(f)).project_settings["filament_extruder_variant"] == X2D_VARIANTS * 2
