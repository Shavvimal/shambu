"""Linear finite elements on a voxel grid: one 8-node brick (hex8) per domain cell.

What it handles: `hex8_ke(nu)`, the 24 x 24 stiffness of a unit cube with E = 1 by 2 x 2 x 2 Gauss quadrature (the
matrix of a cube of side h and modulus E is `E * h * ke`); `VoxelFE`, which numbers the nodes the cells use, maps each
cell to its 24 degrees of freedom (3 per node, x, y, z), fixes supported DOFs, assembles the free-by-free stiffness
for a vector of cell moduli through a sparsity pattern built once, and gives element energies and reactions.

Local node order of a cell: (0,0,0), (1,0,0), (1,1,0), (0,1,0), (0,0,1), (1,0,1), (1,1,1), (0,1,1) in (x, y, z) steps.

What it does not do: no solve (linsolve.py), no loads (loads.py), no nonlinearity, no stress output, no file I/O and
no printing. Needs numpy and scipy (the `topo` extra).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import sparse

CORNERS = np.array([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)], dtype=int)


def elasticity(nu: float) -> np.ndarray:
    """The 6 x 6 isotropic elasticity matrix for E = 1 (engineering shear strains: xx, yy, zz, xy, yz, zx)."""
    lam = nu / ((1 + nu) * (1 - 2 * nu))
    mu = 1 / (2 * (1 + nu))
    d = np.zeros((6, 6))
    d[:3, :3] = lam
    d[np.arange(3), np.arange(3)] = lam + 2 * mu
    d[np.arange(3, 6), np.arange(3, 6)] = mu
    return d


def hex8_ke(nu: float) -> np.ndarray:
    d = elasticity(nu)
    g = (1 - 1 / np.sqrt(3)) / 2, (1 + 1 / np.sqrt(3)) / 2  # Gauss points on [0, 1]
    ke = np.zeros((24, 24))
    for x in g:
        for y in g:
            for z in g:
                p = np.array([x, y, z])
                grad = np.zeros((8, 3))
                for a, c in enumerate(CORNERS):
                    f = np.where(c == 1, p, 1 - p)  # the three 1D factors of the shape function
                    s = np.where(c == 1, 1.0, -1.0)  # their derivatives
                    grad[a] = (s[0] * f[1] * f[2], f[0] * s[1] * f[2], f[0] * f[1] * s[2])
                b = np.zeros((6, 24))
                for a in range(8):
                    dx, dy, dz = grad[a]
                    k = 3 * a
                    b[0, k], b[1, k + 1], b[2, k + 2] = dx, dy, dz
                    b[3, k], b[3, k + 1] = dy, dx
                    b[4, k + 1], b[4, k + 2] = dz, dy
                    b[5, k], b[5, k + 2] = dz, dx
                ke += b.T @ d @ b / 8  # weight 1/8: eight points over a unit volume
    return (ke + ke.T) / 2


@dataclass
class VoxelFE:
    """The finite element model of a cell mask with side `pitch` (mm)."""

    mask: np.ndarray
    pitch: float
    nu: float
    ke: np.ndarray = field(init=False, repr=False)
    edof: np.ndarray = field(init=False, repr=False)  # (ne, 24) global DOF of each element
    node_xyz_index: np.ndarray = field(init=False, repr=False)  # (nn, 3) grid index of each used node
    fixed: np.ndarray = field(init=False, repr=False)
    free: np.ndarray = field(init=False, repr=False)
    _pattern: tuple | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:  # pure numbering on arrays in memory: no I/O
        self.ke = hex8_ke(self.nu)
        _, ny, nz = self.mask.shape
        cells = np.argwhere(self.mask)
        grid_nodes = (cells[:, None, :] + CORNERS[None, :, :]).reshape(-1, 3)
        key = (grid_nodes[:, 0] * (ny + 1) + grid_nodes[:, 1]) * (nz + 1) + grid_nodes[:, 2]
        uniq, inv = np.unique(key, return_inverse=True)
        self.node_xyz_index = np.stack([uniq // ((ny + 1) * (nz + 1)), (uniq // (nz + 1)) % (ny + 1), uniq % (nz + 1)], axis=1)
        enode = inv.reshape(-1, 8)
        self.edof = (3 * enode[:, :, None] + np.arange(3)[None, None, :]).reshape(-1, 24)
        # the sparsity pattern (576 entries a cell) waits for set_fixed, so a caller can check the DOF count first
        self.fixed = np.zeros(0, dtype=np.int64)
        self.free = np.arange(self.n_dof)

    @property
    def n_elements(self) -> int:
        return len(self.edof)

    @property
    def n_nodes(self) -> int:
        return len(self.node_xyz_index)

    @property
    def n_dof(self) -> int:
        return 3 * self.n_nodes

    def node_coords(self, origin: np.ndarray) -> np.ndarray:
        return origin + self.pitch * self.node_xyz_index

    def element_nodes(self) -> np.ndarray:
        """(ne, 8) node index of each element."""
        return self.edof[:, ::3] // 3

    def set_fixed(self, dofs: np.ndarray) -> None:
        """Fix these DOFs at zero and build the free-by-free sparsity pattern once."""
        self.fixed = np.unique(np.asarray(dofs, dtype=np.int64))
        is_free = np.ones(self.n_dof, dtype=bool)
        is_free[self.fixed] = False
        self.free = np.flatnonzero(is_free)
        to_free = np.full(self.n_dof, -1, dtype=np.int64)
        to_free[self.free] = np.arange(len(self.free))
        fe = to_free[self.edof]  # (ne, 24)
        r = np.repeat(fe, 24, axis=1).ravel()
        c = np.tile(fe, (1, 24)).ravel()
        ok = (r >= 0) & (c >= 0)
        nf = len(self.free)
        key = r[ok] * nf + c[ok]
        uniq, inv = np.unique(key, return_inverse=True)
        rows, cols = uniq // nf, uniq % nf
        indptr = np.searchsorted(rows, np.arange(nf + 1))
        self._pattern = (ok, inv, cols.astype(np.int64), indptr.astype(np.int64), nf, len(uniq))

    def assemble(self, modulus: np.ndarray) -> sparse.csc_matrix:
        """The free-by-free stiffness for per-element moduli (MPa). Symmetric, so its CSR arrays read as CSC."""
        if self._pattern is None:
            raise ValueError("set_fixed must run before assemble")
        ok, inv, cols, indptr, nf, nnz = self._pattern
        vals = (modulus[:, None] * (self.pitch * self.ke).ravel()[None, :]).ravel()[ok]
        data = np.bincount(inv, weights=vals, minlength=nnz)
        return sparse.csc_matrix((data, cols, indptr), shape=(nf, nf))

    def element_energy(self, u: np.ndarray) -> np.ndarray:
        """u_e^T (h ke) u_e per element for each column of u (n_dof x n_cases): (ne, n_cases), at E = 1."""
        ue = u[self.edof]  # (ne, 24, nc)
        return np.einsum("eic,ij,ejc->ec", ue, self.pitch * self.ke, ue)

    def internal_force(self, modulus: np.ndarray, u: np.ndarray) -> np.ndarray:
        """K u over every DOF (fixed ones included), for one displacement vector."""
        ue = u[self.edof]  # (ne, 24)
        fe = modulus[:, None] * (ue @ (self.pitch * self.ke))
        return np.bincount(self.edof.ravel(), weights=fe.ravel(), minlength=self.n_dof)
