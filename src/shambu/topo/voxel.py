"""The design domain: a regular grid of cubic cells that the part's mesh fills.

What it handles: `voxelise(facets, pitch, sub)` turns a closed STL (facets as `shambu.mesh.read_stl` gives them) into
a cell mask. Rays run along +z through an `sub x sub` lattice of sample columns per cell; the sorted hits of a
column pair into inside intervals by parity, and a cell is kept when at least half of its `sub^3` samples lie
inside. Ray origins carry a small fixed offset so they miss mesh edges and vertices. A column with an odd hit count
(an open mesh there) drops its last hit and is counted, for the `topo.odd_parity` finding. A hole whose facets lie
edge-on to +z (an open side wall) leaves every column even, so the welded mesh's open and non-manifold edges are
counted too (`topo.open_mesh`). The fill ignores facet winding; a mesh wound inward has a negative signed volume,
which is reported (`topo.inverted_mesh`) and compared by its size. `largest_piece` keeps
the largest face-connected piece. `box_domain` makes a solid block for a spec without an STL.

Cell (i, j, k) has its centre at `origin + pitch * (index + 0.5)`; arrays are indexed x, y, z.

What it does not do: no mesh repair (an open mesh only gets counted), no smoothing, no file I/O, no printing. Needs
numpy and scipy (the `topo` extra).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy import ndimage

from .spec import grid_shape

# Ray-origin offsets in units of the sample spacing: small, irrational-looking fractions that keep a sample column off
# the edges and vertices of meshes drawn on round numbers.
_JITTER = (1.234e-4, 2.345e-4)


@dataclass
class VoxelDomain:
    mask: np.ndarray  # bool, shape (nx, ny, nz)
    origin: np.ndarray  # (3,) the min corner of cell (0, 0, 0)
    pitch: float
    mesh_volume_mm3: float | None = None  # the size of the signed volume; `inverted` says when the sign was negative
    odd_columns: int = 0
    open_edges: int = 0  # welded edges used by one facet or by more than two
    inverted: bool = False
    pieces: int = 1
    dropped: int = 0

    @property
    def shape(self) -> tuple[int, int, int]:
        nx, ny, nz = self.mask.shape
        return nx, ny, nz

    @property
    def cells(self) -> int:
        return int(self.mask.sum())

    @property
    def voxel_volume_mm3(self) -> float:
        return self.cells * self.pitch**3

    def centres(self) -> np.ndarray:
        """Centres of the domain cells, in C order of the mask (the element order everywhere)."""
        idx = np.argwhere(self.mask)
        return self.origin + self.pitch * (idx + 0.5)

    def stats(self) -> dict:
        d: dict = {
            "cells": self.cells,
            "voxel_volume_mm3": round(self.voxel_volume_mm3, 3),
            "mesh_volume_mm3": None if self.mesh_volume_mm3 is None else round(self.mesh_volume_mm3, 3),
            "volume_ratio": None,
            "pieces": self.pieces,
            "dropped": self.dropped,
            "odd_columns": self.odd_columns,
            "open_edges": self.open_edges,
            "inverted": self.inverted,
        }
        if self.mesh_volume_mm3:
            d["volume_ratio"] = round(self.voxel_volume_mm3 / self.mesh_volume_mm3, 4)
        return d

    def grid_json(self) -> dict:
        return {
            "origin": [round(float(x), 6) for x in self.origin],
            "pitch": self.pitch,
            "shape": list(self.shape),
            "axis_order": "x,y,z",
            "cell_centre": "origin + pitch * (index + 0.5)",
            "nan": "outside the domain",
        }


def box_domain(lo: Sequence[float], hi: Sequence[float], pitch: float) -> VoxelDomain:
    """A solid block from `lo`; the far faces round up to whole cells."""
    lo_a = np.asarray(lo, dtype=float)
    span = np.asarray(hi, dtype=float) - lo_a
    shape = grid_shape((float(span[0]), float(span[1]), float(span[2])), pitch)
    return VoxelDomain(np.ones(shape, dtype=bool), lo_a, pitch)


def voxelise(facets: Sequence, pitch: float, sub: int = 3) -> VoxelDomain:
    """The cells of a closed mesh (see the module docstring)."""
    tri = np.asarray(facets, dtype=float).reshape(-1, 3, 3)
    if len(tri) == 0:
        raise ValueError("the mesh has no facets")
    lo, hi = tri.reshape(-1, 3).min(axis=0), tri.reshape(-1, 3).max(axis=0)
    shape = grid_shape((float(hi[0] - lo[0]), float(hi[1] - lo[1]), float(hi[2] - lo[2])), pitch)
    nx, ny, nz = shape
    ds = pitch / sub
    # Sample column positions (x, y) and sample heights z.
    xs = lo[0] + ds * (np.arange(nx * sub) + 0.5 + _JITTER[0])
    ys = lo[1] + ds * (np.arange(ny * sub) + 0.5 + _JITTER[1])
    zs = lo[2] + ds * (np.arange(nz * sub) + 0.5)
    ncol_y = len(ys)

    cols: list[np.ndarray] = []
    hz: list[np.ndarray] = []
    for a, b, c in tri:
        x0, x1 = min(a[0], b[0], c[0]), max(a[0], b[0], c[0])
        y0, y1 = min(a[1], b[1], c[1]), max(a[1], b[1], c[1])
        i0, i1 = np.searchsorted(xs, x0), np.searchsorted(xs, x1, side="right")
        j0, j1 = np.searchsorted(ys, y0), np.searchsorted(ys, y1, side="right")
        if i0 >= i1 or j0 >= j1:
            continue
        den = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(den) < 1e-18:
            continue  # a facet seen edge-on from +z: no ray crosses it
        px, py = np.meshgrid(xs[i0:i1], ys[j0:j1], indexing="ij")
        l1 = ((b[1] - c[1]) * (px - c[0]) + (c[0] - b[0]) * (py - c[1])) / den
        l2 = ((c[1] - a[1]) * (px - c[0]) + (a[0] - c[0]) * (py - c[1])) / den
        l3 = 1.0 - l1 - l2
        inside = (l1 >= 0) & (l2 >= 0) & (l3 >= 0)
        if not inside.any():
            continue
        ii, jj = np.nonzero(inside)
        z = l1[inside] * a[2] + l2[inside] * b[2] + l3[inside] * c[2]
        cols.append((ii + i0) * ncol_y + (jj + j0))
        hz.append(z)

    inside_count = np.zeros((nx * sub, ny * sub, nz * sub), dtype=bool)
    odd = 0
    if cols:
        col = np.concatenate(cols)
        z = np.concatenate(hz)
        order = np.lexsort((z, col))
        col, z = col[order], z[order]
        starts = np.flatnonzero(np.r_[True, col[1:] != col[:-1]])
        counts = np.diff(np.r_[starts, len(col)])
        odd_cols = counts % 2 == 1
        odd = int(odd_cols.sum())
        # Pair hits by parity; an odd column drops its last hit (the mesh is open there).
        keep = np.ones(len(col), dtype=bool)
        keep[(starts + counts - 1)[odd_cols]] = False
        col, z = col[keep], z[keep]
        starts = np.flatnonzero(np.r_[True, col[1:] != col[:-1]]) if len(col) else np.zeros(0, dtype=int)
        for s, e in zip(starts, np.r_[starts[1:], len(col)], strict=True):
            c = int(col[s])
            hits = z[s:e]
            n_below = np.searchsorted(hits, zs)
            ci, cj = divmod(c, ncol_y)
            inside_count[ci, cj, :] = n_below % 2 == 1
    sub3 = inside_count.reshape(nx, sub, ny, sub, nz, sub).sum(axis=(1, 3, 5))
    mask = sub3 * 2 >= sub**3
    from ..mesh import volume as mesh_volume
    from ..meshgeom import weld

    welded = weld(facets)
    bad_edges = len(welded.open_edges()) + len(welded.nonmanifold_edges())
    signed = float(mesh_volume(facets))
    dom = VoxelDomain(mask, lo.copy(), pitch, abs(signed), odd, open_edges=bad_edges, inverted=signed < 0)
    return largest_piece(dom)


def largest_piece(dom: VoxelDomain) -> VoxelDomain:
    """Keep the largest face-connected piece; count the pieces and the cells dropped."""
    labels, n = ndimage.label(dom.mask)
    dom.pieces = int(n)
    if n > 1:
        sizes = np.bincount(labels.ravel())[1:]
        keep = int(np.argmax(sizes)) + 1
        new = labels == keep
        dom.dropped = int(dom.mask.sum() - new.sum())
        dom.mask = new
    return dom


def surface_depth_share(mask: np.ndarray, design: np.ndarray, depth: int = 3) -> float:
    """The share of design cells less than `depth` cells from the domain surface (face neighbours)."""
    if not design.any():
        return 0.0
    eroded = ndimage.binary_erosion(np.pad(mask, 1), iterations=depth)[1:-1, 1:-1, 1:-1]
    return float((design & ~eroded).sum() / design.sum())
