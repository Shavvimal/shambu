"""layout.py: ShelfPacker placement and the LimitChecker rules."""

import pytest

from conftest import mesh
from shambu import layout as L
from shambu.errors import LayoutError

FACTS = {
    "bed": {"x": (0.0, 256.0), "y": (0.0, 256.0)},
    "z_max": 325.0,
    "extruders": [
        {"index": 1, "x": (0.0, 256.0), "y": (0.0, 256.0), "z_max": 320.0, "type": "Direct Drive", "nozzle": "0.4"},
        {"index": 2, "x": (20.5, 256.0), "y": (0.0, 256.0), "z_max": 325.0, "type": "Bowden", "nozzle": "0.4"},
    ],
}


def part(key, w=20.0, h=20.0, z=10.0, filament=1, x0=0.0, y0=0.0, margin=0.0, also=(), manifold=True, lo=(0.0, 0.0, 0.0)):
    return L.Placed(key, mesh(f"{key}.stl", (w, h, z), lo, manifold), filament, x0, y0, margin, also_filaments=frozenset(also))


def failures(placed, **ctx):
    return [c.text for c in L.LimitChecker().check(placed, L.Context(FACTS, **ctx)) if not c.ok]


# ---------------------------------------------------------------------------- packer
def test_packs_one_row_left_to_right():
    ps = [part("a"), part("b"), part("c")]
    plates = L.ShelfPacker(gap=5).place(ps)
    assert len(plates) == 1
    assert [(p.x0, p.y0) for p in plates[0]] == [(2.0, 2.0), (27.0, 2.0), (52.0, 2.0)]


def test_deepest_first_and_new_row_when_full():
    ps = [part("small", 100, 10), part("deep", 100, 50), part("mid", 100, 30)]
    plate = L.ShelfPacker(gap=5).place(ps)[0]
    assert [p.key for p in plate] == ["deep", "mid", "small"]
    assert plate[2].y0 == 2.0 + 50 + 5  # third part starts a second row


def test_margin_offsets_footprint():
    p = L.ShelfPacker().place([part("a", margin=5.2)])[0][0]
    assert (p.x0, p.y0) == (7.2, 7.2)
    assert p.rect() == (2.0, 2.0, 32.4, 32.4)


def test_x_min_of_pushes_part_right():
    a, b = part("a"), part("b")
    L.ShelfPacker().place([a, b], x_min_of={"b": 20.5})
    assert a.x0 == 2.0
    assert b.x0 == 27.0  # 2 + 20 + 5 is already past 20.5
    c = part("c")
    L.ShelfPacker().place([c], (), {"c": 20.5})
    assert c.x0 == 20.5


def test_overflow_to_second_plate():
    ps = [part(f"p{i}", 200, 150) for i in range(3)]
    plates = L.ShelfPacker().place(ps)
    assert [len(p) for p in plates] == [1, 1, 1]
    assert all(p[0].y0 == 2.0 for p in plates)


def test_too_deep_raises():
    with pytest.raises(LayoutError, match="deeper than the bed"):
        L.ShelfPacker().place([part("tall", 20, 300)])


def test_too_wide_raises():
    with pytest.raises(LayoutError, match="wider than the x range"):
        L.ShelfPacker().place([part("wide", 300, 20)])


def test_obstacle_is_avoided():
    tower = (0.0, 0.0, 50.0, 50.0)
    p = part("a")
    L.ShelfPacker(gap=5).place([p], [tower])
    assert not L.overlaps(p.rect(), tower)
    assert p.x0 == 55.0


def test_x_min_of_never_pushes_a_row_off_the_bed():
    a, b = part("a", 120, 20), part("b", 120, 20)
    L.ShelfPacker().place([a, b], x_min_of={"a": 20.5})
    assert max(a.x1, b.x1) <= 254.0


def test_pos_translates_bbox_onto_bed():
    p = part("a", lo=(-10.0, 5.0, 2.0))
    p.x0, p.y0 = 30.0, 40.0
    assert p.pos() == (40.0, 35.0, -2.0)


@pytest.mark.parametrize(
    ("params", "margin"),
    [
        ({"brim_type": "no_brim"}, 0.0),
        ({"brim_type": "brim_ears"}, 3.0),
        ({"brim_type": "outer_only", "brim_width": "5"}, 5.2),
        (None, 5.2),
    ],
)
def test_brim_margin(params, margin):
    assert L.brim_margin(params) == pytest.approx(margin)


# ---------------------------------------------------------------------------- rules
def test_all_good_adds_summary():
    checks = L.LimitChecker().check([part("a", x0=30, y0=30)], L.Context(FACTS))
    assert [c.ok for c in checks] == [True]
    assert "1 part(s) fit" in checks[0].text


def test_overlap_flagged():
    assert any("overlap" in t for t in failures([part("a", x0=30, y0=30), part("b", x0=40, y0=40)]))


def test_bed_exit_flagged():
    assert any("leaves the bed" in t for t in failures([part("a", x0=250, y0=30)]))


def test_filament_2_reach():
    assert any("extruder 2" in t and "reaches x 20.5" in t for t in failures([part("a", filament=2, x0=5, y0=30)]))


def test_height_range_filament_2_reach_via_also_filaments():
    t = failures([part("a", filament=1, also=(2,), x0=5, y0=30)])
    assert any("uses filament 2" in x for x in t)


def test_main_filament_left_edge_is_fine():
    assert failures([part("a", filament=1, x0=5, y0=30)]) == []


def test_aux_x_limit():
    t = failures([part("a", filament=2, x0=220, y0=30)], aux_x_limit=235.5)
    assert any("235.5" in x for x in t)
    assert failures([part("a", filament=1, x0=220, y0=30)], aux_x_limit=235.5) == []


def test_filament_map_redirects_reach():
    assert failures([part("a", filament=2, x0=5, y0=30)], filament_extruder={2: 1}) == []


def test_height_over_extruder_z_max():
    assert any("prints up to 320" in t for t in failures([part("a", z=322, x0=30, y0=30)]))


def test_tpu_off_main_flagged():
    t = failures([part("a", filament=2, x0=30, y0=30)], tpu_filaments={2}, filament_extruder={2: 2})
    assert any("TPU" in x for x in t)


def test_non_manifold_flagged():
    assert any("not manifold" in t for t in failures([part("a", x0=30, y0=30, manifold=False)]))


def test_off_bed_mesh_is_a_note_not_a_failure():
    checks = L.LimitChecker().check([part("a", x0=30, y0=30, lo=(0, 0, 3))], L.Context(FACTS))
    assert all(c.ok for c in checks)
    assert any("drops it onto the bed" in c.text for c in checks)


def test_custom_rule_set():
    class Always:
        def check(self, placed, ctx):
            yield L.Check(False, "custom rule")

    assert [c.text for c in L.LimitChecker((Always(),)).check([], L.Context(FACTS))] == ["custom rule"]
