"""edits.py: replace a mesh, move an object, move the prime tower, add a filament slot, the save() touches guard, and
the --no-check second slicing pass. Offline: a geometry fixture with real meshes (conftest.make_geo_3mf), fake Studio."""

from __future__ import annotations

import copy
import dataclasses
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from conftest import GEO_BODY_AT, VARIANTS, FakeStudio, geo_plate_origin, make_3mf, make_geo_3mf, write_box_stl
from shambu import edits as E
from shambu import layout as L
from shambu.build import Pipeline, two_pass_slice
from shambu.errors import BambuError, ProjectError, StudioError
from shambu.patches import PatchBase, PatchSet, SetFilamentMap
from shambu.studio import CliCommand, RunResult, conflicts_in_log
from shambu.threemf import MODEL, Project


# ---------------------------------------------------------------------------- helpers
def entries(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as z:
        return {i.filename: z.read(i.filename) for i in z.infolist()}


def infos(path: Path) -> list[tuple[str, int]]:
    with zipfile.ZipFile(path) as z:
        return [(i.filename, i.compress_type) for i in z.infolist()]


def rewrite(path: Path, name: str, fn) -> None:
    """Rewrite one entry of a fixture (a test set-up step, not a patch)."""
    with zipfile.ZipFile(path) as z:
        items = [(i, z.read(i.filename)) for i in z.infolist()]
    with zipfile.ZipFile(path, "w") as z:
        for i, d in items:
            z.writestr(i, fn(d.decode()).encode() if i.filename == name else d)


def item_tf(path: Path, oid: str) -> list[float]:
    text = entries(path)[MODEL].decode()
    m = re.search(rf'<item objectid="{oid}"[^>]*transform="([^"]+)"', text)
    assert m
    return [float(x) for x in m.group(1).split()]


def comp_tf(path: Path, sub: str) -> str:
    m = re.search(rf'<component [^>]*objectid="{sub}"[^>]*transform="([^"]+)"', entries(path)[MODEL].decode())
    assert m
    return m.group(1)


def box(path: Path, name: str):
    return E.world_box(Project(str(path)), name)


def replace(path: Path, stl: Path, out: Path | None = None, **kw) -> E.EditSet:
    es = E.EditSet([E.ReplaceMesh("body", str(stl), **kw)])
    es.apply(str(path), str(out) if out else None)
    return es


def ms_part(path: Path, name: str) -> dict:
    p = Project(str(path))
    o = next(o for o in p.model_settings.findall("object") if any(m.get("value") == name for m in o.findall("metadata")))
    part = o.find("part")
    assert part is not None
    return {
        "object": {m.get("key"): m.get("value") for m in o.findall("metadata") if m.get("key")},
        "face_count": next(m.get("face_count") for m in o.findall("metadata") if m.get("face_count")),
        "part": {m.get("key"): m.get("value") for m in part.findall("metadata")},
        "mesh_stat": dict(part.find("mesh_stat").attrib),  # type: ignore[union-attr]
    }


ROT90 = "0 1 0 -1 0 0 0 0 1"  # 90 degrees about z, column order: x -> y


# ---------------------------------------------------------------------------- 1-9 replace mesh
def test_replace_mesh_keeps_transform_and_settings(tmp_path, geo_file):
    stl = write_box_stl(tmp_path / "body-v2.stl", (0, 0, 0), (30, 20, 10), fine=True)
    before = ms_part(geo_file, "body")
    tf = item_tf(geo_file, "2")
    out = tmp_path / "out.3mf"
    replace(geo_file, stl, out)
    after = ms_part(out, "body")
    assert item_tf(out, "2") == tf
    assert after["object"] == before["object"]  # name, extruder, brim_type
    assert after["face_count"] == "24"
    assert after["mesh_stat"]["face_count"] == "24"
    assert after["part"]["source_file"] == "body-v2.stl"


def test_replace_mesh_recentres_vertices(tmp_path, geo_file):
    stl = write_box_stl(tmp_path / "far.stl", (100, 0, 0), (110, 20, 10))
    out = tmp_path / "out.3mf"
    replace(geo_file, stl, out, allow_size_change=True)
    p = Project(str(out))
    mesh = E.read_mesh(p.read("3D/Objects/object_1.model").decode(), "1")
    xs = [v[0] for v in mesh.vertices]
    assert (min(xs), max(xs)) == (-5.0, 5.0)
    assert ms_part(out, "body")["part"]["source_offset_x"] == "105"


@pytest.mark.parametrize("rot, axis", [(None, 0), (ROT90, 1)])
def test_replace_mesh_frame_anchor_moves_item(tmp_path, rot, axis):
    path = make_geo_3mf(tmp_path / "g.3mf")
    if rot:
        rewrite(path, MODEL, lambda t: t.replace('transform="1 0 0 0 1 0 0 0 1 55 60 5"', f'transform="{rot} 55 60 5"'))
    tf = item_tf(path, "2")
    stl = write_box_stl(tmp_path / "longer.stl", (0, 0, 0), (36, 20, 10))  # bbox centre +3 mm in x, in the STL frame
    out = tmp_path / "out.3mf"
    replace(path, stl, out, allow_size_change=True)
    new = item_tf(out, "2")
    delta = [new[9 + i] - tf[9 + i] for i in range(3)]
    want = [0.0, 0.0, 0.0]
    want[axis] = 3.0
    assert delta == pytest.approx(want, abs=1e-6)
    assert new[:9] == tf[:9]


def test_replace_mesh_ensures_on_bed(tmp_path, geo_file):
    stl = write_box_stl(tmp_path / "high.stl", (0, 0, 5), (30, 20, 15), fine=True)
    out = tmp_path / "out.3mf"
    replace(geo_file, stl, out)
    lo, _hi = box(out, "body")
    assert lo[2] == pytest.approx(0.0)
    assert (lo[0], lo[1]) == pytest.approx(GEO_BODY_AT)


def test_replace_mesh_noop_raises(tmp_path, geo_file):
    stl = write_box_stl(tmp_path / "same.stl", (0, 0, 0), (30, 20, 10))
    raw = geo_file.read_bytes()
    with pytest.raises(E.EditError, match="no-op"):
        replace(geo_file, stl)
    assert geo_file.read_bytes() == raw


def test_replace_mesh_size_change_raises(tmp_path, geo_file):
    stl = write_box_stl(tmp_path / "wide.stl", (0, 0, 0), (31, 20, 10))
    with pytest.raises(E.EditError, match=r"delta \(1\.000, 0\.000, 0\.000\)"):
        replace(geo_file, stl)
    es = replace(geo_file, stl, tmp_path / "out.3mf", allow_size_change=True)
    assert any("delta (1.000, 0.000, 0.000)" in n for n in es.notes)


def test_replace_mesh_rotated_hint(tmp_path, geo_file):
    stl = write_box_stl(tmp_path / "turned.stl", (0, 0, 0), (20, 30, 10), fine=True)
    with pytest.raises(E.EditError, match="--rotate-mesh 0,0,90"):
        replace(geo_file, stl)
    replace(geo_file, stl, tmp_path / "out.3mf", rotate=(0.0, 0.0, 90.0), anchor="centre")
    lo, hi = box(tmp_path / "out.3mf", "body")
    assert (hi[0] - lo[0], hi[1] - lo[1]) == pytest.approx((30, 20))


def test_replace_mesh_paint_raises(tmp_path):
    path = make_geo_3mf(tmp_path / "painted.3mf", paint=True)
    stl = write_box_stl(tmp_path / "fine.stl", (0, 0, 0), (30, 20, 10), fine=True)
    with pytest.raises(E.EditError, match="--drop-paint"):
        replace(path, stl)
    replace(path, stl, tmp_path / "out.3mf", drop_paint=True)
    assert "paint_" not in entries(tmp_path / "out.3mf")["3D/Objects/object_1.model"].decode()


def test_replace_mesh_touches_only_declared_entries(tmp_path, geo_file):
    stl = write_box_stl(tmp_path / "fine.stl", (0, 0, 0), (30, 20, 10), fine=True)
    out = tmp_path / "out.3mf"
    replace(geo_file, stl, out)
    a, b = entries(geo_file), entries(out)
    changed = {n for n in b if a.get(n) != b[n]}
    assert changed == {"3D/Objects/object_1.model", "Metadata/model_settings.config"}  # 3dmodel.model may change too
    assert "Metadata/plate_1.json" not in b
    assert [x for x in infos(geo_file) if x[0] != "Metadata/plate_1.json"] == infos(out)
    with zipfile.ZipFile(out) as z:
        assert z.testzip() is None
    # 3dmodel.model: only the transform attribute of the body's item may differ (here it does not)
    assert b[MODEL] == a[MODEL]


def test_replace_mesh_height_range_above_top_raises(tmp_path):
    path = make_geo_3mf(tmp_path / "r.3mf", ranges=[(8.0, 10.0)])
    stl = write_box_stl(tmp_path / "low.stl", (0, 0, 0), (30, 20, 6))
    with pytest.raises(E.EditError, match=r"height range 8\.0\.\.10\.0"):
        replace(path, stl, allow_size_change=True)


def test_replace_mesh_frame_mismatch_raises(tmp_path, geo_file):
    # the same size, exported with its origin at the bbox centre in x and y (a different STL frame)
    stl = write_box_stl(tmp_path / "centred.stl", (-15, -10, 0), (15, 10, 10), fine=True)
    with pytest.raises(E.EditError, match=r"the STL frames differ.*--anchor centre"):
        replace(geo_file, stl)
    es = replace(geo_file, stl, tmp_path / "out.3mf", anchor="centre")
    assert any("moved 0.000 mm" in n for n in es.notes)


def test_replace_mesh_component_layout_keeps_world_box(tmp_path):
    path = make_geo_3mf(tmp_path / "c.3mf", layout="component")
    ctf = comp_tf(path, "1")
    out = tmp_path / "out.3mf"
    replace(path, write_box_stl(tmp_path / "fine.stl", (0, 0, 0), (30, 20, 10), fine=True), out)
    assert comp_tf(out, "1") == ctf
    assert [*box(out, "body")[0], *box(out, "body")[1]] == pytest.approx([*box(path, "body")[0], *box(path, "body")[1]])


# ---------------------------------------------------------------------------- 10 the save() guard
@dataclass(frozen=True)
class _Sneaky(PatchBase):
    """Declares project_settings only, but rewrites the root model."""

    touches: frozenset[str] = frozenset({"Metadata/project_settings.config"})

    def apply(self, p: Project) -> None:
        p.write_entry(MODEL, p.read(MODEL) + b"<!-- -->")

    def describe(self) -> str:
        return "sneaky"


@dataclass(frozen=True)
class _Undeclared(PatchBase):
    def apply(self, p: Project) -> None:
        p.write_entry("3D/Objects/object_1.model", b"<model/>")

    def describe(self) -> str:
        return "no touches: Metadata/* only"


def test_save_rejects_undeclared_entry_change(geo_file):
    raw = geo_file.read_bytes()
    with pytest.raises(ProjectError, match="did not declare"):
        E.EditSet([_Sneaky()]).apply(str(geo_file))
    with pytest.raises(ProjectError, match="did not declare"):
        PatchSet([_Undeclared()]).apply(str(geo_file))  # PatchBase's default `touches`: Metadata/*
    assert geo_file.read_bytes() == raw


def test_save_rejects_rels_and_3d_entry_changes(geo_file):
    raw = geo_file.read_bytes()
    p = Project(str(geo_file))
    p.write_entry("3D/_rels/3dmodel.model.rels", b"<Relationships/>")
    with pytest.raises(ProjectError, match="relationship"):
        p.save(str(geo_file), {"3D/*"})
    p = Project(str(geo_file))
    p._put("3D/Objects/object_9.model", b"<model/>")  # a new 3D entry, even when 3D/* is declared
    with pytest.raises(ProjectError, match="added, removed or reordered a 3D"):
        p.save(str(geo_file), {"3D/*"})
    p = Project(str(geo_file))
    p.write_entry("3D/Objects/object_1.model", b"<model/>")  # a real 3D change under the default declaration
    with pytest.raises(ProjectError, match="did not declare"):
        p.save(str(geo_file))
    assert geo_file.read_bytes() == raw


# ---------------------------------------------------------------------------- 11-16 add filament
NEW_PRESET = {
    "name": "Test PETG Basic",
    "type": "filament",
    "from": "system",
    "filament_extruder_variant": list(VARIANTS),
    "filament_colour": ["#FCECD6"],
    "compatible_printers": [],
    "filament_settings_id": ["Test PETG Basic"],
    "filament_type": ["PETG"],
    "filament_density": ["1.27"],
    "nozzle_temperature": ["250"] * 6,
    "filament_flush_temp_fast": ["0"] * 6,
    "filament_id": "GFG00",
}


def scratch_settings(ps: dict, n: int) -> dict:
    """What Studio's CLI export of the project with `--load-filaments f1..fN;new --filament-colour ...` holds: per-filament
    keys grown by one entry, per-variant keys by one block (values normalised: 250 for the new temperatures), the
    project-only keys and the map not grown, filament_self_index all "1" (the CLI bug), and the flush matrix computed."""
    s = copy.deepcopy(ps)
    for k, v in ps.items():
        if (
            not isinstance(v, list)
            or k in E.PROJECT_ONLY_KEYS
            or k in E.MAP_KEYS
            or k in ("different_settings_to_system", "inherits_group")
        ):
            continue
        if k == "filament_self_index":
            s[k] = ["1"] * (len(v) + 6)
        elif k == "nozzle_temperature":
            s[k] = v + ["250"] * 6
        elif k == "filament_extruder_variant":
            s[k] = v + list(VARIANTS)
        elif len(v) == n and k not in (
            "nozzle_diameter",
            "extruder_type",
            "nozzle_volume_type",
            "extruder_printable_area",
            "extruder_printable_height",
        ):
            s[k] = [*v, {"filament_settings_id": "Test PETG Basic", "filament_type": "PETG", "filament_colour": "#FCECD6"}.get(k, v[0])]
    s["flush_volumes_matrix"] = ["0", "609", "182", "0"] * 2 if n == 1 else ["0"] * (2 * (n + 1) ** 2)
    # tuples above came from the dict default; make them lists of str
    return {k: [str(x) for x in v] if isinstance(v, (list, tuple)) else v for k, v in s.items()}


def one_filament(tmp_path) -> Path:
    return make_geo_3mf(tmp_path / "one.3mf", filaments=1)


def normalised_settings(ps: dict) -> dict:
    """What Studio's CLI export of a scratch copy loaded with the new preset alone holds: one filament, the new one,
    its variant block normalised (250 for the temperatures), the project-only keys as the project had them."""
    shape = E.filament_shape(ps, NEW_PRESET)
    s = copy.deepcopy(ps)
    for k in shape.per_filament:
        s[k] = [str(ps[k][0])] if k in E.PROJECT_ONLY_KEYS else [str((NEW_PRESET.get(k) or ps[k])[0])]
    for k in shape.per_variant:
        s[k] = [str(x) for x in NEW_PRESET[k]] if k in NEW_PRESET else [str(ps[k][0])] * 6
    s.update(filament_settings_id=["Test PETG Basic"], filament_extruder_variant=list(VARIANTS), filament_self_index=["1"] * 6)
    return s


def add_patch(path: Path, colour: str = "#FCECD6", extruder: int = 2, **kw) -> E.AddFilament:
    ps = Project(str(path)).project_settings
    n = len(ps["filament_settings_id"])
    slot = E.slot_from_export(ps, normalised_settings(ps), NEW_PRESET, scratch_settings(ps, n))
    return E.AddFilament(slot, colour, extruder, **kw)


def test_add_filament_extends_every_vector(tmp_path):
    path = one_filament(tmp_path)
    out = tmp_path / "two.3mf"
    E.EditSet([add_patch(path)]).apply(str(path), str(out))
    p = Project(str(out))
    ps = p.project_settings
    for k in (
        "filament_settings_id",
        "filament_colour",
        "filament_type",
        "filament_density",
        "filament_ids",
        "filament_map",
        "filament_nozzle_map",
    ):
        assert len(ps[k]) == 2, k
    for k in ("filament_volume_map", "filament_colour_type", "pressure_advance", "filament_multi_colour"):
        assert len(ps[k]) == 2, k
    assert len(ps["nozzle_temperature"]) == len(ps["filament_extruder_variant"]) == len(ps["filament_self_index"]) == 12
    assert ps["bridge_speed"] == ["50", "50", "50", "50", "200", "200"]  # a process variant key stays
    assert len(ps["flush_volumes_matrix"]) == 2 * 2 * 2
    assert len(ps["different_settings_to_system"]) == len(ps["inherits_group"]) == 4
    assert [pl["filament_maps"] for pl in p.summary()["plates"]] == ["1 2"]
    assert ps["filament_settings_id"][1] == "Test PETG Basic" and ps["filament_colour"][1] == "#FCECD6"
    assert ps["filament_map"] == ["1", "2"]


def test_add_filament_keeps_existing_slots(tmp_path):
    path = one_filament(tmp_path)
    before = Project(str(path)).project_settings
    out = tmp_path / "two.3mf"
    E.EditSet([add_patch(path)]).apply(str(path), str(out))
    after = Project(str(out)).project_settings
    for k, v in before.items():
        if isinstance(v, list) and k not in ("different_settings_to_system", "inherits_group", "flush_volumes_matrix"):
            assert after[k][: len(v)] == v, k
    assert after["inherits_group"][0] == before["inherits_group"][0] and after["inherits_group"][-1] == before["inherits_group"][-1]
    assert after["flush_volumes_matrix"][0] == "0"


def test_add_filament_reads_the_normalised_slot(tmp_path):
    path = one_filament(tmp_path)
    ps = Project(str(path)).project_settings
    slot = E.slot_from_export(ps, normalised_settings(ps), NEW_PRESET, scratch_settings(ps, 1))
    assert dict(slot.block)["nozzle_temperature"] == ("250",) * 6
    assert dict(slot.single)["filament_type"] == "PETG" and slot.flush == ("0", "609", "182", "0") * 2
    with pytest.raises(E.EditError, match="slot 1 is"):
        E.slot_from_export(ps, ps, NEW_PRESET)  # an export whose slot 1 is not the new preset


def test_add_filament_self_index_blocks(tmp_path):
    path = one_filament(tmp_path)
    out = tmp_path / "two.3mf"
    E.EditSet([add_patch(path)]).apply(str(path), str(out))
    ps = Project(str(out)).project_settings
    assert ps["filament_self_index"] == ["1"] * 6 + ["2"] * 6
    assert E.variant_lookup(ps) == {1: 0, 2: 9}  # Direct Drive Standard in block 1, Bowden Standard in block 2


def test_add_filament_twice_is_idempotent(tmp_path):
    path = one_filament(tmp_path)
    es = E.EditSet([add_patch(path)])
    a, b = tmp_path / "a.3mf", tmp_path / "b.3mf"
    done1 = es.apply(str(path), str(a))
    done2 = es.apply(str(a), str(b))
    assert done1 == done2
    assert a.read_bytes() == b.read_bytes()
    assert len(Project(str(b)).project_settings["filament_settings_id"]) == 2
    assert any("no change" in n for n in es.notes)


def test_add_filament_bad_input_raises(tmp_path, resolver):
    ps = Project(str(one_filament(tmp_path))).project_settings
    with pytest.raises(ProjectError, match="bad colour"):
        E.check_preset(ps, NEW_PRESET, "red", 1)
    with pytest.raises(ProjectError, match="extruder 3"):
        E.check_preset(ps, NEW_PRESET, "#FFFFFF", 3)
    with pytest.raises(ProjectError, match="compatible_printers"):
        E.check_preset(ps, {**NEW_PRESET, "compatible_printers": ["Some Other Printer"]}, "#FFFFFF", 1)
    with pytest.raises(BambuError, match="not found"):
        resolver.for_cli("filament", "No Such Filament")


def test_add_filament_duplicate_needs_flag(tmp_path):
    path = make_geo_3mf(tmp_path / "two.3mf", filaments=2)
    ps = Project(str(path)).project_settings
    slot = E.slot_from_preset(ps, {**NEW_PRESET, "name": "Test PLA"})
    with pytest.raises(E.EditError, match="--allow-duplicate"):
        E.EditSet([E.AddFilament(slot, "#00AE42", 1)]).apply(str(path), str(tmp_path / "x.3mf"))
    E.EditSet([E.AddFilament(slot, "#00AE42", 1, allow_duplicate=True)]).apply(str(path), str(tmp_path / "x.3mf"))
    assert len(Project(str(tmp_path / "x.3mf")).project_settings["filament_settings_id"]) == 3


def test_add_filament_from_preset_warns(tmp_path):
    path = one_filament(tmp_path)
    ps = Project(str(path)).project_settings
    es = E.EditSet([E.AddFilament(E.slot_from_preset(ps, NEW_PRESET), "#FCECD6", 2)])
    es.apply(str(path), str(tmp_path / "two.3mf"))
    assert any("not normalised by Studio" in n for n in es.notes)
    assert any("placeholder" in n for n in es.notes)
    assert len(Project(str(tmp_path / "two.3mf")).project_settings["flush_volumes_matrix"]) == 8


def test_set_filament_map_keeps_variant_blocks(tmp_path):
    path = make_geo_3mf(tmp_path / "two.3mf", filaments=2)
    PatchSet([SetFilamentMap((1, 2))]).apply(str(path))
    ps = Project(str(path)).project_settings
    assert len(ps["filament_extruder_variant"]) == 12
    assert E.variant_lookup(ps)[2] is not None


# ---------------------------------------------------------------------------- 17-20 move
@pytest.mark.parametrize("layout", ["gui", "component"])
def test_move_sets_min_corner(tmp_path, layout):
    path = make_geo_3mf(tmp_path / "g.3mf", layout=layout)
    ctf = comp_tf(path, "1")
    size = [h - lo for lo, h in zip(*box(path, "body"), strict=True)]
    out = tmp_path / "out.3mf"
    E.EditSet([E.MoveObject("body", 50, 60)]).apply(str(path), str(out))
    lo, hi = box(out, "body")
    assert (lo[0], lo[1], lo[2]) == pytest.approx((50, 60, 0))
    assert [h - x for x, h in zip(lo, hi, strict=True)] == pytest.approx(size)
    assert comp_tf(out, "1") == ctf


def test_move_plate_two_uses_plate_origin(tmp_path):
    path = make_geo_3mf(tmp_path / "p2.3mf", plates=2)
    out = tmp_path / "out.3mf"
    E.EditSet([E.MoveObject("label", 10, 10, plate=2)]).apply(str(path), str(out))
    lo, _ = box(out, "label")
    assert lo[0] == pytest.approx(10 + 256 * 1.2)
    assert geo_plate_origin(2, 2) == (256 * 1.2, 0.0)


def test_move_to_another_plate_moves_the_instance(tmp_path):
    path = make_geo_3mf(tmp_path / "p2.3mf", plates=2)
    out = tmp_path / "out.3mf"
    E.EditSet([E.MoveObject("body", 20, 20, plate=2)]).apply(str(path), str(out))
    plates = Project(str(out)).summary()["plates"]
    assert plates[0]["objects"] == [] and sorted(plates[1]["objects"]) == ["body", "label"]
    assert box(out, "body")[0][0] == pytest.approx(20 + 256 * 1.2)


def test_move_relative_and_dry_run(tmp_path, geo_file):
    out = tmp_path / "out.3mf"
    es = E.EditSet([E.MoveObject("body", 5, -5, relative=True)])
    notes = es.preview(str(geo_file))
    assert any("(45.000, 45.000) plate 1" in n and "centre" in n for n in notes)
    assert not out.exists()
    es.apply(str(geo_file), str(out))
    lo, _ = box(out, "body")
    assert (lo[0], lo[1]) == pytest.approx((GEO_BODY_AT[0] + 5, GEO_BODY_AT[1] - 5))


def test_move_reports_bed_and_reach_fail(tmp_path, geo_file):
    es = E.EditSet([E.MoveObject("label", 10, 100)])
    es.apply(str(geo_file), str(tmp_path / "out.3mf"))
    fails = [n for n in es.notes if "FAIL" in n]
    assert fails and "20.5" in fails[0]
    with pytest.raises(E.EditError, match="--strict"):
        E.EditSet([E.MoveObject("label", 10, 100, strict=True)]).apply(str(geo_file), str(tmp_path / "x.3mf"))


def test_move_overlap_is_a_fail_line(tmp_path, geo_file):
    es = E.EditSet([E.MoveObject("label", GEO_BODY_AT[0] + 2, GEO_BODY_AT[1] + 2)])
    es.apply(str(geo_file), str(tmp_path / "out.3mf"))
    assert any("overlap" in n for n in es.notes)


def test_move_unknown_object_and_plate_raise(geo_file):
    with pytest.raises(ProjectError, match="no object named"):
        E.EditSet([E.MoveObject("nope", 1, 1)]).apply(str(geo_file))
    with pytest.raises(ProjectError, match="plate 3 does not exist"):
        E.EditSet([E.MoveObject("body", 1, 1, plate=3)]).apply(str(geo_file))


def test_move_touches_only_the_item_transform(tmp_path, geo_file):
    out = tmp_path / "out.3mf"
    E.EditSet([E.MoveObject("body", 50, 60)]).apply(str(geo_file), str(out))
    a, b = entries(geo_file), entries(out)
    assert {n for n in b if a.get(n) != b[n]} == {MODEL}
    diff = [(x, y) for x, y in zip(a[MODEL].decode().splitlines(), b[MODEL].decode().splitlines(), strict=True) if x != y]
    assert len(diff) == 1 and 'objectid="2"' in diff[0][0]


# ---------------------------------------------------------------------------- 21-22 tower
def test_tower_per_plate_vector(tmp_path):
    """Plate 1 prints two filaments (the body's walls on slot 2), plate 2 is empty, plate 3 prints the label alone."""
    path = _body_part_meta(make_geo_3mf(tmp_path / "p3.3mf", plates=3), "wall_filament", "2")
    E.EditSet([E.SetTower(90, 100, plate=1)]).apply(str(path), str(tmp_path / "a.3mf"))
    ps = Project(str(tmp_path / "a.3mf")).project_settings
    assert ps["wipe_tower_x"] == ["90", "208", "208"] and ps["wipe_tower_y"] == ["100", "208", "208"]
    es = E.EditSet([E.SetTower(90, 100)])
    es.apply(str(path), str(tmp_path / "b.3mf"))
    assert Project(str(tmp_path / "b.3mf")).project_settings["wipe_tower_x"] == ["90"] * 3
    assert any("plate 2 prints with one filament: no prime tower there" in n for n in es.notes)
    with pytest.raises(E.EditError, match="no plate here prints with two"):
        E.EditSet([E.SetTower(90, 100, plate=2)]).apply(str(path))


def test_tower_size_counts_the_plates_filaments_not_the_loaded_slots(tmp_path):
    """Four loaded slots, a plate printing two: Studio sizes the tower for two (`plate_extruder_size`). For four the
    20 mm tower without the rib wall is deep enough to leave the bed at (230, 200); for two it fits."""
    path = make_geo_3mf(tmp_path / "f4.3mf", filaments=3, prime_tower_width="20", prime_tower_rib_wall="0", prime_tower_brim_width="2")
    from shambu import tower as T

    p = Project(str(path))
    four = dataclasses.replace(E.project_tower_settings(p), plate_filaments=4, width=20.0, rib_wall=False, brim=2.0)
    assert T.footprint(four, 10.0, 230, 200).rect[3] > 256  # sized for every loaded slot: off the bed
    es = E.EditSet([E.SetTower(230, 200)])
    es.apply(str(path), str(tmp_path / "o.3mf"))  # sized for the plate's two: accepted
    assert Project(str(tmp_path / "o.3mf")).project_settings["wipe_tower_x"] == ["230"]


def test_tower_off_bed_raises(tmp_path, geo_file):
    with pytest.raises(E.EditError, match="off the bed"):
        E.EditSet([E.SetTower(230, 30)]).apply(str(geo_file))  # a 33.2 mm tower at H 10: x 229.2..264
    with pytest.raises(E.EditError, match="extruder 2"):
        E.EditSet([E.SetTower(10, 30)]).apply(str(geo_file))
    with pytest.raises(E.EditError, match="no prime tower"):
        E.EditSet([E.SetTower(90, 100)]).apply(str(make_geo_3mf(tmp_path / "one.3mf", filaments=1)))
    with pytest.raises(ProjectError, match="plate 2 does not exist"):
        E.EditSet([E.SetTower(90, 100, plate=2)]).apply(str(geo_file))


# ---------------------------------------------------------------------------- idempotence per patch
@pytest.mark.parametrize(
    "make",
    [
        lambda tmp: E.ReplaceMesh("body", str(write_box_stl(tmp / "body-v2.stl", (0, 0, 0), (30, 20, 10), fine=True))),
        lambda tmp: E.MoveObject("body", 70, 80),
        lambda tmp: E.SetTower(90, 100),
    ],
    ids=["replace-mesh", "move", "tower"],
)
def test_edit_applied_twice_is_idempotent(tmp_path, geo_file, make):
    es = E.EditSet([make(tmp_path)])
    a, b = tmp_path / "a.3mf", tmp_path / "b.3mf"
    d1 = es.apply(str(geo_file), str(a))
    s1 = Project(str(a)).summary()
    d2 = es.apply(str(a), str(b))
    assert d1 == d2
    assert Project(str(b)).summary() | {"file": None} == s1 | {"file": None}
    assert a.read_bytes() == b.read_bytes()


# ---------------------------------------------------------------------------- 23-25 --no-check
def test_build_command_no_check_passthrough(tmp_path, stls, resolver):
    from test_pipeline import make_spec

    pl = Pipeline(FakeStudio(), resolver)
    plan = pl.plan(make_spec(tmp_path, stls))
    paths = pl.write_inputs(plan)
    args = pl.command(plan, paths, "x", do_slice=True, no_check=True).args()
    assert args.index("--no-check") < args.index("--slice=0")
    assert "--no-check" not in pl.command(plan, paths, "x", do_slice=True).args()


@dataclass
class SequenceStudio(FakeStudio):
    """Exit codes and log texts per call, in order. Each call writes its log and a result.json into its outputdir, and a
    call with exit 0 writes the fixture to the expected output."""

    codes: list[int] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)

    def run(self, cmd, *, name="run", outputdir=None, timeout=1200, expect_output=None, debug=None) -> RunResult:
        import shutil

        args = cmd.args()
        self.calls.append(("run", args))
        i = len([c for c in self.calls if c[0] == "run"]) - 1
        code = self.codes[i] if i < len(self.codes) else 0
        d = Path(outputdir or self.scratch)
        d.mkdir(parents=True, exist_ok=True)
        log = d / f"{name}.log"
        log.write_text("$ fake\n" + (self.logs[i] if i < len(self.logs) else ""), encoding="utf-8")
        (d / "result.json").write_text(json.dumps({"return_code": code, "call": i}), encoding="utf-8")
        if code == 0 and expect_output is not None and self.fixture is not None:
            shutil.copyfile(self.fixture, expect_output)
        return RunResult(["BambuStudio", *args], code % 256, code, 0.1, log, {"return_code": code}, expect_output)


