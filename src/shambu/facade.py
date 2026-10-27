"""`Bambu`: the one object a caller needs. Everything else is reachable through it.

Printer profiles (0.5.0): `printer()` builds the credential chain `[OneShotCredentials]` (a one-shot code the CLI read)
`+ [ProfileCredentials]` (the selected profile and the credentials file) `+ [StudioCredentials]`; with a profile it also
takes the profile's IP and TLS flag and scans the default-route /24 after the ARP table. `login()` reads the printer
once with the new code and only then commits the profile and the code; `forget()` removes a profile and its code.

What this module does not do: no constructor does I/O (the Studio binary, the preset tree and the credentials file
are opened on first use), nothing here prints, and nothing reads stdin. The CLI reads a one-shot code and sets
`printer_code`; the facade only passes it on and never stores it.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from . import conf, presets, recipes, runs
from . import printer as printer_mod
from . import vault as vault_mod
from .build import BuildResult, Listener, Pipeline, load_spec
from .edits import AddFilament, EditSet
from .errors import BambuError, ConfError, PrinterError, StudioError
from .loaded import FilamentResolution, reconcile
from .patches import PatchSet
from .printer import (
    ArpProbe,
    LoadedFilaments,
    OneShotCredentials,
    PrinterService,
    ProfileCredentials,
    Status,
    StudioCredentials,
    SubnetProbe,
    utc_iso,
)
from .profiles import UNKNOWN, Profile, ProfileError, ProfileSet
from .spec import Spec
from .studio import MeshInfo, StudioCLI
from .threemf import Project
from .vault import Vault, VaultError, safe_code

DEFAULT_MACHINE = "Bambu Lab X2D 0.4 nozzle"
TESTED_STUDIO = "02.08.02.61"  # the Studio build every rule in this package was reverse engineered and tested on
REPO = "https://github.com/Shavvimal/shambu"
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")  # used with fullmatch: "0.4.0\n" is not a version
_TAG_RE = re.compile(r"v\d+\.\d+\.\d+")


redact_ip = conf.redact_ip  # one detector: conf.py
log = logging.getLogger(__name__)
CODE_REFUSAL = "the access code must be 4 to 128 characters with no space, quote or backslash"
STUDIO_UNREADABLE = "Bambu Studio's config could not be read or has an unexpected shape"


def studio_login_values() -> tuple[str, str]:
    """(serial, code) of Bambu Studio's selected printer, for `login --from-studio`: one guarded read of its config.
    An unreadable file, bad JSON or a wrong shape is ConfError(STUDIO_UNREADABLE), a fixed text that never echoes the
    file; a missing file, no selected printer or no code for it keep their own ConfError."""
    try:
        doc = conf.load()
        if not isinstance(doc, dict) or not isinstance(doc.get("app", {}), dict) or not isinstance(doc.get("access_code", {}), dict):
            raise TypeError("shape")
        serial = conf.selected_serial(doc)
        if serial is not None and not isinstance(serial, str):
            raise TypeError("shape")
        if not serial:
            raise ConfError("no printer selected in Bambu Studio (app.user_last_selected_machine is empty)")
        code = conf.access_code(serial, doc)
        if not isinstance(code, str):
            raise TypeError("shape")
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        raise ConfError(STUDIO_UNREADABLE) from None
    return serial, code


def install_kind(direct_url: dict | None, installer: str | None = None) -> tuple[str, str | None]:
    """How this copy of shambu was installed, from its `direct_url.json` (PEP 610), and the commit when a VCS install
    names one. A git install names its tag in `vcs_info.requested_revision`; a folder install is a snapshot unless
    `dir_info.editable` is true. `installer` ("uv tool") leads the text only when the caller has proof of it."""
    lead = f"{installer}, " if installer else ""
    if not isinstance(direct_url, dict):
        return "other", None
    vcs = direct_url.get("vcs_info")
    if isinstance(vcs, dict):
        commit = str(vcs["commit_id"])[:7] if vcs.get("commit_id") else None
        rev = vcs.get("requested_revision")
        if not rev:
            return f"{lead}git (no tag)", commit
        return (f"{lead}git tag {rev}" if _TAG_RE.fullmatch(str(rev)) else f"{lead}git revision {rev}"), commit
    d = direct_url.get("dir_info")
    if isinstance(d, dict):
        return ("editable checkout" if d.get("editable") else f"{lead}local folder (snapshot)"), None
    return "other", None


def _installer() -> str | None:
    """ "uv tool" when this interpreter's environment holds uv's tool receipt (`uv-receipt.toml`), else None."""
    return "uv tool" if (Path(sys.prefix) / "uv-receipt.toml").is_file() else None


def _direct_url() -> dict | None:
    try:
        text = metadata.distribution("shambu").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def shambu_section(expected: str | None, direct_url: dict | None = None) -> dict:
    """doctor's first section. The version is `shambu.__version__`, never the installed metadata: an editable install
    keeps the version it had when it was first built (0.1.0 metadata under a 0.3.0 checkout, 2026-10-21)."""
    import shambu

    install, commit = install_kind(direct_url if direct_url is not None else _direct_url(), _installer())
    return {
        "version": shambu.__version__,
        "package_path": str(Path(shambu.__file__).resolve().parent),
        "install": install,
        "commit": commit,
        "expected": expected,
        "match": None if expected is None else expected == shambu.__version__,
    }


def install_command(version: str) -> str:
    return f'uv tool install --reinstall "shambu @ git+{REPO}@v{version}"'


@dataclass(frozen=True)
class LoginResult:
    """What `Bambu.login` stored: the profile name, the masked serial, the mode, the time of the verified read, whether
    the profile is the default and whether the code went into the credentials file."""

    name: str
    serial_masked: str
    mode: str
    verified: str
    default: bool
    code_stored: bool


def profile_section(ps: ProfileSet, profile: Profile | None, config_dir: str, code: str | None = None) -> dict:
    """doctor's profile keys for the config section: the selected profile (masked serial, no IP, and `code`: "stored"
    or "none" for its serial in credentials.json, never the value), the count and the dir."""
    p = None
    if profile is not None:
        p = {
            "name": profile.name,
            "default": ps.default == profile.name,
            "serial": conf.mask(profile.serial),
            "mode": profile.mode,
            "verified": profile.verified,
            "insecure": profile.insecure,
            "code": code,
        }
    return {"printer_profile": p, "profiles": len(ps.printers), "config_dir": config_dir}


@dataclass
class Bambu:
    studio: StudioCLI | None = None  # created on first use, so commands that never touch the binary work without it
    resolver: presets.PresetResolver | None = None  # created on first use: commands that need no presets work without a profile tree
    listeners: list[Listener] = field(default_factory=list)
    printer_ip: str | None = None  # `--ip` for the printer reads that check/build/doctor make
    printer_wait: float = 8.0
    printer_timeout: float | None = 15.0  # timeout for one whole printer read (discovery, connect, report); None: none
    printer_progress: Callable[[str, float], None] | None = field(default=None, repr=False)  # hears each read step
    printer_profile: str | None = None  # `--printer NAME`; None: the default profile, if any
    printer_serial: str | None = None  # `--serial`
    printer_code: str | None = field(default=None, repr=False)  # a one-shot code the CLI read; never stored
    printer_insecure: bool = False
    vault: Vault | None = None  # created on first use (`vault_store`): the credentials file
    _loaded_cache: dict[str, LoadedFilaments | BambuError] = field(default_factory=dict, init=False, repr=False)

    def presets(self) -> presets.PresetResolver:
        if self.resolver is None:
            self.resolver = presets.resolver()
        return self.resolver

    def studio_cli(self) -> StudioCLI:
        if self.studio is None:
            self.studio = StudioCLI()
        return self.studio

    def vault_store(self) -> Vault:
        if self.vault is None:
            self.vault = vault_mod.default_vault()  # looked up at use: the tests patch it
        return self.vault

    def selected_profile(self) -> Profile | None:
        """The profile `printer_profile` names, else the default one, else None. ConfError for an unknown name or a
        damaged printers.json. Loading registers every profile serial for scrubbing."""
        return conf.select_profile(self.printer_profile)

    # ---- environment
    def gui_machine_preset(self) -> str:
        """The GUI's current machine preset, else the X2D default."""
        try:
            return conf.summary().get("gui_machine_preset") or DEFAULT_MACHINE
        except BambuError:
            return DEFAULT_MACHINE

    def doctor(self, printer: bool = True, expected: str | None = None, progress: Callable[[str, float], None] | None = None) -> dict:
        """Every section, in order, each timed. A failing section holds `{"error": ...}` and the others still run."""
        timings: dict[str, float] = {}

        def step(name: str, t0: float) -> None:
            timings[name] = round(time.monotonic() - t0, 2)
            if progress is not None:
                progress(name, timings[name])

        t0 = time.monotonic()
        d: dict = {"shambu": shambu_section(expected)}
        try:
            st = self.studio_cli()
            v = st.version()
            d["studio"] = {"binary": str(st.binary), "version": v, "tested": TESTED_STUDIO, "match": v == TESTED_STUDIO}
            d["studio"]["running_pids"] = st.running_pids()
        except BambuError as e:  # version() turns an unreadable or bad plist into StudioError
            d["studio"] = {"error": f"{type(e).__name__}: {e}", "tested": TESTED_STUDIO}
        step("studio", t0)
        t0 = time.monotonic()
        try:
            d["profiles"] = [getattr(s, "label", str(s)) for s in self.presets().source.sources]
        except (BambuError, OSError, ValueError) as e:  # a missing or unreadable profile tree
            d["profiles"] = {"error": f"{type(e).__name__}: {e}"}
        step("profiles", t0)
        t0 = time.monotonic()
        try:
            d["config"] = conf.summary()
        except Exception as e:  # noqa: BLE001
            d["config"] = {"error": str(e)}
        try:
            ps = conf.load_profiles()
            prof = conf.select_profile(self.printer_profile, ps)
            d["config"].update(profile_section(ps, prof, str(conf.PRINTERS.parent), self._code_state(prof)))
        except BambuError as e:
            d["config"].update(printer_profile={"error": conf.scrub_text(f"{type(e).__name__}: {e}")}, profiles=None)
            d["config"]["config_dir"] = str(conf.PRINTERS.parent)
        step("config", t0)
        t0 = time.monotonic()
        machine_name = d["config"].get("gui_machine_preset") or DEFAULT_MACHINE
        try:
            m = self.presets().for_cli("machine", machine_name)
            d["machine"] = presets.machine_facts(m)
            d["machine"]["start_gcode_chars"] = len(m.get("machine_start_gcode", ""))
        except Exception as e:  # noqa: BLE001
            d["machine"] = {"error": str(e)}
        step("machine", t0)
        if printer:
            t0 = time.monotonic()
            outer = self.printer_progress

            def sub(name: str, dt: float) -> None:
                timings[f"printer.{name}"] = dt
                if progress is not None:
                    progress(f"printer {name}", dt)

            self.printer_progress = sub
            try:
                d["printer"] = self.loaded_filaments(machine_name).to_json()
            except BambuError as e:
                d["printer"] = {"error": redact_ip(f"{type(e).__name__}: {e}")}
            finally:
                self.printer_progress = outer
            step("printer", t0)
        else:
            d["printer"] = {"error": "skipped (--no-printer)"}
        d["recipes"] = [r.name for r in recipes.RECIPES]
        d["timings_s"] = timings
        return d

    def _code_state(self, profile: Profile | None) -> str | None:
        """ "stored" when credentials.json holds a code for the profile's serial, "none" when it does not, "unreadable"
        when the file cannot be read; None without a profile. The code itself never leaves the vault."""
        if profile is None:
            return None
        try:
            return "stored" if self.vault_store().get(profile.serial) is not None else "none"
        except VaultError:
            return "unreadable"

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
        probe_supports: bool = False,
        no_check: bool = False,
        no_probe: bool = False,
        probe_dir: str | Path | None = None,
    ) -> BuildResult:
        """`no_check` (with `do_slice`): a --no-check pass after a checked slice exits -101 or -100 (build.two_pass_slice)."""
        if not isinstance(spec, Spec):
            spec = load_spec(spec)
        pipeline = self.pipeline()  # a missing Studio binary is the environment error to report first
        if probe_dir is not None:  # sweep: one probe cache for every variant (its key holds orientation and settings)
            pipeline.probe_dir = Path(probe_dir).expanduser()
        res = self.resolve_filaments(spec, printer=printer, allow_mismatch=allow_filament_mismatch)
        kw: dict = {"no_check": True} if no_check else {}
        if no_probe:
            kw["no_probe"] = True
        return pipeline.build(
            spec,
            export=export,
            timeout=timeout,
            keep_going=keep_going,
            do_slice=do_slice,
            filaments=res,
            probe_supports=probe_supports,
            **kw,
        )

    def inspect(self, path: str | Path, features: bool = True) -> dict:
        """A project built elsewhere has no result.json, so `inspect` keeps the G-code parser for grams by feature.
        Each object carries its `position` (plate, min corner and size in plate-local mm, `edits.object_positions`)."""
        from . import edits

        pr = Project(str(path))
        s = pr.summary(features=features)
        pos = edits.object_positions(pr)
        for o in s["objects"]:
            o["position"] = pos.get(o["name"])
        return s

    def patch(self, path: str | Path, patch_set: PatchSet, out: str | Path | None = None) -> list[str]:
        return patch_set.apply(str(path), str(out) if out else None)

    # ---- project edits, slice and the escape hatch (runs.py does the Studio runs)
    def studio_or_none(self) -> StudioCLI | None:
        """The Studio adapter, or None when the binary is absent (the edits then work without its normalisation)."""
        try:
            return self.studio_cli()
        except StudioError:
            return None

    def add_filament_patch(self, path: str | Path, arg: str, allow_duplicate: bool = False) -> AddFilament:
        return runs.add_filament_patch(self.studio_or_none(), self.presets(), str(path), arg, allow_duplicate)

    def edit(self, path: str | Path, edits: EditSet, out: str | Path | None = None, verify: bool = True) -> list[str]:
        """Apply an EditSet; an added filament gets the verify slice on the temporary archive when Studio is present."""
        return runs.apply_edits(self.studio_or_none() if verify else None, str(path), edits, str(out) if out else None, verify)

    def slice_project(self, path: str | Path, out: str | Path | None = None, **kw) -> runs.SliceResult:
        return runs.slice_project(self.studio_cli(), self.presets(), str(path), str(out) if out else None, **kw)

    def studio_raw(self, raw: list[str], out_dir: str | Path = ".", timeout: int = 1200) -> dict:
        return runs.studio_raw(self.studio_cli(), raw, Path.cwd(), Path(out_dir), timeout)

    def open_in_studio(self, path: str | Path) -> None:
        """Opens the project for the user. With single_instance off this starts a second Studio process. StudioError
        when `open` fails or is missing (exit 3 at the CLI, not a traceback)."""
        try:
            subprocess.run(["open", "-a", "BambuStudio", str(Path(path).resolve())], check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            err = (e.stderr or b"").decode("utf-8", "replace").strip()[:200]
            raise StudioError(f"`open -a BambuStudio` exited {e.returncode}" + (f": {err}" if err else "")) from e
        except OSError as e:
            raise StudioError(f"`open -a BambuStudio` could not run ({e.strerror or e})") from e

    # ---- printer (read-only)
    def printer(
        self, ip: str | None = None, serial: str | None = None, wait: float | None = None, insecure: bool = False
    ) -> PrinterService:
        """Read-only printer access. `insecure=True` skips TLS verification: the access code then travels to an unverified endpoint.

        The credential chain is the one-shot code (when the CLI read one), then the selected profile and its stored
        code, then Studio's config. With a profile, its IP and TLS flag apply unless the caller gives others, and
        discovery adds the default-route /24 after the ARP table; the serial is left to the profile source."""
        profile = self.selected_profile()
        chain: list = [OneShotCredentials(self.printer_code)] if self.printer_code else []
        if profile is not None:
            chain.append(ProfileCredentials(profile, self.vault_store()))
        chain.append(StudioCredentials())
        kw: dict = {"discovery": (ArpProbe(), SubnetProbe())} if profile is not None else {}
        return PrinterService(
            ip or self.printer_ip or (profile.ip if profile is not None else None),
            serial or self.printer_serial,
            wait if wait is not None else self.printer_wait,
            insecure=insecure or self.printer_insecure or (profile.insecure if profile is not None else False),
            timeout_s=self.printer_timeout,
            on_step=self.printer_progress,
            sources=tuple(chain),
            profile=profile,
            **kw,
        )

    def login(
        self,
        *,
        ip: str | None = None,
        serial: str | None = None,
        code: str | None = None,
        from_studio: bool = False,
        name: str | None = None,
        make_default: bool = False,
        insecure: bool = False,
        mode: str | None = None,
        pin: str | None = None,
    ) -> LoginResult:
        """Read the printer once with `code`, then store the profile and the code. The CLI has checked the flags.

        Which printer: `from_studio` takes the serial and the code from Studio's config; else the selected profile
        (`printer_profile`, or the default when no `ip` is given) pins the serial and `ip` overrides its address; else
        `ip` names a new printer whose serial is `serial` or the CN of its certificate (they must agree). `pin` is the
        serial the CLI checked before its prompt: the profile is read again under the lock before the printer read and
        once more in the commit, and a profile whose serial is no longer `pin` is ConfError (nothing read or written);
        a supplied `serial` is never replaced by the profile's. The read must
        succeed before anything is written: a refused code (`LoginRefused`), a wrong host or a timeout writes nothing.
        The commit runs under the profiles lock; when printers.json cannot be written after the code was stored, the
        previous code (or none) is put back."""
        ps = conf.load_profiles()
        existing: Profile | None = None
        service_profile: Profile | None = None
        if from_studio:
            serial, code = studio_login_values()
            existing = ps.by_serial(serial)
        else:
            selected = None
            if self.printer_profile is not None or ip is None:  # read again, under the lock: the prompt may have taken minutes
                selected = conf.locked_profiles(lambda current: conf.select_profile(self.printer_profile, current))
            if selected is not None:
                self._check_pin(selected, pin)
                existing = service_profile = selected
                serial = serial or selected.serial
                ip = ip or selected.ip
                # `insecure` stays the flag as given: a refresh without --insecure turns the TLS check back on
            else:
                if not ip:
                    raise PrinterError("shambu printer login needs --ip for a new printer")
                if not insecure:
                    cn = printer_mod.certificate_serial(ip, self.printer_timeout)
                    if serial and cn.casefold() != serial.casefold():
                        raise PrinterError(f"the host at {ip} presents a certificate for printer {conf.mask(cn)}, not {conf.mask(serial)}")
                    serial = serial or cn
                if not serial:
                    raise PrinterError("--insecure cannot read the printer's certificate: give --serial")
                existing = ps.by_serial(serial)
        if code is None or not safe_code(code):
            raise ConfError(CODE_REFUSAL)
        if not serial:
            raise PrinterError("no printer serial: give --serial")
        conf.remember_secret(code)
        name = name or (existing.name if existing is not None else "printer")
        mode = mode or (existing.mode if existing is not None else "unknown")
        draft = Profile(name, serial, ip, mode, insecure, added=utc_iso(time.time()), verified=None)
        ps.upsert(draft, default=make_default, secrets=conf.sensitive_values())  # a bad name or a clash: before the read

        svc = PrinterService(
            ip,
            serial,
            self.printer_wait,
            discovery=(ArpProbe(), SubnetProbe()),
            insecure=insecure,
            timeout_s=self.printer_timeout,
            on_step=self.printer_progress,
            sources=(OneShotCredentials(code),),
            profile=service_profile,
        )
        svc.raw_report()
        resolved = svc.last_resolved  # the address the read went to: the given one, the profile's or discovery's
        read_ip = resolved.ip if resolved is not None else ip
        now = utc_iso(time.time())
        store = self.vault_store()

        def commit(current: ProfileSet) -> ProfileSet:
            if service_profile is not None:  # the profile must still be the printer that was read
                self._check_pin(current.get(service_profile.name), pin or serial, service_profile.name)
            old = current.by_serial(serial)
            p = Profile(name, serial, read_ip, mode, insecure, added=old.added if old is not None else now, verified=now)
            new = current.upsert(p, default=make_default, secrets=conf.sensitive_values())
            previous = store.get(serial)
            store.put(serial, code)
            try:
                conf.save_profiles(new)
            except BaseException:
                try:
                    if previous is None:
                        store.delete(serial)
                    else:
                        store.put(serial, previous)
                except VaultError as e:  # the write failure is the error to report; this one is logged
                    log.warning("could not restore the stored access code for %s (%s)", conf.mask(serial), type(e).__name__)
                raise
            return new

        saved = conf.locked_profiles(commit)
        return LoginResult(name, conf.mask(serial), mode, now, saved.default == name, True)

    def _check_pin(self, profile: Profile | None, pin: str | None, name: str | None = None) -> None:
        """ConfError when `profile` is gone or its serial is not `pin` (case-insensitive). The text holds the profile
        name only: no serial, raw or masked."""
        if pin is None:
            return
        if profile is None or profile.serial.casefold() != pin.casefold():
            shown = name or self.printer_profile or (profile.name if profile is not None else "default")
            raise ConfError(f"profile {shown} changed while login ran; retry")

    def forget(self, name: str, keep_code: bool = False) -> dict:
        """Remove the profile `name` (ProfileError when there is none) and, unless `keep_code` or another profile has
        the same serial, its code. A failure to remove the code after the profile is gone is reported in `note`."""
        store = None if keep_code else self.vault_store()

        def run(ps: ProfileSet) -> dict:
            new = ps.remove(name)  # ProfileError when no profile has that name
            p = ps.get(name)
            if p is None:
                raise ProfileError(UNKNOWN)
            conf.save_profiles(new)
            removed, note = False, None
            if store is not None and new.by_serial(p.serial) is None:
                try:
                    removed = store.delete(p.serial)
                except VaultError as e:
                    note = f"the profile is gone but the code could not be removed ({e})"
            return {"name": p.name, "serial_masked": conf.mask(p.serial), "code_removed": removed, "note": note}

        return conf.locked_profiles(run)

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
