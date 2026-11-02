"""The invariants and the "a map, not a proof" findings of a topology run.

What it handles: `equilibrium` (at full density the reactions balance the loads, fixed DOFs read zero, the free-DOF
residual is small: else `topo.equilibrium`, FAIL), `passive_sanity` (every passive region reads density 1: else
`topo.passive_not_one`, FAIL), and the computed caveats of a domain (`domain_findings`) and of each run
(`run_findings`). The thresholds are module constants, each with its reason. `result.py` holds the codes and texts.

What it does not do: no solving, no file I/O, no printing. Needs numpy (the `topo` extra).
"""

from __future__ import annotations

import numpy as np

from ..findings import Finding
from .fe import VoxelFE
from .optimise import Analysis, Problem, RunResult
from .result import finding
from .voxel import VoxelDomain

EQ_REL = 1e-6  # relative force and moment balance: a direct solve on a sane model lands near 1e-10
VOLUME_BAND = (0.98, 1.02)  # voxel / mesh volume: outside 2 % the grid is too coarse for the part's walls
THIN_SHARE = 0.5  # more than half the design cells within 3 cells of the surface
FILTER_MIN_CELLS = 1.5  # below 1.5 x pitch the filter no longer suppresses checkerboards
UNLOADED_SHARE = 1e-3  # a region holding under 0.1 % of the strain energy in every case is idle
INERT_BAND = 0.01  # optimised / full compliance within 1 % of 1
DOMINATE_SHARE = 0.8  # one case over 80 % of the objective


def equilibrium(fe: VoxelFE, prob: Problem, a: Analysis, nodes_xyz: np.ndarray, names: list[str]) -> tuple[list[dict], list[Finding]]:
    rows, out = [], []
    modulus = prob.modulus(np.ones(fe.n_elements))
    fixed_mask = np.zeros(fe.n_dof, dtype=bool)
    fixed_mask[fe.fixed] = True
    for k, name in enumerate(names):
        u, f = a.u[:, k], a.f[:, k]
        r = fe.internal_force(modulus, u) - f
        reaction = np.where(fixed_mask, r, 0.0)
        load_sum = f.reshape(-1, 3).sum(axis=0)
        reac_sum = reaction.reshape(-1, 3).sum(axis=0)
        moment = (np.cross(nodes_xyz, (f + reaction).reshape(-1, 3))).sum(axis=0)
        span = float(np.ptp(nodes_xyz, axis=0).max()) or 1.0
        scale = max(float(np.abs(f).sum()), 1e-30)
        free_res = float(np.abs(r[~fixed_mask]).max()) if (~fixed_mask).any() else 0.0
        u_fixed = float(np.abs(u[fixed_mask]).max()) if fixed_mask.any() else 0.0
        force_res = float(np.linalg.norm(load_sum + reac_sum))
        mom_res = float(np.linalg.norm(moment))
        ok = force_res <= EQ_REL * scale and mom_res <= EQ_REL * scale * span and free_res <= EQ_REL * scale and u_fixed == 0.0
        row = {
            "case": name,
            "compliance": round(float(a.compliance[k]), 9),
            "load": [round(float(x), 9) for x in load_sum],
            "reaction": [round(float(x), 9) for x in reac_sum],
            "moment_residual_nmm": float(f"{mom_res:.3g}"),
            "free_residual_n": float(f"{free_res:.3g}"),
            "umax_mm": round(float(np.abs(u).max()), 9),
            "ok": ok,
        }
        rows.append(row)
        if not ok:
            out.append(
                finding(
                    "topo.equilibrium",
                    f"case {name}: force residual {force_res:.3g} N, moment residual {mom_res:.3g} N mm, free residual "
                    f"{free_res:.3g} N, largest fixed displacement {u_fixed:.3g} mm",
                    {"force_residual_n": force_res, "moment_residual_nmm": mom_res, "free_residual_n": free_res},
                    name,
                )
            )
    return rows, out


def passive_sanity(rho: np.ndarray, regions: dict[str, np.ndarray]) -> tuple[dict[str, float], list[Finding]]:
    means, out = {}, []
    for name, m in regions.items():
        if not m.any():
            continue
        mean = float(rho[m].mean())
        means[name] = round(mean, 6)
        if mean < 0.999:
            out.append(finding("topo.passive_not_one", f"passive region {name} reads mean density {mean:.4f}", {"mean": mean}, name))
    return means, out


