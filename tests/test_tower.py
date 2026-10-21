"""tower.py and the two-filament fixes: process-level support filaments, the prime-tower footprint, its placement and
rules, the per-plate wipe_tower vectors, and a CLI re-slice of an unsliced export (integration, gated). Offline except
the last test. Numbers come from the plate-14 and seed runs of 2026-10-21 (reference 06, "Prime tower")."""

from __future__ import annotations

import json
import random

import pytest

from conftest import MACHINE, PROCESS, PROCESS_NAME, FakeStudio, make_3mf, requires_studio
from shambu import layout as L
from shambu import tower as T
from shambu.build import Pipeline, effective_filaments
from shambu.spec import PartSpec, Spec

X2D_EXTRUDERS = [
    {"index": 1, "x": (0.0, 256.0), "y": (0.0, 256.0), "z_max": 320.0, "type": "Direct Drive", "nozzle": "0.4"},
    {"index": 2, "x": (20.5, 256.0), "y": (0.0, 256.0), "z_max": 325.0, "type": "Bowden", "nozzle": "0.4"},
]
FACTS = {"bed": {"x": (0.0, 256.0), "y": (0.0, 256.0)}, "extruders": X2D_EXTRUDERS}
BED: T.Rect = (0.0, 0.0, 256.0, 256.0)
REACH = T.shared_reach(X2D_EXTRUDERS, FACTS["bed"])
# The plate-14 part boxes (bed coordinates of the user's layout, B-runs bbox.py), the housing 218 mm tall
PLATE14 = [
    ("housing_print", (44.3, 25.9, 168.5, 160.9)),
    ("camera_mount_print", (32.7, 181.0, 73.3, 239.8)),
    ("camera_housing_v2_print", (105.7, 161.8, 164.3, 222.5)),
    ("feeler_module_print", (195.1, 16.0, 245.1, 242.4)),
    ("motor_cradle_print", (176.7, 46.6, 198.8, 145.6)),
]
H14 = 218.0
# Two X2D filaments with Studio's prime volume 45 and filament_change_length 10 (the plate-14 presets)
PLATE14_PRESETS = [{"filament_prime_volume": ["45"], "filament_change_length": ["10"], "filament_diameter": ["1.75"]}] * 2
DEFAULT = T.TowerSettings.from_config({"layer_height": "0.2"}, PLATE14_PRESETS)


# ---------------------------------------------------------------------------- 1. process-level support filaments
def test_process_interface_filament_counts_for_a_supported_part(tmp_path):
    process = {"support_interface_filament": "2", "support_filament": "0", "enable_support": "1"}
    supported = PartSpec.from_json({"path": str(tmp_path / "a.stl")})
    assert effective_filaments(supported, process=process) == {1, 2}
    bare = PartSpec.from_json({"path": str(tmp_path / "a.stl"), "set": {"enable_support": "0"}})
    assert effective_filaments(bare, process=process) == {1}
    assert effective_filaments(supported) == {1}  # without the process: the old reading (part set only)


def test_process_wall_filament_counts_for_every_part(tmp_path):
    part = PartSpec.from_json({"path": str(tmp_path / "a.stl")})
    assert effective_filaments(part, process={"wall_filament": "2", "enable_support": "0"}) == {1, 2}
    pinned = PartSpec.from_json({"path": str(tmp_path / "a.stl"), "set": {"wall_filament": "0"}})
    assert effective_filaments(pinned, process={"wall_filament": "2"}) == {1}  # the part's own value wins


# ---------------------------------------------------------------------------- 2. the seed spec fails before Studio runs
def seed_spec(tmp_path, stls, cradle_at=(9.2, 40.0), mount_at=(60.0, 40.0)) -> Spec:
    return Spec.from_json(
        {
            "name": "seed-dual",
            "outdir": str(tmp_path / "out"),
            "printer": MACHINE,
            "process": {"base": PROCESS, "name": PROCESS_NAME, "recipes": ["dual-material-interface"]},
            "filaments": [{"preset": "Test PLA", "colour": "#161616"}, {"preset": "Test PLA", "colour": "#FCECD6"}],
            "plates": [
                {
                    "name": "seed",
                    "parts": [
                        {
                            "path": str(stls["body"]),
                            "name": "camera_mount_print",
                            "recipes": ["tree-supports"],
                            **({"at": list(mount_at)} if mount_at else {}),
                        },
                        {
                            "path": str(stls["base"]),
                            "name": "motor_cradle_print",
                            "recipes": ["tree-supports", "tall-outer-brim"],
                            **({"at": list(cradle_at)} if cradle_at else {}),
                        },
                    ],
                }
            ],
        }
    )


