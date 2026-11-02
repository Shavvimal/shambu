"""`shambu topo`: a density-based stiffness map for a part, as a design aid before a print.

What it handles: a voxel grid of a closed STL (read by `shambu.mesh`), linear hex8 finite elements, SIMP density
optimisation with a density filter and an optimality-criteria update, several weighted load cases with optional
self-weight, a region report, an iso-surface STL, and the tracing of a density map into window polygons on plate
walls. Every result carries the "a map, not a proof" findings (`checks.py`) and a not-checked list.

What it does not do: no stress, buckling, fatigue, layer anisotropy or proof load; no structural certification of any
kind. It never edits a CAD model or a project. Nothing here prints (cli.py prints).

Layout: `spec.py` and this file are stdlib only, so `shambu topo --help` and `shambu topo check` work on a base
install. Every other module imports numpy and scipy and is reached only after `require()` (extra `topo`); `trace.py`
needs shapely (extra `topo-trace`) and the smooth iso-surface needs scikit-image (extra `topo-mesh`).
"""

from __future__ import annotations

import importlib

from ..errors import TopoDependencyError

__all__ = ["EXTRAS", "TopoDependencyError", "install_line", "require"]

# Each extra and the modules it must make importable.
EXTRAS: dict[str, tuple[str, ...]] = {
    "topo": ("numpy", "scipy.sparse", "scipy.sparse.linalg", "scipy.spatial", "scipy.ndimage"),
    "topo-trace": ("numpy", "scipy.ndimage", "shapely"),
    "topo-mesh": ("numpy", "skimage.measure"),
}
_WHAT = {
    "topo": "numpy and scipy",
    "topo-trace": "numpy, scipy and shapely",
    "topo-mesh": "numpy and scikit-image",
}


def install_line(extra: str) -> str:
    from .. import __version__

    return f"uv tool install --force 'shambu[{extra}] @ git+https://github.com/Shavvimal/shambu@v{__version__}'"


def require(extra: str, verb: str) -> None:
    """Import every module the extra needs, or raise TopoDependencyError (exit 3) naming the install line."""
    try:
        for mod in EXTRAS[extra]:
            importlib.import_module(mod)
    except ImportError as exc:
        raise TopoDependencyError(f"shambu topo {verb} needs {_WHAT[extra]} (the {extra} extra): {install_line(extra)}") from exc
