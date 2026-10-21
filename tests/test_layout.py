"""layout.py: support spread, the pair rule, footprints, both packers and the LimitChecker rules."""

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


def test_too_deep_is_unplaced_not_raised():
    p = part("tall", 20, 300)
    got = L.ShelfPacker().place([p])
    assert got.plates == [] and "300.0 mm deep" in got.unplaced["tall"] and not p.placed


def test_too_wide_is_unplaced_not_raised():
    got = L.ShelfPacker().place([part("wide", 300, 20), part("ok")])
    assert "300.0 mm wide" in got.unplaced["wide"] and [p.key for p in got[0]] == ["ok"]


def test_union_info_needs_a_mesh():
    with pytest.raises(LayoutError):
        L.union_info([])


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


def test_aux_x_limit_clips_a_brim_and_fails_a_support():
    """The mesh ends at x 230 (under 235.5). Its 10 mm brim reaches x 240: Studio clips it (WARN). A 10 mm support
    spread reaching x 240 is a FAIL."""
    p = part("a", filament=2, x0=210, y0=30, margin=10)
    cs = L.LimitChecker().check([p], L.Context(FACTS, aux_x_limit=235.5))
    assert all(c.ok for c in cs) and any(
        c.kind == "brim" and "reaches x 240.0 on +x" in c.text and "clipped at x 235.5" in c.text for c in cs
    )
    p.margin, p.spread = 0.0, L.Spread(0, 10, 0, 0, "probe", True)
    t = failures([p], aux_x_limit=235.5)
    assert any("aux_x_limit" in x and "235.5" in x and "reaches x 240.0" in x for x in t)


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


# ---------------------------------------------------------------------------- batch 1 (real-STL test findings)
def test_mesh_in_metres_fails_the_check():
    t = failures([part("jaw", w=0.022, h=0.092, z=0.048, x0=30, y0=30)])
    assert any("probably a mesh in metres" in x and "--convert-unit" in x for x in t)
    assert not any("metres" in x for x in failures([part("a", w=0.5, h=20, z=10, x0=30, y0=30)]))  # one thin axis is fine


def test_multi_body_stl_is_a_note():
    from dataclasses import replace

    p = L.Placed("jaw", replace(mesh("jaw.stl"), parts=3), 1, 30, 30)
    checks = L.LimitChecker().check([p], L.Context(FACTS))
    assert all(c.ok for c in checks)
    assert any("holds 3 bodies" in c.text and "shambu split" in c.text for c in checks)


SEQ_FACTS = {
    **FACTS,
    "clearance": {"radius": 60.0, "height_to_rod": 32.8, "height_to_lid": 133.0, "dist_to_rod": 33.0, "nozzle_height": 4.0},
}


def test_sequential_clearance_has_no_ok_line_after_a_fail():
    rule = L.SequentialClearance()
    ctx = L.Context(SEQ_FACTS, by_object=True)
    bad = list(rule.check([part("a", x0=2, y0=2), part("b", x0=40, y0=2)], ctx))
    assert [c.ok for c in bad] == [False]  # the gap failure only, no "print-by-object: ... within the gantry limits"
    good = list(rule.check([part("a", x0=2, y0=2), part("b", x0=90, y0=2)], ctx))
    assert [c.ok for c in good] == [True] and good[0].text.startswith("print-by-object")


def test_extruder_reach_counts_the_margin():
    """Support prints with the part's filament, so it must stay in that extruder's reach (FAIL). A brim past the reach
    is clipped by Studio to the nozzle's printable area (Brim.cpp:1079-1095, 1129-1138): a WARN."""
    assert failures([part("a", filament=2, x0=25, y0=30)]) == []
    brim = part("a", filament=2, x0=25, y0=30, margin=5)  # 25 - 5 = 20 < 20.5
    cs = L.LimitChecker().check([brim], L.Context(FACTS))
    assert all(c.ok for c in cs)
    assert any(c.kind == "brim" and "brim on filament 2 reaches x 20.0 on -x and will be clipped at x 20.5" in c.text for c in cs)
    sup_part = part("a", filament=2, x0=25, y0=30)
    sup_part.spread = L.Spread(5, 0, 0, 0, "probe", True)
    t = failures([sup_part])
    assert any("reaches x 20.0 on -x with its support 5.0 mm; that extruder reaches x 20.5..256.0" in x for x in t)


