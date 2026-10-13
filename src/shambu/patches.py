"""Command pattern over `threemf.Project`: each patch is an object with `apply()` and `describe()`.

A `PatchSet` is a Composite of patches. The build pipeline and the `patch` subcommand both build a
PatchSet, so a new kind of patch is one new class here and one flag in the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .threemf import Project


class Patch(Protocol):
    def apply(self, project: Project) -> None: ...
    def describe(self) -> str: ...


@dataclass(frozen=True)
class SetBedType:
    bed_type: str
    plate: int | None = None  # None = project default; plates with an override are updated too

    def apply(self, p: Project) -> None:
        p.set_bed_type(self.bed_type, self.plate)

    def describe(self) -> str:
        return f"bed type {self.bed_type}" + (f" on plate {self.plate}" if self.plate else " (project)")


@dataclass(frozen=True)
class SetBedTypeEverywhere:
    bed_type: str

    def apply(self, p: Project) -> None:
        p.set_bed_type(self.bed_type)
        for pl in p.summary()["plates"]:
            p.set_bed_type(self.bed_type, pl["id"])

    def describe(self) -> str:
        return f"bed type {self.bed_type} (project and every plate)"


@dataclass(frozen=True)
class SetFilamentColours:
    colours: tuple[str, ...]

    def apply(self, p: Project) -> None:
        p.set_filament_colours(list(self.colours))

    def describe(self) -> str:
        return f"filament colours {list(self.colours)}"


@dataclass(frozen=True)
class SetFilamentMap:
    extruders: tuple[int, ...]
    plate: int | None = None
    mode: str = "Manual"

    def apply(self, p: Project) -> None:
        p.set_filament_map(list(self.extruders), self.plate, self.mode)

    def describe(self) -> str:
        return f"filament -> extruder map {list(self.extruders)} ({self.mode}" + (
            f", plate {self.plate})" if self.plate else ", project; filament_extruder_variant updated)"
        )


@dataclass(frozen=True)
class SetObjectSetting:
    object_name: str
    key: str
    value: str

    def apply(self, p: Project) -> None:
        p.set_object_setting(self.object_name, self.key, self.value)

    def describe(self) -> str:
        return f"object '{self.object_name}': {self.key} = {self.value}"


@dataclass(frozen=True)
class ClearObjectSetting:
    object_name: str
    key: str

    def apply(self, p: Project) -> None:
        p.clear_object_setting(self.object_name, self.key)

    def describe(self) -> str:
        return f"object '{self.object_name}': remove {self.key}"


@dataclass(frozen=True)
class SetExtruder:
    object_name: str
    extruder: int
    part_name: str | None = None

    def apply(self, p: Project) -> None:
        p.set_extruder(self.object_name, self.extruder, self.part_name)

    def describe(self) -> str:
        return f"object '{self.object_name}'" + (f" part '{self.part_name}'" if self.part_name else "") + f": filament {self.extruder}"


@dataclass(frozen=True)
class SetHeightRange:
    object_name: str
    min_z: float
    max_z: float
    options: tuple[tuple[str, str], ...]  # layer_height is added when absent (see Project.set_height_range)

    def apply(self, p: Project) -> None:
        p.set_height_range(self.object_name, self.min_z, self.max_z, dict(self.options))

    def describe(self) -> str:
        return f"object '{self.object_name}': z {self.min_z}..{self.max_z} -> {dict(self.options)}"


@dataclass(frozen=True)
class AddCustomGcode:
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
class RenamePlate:
    plate: int
    name: str

    def apply(self, p: Project) -> None:
        p.rename_plate(self.plate, self.name)

    def describe(self) -> str:
        return f"plate {self.plate} named '{self.name}'"


@dataclass(frozen=True)
class RenameObject:
    old: str
    new: str

    def apply(self, p: Project) -> None:
        p.rename_object(self.old, self.new)  # no-op when 'old' is gone and 'new' exists once

    def describe(self) -> str:
        return f"object '{self.old}' renamed '{self.new}'"


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
        project.save(out or path)
        return done

    def describe(self) -> list[str]:
        return [p.describe() for p in self.patches]