def test_seed_spec_fails_the_right_nozzle_reach_without_a_studio_run(tmp_path, stls, resolver):
    studio = FakeStudio(sizes={"body.stl": (40.0, 58.0, 30.0), "base.stl": (22.1, 99.0, 25.0)})
    plan = Pipeline(studio, resolver).plan(seed_spec(tmp_path, stls))
    fails = [c.text for c in plan.plates[0].checks if not c.ok]
    assert any(
        t.startswith("motor_cradle_print has support with the process support_interface_filament 2 (extruder 2, Bowden)")
        and "extruder 2 reaches x 20.5..256.0" in t
        for t in fails
    ), fails
    assert not plan.ok
    assert not [c for c in studio.calls if c[0] == "run"]  # planning never runs the export


def test_seed_spec_moved_right_passes_and_gets_a_tower(tmp_path, stls, resolver):
    studio = FakeStudio(sizes={"body.stl": (40.0, 58.0, 30.0), "base.stl": (22.1, 99.0, 25.0)})
    plan = Pipeline(studio, resolver).plan(seed_spec(tmp_path, stls, cradle_at=(45.0, 40.0), mount_at=(110.0, 40.0)))
    assert plan.ok, (plan.problems, [c.text for c in plan.plates[0].checks if not c.ok])
    tower = plan.plates[0].tower
    assert tower is not None and tower.footprint is not None and tower.mode == "auto"
    assert tower.footprint.rect[0] >= 20.5


def test_packed_seed_starts_far_enough_right_for_the_interface(tmp_path, stls, resolver):
    """The packer starts a supported part's rect at x 20.5 plus the interface margin beyond the part margin."""
    studio = FakeStudio(sizes={"body.stl": (40.0, 58.0, 30.0), "base.stl": (22.1, 99.0, 25.0)})
    plan = Pipeline(studio, resolver).plan(seed_spec(tmp_path, stls, cradle_at=None, mount_at=None))
    assert plan.ok, (plan.problems, [c.text for c in plan.plates[0].checks if not c.ok])
    for x in plan.plates[0].parts:
        assert x.placed.x0 - (L.brim_margin({**plan.process, **x.spec.set}) + T.INTERFACE_OVERSHOOT) >= 20.5 - 1e-9


# ---------------------------------------------------------------------------- 3. the footprint estimate
def test_footprint_reproduces_studios_log_at_218_mm():
    assert T.min_depth(H14) == pytest.approx(35.733, abs=1e-3)
    assert DEFAULT.size(H14) == pytest.approx((41.39, 41.39), abs=0.01)  # log: width 41.3902, depth 41.3902
    assert DEFAULT.brim_at(H14) == 8.0
    narrow = T.TowerSettings.from_config({"prime_tower_width": "22", "prime_tower_rib_wall": "0"}, PLATE14_PRESETS)
    assert narrow.size(H14) == pytest.approx((22.0, 38.88), abs=0.01)
    fp = T.footprint(DEFAULT, H14, 208, 208)
    assert fp.rect == pytest.approx((200.0, 200.0, 257.39, 257.39), abs=0.01)  # log: wipe bbox {200, 200} - {257.39, 257.39}


@pytest.mark.parametrize(("h", "depth", "brim"), [(3, 5, 0.24), (5, 5, 0.4), (50, 12.105, 4.0), (100, 20, 8), (300, 50, 8), (400, 60, 8)])
def test_min_depth_and_auto_brim_by_height(h, depth, brim):
    assert T.min_depth(h) == pytest.approx(depth, abs=1e-3)
    assert T.auto_brim(h) == pytest.approx(brim)


# ---------------------------------------------------------------------------- 4. TowerClear on the plate-14 boxes
def test_tower_clear_fails_the_fixed_corner_and_names_the_feeler():
    fp = T.footprint(DEFAULT, H14, 208, 208)
    line = T.check_fixed(1, fp, PLATE14, REACH, BED)
    assert line is not None
    assert line.startswith("plate 1 prime tower at (208.0, 208.0) is 57.4 x 57.4 mm with its 8 mm brim (rib wall, tallest part 218 mm)")
    assert "it overlaps feeler_module_print (x 195.1..245.1 y 16.0..242.4)" in line
    assert "leaves the bed at x 257.4" in line and "y 257.4" in line
    assert "none at 41.4 mm" in line and line.endswith("Set plates[0].wipe_tower.")
    (check,) = T.TowerClear(T.TowerPlan(1, "fixed", fp, line)).check([], None)
    assert not check.ok and check.text == line