# ---------------------------------------------------------------------------- support spread (reference 08 section 11)
def spread(mx, px=None, my=None, py=None, source="probe", tree=True):
    px, my, py = (mx if v is None else v for v in (px, my, py))
    return L.Spread(mx, px, my, py, source, tree)


def sup(key, w=20.0, h=20.0, x0=0.0, y0=0.0, s=L.NO_SPREAD, margin=0.0, filament=1, footprint=None, z=10.0):
    p = part(key, w, h, z=z, x0=x0, y0=y0, margin=margin, filament=filament)
    p.spread, p.footprint = s, footprint
    return p


def checks(placed, **ctx):
    return L.LimitChecker().check(placed, L.Context(FACTS, **ctx))


@pytest.mark.parametrize("params", [{"enable_support": "0"}, {}, None])
def test_support_spread_zero_without_support(params):
    s = L.support_spread(params, 95.0)
    assert s.sides() == (0.0, 0.0, 0.0, 0.0) and s.source == "none"


@pytest.mark.parametrize(("height", "want"), [(28.9, 8.0), (218.0, 21.8), (300.0, 22.0)])
def test_tree_spread_estimate_floor_slope_cap(height, want):
    s = L.support_spread({"enable_support": "1", "support_type": "tree(auto)"}, height)
    assert s.sides() == pytest.approx((want,) * 4) and s.source == "estimate" and s.tree


def test_normal_spread_estimate_is_five():
    s = L.support_spread({"enable_support": "1", "support_type": "normal(auto)"}, 200.0)
    assert s.sides() == (5.0, 5.0, 5.0, 5.0) and s.source == "estimate" and not s.tree


def test_probe_spread_replaces_the_estimate():
    probe = L.Spread(21.0, 0.3, 0.3, 2.9, "probe")
    assert L.support_spread({"enable_support": "1"}, 218.0, probe) is probe


def test_spread_turns_with_a_quarter_turn():
    s = L.Spread(1.0, 2.0, 3.0, 4.0, "probe")
    assert s.turned(1).sides() == (4.0, 3.0, 1.0, 2.0)  # (mx, px, my, py) -> (py, my, mx, px)
    assert s.turned(4) == s


def test_pair_clearance_takes_the_max_not_the_sum():
    a = sup("a", x0=10, y0=10, s=spread(8.0))
    b = sup("b", x0=30 + 9.5, y0=10, s=spread(7.0))
    assert L.pair_check(a, b) is None  # needs max(8, 7) + 1 = 9; the sum would need 16
    b.x0 = 30 + 8.5
    c = L.pair_check(a, b)
    assert c is not None and not c.ok and c.kind == "margin" and "a's support reaches 8.0 mm on +x" in c.text


def test_pair_clearance_uses_the_facing_side():
    a = sup("a", x0=50, y0=50, s=spread(21.0, 0.3, 0.3, 0.3))
    b = sup("b", x0=50, y0=74, s=spread(0.0))  # 4 mm above a: a's +y side faces it
    assert L.pair_check(a, b) is None
    b.x0, b.y0 = 26, 50  # 4 mm left of a: a's -x side faces it
    assert L.pair_check(a, b) is not None


def test_estimate_shortfall_warns_probe_shortfall_fails():
    est = L.support_spread({"enable_support": "1"}, 100.0)  # 10 mm, estimated
    a, b = sup("a", x0=10, y0=10, s=est), sup("b", x0=35, y0=10)
    (c,) = [x for x in checks([a, b]) if x.kind == "margin"]
    assert c.ok and c.warn and c.text == "a and b are 5.0 mm apart; support reaches 10.0 mm (estimated); the slice decides"
    a.spread = spread(10.0)
    (c,) = [x for x in checks([a, b]) if x.kind == "margin"]
    assert not c.ok and "measured by a solo slice" in c.text


