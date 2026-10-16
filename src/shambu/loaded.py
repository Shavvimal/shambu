"""Reconcile a spec's filaments with what the printer holds (or, with the printer unreachable, with the GUI's presets).

Why: a spec written for one spool passed every check after the owner changed spools (docs/errors/2026-10-16). The
tool read Studio's last-used presets from BambuStudio.conf, which can be days old, and never compared them with
the printer. Now `check` and `build` read the trays over the same single `pushall` as `printer status` and apply:

- a filament whose `preset` is "loaded" (or omitted) takes the spool in that nozzle, with its colour;
- a pinned preset that differs from the spool is a problem (exit 1) unless `allow_mismatch`, which the report records;
- a pinned colour that differs from the spool is replaced unless the spec gives a `colour_note`;
- printer unreachable: a warning first, then the GUI presets stand in for "loaded" and pinned presets are kept.

The result carries the rewritten spec (concrete preset names and colours), one source statement per filament for
the report, and the problems and warnings for the plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .printer import LoadedFilaments, NozzleLoad, colour_name
from .spec import FilamentSpec, Spec

MISMATCH_FLAG = "--allow-filament-mismatch"


@dataclass
class FilamentSource:
    slot: int
    preset: str
    colour: str | None
    source: str  # printer | spec | spec (override) | gui | unresolved
    text: str  # one statement for the report, e.g. "from the printer (left external spool, white Generic PETG, read 15:41)"

    def to_json(self) -> dict:
        return dict(self.__dict__)


@dataclass
class FilamentResolution:
    spec: Spec
    sources: list[FilamentSource]
    headline: str  # the one line the report and the hand-back carry first
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    read_at: str | None = None
    unreachable: str | None = None

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_json(self) -> dict:
        return {
            "headline": self.headline,
            "read_at": self.read_at,
            "unreachable": self.unreachable,
            "sources": [s.to_json() for s in self.sources],
            "problems": list(self.problems),
            "warnings": list(self.warnings),
        }


def _absence(nz: NozzleLoad | None, where: str, read: str) -> str:
    """Why a nozzle has no comparable spool: not reported, a slot the report does not list, or nothing loaded (then the
    fitted spool is named so the owner can load it). Never a guess at what the nozzle holds."""
    if nz is None:
        return f"the printer did not report {where} ({read})"
    if nz.unlisted:
        return f"{where} holds slot {nz.loaded_slot} which the report does not list ({read})"
    fitted = [t for t in nz.fitted if not t.empty]
    hint = "; ".join(f"the {t.label} holds {t.describe()}: load it, or pin its preset" for t in fitted)
    return f"nothing is loaded in {where} ({read})" + (f"; {hint}" if hint else "")


def _colour(i: int, fs: FilamentSpec, spool: str | None, warnings: list[str], where: str) -> tuple[str | None, str]:
    """The colour to use and a note: the spool's unless the spec pins one with a colour_note."""
    if fs.colour and spool and fs.colour.upper() != spool.upper():
        if fs.colour_note:
            return fs.colour, f"; colour {fs.colour} pinned by the spec ({fs.colour_note}), the spool is {spool}"
        warnings.append(
            f"filament {i}: colour {fs.colour} in the spec replaced by the spool colour {spool} ({where}); add a colour_note to keep it"
        )
        return spool, ""
    if fs.colour and not spool:
        return fs.colour, "; colour from the spec (the printer gave none)"
    return spool or fs.colour, ""


def _split_colours(v) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        return [c.strip() for c in v.replace(",", ";").split(";") if c.strip()]
    return []


