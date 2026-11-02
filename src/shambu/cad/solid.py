"""`shambu cad check`: one exported part judged as a solid, and its volume, size and mass against the numbers the CAD
session reported (the record), the STEP re-import and the spec.

Areas (each with PR 1's status: attention, no-finding, not-checked plus a reason):

- `solid`: open and non-manifold edges (`solid.open` FAIL), more than one outer shell (`solid.bodies` FAIL),
  facets wound the wrong way (`solid.flipped` WARN), an inward shell, which is an enclosed void (`solid.void` INFO),
  a mesh in metres (`mesh.metres` FAIL, the printability rule);
- `volume`: the STL against the CAD volume, the STEP re-import against the CAD volume, and the spec's expectation;
- `size`: the bounding box against the spec's expected size;
- `mass`: grams at the intent's density (INFO; the slicer's grams are the real number) and the centre of mass
  against the CAD's.

Tolerances, derived rather than copied:

- STL against CAD: every STL vertex lies on the CAD surface and every facet is within the faceting tolerance `t` of
  it, so |V_mesh - V_cad| <= A_mesh x t. The band is max(A_mesh x t, 5e-4 x V_cad). 5e-4 (0.05 %) is the
  kernel-integration band ClassCAD's own verification recipe gives for mass properties (curved solids deviate by up
  to about 0.02 %). With no faceting tolerance in the record, the export default of 0.1 mm is assumed and named in
  `expected_from`.
- CAD against the STEP re-import: 5e-4 x V_cad alone (both are B-rep integrals).
- Spec expectation: the spec's own `volume_tol_mm3` and `size_tol_mm`, required beside the value (no hidden default).
- Centre of mass: the shell between the facets and the CAD surface holds at most A x t of volume, anywhere within
  the part's extent, so the centroid moves at most A x t x D / V (D the box diagonal).

What it does not do: no mesh repair, no file reading (load.py), no printing.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from .. import meshgeom as G
from ..findings import FAIL, INFO, MEASURED, WARN, Finding, Fix
from ..mesh import bbox, looks_like_metres
from ..printability import ATTENTION, NO_FINDING, NOT_CHECKED_STATUS, AreaResult
from .linalg import Facet, Vec

KERNEL_REL_TOL = 5e-4  # ClassCAD verification recipe: mass-property integration band (see the module docstring)
DEFAULT_FACETING_MM = 0.1  # ClassCAD's documented STL export default (facetingTol)
AREAS = ("provenance", "solid", "volume", "size", "mass")


def result(measures: dict, findings: list[Finding], reason: str | None = None) -> AreaResult:
    status = ATTENTION if any(f.severity in (FAIL, WARN) for f in findings) else NO_FINDING
    return AreaResult(status, measures, reason, findings)


def skip(reason: str) -> AreaResult:
    return AreaResult(NOT_CHECKED_STATUS, {}, reason, [])


def surface_area(facets: Sequence[Facet]) -> float:
    total = 0.0
    for a, b, c in facets:
        n = G._cross(G._sub(b, a), G._sub(c, a))
        total += math.sqrt(G._dot(n, n)) / 2
    return total


@dataclass(frozen=True)
class Shells:
    outer: int
    inward: int
    open_edges: int
    nonmanifold_edges: int
    flipped_edges: int

    @property
    def closed(self) -> bool:
        return self.open_edges == 0 and self.nonmanifold_edges == 0


def shells(facets: Sequence[Facet]) -> Shells:
    """Connected shells by welded edges; a shell with a negative signed volume faces inwards (a void)."""
    w = G.weld(facets)
    outer = inward = 0
    for reg in G.regions(range(len(facets)), w):
        v, _ = G.moments([facets[i] for i in reg])
        if v > 0:
            outer += 1
        elif v < 0:
            inward += 1
    return Shells(outer, inward, len(w.open_edges()), len(w.nonmanifold_edges()), w.flipped_edges())


def facts(facets: Sequence[Facet]) -> dict:
    """The mesh facts a record keeps: facets, volume, area, box, bodies and open edges."""
    vol, _ = G.moments(facets)
    lo, hi = bbox(facets)
    sh = shells(facets)
    return {
        "facets": len(facets),
        "volume_mm3": round(vol, 3),
        "area_mm2": round(surface_area(facets), 3),
        "bbox": [[round(x, 4) for x in lo], [round(x, 4) for x in hi]],
        "bodies": sh.outer,
        "open_edges": sh.open_edges,
    }


@dataclass(frozen=True)
class Expect:
    """What the part should measure, from the flags or the spec part. `None` means not given."""

    volume_mm3: float | None = None
    volume_tol_mm3: float | None = None
    size_mm: Vec | None = None
    size_tol_mm: float | None = None
    density_g_cm3: float | None = None
    source: str = "flag"  # "flag" or "spec"


def check(name: str, facets: Sequence[Facet], record: dict | None, expect: Expect) -> dict[str, AreaResult]:
    """The solid, volume, size and mass areas of one part. `record` is the record's JSON (or None)."""
    areas: dict[str, AreaResult] = {}
    sh = shells(facets)
    fs: list[Finding] = []
    vol, com = G.moments(facets)
    lo, hi = bbox(facets)
    if not sh.closed:
        fs.append(
            Finding(
                "solid.open",
                FAIL,
                "solid",
                f"{sh.open_edges} open and {sh.nonmanifold_edges} non-manifold edges: not one closed solid",
                MEASURED,
                name,
                measure={"open_edges": sh.open_edges, "nonmanifold_edges": sh.nonmanifold_edges},
                fix=Fix("fix the model in CAD (a failed Boolean or a surface body) and re-export; shambu does not repair meshes"),
            )
        )
    if sh.outer > 1:
        fs.append(
            Finding(
                "solid.bodies",
                FAIL,
                "solid",
                f"{sh.outer} separate bodies: a part must be one solid",
                MEASURED,
                name,
                measure={"bodies": sh.outer},
                fix=Fix("unite the bodies in CAD, or export each as its own part"),
            )
        )
    if sh.flipped_edges:
        fs.append(
            Finding(
                "solid.flipped",
                WARN,
                "solid",
                f"{sh.flipped_edges} edges run the same way in two facets: some facets face inwards",
                MEASURED,
                name,
                measure={"flipped_edges": sh.flipped_edges},
                fix=Fix("re-export from CAD"),
            )
        )
    if sh.inward:
        fs.append(
            Finding(
                "solid.void",
                INFO,
                "solid",
                f"{sh.inward} inward shell(s): an enclosed void inside the part (legal, and the slicer fills around it)",
                MEASURED,
                name,
                measure={"inward_shells": sh.inward},
            )
        )
    if looks_like_metres(facets):
        fs.append(
            Finding(
                "mesh.metres",
                FAIL,
                "solid",
                "the whole mesh is under 1 mm: it is probably in metres",
                MEASURED,
                name,
                measure={"size_mm": [round(hi[i] - lo[i], 4) for i in range(3)]},
                fix=Fix("export in millimetres"),
            )
        )
    areas["solid"] = result(
        {
            "bodies": sh.outer,
            "open_edges": sh.open_edges,
            "nonmanifold_edges": sh.nonmanifold_edges,
            "flipped_edges": sh.flipped_edges,
            "inward_shells": sh.inward,
            "watertight": sh.closed and not sh.flipped_edges,
            "volume_mm3": round(vol, 3),
        },
        fs,
    )

    area = surface_area(facets)
    rec = record or {}

    def section(key: str) -> dict:
        v = rec.get(key)
        return v if isinstance(v, dict) else {}

    stl_rec, cad, step = section("stl"), section("cad"), section("step")
    tol_mm = stl_rec.get("faceting_tol_mm")
    tol_from = "record"
    if tol_mm is None:
        tol_mm, tol_from = DEFAULT_FACETING_MM, f"default {DEFAULT_FACETING_MM} mm faceting (not in the record)"
    tol_mm = float(tol_mm)
    vf: list[Finding] = []
    vm: dict = {"mesh_mm3": round(vol, 3), "area_mm2": round(area, 3), "faceting_tol_mm": tol_mm, "faceting_from": tol_from}
    if vol <= 0:
        vf.append(
            Finding(
                "volume.nonpositive",
                FAIL,
                "volume",
                f"signed volume {vol:.3f} mm3: the mesh is inside out or not closed",
                MEASURED,
                name,
                measure={"mesh_mm3": vol},
                fix=Fix("re-export a closed, outward-facing solid"),
            )
        )
    v_cad = cad.get("volume_mm3")
    if v_cad is not None:
        v_cad = float(v_cad)
        band = max(area * tol_mm, KERNEL_REL_TOL * abs(v_cad))
        vm.update(
            {
                "cad_mm3": v_cad,
                "tolerance_mm3": round(band, 3),
                "expected_from": f"record (CAD {cad.get('from', 'mass properties')}); band max(A x {tol_mm} mm, 0.05 %)",
            }
        )
        if abs(vol - v_cad) > band:
            vf.append(
                Finding(
                    "volume.vs_cad",
                    FAIL,
                    "volume",
                    f"STL volume {vol:.1f} mm3 differs from the CAD's {v_cad:.1f} mm3 by {vol - v_cad:+.1f} mm3 (band {band:.1f})",
                    MEASURED,
                    name,
                    measure={"mesh_mm3": vol, "cad_mm3": v_cad, "delta_mm3": vol - v_cad, "tolerance_mm3": band, "expected_from": "record"},
                    fix=Fix("the STL is not the solid the CAD measured: re-export it, then stamp again"),
                )
            )
    v_step = step.get("reimport_volume_mm3")
    if v_step is not None:
        v_step = float(v_step)
        ref, ref_name = (v_cad, "CAD") if v_cad is not None else (vol, "STL")
        band = KERNEL_REL_TOL * abs(ref) if v_cad is not None else max(area * tol_mm, KERNEL_REL_TOL * abs(vol))
        vm.update({"step_mm3": v_step, "step_tolerance_mm3": round(band, 3)})
        if abs(v_step - ref) > band:
            vf.append(
                Finding(
                    "volume.vs_step",
                    FAIL,
                    "volume",
                    f"the STEP re-imports at {v_step:.1f} mm3, the {ref_name} says {ref:.1f} mm3 ({v_step - ref:+.1f}, band {band:.1f})",
                    MEASURED,
                    name,
                    measure={"step_mm3": v_step, "reference_mm3": ref, "tolerance_mm3": band, "expected_from": "record"},
                    fix=Fix("the STEP lost or changed geometry on export: export it again and re-import it before stamping"),
                )
            )
    if expect.volume_mm3 is not None:
        tol = float(expect.volume_tol_mm3 or 0.0)
        vm.update({"expected_mm3": expect.volume_mm3, "expected_tolerance_mm3": tol, "expected_volume_from": expect.source})
        if abs(vol - expect.volume_mm3) > tol:
            vf.append(
                Finding(
                    "volume.vs_expected",
                    FAIL,
                    "volume",
                    f"volume {vol:.1f} mm3, expected {expect.volume_mm3:.1f} +/- {tol:g} mm3",
                    MEASURED,
                    name,
                    measure={"mesh_mm3": vol, "expected_mm3": expect.volume_mm3, "tolerance_mm3": tol, "expected_from": expect.source},
                    fix=Fix("compare the parameter table with the model; a missing or extra cut shows here first"),
                )
            )
    areas["volume"] = result(vm, vf)

    size = (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])
    if expect.size_mm is None:
        areas["size"] = AreaResult(NOT_CHECKED_STATUS, {"bbox_mm": [round(x, 4) for x in size]}, "no expected size", [])
    else:
        tol = float(expect.size_tol_mm or 0.0)
        off = [size[i] - expect.size_mm[i] for i in range(3)]
        sf: list[Finding] = []
        if any(abs(d) > tol for d in off):
            sf.append(
                Finding(
                    "size.vs_expected",
                    FAIL,
                    "size",
                    "box "
                    + " x ".join(f"{x:.3f}" for x in size)
                    + " mm, expected "
                    + " x ".join(f"{x:g}" for x in expect.size_mm)
                    + f" +/- {tol:g}",
                    MEASURED,
                    name,
                    measure={
                        "bbox_mm": list(size),
                        "expected_mm": list(expect.size_mm),
                        "delta_mm": off,
                        "tolerance_mm": tol,
                        "expected_from": expect.source,
                    },
                    fix=Fix("check the parameter table against the model's outer dimensions"),
                )
            )
        areas["size"] = result({"bbox_mm": [round(x, 4) for x in size], "expected_mm": list(expect.size_mm), "tolerance_mm": tol}, sf)

    mm: dict = {"cog_mm": [round(x, 4) for x in com]}
    mf: list[Finding] = []
    if expect.density_g_cm3 is not None:
        mm.update({"density_g_cm3": expect.density_g_cm3, "mass_g_solid": round(vol * expect.density_g_cm3 / 1000, 2)})
    cog = cad.get("cog")
    if isinstance(cog, list) and len(cog) == 3 and vol > 0:
        diag = math.dist(lo, hi)
        tol = area * tol_mm * diag / vol
        d = math.dist(com, tuple(float(x) for x in cog))
        mm.update({"cog_vs_cad_mm": round(d, 4), "cog_tolerance_mm": round(tol, 4)})
        if d > tol:
            mf.append(
                Finding(
                    "mass.cog_vs_cad",
                    WARN,
                    "mass",
                    f"centre of mass {d:.3f} mm from the CAD's (bound {tol:.3f} mm)",
                    MEASURED,
                    name,
                    measure={"cog_mm": list(com), "cad_cog_mm": cog, "distance_mm": d, "tolerance_mm": tol, "expected_from": "record"},
                    fix=Fix("the STL and the CAD solid differ in where the material is: re-export and stamp again"),
                )
            )
    if "mass_g_solid" in mm or "cog_vs_cad_mm" in mm:
        areas["mass"] = result(mm, mf)
    else:
        areas["mass"] = AreaResult(NOT_CHECKED_STATUS, mm, "no density and no CAD centre of mass", [])
    return areas