def domain_findings(dom: VoxelDomain, design3d: np.ndarray) -> tuple[list[Finding], float]:
    out = []
    from .voxel import surface_depth_share

    if dom.mesh_volume_mm3:
        ratio = dom.voxel_volume_mm3 / dom.mesh_volume_mm3
        if not VOLUME_BAND[0] <= ratio <= VOLUME_BAND[1]:
            out.append(
                finding(
                    "topo.voxel_volume",
                    f"voxel volume {dom.voxel_volume_mm3:.1f} mm3 is {100 * ratio:.1f} % of the mesh volume "
                    f"{dom.mesh_volume_mm3:.1f} mm3; a finer pitch follows the walls better",
                    {"ratio": ratio},
                )
            )
    if dom.odd_columns:
        out.append(finding("topo.odd_parity", None, {"odd_columns": dom.odd_columns}))
    if dom.dropped:
        out.append(
            finding("topo.dropped_pieces", f"{dom.dropped} cells in {dom.pieces - 1} smaller pieces were removed", {"cells": dom.dropped})
        )
    share = surface_depth_share(dom.mask, design3d)
    if share > THIN_SHARE:
        out.append(
            finding(
                "topo.thin_walls",
                f"{100 * share:.0f} % of design cells lie within 3 cells of the surface; walls this thin leave the map little room",
                {"share": share},
            )
        )
    return out, share


def filter_findings(rmin: float, pitch: float, min_member_mm: float | None) -> list[Finding]:
    out = []
    if rmin < FILTER_MIN_CELLS * pitch:
        out.append(
            finding("topo.filter_small", f"rmin {rmin:g} mm is under {FILTER_MIN_CELLS} x the pitch {pitch:g} mm", {"rmin_mm": rmin})
        )
    elif min_member_mm is not None and rmin < min_member_mm:
        out.append(
            finding(
                "topo.filter_small",
                f"rmin {rmin:g} mm is under the minimum member {min_member_mm:g} mm: members the map draws can be too thin to print as members",
                {"rmin_mm": rmin, "min_member_mm": min_member_mm},
            )
        )
    return out


def run_findings(
    run: RunResult,
    full: Analysis,
    weights: np.ndarray,
    raw_weights: bool,
    names: list[str],
) -> list[Finding]:
    tag = f"v{round(100 * run.volfrac):02d}"
    out = []
    if run.budget_stop:
        out.append(
            finding(
                "topo.budget_stop",
                f"{tag}: stopped by the time budget after {run.iterations} iterations",
                {"iterations": run.iterations},
                tag,
            )
        )
    elif not run.converged:
        drift = run.drift_last20
        d = "n/a" if drift is None else f"{drift:.2g}"
        out.append(
            finding(
                "topo.not_converged",
                f"{tag}: final change {run.final_change:.3g} after {run.iterations} iterations; objective drift over the last 20: {d}",
                {"final_change": run.final_change, "iterations": run.iterations, "drift_last20": drift},
                tag,
            )
        )
    if run.clipped:
        out.append(finding("topo.sens_clipped", f"{tag}: {run.clipped} cells clipped in the last iteration", {"cells": run.clipped}, tag))
    comp_full = full.compliance
    for k, name in enumerate(names):
        if comp_full[k] > 0 and abs(run.compliance[k] / comp_full[k] - 1) < INERT_BAND:
            out.append(
                finding("topo.inert_case", f"{tag}: case {name} compliance ratio {run.compliance[k] / comp_full[k]:.4f}", None, name)
            )
    if raw_weights:
        parts = weights * np.asarray(run.compliance)
        total = float(parts.sum())
        if total > 0 and float(np.max(parts)) / total > DOMINATE_SHARE:
            k = int(np.argmax(parts))
            out.append(
                finding(
                    "topo.case_dominates",
                    f'{tag}: case {names[k]} makes {100 * parts[k] / total:.0f} % of the objective; use "weights": "normalised"',
                    {"share": float(parts[k] / total)},
                    names[k],
                )
            )
    return out


def unloaded_findings(full: Analysis, regions: dict[str, np.ndarray], passive: np.ndarray) -> list[Finding]:
    """Regions that nothing loads at full density (their low density says nothing about spare material)."""
    out = []
    tot = full.energy.sum(axis=0)
    design = ~passive
    for name, m in regions.items():
        cells = m & design
        if not cells.any():
            continue
        share = full.energy[cells].sum(axis=0) / np.where(tot > 0, tot, 1.0)
        if float(share.max()) < UNLOADED_SHARE:
            out.append(
                finding(
                    "topo.unloaded_region",
                    f"region {name} holds at most {100 * float(share.max()):.3f} % of the full-density strain energy in any case",
                    {"energy_share_max": float(share.max())},
                    name,
                )
            )
    return out
