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