def test_tower_clear_passes_a_free_spot():
    fp = T.footprint(DEFAULT, 20.0, 150, 150)
    assert T.check_fixed(1, fp, [("a", (10, 10, 50, 50))], REACH, BED) is None
    (check,) = T.TowerClear(T.TowerPlan(1, "fixed", fp)).check([], None)
    assert check.ok and "clear of every part" in check.text


# ---------------------------------------------------------------------------- 5. auto placement on the plate-14 boxes
def test_auto_placement_on_plate_14_drops_the_rib_wall():
    assert T.find_spot(DEFAULT, H14, PLATE14, REACH)[0] is None  # no free 41.4 mm rib-wall square (57.4 with brim)
    p = T.auto_place(DEFAULT, H14, PLATE14, REACH)
    assert p.tried[0] == "41.4 mm (rib wall)"
    fp = p.footprint
    assert fp is not None and not fp.settings.rib_wall and fp.settings.brim == 2.0
    r = fp.rect
    assert T.inside(r, REACH)
    assert all(T._gap(r, o) >= T.PART_CLEARANCE - 1e-9 and not T._overlaps(r, o) for _, o in PLATE14)


def test_plate_14_t8_strip_is_narrower_than_a_22_mm_tower_with_3_mm_clearance():
    """T8 sliced a 22 mm tower without rib wall at (172, 150) in the strip between the housing (x <= 168.5) and the
    feeler (x >= 195.1). By bounding boxes the strip is 26.6 mm wide and the footprint 26 mm, so the box model with
    3 mm clearance rejects it: the real feeler is narrower than its box there (the T8 G-code tower reached x 195.8)."""
    t8 = T.TowerSettings.from_config(
        {"prime_tower_width": "22", "prime_tower_rib_wall": "0", "prime_tower_brim_width": "2"}, PLATE14_PRESETS
    )
    fp = T.footprint(t8, H14, 172, 150)
    assert fp.rect == pytest.approx((170.0, 148.0, 196.0, 190.88), abs=0.01)
    assert fp.outer[0] + 2 * T.PART_CLEARANCE > 195.1 - 168.5
    assert T.find_spot(t8, H14, PLATE14, (168.5, 145.6, 195.1, 256.0), clearance=0.0)[0] is not None  # it fits at 0 mm


def test_auto_placement_failure_names_the_blocking_part():
    wall = [("wall", (20.5, 0.0, 256.0, 256.0))]
    plan = T.plan_auto(3, DEFAULT, H14, wall, REACH)
    assert plan.footprint is None and plan.problem is not None
    assert plan.problem.startswith("plate 3: no free prime tower spot both nozzles reach")
    assert "is blocked by wall" in plan.problem and '"wipe_tower": false' in plan.problem
    (check,) = T.TowerClear(plan).check([], None)
    assert not check.ok


def test_auto_placement_prefers_the_back_right_on_an_empty_plate():
    fp = T.auto_place(DEFAULT, 30.0, [], REACH).footprint
    assert fp is not None and fp.settings == DEFAULT
    assert fp.rect[2] == pytest.approx(256.0, abs=1.0) and fp.rect[3] == pytest.approx(256.0, abs=1.0)


# ---------------------------------------------------------------------------- 6. never in the right nozzle's dead strip
def test_auto_placement_never_leaves_the_shared_reach():
    rng = random.Random(7)  # noqa: S311 (a fixed test layout, not a secret)
    for _ in range(25):
        obs = []
        for i in range(rng.randint(0, 6)):
            x, y = rng.uniform(0, 220), rng.uniform(0, 220)
            obs.append((f"p{i}", (x, y, x + rng.uniform(10, 80), y + rng.uniform(10, 80))))
        p = T.auto_place(DEFAULT, rng.uniform(5, 250), obs, REACH)
        if p.footprint is not None:
            r = p.footprint.rect
            assert r[0] >= 20.5 - 1e-9 and r[2] <= 256 + 1e-9 and r[1] >= -1e-9 and r[3] <= 256 + 1e-9
            assert p.footprint.x - p.footprint.brim >= 20.5 - 1e-9


def test_shared_reach_takes_the_aux_x_limit():
    assert T.shared_reach(X2D_EXTRUDERS, FACTS["bed"], 235.5) == (20.5, 0.0, 235.5, 256.0)


