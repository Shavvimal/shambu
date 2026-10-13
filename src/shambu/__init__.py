"""shambu: prepare, inspect and patch Bambu Studio projects, and read a Bambu Lab printer.

Entry points:
- `Bambu` (facade) for programmatic use.
- `shambu.cli.main` for the command line (the `shambu` console script).
"""

from .errors import AssembleError, BambuError, BuildError, ConfError, LayoutError, PresetError, PrinterError, ProjectError, StudioError
from .facade import Bambu
from .spec import Spec, SpecBuilder

__all__ = [
    "AssembleError",
    "Bambu",
    "BambuError",
    "BuildError",
    "ConfError",
    "LayoutError",
    "PresetError",
    "PrinterError",
    "ProjectError",
    "Spec",
    "SpecBuilder",
    "StudioError",
    "__version__",
]
__version__ = "0.2.0"
