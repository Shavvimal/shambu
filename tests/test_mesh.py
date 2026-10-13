"""mesh.py: STL round trip, rotation, lay-on-face, metrics and candidate ranking on synthetic shapes."""

import math

import pytest

from shambu import mesh as M
from shambu.errors import BuildError


def box(lo, hi):
    """12 outward-wound facets of an axis-aligned box."""
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    p = {
        0: (x0, y0, z0), 1: (x1, y0, z0), 2: (x1, y1, z0), 3: (x0, y1, z0),
        4: (x0, y0, z1), 5: (x1, y0, z1), 6: (x1, y1, z1), 7: (x0, y1, z1),
    }  # fmt: skip
    quads = [
        (0, 3, 2, 1),  # bottom (-z)
        (4, 5, 6, 7),  # top (+z)
        (0, 1, 5, 4),  # front (-y)
        (2, 3, 7, 6),  # back (+y)
        (1, 2, 6, 5),  # right (+x)
        (3, 0, 4, 7),  # left (-x)
    ]
    out = []
    for a, b, c, d in quads:
        out.append((p[a], p[b], p[c]))
        out.append((p[a], p[c], p[d]))
    return out


def table():
    """A 20 x 20 x 2 slab on a 4 x 4 x 10 column, one closed surface (no internal faces): as-is the slab's
    underside ring (400 - 16 mm2) needs supports, upside down nothing does."""
    column = [f for f in box((8, 8, 0), (12, 12, 10)) if not all(v[2] == 10 for v in f)]  # drop the top
    slab = [f for f in box((0, 0, 10), (20, 20, 12)) if not all(v[2] == 10 for v in f)]  # drop the bottom
    o = [(0, 0, 10), (20, 0, 10), (20, 20, 10), (0, 20, 10)]
    i = [(8, 8, 10), (12, 8, 10), (12, 12, 10), (8, 12, 10)]
    ring = []
    for k in range(4):
        a, b, c, d = o[k], o[(k + 1) % 4], i[(k + 1) % 4], i[k]
        for tri in ((a, b, c), (a, c, d)):
            ring.append(tri if M.facet_normal(tri)[2] < 0 else (tri[0], tri[2], tri[1]))
    return column + slab + ring


# ---------------------------------------------------------------------------- geometry
def test_box_metrics():
    m = M.metrics(box((0, 0, 0), (20, 10, 5)))
    assert m.size == (20, 10, 5)
    assert m.contact_mm2 == pytest.approx(200)
    assert m.overhang_mm2 == 0 and m.shadow_mm2 == 0
    assert m.volume_mm3 == pytest.approx(1000)
    assert m.centroid_z == pytest.approx(2.5)
    assert m.steepest_deg == 0


def test_table_overhang_and_flip():
    m = M.metrics(table())
    assert m.contact_mm2 == pytest.approx(16)
    assert m.shadow_mm2 == pytest.approx(384)  # the slab's underside ring, projected
    assert m.steepest_deg == pytest.approx(90)
    flipped = M.metrics(M.transform(table(), M.lay_on_face_matrix((0, 0, 1))))
    assert flipped.contact_mm2 == pytest.approx(400)
    assert flipped.shadow_mm2 == 0
    assert flipped.height == pytest.approx(12)
    assert M.volume(table()) == pytest.approx(16 * 10 + 400 * 2)


def test_threshold_counts_only_steeper_facets():
    # a 45-degree roof: a wedge whose underside slopes at exactly 45 degrees from vertical
    wedge = [((0, 0, 0), (10, 0, 0), (10, 0, 10)), ((0, 10, 0), (10, 10, 10), (10, 10, 0))]  # two end caps
    wedge += [((0, 0, 0), (0, 10, 0), (10, 10, 10)), ((0, 0, 0), (10, 10, 10), (10, 0, 10))]  # the 45-degree underside (downward normal)
    wedge += [((10, 0, 0), (10, 10, 0), (10, 10, 10)), ((10, 0, 0), (10, 10, 10), (10, 0, 10))]  # back wall
    wedge += [((0, 0, 0), (10, 0, 0), (10, 10, 0)), ((0, 0, 0), (10, 10, 0), (0, 10, 0))]  # floor
    m45 = M.metrics(wedge, threshold_deg=45)
    m40 = M.metrics(wedge, threshold_deg=40)
    assert m45.steepest_deg == pytest.approx(45)
    assert m45.overhang_mm2 == 0  # exactly at the threshold: printable
    assert m40.overhang_mm2 == pytest.approx(10 * 10 * math.sqrt(2))


