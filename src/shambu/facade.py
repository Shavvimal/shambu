"""`Bambu`: the one object a caller needs. Everything else is reachable through it."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import conf, presets, recipes
from .build import BuildResult, Listener, Pipeline, load_spec
from .errors import BambuError
from .loaded import FilamentResolution, reconcile
from .patches import PatchSet
from .printer import LoadedFilaments, PrinterService, Status
from .spec import Spec
from .studio import MeshInfo, StudioCLI
from .threemf import Project

DEFAULT_MACHINE = "Bambu Lab X2D 0.4 nozzle"
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def redact_ip(text: str) -> str:
    """A printer error names the host it tried; the report file must not (CONTRIBUTING: never commit an IP address)."""
    return _IPV4.sub("<ip>", text)


@dataclass
class Bambu:
    studio: StudioCLI | None = None  # created on first use, so commands that never touch the binary work without it
    resolver: presets.PresetResolver | None = None  # created on first use: commands that need no presets work without a profile tree
    listeners: list[Listener] = field(default_factory=list)
    printer_ip: str | None = None  # `--ip` for the printer reads that check/build/doctor make
    printer_wait: float = 8.0
    _loaded_cache: dict[str, LoadedFilaments | BambuError] = field(default_factory=dict, init=False, repr=False)

    def presets(self) -> presets.PresetResolver:
        if self.resolver is None:
            self.resolver = presets.resolver()
        return self.resolver

    def studio_cli(self) -> StudioCLI:
        if self.studio is None:
            self.studio = StudioCLI()
        return self.studio

    # ---- environment
    def gui_machine_preset(self) -> str:
        """The GUI's current machine preset, else the X2D default."""
        try:
            return conf.summary().get("gui_machine_preset") or DEFAULT_MACHINE
        except BambuError:
            return DEFAULT_MACHINE

    def doctor(self, printer: bool = True) -> dict:
        st = self.studio_cli()
        d: dict = {"studio": {"binary": str(st.binary), "version": st.version(), "running_pids": st.running_pids()}}
        d["profiles"] = [getattr(s, "label", str(s)) for s in self.presets().source.sources]
        try:
            d["config"] = conf.summary()
        except Exception as e:  # noqa: BLE001
            d["config"] = {"error": str(e)}
        machine_name = d["config"].get("gui_machine_preset") or DEFAULT_MACHINE
        try:
            m = self.presets().for_cli("machine", machine_name)
            d["machine"] = presets.machine_facts(m)
            d["machine"]["start_gcode_chars"] = len(m.get("machine_start_gcode", ""))
        except Exception as e:  # noqa: BLE001
            d["machine"] = {"error": str(e)}
        if printer:
            try:
                d["printer"] = self.loaded_filaments(machine_name).to_json()
            except BambuError as e:
                d["printer"] = {"error": f"{type(e).__name__}: {e}"}
        else:
            d["printer"] = {"error": "skipped (--no-printer)"}
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

    def resolve_filaments(self, spec: Spec, *, printer: bool = True, allow_mismatch: bool = False) -> FilamentResolution:
        """The spec's filaments against the printer's trays (one read per machine preset, cached for this process).
        Printer unreachable or `printer=False`: the GUI presets from BambuStudio.conf stand in, with a warning."""
        loaded: LoadedFilaments | None = None
        why: str | None = "skipped (--no-printer)"
        if printer:
            cached = self._loaded_cache.get(spec.printer)
            if cached is None:
                try:
                    cached = self.loaded_filaments(spec.printer)
                except BambuError as e:
                    cached = e
                self._loaded_cache[spec.printer] = cached
            if isinstance(cached, BambuError):
                why = redact_ip(f"{type(cached).__name__}: {cached}")
            else:
                loaded, why = cached, None
        gui_presets: list[str] | None = None
        gui_colours = None
        if loaded is None:
            try:
                s = conf.summary()
                gui_presets, gui_colours = s.get("gui_filament_presets") or None, s.get("gui_filament_colours")
            except BambuError:
                pass
        n_ext = len(presets.machine_facts(self.presets().for_cli("machine", spec.printer))["extruders"])  # the pipeline's own count
        return reconcile(
            spec,
            loaded,
            gui_presets=gui_presets,
            gui_colours=gui_colours,
            allow_mismatch=allow_mismatch,
            unreachable=why,
            n_extruders=n_ext,
        )

    def build(
        self,
        spec: Spec | str | Path,
        *,
        export: bool = True,
        timeout: int = 1200,
        keep_going: bool = False,
        do_slice: bool = False,
        printer: bool = True,
        allow_filament_mismatch: bool = False,
    ) -> BuildResult:
        if not isinstance(spec, Spec):
            spec = load_spec(spec)
        pipeline = self.pipeline()  # a missing Studio binary is the environment error to report first
        res = self.resolve_filaments(spec, printer=printer, allow_mismatch=allow_filament_mismatch)
        return pipeline.build(spec, export=export, timeout=timeout, keep_going=keep_going, do_slice=do_slice, filaments=res)

    def inspect(self, path: str | Path, features: bool = True) -> dict:
        """A project built elsewhere has no result.json, so `inspect` keeps the G-code parser for grams by feature."""
        return Project(str(path)).summary(features=features)

    def patch(self, path: str | Path, patch_set: PatchSet, out: str | Path | None = None) -> list[str]:
        return patch_set.apply(str(path), str(out) if out else None)

    def open_in_studio(self, path: str | Path) -> None:
        """Opens the project for the user. With single_instance off this starts a second Studio process."""
        subprocess.run(["open", "-a", "BambuStudio", str(Path(path).resolve())], check=True)

    # ---- printer (read-only)
    def printer(
        self, ip: str | None = None, serial: str | None = None, wait: float | None = None, insecure: bool = False
    ) -> PrinterService:
        """Read-only printer access. `insecure=True` skips TLS verification: the access code then travels to an unverified endpoint."""
        return PrinterService(ip or self.printer_ip, serial, wait if wait is not None else self.printer_wait, insecure=insecure)

    def printer_status(self, ip: str | None = None) -> Status:
        return self.printer(ip).status()

    def loaded_filaments(
        self,
        printer_preset: str | None = None,
        ip: str | None = None,
        serial: str | None = None,
        wait: float | None = None,
        insecure: bool = False,
    ) -> LoadedFilaments:
        """The spools in each nozzle and slot (one `pushall`), with preset names resolved for `printer_preset`."""
        return self.printer(ip, serial, wait, insecure=insecure).filaments(self.presets(), printer_preset or self.gui_machine_preset())
