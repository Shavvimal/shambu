"""The `shambu topo` facade: check, voxelise, solve, validate and trace, each returning a `TopoReport`.

What it handles: the order of the steps (read the spec, build the domain, number the model, apply supports and loads,
check equilibrium at full density, optimise each volume fraction, read the map), the findings each step adds, and the
files `solve` writes into its output folder: `run.json` (the same document `--json` prints, strict JSON),
`density_<tag>.npy` (float32, NaN outside the domain, indexed x, y, z; the tag is `spec.volfrac_tag`, `v50` for 0.5),
`passive.npy` (int16: 0 for a design or outside cell, i + 1 for a cell of the i-th passive region in `passive`),
`regions.md` and `iso_<tag>.stl`.

Size: the grid, the cells, the degrees of freedom and the filter weights are checked against the caps of
`spec.limits(max_cells)` before each is allocated; a grid over a cap is a TopoSpecError (exit 1) with the memory
estimate. Every verb's data goes through `result.seal`, so a NaN or an infinity is a `topo.non_finite` FAIL, never
an ok result.

`Topo` receives its listener (progress events) and its linear solver in the constructor and only stores them. Only
`check` runs without the `topo` extra; every other verb calls `require` first, so a missing extra is a
TopoDependencyError (exit 3) with the install line, never an ImportError.

What it does not do: no printing (cli.py prints), no CAD edits, no structural sign-off.
"""

from __future__ import annotations

import json
import math
import platform
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..errors import TopoError, TopoSpecError
from . import require
from .result import TopoReport, finding, seal, standing
from .spec import (
    MAX_CELLS,
    TopoSpec,
    grid_shape,
    limits,
    load_spec,
    memory_mb,
    override_errors,
    parse_spec,
    rules_from,
    size_errors,
    spec_summary,
    volfrac_tag,
)

Listener = Any  # Callable[[str, dict], None]; kept loose so this module imports without numpy


@dataclass
class _Artefacts:
    """What `solve` writes besides run.json: the domain and, per run tag, the densities and the run entry."""

    dom: Any
    arrays: dict[str, Any]


