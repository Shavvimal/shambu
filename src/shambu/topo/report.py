"""Read a density map: region tables, low-density clusters, solid skin next to passive regions, and iso-surfaces.

What it handles:

- `density_grid`: the per-cell densities as a float32 grid, NaN outside the domain (the `.npy` file `solve` writes).
- `region_rows`: per named region, the design cells, the mean density and the share under 0.3.
- `low_clusters`: face-connected groups of design cells under 0.3, largest first: where material works least.
- `skin_next_to_passive`: design cells at density 0.9 or more that touch a passive region: the map asking for more
  material around a fixed feature.
- `cube_facets`: the blocky iso-surface (the faces between kept and removed cells), numpy only.
- `smooth_facets`: a marching-cubes iso-surface (scikit-image, the `topo-mesh` extra), wound outward.

What it does not do: no plots, no CAD output, no smoothing of the blocky surface, no printing. The caller writes the
files. Needs numpy and scipy (the `topo` extra).
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage

from .voxel import VoxelDomain

LOW = 0.3  # "works little": the cut-off of the region table and the clusters
SKIN = 0.9  # "wants to be solid"


def density_grid(dom: VoxelDomain, rho: np.ndarray) -> np.ndarray:
    g = np.full(dom.shape, np.nan, dtype=np.float32)
    g[dom.mask] = rho.astype(np.float32)
    return g


def region_rows(rho: np.ndarray, design: np.ndarray, regions: dict[str, np.ndarray]) -> list[dict]:
    rows = []
    for name, m in regions.items():
        cells = m & design
        n = int(cells.sum())
        rows.append(
            {
                "name": name,
                "design_cells": n,
                "mean": round(float(rho[cells].mean()), 4) if n else None,
                "under_0_3": round(float((rho[cells] < LOW).mean()), 4) if n else None,
            }
        )
    return rows


def low_clusters(dom: VoxelDomain, rho: np.ndarray, design: np.ndarray, top: int = 10) -> list[dict]:
    g = np.zeros(dom.shape, dtype=bool)
    g[dom.mask] = design & (rho < LOW)
    labels, n = ndimage.label(g)
    if n == 0:
        return []
    sizes = np.bincount(labels.ravel())[1:]
    order = np.argsort(-sizes, kind="stable")[:top]
    rho3 = density_grid(dom, rho)
    out = []
    for i in order:
        idx = np.argwhere(labels == i + 1)
        centre = dom.origin + dom.pitch * (idx.mean(axis=0) + 0.5)
        out.append(
            {
                "cells": int(sizes[i]),
                "volume_mm3": round(float(sizes[i] * dom.pitch**3), 3),
                "centroid": [round(float(c), 3) for c in centre],
                "mean": round(float(rho3[labels == i + 1].mean()), 4),
            }
        )
    return out


def skin_next_to_passive(dom: VoxelDomain, rho: np.ndarray, passive_regions: dict[str, np.ndarray]) -> list[dict]:
    solid = np.zeros(dom.shape, dtype=bool)
    out = []
    all_passive = np.zeros(dom.shape, dtype=bool)
    for m in passive_regions.values():
        all_passive[dom.mask] |= m
    solid[dom.mask] = rho >= SKIN
    for name, m in passive_regions.items():
        reg = np.zeros(dom.shape, dtype=bool)
        reg[dom.mask] = m
        touch = ndimage.binary_dilation(reg) & solid & ~all_passive
        out.append({"name": name, "cells": int(touch.sum())})
    return out


def cube_facets(dom: VoxelDomain, rho: np.ndarray, iso: float = 0.5) -> list:
    """Two triangles per face between a kept cell (density >= iso) and a removed or outside one, wound outward."""
    keep = np.zeros(dom.shape, dtype=bool)
    keep[dom.mask] = rho >= iso
    pad = np.pad(keep, 1)
    h, o = dom.pitch, dom.origin
    tris = []
    for ax in range(3):
        for side in (-1, 1):
            nb = np.roll(pad, -side, axis=ax)
            face = (pad & ~nb)[1:-1, 1:-1, 1:-1]
            idx = np.argwhere(face).astype(float)
            if not len(idx):
                continue
            a, b = (ax + 1) % 3, (ax + 2) % 3
            base = idx.copy()
            base[:, ax] += 1.0 if side > 0 else 0.0
            ea, eb = np.zeros(3), np.zeros(3)
            ea[a], eb[b] = 1.0, 1.0
            p0 = base
            p1, p2, p3 = base + ea, base + ea + eb, base + eb
            if side < 0:  # outward normal -axis: reverse the winding
                p1, p3 = p3, p1
            tris.append(np.stack([p0, p1, p2], axis=1))
            tris.append(np.stack([p0, p2, p3], axis=1))
    if not tris:
        return []
    t = o + h * np.concatenate(tris)
    return [tuple(tuple(float(c) for c in v) for v in f) for f in t]


def smooth_facets(dom: VoxelDomain, rho: np.ndarray, iso: float = 0.5) -> list:
    from skimage import measure

    g = np.nan_to_num(density_grid(dom, rho), nan=0.0)
    if float(g.max()) < iso:
        return []
    verts, faces, _, _ = measure.marching_cubes(np.pad(g, 1), level=iso, spacing=(dom.pitch,) * 3)
    verts = dom.origin + verts - dom.pitch / 2  # padded index p is cell p - 1, whose centre is at (p - 0.5) pitch
    t = verts[faces]
    vol = np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6
    if vol < 0:
        t = t[:, ::-1]
    return [tuple(tuple(float(c) for c in v) for v in f) for f in t]