def test_touching_footprints_fail_without_support():
    a, b = sup("a", x0=10, y0=10), sup("b", x0=30.5, y0=10)
    c = L.pair_check(a, b)
    assert c is not None and not c.ok and c.kind == "body" and "touch (0.5 mm apart" in c.text
    b.x0 = 31.0
    assert L.pair_check(a, b) is None


def test_body_overlap_names_both_boxes_and_the_intersection():
    c = L.pair_check(sup("cube", x0=50, y0=50), sup("cube2", x0=60, y0=60))
    assert c is not None and c.kind == "body"
    assert c.text.startswith("cube (x 50.0..70.0, y 50.0..70.0) and cube2 (x 60.0..80.0, y 60.0..80.0) intersect by 10.0 x 10.0 mm")


def test_brims_do_not_count_between_parts():
    a, b = sup("a", x0=10, y0=10, margin=5.2), sup("b", x0=32, y0=10, margin=5.2)
    assert [x for x in checks([a, b]) if not x.ok or x.warn] == []


def test_inside_bed_ignores_tree_spread_counts_normal_spread():
    tree = sup("feeler", 50.0, 226.4, x0=100, y0=14.8, margin=5.2, s=spread(16.0, source="estimate", tree=True))
    assert failures([tree]) == []
    normal = sup("feeler", 50.0, 226.4, x0=100, y0=14.8, margin=5.2, s=spread(16.0, source="estimate", tree=False))
    (t,) = failures([normal])
    assert "is on the bed, but with its estimate support margin 16.0 mm each side" in t and "leaves the bed" in t


def test_brim_only_off_the_bed_is_a_warn_support_off_the_bed_a_fail():
    brim = sup("a", x0=2, y0=30, margin=5.2)
    cs = checks([brim])
    assert all(c.ok for c in cs) and [c.kind for c in cs if c.warn] == ["brim"]
    normal = sup("a", x0=2, y0=30, s=spread(5.0, source="estimate", tree=False))
    assert any("support margin" in t for t in failures([normal]))


def test_packer_keeps_the_brim_on_the_bed_when_it_can_and_clips_it_when_it_must():
    p = sup("wide", 250, 20, margin=5.2)  # 250 + 2 x 5.2 > 252: only a clipped brim fits
    got = L.BottomLeftPacker().place([p])
    assert got.unplaced == {} and any(c.kind == "brim" for c in checks([p]))
    q = sup("small", 20, 20, margin=5.2)
    L.BottomLeftPacker().place([q])
    assert q.x0 - q.margin >= 2.0  # room for the whole brim: the brim stays on the bed


def test_reach_counts_spread_only_toward_the_dead_strip():
    left = sup("jaw", x0=25, y0=30, filament=2, s=spread(7.0, 0.0, 0.0, 0.0))
    assert any("reaches x 18.0 on -x" in t for t in failures([left]))
    right = sup("jaw", x0=25, y0=30, filament=2, s=spread(0.0, 7.0, 0.0, 0.0))
    assert failures([right]) == []


def test_no_reach_line_when_reach_equals_bed():
    t = failures([part("a", x0=250, y0=30)])
    assert len(t) == 1 and "leaves the bed" in t[0]


def test_unplaced_parts_skip_the_geometric_rules():
    p = part("a", x0=-50, y0=-50)
    p.placed, p.why_not = False, "the packer could not place a"
    assert failures([p, part("b", x0=-50, y0=-50)]) == ["b body x -50.0..-30.0 y -50.0..-30.0 leaves the bed 256 x 256"]
    assert any("not placed" in line for line in L.describe([p]))


# ---------------------------------------------------------------------------- footprints
def _boxes(*rects, z=10.0):
    from test_mesh import box

    return [f for x0, y0, x1, y1 in rects for f in box((x0, y0, 0.0), (x1, y1, z))]