CONFLICT_LOG = "[error]   gcode path conflicts found between a and b\n"


def _cmd(nc: bool, plate: int, d: Path) -> CliCommand:
    return CliCommand().slice(plate, no_check=nc).export_3mf("x.gcode.3mf", d)


def _two_pass(studio, tmp_path, plates=(0,)):
    return two_pass_slice(
        studio, _cmd, name="x", outputdir=tmp_path, timeout=10, expect_output=tmp_path / "x.gcode.3mf", no_check=True, plates=plates
    )


def test_slice_no_check_two_pass(tmp_path, stls, resolver):
    from test_pipeline import make_spec

    fixture = make_3mf(tmp_path / "sliced.3mf", sliced=True, object_names=("body_1", "assemble_1"), change_filament_gcode="M600")
    studio = SequenceStudio(fixture, codes=[-101, 0], logs=[CONFLICT_LOG, ""])
    r = Pipeline(studio, resolver).build(make_spec(tmp_path, stls), do_slice=True, no_check=True)
    runs = [c[1] for c in studio.calls if c[0] == "run"]
    assert len(runs) == 2 and "--no-check" not in runs[0] and "--no-check" in runs[1]
    assert r.checks_skipped is not None and r.checks_skipped["pair"] == ["a", "b"]
    assert r.project.is_file()
    doc = r.to_json()
    assert "G-code path conflict check (exit -101)" in doc["checks_skipped"]["checks"]
    assert "found crossing G-code paths between a and b (exit -101); exported with --no-check" in doc["checks_skipped"]["report_line"]
    assert not any("--no-check" in w for w in doc["warnings"])  # the line sits under the estimates, not in Warnings
    est = r.report.split("## Export", 1)[1]
    assert "- plate 1: Checks skipped: crossing G-code paths between a and b (exit -101). Exported with --no-check" in est
    assert "Studio estimate" not in r.checks_skipped["report_line"]  # the estimate line alone names the estimate
    # the --no-check export still gets the build's strict verify (re-slice check and the tower plan's vectors)
    assert any(v.startswith("wipe_tower_x in file is") for v in r.verify_problems or [])


