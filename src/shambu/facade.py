"""`Bambu`: the one object a caller needs. Everything else is reachable through it."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import conf, presets, recipes
from .build import BuildResult, Listener, Pipeline, load_spec
from .patches import PatchSet
from .printer import PrinterService, Status
from .spec import Spec
from .studio import MeshInfo, StudioCLI
from .threemf import Project


@dataclass
class Bambu:
    studio: StudioCLI | None = None  # created on first use, so commands that never touch the binary work without it
    resolver: presets.PresetResolver | None = None  # created on first use: commands that need no presets work without a profile tree
    listeners: list[Listener] = field(default_factory=list)

    def presets(self) -> presets.PresetResolver:
        if self.resolver is None:
            self.resolver = presets.resolver()
        return self.resolver

    def studio_cli(self) -> StudioCLI:
        if self.studio is None:
            self.studio = StudioCLI()
        return self.studio

    # ---- environment
    def doctor(self) -> dict:
        st = self.studio_cli()
        d: dict = {"studio": {"binary": str(st.binary), "version": st.version(), "running_pids": st.running_pids()}}
        d["profiles"] = [getattr(s, "label", str(s)) for s in self.presets().source.sources]
        try:
            d["config"] = conf.summary()
        except Exception as e:  # noqa: BLE001
            d["config"] = {"error": str(e)}
        try:
            m = self.presets().for_cli("machine", d["config"].get("gui_machine_preset") or "Bambu Lab X2D 0.4 nozzle")
            d["machine"] = presets.machine_facts(m)
            d["machine"]["start_gcode_chars"] = len(m.get("machine_start_gcode", ""))
        except Exception as e:  # noqa: BLE001
            d["machine"] = {"error": str(e)}
        d["recipes"] = [r.name for r in recipes.RECIPES]
        return d

    # ---- meshes and presets
    def info(self, path: str | Path) -> MeshInfo:
        return self.studio_cli().info(path)

    def list_presets(self, kind: str, contains: str | None = None, printer: str | None = None) -> list[dict]:
        return self.presets().list(kind, contains, printer)

    def resolve_preset(self, kind: str, name: str, overrides: dict | None = None, new_name: str | None = None) -> dict:
        return self.presets().for_cli(kind, name, overrides, new_name)

    # ---- projects
    def pipeline(self) -> Pipeline:
        return Pipeline(self.studio_cli(), self.presets(), listeners=self.listeners)

    def build(
        self, spec: Spec | str | Path, *, export: bool = True, timeout: int = 1200, keep_going: bool = False, do_slice: bool = False
    ) -> BuildResult:
        if not isinstance(spec, Spec):
            spec = load_spec(spec)
        return self.pipeline().build(spec, export=export, timeout=timeout, keep_going=keep_going, do_slice=do_slice)

    def inspect(self, path: str | Path, features: bool = True) -> dict:
        """A project built elsewhere has no result.json, so `inspect` keeps the G-code parser for grams by feature."""
        return Project(str(path)).summary(features=features)

    def patch(self, path: str | Path, patch_set: PatchSet, out: str | Path | None = None) -> list[str]:
        return patch_set.apply(str(path), str(out) if out else None)

    def open_in_studio(self, path: str | Path) -> None:
        """Opens the project for the user. With single_instance off this starts a second Studio process."""
        subprocess.run(["open", "-a", "BambuStudio", str(Path(path).resolve())], check=True)

    # ---- printer (read-only)
    def printer(self, ip: str | None = None, serial: str | None = None, wait: float = 8.0, insecure: bool = False) -> PrinterService:
        """Read-only printer access. `insecure=True` skips TLS verification: the access code then travels to an unverified endpoint."""
        return PrinterService(ip, serial, wait, insecure=insecure)

    def printer_status(self, ip: str | None = None) -> Status:
        return self.printer(ip).status()
