"""The optional `geom` extra: numpy, scipy and manifold3d for Booleans and exact distances (`shambu cad clearance`,
`sweep` and `diff --surface`).

What it handles: `require()`, the one place that imports the extra. It imports inside the function, so
`shambu --help`, `shambu cad --help` and every stdlib verb run without it, and a missing module raises `GeomMissing`
(a `BambuError`, exit 3) with the install line. What it does not do: no geometry here (engines.py, clearance.py,
sweep.py), no import at module load, no printing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...errors import GeomError, GeomMissing

__all__ = ["GeomError", "GeomMissing", "GeomModules", "install_line", "require"]


def install_line() -> str:
    from ... import __version__

    return f"uv tool install --force 'shambu[geom] @ git+https://github.com/Shavvimal/shambu@v{__version__}'"


@dataclass(frozen=True)
class GeomModules:
    numpy: Any
    spatial: Any
    manifold3d: Any


def require() -> GeomModules:
    """The extra's modules, or GeomMissing naming the install line."""
    try:
        import manifold3d
        import numpy
        import scipy.spatial
    except ImportError as exc:
        raise GeomMissing(f"this check needs the optional geom extra (numpy, scipy, manifold3d): {install_line()}") from exc
    return GeomModules(numpy, scipy.spatial, manifold3d)
