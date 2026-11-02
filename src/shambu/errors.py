"""Exception hierarchy. Callers catch `BambuError` for anything this library raises on purpose."""


class BambuError(Exception):
    """Base of every error raised by shambu."""


class PresetError(BambuError):
    """A preset could not be found or resolved."""


class StudioError(BambuError):
    """The Bambu Studio binary is missing, failed, or returned unusable output."""


class AssembleError(BambuError):
    """An assemble list breaks one of the CLI's rules."""


class LayoutError(BambuError):
    """Parts do not fit the bed or a nozzle's reach."""


class ProjectError(BambuError):
    """A .3mf file is not a Bambu project or a patch is invalid."""


class ConfError(BambuError):
    """Bambu Studio's configuration is missing something."""


class PrinterError(BambuError):
    """The printer could not be found or read."""


class BuildError(BambuError):
    """A plate spec is invalid or the pipeline could not finish."""


class FileError(BambuError):
    """A file or folder shambu had to create, write or replace could not be: the operation and the path, from the
    OSError at the orchestration boundary (exit 3 at the CLI, one JSON document with --json)."""


class CadError(BambuError):
    """A `shambu cad` input that cannot be used: a file that does not read, a record or a destination that cannot be
    written. Exit 3 at the CLI."""


class MeshMissing(CadError):
    """A part's mesh file is not there. Inside a gate it becomes `cad.incomplete.mesh_missing` for that rule."""


class CadSpecError(CadError):
    """A CAD gate spec (`shambu.cad/1`) or an expectation file that breaks its schema. The CLI reports it as a failed
    check (exit 1, like an invalid plate spec), not as an environment error."""


class GeomMissing(CadError):
    """The optional `geom` extra (numpy, scipy, manifold3d) is not installed; the message carries the install line."""


class GeomError(CadError):
    """The geometry library refused an operation (a Boolean on a mesh it cannot use). Inside a gate it becomes a FAIL
    finding for the pair, never an exemption."""