def test_checks_skipped_lines_one_per_plate():
    from shambu.report import checks_skipped_lines

    cs = {
        "skipped": True,
        "report_line": "slice: ...",
        "plates": [
            {"plate": 1, "kind": "skippable", "reason": "crossing G-code paths between a and b (exit -101)"},
            {"plate": 2, "kind": "ok", "reason": "passed the checks"},
            {"plate": 3, "kind": "skippable", "reason": "empty layers or overlap: x (exit -100)"},
        ],
    }
    lines = checks_skipped_lines(cs)
    assert [x.split(": Checks skipped: ")[0] for x in lines] == ["- plate 1", "- plate 3"]
    assert checks_skipped_lines({"skipped": False, "report_line": "slice: the plate failed"}) == ["- slice: the plate failed"]
    assert checks_skipped_lines(None) == []


def test_no_check_does_not_mask_unprintable(tmp_path):
    studio = SequenceStudio(None, codes=[-102, 0], logs=["[error]   plate 1: path in the unprintable area\n"])
    rr, skipped = _two_pass(studio, tmp_path)
    assert rr.code == -102 and not rr.ok and skipped and skipped["skipped"] is False
    assert "unprintable area" in skipped["report_line"] and "no unchecked pass ran" in skipped["report_line"]
    assert len(studio.calls) == 1


