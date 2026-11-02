"""Loads and supports on the voxel model.

What it handles:

- `rigid_distribute(xyz, F, M)`: a force and a moment about the centroid of n nodes, spread as the least-norm field of
  a rigid insert, `f_i = F / n + w x r_i` with `A w = M` and `A = sum(|r_i|^2 I - r_i r_i^T)`. The node forces sum
  to F and their moments to M; a moment the nodes cannot carry (one node, or nodes on a line) is a spec error.
- `body_force_vector`: the nodal load of one full-density cell under an acceleration given in g (each of the eight
  nodes takes an eighth), from the material density and fill.
- `build`: the supports (fixed DOFs) and, per case, the constant load vector, the per-cell self-weight vector that
  scales with density, and notes on any selection that had to grow.

Units: mm, N, MPa, so mass is in tonnes (1 g/cm3 = 1e-9 t/mm3) and 1 g = 9810 mm/s2.

What it does not do: no contact, no bolt preload, no follower loads, no file I/O, no printing. Needs numpy (the
`topo` extra).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..errors import TopoSpecError
from . import select
from .fe import VoxelFE
from .spec import AXES, Load, TopoSpec

G_MM_S2 = 9810.0
T_PER_MM3_PER_G_CM3 = 1e-9


def rigid_distribute(xyz: np.ndarray, force: np.ndarray, moment: np.ndarray, where: str = "load") -> np.ndarray:
    xyz = np.asarray(xyz, dtype=float)
    n = len(xyz)
    r = xyz - xyz.mean(axis=0)
    a = (r * r).sum() * np.eye(3) - r.T @ r
    m = np.asarray(moment, dtype=float)
    w = np.linalg.lstsq(a, m, rcond=None)[0] if np.any(m) else np.zeros(3)
    f = np.asarray(force, dtype=float)[None, :] / n + np.cross(w[None, :], r)
    carried = np.cross(r, f).sum(axis=0)
    if float(np.linalg.norm(carried - m)) > 1e-9 * max(1.0, float(np.linalg.norm(m))):
        raise TopoSpecError([f"{where}: the moment {m.tolist()} N mm cannot be carried by {n} node(s) that lie on a point or a line"])
    return f


def body_force_vector(accel_g: tuple[float, float, float], density_g_cm3: float, fill: float, pitch: float) -> np.ndarray:
    """(24,) nodal forces of one full-density cell."""
    mass_t = density_g_cm3 * T_PER_MM3_PER_G_CM3 * fill * pitch**3
    per_node = mass_t * G_MM_S2 * np.asarray(accel_g, dtype=float) / 8
    return np.tile(per_node, 8)


@dataclass
class CaseLoads:
    name: str
    f0: np.ndarray  # (n_dof,) loads that do not change with density
    fb: np.ndarray | None  # (24,) self-weight of one full cell when it scales with density, else None
    nodes_loaded: list[int] = field(default_factory=list)  # per load
    notes: list[str] = field(default_factory=list)
    grown: list[dict] = field(default_factory=list)  # loads whose selection was empty and grew


@dataclass
class Model:
    fixed: np.ndarray
    support_nodes: dict[str, int]
    cases: list[CaseLoads]


def _nodes_of(load: Load, fe: VoxelFE, centres: np.ndarray, nodes_xyz: np.ndarray, grow: float) -> np.ndarray:
    if load.on == "nodes":
        return np.flatnonzero(select.mask(load.selection, nodes_xyz, grow))
    cells = select.mask(load.selection, centres, grow)
    return np.unique(fe.element_nodes()[cells])


def build(spec: TopoSpec, fe: VoxelFE, centres: np.ndarray, nodes_xyz: np.ndarray) -> Model:
    errors: list[str] = []
    fixed: list[np.ndarray] = []
    support_nodes: dict[str, int] = {}
    for s in spec.supports:
        nodes = np.flatnonzero(select.mask(s.nodes, nodes_xyz))
        support_nodes[s.name] = len(nodes)
        if len(nodes) == 0:
            errors.append(f"support {s.name!r}: the selection holds no node of the domain")
        for a in s.dofs:
            fixed.append(3 * nodes + AXES.index(a))
    cases = []
    for c in spec.cases:
        f0 = np.zeros(fe.n_dof)
        cl = CaseLoads(c.name, f0, None)
        for j, ld in enumerate(c.loads):
            where = f"case {c.name!r} load {j}"
            nodes = _nodes_of(ld, fe, centres, nodes_xyz, 0.0)
            if len(nodes) == 0 and ld.grow_if_empty > 0:
                nodes = _nodes_of(ld, fe, centres, nodes_xyz, ld.grow_if_empty)
                cl.grown.append({"load": j, "grow_mm": ld.grow_if_empty, "nodes": len(nodes)})
                cl.notes.append(f"{where}: the selection was empty; grown by {ld.grow_if_empty:g} mm to {len(nodes)} nodes")
            if len(nodes) == 0:
                errors.append(f"{where}: the selection holds no {ld.on[:-1]} of the domain (set grow_if_empty to allow a fallback)")
                continue
            xyz = nodes_xyz[nodes]
            force = np.asarray(ld.force, dtype=float)
            moment = np.asarray(ld.moment, dtype=float)
            if ld.about is not None:  # a force acting at `about`, moved to the centroid as force plus couple
                moment = moment + np.cross(np.asarray(ld.about) - xyz.mean(axis=0), force)
            try:
                f = rigid_distribute(xyz, force, moment, where)
            except TopoSpecError as e:
                errors += e.errors
                continue
            np.add.at(f0, (3 * nodes[:, None] + np.arange(3)[None, :]).ravel(), f.ravel())
            cl.nodes_loaded.append(len(nodes))
        if c.body_force is not None:
            fb = body_force_vector(c.body_force.accel_g, spec.material.density_g_cm3, spec.material.fill, fe.pitch)
            if c.body_force.scale_with_density:
                cl.fb = fb
            else:
                np.add.at(f0, fe.edof.ravel(), np.tile(fb, fe.n_elements))
        cases.append(cl)
    if errors:
        raise TopoSpecError(errors)
    return Model(np.concatenate(fixed) if fixed else np.zeros(0, dtype=int), support_nodes, cases)


def body_load(fe: VoxelFE, fb: np.ndarray, scale: np.ndarray) -> np.ndarray:
    """The assembled self-weight for per-cell scale factors (the physical densities)."""
    return np.bincount(fe.edof.ravel(), weights=(scale[:, None] * fb[None, :]).ravel(), minlength=fe.n_dof)