def test_candidates_prefer_the_flipped_table():
    cands = M.candidates(table())
    best = cands[0]
    assert best.metrics.shadow_mm2 == 0
    assert best.metrics.contact_mm2 == pytest.approx(400)
    assert best.normal == pytest.approx((0, 0, 1))
    assert cands[-1].metrics.shadow_mm2 > 0
    labels = [c.label for c in cands]
    assert labels[0] != "as is"
    assert "as is" in labels
    assert len({tuple(round(v, 3) for v in c.normal) for c in cands}) == len(cands)  # no duplicate orientations


def test_euler_round_trip():
    for angles in ((0, 0, 0), (90, 0, 0), (0, 90, 0), (0, 0, 90), (30, 45, 60), (-20, 10, 170)):
        m = M.euler_matrix(*angles)
        back = M.euler_matrix(*M.euler_angles(m))
        for i in range(3):
            for j in range(3):
                assert back[i][j] == pytest.approx(m[i][j], abs=1e-6)


def test_lay_on_face_opposite_direction():
    m = M.lay_on_face_matrix((0, 0, 1))
    assert M.apply(m, (0, 0, 1)) == pytest.approx((0, 0, -1))
    m = M.lay_on_face_matrix((1, 0, 0))
    assert M.apply(m, (1, 0, 0)) == pytest.approx((0, 0, -1))


def test_split_bodies_orders_largest_first():
    facets = box((0, 0, 0), (5, 5, 5)) + box((20, 0, 0), (40, 20, 20))
    bodies = M.split_bodies(facets)
    assert len(bodies) == 2
    assert abs(M.volume(bodies[0])) == pytest.approx(8000)
    assert abs(M.volume(bodies[1])) == pytest.approx(125)


# ---------------------------------------------------------------------------- STL i/o
def test_binary_round_trip(tmp_path):
    p = M.write_stl(tmp_path / "b.stl", box((0, 0, 0), (3, 2, 1)), "box")
    facets = M.read_stl(p)
    assert len(facets) == 12
    assert M.bbox(facets) == ((0, 0, 0), (3, 2, 1))
    assert M.volume(facets) == pytest.approx(6)


def test_ascii_read(tmp_path):
    text = "solid t\n"
    for a, b, c in box((0, 0, 0), (1, 1, 1)):
        text += "facet normal 0 0 0\nouter loop\n"
        text += "".join(f"vertex {v[0]} {v[1]} {v[2]}\n" for v in (a, b, c))
        text += "endloop\nendfacet\n"
    text += "endsolid t\n"
    p = tmp_path / "a.stl"
    p.write_text(text)
    assert M.volume(M.read_stl(p)) == pytest.approx(1)


def test_read_rejects_garbage(tmp_path):
    p = tmp_path / "x.stl"
    p.write_bytes(b"\x00" * 10)
    with pytest.raises(BuildError):
        M.read_stl(p)


def test_oriented_copy_writes_on_bed_mesh(tmp_path):
    src = M.write_stl(tmp_path / "table.stl", table())
    o = M.Orientation(lay_on_face=(0, 0, 1))
    out = M.oriented_copy(src, o, tmp_path / "oriented")
    assert out.parent == tmp_path / "oriented"
    assert out.name.startswith("table__face")
    lo, hi = M.bbox(M.read_stl(out))
    assert lo == pytest.approx((0, 0, 0))
    assert hi == pytest.approx((20, 20, 12))
    assert M.oriented_copy(src, M.Orientation(), tmp_path) == src  # identity: untouched


def test_orientation_json():
    o = M.Orientation.from_json({"lay_on_face": [0, 0, 1], "rotate": [0, 0, 45]})
    assert o.to_json() == {"lay_on_face": [0.0, 0.0, 1.0], "rotate": [0.0, 0.0, 45.0]}
    assert M.Orientation.from_json({}).is_identity
    with pytest.raises(BuildError):
        M.Orientation.from_json({"rotate": [1, 2]})


def test_no_flat_contact_sinks_to_the_bottom():
    """A band of small 45-degree chamfers is a large normal cluster; laid down, nothing lies flat on the bed."""
    facets = box((0, 0, 0), (40, 40, 10))
    # replace the top with a shallow pyramid: four 45-degree-ish faces with the largest total area
    apex = (20, 20, 30)
    facets = [f for f in facets if not all(v[2] == 10 for v in f)]
    for a, b in (((0, 0, 10), (40, 0, 10)), ((40, 0, 10), (40, 40, 10)), ((40, 40, 10), (0, 40, 10)), ((0, 40, 10), (0, 0, 10))):
        facets.append((a, b, apex))
    cands = M.candidates(facets)
    assert cands[0].label == "as is" and cands[0].metrics.stands
    tilted = [c for c in cands if not c.metrics.stands]
    assert tilted and all(c.metrics.score > M.NO_CONTACT_PENALTY for c in tilted)
    assert cands[-1].metrics.stands is False
    assert "NO FLAT CONTACT" in "\n".join(M.describe_candidates(cands))