def test_no_check_one_pass_when_the_first_slice_passes(tmp_path):
    studio = SequenceStudio(make_3mf(tmp_path / "f.3mf", sliced=True), codes=[0])
    rr, skipped = _two_pass(studio, tmp_path)
    assert skipped is None and len(studio.calls) == 1
    assert rr.ok and rr.output == tmp_path / "x.gcode.3mf" and rr.output.is_file()


def test_no_check_pair_not_in_log_still_continues(tmp_path):
    studio = SequenceStudio(make_3mf(tmp_path / "f.3mf", sliced=True), codes=[-101, 0], logs=["nothing useful\n"])
    _, skipped = _two_pass(studio, tmp_path)
    assert skipped and skipped["pair"] is None and "its log names no objects" in skipped["report_line"]


def test_no_check_keeps_both_passes_logs_and_results(tmp_path):
    studio = SequenceStudio(make_3mf(tmp_path / "f.3mf", sliced=True), codes=[-101, 0], logs=[CONFLICT_LOG, ""])
    rr, skipped = _two_pass(studio, tmp_path)
    assert rr.ok and skipped and skipped["skipped"]
    first, second = Path(skipped["first_log"]), Path(skipped["second_log"])
    assert first.parent != second.parent and first.is_file() and second.is_file()
    assert "gcode path conflicts" in first.read_text()
    assert json.loads((first.parent / "result.json").read_text())["return_code"] == -101
    assert json.loads(Path(skipped["second_result"]).read_text())["return_code"] == 0


def test_no_check_checks_every_plate(tmp_path):
    """Studio stops at the first conflicting plate, so each plate gets its own checked pass and its own line."""
    logs = [CONFLICT_LOG, "", CONFLICT_LOG.replace("a and b", "c and d"), ""]
    studio = SequenceStudio(make_3mf(tmp_path / "f.3mf", sliced=True), codes=[-101, 0, -101, 0], logs=logs)
    rr, skipped = _two_pass(studio, tmp_path, plates=(1, 2, 3))
    runs = [c[1] for c in studio.calls]
    assert [r for r in runs[0] if r.startswith("--slice")] == ["--slice=1"] and "--slice=3" in runs[2]
    assert "--no-check" in runs[3] and "--slice=0" in runs[3]
    assert rr.ok and skipped
    assert [(p["plate"], p["kind"]) for p in skipped["plates"]] == [(1, "skippable"), (2, "ok"), (3, "skippable")]
    assert "plate 1: crossing G-code paths between a and b (exit -101)" in skipped["report_line"]
    assert "plate 3: crossing G-code paths between c and d (exit -101)" in skipped["report_line"]


def test_no_check_exception_exit_100_is_not_retried(tmp_path):
    log = "[error]   found slicing or export error for partplate 1\n[error]   extruder_nozzle_stats size mismatch\n"
    studio = SequenceStudio(None, codes=[-100, 0], logs=[log])
    rr, skipped = _two_pass(studio, tmp_path)
    assert rr.code == -100 and len(studio.calls) == 1
    assert skipped and skipped["skipped"] is False and "extruder_nozzle_stats size mismatch" in skipped["report_line"]


def test_no_check_slicing_warning_exit_100_is_retried(tmp_path):
    log = "[warning] plate 1: found slicing warnings: Empty layers around bottom, no_check=0\n"
    studio = SequenceStudio(make_3mf(tmp_path / "f.3mf", sliced=True), codes=[-100, 0], logs=[log])
    rr, skipped = _two_pass(studio, tmp_path)
    assert rr.ok and skipped and "empty layers or overlap: Empty layers around bottom (exit -100)" in skipped["report_line"]


def test_two_pass_uses_the_one_conflict_parser(tmp_path):
    """studio.conflicts_in_log reads the timestamped line, splits at the known CLI names (a name may hold " and "), and
    the two-pass line reads as export_fails does."""
    line = "[2026-10-21 15:35:34.720818] [0x00000001f34c2180] [error]   gcode path conflicts found between camera_housing_v2_print and housing_print\n"
    assert conflicts_in_log(line) == [("camera_housing_v2_print", "housing_print")]
    assert conflicts_in_log("no conflict here") == []
    log = "[error]   gcode path conflicts found between nut and bolt_1 and base_1\n"
    studio = SequenceStudio(make_3mf(tmp_path / "f.3mf", sliced=True), codes=[-101, 0], logs=[log, ""])
    _, skipped = two_pass_slice(
        studio, _cmd, name="x", outputdir=tmp_path, timeout=10, expect_output=tmp_path / "x.gcode.3mf", no_check=True,
        known={"nut and bolt_1", "base_1"},
    )  # fmt: skip
    assert skipped and skipped["pair"] == ["nut and bolt_1", "base_1"]
    assert "Studio's checks found crossing G-code paths between nut and bolt_1 and base_1 (exit -101);" in skipped["report_line"]


def test_pipeline_no_check_needs_slice(tmp_path, stls, resolver):
    from test_pipeline import make_spec

    with pytest.raises(BambuError, match="--no-check needs --slice"):
        Pipeline(FakeStudio(), resolver).build(make_spec(tmp_path, stls), no_check=True)