@dataclass
class Topo:
    listener: Listener | None = None  # hears "domain", "full", "iteration" and "run" events
    solver: Any | None = None  # a linsolve.LinearSolver; None: SuperLUSolver

    def _emit(self, event: str, data: dict) -> None:
        if self.listener is not None:
            self.listener(event, data)

    # ------------------------------------------------------------------ check
    def check(self, spec_path: str) -> TopoReport:
        """Validate a spec with the stdlib only. A spec error is a failed check (exit 1), not an exception."""
        try:
            spec = load_spec(spec_path)
        except TopoSpecError as e:
            return TopoReport("check", {"spec": spec_path, "errors": e.errors}, not_checked=[], text=list(e.errors), failed=True)
        errors = []
        span = None
        if spec.domain.box is not None:
            lo, hi = spec.domain.box
            span = (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])
        elif not Path(spec.domain.stl or "").is_file():
            errors.append(f"spec.domain.stl: {spec.domain.stl} is not a file")
        else:
            from ..errors import BambuError
            from ..mesh import bbox, read_stl

            try:
                lo, hi = bbox(read_stl(spec.domain.stl or ""))
                span = (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])
            except (BambuError, ValueError) as e:
                errors.append(f"spec.domain.stl: {e}")
        size = None
        if span is not None:
            shape = grid_shape(span, spec.domain.pitch)
            n = shape[0] * shape[1] * shape[2]
            key = "cells" if spec.domain.box is not None else "grid_cells"
            size = {"grid_shape": list(shape), "grid_cells": n, "caps": limits(MAX_CELLS), "memory_mb_at_grid": memory_mb(n)}
            what = f"spec.domain.pitch: the grid at {spec.domain.pitch:g} mm holds {'x'.join(map(str, shape))} cells, and"
            errors += size_errors(what, n, key, MAX_CELLS, n if key == "cells" else min(n, MAX_CELLS))
        data = {"spec": spec_path, "spec_sha256": spec.sha256, "errors": errors, "summary": spec_summary(spec), "size": size}
        text = errors or [f"spec {spec_path}: valid ({len(spec.cases)} cases, {len(spec.supports)} supports)"]
        return TopoReport("check", data, not_checked=[], text=text, failed=bool(errors))

    # ------------------------------------------------------------------ shared set-up
    def _domain(self, spec: TopoSpec, max_cells: int):
        from .voxel import box_domain, voxelise

        d = spec.domain
        if d.box is not None:
            lo, hi = d.box
            shape = grid_shape((hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]), d.pitch)
            n = shape[0] * shape[1] * shape[2]
            _refuse(
                size_errors(
                    f"spec.domain: the box at {d.pitch:g} mm holds {'x'.join(map(str, shape))} cells, and", n, "cells", max_cells, n
                )
            )
            return box_domain(lo, hi, d.pitch)
        from ..errors import BambuError
        from ..mesh import bbox, read_stl

        try:
            facets = read_stl(d.stl or "")
        except BambuError as e:
            raise TopoError(f"spec.domain.stl: {e}") from e
        if facets:
            lo, hi = bbox(facets)
            shape = grid_shape((hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]), d.pitch)
            n = shape[0] * shape[1] * shape[2]
            what = f"spec.domain: the mesh's bounding grid at {d.pitch:g} mm holds {'x'.join(map(str, shape))} cells, and"
            _refuse(size_errors(what, n, "grid_cells", max_cells, min(n, max_cells)))
        return voxelise(facets, d.pitch, d.subsamples)

    def _model(self, spec: TopoSpec, max_cells: int):
        """Domain, FE model, element and node coordinates, passive and region masks."""
        import numpy as np

        from . import select
        from .fe import VoxelFE

        t0 = time.perf_counter()
        dom = self._domain(spec, max_cells)
        if dom.cells == 0:
            raise TopoError("the domain has no cells: the pitch is coarser than the part, or the mesh is open everywhere")
        _refuse(size_errors("the domain cells", dom.cells, "cells", max_cells, dom.cells))
        centres = dom.centres()
        fe = VoxelFE(dom.mask, dom.pitch, spec.material.nu)
        _refuse(size_errors("the degrees of freedom", fe.n_dof, "dof", max_cells, dom.cells))
        nodes_xyz = fe.node_coords(dom.origin)
        passive = {r.name: select.mask(r.cells, centres) for r in spec.passive}
        regions = {r.name: select.mask(r.cells, centres) for r in spec.regions}
        pmask = np.any(np.stack(list(passive.values())), axis=0) if passive else np.zeros(len(centres), dtype=bool)
        self._emit("domain", {"cells": dom.cells, "shape": list(dom.shape), "seconds": round(time.perf_counter() - t0, 3)})
        return dom, fe, centres, nodes_xyz, passive, regions, pmask

    @staticmethod
    def _with(spec: TopoSpec, pitch: float | None = None, **simp: Any) -> TopoSpec:
        """The spec with the command-line overrides, each checked against the spec's own range first."""
        errors = override_errors(
            pitch=pitch, maxit=simp.get("maxit"), tol=simp.get("tol"), budget_min=simp.get("budget_min"), volfrac=simp.get("volfrac") or ()
        )
        if errors:
            raise TopoSpecError(errors)
        s = replace(spec.simp, **{k: v for k, v in simp.items() if v is not None})
        d = spec.domain if pitch is None else replace(spec.domain, pitch=pitch)
        return replace(spec, simp=s, domain=d)

    # ------------------------------------------------------------------ voxelise
    def voxelise(self, spec_path: str, pitch: float | None = None, max_cells: int = MAX_CELLS) -> TopoReport:
        _refuse(override_errors(max_cells=max_cells))
        spec = self._with(load_spec(spec_path), pitch)
        require("topo", "voxelise")
        import numpy as np

        from . import select
        from .checks import domain_findings

        dom, fe, centres, nodes_xyz, passive, regions, pmask = self._model(spec, max_cells)
        counts: dict[str, list] = {"supports": [], "loads": [], "passive": [], "regions": []}
        errors = []
        for s in spec.supports:
            n = int(select.mask(s.nodes, nodes_xyz).sum())
            counts["supports"].append({"name": s.name, "nodes": n})
            if n == 0:
                errors.append(f"support {s.name!r}: no node")
        for c in spec.cases:
            for j, ld in enumerate(c.loads):
                pts = centres if ld.on == "cells" else nodes_xyz
                n = int(select.mask(ld.selection, pts).sum())
                grown = int(select.mask(ld.selection, pts, ld.grow_if_empty).sum()) if n == 0 and ld.grow_if_empty > 0 else None
                counts["loads"].append({"case": c.name, "load": j, ld.on: n, "grown": grown})
                if n == 0 and not grown:
                    errors.append(f"case {c.name!r} load {j}: no {ld.on[:-1]} (no grow_if_empty fallback)")
        counts["passive"] = [{"name": k, "cells": int(m.sum())} for k, m in passive.items()]
        counts["regions"] = [{"name": k, "cells": int(m.sum())} for k, m in regions.items()]
        if pmask.all():
            errors.append("spec.passive: the passive regions cover every domain cell; nothing is left to optimise")
        design3d = np.zeros(dom.shape, dtype=bool)
        design3d[dom.mask] = ~pmask
        findings, share = domain_findings(dom, design3d)
        data = {
            "spec": spec_path,
            "spec_sha256": spec.sha256,
            "grid": dom.grid_json(),
            "domain": {**dom.stats(), "surface_share_3_cells": round(share, 4)},
            "fe": {"elements": fe.n_elements, "nodes": fe.n_nodes, "dof": fe.n_dof},
            "selections": counts,
            "errors": errors,
        }
        text = [
            f"grid {'x'.join(map(str, dom.shape))} at {dom.pitch:g} mm: {dom.cells} cells, {fe.n_dof} DOF",
            _volume_line(dom),
            *[f"support {x['name']}: {x['nodes']} nodes" for x in counts["supports"]],
            *[
                f"case {x['case']} load {x['load']}: {x.get('cells', x.get('nodes'))} {'cells' if 'cells' in x else 'nodes'}"
                for x in counts["loads"]
            ],
            *errors,
        ]
        return seal(TopoReport("voxelise", data, findings, not_checked=[], text=text, failed=bool(errors)))

    # ------------------------------------------------------------------ solve
    def solve(
        self,
        spec_path: str,
        out_dir: str | None,
        volfrac: list[float] | None = None,
        pitch: float | None = None,
        maxit: int | None = None,
        tol: float | None = None,
        budget_min: float | None = None,
        full_only: bool = False,
        mesh: str = "auto",
        max_cells: int = MAX_CELLS,
    ) -> TopoReport:
        _refuse(override_errors(max_cells=max_cells))
        spec = load_spec(spec_path)
        spec = self._with(spec, pitch, volfrac=tuple(volfrac) if volfrac else None, maxit=maxit, tol=tol, budget_min=budget_min)
        require("topo", "solve")
        if mesh == "smooth":
            require("topo-mesh", "solve --mesh smooth")
        rep, art = self._solve(spec, full_only, max_cells)
        rep.data = {"spec": spec_path, **rep.data}
        if out_dir is not None:
            self._write(rep, art, Path(out_dir), mesh)
        return rep

    def _solve(self, spec: TopoSpec, full_only: bool, max_cells: int = MAX_CELLS) -> tuple[TopoReport, _Artefacts]:
        import numpy as np

        from . import checks, loads
        from . import report as R
        from .filter import DensityFilter
        from .linsolve import SuperLUSolver
        from .optimise import Problem, optimise

        t_start = time.perf_counter()
        dom, fe, centres, nodes_xyz, passive, regions, pmask = self._model(spec, max_cells)
        if pmask.all():
            raise TopoSpecError(["spec.passive: the passive regions cover every domain cell; nothing is left to optimise"])
        names = [c.name for c in spec.cases]
        model = loads.build(spec, fe, centres, nodes_xyz)
        labels = np.zeros(len(centres))
        for i, m in enumerate(passive.values()):
            labels[m] = i + 1
        arrays: dict[str, Any] = {"passive": np.nan_to_num(R.density_grid(dom, labels)).astype(np.int16)}
        free_modes = checks.free_rigid_modes(nodes_xyz, model.fixed)
        if free_modes:  # a mechanism: stop before the factorisation, which would pivot on round-off
            return self._stopped(spec, dom, fe, names, passive, [checks.under_constrained(free_modes)], t_start), _Artefacts(dom, arrays)
        rmin = spec.simp.rmin_mm(dom.pitch)
        weights_est = dom.cells * 4.0 / 3.0 * math.pi * (rmin / dom.pitch) ** 3
        _refuse(size_errors(f"the filter weights at rmin {rmin:g} mm", weights_est, "filter_weights", max_cells, dom.cells))
        fe.set_fixed(model.fixed)
        filt = DensityFilter.build(centres, rmin)
        prob = Problem(fe, filt, model.cases, pmask, spec.material.E, spec.simp.emin_rel, spec.simp.penal, self.solver or SuperLUSolver())
        full = prob.analyse(np.ones(fe.n_elements))
        if not np.all(np.isfinite(full.compliance)) or np.any(full.compliance < 0):
            bad = ", ".join(f"{n} {c:.3g}" for n, c in zip(names, full.compliance, strict=True) if not c >= 0)
            why = f"full-density compliance is negative or not finite ({bad} N mm): the stiffness is singular or ill-conditioned"
            return self._stopped(spec, dom, fe, names, passive, [checks.under_constrained([], why)], t_start), _Artefacts(dom, arrays)
        eq_rows, findings = checks.equilibrium(fe, prob, full, nodes_xyz, names)
        self._emit("full", {"compliance": [float(c) for c in full.compliance]})
        zero = [n for n, c in zip(names, full.compliance, strict=True) if c <= 0]
        if zero:  # its ratio would be 0 / 0, and a normalised weight 1 / 0
            raise TopoSpecError([f"case {n!r}: zero compliance at full density (its loads sit on fixed nodes)" for n in zero])
        if spec.weights == "normalised":
            prob.weights = (1.0 / len(names)) / full.compliance
        else:
            prob.weights = np.array([c.weight or 1.0 for c in spec.cases])

        design3d = np.zeros(dom.shape, dtype=bool)
        design3d[dom.mask] = ~pmask
        dfind, share = checks.domain_findings(dom, design3d)
        findings += standing() + dfind + checks.filter_findings(rmin, dom.pitch, spec.simp.min_member_mm)
        if any(c.loads for c in spec.cases):
            findings.append(finding("topo.rigid_load_patch"))
        if any(len(s.dofs) == 3 for s in spec.supports):
            findings.append(finding("topo.clamped_support"))
        for c in model.cases:
            for note in c.grown:
                findings.append(
                    finding("topo.load_fallback", f"case {c.name} load {note['load']}: grown by {note['grow_mm']:g} mm", note, c.name)
                )
        findings += checks.unloaded_findings(full, regions, pmask)

        runs: list[dict] = []
        eq_ok = all(r["ok"] for r in eq_rows)
        if not full_only and eq_ok:
            for vf in spec.simp.volfrac:
                run = optimise(
                    prob, vf, spec.simp.maxit, spec.simp.tol, spec.simp.move, spec.simp.eta, spec.simp.budget_min * 60, self.listener
                )
                tag = volfrac_tag(vf)
                means, pfind = checks.passive_sanity(run.rho, passive)
                findings += pfind + checks.run_findings(run, full, prob.weights, spec.weights == "raw", names)
                rows = R.region_rows(run.rho, ~pmask, regions)
                entry = {
                    "volfrac": vf,
                    "iterations": run.iterations,
                    "converged": run.converged,
                    "budget_stop": run.budget_stop,
                    "final_change": round(run.final_change, 6),
                    "objective_drift_last20": None if run.drift_last20 is None else float(f"{run.drift_last20:.3g}"),
                    "objective": [float(f"{j:.9g}") for j in run.history],
                    "compliance": [round(c, 9) for c in run.compliance],
                    "ratio": [round(c / f, 6) for c, f in zip(run.compliance, full.compliance, strict=True)],
                    "design_volume_fraction": round(run.design_volume_fraction, 6),
                    "clipped_sensitivities": run.clipped,
                    "passive_means": means,
                    "regions": rows,
                    "low_clusters": R.low_clusters(dom, run.rho, ~pmask),
                    "skin_solid_next_to_passive": R.skin_next_to_passive(dom, run.rho, passive),
                    "files": {"density": f"density_{tag}.npy", "iso": None, "iso_kind": None},
                    "seconds": round(run.seconds, 3),
                    "timings": {k: round(v, 3) for k, v in run.timings.items()},
                    "peak_rss_mb": _peak_rss_mb(),
                }
                runs.append(entry)
                self._emit("run", {"volfrac": vf, "iterations": run.iterations, "seconds": round(run.seconds, 3)})
                arrays[tag] = (run.rho, entry)

        data = {
            "spec_sha256": spec.sha256,
            "grid": dom.grid_json(),
            "domain": {**dom.stats(), "surface_share_3_cells": round(share, 4)},
            "fe": {"elements": fe.n_elements, "nodes": fe.n_nodes, "dof": fe.n_dof, "free": len(fe.free), "fixed": len(fe.fixed)},
            "settings": {
                "E_mpa": spec.material.E,
                "nu": spec.material.nu,
                "penal": spec.simp.penal,
                "rmin_mm": rmin,
                "move": spec.simp.move,
                "eta": spec.simp.eta,
                "maxit": spec.simp.maxit,
                "tol": spec.simp.tol,
                "weights": spec.weights,
                "weight_values": [float(f"{w:.9g}") for w in prob.weights],
                "solver": prob.solver.name,
            },
            "cases": names,
            "passive": list(passive),
            "full_density": eq_rows,
            "runs": runs,
            "versions": _versions(),
            "elapsed_s": round(time.perf_counter() - t_start, 3),
        }
        text = _solve_lines(data)
        return seal(TopoReport("solve", data, findings, text=text)), _Artefacts(dom, arrays)

    def _stopped(self, spec: TopoSpec, dom, fe, names: list[str], passive: dict, found: list, t_start: float) -> TopoReport:
        """The report of a model that cannot be solved: the findings, the grid and no runs."""
        data = {
            "spec_sha256": spec.sha256,
            "grid": dom.grid_json(),
            "domain": dom.stats(),
            "fe": {"elements": fe.n_elements, "nodes": fe.n_nodes, "dof": fe.n_dof},
            "cases": names,
            "passive": list(passive),
            "full_density": [],
            "runs": [],
            "versions": _versions(),
            "elapsed_s": round(time.perf_counter() - t_start, 3),
        }
        text = [f"grid {'x'.join(map(str, dom.shape))} at {dom.pitch:g} mm: {fe.n_elements} elements, {fe.n_dof} DOF", "no solve"]
        return seal(TopoReport("solve", data, standing() + found, text=text))

    def _write(self, rep: TopoReport, art: _Artefacts, out: Path, mesh: str) -> None:
        import numpy as np

        from ..mesh import write_stl
        from . import report as R

        dom, arrays = art.dom, art.arrays
        if mesh == "auto":
            try:
                require("topo-mesh", "solve")
                mesh = "smooth"
            except TopoError:
                mesh = "cubes"
        try:
            out.mkdir(parents=True, exist_ok=True)
            np.save(out / "passive.npy", arrays["passive"])
            for tag, val in arrays.items():
                if tag == "passive":
                    continue
                rho, entry = val
                np.save(out / entry["files"]["density"], R.density_grid(dom, rho))
                if mesh != "none":
                    facets = R.smooth_facets(dom, rho) if mesh == "smooth" else R.cube_facets(dom, rho)
                    if facets:
                        name = f"iso_{tag}.stl"
                        write_stl(out / name, facets, name=f"shambu topo {tag}")
                        entry["files"]["iso"] = name
                        entry["files"]["iso_kind"] = mesh
            rep.data["files"] = {"run": "run.json", "regions": "regions.md", "passive": "passive.npy"}
            (out / "regions.md").write_text(_regions_md(rep.data))
            (out / "run.json").write_text(json.dumps(rep.to_json(), indent=1, allow_nan=False) + "\n")
        except OSError as e:
            raise TopoError(f"{out}: cannot write the run files: {e}") from e
        rep.text.append(f"wrote {out}/run.json, regions.md and {len(arrays) - 1} density map(s)")

    # ------------------------------------------------------------------ validate
    def validate(self, size: str = "small") -> TopoReport:
        require("topo", "validate")
        from . import michell

        doc = michell.spec_doc(size)
        spec, errors = parse_spec(doc, f"michell-{size}.json")
        if spec is None:  # the built-in spec is valid; this guards an edit that breaks it
            raise TopoSpecError(errors)
        rep, _ = self._solve(spec, full_only=False)
        if not rep.data["runs"]:
            raise TopoError(f"the built-in Michell spec ({size}) gave no run: {[f.message for f in rep.findings if f.severity == 'FAIL']}")
        run = rep.data["runs"][0]
        means = {r["name"]: r["mean"] for r in run["regions"]}
        m = michell.metrics(means, run["objective"])
        rep.verb = "validate"
        rep.failed = not m["pass"]
        rep.data = {"size": size, "problem": doc, **rep.data, "metrics": m}
        rep.text += [f"{k}: {v['value']} (want {v['band']}): {'PASS' if v['pass'] else 'FAIL'}" for k, v in m["checks"].items()]
        rep.text.append(f"Michell cantilever {size}: {'PASS' if m['pass'] else 'FAIL'}")
        return seal(rep)

    # ------------------------------------------------------------------ trace
    def trace(self, run_path: str, faces_path: str, rules: str = "conservative", volfrac: float | None = None) -> TopoReport:
        from .spec import load_faces

        faces = load_faces(faces_path)
        rule_set = rules_from(rules)
        if volfrac is not None:
            _refuse(override_errors(volfrac=[volfrac]))
            rule_set = replace(rule_set, keep_volfrac=volfrac)
        require("topo-trace", "trace")
        from . import trace as T

        try:
            run_doc = json.loads(Path(run_path).read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise TopoError(f"{run_path}: cannot read the run file: {e}") from e
        data, findings, text = T.trace(run_doc, Path(run_path).parent, faces, rule_set)
        return seal(TopoReport("trace", {"run": run_path, "faces": faces_path, **data}, standing() + findings, text=text))


# ---------------------------------------------------------------------------- helpers
def _refuse(errors: list[str]) -> None:
    if errors:
        raise TopoSpecError(errors)


def _volume_line(dom) -> str:
    s = dom.stats()
    if s["mesh_volume_mm3"] is None:
        return f"voxel volume {s['voxel_volume_mm3']:.1f} mm3 (box domain)"
    return f"voxel volume {s['voxel_volume_mm3']:.1f} mm3, mesh {s['mesh_volume_mm3']:.1f} mm3 (ratio {s['volume_ratio']})"


def _solve_lines(d: dict) -> list[str]:
    g = d["grid"]
    out = [f"grid {'x'.join(map(str, g['shape']))} at {g['pitch']:g} mm: {d['fe']['elements']} elements, {d['fe']['dof']} DOF"]
    for r in d["full_density"]:
        out.append(
            f"full density, case {r['case']}: compliance {r['compliance']:.6g} N mm, reaction {r['reaction']}, {'balanced' if r['ok'] else 'NOT balanced'}"
        )
    for r in d["runs"]:
        state = "converged" if r["converged"] else ("budget stop" if r["budget_stop"] else "not converged")
        ratios = ", ".join(f"{n} {x:.3f}" for n, x in zip(d["cases"], r["ratio"], strict=True))
        out.append(
            f"volfrac {r['volfrac']:g}: {r['iterations']} iterations ({state}, change {r['final_change']:.3g}), stiffness loss {ratios}"
        )
        out += [
            f"  region {x['name']}: mean {x['mean']}, share under 0.3 {x['under_0_3']} ({x['design_cells']} cells)"
            for x in r["regions"]
            if x["design_cells"]
        ]
    return out


def _regions_md(d: dict) -> str:
    lines = ["# Topology run regions", "", "A map, not a proof: read the findings in run.json before acting on a number.", ""]
    for r in d["runs"]:
        lines += [
            f"## volfrac {r['volfrac']:g}",
            "",
            "| Region | Design cells | Mean density | Share under 0.3 |",
            "| --- | --- | --- | --- |",
        ]
        lines += [f"| {x['name']} | {x['design_cells']} | {x['mean']} | {x['under_0_3']} |" for x in r["regions"]]
        lines += ["", "| Passive region | Mean density |", "| --- | --- |"]
        lines += [f"| {k} | {v} |" for k, v in r["passive_means"].items()]
        lines.append("")
    return "\n".join(lines)


def _versions() -> dict:
    from .. import __version__

    v = {"shambu": __version__, "python": platform.python_version()}
    for mod in ("numpy", "scipy", "shapely", "skimage"):
        m = sys.modules.get(mod)
        if m is not None:
            v[mod] = getattr(m, "__version__", "?")
    return v


def _peak_rss_mb() -> float | None:
    try:
        import resource
    except ImportError:  # not on this platform: the field stays null
        return None
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024, 1)
