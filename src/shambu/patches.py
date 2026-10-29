"""Command pattern over `threemf.Project`: each patch is an object with `apply()` and `describe()`.

A `PatchSet` is a Composite of patches. The build pipeline and the `patch` subcommand both build a
PatchSet, so a new kind of patch is one new class here and one flag in the CLI.

Not here: preset I/O (a caller resolves any base preset values and passes them in), Studio runs, and the geometry and
filament-slot edits (`edits.py`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .threemf import DEFAULT_TOUCHES, PROJECT_SETTINGS, Project


class Patch(Protocol):
    """A change to a project. `touches` are the entry patterns it may change (`Project.save` refuses any other);
    `resolve_touches(project)` narrows them for that project; `verify(after, before)` re-opens the written archive.
    `PatchBase` gives the three optional members their defaults (Metadata/*, the declared set, no check)."""

    @property
    def touches(self) -> frozenset[str]: ...

    def apply(self, project: Project, /) -> None: ...
    def describe(self) -> str: ...
    def resolve_touches(self, project: Project, /) -> frozenset[str]: ...
    def verify(self, after: Project, before: Project | None = None, /) -> None: ...


class PatchBase:
    """The defaults of the optional `Patch` members, for every patch here and in edits.py."""

    touches: frozenset[str] = DEFAULT_TOUCHES

    def resolve_touches(self, project: Project) -> frozenset[str]:
        return self.touches

    def verify(self, after: Project, before: Project | None = None) -> None:
        return None


@dataclass(frozen=True)
class SetBedType(PatchBase):
    bed_type: str
    plate: int | None = None  # None = project default; plates with an override are updated too

    def apply(self, p: Project) -> None:
        p.set_bed_type(self.bed_type, self.plate)

    def describe(self) -> str:
        return f"bed type {self.bed_type}" + (f" on plate {self.plate}" if self.plate else " (project)")


@dataclass(frozen=True)
class SetBedTypeEverywhere(PatchBase):
    bed_type: str

    def apply(self, p: Project) -> None:
        p.set_bed_type(self.bed_type)
        for pl in p.summary()["plates"]:
            p.set_bed_type(self.bed_type, pl["id"])

    def describe(self) -> str:
        return f"bed type {self.bed_type} (project and every plate)"


@dataclass(frozen=True)
class SetPrintSequence(PatchBase):
    sequence: str  # "by layer" | "by object"
    plate: int | None = None

    def apply(self, p: Project) -> None:
        p.set_print_sequence(self.sequence, self.plate)

    def describe(self) -> str:
        return f"print sequence {self.sequence}" + (f" on plate {self.plate}" if self.plate else " (project)")


@dataclass(frozen=True)
class SetFilamentColours(PatchBase):
    colours: tuple[str, ...]

    def apply(self, p: Project) -> None:
        p.set_filament_colours(list(self.colours))

    def describe(self) -> str:
        return f"filament colours {list(self.colours)}"


@dataclass(frozen=True)
class SetFilamentMap(PatchBase):
    extruders: tuple[int, ...]
    plate: int | None = None
    mode: str = "Manual"

    def apply(self, p: Project) -> None:
        p.set_filament_map(list(self.extruders), self.plate, self.mode)

    def describe(self) -> str:
        return f"filament -> extruder map {list(self.extruders)} ({self.mode}" + (
            f", plate {self.plate})" if self.plate else ", project; filament_extruder_variant kept in block form)"
        )


@dataclass(frozen=True)
class RepairNozzleStats(PatchBase):
    """Make a CLI export re-sliceable by the CLI (reference 03 section "Re-slicing a CLI export"):
    - `extruder_nozzle_stats` `[]` or a `#0` count becomes `<nozzle_volume_type>#<extruder_max_nozzle_count>` per
      extruder (an empty stat exits -100 "No valid nozzle found");
    - `filament_extruder_variant` / `filament_self_index` become one block per filament (each filament's variants,
      from `filament_variants` when given, else the printer's variant list);
    - `filament_volume_map` and `filament_nozzle_map` get one entry per filament.
    Idempotent: a repaired file is left as it is."""

    filament_variants: tuple[tuple[str, ...], ...] = ()  # per filament slot, its preset's filament_extruder_variant

    def apply(self, p: Project) -> None:
        p.repair_nozzle_stats()
        p.repair_extruder_variants(self.filament_variants or None)

    def describe(self) -> str:
        return (
            "extruder_nozzle_stats(_new), filament_extruder_variant / filament_self_index blocks, "
            "filament_volume_map / filament_nozzle_map made re-sliceable (CLI --slice)"
        )


@dataclass(frozen=True)
class SetObjectSetting(PatchBase):
    object_name: str
    key: str
    value: str

    def apply(self, p: Project) -> None:
        p.set_object_setting(self.object_name, self.key, self.value)

    def describe(self) -> str:
        return f"object '{self.object_name}': {self.key} = {self.value}"


@dataclass(frozen=True)
class ClearObjectSetting(PatchBase):
    object_name: str
    key: str

    def apply(self, p: Project) -> None:
        p.clear_object_setting(self.object_name, self.key)

    def describe(self) -> str:
        return f"object '{self.object_name}': remove {self.key}"


@dataclass(frozen=True)
class SetExtruder(PatchBase):
    object_name: str
    extruder: int
    part_name: str | None = None

    def apply(self, p: Project) -> None:
        p.set_extruder(self.object_name, self.extruder, self.part_name)

    def describe(self) -> str:
        return f"object '{self.object_name}'" + (f" part '{self.part_name}'" if self.part_name else "") + f": filament {self.extruder}"


@dataclass(frozen=True)
class SetHeightRange(PatchBase):
    object_name: str
    min_z: float
    max_z: float
    options: tuple[tuple[str, str], ...]  # layer_height is added when absent (see Project.set_height_range)

    def apply(self, p: Project) -> None:
        p.set_height_range(self.object_name, self.min_z, self.max_z, dict(self.options))

    def describe(self) -> str:
        return f"object '{self.object_name}': z {self.min_z}..{self.max_z} -> {dict(self.options)}"


@dataclass(frozen=True)
class AddCustomGcode(PatchBase):
    plate: int
    top_z: float
    kind: str = "pause"  # pause | color_change | tool_change | custom | template
    extruder: int = 1
    colour: str = ""
    extra: str = ""

    def apply(self, p: Project) -> None:
        p.add_custom_gcode(self.plate, self.top_z, self.kind, self.extruder, self.colour, self.extra)

    def describe(self) -> str:
        what = {
            "pause": "pause",
            "color_change": f"colour change to filament {self.extruder}",
            "custom": f"custom G-code {self.extra!r}",
        }.get(self.kind, self.kind)
        return f"plate {self.plate}: {what} at z {self.top_z}"


@dataclass(frozen=True)
class RenamePlate(PatchBase):
    plate: int
    name: str

    def apply(self, p: Project) -> None:
        p.rename_plate(self.plate, self.name)

    def describe(self) -> str:
        return f"plate {self.plate} named '{self.name}'"


@dataclass(frozen=True)
class RenameObject(PatchBase):
    old: str
    new: str

    def apply(self, p: Project) -> None:
        p.rename_object(self.old, self.new)  # no-op when 'old' is gone and 'new' exists once

    def describe(self) -> str:
        return f"object '{self.old}' renamed '{self.new}'"


@dataclass(frozen=True)
class SetPresetBookkeeping(PatchBase):
    """One Studio preset slot (0 process, 1..N filaments, N+1 printer) named by its system preset `base`, with the keys
    Studio must keep on open, merged with those the slot already lists (`Project.set_preset_bookkeeping`). Keeps a
    slice: these keys never change the G-code, so only project_settings.config changes (the plate G-code and its MD5
    stay byte for byte; the MD5 covers the G-code only, bbs_3mf.cpp:6776-6800)."""

    slot: int
    base: str
    keys: tuple[str, ...] = ()
    touches: frozenset[str] = field(default=frozenset({PROJECT_SETTINGS}), init=False, repr=False, compare=False)

    def apply(self, p: Project) -> None:
        p.set_preset_bookkeeping(self.slot, self.base, self.keys)

    def describe(self) -> str:
        n = len(self.keys)
        listed = f" ({';'.join(sorted(self.keys))})" if self.keys else ""
        return f"preset slot {self.slot}: {self.base} + {n} override{'' if n == 1 else 's'}{listed}"


BaseValues = Mapping[str, Any] | None  # the resolved base process preset, or None when it is not in the Studio library


@dataclass(frozen=True)
class SetProcessSetting(PatchBase):
    """A process key in `project_settings.config`, listed in the process slot's diff list (`Project.set_process_setting`).
    `base_values`: the resolved base preset, which the caller resolves (this module does no preset I/O)."""

    key: str
    value: str
    base_values: BaseValues = field(default=None, hash=False, compare=False, repr=False)

    def apply(self, p: Project) -> None:
        p.set_process_setting(self.key, self.value, self.base_values)

    def describe(self) -> str:
        return f"process: {self.key} = {self.value} (different_settings_to_system[0] updated)"


@dataclass(frozen=True)
class UnsetProcessSetting(PatchBase):
    """A process key back to its base preset's value, removed from the process slot's diff list. The caller resolves
    `base_value` and `base_values` (this module does no preset I/O)."""

    key: str
    base_value: str | tuple[str, ...]
    base_values: BaseValues = field(default=None, hash=False, compare=False, repr=False)

    def apply(self, p: Project) -> None:
        p.clear_process_setting(self.key, self.base_value, self.base_values)

    def describe(self) -> str:
        v = ",".join(self.base_value) if isinstance(self.base_value, tuple) else self.base_value
        return f"process: {self.key} restored to the base value {v} (removed from different_settings_to_system[0])"


@dataclass
class PatchSet:
    """Composite: applies patches in order and reports each one.

    Every patch is idempotent, so applying the same PatchSet to its own output gives the same
    project: setters overwrite, a height range or custom G-code entry replaces the one at the
    same span or layer, and RenameObject is a no-op once the rename is done. `apply()` describes
    every patch in the set, including one that found nothing to change. The project is written
    atomically: on any error the input file stays as it was.
    """

    patches: list[Patch] = field(default_factory=list)

    def add(self, *patches: Patch) -> PatchSet:
        self.patches.extend(patches)
        return self

    def apply(self, path: str, out: str | None = None) -> list[str]:
        if not self.patches:
            return []
        project = Project(path)
        done = []
        for patch in self.patches:
            patch.apply(project)
            done.append(patch.describe())
        # The union of what the patches declare: a set of narrow patches is checked narrowly; any patch with the
        # default `Metadata/*` widens it to that.
        touches: frozenset[str] = frozenset().union(*(p.resolve_touches(project) for p in self.patches))
        project.save(out or path, touches)
        return done

    def describe(self) -> list[str]:
        return [p.describe() for p in self.patches]