# ---------------------------------------------------------------------------- 26 the escape hatch rules
def test_studio_hatch_rules(tmp_path):
    (tmp_path / "x.3mf").write_bytes(b"")
    args = CliCommand.parse(["--slice", "1", "x.3mf"], tmp_path).args()
    assert args == ["--slice=1", str(tmp_path / "x.3mf")]
    args = CliCommand.parse(["--slice", "1", "--no-check", "--min-save", "1", "x.3mf"], tmp_path).args()
    assert args.index("--no-check") < args.index("--slice=1") and "--min-save=1" in args
    args = CliCommand.parse(["--load-settings", "m.json;p.json", "--export-3mf", "o.3mf", "x.3mf"], tmp_path).args()
    assert f"{tmp_path / 'm.json'};{tmp_path / 'p.json'}" in args and args[args.index("--export-3mf") + 1] == "o.3mf"
    for bad, msg in [
        (["--export-3mf", "/tmp/a.3mf", "x.3mf"], "bare file name"),
        (["--pipe", "p", "--slice", "1", "x.3mf"], "refused"),
        (["--datadir", "d", "--slice", "1", "x.3mf"], "refused"),
        (["--uptodate", "--slice", "1", "x.3mf"], "refused"),
        (["--slice", "1", "../outside.3mf"], "outside the working tree"),
        (["x.3mf"], "no action"),
        (["-o", "x", "--slice", "1"], "short options"),
    ]:
        with pytest.raises(StudioError, match=msg):
            CliCommand.parse(bad, tmp_path)


