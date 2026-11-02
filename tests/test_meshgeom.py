"""meshgeom: welding and edge use, regions, the 2D hull, moments and the ray grid, on meshes built in code."""

from __future__ import annotations

import math
import random

import pytest

from meshes import box, flipped, without
from shambu import meshgeom as G


def test_closed_box_has_no_open_or_flipped_edges():
    w = G.weld(box((0, 0, 0), (10, 20, 5)))
    assert w.open_edges() == [] and w.nonmanifold_edges() == [] and w.flipped_edges() == 0


def test_box_minus_one_facet_has_three_open_edges():
    assert len(G.weld(without(box((0, 0, 0), (10, 10, 10)), 3)).open_edges()) == 3


def test_one_reversed_facet_gives_three_flipped_edges():
    assert G.weld(flipped(box((0, 0, 0), (10, 10, 10)), 5)).flipped_edges() == 3


def test_weld_joins_vertices_within_the_tolerance():
    f = box((0, 0, 0), (1, 1, 1))
    nudged = [tuple((x + 2e-5, y, z) for x, y, z in t) if i == 0 else t for i, t in enumerate(f)]
    assert G.weld(nudged).open_edges() == []  # 2e-5 mm is inside the 1e-4 grid


def test_regions_split_two_separate_boxes():
    f = box((0, 0, 0), (1, 1, 1)) + box((5, 0, 0), (6, 1, 1))
    regs = G.regions(range(len(f)), G.weld(f))
    assert [len(r) for r in regs] == [12, 12]


def test_hull_and_signed_margin():
    pts = [(0, 0), (10, 0), (10, 10), (0, 10), (5, 5), (2, 7)]
    hull = G.hull_2d(pts)
    assert sorted(hull) == [(0, 0), (0, 10), (10, 0), (10, 10)]
    assert G.convex_margin((5, 5), hull) == pytest.approx(5)
    assert G.convex_margin((13, 5), hull) == pytest.approx(-3)


def test_min_width_of_a_rotated_rectangle():
    a = math.radians(30)
    rect = [(0, 0), (100, 0), (100, 10), (0, 10)]
    rot = [(x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)) for x, y in rect]
    width, length, _ = G.min_width(G.hull_2d(rot))
    assert width == pytest.approx(10, abs=1e-6) and length == pytest.approx(100, abs=1e-6)


def test_moments_of_a_box():
    vol, c = G.moments(box((0, 0, 0), (10, 20, 30)))
    assert vol == pytest.approx(6000) and c == pytest.approx((5, 10, 15))


def test_ray_grid_matches_brute_force():
    f = box((0, 0, 0), (10, 7, 4)) + box((2, 2, 1), (3, 3, 2))
    grid = G.RayGrid(f, cell=0.7)
    rng = random.Random(7)  # noqa: S311 (fixed test rays, not a secret)
    for _ in range(500):
        o = (rng.uniform(-2, 12), rng.uniform(-2, 9), rng.uniform(-2, 6))
        d = (rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(0, 1))
        n = math.sqrt(sum(x * x for x in d))
        d = (d[0] / n, d[1] / n, d[2] / n)
        brute = None
        for i, t in enumerate(f):
            h = G.ray_triangle(o, d, t)
            if h is not None and h > 1e-3 and (brute is None or h < brute[0]):
                brute = (h, i)
        got = grid.first_hit(o, d)
        if brute is None:
            assert got is None
        else:
            assert got is not None and got[0] == pytest.approx(brute[0], abs=1e-9)


def test_edge_chains_find_a_closed_loop():
    loop = [(0, 1), (1, 2), (2, 3), (0, 3)]
    assert G.edge_chains(loop) == [(sorted(loop), True)]
    two = G.edge_chains([(0, 1), (5, 6)])
    assert len(two) == 2 and not any(closed for _, closed in two)


@pytest.mark.parametrize("deg", [0, 30, 45])
def test_farthest_point_from_two_anchors(deg):
    # a 30 x 10 ceiling held at both short ends: the middle is 15 mm from either anchor, at any rotation
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))

    def r(p):
        return (p[0] * c - p[1] * s, p[0] * s + p[1] * c)

    a, b, cc, d = r((0, 0)), r((30, 0)), r((30, 10)), r((0, 10))
    tris = [(a, b, cc), (a, cc, d)]
    assert G.farthest_in(tris, [(a, d), (b, cc)]) == pytest.approx(15, abs=0.25)
    assert G.farthest_in(tris, [(a, d), (b, cc), (a, b), (d, cc)]) == pytest.approx(5, abs=0.25)