# ---------------------------------------------------------------------------- 8. per-plate wipe_tower vectors in command()
def two_plates(stls, tower1, tower2):
    return [
        {
            "name": "p",
            "wipe_tower": tower1,
            "parts": [
                {"path": str(stls["body"]), "name": "body_1", "at": [30, 30]},
                {"path": str(stls["text"]), "name": "text_1", "filament": 2, "at": [80, 30]},
            ],
        },
        {
            "name": "q",
            "wipe_tower": tower2,
            "parts": [
                {"path": str(stls["base"]), "name": "base_1", "at": [30, 30]},
                {"path": str(stls["text"]), "name": "text_1", "filament": 2, "at": [80, 30]},
            ],
        },
    ]


def spec_of(tmp_path, plates, **process_set) -> Spec:
    return Spec.from_json(
        {
            "name": "towers",
            "outdir": str(tmp_path / "out"),
            "printer": MACHINE,
            "process": {"base": PROCESS, "name": PROCESS_NAME, "set": {"wall_loops": "2", **process_set}},
            "filaments": [{"preset": "Test PLA", "colour": "#000000"}, {"preset": "Test PLA", "colour": "#FFFFFF"}],
            "plates": plates,
        }
    )


def test_command_writes_the_spec_tower_spot_per_plate(tmp_path, stls, resolver):
    t1 = {"x": 172, "y": 150, "width": 22, "rib_wall": False, "brim": 2}
    t2 = {"x": 120, "y": 180, "width": 22, "rib_wall": False, "brim": 2}
    fixture = make_3mf(tmp_path / "two.3mf", change_filament_gcode="M600")
    studio = FakeStudio(fixture)
    r = Pipeline(studio, resolver).build(spec_of(tmp_path, two_plates(stls, t1, t2)), keep_going=True)
    args = next(a for kind, a in studio.calls if kind == "run")
    assert "--wipe-tower-x=172.0,120.0" in args and "--wipe-tower-y=150.0,180.0" in args
    assert "--wipe-tower-x=208.0,208.0" not in args
    assert (r.plan.process["prime_tower_width"], r.plan.process["prime_tower_rib_wall"], r.plan.process["prime_tower_brim_width"]) == (
        "22",
        "0",
        "2",
    )
    written = json.loads((tmp_path / "out" / "shambu" / "process.json").read_text())
    assert written["prime_tower_width"] == "22" and written["prime_tower_rib_wall"] == "0"


def test_two_plates_with_different_tower_sizes_is_a_build_error(tmp_path, stls):
    from shambu.errors import BuildError

    with pytest.raises(BuildError, match="prime tower size is per project"):
        spec_of(tmp_path, two_plates(stls, {"x": 172, "y": 150, "width": 22}, {"x": 120, "y": 180, "width": 30}))


def test_wipe_tower_false_writes_no_tower_and_warns(tmp_path, stls, resolver):
    plan = Pipeline(FakeStudio(), resolver).plan(spec_of(tmp_path, two_plates(stls, False, False)))
    assert plan.process["enable_prime_tower"] == "0"
    assert any(w.startswith("plate 1 wipe_tower false: two filaments with no prime tower") for w in plan.warnings)
    assert all(p.tower is not None and p.tower.mode == "off" for p in plan.plates)


def test_tower_on_one_plate_and_off_on_another_is_a_problem(tmp_path, stls, resolver):
    plan = Pipeline(FakeStudio(), resolver).plan(spec_of(tmp_path, two_plates(stls, "auto", False)))
    assert any("prime tower on/off is per project" in p for p in plan.problems)


def test_fixed_tower_outside_the_shared_reach_is_a_problem_naming_the_plate(tmp_path, stls, resolver):
    plan = Pipeline(FakeStudio(), resolver).plan(
        spec_of(tmp_path, two_plates(stls, {"x": 10, "y": 150, "width": 22}, {"x": 120, "y": 150, "width": 22}))
    )
    assert any(p.startswith("plate 1 wipe_tower at (10, 150)") and "x 20.5..256" in p for p in plan.problems), plan.problems


def test_auto_tower_avoids_packed_parts(tmp_path, stls, resolver):
    plates = [{"name": "p", "parts": [{"path": str(stls["body"]), "count": 6}, {"path": str(stls["text"]), "filament": 2, "count": 3}]}]
    plan = Pipeline(FakeStudio(sizes={"body.stl": (60, 60, 40), "text.stl": (30, 30, 5)}), resolver).plan(spec_of(tmp_path, plates))
    assert plan.ok, (plan.problems, [c.text for c in plan.plates[0].checks if not c.ok])
    fp = plan.plates[0].tower.footprint
    assert fp is not None
    for x in plan.plates[0].parts:
        assert not L.overlaps(fp.rect, x.placed.rect())