def raster_part(key, rects, x0, y0, s=L.NO_SPREAD):
    from shambu import mesh as M

    facets = _boxes(*rects)
    sil = M.silhouette(facets)
    (lx, ly, _), (hx, hy, _) = M.bbox(facets)
    return sup(key, hx - lx, hy - ly, x0=x0, y0=y0, s=s, footprint=L.RasterFootprint.from_silhouette(sil))


def test_raster_gap_sees_into_a_notch():
    from shambu import mesh as M

    u_rects = [(0, 0, 10, 100), (10, 0, 40, 10), (10, 90, 40, 100)]  # a U open to +x, like the feeler's end blocks
    u = raster_part("u", u_rects, 0, 0)
    inner = sup("inner", 20, 60, x0=15, y0=20)
    assert L.overlaps(u.rect(), inner.rect())  # the boxes overlap
    hull = M.hull_2d((v[0], v[1]) for f in _boxes(*u_rects) for v in f)
    assert {(0.0, 0.0), (40.0, 0.0), (40.0, 100.0), (0.0, 100.0)} == set(hull)  # the hull covers the inner box: gap 0
    g, _ = u.shape().gap(inner.shape())
    assert g == pytest.approx(5.0)
    assert L.pair_check(u, inner) is None


def test_raster_gap_beats_bbox_for_a_part_on_its_side():
    from shambu import mesh as M

    wedge = [  # a prism over the triangle (0, 0), (100, 0), (0, 100)
        ((0, 0, 0), (0, 100, 0), (100, 0, 0)),
        ((0, 0, 10), (100, 0, 10), (0, 100, 10)),
        ((0, 0, 0), (100, 0, 0), (100, 0, 10)),
        ((0, 0, 0), (100, 0, 10), (0, 0, 10)),
        ((0, 0, 0), (0, 0, 10), (0, 100, 10)),
        ((0, 0, 0), (0, 100, 10), (0, 100, 0)),
        ((100, 0, 0), (0, 100, 0), (0, 100, 10)),
        ((100, 0, 0), (0, 100, 10), (100, 0, 10)),
    ]
    w = sup("wedge", 100, 100, x0=0, y0=0, footprint=L.RasterFootprint.from_silhouette(M.silhouette(wedge)))
    other = sup("box", 20, 20, x0=60, y0=100.8)
    assert L.rect_gap(w.rect(False), other.rect(False))[0] == pytest.approx(0.8)
    g, (_dx, dy) = w.shape().gap(other.shape())
    assert g > 20 and dy > 0


def test_footprint_turned_45_is_not_its_square_bbox():
    from shambu import mesh as M

    bar = M.transform(_boxes((0, 0, 50, 226)), M.euler_matrix(0, 0, 45))
    sil = M.silhouette(bar)
    assert (sil.x1 - sil.x0) == pytest.approx(195.2, abs=0.1)  # the turned box: about 195 x 195
    assert 11300 <= sil.area() < 12300  # the bar itself: 50 x 226 = 11300 mm2, plus less than one cell of outline


# ---------------------------------------------------------------------------- packers
PLATE14 = {  # w, d, h (post-mortem 2.2)
    "housing": (124.2, 135.0, 218.0),
    "mount": (40.6, 58.8, 32.1),
    "shell": (58.6, 60.7, 47.8),
    "feeler": (50.0, 226.4, 64.0),
    "cradle": (22.1, 99.0, 28.9),
}
# support past the model box per side, z 0..5, measured in the G-code of the printed plate (reference 08 section 11.2)
PLATE14_PROBE = {
    "housing": (20.9, 0.3, 0.3, 2.9),
    "mount": (0.0, 0.0, 0.0, 0.0),
    "shell": (2.5, 0.0, 0.0, 2.9),
    "feeler": (0.2, 0.6, 4.7, 3.4),
    "cradle": (7.1, 7.1, 0.0, 0.0),
}
TREE = {"enable_support": "1", "support_type": "tree(auto)"}


def plate14(spreads):
    return [sup(k, w, d, z=h, margin=5.2, s=spreads(k, h)) for k, (w, d, h) in PLATE14.items()]