def test_studio_hatch_run_folder(tmp_path, monkeypatch, capsys, resolver):
    from shambu import cli
    from shambu.facade import Bambu

    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.3mf").write_bytes(b"")
    monkeypatch.setattr(cli, "Bambu", lambda: Bambu(studio=SequenceStudio(None, codes=[-101]), resolver=resolver))
    code = cli.main(["--json", "studio", "--out", str(tmp_path / "runs"), "--", "--slice", "1", "x.3mf"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 1 and doc["exit"] == -101
    run_dir = Path(doc["run_dir"])
    assert run_dir.parent == tmp_path / "runs" / "studio-runs"
    assert {p.name for p in run_dir.iterdir()} >= {"command.json", "studio.log", "result.json", "exit.txt"}
    assert (run_dir / "exit.txt").read_text() == "exit -101 (G-code path conflicts)\n"
    assert json.loads((run_dir / "command.json").read_text())["raw"] == ["--slice", "1", "x.3mf"]


# ---------------------------------------------------------------------------- review findings (shared mesh, verify, shapes)
def test_replace_mesh_refuses_a_shared_mesh(tmp_path):
    path = make_geo_3mf(tmp_path / "shared.3mf")
    rewrite(
        path,
        MODEL,
        lambda t: t.replace('p:path="/3D/Objects/object_2.model" objectid="3"', 'p:path="/3D/Objects/object_1.model" objectid="1"'),
    )
    raw = path.read_bytes()
    stl = write_box_stl(tmp_path / "fine.stl", (0, 0, 0), (30, 20, 10), fine=True)
    with pytest.raises(E.EditError, match=r"object 'body' shares its mesh with \['label'\]"):
        replace(path, stl)
    assert path.read_bytes() == raw  # the unselected object (and everything else) byte-identical


def test_replace_mesh_same_bounds_other_topology_is_not_a_noop(tmp_path, geo_file):
    from conftest import box_facets
    from shambu import mesh as M

    facets = box_facets((0, 0, 0), (30, 20, 10))
    a, b = facets[0], facets[1]  # the bottom quad, split along the other diagonal: same vertices, same count
    q = [a[0], a[1], a[2], b[1]]
    facets[0], facets[1] = (q[0], q[1], q[3]), (q[1], q[2], q[3])
    stl = M.write_stl(tmp_path / "flipped.stl", facets)
    replace(geo_file, stl, tmp_path / "out.3mf")
    got = E.read_mesh(entries(tmp_path / "out.3mf")["3D/Objects/object_1.model"].decode(), "1")
    new = E.load_stl(stl)
    old = E.read_mesh(entries(geo_file)["3D/Objects/object_1.model"].decode(), "1")
    assert E.canonical_triangles(got.vertices, got.triangles) == E.canonical_triangles(new.local, new.triangles)
    assert E.canonical_triangles(got.vertices, got.triangles) != E.canonical_triangles(old.vertices, old.triangles)


def test_failed_verification_leaves_the_original(tmp_path, geo_file, monkeypatch):
    def broken(self, p, before=None):
        raise E.EditError("re-open check: forced")

    monkeypatch.setattr(E.ReplaceMesh, "verify", broken)
    raw = geo_file.read_bytes()
    with pytest.raises(E.EditError, match="forced"):
        replace(geo_file, write_box_stl(tmp_path / "fine.stl", (0, 0, 0), (30, 20, 10), fine=True))  # in place
    assert geo_file.read_bytes() == raw
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_replace_mesh_keeps_an_intentional_sink(tmp_path):
    path = make_geo_3mf(tmp_path / "sunk.3mf")
    rewrite(path, MODEL, lambda t: t.replace('transform="1 0 0 0 1 0 0 0 1 55 60 5"', 'transform="1 0 0 0 1 0 0 0 1 55 60 3"'))
    out = tmp_path / "out.3mf"
    replace(path, write_box_stl(tmp_path / "fine.stl", (0, 0, 0), (30, 20, 10), fine=True), out)
    assert box(out, "body")[0][2] == pytest.approx(-2.0)


def test_add_filament_two_to_three_keeps_flush_cells(tmp_path):
    path = make_geo_3mf(tmp_path / "two.3mf", filaments=2)
    ps = Project(str(path)).project_settings
    scratch = scratch_settings(ps, 2)
    scratch["flush_volumes_matrix"] = [str(100 + i) for i in range(18)]
    out = tmp_path / "three.3mf"
    E.EditSet([E.AddFilament(E.slot_from_export(ps, normalised_settings(ps), NEW_PRESET, scratch), "#FCECD6", 1)]).apply(
        str(path), str(out)
    )
    new = Project(str(out)).project_settings
    assert new["filament_self_index"] == ["1"] * 6 + ["2"] * 6 + ["3"] * 6
    fvm = new["flush_volumes_matrix"]
    assert len(fvm) == 18
    for ex in range(2):  # the old 2 x 2 cells of each extruder block stay; the new row and column come from Studio
        assert [fvm[ex * 9 + i * 3 + j] for i in range(2) for j in range(2)] == ["0", "300", "200", "0"]
        assert fvm[ex * 9 + 2] == str(100 + ex * 9 + 2)
    assert [pl["filament_maps"] for pl in Project(str(out)).summary()["plates"]] == ["1 2 1"]


def test_add_filament_unequal_variant_counts(tmp_path):
    path = one_filament(tmp_path)
    ps = Project(str(path)).project_settings
    two_variants = {
        **NEW_PRESET,
        "filament_extruder_variant": ["Direct Drive Standard", "Bowden Standard"],
        "nozzle_temperature": ["240", "245"],
        "filament_flush_temp_fast": ["0", "0"],
    }
    out = tmp_path / "two.3mf"
    E.EditSet([E.AddFilament(E.slot_from_preset(ps, two_variants), "#FCECD6", 2)]).apply(str(path), str(out))
    new = Project(str(out)).project_settings
    assert new["filament_self_index"] == ["1"] * 6 + ["2"] * 2
    assert new["nozzle_temperature"][6:] == ["240", "245"]
    assert len(new["filament_flush_temp_fast"]) == 8  # the one-entry vector written out (6) plus the new block (2)
    assert E.variant_lookup(new) == {1: 0, 2: 7}


def test_add_filament_rejects_an_ambiguous_shape(tmp_path):
    path = make_geo_3mf(tmp_path / "odd.3mf", filaments=1, nozzle_temperature=["220", "225"])
    ps = Project(str(path)).project_settings
    with pytest.raises(E.EditError, match="nozzle_temperature has 2 entries"):
        E.EditSet([E.AddFilament(E.slot_from_preset(ps, NEW_PRESET), "#FCECD6", 2)]).apply(str(path))


def test_slot_presets_come_from_the_project(tmp_path):
    path = make_geo_3mf(tmp_path / "gone.3mf", filaments=1, filament_settings_id=["My Deleted PLA"])
    ps = Project(str(path)).project_settings
    (old,) = E.slot_presets(ps, NEW_PRESET)
    assert old["name"] == "My Deleted PLA" and old["from"] == "User" and old["inherits"] == ""
    assert old["nozzle_temperature"] == ps["nozzle_temperature"] and len(old["filament_extruder_variant"]) == 6
    assert old["filament_colour"] == ["#00AE42"]


def test_settings_patch_on_a_sliced_archive_declares_the_invalidation(tmp_path):
    from shambu.patches import SetBedType

    path = make_geo_3mf(tmp_path / "sliced.3mf", sliced=True)
    PatchSet([SetBedType("Textured PEI Plate")]).apply(str(path), str(tmp_path / "a.3mf"))
    assert "Metadata/plate_1.gcode" not in entries(tmp_path / "a.3mf")
    E.EditSet([E.SetTower(90, 100)]).apply(str(path), str(tmp_path / "b.3mf"))  # declares SLICE_TOUCHES + model settings
    b = entries(tmp_path / "b.3mf")
    assert "Metadata/plate_1.gcode" not in b and "Metadata/plate_1.gcode.md5" not in b and "Metadata/plate_1.json" not in b
    assert b"<plate" not in b["Metadata/slice_info.config"]


@dataclass(frozen=True)
class _DropsSlices(PatchBase):
    """Declares project_settings only, but its change drops the slice (an undeclared deletion)."""

    touches: frozenset[str] = frozenset({"Metadata/project_settings.config"})

    def apply(self, p: Project) -> None:
        p.project_settings["wall_loops"] = "3"
        p.touch_project_settings()

    def describe(self) -> str:
        return "wall_loops 3"


def test_undeclared_deletion_raises(tmp_path):
    path = make_geo_3mf(tmp_path / "sliced.3mf", sliced=True)
    raw = path.read_bytes()
    with pytest.raises(ProjectError, match=r"Metadata/plate_1\.gcode"):
        E.EditSet([_DropsSlices()]).apply(str(path))
    assert path.read_bytes() == raw


# ---------------------------------------------------------------------------- review round 2
def _two_parts_sharing(path: Path) -> Path:
    """Body with a second part (sub-object 1 again): two components of one object name the same mesh."""
    rewrite(
        path,
        MODEL,
        lambda t: t.replace(
            '<component p:path="/3D/Objects/object_1.model" objectid="1" p:UUID="00010000-b206-40ff-9872-83e8017abed1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>',
            '<component p:path="/3D/Objects/object_1.model" objectid="1" p:UUID="00010000-b206-40ff-9872-83e8017abed1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n'
            '    <component p:path="/3D/Objects/object_1.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 20"/>',
        ),
    )
    return path


def test_replace_mesh_refuses_a_mesh_shared_inside_one_object(tmp_path):
    path = _two_parts_sharing(make_geo_3mf(tmp_path / "s.3mf"))
    raw = path.read_bytes()
    with pytest.raises(E.EditError, match="shares its mesh"):
        replace(path, write_box_stl(tmp_path / "fine.stl", (0, 0, 0), (30, 20, 10), fine=True))
    assert path.read_bytes() == raw


def test_replace_mesh_refuses_mesh_shared_metadata(tmp_path):
    path = make_geo_3mf(tmp_path / "m.3mf")
    rewrite(
        path,
        "Metadata/model_settings.config",
        lambda t: t.replace(
            '<metadata key="name" value="label_1"/>',
            '<metadata key="name" value="label_1"/>\n      <metadata key="mesh_shared" value="1"/>',
        ),
    )
    with pytest.raises(E.EditError, match=r"shares its mesh with \['label'\]"):
        replace(path, write_box_stl(tmp_path / "fine.stl", (0, 0, 0), (30, 20, 10), fine=True))
    with pytest.raises(E.EditError, match="mesh_shared"):
        E.EditSet([E.ReplaceMesh("label", str(write_box_stl(tmp_path / "l.stl", (-5, -5, 0), (5, 5, 2), fine=True)))]).apply(str(path))


def test_add_filament_expands_one_entry_vectors(tmp_path):
    path = make_geo_3mf(
        tmp_path / "short.3mf",
        filaments=2,
        filament_colour=["#00AE42"],
        filament_multi_colour=["#00AE42"],
        filament_map=["1"],
        pressure_advance=["0.02"],
    )
    ps = Project(str(path)).project_settings
    slot = E.slot_from_preset(ps, NEW_PRESET)
    es = E.EditSet([E.AddFilament(slot, "#FCECD6", 2)])
    a, b = tmp_path / "a.3mf", tmp_path / "b.3mf"
    es.apply(str(path), str(a))
    new = Project(str(a)).project_settings
    assert new["filament_colour"] == ["#00AE42", "#00AE42", "#FCECD6"]
    assert new["filament_multi_colour"] == ["#00AE42", "#00AE42", "#FCECD6"]
    assert new["filament_map"] == ["1", "1", "2"]
    assert new["pressure_advance"] == ["0.02"] * 3
    assert any("slot 1's value copied" in n and "pressure_advance" in n for n in es.notes) or any(
        "copied from slot 1" in n and "pressure_advance" in n for n in es.notes
    )
    es.apply(str(a), str(b))  # the same request again: no fourth slot
    assert len(Project(str(b)).project_settings["filament_settings_id"]) == 3
    assert a.read_bytes() == b.read_bytes()


def test_add_filament_variant_key_missing_from_the_preset_is_reported(tmp_path):
    path = one_filament(tmp_path)
    ps = Project(str(path)).project_settings
    preset = {k: v for k, v in NEW_PRESET.items() if k != "nozzle_temperature"}
    es = E.EditSet([E.AddFilament(E.slot_from_preset(ps, preset), "#FCECD6", 2)])
    es.apply(str(path), str(tmp_path / "two.3mf"))
    new = Project(str(tmp_path / "two.3mf")).project_settings
    assert new["nozzle_temperature"] == ps["nozzle_temperature"] + [ps["nozzle_temperature"][0]] * 6
    assert any("slot 1's value copied" in n and "nozzle_temperature" in n for n in es.notes)


def test_move_uses_the_destination_plate_map(tmp_path):
    path = make_geo_3mf(tmp_path / "p2.3mf", plates=2)

    def plate2_reversed(t: str) -> str:
        head, sep, tail = t.partition('<metadata key="plater_id" value="2"/>')
        return head + sep + tail.replace('<metadata key="filament_maps" value="1 2"/>', '<metadata key="filament_maps" value="2 1"/>', 1)

    rewrite(path, "Metadata/model_settings.config", plate2_reversed)
    with pytest.raises(E.EditError, match="extruder 2"):  # body is filament 1, which plate 2 prints on extruder 2
        E.EditSet([E.MoveObject("body", 10, 120, plate=2, strict=True)]).apply(str(path))
    E.EditSet([E.MoveObject("body", 10, 120, strict=True)]).apply(str(path), str(tmp_path / "ok.3mf"))  # plate 1: extruder 1


def test_move_checks_every_instance_of_a_neighbour(tmp_path):
    path = make_geo_3mf(tmp_path / "inst.3mf")
    rewrite(
        path,
        MODEL,
        lambda t: t.replace(" </build>", '  <item objectid="4" transform="1 0 0 0 1 0 0 0 1 105 105 1" printable="1"/>\n </build>'),
    )
    rewrite(
        path,
        "Metadata/model_settings.config",
        lambda t: t.replace(
            "  </plate>",
            '    <model_instance>\n      <metadata key="object_id" value="4"/>\n      <metadata key="instance_id" value="1"/>\n    </model_instance>\n  </plate>',
        ),
    )
    es = E.EditSet([E.MoveObject("body", 90, 95)])
    es.apply(str(path), str(tmp_path / "out.3mf"))
    assert any("label (instance 1)" in n and n.startswith("  FAIL") for n in es.notes)
    assert not any("label (instance 0)" in n for n in es.notes)


def test_replace_mesh_anchor_min_keeps_a_sink(tmp_path):
    path = make_geo_3mf(tmp_path / "sunk.3mf")
    rewrite(path, MODEL, lambda t: t.replace('transform="1 0 0 0 1 0 0 0 1 55 60 5"', 'transform="1 0 0 0 1 0 0 0 1 55 60 3"'))
    out = tmp_path / "out.3mf"
    replace(path, write_box_stl(tmp_path / "fine.stl", (0, 0, 0), (30, 20, 10), fine=True), out, anchor="min")
    lo, hi = box(out, "body")
    assert (lo[0], lo[1], lo[2], hi[2]) == pytest.approx((GEO_BODY_AT[0], GEO_BODY_AT[1], -2.0, 8.0))


def test_tower_footprint_must_fit_extruder_two(tmp_path, geo_file):
    with pytest.raises(E.EditError, match=r"x 20\.2\.\.53\.4.*extruder 2"):  # x 21 passes the corner, not the 0.8 mm brim
        E.EditSet([E.SetTower(21, 100)]).apply(str(geo_file))
    E.EditSet([E.SetTower(29, 100)]).apply(str(geo_file), str(tmp_path / "ok.3mf"))


def test_tower_refuses_non_finite_values(geo_file):
    with pytest.raises(E.EditError, match="finite"):
        E.EditSet([E.SetTower(float("nan"), 30)]).apply(str(geo_file))


def test_final_combined_pass_is_classified(tmp_path):
    studio = SequenceStudio(make_3mf(tmp_path / "f.3mf", sliced=True), codes=[0, 0, -101, 0], logs=["", "", CONFLICT_LOG, ""])
    rr, skipped = _two_pass(studio, tmp_path, plates=(1, 2))
    runs = [c[1] for c in studio.calls]
    assert len(runs) == 4 and "--no-check" in runs[3]
    assert rr.ok and skipped and skipped["skipped"]
    assert [(p["plate"], p["kind"]) for p in skipped["plates"]] == [(1, "ok"), (2, "ok"), (0, "skippable")]
    assert "all plates together: crossing G-code paths between a and b (exit -101)" in skipped["report_line"]


def test_variant_lookup_maps_hybrid_to_standard():
    ps = {
        "filament_settings_id": ["a"],
        "filament_map": ["1"],
        "extruder_type": ["Direct Drive"],
        "nozzle_volume_type": ["Hybrid"],
        "filament_extruder_variant": list(VARIANTS),
        "filament_self_index": ["1"] * 6,
    }
    assert E.variant_lookup(ps) == {1: 0}


def test_relative_move_applied_twice_is_a_no_op(tmp_path, geo_file):
    es = E.EditSet([E.MoveObject("body", 5, 5, relative=True)])
    a, b = tmp_path / "a.3mf", tmp_path / "b.3mf"
    d1 = es.apply(str(geo_file), str(a))
    d2 = es.apply(str(a), str(b))
    assert d1 == d2 == ["object 'body': moved to min corner (45, 55) on plate 1"]
    assert a.read_bytes() == b.read_bytes()
    assert box(b, "body")[0][:2] == pytest.approx((45, 55))


def test_malformed_project_data_is_an_edit_error(tmp_path):
    path = make_geo_3mf(tmp_path / "bad.3mf")
    rewrite(path, MODEL, lambda t: t.replace('transform="1 0 0 0 1 0 0 0 1 55 60 5"', 'transform="1 0 0 0 1 0 0 0 1 55 sixty 5"'))
    with pytest.raises(E.EditError, match="sixty"):
        E.EditSet([E.MoveObject("body", 1, 1)]).apply(str(path))
    path = make_geo_3mf(tmp_path / "bad2.3mf")
    rewrite(path, "3D/Objects/object_1.model", lambda t: t.replace(' z="-5"/>', "/>", 1))
    with pytest.raises(E.EditError, match="sub-object 1: a <vertex> or <triangle> lacks"):
        E.EditSet([E.MoveObject("body", 1, 1)]).apply(str(path))


def test_dry_run_checks_the_touches(geo_file):
    with pytest.raises(ProjectError, match="did not declare"):
        E.EditSet([_Sneaky()]).preview(str(geo_file))


def test_frame_anchor_with_component_and_item_rotation(tmp_path):
    path = make_geo_3mf(tmp_path / "rot.3mf")
    rewrite(
        path,
        MODEL,
        lambda t: t.replace('transform="1 0 0 0 1 0 0 0 1 55 60 5"', f'transform="{ROT90} 55 60 5"').replace(
            'objectid="1" p:UUID="00010000-b206-40ff-9872-83e8017abed1" transform="1 0 0 0 1 0 0 0 1 0 0 0"',
            f'objectid="1" p:UUID="00010000-b206-40ff-9872-83e8017abed1" transform="{ROT90} 0 0 0"',
        ),
    )
    tf = item_tf(path, "2")
    out = tmp_path / "out.3mf"
    replace(path, write_box_stl(tmp_path / "longer.stl", (0, 0, 0), (36, 20, 10)), out, allow_size_change=True)
    new = item_tf(out, "2")
    assert [new[9 + i] - tf[9 + i] for i in range(3)] == pytest.approx([-3.0, 0.0, 0.0], abs=1e-6)  # 180 degrees: +3 in x -> -3


def test_move_verify_checks_size_and_z(tmp_path, geo_file):
    out = tmp_path / "out.3mf"
    E.EditSet([E.MoveObject("body", 50, 60)]).apply(str(geo_file), str(out))
    E.MoveObject("body", 50, 60).verify(Project(str(out)), Project(str(geo_file)))
    rewrite(out, MODEL, lambda t: t.replace("1 0 0 0 1 0 0 0 1 65 70 5", "1 0 0 0 1 0 0 0 1 65 70 7"))
    with pytest.raises(E.EditError, match="re-open check"):
        E.MoveObject("body", 50, 60).verify(Project(str(out)), Project(str(geo_file)))


def test_replace_mesh_verify_checks_the_written_triangles(tmp_path, geo_file):
    stl = write_box_stl(tmp_path / "fine.stl", (0, 0, 0), (30, 20, 10), fine=True)
    out = tmp_path / "out.3mf"
    replace(geo_file, stl, out)
    rm = E.ReplaceMesh("body", str(stl))
    rm.verify(Project(str(out)), Project(str(geo_file)))
    rewrite(
        out, "3D/Objects/object_1.model", lambda t: t.replace('<triangle v1="0" v2="1" v3="2"/>', '<triangle v1="0" v2="2" v3="1"/>', 1)
    )
    with pytest.raises(E.EditError, match="re-open check"):
        rm.verify(Project(str(out)), Project(str(geo_file)))


def test_add_filament_then_map_keeps_the_blocks(tmp_path):
    path = one_filament(tmp_path)
    E.EditSet([add_patch(path), SetFilamentMap((1, 2))]).apply(str(path), str(tmp_path / "two.3mf"))
    ps = Project(str(tmp_path / "two.3mf")).project_settings
    assert ps["filament_self_index"] == ["1"] * 6 + ["2"] * 6 and len(ps["filament_extruder_variant"]) == 12
    assert E.variant_lookup(ps) == {1: 0, 2: 9}


def test_studio_hatch_contains_every_path(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "x.3mf").write_bytes(b"")
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (root / "link.json").symlink_to(outside)
    (root / "a.json").write_text(json.dumps({"plates": [{"objects": [{"path": str(tmp_path / "far.stl")}]}]}))
    for bad, msg in [
        (["--load-settings", str(outside), "--slice", "1", "x.3mf"], "outside the working tree"),
        (["--load-settings", "../outside.json", "--slice", "1", "x.3mf"], "outside the working tree"),
        (["--load-settings", "link.json", "--slice", "1", "x.3mf"], "outside the working tree"),
        (["--load-filaments", "x.json;../outside.json", "--slice", "1", "x.3mf"], "outside the working tree"),
        (["--load-assemble-list", "a.json", "--slice", "1"], "assemble-list model path"),
        (["--slice", "oops", "x.3mf"], "whole number"),
    ]:
        with pytest.raises(StudioError, match=msg):
            CliCommand.parse(bad, root)


# ---------------------------------------------------------------------------- review round 3
def test_add_filament_extends_a_key_the_preset_omits(tmp_path):
    path = make_geo_3mf(tmp_path / "two.3mf", filaments=2)
    ps = Project(str(path)).project_settings
    preset = {k: v for k, v in NEW_PRESET.items() if k != "filament_density"}
    es = E.EditSet([E.AddFilament(E.slot_from_preset(ps, preset), "#FCECD6", 1)])
    es.apply(str(path), str(tmp_path / "three.3mf"))
    new = Project(str(tmp_path / "three.3mf")).project_settings
    assert new["filament_density"] == ["1.24", "1.24", "1.24"]
    assert any("slot 1's value copied" in n and "filament_density" in n for n in es.notes)


def test_add_filament_prefers_the_supplied_value_over_a_short_vector(tmp_path):
    path = make_geo_3mf(tmp_path / "two.3mf", filaments=2, pressure_advance=["0.02"])
    ps = Project(str(path)).project_settings
    es = E.EditSet([E.AddFilament(E.slot_from_preset(ps, {**NEW_PRESET, "pressure_advance": ["0.08"]}), "#FCECD6", 1)])
    es.apply(str(path), str(tmp_path / "three.3mf"))
    assert Project(str(tmp_path / "three.3mf")).project_settings["pressure_advance"] == ["0.02", "0.02", "0.08"]
    assert not any("pressure_advance" in n for n in es.notes)


def test_composed_relative_moves_add_up(tmp_path, geo_file):
    out = tmp_path / "out.3mf"
    es = E.EditSet([E.MoveObject("body", 5, 5, relative=True), E.MoveObject("body", 5, 5, relative=True)])
    es.apply(str(geo_file), str(out))
    assert box(out, "body")[0][:2] == pytest.approx((GEO_BODY_AT[0] + 10, GEO_BODY_AT[1] + 10))
    again = tmp_path / "again.3mf"
    es.apply(str(out), str(again))  # the set now holds the two absolute targets: a no-op
    assert again.read_bytes() == out.read_bytes()


def test_rename_then_relative_move_and_move_then_rename(tmp_path, geo_file):
    from shambu.patches import RenameObject

    out = tmp_path / "a.3mf"
    E.EditSet([RenameObject("body", "shell"), E.MoveObject("shell", 5, 5, relative=True)]).apply(str(geo_file), str(out))
    assert box(out, "shell")[0][:2] == pytest.approx((GEO_BODY_AT[0] + 5, GEO_BODY_AT[1] + 5))
    out2 = tmp_path / "b.3mf"
    E.EditSet([E.MoveObject("body", 70, 80), RenameObject("body", "shell")]).apply(str(geo_file), str(out2))
    assert box(out2, "shell")[0][:2] == pytest.approx((70, 80))


def test_replace_with_a_size_change_then_move(tmp_path, geo_file):
    stl = write_box_stl(tmp_path / "wide.stl", (0, 0, 0), (40, 20, 10), fine=True)
    out = tmp_path / "out.3mf"
    E.EditSet([E.ReplaceMesh("body", str(stl), allow_size_change=True), E.MoveObject("body", 70, 80)]).apply(str(geo_file), str(out))
    lo, hi = box(out, "body")
    assert (lo[0], lo[1], hi[0] - lo[0]) == pytest.approx((70, 80, 40))


@dataclass(frozen=True)
class _CachedSneak(PatchBase):
    """Declares project_settings only, but changes the parsed model settings (no slice to drop)."""

    touches: frozenset[str] = frozenset({"Metadata/project_settings.config"})

    def apply(self, p: Project) -> None:
        p.model_settings.find("object").set("id", "2")  # type: ignore[union-attr]
        next(m for m in p.model_settings.iter("metadata") if m.get("key") == "brim_type").set("value", "outer_only")
        p.touch_model_settings(invalidate=False)

    def describe(self) -> str:
        return "cached model settings change"


def test_dry_run_sees_cached_settings_changes(geo_file):
    with pytest.raises(ProjectError, match="did not declare"):
        E.EditSet([_CachedSneak()]).preview(str(geo_file))


def test_studio_hatch_rewrites_a_relative_assemble_list(tmp_path):
    root = tmp_path / "tree"
    (root / "parts").mkdir(parents=True)
    (root / "parts" / "a.stl").write_bytes(b"solid x\nendsolid x\n")
    (root / "list.json").write_text(
        json.dumps({"plates": [{"plate_name": "p", "need_arrange": False, "objects": [{"path": "parts/a.stl"}]}]})
    )
    run_dir = tmp_path / "run"
    args = CliCommand.parse(["--load-assemble-list", "list.json", "--slice", "1"], root, scratch=run_dir).args()
    copy_path = Path(args[args.index("--load-assemble-list") + 1])
    assert copy_path.parent == run_dir.resolve()
    assert json.loads(copy_path.read_text())["plates"][0]["objects"][0]["path"] == str((root / "parts" / "a.stl").resolve())
    assert json.loads((root / "list.json").read_text())["plates"][0]["objects"][0]["path"] == "parts/a.stl"  # the user's file stays


# ---------------------------------------------------------------------------- edits on silhouettes (integration)
def _l_body(tmp_path, path):
    """The body becomes an L (x 0..30 y 0..8, plus x 0..10 y 8..20) inside the same 30 x 20 box: a notch x 10..30,
    y 8..20, as the printed layout's camera shell sat in the housing's notch."""
    from conftest import box_facets
    from shambu import mesh as M

    stl = M.write_stl(tmp_path / "l.stl", box_facets((0, 0, 0), (30, 8, 10)) + box_facets((0, 8, 0), (10, 20, 10)))
    replace(path, stl)
    return path


def test_move_into_a_notch_passes_on_silhouettes_and_fails_on_boxes(tmp_path):
    path = _l_body(tmp_path, make_geo_3mf(tmp_path / "l.3mf"))
    notch = (GEO_BODY_AT[0] + 14, GEO_BODY_AT[1] + 10)  # 2 mm above the L's foot, 4 mm right of its upright
    out = tmp_path / "moved.3mf"
    es = E.EditSet([E.MoveObject("label", *notch, strict=True)])  # strict: any FAIL line raises
    es.apply(str(path), str(out))
    assert any(n.startswith("  OK: label: clearance on outlines to 1 other object(s)") and "no FAIL" in n for n in es.notes)
    p = Project(str(out))
    obj = E.find_object(p, "label")
    lo, hi = E._Geometry(p).world_box(obj)

    def boxes(a, b):
        return L.pair_check(dataclasses.replace(a, footprint=None), dataclasses.replace(b, footprint=None))

    fails = [c for c in E.layout_checks(p, obj, lo, hi, 1, E._Geometry(p), clear=boxes) if not c.ok]
    assert [c.kind for c in fails] == ["body"] and "intersect" in fails[0].text


def test_tower_checks_the_other_objects_outlines(tmp_path):
    path = make_geo_3mf(tmp_path / "t.3mf")
    with pytest.raises(E.EditError, match="body"):
        E.EditSet([E.SetTower(GEO_BODY_AT[0], GEO_BODY_AT[1])]).apply(str(path), str(tmp_path / "o.3mf"))
    es = E.EditSet([E.SetTower(150, 150)])
    es.apply(str(path), str(tmp_path / "o.3mf"))
    assert any("clear of every object" in n for n in es.notes)


# ---------------------------------------------------------------------------- one filament classification for check and patch
def _body_meta(path: Path, *pairs: tuple[str, str]) -> Path:
    extra = "".join(f'\n    <metadata key="{k}" value="{v}"/>' for k, v in pairs)
    rewrite(
        path,
        "Metadata/model_settings.config",
        lambda t: t.replace('<metadata key="brim_type" value="no_brim"/>', '<metadata key="brim_type" value="no_brim"/>' + extra, 1),
    )
    return path


def _reach_fails(es: E.EditSet) -> list[str]:
    return [n for n in es.notes if n.startswith("  FAIL") and "extruder 2" in n]


def test_move_counts_a_height_range_wall_filament(tmp_path):
    path = make_geo_3mf(tmp_path / "r.3mf", ranges=[(0.0, 5.0)])
    rewrite(
        path,
        "Metadata/layer_config_ranges.xml",
        lambda t: t.replace(
            '<option opt_key="layer_height">0.2</option>',
            '<option opt_key="layer_height">0.2</option>\n   <option opt_key="wall_filament">2</option>',
        ),
    )
    es = E.EditSet([E.MoveObject("body", 5, 50)])
    es.apply(str(path), str(tmp_path / "o.3mf"))
    assert _reach_fails(es), es.notes  # the right nozzle prints the range's walls inside its dead strip


def test_move_ignores_a_support_slot_when_support_is_off(tmp_path):
    path = _body_meta(make_geo_3mf(tmp_path / "s.3mf"), ("enable_support", "0"), ("support_interface_filament", "2"))
    es = E.EditSet([E.MoveObject("body", 5, 50)])
    es.apply(str(path), str(tmp_path / "o.3mf"))
    assert not _reach_fails(es), es.notes


def test_move_holds_an_interface_only_slot_to_the_interface_spread(tmp_path):
    path = _body_meta(make_geo_3mf(tmp_path / "i.3mf"), ("enable_support", "1"), ("support_interface_filament", "2"))
    es = E.EditSet([E.MoveObject("body", 24, 50)])  # interface growth 0 brim + 3 mm: x 21 is in the right nozzle's reach
    es.apply(str(path), str(tmp_path / "o.3mf"))
    assert not _reach_fails(es), es.notes
    es = E.EditSet([E.MoveObject("body", 22, 50)])  # x 19: past it
    es.apply(str(path), str(tmp_path / "o2.3mf"))
    assert any("support interface 3.0 mm" in n for n in _reach_fails(es)), es.notes


def test_tower_edit_uses_the_tower_model_check_plans_with(tmp_path):
    """A tower build planned (width 20, no rib wall, brim 2, tallest part 10 mm) at x 230 y 180: tower.footprint gives
    x 228..252 y 178..215.75; the old edits model (a 35 mm square plus 8 mm) put it at 222..273 and refused it."""
    from shambu import tower as T

    path = make_geo_3mf(tmp_path / "t.3mf", prime_tower_width="20", prime_tower_rib_wall="0", prime_tower_brim_width="2")
    es = E.EditSet([E.SetTower(230, 180)])
    es.apply(str(path), str(tmp_path / "o.3mf"))
    want = T.footprint(T.TowerSettings(width=20.0, rib_wall=False, brim=2.0), 10.0, 230, 180).rect
    assert want == pytest.approx((228.0, 178.0, 252.0, 215.75), abs=0.01)
    assert any("x 228.0..252.0 y 178.0..215.8" in n for n in es.notes), es.notes


def test_tower_edit_on_one_filament_with_smooth_timelapse(tmp_path):
    path = make_geo_3mf(tmp_path / "s.3mf", filaments=1, timelapse_type="1")
    E.EditSet([E.SetTower(90, 100)]).apply(str(path), str(tmp_path / "o.3mf"))
    assert Project(str(tmp_path / "o.3mf")).project_settings["wipe_tower_x"] == ["90"]


def test_add_filament_repairs_an_unsliced_cli_export_first(tmp_path):
    """An unsliced CLI export (`extruder_nozzle_stats []`): the edit repairs the stats in the same transaction, before
    the new slot, so the result is re-sliceable and slot 2 keeps its variant block."""
    from shambu import runs

    path = make_geo_3mf(tmp_path / "u.3mf", filaments=1, extruder_nozzle_stats=[], extruder_max_nozzle_count=["1", "1"])
    assert Project(str(path)).reslice_problems()
    es = E.EditSet([add_patch(path)])
    runs.apply_edits(None, str(path), es, str(tmp_path / "two.3mf"))
    p = Project(str(tmp_path / "two.3mf"))
    assert p.reslice_problems() == [] and p.n_filaments() == 2
    ps = p.project_settings
    assert len(ps["filament_extruder_variant"]) == len(ps["filament_self_index"]) and "2" in ps["filament_self_index"]


def test_an_unreadable_pass_log_is_a_warning_not_silence(tmp_path):
    from shambu.build import checked_outcome

    rr = RunResult(["BambuStudio"], 101 % 256, -101, 0.1, tmp_path / "gone" / "x.log", {"return_code": -101}, None)
    rec = checked_outcome(rr, 1)
    assert rec["kind"] == "skippable" and "could not be read" in rec["warning"] and str(tmp_path / "gone" / "x.log") in rec["reason"]


def test_add_filament_reports_a_project_tower_that_sits_on_an_object(tmp_path):
    """The verify slice passes while slot 2 is unused; the first slice that uses it prints the tower at the project's
    spot. On the body (x 40..70 y 50..70) that is a FAIL line naming it and `--tower`; the tower is not moved."""
    path = make_geo_3mf(tmp_path / "on.3mf", filaments=1, wipe_tower_x=["35"], wipe_tower_y=["45"])
    es = E.EditSet([add_patch(path)])
    es.apply(str(path), str(tmp_path / "two.3mf"))
    fails = [n for n in es.notes if n.startswith("  FAIL: the project's prime tower")]
    assert fails and "body" in fails[0] and "--tower X,Y" in fails[0]
    assert Project(str(tmp_path / "two.3mf")).project_settings["wipe_tower_x"] == ["35"]
    clear = make_geo_3mf(tmp_path / "off.3mf", filaments=1, wipe_tower_x=["90"], wipe_tower_y=["100"])
    es = E.EditSet([add_patch(clear)])
    es.apply(str(clear), str(tmp_path / "two2.3mf"))
    assert any(n.startswith("  OK: plate 1: the tower is clear of every object") for n in es.notes)


def _body_part_meta(path: Path, key: str, value: str, subtype: str = "normal_part") -> Path:
    """A per-volume setting on the body's one sub-part (and, for a modifier, its subtype)."""

    def fix(t: str) -> str:
        i = t.index('<metadata key="name" value="body.stl"') if '<metadata key="name" value="body.stl"' in t else t.index('<part id="1"')
        j = t.index("</part>", i)
        block = t[i:j].replace('subtype="normal_part"', f'subtype="{subtype}"', 1)
        return t[:i] + block + f'  <metadata key="{key}" value="{value}"/>\n    ' + t[j:]

    rewrite(path, "Metadata/model_settings.config", fix)
    return path


@pytest.mark.parametrize("subtype", ["normal_part", "modifier_part"])
def test_strict_move_counts_a_sub_parts_wall_filament(tmp_path, subtype):
    """Studio resolves wall and infill filaments per volume (PrintObject.cpp:3105): a body sub-part (or a modifier) with
    wall_filament 2 at x 5 is right-nozzle work in its dead strip."""
    path = _body_part_meta(make_geo_3mf(tmp_path / "v.3mf"), "wall_filament", "2", subtype)
    with pytest.raises(E.EditError, match="extruder 2"):
        E.EditSet([E.MoveObject("body", 5, 50, strict=True)]).apply(str(path), str(tmp_path / "o.3mf"))


def _range_option(path: Path, key: str, value: str) -> Path:
    rewrite(
        path,
        "Metadata/layer_config_ranges.xml",
        lambda t: t.replace(
            '<option opt_key="layer_height">0.2</option>',
            f'<option opt_key="layer_height">0.2</option>\n   <option opt_key="{key}">{value}</option>',
        ),
    )
    return path


def test_a_range_repeating_the_interface_slot_keeps_it_interface_only(tmp_path):
    path = _body_meta(make_geo_3mf(tmp_path / "r.3mf", ranges=[(0.0, 5.0)]), ("enable_support", "1"), ("support_interface_filament", "2"))
    _range_option(path, "support_interface_filament", "2")
    es = E.EditSet([E.MoveObject("body", 24, 50)])  # 3 mm interface growth: x 21 is in reach
    es.apply(str(path), str(tmp_path / "o.3mf"))
    assert not _reach_fails(es), es.notes


def test_a_stale_range_interface_slot_with_support_off_does_not_constrain(tmp_path):
    path = _body_meta(make_geo_3mf(tmp_path / "s.3mf", ranges=[(0.0, 5.0)]), ("enable_support", "0"))
    _range_option(path, "support_interface_filament", "2")
    es = E.EditSet([E.MoveObject("body", 5, 50)])
    es.apply(str(path), str(tmp_path / "o.3mf"))
    assert not _reach_fails(es), es.notes
