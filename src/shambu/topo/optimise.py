"""SIMP density optimisation: minimise the weighted compliance of several load cases at a fixed design volume.

    E(rho)  = Emin + rho^p (E0 - Emin)
    c_k     = F_k^T U_k                         J = sum_k w_k c_k
    dc_k/drho_e = -p rho_e^(p-1) (E0 - Emin) u_e^T k0 u_e  +  2 u_e^T f_b    (the second term only for self-weight
                                                                               that scales with density)
    x_new   = clip(x (-dJ/dx / (lambda dV/dx))^eta, x - move, x + move, 0, 1), lambda by bisection on the volume

`rho` is the physical density (the filtered design variable `x`; passive cells are 1). Weights are either given
(`raw`) or `(1/n) / c_k(full density)` (`normalised`), so every case counts the same at the start. A positive
sensitivity (where self-weight makes added mass hurt) is clipped to a tiny negative value, because the update needs
negative ones; the count is reported (`topo.sens_clipped`).

What it handles: `Problem` (the model at one pitch: analysis, objective, gradient, volume) and `optimise`, which runs
the loop and returns a `RunResult`. Progress goes to an optional `Listener` callback, never to stdout.

What it does not do: no stress or buckling constraints, no overhang filter, no projection, no file I/O, no printing.
Needs numpy and scipy (the `topo` extra).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .fe import VoxelFE
from .filter import DensityFilter
from .linsolve import LinearSolver
from .loads import CaseLoads, body_load

Listener = Callable[[str, dict], None]
CLIP = -1e-30  # the largest sensitivity the update accepts (it needs negative values)


@dataclass
class Analysis:
    u: np.ndarray  # (n_dof, n_cases)
    f: np.ndarray  # (n_dof, n_cases) the loads, self-weight at this density included
    compliance: np.ndarray  # (n_cases,)
    energy: np.ndarray  # (ne, n_cases) u_e^T (h k0) u_e at E = 1


@dataclass
class Problem:
    fe: VoxelFE
    filt: DensityFilter
    cases: list[CaseLoads]
    passive: np.ndarray  # (ne,) bool
    e0: float
    emin_rel: float
    penal: float
    solver: LinearSolver
    weights: np.ndarray = field(default_factory=lambda: np.zeros(0))

    @property
    def emin(self) -> float:
        return self.e0 * self.emin_rel

    @property
    def design(self) -> np.ndarray:
        return ~self.passive

    def physical(self, x: np.ndarray) -> np.ndarray:
        rho = self.filt.forward(x)
        rho[self.passive] = 1.0
        return rho

    def modulus(self, rho: np.ndarray) -> np.ndarray:
        return self.emin + rho**self.penal * (self.e0 - self.emin)

    def loads(self, rho: np.ndarray) -> np.ndarray:
        f = np.stack([c.f0 for c in self.cases], axis=1)
        for k, c in enumerate(self.cases):
            if c.fb is not None:
                f[:, k] = f[:, k] + body_load(self.fe, c.fb, rho)
        return f

    def analyse(self, rho: np.ndarray, timings: dict | None = None) -> Analysis:
        t0 = time.perf_counter()
        k = self.fe.assemble(self.modulus(rho))
        t1 = time.perf_counter()
        f = self.loads(rho)
        u = np.zeros_like(f)
        free = self.fe.free
        uf = self.solver.solve(k, f[free])
        u[free] = uf.reshape(len(free), -1)
        t2 = time.perf_counter()
        comp = np.einsum("ik,ik->k", f[free], u[free])
        energy = self.fe.element_energy(u)
        if timings is not None:
            timings["assemble_s"] = timings.get("assemble_s", 0.0) + t1 - t0
            timings["solve_s"] = timings.get("solve_s", 0.0) + t2 - t1
        return Analysis(u, f, comp, energy)

    def objective(self, x: np.ndarray) -> float:
        return float(self.weights @ self.analyse(self.physical(x)).compliance)

    def grad_physical(self, rho: np.ndarray, a: Analysis) -> np.ndarray:
        """dJ/drho (unclipped), passive cells included."""
        dmod = self.penal * rho ** (self.penal - 1) * (self.e0 - self.emin)
        g = -dmod * (a.energy @ self.weights)
        for k, c in enumerate(self.cases):
            if c.fb is not None:
                ue = a.u[self.fe.edof, k]  # (ne, 24)
                g = g + self.weights[k] * 2.0 * (ue @ c.fb)
        return g

    def gradient(self, x: np.ndarray) -> np.ndarray:
        """dJ/dx through the filter (passive cells hold rho = 1, so their rho has no gradient). For the tests."""
        rho = self.physical(x)
        g = self.grad_physical(rho, self.analyse(rho))
        g[self.passive] = 0.0
        return self.filt.backward(g)

    def volume(self, rho: np.ndarray) -> float:
        return float(rho[self.design].mean())


@dataclass
class RunResult:
    volfrac: float
    iterations: int
    converged: bool
    budget_stop: bool
    final_change: float
    history: list[float]
    compliance: list[float]
    rho: np.ndarray
    energy: np.ndarray
    design_volume_fraction: float
    clipped: int
    seconds: float
    timings: dict

    @property
    def drift_last20(self) -> float | None:
        h = self.history
        if len(h) < 21 or h[-1] == 0:
            return None
        return abs(h[-1] - h[-21]) / abs(h[-1])


def optimise(
    prob: Problem,
    volfrac: float,
    maxit: int,
    tol: float,
    move: float,
    eta: float,
    budget_s: float | None = None,
    listener: Listener | None = None,
) -> RunResult:
    design = prob.design
    n_design = int(design.sum())
    x = np.where(design, volfrac, 1.0)
    dv_rho = np.where(design, 1.0 / max(1, n_design), 0.0)
    dv = prob.filt.backward(dv_rho)
    timings: dict = {}
    history: list[float] = []
    t_start = time.perf_counter()
    change, it, clipped, budget_stop = 1.0, 0, 0, False
    for it in range(1, maxit + 1):
        t_it = time.perf_counter()
        rho = prob.physical(x)
        a = prob.analyse(rho, timings)
        j = float(prob.weights @ a.compliance)
        history.append(j)
        t_up = time.perf_counter()
        g = prob.grad_physical(rho, a)
        g[prob.passive] = 0.0
        clipped = int((design & (g > 0)).sum())  # where added mass would raise the objective (self-weight)
        g[design & (g > CLIP)] = CLIP
        dc = prob.filt.backward(g)
        x_new = _oc(prob, x, dc, dv, volfrac, move, eta)
        change = float(np.abs(x_new - x)[design].max()) if n_design else 0.0
        x = x_new
        timings["update_s"] = timings.get("update_s", 0.0) + time.perf_counter() - t_up
        elapsed = time.perf_counter() - t_start
        if listener is not None:
            listener(
                "iteration",
                {"volfrac": volfrac, "it": it, "objective": j, "change": change, "seconds": round(time.perf_counter() - t_it, 3)},
            )
        if change < tol:
            break
        if budget_s is not None and it < maxit and elapsed + (elapsed / it) > budget_s:
            budget_stop = True
            break
    rho = prob.physical(x)
    final = prob.analyse(rho, timings)
    return RunResult(
        volfrac=volfrac,
        iterations=it,
        converged=change < tol,
        budget_stop=budget_stop,
        final_change=change,
        history=history,
        compliance=[float(c) for c in final.compliance],
        rho=rho,
        energy=final.energy,
        design_volume_fraction=prob.volume(rho),
        clipped=clipped,
        seconds=time.perf_counter() - t_start,
        timings=timings,
    )


def _oc(prob: Problem, x: np.ndarray, dc: np.ndarray, dv: np.ndarray, volfrac: float, move: float, eta: float) -> np.ndarray:
    design = prob.design
    lo, hi = np.maximum(0.0, x - move), np.minimum(1.0, x + move)
    ratio = np.where(design, -dc / np.maximum(dv, 1e-300), 0.0)
    l1, l2 = 0.0, 1e12
    x_new = x
    for _ in range(200):
        if (l2 - l1) / (l1 + l2) <= 1e-4:
            break
        lmid = 0.5 * (l1 + l2)
        x_new = np.where(design, np.clip(x * (ratio / lmid) ** eta, lo, hi), 1.0)
        if prob.volume(prob.physical(x_new)) > volfrac:
            l1 = lmid
        else:
            l2 = lmid
    return x_new