def reconcile(
    spec: Spec,
    loaded: LoadedFilaments | None,
    *,
    gui_presets: list[str] | None = None,
    gui_colours=None,
    allow_mismatch: bool = False,
    unreachable: str | None = None,
    n_extruders: int = 2,
) -> FilamentResolution:
    """Rewrite `spec.filaments` against the printer (`loaded`), or against the GUI presets when `loaded` is None.
    `n_extruders` comes from the machine preset (the same default map the pipeline uses: slot n -> extruder n, capped),
    never from the report, so a report that lists one nozzle cannot re-route a filament."""
    problems: list[str] = []
    warnings: list[str] = []
    sources: list[FilamentSource] = []
    new: list[FilamentSpec] = []
    n_noz = max(1, n_extruders)
    gui = list(gui_presets or [])
    gcols = _split_colours(gui_colours)
    if loaded is None:
        why = unreachable or "no printer read"
        warnings.append(
            f"WARNING: printer not read ({why}): filaments come from the GUI presets in BambuStudio.conf, not from the spools; "
            "confirm by eye that they match what is loaded"
        )
    for i, fs in enumerate(spec.filaments, 1):
        nozzle = spec.filament_map[i - 1] if spec.filament_map and i - 1 < len(spec.filament_map) else min(i, n_noz)
        if loaded is None:
            gp = gui[i - 1] if i - 1 < len(gui) else None
            gc = gcols[i - 1] if i - 1 < len(gcols) else None
            if fs.loaded:
                if not gp:
                    problems.append(
                        f'filament {i}: "loaded" needs the printer or a GUI filament preset; neither is available ({unreachable or "no printer read"}). '
                        "Pin a preset, or run with the printer on"
                    )
                    sources.append(FilamentSource(i, fs.preset, fs.colour, "unresolved", "unresolved: no printer and no GUI preset"))
                    new.append(fs)
                    continue
                colour = fs.colour or gc
                new.append(replace(fs, preset=gp, colour=colour))
                sources.append(
                    FilamentSource(i, gp, colour, "gui", f"from the GUI's last-used preset (printer not read: {unreachable or 'skipped'})")
                )
            else:
                note = ""
                if gp and gp != fs.preset:
                    warnings.append(
                        f"filament {i}: the spec pins '{fs.preset}' but the GUI's last-used filament {i} is '{gp}'; the printer was not read"
                    )
                    note = f"; the GUI's last-used preset is '{gp}'"
                new.append(fs)
                sources.append(
                    FilamentSource(i, fs.preset, fs.colour, "spec", f"from the spec (printer not read: {unreachable or 'skipped'}){note}")
                )
            continue
        nz = loaded.nozzle(nozzle)
        tray = nz.tray if nz else None
        where = f"nozzle {nozzle}" + (f" ({nz.name})" if nz else "")
        read = f"read {loaded.read_at}"
        if fs.loaded:
            if tray is None:
                problems.append(f'filament {i}: "loaded", but {_absence(nz, where, read)}')
                sources.append(FilamentSource(i, fs.preset, fs.colour, "unresolved", f"unresolved: {_absence(nz, where, read)}"))
                new.append(fs)
                continue
            if tray.preset is None:
                problems.append(
                    f'filament {i}: "loaded", but no installed filament preset matches the spool in {where} '
                    f"({tray.label}: {tray.describe()}, {read}) for {spec.printer}; pin a preset"
                )
                sources.append(
                    FilamentSource(i, fs.preset, fs.colour, "unresolved", f"unresolved: {tray.label} {tray.describe()} has no preset")
                )
                new.append(fs)
                continue
            colour, cnote = _colour(i, fs, tray.colour, warnings, tray.label)
            new.append(replace(fs, preset=tray.preset, colour=colour))
            sources.append(
                FilamentSource(i, tray.preset, colour, "printer", f"from the printer ({tray.label}, {tray.describe()}, {read}){cnote}")
            )
            continue
        # A pinned preset: it must be compared with the spool, and the comparison must pass. A spool that cannot be
        # compared (nothing loaded, an unlisted slot, no installed preset for it) is as much a problem as a mismatch:
        # the project would still reach Studio with a filament the printer may not hold.
        if tray is None or tray.preset is None:
            why = (
                _absence(nz, where, read)
                if tray is None
                else f"no installed filament preset matches the spool in {where} ({tray.label}: {tray.describe()}, {read})"
            )
            if not allow_mismatch:
                problems.append(
                    f"filament {i}: the spec pins '{fs.preset}' but {why}; it cannot be compared with the spool. Fix the spool, or pass {MISMATCH_FLAG}"
                )
                new.append(fs)
                sources.append(FilamentSource(i, fs.preset, fs.colour, "spec", f"from the spec; NOT COMPARED: {why}"))
                continue
            warnings.append(f"filament {i}: the spec pins '{fs.preset}' but {why}; kept because {MISMATCH_FLAG} was passed")
            colour, cnote = _colour(i, fs, tray.colour if tray else None, warnings, where)
            new.append(replace(fs, colour=colour))
            sources.append(
                FilamentSource(i, fs.preset, colour, "spec (override)", f"from the spec (override {MISMATCH_FLAG}; {why}){cnote}")
            )
            continue
        if tray.preset != fs.preset:
            if not allow_mismatch:
                problems.append(
                    f"filament {i}: the spec pins '{fs.preset}' but {where} holds '{tray.preset}' ({tray.label}: {tray.describe()}, {read}); "
                    f'write "preset": "loaded" to take the spool, change the spool, or pass {MISMATCH_FLAG}'
                )
                new.append(fs)
                sources.append(
                    FilamentSource(i, fs.preset, fs.colour, "spec", f"from the spec; MISMATCH: {where} holds {tray.describe()} ({read})")
                )
                continue
            warnings.append(
                f"filament {i}: the spec pins '{fs.preset}' but {where} holds '{tray.preset}' ({tray.describe()}, {read}); kept because {MISMATCH_FLAG} was passed"
            )
            colour, cnote = _colour(i, fs, tray.colour, warnings, tray.label)  # the override waives the preset check, not the colour rule
            new.append(replace(fs, colour=colour))
            sources.append(
                FilamentSource(
                    i,
                    fs.preset,
                    colour,
                    "spec (override)",
                    f"from the spec (override {MISMATCH_FLAG}; {where} holds {tray.describe()}, {read}){cnote}",
                )
            )
            continue
        colour, cnote = _colour(i, fs, tray.colour, warnings, tray.label)
        new.append(replace(fs, colour=colour))
        sources.append(
            FilamentSource(
                i,
                fs.preset,
                colour,
                "printer",
                f"from the spec, and it matches the printer ({tray.label}, {tray.describe()}, {read}){cnote}",
            )
        )
    new_spec = replace(spec, filaments=tuple(new))
    if loaded is None:
        head = f"Filaments: WARNING printer not read ({unreachable or 'skipped'}); " + ", ".join(
            f"{s.slot} = {s.preset} {s.colour or 'colour unset'} ({s.source})" for s in sources
        )
    else:
        head = f"Filaments: from the printer at {loaded.read_at}: " + ", ".join(
            f"{s.slot} = {s.preset} {s.colour or 'colour unset'}"
            + (f" ({colour_name(s.colour)}, {s.source})" if s.source != "printer" else f" ({colour_name(s.colour)})")
            for s in sources
        )
    return FilamentResolution(
        new_spec, sources, head, problems, warnings, loaded.read_at if loaded else None, None if loaded else unreachable
    )
