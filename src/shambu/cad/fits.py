"""The fit table `shambu cad fit` gauges holes against, held as data (like `recipes.RECIPES`).

What it handles: a frozen `Fit` per named fit and the built-in rows: ISO 273 fine, medium and coarse clearance
holes for M2 to M8, a published standard (the `basis` of each row names it). A user's own fits (heat-set inserts,
press pins, slip fits, self-tapping holes) come from a fits file or the spec's `fits` block, each with its own
`basis`, because their sizes depend on the part the user buys. `merge` joins the two and names every built-in a user
row shadows.

What it does not do: no hole compensation. A row holds the diameter the user wants on the mesh (the CAD size). The
allowance a printed horizontal bore needs is the user's design decision, recorded in the spec's
`intent.hole_allowance_mm` and reported as `fit.horizontal`. No file reading, no printing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ..errors import CadSpecError

KINDS = ("clearance", "insert", "press", "slip", "tapped-in-plastic")
COUPON_KINDS = ("insert", "press", "slip")  # the first print of such a fit is a coupon, in the feature's own orientation
# The gauge refits the diameter on mesh vertices, which lie on the CAD surface, so it reads the CAD size to rounding.
# +/- 0.02 mm leaves room for the STL writer's float32 coordinates and a 0.01 mm export grid, nothing more.
DEFAULT_BAND_MM = (-0.02, 0.02)
_KEYS = {"name", "kind", "hole_mm", "band_mm", "depth_min_mm", "wall_min_mm", "offset_max_mm", "basis"}


@dataclass(frozen=True)
class Fit:
    name: str
    kind: str
    hole_mm: float
    band_mm: tuple[float, float] = DEFAULT_BAND_MM
    depth_min_mm: float | None = None
    wall_min_mm: float | None = None
    offset_max_mm: float | None = None
    basis: str = ""

    @classmethod
    def from_json(cls, d: Mapping, where: str = "fits") -> Fit:
        if not isinstance(d, Mapping):
            raise CadSpecError(f"{where}: a fit is an object")
        unknown = sorted(set(d) - _KEYS)
        if unknown:
            raise CadSpecError(f"{where}: unknown fit keys {unknown}")
        for req in ("name", "kind", "hole_mm", "basis"):
            if req not in d:
                raise CadSpecError(f"{where}: a fit needs {req!r} (every fit says where its number comes from in 'basis')")
        if d["kind"] not in KINDS:
            raise CadSpecError(f"{where}: fit {d['name']!r} kind {d['kind']!r} is not one of {KINDS}")
        if not str(d["basis"]).strip():
            raise CadSpecError(f"{where}: fit {d['name']!r} has an empty 'basis'")
        band = d.get("band_mm", DEFAULT_BAND_MM)
        try:
            lo, hi = float(band[0]), float(band[1])
            hole = float(d["hole_mm"])
        except (TypeError, ValueError, IndexError) as e:
            raise CadSpecError(f"{where}: fit {d['name']!r}: {e}") from e
        if lo > hi or hole <= 0:
            raise CadSpecError(f"{where}: fit {d['name']!r}: hole_mm must be positive and band_mm [low, high]")

        def opt(k: str) -> float | None:
            return float(d[k]) if d.get(k) is not None else None

        return cls(
            str(d["name"]), str(d["kind"]), hole, (lo, hi), opt("depth_min_mm"), opt("wall_min_mm"), opt("offset_max_mm"), str(d["basis"])
        )

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "hole_mm": self.hole_mm,
            "band_mm": list(self.band_mm),
            "depth_min_mm": self.depth_min_mm,
            "wall_min_mm": self.wall_min_mm,
            "offset_max_mm": self.offset_max_mm,
            "basis": self.basis,
        }


# ISO 273 clearance holes (fine, medium, coarse series), metric coarse thread sizes M2 to M8.
_ISO_273 = {
    "M2": (2.2, 2.4, 2.6),
    "M2.5": (2.7, 2.9, 3.1),
    "M3": (3.2, 3.4, 3.6),
    "M4": (4.3, 4.5, 4.8),
    "M5": (5.3, 5.5, 5.8),
    "M6": (6.4, 6.6, 7.0),
    "M8": (8.4, 9.0, 10.0),
}
FITS: tuple[Fit, ...] = tuple(
    Fit(f"{size}-clearance-{series}", "clearance", d, basis=f"ISO 273 {series} series clearance hole for {size}")
    for size, ds in _ISO_273.items()
    for series, d in zip(("fine", "medium", "coarse"), ds, strict=True)
)


def merge(user: Iterable[Fit]) -> tuple[dict[str, Fit], list[str]]:
    """(name -> fit, notes): the built-in rows, then the user's. A user row with a built-in name replaces it, and a
    note names both, so a shadowed standard size is visible."""
    out = {f.name: f for f in FITS}
    notes: list[str] = []
    for f in user:
        old = out.get(f.name)
        if old is not None and old in FITS:
            notes.append(f"fit {f.name!r} from the spec ({f.hole_mm} mm, {f.basis}) shadows the built-in {old.hole_mm} mm ({old.basis})")
        out[f.name] = f
    return out, notes
