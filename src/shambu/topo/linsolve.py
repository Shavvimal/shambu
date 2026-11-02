"""Linear solvers for the free-by-free stiffness system: factor once per iteration, solve every load case from it.

What it handles: the `LinearSolver` Protocol and two implementations. `SuperLUSolver` (scipy's sparse LU with a
symmetric ordering) is the default; `DenseSolver` (numpy's dense solve) is the small-grid reference the tests compare
against. A singular matrix (a mechanism: a part not held in some direction) raises TopoError.

What it does not do: no iterative solvers, no assembly, no file I/O, no printing. Needs numpy and scipy (the `topo`
extra).
"""

from __future__ import annotations

from typing import Protocol

import numpy as np
from scipy import sparse
from scipy.sparse import linalg as spla

from ..errors import TopoError


class LinearSolver(Protocol):
    name: str

    def solve(self, k: sparse.csc_matrix, b: np.ndarray) -> np.ndarray:
        """x with k x = b; b is (n,) or (n, cases)."""
        ...


_SINGULAR = "the stiffness matrix is singular: the supports leave the part free to move or turn in some direction"


class SuperLUSolver:
    name = "superlu"

    def solve(self, k: sparse.csc_matrix, b: np.ndarray) -> np.ndarray:
        try:
            lu = spla.splu(k, permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0.0, options={"SymmetricMode": True})
        except RuntimeError as e:  # "Factor is exactly singular"
            raise TopoError(f"{_SINGULAR} ({e})") from e
        x = lu.solve(np.asarray(b, dtype=float))
        if not np.all(np.isfinite(x)):
            raise TopoError(_SINGULAR)
        return x


class DenseSolver:
    name = "dense"

    def solve(self, k: sparse.csc_matrix, b: np.ndarray) -> np.ndarray:
        try:
            return np.linalg.solve(k.toarray(), np.asarray(b, dtype=float))
        except np.linalg.LinAlgError as e:
            raise TopoError(f"{_SINGULAR} ({e})") from e
