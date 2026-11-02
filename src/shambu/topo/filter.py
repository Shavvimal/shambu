"""The density filter: each cell's physical density is a cone-weighted mean of the design variables around it.

    H_ej = max(0, rmin - |x_e - x_j|)      rho_e = sum_j H_ej x_j / sum_j H_ej

`DensityFilter.forward` maps design variables to physical densities; `backward` maps a gradient with respect to the
physical densities back to the design variables (the chain rule through the same weights). A radius at or below the
pitch gives the identity, which lets checkerboards through (the `topo.filter_small` finding says so).

What it does not do: no sensitivity filter, no projection (Heaviside), no file I/O, no printing. Needs numpy and scipy
(the `topo` extra).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import sparse
from scipy.spatial import cKDTree


@dataclass
class DensityFilter:
    h: sparse.csr_matrix  # symmetric weights
    hs: np.ndarray  # row sums

    @classmethod
    def build(cls, centres: np.ndarray, rmin: float) -> DensityFilter:
        n = len(centres)
        pairs = cKDTree(centres).query_pairs(rmin, output_type="ndarray")
        if len(pairs):
            w = rmin - np.linalg.norm(centres[pairs[:, 0]] - centres[pairs[:, 1]], axis=1)
            keep = w > 0
            pairs, w = pairs[keep], w[keep]
        else:
            w = np.zeros(0)
        rows = np.concatenate([pairs[:, 0], pairs[:, 1], np.arange(n)])
        cols = np.concatenate([pairs[:, 1], pairs[:, 0], np.arange(n)])
        vals = np.concatenate([w, w, np.full(n, rmin)])  # the diagonal: distance 0
        h = sparse.csr_matrix((vals, (rows, cols)), shape=(n, n))
        return cls(h, np.asarray(h.sum(axis=1)).ravel())

    def forward(self, x: np.ndarray) -> np.ndarray:
        return (self.h @ x) / self.hs

    def backward(self, g: np.ndarray) -> np.ndarray:
        return self.h @ (g / self.hs)
