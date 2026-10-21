"""tower.py and the two-filament fixes: process-level support filaments, the prime-tower footprint, its placement and
rules, the per-plate wipe_tower vectors, and a CLI re-slice of an unsliced export (integration, gated). Offline except
the last test. Numbers come from the plate-14 and seed runs of 2026-10-21 (reference 06, "Prime tower")."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, replace

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
    fails = [c for c in plan.plates[0].checks if not c.ok]
    assert [c.kind for c in fails] == ["reach"], [c.text for c in fails]  # one line per part and extruder (ExtruderReach)
    assert fails[0].text == (
        "motor_cradle_print uses filament 2 as the process support_interface_filament (extruder 2, Bowden) and reaches "
        "x 1.0 on -x with its support interface 8.2 mm; that extruder reaches x 20.5..256.0"
    )
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
        assert x.placed.x0 - (L.brim_margin({**plan.process, **x.spec.set}) + L.INTERFACE_SPREAD) >= 20.5 - 1e-9


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
    assert all(T._gap(r, o) >= T.PART_CLEARANCE - 1e-9 and not L.overlaps(r, o) for _, o in PLATE14)


def _shape(key, rects, z=10.0, filament=1, spread=None):
    """A placed part whose outline is the union of `rects` (bed coordinates), as a silhouette footprint."""
    from conftest import mesh

    x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
    x1, y1 = max(r[2] for r in rects), max(r[3] for r in rects)
    boxes = [L.BoxFootprint.of(r[2] - r[0], r[3] - r[1]).translated(r[0], r[1]) for r in rects]
    fp = L.union_footprint(boxes, (x0, y0))
    p = L.Placed(key, mesh(f"{key}.stl", (x1 - x0, y1 - y0, z)), filament, x0, y0, footprint=fp)
    if spread is not None:
        p.spread = spread
    return p


def plate14_outlines(probe: bool) -> list[L.Placed]:
    """The plate-14 silhouettes near the T8 strip, simplified to rects (from the parts' own STLs): the housing is an
    L (x <= 95 up to y 63.4, then x <= 169 to y 105 and x <= 132 above), the feeler two end blocks on a 10 mm beam at
    x 215..225, the cradle a 22 mm body. `probe`: the measured spreads (n=1 solo slices), else the estimates."""

    def sp(*v):
        return L.Spread(*v, "probe", True) if probe else L.Spread(v[4], v[4], v[4], v[4], "estimate", True)

    return [
        _shape(
            "housing",
            [(44.3, 25.9, 95.0, 63.4), (64.0, 63.4, 168.5, 105.0), (64.0, 105.0, 132.0, 141.9)],
            218,
            spread=sp(13.8, 4.4, 1.3, 1.3, 21.8),
        ),
        _shape("camera_mount", [(32.7, 181.0, 73.3, 239.8)], 32.1, spread=sp(0.0, 4.6, 0.0, 0.0, 8.0)),
        _shape("camera_housing", [(105.7, 161.8, 164.3, 222.5)], 47.8, spread=sp(10.6, 0.0, 0.0, 8.1, 8.0)),
        _shape(
            "feeler",
            [(195.1, 16.0, 245.1, 28.5), (215.0, 28.5, 225.0, 230.0), (195.1, 230.0, 245.1, 242.4)],
            64,
            spread=sp(7.0, 7.0, 5.0, 5.0, 8.0),
        ),
        _shape(
            "cradle",
            [(176.7, 70.0, 198.8, 136.0), (182.0, 46.6, 190.0, 70.0), (182.0, 136.0, 190.0, 145.6)],
            28.9,
            spread=sp(14.9, 8.2, 2.1, 0.0, 8.0),
        ),
    ]


T8 = T.TowerSettings.from_config({"prime_tower_width": "22", "prime_tower_rib_wall": "0", "prime_tower_brim_width": "2"}, PLATE14_PRESETS)


def test_plate_14_t8_strip_takes_the_22_mm_tower_on_the_silhouettes():
    """T8 sliced a 22 mm tower without rib wall at (172, 150) in the strip between the housing (x <= 168.5) and the
    feeler (box x >= 195.1). By boxes the strip is 26.6 mm wide and the footprint 26 mm, so the box test with 3 mm
    rejects it. On the silhouettes (the feeler is a 10 mm beam there) with the measured spreads it is free, and auto
    placement inside the strip finds width 22: 35, 30 and 25 are deeper than the strip allows beside the cradle."""
    fp = T.footprint(T8, H14, 172, 150)
    assert fp.rect == pytest.approx((170.0, 148.0, 196.0, 190.88), abs=0.01)
    boxes = T.BoxClearance(tuple((p.key, p.rect()) for p in plate14_outlines(probe=True)))
    assert not boxes.clear(fp.rect)
    test = T.FootprintClearance.of(plate14_outlines(probe=True))
    assert test.clear(fp.rect) and test.estimates_within(fp.rect) == []
    strip = (168.5, 145.6, 196.0, 256.0)
    req = T.TowerRequest(1, "auto", DEFAULT, H14, tuple(PLATE14), strip, BED, clearance=test)
    (plan,) = T.plan_project([req])
    assert plan.footprint is not None and plan.footprint.settings == replace(DEFAULT, width=22.0, rib_wall=False, brim=2.0)
    assert T.inside(plan.footprint.rect, strip) and plan.warning is None


def test_estimated_spreads_block_strictly_then_warn():
    """With the estimates the T8 spot is inside the cradle's and the camera housing's 8 mm: the strict test blocks
    it, the relaxed test (outlines only) takes it, and the plan says so in a WARN line."""
    test = T.FootprintClearance.of(plate14_outlines(probe=False))
    r = T.footprint(T8, H14, 172, 150).rect
    assert not test.clear(r) and test.relaxed().clear(r)
    assert test.estimates_within(r) == ["camera_housing", "cradle"]
    req = T.TowerRequest(1, "fixed", T8, H14, (), REACH, BED, 172, 150, clearance=test)
    (plan,) = T.plan_project([req])
    assert plan.problem is None and plan.warning is not None and "cradle" in plan.warning  # a fixed spot: WARN too
    req = T.TowerRequest(1, "fixed", T8, H14, (), REACH, BED, 180, 120, clearance=test)
    (plan,) = T.plan_project([req])
    assert plan.problem is not None and "it overlaps cradle" in plan.problem and plan.warning is None
    req = T.TowerRequest(1, "auto", T8, H14, (), (168.5, 145.6, 196.0, 256.0), BED, clearance=test)
    (plan,) = T.plan_project([req], fallback=False)
    assert plan.footprint is not None and plan.warning is not None and "estimated support spread of" in plan.warning
    checks = list(T.TowerClear(plan).check([], None))
    assert [c.ok for c in checks] == [True, True] and checks[1].warn


def test_silhouettes_open_the_front_of_plate_14():
    """The housing's box covers x 44..169 from y 25.9, its outline only x 44..95 below y 63.4: the front then holds a
    35 mm tower clear of every estimated spread, so auto takes it before any narrower size."""
    (plan,) = T.plan_project(
        [T.TowerRequest(1, "auto", DEFAULT, H14, (), REACH, BED, clearance=T.FootprintClearance.of(plate14_outlines(False)))]
    )
    fp = plan.footprint
    assert fp is not None and fp.settings.width == 35.0 and fp.rect[3] < 46.6 and plan.warning is None


def test_footprint_clearance_keeps_the_box_test_for_a_part_without_an_outline():
    from conftest import mesh

    boxed = L.Placed("boxed", mesh("boxed.stl", (20, 20, 10)), 1, 100.0, 100.0, brim=5.0)  # footprint None
    test = T.FootprintClearance.of([boxed])
    assert not test.clear((122.0, 100.0, 140.0, 120.0))  # 2 mm from the brim rect: inside PART_CLEARANCE
    assert test.clear((128.5, 100.0, 140.0, 120.0)) and test.blocking((122.0, 100.0, 140.0, 120.0))[0][0] == "boxed"
    outlined = _shape("outlined", [(100.0, 100.0, 120.0, 120.0)])
    assert T.FootprintClearance.of([outlined]).clear((121.5, 100.0, 140.0, 120.0))  # the line allowance, no brim


def test_auto_placement_failure_names_the_blocking_part():
    wall = (("wall", (20.5, 0.0, 256.0, 256.0)),)
    (plan,) = T.plan_project([T.TowerRequest(3, "auto", DEFAULT, H14, wall, REACH, BED)])
    assert plan.footprint is None and plan.problem is not None
    assert plan.problem.startswith(f"plate 3: no free prime tower spot {T.BOTH} reach")
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


def test_two_plates_with_different_tower_sizes_is_a_plan_problem(tmp_path, stls, resolver):
    sp = spec_of(tmp_path, two_plates(stls, {"x": 172, "y": 150, "width": 22}, {"x": 120, "y": 180, "width": 30}))
    plan = Pipeline(FakeStudio(), resolver).plan(sp)
    assert any(p.startswith("prime tower size is per project: the plates need different towers") for p in plan.problems)


def test_same_effective_size_explicit_and_inherited_is_accepted(tmp_path, stls, resolver):
    """One plate spells out the process values, the other gives only x/y: the same tower after resolution."""
    explicit = {"x": 172, "y": 150, "width": 22, "rib_wall": False, "brim": 2}
    inherited = {"x": 120, "y": 180}
    sp = spec_of(
        tmp_path, two_plates(stls, explicit, inherited), prime_tower_width="22", prime_tower_rib_wall="0", prime_tower_brim_width="2"
    )
    plan = Pipeline(FakeStudio(), resolver).plan(sp)
    assert not any("per project" in p for p in plan.problems), plan.problems
    t1, t2 = (p.tower for p in plan.plates)
    assert t1 is not None and t2 is not None and t1.footprint and t2.footprint
    assert t1.footprint.settings == t2.footprint.settings


def test_wipe_tower_false_writes_no_tower_and_warns(tmp_path, stls, resolver):
    plan = Pipeline(FakeStudio(), resolver).plan(spec_of(tmp_path, two_plates(stls, False, False)))
    assert plan.process["enable_prime_tower"] == "0"
    assert any(w.startswith("plate 1 wipe_tower false: two filaments with no prime tower") for w in plan.warnings)
    assert all(p.tower is not None and p.tower.mode == "off" for p in plan.plates)


def test_tower_on_one_plate_and_off_on_another_is_a_problem(tmp_path, stls, resolver):
    plan = Pipeline(FakeStudio(), resolver).plan(spec_of(tmp_path, two_plates(stls, "auto", False)))
    assert any("prime tower on/off is per project" in p for p in plan.problems)


def test_fixed_tower_outside_the_shared_reach_fails_once_naming_the_plate(tmp_path, stls, resolver):
    plan = Pipeline(FakeStudio(), resolver).plan(
        spec_of(tmp_path, two_plates(stls, {"x": 10, "y": 150, "width": 22}, {"x": 120, "y": 150, "width": 22}))
    )
    fails = [c.text for c in plan.plates[0].checks if not c.ok]
    tower = [t for t in fails if t.startswith("plate 1 prime tower at (10.0, 150.0)")]
    assert len(tower) == 1 and "leaves the area extruders 1 and 2 (left and right nozzle) reach" in tower[0], fails
    assert not any("wipe_tower at" in p for p in plan.problems)  # reported once, as the FAIL


def test_fixed_tower_overlapping_a_part_fails_at_pipeline_level(tmp_path, stls, resolver):
    plan = Pipeline(FakeStudio(), resolver).plan(
        spec_of(tmp_path, two_plates(stls, {"x": 25, "y": 25, "width": 22}, {"x": 120, "y": 150, "width": 22}))
    )
    fails = [c.text for c in plan.plates[0].checks if not c.ok]
    assert any(t.startswith("plate 1 prime tower at (25.0, 25.0)") and "it overlaps body_1" in t for t in fails), fails
    assert not plan.ok


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


# ---------------------------------------------------------------------------- one size per project, depth per width
def test_every_fallback_width_recomputes_the_depth():
    depths = [s.size(H14)[1] for s in T.candidates(DEFAULT)[1:]]  # widths 35, 30, 25, 22, 20, no rib wall
    assert depths == sorted(depths)  # narrower is deeper: volume / (layer_height x width) x spacing
    assert depths[-1] > depths[-2] > T.min_depth(H14)


def _req(plate, obstacles, height=H14):
    return T.TowerRequest(plate, "auto", DEFAULT, height, tuple(obstacles), REACH, BED)


def test_plan_project_picks_one_size_every_plate_accepts():
    roomy = _req(1, [("a", (20.5, 0.0, 120.0, 120.0))])  # plate 1 takes the 41.4 mm rib-wall tower
    tight = _req(2, PLATE14)  # plate 2 needs the rib wall off
    p1, p2 = T.plan_project([roomy, tight])
    assert p1.footprint is not None and p2.footprint is not None
    assert p1.footprint.settings == p2.footprint.settings  # one configuration for the project
    assert not p1.footprint.settings.rib_wall
    assert T.find_spot(DEFAULT, H14, roomy.obstacles, REACH)[0] is not None  # alone, plate 1 would keep the rib wall


def test_plan_project_fails_the_plate_that_blocks_every_size():
    roomy = _req(1, [("a", (20.5, 0.0, 120.0, 120.0))])
    full = _req(2, [("wall", (20.5, 0.0, 256.0, 256.0))])
    _, p2 = T.plan_project([roomy, full])
    assert p2.footprint is None and p2.problem is not None
    assert p2.problem.startswith("plate 2: no free prime tower spot") and "plate 2 blocks every size" in p2.problem
    assert "is blocked by wall" in p2.problem


def test_build_writes_one_tower_size_for_two_plates(tmp_path, stls, resolver):
    plates = [
        {
            "name": "p",
            "parts": [
                {"path": str(stls["body"]), "name": "body_1", "at": [30, 30]},
                {"path": str(stls["text"]), "filament": 2, "at": [80, 30]},
            ],
        },
        {
            "name": "q",
            "parts": [
                {"path": str(stls["base"]), "name": "base_1", "at": [21, 0]},  # fills most of the bed: only a narrow strip stays free
                {"path": str(stls["text"]), "name": "text_1", "filament": 2, "at": [30, 230]},
            ],
        },
    ]
    studio = FakeStudio(sizes={"body.stl": (20, 20, 150), "base.stl": (200, 210, 150), "text.stl": (10, 10, 5)})
    plan = Pipeline(studio, resolver).plan(spec_of(tmp_path, plates, brim_type="no_brim"))
    t1, t2 = (p.tower for p in plan.plates)
    assert t1 is not None and t2 is not None and t1.footprint is not None and t2.footprint is not None, [
        c.text for p in plan.plates for c in p.checks if not c.ok
    ]
    assert t1.footprint.settings == t2.footprint.settings and not t1.footprint.settings.rib_wall
    assert plan.process["prime_tower_rib_wall"] == "0" and plan.process["prime_tower_width"] == f"{t1.footprint.settings.width:g}"
    assert not any("prime tower size is per project" in p for p in plan.problems)


def test_smooth_timelapse_plans_a_tower_on_a_one_filament_plate(tmp_path, stls, resolver):
    plates = [{"name": "p", "parts": [{"path": str(stls["body"]), "name": "body_1"}]}]
    plan = Pipeline(FakeStudio(), resolver).plan(spec_of(tmp_path, plates, timelapse_type="1"))
    tower = plan.plates[0].tower
    assert tower is not None and tower.footprint is not None and tower.mode == "auto"
    plain = Pipeline(FakeStudio(), resolver).plan(spec_of(tmp_path, plates))
    assert plain.plates[0].tower is None


# ---------------------------------------------------------------------------- one support predicate
def test_raft_only_part_counts_the_process_interface_filament(tmp_path, stls, resolver):
    process = {"support_interface_filament": "2", "enable_support": "0"}
    raft = PartSpec.from_json({"path": str(tmp_path / "a.stl"), "set": {"raft_layers": "3"}})
    assert effective_filaments(raft, process=process) == {1, 2}
    plates = [{"name": "p", "parts": [{"path": str(stls["body"]), "name": "body_1", "at": [5, 40], "set": {"raft_layers": "3"}}]}]
    plan = Pipeline(FakeStudio(), resolver).plan(spec_of(tmp_path, plates, support_interface_filament="2", enable_support="0"))
    fails = [c.text for c in plan.plates[0].checks if not c.ok]
    assert any(t.startswith("body_1 uses filament 2 as the process support_interface_filament (extruder 2") for t in fails), fails


# ---------------------------------------------------------------------------- the obstacle test is a parameter
@dataclass(frozen=True)
class OpenStrip:
    """A test fake of `tower.Clearance`: only the T8 strip between housing and feeler is free."""

    def clear(self, rect):
        return rect[0] >= 168.5 and rect[2] <= 196.0 and rect[1] >= 145.6

    def gap(self, rect):
        return 0.0

    def blocker(self, rect):
        return (1.0, "feeler_module_print")

    def blocking(self, rect):
        return [] if self.clear(rect) else [("feeler_module_print", (195.1, 16.0, 245.1, 242.4))]

    def describe(self):
        return "the T8 strip only"


def test_placement_takes_a_clearance_binding():
    t8 = replace(DEFAULT, rib_wall=False, width=22.0, brim=2.0)
    fp, _, _ = T.find_spot(t8, H14, OpenStrip(), REACH)
    assert fp is not None and fp.rect[0] >= 168.5 and fp.rect[2] <= 196.0
    req = T.TowerRequest(1, "auto", DEFAULT, H14, tuple(PLATE14), REACH, BED, clearance=OpenStrip())
    (plan,) = T.plan_project([req])
    assert plan.footprint is not None and plan.footprint.settings.width == 22.0  # 35, 30, 25 do not fit the strip


# ---------------------------------------------------------------------------- review round 2
def recipe_spec(tmp_path, plates, recipes=("dual-material-interface",), **process_set) -> Spec:
    return Spec.from_json(
        {
            "name": "towers",
            "outdir": str(tmp_path / "out"),
            "printer": MACHINE,
            "process": {"base": PROCESS, "name": PROCESS_NAME, "recipes": list(recipes), "set": dict(process_set)},
            "filaments": [{"preset": "Test PLA", "colour": "#000000"}, {"preset": "Test PLA", "colour": "#FFFFFF"}],
            "plates": plates,
        }
    )


def test_short_supported_part_packs_its_interface_inside_the_right_nozzle_reach(tmp_path, stls, resolver):
    plates = [{"name": "p", "parts": [{"path": str(stls["body"]), "name": "body_1"}]}]
    plan = Pipeline(FakeStudio(sizes={"body.stl": (20, 20, 10)}), resolver).plan(recipe_spec(tmp_path, plates))
    assert plan.ok, (plan.problems, [c.text for c in plan.plates[0].checks if not c.ok])
    (part,) = plan.plates[0].parts
    spread = L.brim_margin({**plan.process, **part.spec.set}) + L.INTERFACE_SPREAD
    assert part.placed.interface == {2: (spread, "process support_interface_filament")}
    assert part.placed.x0 - spread >= 20.5 - 1e-9  # the packer holds the interface spread inside extruder 2's reach


def _fixture_for(tmp_path, plan, **over):
    keys = {k: plan.process[k] for k in plan.spec.process.set}
    return make_3mf(tmp_path / "fx.3mf", object_names=("body_1", "base_1"), change_filament_gcode="M600", **{**keys, **over})


def test_wipe_tower_false_with_the_recipe_verifies(tmp_path, stls, resolver):
    plates = [
        {
            "name": "p",
            "wipe_tower": False,
            "parts": [
                {"path": str(stls["body"]), "name": "body_1", "at": [60, 40]},
                {"path": str(stls["base"]), "name": "base_1", "at": [120, 40]},
            ],
        }
    ]
    sp = recipe_spec(tmp_path, plates)
    plan = Pipeline(FakeStudio(), resolver).plan(sp)
    assert plan.process["enable_prime_tower"] == "0" and sp.process.set["enable_prime_tower"] == "1"
    r = Pipeline(FakeStudio(_fixture_for(tmp_path, plan)), resolver).build(sp)
    assert r.verify_problems is not None
    assert not [p for p in r.verify_problems if "enable_prime_tower" in p or "prime_tower" in p], r.verify_problems


def test_verify_compares_the_tower_size_and_spot(tmp_path, stls, resolver):
    plates = [
        {
            "name": "p",
            "wipe_tower": {"x": 172, "y": 150, "width": 22, "rib_wall": False, "brim": 2},
            "parts": [
                {"path": str(stls["body"]), "name": "body_1", "at": [60, 40]},
                {"path": str(stls["base"]), "name": "base_1", "at": [120, 40]},
            ],
        }
    ]
    sp = recipe_spec(tmp_path, plates)
    plan = Pipeline(FakeStudio(), resolver).plan(sp)
    good = _fixture_for(tmp_path, plan, wipe_tower_x=["172"], wipe_tower_y=["150"])
    r = Pipeline(FakeStudio(good), resolver).build(sp)
    assert not [p for p in r.verify_problems or [] if "tower" in p], r.verify_problems
    ps = {k: plan.process[k] for k in ("prime_tower_width", "prime_tower_rib_wall", "prime_tower_brim_width", "enable_prime_tower")}
    probs = Pipeline._verify_tower(plan, {**ps, "wipe_tower_x": ["208"], "wipe_tower_y": ["150"], "prime_tower_width": "35"})
    assert any(p.startswith("plate 1 wipe_tower_x in file is ['208']") for p in probs)
    assert any(p.startswith("prime_tower_width in file is '35', the tower plan wrote '22'") for p in probs)
    missing = Pipeline._verify_tower(plan, {k: v for k, v in ps.items() if k != "prime_tower_brim_width"})
    assert "prime_tower_brim_width is missing from the file, the tower plan wrote '2'" in missing
    assert any(p.startswith("wipe_tower_x in file is None: it needs one value per plate") for p in missing)


@pytest.mark.parametrize("vec", [[], ["garbage"], ["NaN"], ["inf"]])
def test_verify_rejects_empty_garbage_and_nan_tower_vectors(tmp_path, stls, resolver, vec):
    plates = [
        {
            "name": "p",
            "wipe_tower": {"x": 172, "y": 150, "width": 22},
            "parts": [{"path": str(stls["body"]), "at": [60, 40]}, {"path": str(stls["text"]), "filament": 2, "at": [120, 40]}],
        }
    ]
    plan = Pipeline(FakeStudio(), resolver).plan(spec_of(tmp_path, plates))
    ps = {k: plan.process[k] for k in ("prime_tower_width", "prime_tower_rib_wall", "prime_tower_brim_width", "enable_prime_tower")}
    probs = Pipeline._verify_tower(plan, {**ps, "wipe_tower_x": vec, "wipe_tower_y": ["150"]})
    assert any("wipe_tower_x in file is" in p for p in probs), probs
    assert not any("wipe_tower_y" in p for p in probs)


def test_verify_rejects_a_short_tower_vector(tmp_path, stls, resolver):
    """Studio's get_at() falls back to the FIRST value: a one-entry vector would put plate 2's tower at plate 1's spot."""
    t = {"x": 172, "y": 150, "width": 22, "rib_wall": False, "brim": 2}
    plan = Pipeline(FakeStudio(), resolver).plan(spec_of(tmp_path, two_plates(stls, t, {**t, "x": 120})))
    ps = {k: plan.process[k] for k in ("prime_tower_width", "prime_tower_rib_wall", "prime_tower_brim_width", "enable_prime_tower")}
    probs = Pipeline._verify_tower(plan, {**ps, "wipe_tower_x": ["172"], "wipe_tower_y": ["150", "150"]})
    assert probs == ["wipe_tower_x in file is ['172']: it needs one value per plate (2)"]
    assert Pipeline._verify_tower(plan, {**ps, "wipe_tower_x": ["172", "120"], "wipe_tower_y": ["150", "150"]}) == []


def test_one_filament_smooth_timelapse_tower_is_written(tmp_path, stls, resolver):
    sp = Spec.from_json(
        {
            "name": "smooth",
            "outdir": str(tmp_path / "out"),
            "printer": MACHINE,
            "process": {"base": PROCESS, "name": PROCESS_NAME, "set": {"timelapse_type": "1"}},
            "filaments": [{"preset": "Test PLA", "colour": "#000000"}],
            "plates": [{"name": "p", "parts": [{"path": str(stls["body"]), "name": "body_1"}]}],
        }
    )
    studio = FakeStudio(
        make_3mf(tmp_path / "one.3mf", filament_settings_id=["Test PLA"], filament_type=["PLA"], change_filament_gcode="M600")
    )
    r = Pipeline(studio, resolver).build(sp, keep_going=True)
    fp = r.plan.plates[0].tower.footprint
    assert fp is not None
    args = next(a for kind, a in studio.calls if kind == "run")
    assert f"--wipe-tower-x={float(fp.x)}" in args and f"--wipe-tower-y={float(fp.y)}" in args


def test_spec_warnings_reach_the_plan(tmp_path, stls, resolver):
    d = recipe_spec(tmp_path, [{"name": "p", "parts": [{"path": str(stls["body"])}]}]).to_json()
    d["plates"][0]["parts"][0]["brim_margn"] = 2
    d["wipe_tower"] = False  # a project-level wipe_tower is not read
    plan = Pipeline(FakeStudio(), resolver).plan(Spec.from_json(d))
    assert any("unknown key 'brim_margn'" in w for w in plan.warnings)
    assert any("spec: unknown key 'wipe_tower'" in w for w in plan.warnings)


def test_origins_reject_a_footprint_taller_than_the_bed():
    """Width 20, no rib wall, no brim and a 342 mm3 prime volume: 256.5 mm deep, so there is no spot (int() once
    accepted y 0..256.5)."""
    s = T.TowerSettings(width=20.0, rib_wall=False, brim=0.0, wipe_volume=342.0, change_volume=0.0)
    assert s.size(H14)[1] > 256
    assert T.find_spot(s, H14, [], REACH)[0] is None
    assert list(T._origins(20.0, 256.5, 0.0, BED, 1.0)) == []


def test_unparsable_tower_value_is_a_build_error():
    from shambu.errors import BuildError

    with pytest.raises(BuildError, match=r"prime_tower_width = '22mm' is not a number"):
        T.TowerSettings.from_config({"prime_tower_width": "22mm"})


def test_unparsable_interface_slot_is_a_build_error(tmp_path):
    from shambu.build import interface_uses
    from shambu.errors import BuildError

    part = PartSpec.from_json({"path": str(tmp_path / "a.stl")})
    with pytest.raises(BuildError, match="process support_interface_filament = 'two'"):
        interface_uses(part, {"enable_support": "1", "support_interface_filament": "two"}, 5.2)


def test_part_support_slot_with_support_off_is_not_counted(tmp_path):
    part = PartSpec.from_json({"path": str(tmp_path / "a.stl"), "set": {"support_filament": "2", "enable_support": "0"}})
    assert effective_filaments(part, process={"enable_support": "0"}) == {1}


def test_auto_tower_beside_a_part_packed_against_the_reserve(tmp_path, stls, resolver):
    """The provisional back-right reserve is grown by the clearance, so a packed part never ends flush with it."""
    plates = [{"name": "p", "parts": [{"path": str(stls["body"]), "count": 12}, {"path": str(stls["text"]), "filament": 2}]}]
    plan = Pipeline(FakeStudio(sizes={"body.stl": (45, 45, 20), "text.stl": (10, 10, 5)}), resolver).plan(spec_of(tmp_path, plates))
    assert plan.ok, (plan.problems, [c.text for c in plan.plates[0].checks if not c.ok])
    fp = plan.plates[0].tower.footprint
    assert fp is not None
    assert all(T._gap(fp.rect, x.placed.rect()) >= T.PART_CLEARANCE - 1e-9 for x in plan.plates[0].parts)


def test_process_enable_prime_tower_0_turns_the_tower_off(tmp_path, stls, resolver):
    plan = Pipeline(FakeStudio(), resolver).plan(spec_of(tmp_path, two_plates(stls, "auto", "auto"), enable_prime_tower="0"))
    assert all(p.tower is not None and p.tower.mode == "off" for p in plan.plates)
    assert any(w.startswith("plate 1 wipe_tower false") for w in plan.warnings)


def test_tower_size_in_process_set_turns_the_fallback_off():
    tight = _req(1, PLATE14)
    (with_fallback,) = T.plan_project([tight])
    (pinned,) = T.plan_project([tight], fallback=False)
    assert with_fallback.footprint is not None and pinned.footprint is None
    assert pinned.problem is not None and "tried 41.4 mm (rib wall) " in pinned.problem


def test_prime_volume_mode_saving_uses_15_mm3():
    s = T.TowerSettings.from_config({"prime_volume_mode": "Saving"}, PLATE14_PRESETS)
    assert s.wipe_volume == 15.0


def test_slicing_error_without_a_log_file(tmp_path):
    from shambu.studio import RunResult

    rr = RunResult(["BambuStudio"], 156, -100, 1.0, tmp_path / "missing.log", None)
    assert rr.slicing_error() is None and rr.summary().startswith("exit -100 (")


def test_column_narrowing_keeps_rects_inside_the_scoring_radius():
    c = T.BoxClearance((("near", (110.0, 0.0, 120.0, 10.0)), ("far", (130.0, 0.0, 140.0, 10.0))))
    kept = [n for n, _ in c.narrow(0.0, 104.0).obstacles]
    assert kept == ["near"]  # 6 mm away: it still lowers the clearance score (capped at 10 mm)
    assert c.narrow(0.0, 104.0).gap((90.0, 0.0, 104.0, 10.0)) == pytest.approx(6.0)
    assert c.grown is c.grown  # computed once