# ---------------------------------------------------------------------------- batch 1 (real-STL test findings)
def wedge45():
    """A 20 x 10 x 10 wedge: a flat base, a back wall and a slope at exactly 45 degrees (facing up as modelled)."""
    a, b, c, d = (0, 0, 0), (20, 0, 0), (20, 10, 0), (0, 10, 0)  # bed face, y 0..10
    e, f = (0, 10, 10), (20, 10, 10)  # top edge above the back
    return [
        (a, c, b), (a, d, c),  # bottom
        (a, b, f), (a, f, e),  # sloped front face, normal (0, -1, 1): faces up, 45 degrees
        (d, e, f), (d, f, c),  # back wall
        (a, e, d), (b, c, f),  # sides
    ]  # fmt: skip


def test_exact_threshold_facet_is_printable():
    """A facet that faces down at exactly the threshold angle (Gridfinity's 45-degree foot) needs no support."""
    upside_down = M.transform(wedge45(), M.euler_matrix(180, 0, 0))  # the slope now faces down at 45
    lo, _ = M.bbox(upside_down)
    facets = M.translate(upside_down, (-lo[0], -lo[1], -lo[2]))
    slope = [f for f in facets if abs(M.facet_normal(f)[2] + math.sqrt(0.5)) < 1e-6]
    assert slope, "the wedge must have a 45-degree downward face"
    assert M.metrics(facets, threshold_deg=45).overhang_mm2 == pytest.approx(0)
    assert M.metrics(facets, threshold_deg=44).overhang_mm2 > 0  # one degree stricter: it counts


def test_slenderness_ranks_a_rod_lying_down_above_standing():
    rod = box((0, 0, 0), (6, 6, 204))  # a 6 mm rod, 204 mm tall as modelled
    standing = M.metrics(rod)
    assert standing.slenderness == pytest.approx(204 / 6)
    cands = M.candidates(rod)
    assert cands[0].metrics.height == pytest.approx(6)  # best: lying down
    as_is = next(c for c in cands if c.label == "as is")
    assert as_is.metrics.score > cands[0].metrics.score + 50 * (34 - 3) - 1  # the slenderness penalty dominates


def test_split_bodies_drops_an_inward_shell():
    cavity = [(a, c, b) for a, b, c in box((5, 2, 1), (10, 6, 3))]  # reversed winding: a void inside the box
    facets = box((0, 0, 0), (30, 10, 5)) + cavity
    bodies = M.split_bodies(facets)
    assert len(bodies) == 1 and len(bodies[0]) == 12 and M.volume(bodies[0]) == pytest.approx(1500)
    assert M.count_shells(facets) == 2 and M.cavities(facets) == 1
    assert M.cavities(box((0, 0, 0), (1, 1, 1))) == 0


def test_split_bodies_keeps_a_standalone_reversed_shell():
    """A closed cube with reversed winding and no enclosing body is a part, not a cavity: it comes back flipped."""
    inside_out = [(a, c, b) for a, b, c in box((0, 0, 0), (10, 10, 10))]
    bodies = M.split_bodies(inside_out)
    assert len(bodies) == 1 and M.volume(bodies[0]) == pytest.approx(1000)
    assert M.cavities(inside_out) == 0
    # beside (not inside) an outward box: still two bodies, no cavity
    facets = box((20, 0, 0), (30, 10, 10)) + inside_out
    assert len(M.split_bodies(facets)) == 2 and M.cavities(facets) == 0


def test_oriented_copies_same_stem_members_get_two_files(tmp_path):
    """A merged object with body/part.stl and label/part.stl: two output files, each with its own mesh."""
    (tmp_path / "body").mkdir()
    (tmp_path / "label").mkdir()
    a = M.write_stl(tmp_path / "body" / "part.stl", box((0, 0, 0), (10, 10, 10)))
    b = M.write_stl(tmp_path / "label" / "part.stl", box((0, 0, 10), (4, 4, 11)))
    ta, tb = M.oriented_copies([a, b], M.Orientation(rotate=(90, 0, 0)), tmp_path / "o")
    assert ta != tb and ta.is_file() and tb.is_file()
    assert M.volume(M.read_stl(ta)) == pytest.approx(1000) and M.volume(M.read_stl(tb)) == pytest.approx(16)