# ---------------------------------------------------------------------------- 10. verify FAILs a file Studio cannot re-slice
class NoPatch(Pipeline):
    def patch(self, plan, project, sliced=False):
        return []


def test_verify_fails_an_export_with_empty_nozzle_stats(tmp_path, stls, resolver):
    fixture = make_3mf(tmp_path / "bad.3mf", change_filament_gcode="M600", extruder_nozzle_stats=[])
    r = NoPatch(FakeStudio(fixture), resolver).build(spec_of(tmp_path, two_plates(stls, False, False)), keep_going=True)
    assert any(p.startswith("not re-sliceable by the CLI: extruder_nozzle_stats []") for p in r.verify_problems or [])


def test_verify_fails_a_zero_count_and_passes_after_the_build_patch(tmp_path, stls, resolver):
    fixture = make_3mf(tmp_path / "zero.3mf", change_filament_gcode="M600", extruder_nozzle_stats=["Standard#0", "Standard#0"])
    r = NoPatch(FakeStudio(fixture), resolver).build(spec_of(tmp_path, two_plates(stls, False, False)), keep_going=True)
    assert any("Standard#0" in p for p in r.verify_problems or [])
    fixture = make_3mf(tmp_path / "zero2.3mf", change_filament_gcode="M600", extruder_nozzle_stats=["Standard#0", "Standard#0"])
    r = Pipeline(FakeStudio(fixture), resolver).build(spec_of(tmp_path, two_plates(stls, False, False)), keep_going=True)
    assert not any("re-sliceable" in p for p in r.verify_problems or [])


# ---------------------------------------------------------------------------- 12. live: CLI re-slice of an unsliced export
def _box(path, lo, hi):
    from shambu import mesh as M

    x0, y0, z0 = lo
    x1, y1, z1 = hi
    p = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    quads = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (2, 3, 7, 6), (1, 2, 6, 5), (3, 0, 4, 7)]
    facets = [t for a, b, c, d in quads for t in ((p[a], p[b], p[c]), (p[a], p[c], p[d]))]
    return M.write_stl(path, facets)


@pytest.mark.integration
@requires_studio
def test_live_reslice_of_patched_unsliced_export(tmp_path):
    from shambu import presets as P
    from shambu.studio import CliCommand, StudioCLI
    from shambu.threemf import Project

    a = _box(tmp_path / "a.stl", (0, 0, 0), (20, 20, 10))
    b = _box(tmp_path / "b.stl", (0, 0, 0), (20, 20, 10))
    spec = Spec.from_json(
        {
            "name": "two-boxes",
            "outdir": str(tmp_path / "out"),
            "printer": "Bambu Lab X2D 0.4 nozzle",
            "process": {"base": "0.20mm Standard @BBL X2D", "name": "two boxes"},
            "filaments": [
                {"preset": "Bambu PLA Basic @BBL X2D 0.4 nozzle", "colour": "#161616"},
                {"preset": "Bambu PLA Basic @BBL X2D 0.4 nozzle", "colour": "#FCECD6"},
            ],
            "plates": [{"name": "p", "parts": [{"path": str(a), "at": [60, 60]}, {"path": str(b), "filament": 2, "at": [120, 60]}]}],
        }
    )
    studio = StudioCLI(scratch=tmp_path / "scratch")
    r = Pipeline(studio, P.resolver()).build(spec)
    assert r.exported and not r.verify_problems, (r.export_problem(), r.verify_problems)
    ps = Project(str(r.project)).project_settings
    assert ps["extruder_nozzle_stats"] and len(ps["filament_extruder_variant"]) == len(ps["filament_self_index"])

    def reslice(src, name):
        cmd = CliCommand(inputs=[str(src)]).slice(0, no_check=False, max_seconds=600).export_3mf(f"{name}.gcode.3mf", tmp_path / name)
        return studio.run(cmd, name=name, outputdir=tmp_path / name, timeout=900)

    ok = reslice(r.project, "patched")
    assert ok.code == 0, ok.summary()
    bad = Project(str(r.project))
    bad.project_settings["extruder_nozzle_stats"] = []
    bad._touch_ps()
    bad.save(str(tmp_path / "unpatched.3mf"))
    fail = reslice(tmp_path / "unpatched.3mf", "unpatched")
    assert fail.code == -100 and "No valid nozzle found" in fail.summary()