def test_bottom_left_fits_plate14_on_one_plate_with_probe_spreads():
    ps = plate14(lambda k, h: L.Spread(*PLATE14_PROBE[k], "probe", True))
    got = L.BottomLeftPacker().place(ps)
    assert len(got) == 1 and got.unplaced == {}
    assert [c for c in checks(ps) if not c.ok or c.warn] == []


def test_bottom_left_fits_plate14_on_one_plate_with_estimated_spreads():
    ps = plate14(lambda k, h: L.support_spread(TREE, h))
    got = L.BottomLeftPacker().place(ps)
    assert len(got) == 1 and got.unplaced == {}
    assert all(c.ok for c in checks(ps))  # no FAIL, and no WARN either: the packer keeps every estimate
    assert not any(c.warn for c in checks(ps))


def test_row_packer_needs_two_plates_for_plate14():
    """The comparison from reference 08 section 11.5: rows by depth waste the bed above the housing."""
    ps = plate14(lambda k, h: L.NO_SPREAD)
    assert len(L.ShelfPacker().place(ps)) == 2


def test_packer_reports_unplaced_instead_of_raising():
    deep, ok = sup("deep", 20, 300), sup("ok")
    got = L.BottomLeftPacker().place([deep, ok])
    assert list(got.unplaced) == ["deep"] and not deep.placed and ok.placed
    assert got.unplaced["deep"] == (
        "the packer could not place deep: 300.0 mm deep + 2 x 0.0 mm margin = 300.0 mm, the bed area is 252.0 mm deep "
        "(2.0..254.0, inside the layout edges)"
    )


def test_bottom_left_spills_to_a_second_plate():
    ps = [sup(f"p{i}", 200, 150) for i in range(3)]
    got = L.BottomLeftPacker().place(ps)
    assert [len(p) for p in got] == [1, 1, 1]


def test_bottom_left_keeps_clear_of_fixed_parts_and_obstacles():
    fixed = sup("fixed", 100, 100, x0=2, y0=2)
    fixed.fixed = True
    p = sup("p", 50, 50, s=spread(8.0, source="estimate"))
    got = L.BottomLeftPacker().place([p], [(203.0, 203.0, 256.0, 256.0)], fixed=[fixed])
    assert len(got) == 1 and L.pair_check(p, fixed) is None and not L.overlaps(p.rect(), (203.0, 203.0, 256.0, 256.0))


def test_bottom_left_keeps_filament_2_in_reach():
    p = sup("label", 30, 30, filament=2, margin=5.2)
    L.BottomLeftPacker().place([p], x_min_of={"label": 20.5})
    assert p.x0 - p.margin >= 20.5


def test_shelf_packer_kept_for_by_object_order():
    ps = [part("tall", 30, 30, z=60), part("a"), part("b")]
    plate = L.ShelfPacker(gap=60, keep_order=True, solo_row_above=32.8).place(ps)[0]
    assert [p.key for p in plate] == ["tall", "a", "b"]  # list order kept
    assert plate[1].y0 > plate[0].y1  # the tall object ends its row


def test_plate14_printed_layout_passes():
    """The five footprints where the printed plate had them (model boxes from its G-code, reference 08 section 11.2)
    with the measured spreads: no FAIL. The feeler's wide part exists only in its two end blocks, and the housing,
    on its side, is lower under the shell than its box: both need the silhouette."""
    housing = raster_part("housing", [(44.5, 26.2, 168.3, 139.0), (44.5, 139.0, 100.0, 160.7)], 44.5, 26.2)
    feeler = raster_part("feeler", [(218.4, 28.0, 244.8, 230.0), (195.3, 16.2, 244.8, 28.0), (195.3, 230.0, 244.8, 242.2)], 195.3, 16.2)
    others = [
        sup("mount", 40.0, 58.3, x0=33.0, y0=181.3),
        sup("shell", 58.0, 60.1, x0=106.0, y0=162.1),
        sup("cradle", 21.7, 98.6, x0=176.9, y0=46.8),
    ]
    ps = [housing, feeler, *others]
    for p in ps:
        p.spread = L.Spread(*PLATE14_PROBE[p.key], "probe", True)
    assert L.overlaps(feeler.rect(False), others[2].rect(False))  # the boxes overlap by 3.3 mm
    assert [c.text for c in checks(ps) if not c.ok or c.warn] == []


# ---------------------------------------------------------------------------- review fixes (Codex, 2026-10-21)
U_RECTS = [(0, 0, 10, 100), (10, 0, 40, 10), (10, 90, 40, 100)]  # a U open to +x


@pytest.mark.parametrize(
    ("params", "want"),
    [
        ({"enable_support": "1"}, True),
        ({"enable_support": "true"}, True),
        ({"enforce_support_layers": "3"}, True),
        ({"raft_layers": "2"}, True),
        ({"enable_support": "0", "enforce_support_layers": "0", "raft_layers": "0"}, False),
        ({}, False),
        (None, False),
    ],
)
def test_needs_support_follows_studio(params, want):
    """Print.hpp:492-494: has_support() || has_raft()."""
    assert L.needs_support(params) is want


def test_raft_only_part_gets_a_spread():
    s = L.support_spread({"raft_layers": "3"}, 40.0)
    assert s.sides() == (5.0,) * 4 and s.source == "estimate" and not s.tree
    assert L.support_spread({"raft_layers": "3", "raft_first_layer_expansion": "8"}, 40.0).most() == 8.0


def test_support_inside_a_notch_fails_with_the_mask():
    """Support that fills a U-shaped notch stays inside the U's box: all four spreads are 0. A part nested in the
    notch 5 mm from the U's back passes on the spreads and fails on the measured support mask."""
    u = raster_part("u", U_RECTS, 0, 0, s=L.Spread(0, 0, 0, 0, "probe", True))
    inner = sup("inner", 20, 60, x0=15, y0=20)
    assert L.pair_check(u, inner) is None  # the spreads cannot see it
    u.support_mask = L.mask_from_points((x / 2, y / 2) for x in range(24, 78) for y in range(30, 170))  # x 12..39, y 15..85
    c = L.pair_check(u, inner)
    assert c is not None and not c.ok and c.kind == "margin"
    assert "u's support (measured by a solo slice) comes within 0.0 mm of inner" in c.text
    inner.x0 = 60  # out of the notch, clear of the mask
    assert L.pair_check(u, inner) is None


def test_mask_round_trips_through_json():
    m = L.mask_from_points([(-3.2, 1.1), (-2.9, 1.2), (5.0, -4.0)])
    assert m is not None and m.bbox() == (-3.5, -4.0, 5.5, 1.5)
    assert L.RasterFootprint.from_json(m.to_json()) == m


def test_group_outline_keeps_a_members_notch():
    u = raster_part("u", U_RECTS, 0, 0)
    group = L.union_footprint([u.shape()], (0.0, 0.0))
    assert group is not None
    inner = sup("inner", 20, 60, x0=15, y0=20)
    g, _ = group.gap(inner.shape())
    assert g == pytest.approx(5.0)


def test_a_concave_part_keeps_its_outline_when_fixed():
    """A part that fits beside a concave free object fits beside the same object when it is fixed with `at`: the
    packer reads the fixed part's footprint, not its box."""
    free_u = raster_part("u", U_RECTS, 0, 0)
    small = sup("small", 20, 60)
    got = L.BottomLeftPacker().place([free_u, small])
    assert len(got) == 1 and got.unplaced == {}
    fixed_u = raster_part("u", U_RECTS, free_u.x0, free_u.y0)
    fixed_u.fixed = True
    small2 = sup("small", 20, 60)
    got2 = L.BottomLeftPacker().place([small2], fixed=[fixed_u])
    assert len(got2) == 1 and got2.unplaced == {} and L.pair_check(small2, fixed_u) is None
    assert (small2.x0, small2.y0) == (small.x0, small.y0)
