"""Command line: `shambu <command> ...`. Each subcommand is a small class registered with @command.

Output is human text by default and JSON with `--json` (before or after the command name), so an agent can parse
it. Exit codes: 0 ok, 1 a check or verification failed, the spec is invalid or a patch was refused, 2 bad usage, 3 an
environment error (binary, config, a file that cannot be written), 130 interrupted.

Printer profiles (0.5.0): `printer login` reads the printer once with a new access code and then stores the profile
(printers.json) and the code (credentials.json, 0600); `printer list` shows the profiles; `printer forget --printer
NAME` removes one and its code. Every command that reads the printer (check, build, sweep, doctor, printer status,
filaments, watch and raw) takes `--printer NAME` and the one-shot code flags `--access-code-file FILE` and
`--access-code-stdin` (the first line; this run only, never stored); `login` alone also prompts on a terminal.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from pathlib import Path

from . import __version__, conf, recipes
from .edits import (
    EditError,
    EditSet,
    MoveObject,
    ReplaceMesh,
    SetTower,
)
from .errors import BambuError, BuildError, ConfError, PresetError, PrinterError, ProjectError, StudioError
from .facade import Bambu, OpenResult
from .mesh import DEFAULT_THRESHOLD
from .patches import (
    AddCustomGcode,
    ClearObjectSetting,
    RenameObject,
    RenamePlate,
    RepairNozzleStats,
    SetBedType,
    SetBedTypeEverywhere,
    SetExtruder,
    SetFilamentColours,
    SetFilamentMap,
    SetHeightRange,
    SetObjectSetting,
    SetPrintSequence,
    SetProcessSetting,
    UnsetProcessSetting,
)
from .profiles import ProfileSet
from .report import checks_skipped_lines, estimate_lines
from .spec import Spec
from .studio import CliCommand
from .threemf import Project

log = logging.getLogger("shambu")
_REGISTRY: dict[str, Command] = {}


def command(name: str, help_text: str) -> Callable:
    def deco(cls):
        cls.name, cls.help = name, help_text
        _REGISTRY[name] = cls()
        return cls

    return deco


class Command:
    name = ""
    help = ""

    def configure(self, p: argparse.ArgumentParser) -> None: ...
    def run(self, a: argparse.Namespace, app: Bambu, out: Output) -> int:
        raise NotImplementedError


class Output:
    """The one way a command prints its result. Every string value of the document (recursively; keys, numbers and
    booleans untouched) and every text line is scrubbed of every known code and raw serial (`_scrub_values`) before it
    is printed. A masked serial and an IP are not in that list, so `printer list` and `discover` still show them."""

    def __init__(self, as_json: bool):
        self.as_json = as_json

    def emit(self, data, text_lines: list[str] | None = None) -> None:
        values = _scrub_values()
        doc = _scrub_tree(_jsonable(data), values)
        if self.as_json:
            print(json.dumps(doc, indent=1, default=str))
        else:
            for line in [conf.scrub_text(x, values) for x in text_lines] if text_lines is not None else _default_lines(doc):
                print(line)


def _scrub_tree(x, values: list[str]):
    """`x` with `conf.scrub_text` applied to every str value in it, through dicts and lists. Keys and other values stay,
    so a code equal to `true` or to a key name cannot break the document."""
    if isinstance(x, dict):
        return {k: _scrub_tree(v, values) for k, v in x.items()}
    if isinstance(x, list):
        return [_scrub_tree(v, values) for v in x]
    if isinstance(x, str):
        return conf.scrub_text(x, values)
    return x


def usage(out: Output, msg: str) -> int:
    """A usage error: one error document (or one line) and exit code 2. `Output.emit` scrubs the message."""
    out.emit({"error": msg}, [msg])
    return 2


METRES_WARNING = (
    "the whole mesh is under 1 mm; it is probably in metres. `shambu rotate FILE -o DIR --convert-unit` writes a mm copy "
    '(Studio\'s own unit fix), and "orientation": "auto" on a spec part converts it during the build.'
)
THRESHOLD_HELP = "overhang angle from vertical that needs support (default 60, Studio's own; 45 is the cautious rule)"


def load_spec_or_error(path: str, out: Output) -> Spec | int:
    """The spec, or exit code 1 after one error document: a bad spec (unreadable JSON, unknown recipe, missing
    key) is the user's input failing a check, not an environment error (3)."""
    from .build import load_spec

    try:
        return load_spec(path)
    except BuildError as e:
        msg = f"BuildError: {e}"
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as e:
        msg = f"BuildError: {path}: bad spec ({type(e).__name__}: {e})"
    out.emit({"error": msg}, [msg])
    return 1


def _jsonable(x):
    if is_dataclass(x) and not isinstance(x, type):
        return {k: _jsonable(v) for k, v in asdict(x).items() if k != "raw"}
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, Path):
        return str(x)
    return x


def _default_lines(data) -> list[str]:
    if isinstance(data, str):
        return [data]
    if isinstance(data, list) and all(isinstance(d, str) for d in data):
        return data
    return json.dumps(_jsonable(data), indent=1, default=str).splitlines()


# ---------------------------------------------------------------------------- commands
def _printer_flags(p: argparse.ArgumentParser) -> None:
    """The printer read that check, build, sweep and doctor make before resolving a spec's filaments."""
    p.add_argument("--no-printer", action="store_true", help='do not read the printer; take the GUI presets for "loaded" filaments (WARN)')
    p.add_argument("--ip", dest="printer_ip", help="the printer's IP when LAN discovery does not find it")
    _profile_flags(p)


def _profile_flags(p: argparse.ArgumentParser, serial: bool = True) -> None:
    """The printer profile and the one-shot code flags every command that reads the printer takes."""
    p.add_argument("--printer", dest="printer_profile", metavar="NAME", help="the printer profile to read (default: the default profile)")
    if serial:
        p.add_argument("--serial", help="the printer's serial (default: the profile's, else Bambu Studio's selected printer)")
    p.add_argument("--access-code-file", metavar="FILE", help="read the LAN access code from the first line of FILE (this run only)")
    p.add_argument("--access-code-stdin", action="store_true", help="read the LAN access code from the first line of stdin (this run only)")


ACCESS_CODE_PROMPT = "LAN access code (not shown): "
NO_CODE = "no access code: give --access-code-stdin or --access-code-file, or run login on a terminal"
UNSAFE_CODE = "the access code must be 4 to 128 characters with no space, quote or backslash"


def read_access_code(a: argparse.Namespace, out: Output) -> str | int:
    """The LAN access code from `--access-code-file` (first line), `--access-code-stdin` (first line) or a no-echo
    prompt on a terminal; else a usage error (exit 2). The only read of stdin in shambu. A prompt that would echo
    (`getpass.GetPassWarning`) is a usage error too. The code is registered with `conf.remember_secret` and never printed."""
    import getpass
    import warnings

    from .vault import safe_code

    if a.access_code_file and a.access_code_stdin:
        return usage(out, "give one of --access-code-file and --access-code-stdin, not both")
    if a.access_code_file:
        try:
            with open(a.access_code_file, encoding="utf-8") as f:
                text = f.readline()
        except (OSError, ValueError) as e:
            return usage(
                out, f"--access-code-file {a.access_code_file}: cannot read it ({getattr(e, 'strerror', None) or type(e).__name__})"
            )
    elif a.access_code_stdin:
        text = sys.stdin.readline()
    elif sys.stdin.isatty() and sys.stderr.isatty():
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                text = getpass.getpass(ACCESS_CODE_PROMPT)
            except getpass.GetPassWarning:
                return usage(out, "this terminal cannot hide the access code: give --access-code-stdin or --access-code-file")
            except EOFError:
                return usage(out, NO_CODE)
    else:
        return usage(out, NO_CODE)
    code = text.strip()
    if not code:
        return usage(out, "the access code is empty")
    if not safe_code(code):
        return usage(out, UNSAFE_CODE)
    conf.remember_secret(code)
    return code


def load_profiles_named() -> ProfileSet:
    """`conf.load_profiles()`, with a damaged printers.json named: ConfError("<path>: <REFUSAL>; fix or remove the
    file"). The path is the only added text; the file's content never appears."""
    from .profiles import REFUSAL

    try:
        return conf.load_profiles()
    except ConfError:  # REFUSAL; a DataFileError (cannot read it) already names the file
        raise ConfError(f"{conf.PRINTERS}: {REFUSAL}; fix or remove the file") from None


def _use_printer(
    a: argparse.Namespace, app: Bambu, out: Output, *, serial: str | None, insecure: bool = False, doctor: bool = False
) -> int | None:
    """Set the printer selection on `app` before any network step: the profile (an unknown name is a usage error, exit
    2), the serial, the TLS flag and a one-shot code when a code flag is set. None when the read can go ahead, else the
    exit code. A damaged printers.json is an environment error (exit 3) that names the file, except for `doctor`: it
    skips the file when no --printer and no code flag is given, and otherwise lets its config section report the damage,
    so doctor never stops on it."""
    app.printer_profile = a.printer_profile
    app.printer_serial = serial
    app.printer_insecure = insecure
    has_code_flag = bool(a.access_code_file or a.access_code_stdin)
    if not (doctor and a.printer_profile is None and not has_code_flag):
        try:
            ps: ProfileSet | None = load_profiles_named()
        except ConfError:
            if not doctor:
                raise
            ps = None
        if ps is not None:
            try:
                conf.select_profile(a.printer_profile, ps)
            except ConfError as e:
                return usage(out, str(e))
    if has_code_flag:
        code = read_access_code(a, out)
        if isinstance(code, int):
            return code
        app.printer_code = code
    return None


def _mismatch_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--allow-filament-mismatch",
        action="store_true",
        help="keep a pinned filament preset that differs from the spool in that nozzle (otherwise a FAIL); the report records it",
    )


def _probe_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--probe-supports",
        action="store_true",
        help="slice each supported part alone (cached under <outdir>/.probe) and use its measured support spread per side "
        "instead of the estimate (tree 0.22 x height, 8..26 mm, swept from the box; normal 5 mm)",
    )


def _no_probe_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--no-probe",
        action="store_true",
        help="keep the estimated support spread even when packing with it splits a plate or leaves no tower spot "
        "(by default shambu then measures the supported parts with a cached solo slice)",
    )


def printer_lines(d: dict, config: dict) -> list[str]:
    """Doctor's 'loaded on the printer' section next to the GUI presets, with a MISMATCH line per differing slot."""
    if "error" in d:
        return [f"loaded on the printer: not read ({d['error']})"]
    lines = [f"loaded on the printer (read {d['read_at']}, presets for {d.get('printer_preset')}):"]
    gui = list(config.get("gui_filament_presets") or [])
    for z in d["nozzles"]:
        t = z["tray"]
        if t is None:
            lines.append(f"  nozzle {z['nozzle']} ({z['name']}): nothing loaded")
            continue
        lines.append(
            f"  nozzle {z['nozzle']} ({z['name']}): {t['label']}: {t['material'] or '?'} {t['colour'] or ''} ({t['preset_id'] or 'no id'}) "
            f"-> {t['preset'] or 'no installed preset matches'}"
        )
        g = gui[z["nozzle"] - 1] if z["nozzle"] - 1 < len(gui) else None
        if t["preset"] and g and g != t["preset"]:
            lines.append(
                f"  MISMATCH: the GUI's last-used filament {z['nozzle']} is '{g}', the spool in nozzle {z['nozzle']} is '{t['preset']}'"
            )
    return lines


def shambu_lines(d: dict) -> list[str]:
    """doctor's first line: this copy's version and install, against the version the skill text expects."""
    from .facade import install_command

    head = f"shambu {d['version']} ({d['install']}) at {d['package_path']}"
    if d.get("expected") is None:
        return [f"{head}; no expected version given (--expect-version)"]
    if d.get("match"):
        return [f"{head}; skill expects {d['expected']}: OK"]
    return [
        f"{head}; skill expects {d['expected']}: MISMATCH",
        f"Fix: {install_command(d['expected'])}   (or /shambu:setup update)",
    ]


def profile_lines(c: dict) -> list[str]:
    """doctor's printer profile line; none when no profile is selected (the 0.4.0 lines stay as they were)."""
    p = c.get("printer_profile")
    if p is None:
        return []
    if "error" in p:
        return [f"printer profile: {p['error']}"]
    when = _hhmm(p.get("verified"))
    tls = "TLS check off (--insecure)" if p["insecure"] else "TLS verified"
    return [
        f"printer profile {p['name']}{' (default)' if p['default'] else ''}: serial {p['serial']}, mode {p['mode']}, "
        f"last login {when}, {tls}, code: {p.get('code') or 'none'}; {c.get('profiles')} profile(s) in {c.get('config_dir')}"
    ]


def studio_lines(st: dict) -> list[str]:
    if "error" in st:
        return [f"Bambu Studio: {st['error']} (tested {st['tested']})"]
    pids = st["running_pids"]
    shown = "unknown (pgrep failed)" if pids is None else (pids or "none")
    lines = [f"Bambu Studio {st['version']} (tested {st['tested']}) at {st['binary']} (running pids: {shown})"]
    if not st["match"]:
        lines.append(f"WARN: Bambu Studio {st['version']} is not the tested {st['tested']}; other 02.08.x builds work but are untested")
    return lines


@command(
    "doctor", "Check the shambu version, the Studio install, profiles, config, the X2D machine facts and the spools loaded on the printer"
)
class Doctor(Command):
    def configure(self, p):
        _printer_flags(p)
        p.add_argument(
            "--expect-version",
            metavar="X.Y.Z",
            help="the shambu version the skill text expects; a different installed version is exit 3 (after every section prints)",
        )
        p.add_argument(
            "--printer-timeout",
            type=float,
            default=15.0,
            metavar="S",
            help="seconds for the whole printer read: discovery, connect and report (default 15)",
        )

    def run(self, a, app, out):
        from .facade import VERSION_RE

        if a.expect_version is not None and not VERSION_RE.fullmatch(a.expect_version):
            return usage(out, f"--expect-version {a.expect_version!r}: give X.Y.Z, for example 0.4.0")
        if not (math.isfinite(a.printer_timeout) and 0 < a.printer_timeout < threading.TIMEOUT_MAX):
            return usage(out, f"--printer-timeout {a.printer_timeout:g}: give a finite number of seconds above 0")
        app.printer_ip = a.printer_ip
        app.printer_timeout = a.printer_timeout
        app.printer_profile = a.printer_profile
        if not a.no_printer:
            stop = _use_printer(a, app, out, serial=a.serial, doctor=True)
            if stop is not None:
                return stop
        d = app.doctor(
            printer=not a.no_printer,
            expected=a.expect_version,
            progress=lambda name, dt: print(f"doctor: {name} {dt:.1f} s", file=sys.stderr, flush=True),
        )
        lines = shambu_lines(d["shambu"]) + studio_lines(d["studio"])
        prof = d["profiles"]
        lines.append("profile sources: " + ("; ".join(prof) if isinstance(prof, list) else prof.get("error", "?")))
        c = d.get("config", {})
        lines.append(
            f"GUI presets: machine {c.get('gui_machine_preset')}, process {c.get('gui_process_preset')}, filaments {c.get('gui_filament_presets')}, bed {c.get('gui_bed_type')}"
        )
        lines.append(
            f"selected printer {c.get('selected_printer_serial')}, access code stored: {c.get('has_access_code')}, single_instance {c.get('single_instance')}"
        )
        lines += profile_lines(c)
        m = d.get("machine", {})
        if "bed" in m:
            lines.append(
                f"machine {m['name']}: bed {m['bed']['x'][1]:.0f} x {m['bed']['y'][1]:.0f} x {m['z_max']:.0f}; start G-code {m['start_gcode_chars']} chars"
            )
            for e in m["extruders"]:
                lines.append(
                    f"  extruder {e['index']} ({e['type']}, {e['nozzle']} mm): x {e['x'][0]}..{e['x'][1]}, z up to {e['z_max']:.0f}"
                )
        elif "error" in m:
            lines.append(f"machine: {m['error']}")
        lines += printer_lines(d.get("printer", {}), c)
        lines.append("recipes: " + ", ".join(d["recipes"]))
        lines.append("timings: " + ", ".join(f"{k} {v:.1f} s" for k, v in d["timings_s"].items()))
        out.emit(d, lines)
        if d["shambu"]["match"] is False or "error" in d["studio"]:
            return 3  # a version mismatch or no Studio binary: the environment, after every section printed
        return 0


@command("info", "Measure STL/OBJ/3MF files with Studio's --info")
class Info(Command):
    def configure(self, p):
        p.add_argument("files", nargs="+")

    def run(self, a, app, out):
        app.studio_cli()  # a missing binary is an environment error (exit 3), not a bad file
        entries = []
        lines = []
        failed = False
        for f in a.files:
            try:
                i = app.info(f)
            except BambuError as e:  # one bad file must not hide the others
                msg = f"{type(e).__name__}: {e}"
                entries.append({"file": f, "error": msg})
                lines.append(f"{f}: ERROR {msg}")
                failed = True
                continue
            warnings = [METRES_WARNING] if max(i.size) < 1.0 else []
            entries.append({**_jsonable(i), "warnings": warnings})
            lines += [i.describe()] + [f"  WARNING: {w}" for w in warnings]
            failed = failed or not i.manifold
        out.emit(entries, lines)
        return 1 if failed else 0


@command("presets", "List or resolve Bambu presets (machine | process | filament)")
class Presets(Command):
    def configure(self, p):
        sub = p.add_subparsers(dest="action", required=True)
        ls = sub.add_parser("list")
        ls.add_argument("kind", choices=("machine", "process", "filament"))
        ls.add_argument("--contains")
        ls.add_argument("--printer", default="Bambu Lab X2D 0.4 nozzle", help="filter by compatible_printers ('' for none)")
        rs = sub.add_parser("resolve", help="flatten inherits + include into one CLI-ready JSON")
        rs.add_argument("kind", choices=("machine", "process", "filament"))
        rs.add_argument("name")
        rs.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
        rs.add_argument("--name", dest="new_name")
        rs.add_argument("-o", "--out")
        ks = sub.add_parser("keys", help="setting keys and current values of a resolved preset")
        ks.add_argument("kind", choices=("machine", "process", "filament"))
        ks.add_argument("name")
        ks.add_argument("--grep")

    def run(self, a, app, out):
        if a.action == "list":
            rows = app.list_presets(a.kind, a.contains, a.printer or None)
            out.emit(
                rows,
                [
                    f"{r['name']}"
                    + (f"  (inherits {r['inherits']})" if r["inherits"] else "")
                    + (f"  [{r['from']}]" if r["from"] != "system" else "")
                    for r in rows
                ],
            )
            return 0
        overrides = dict(kv.split("=", 1) for kv in getattr(a, "set", []))
        d = app.resolve_preset(a.kind, a.name, overrides or None, getattr(a, "new_name", None))
        if a.action == "keys":
            items = sorted((k, v) for k, v in d.items() if not a.grep or a.grep.lower() in k.lower())
            out.emit(dict(items), [f"{k} = {v}" for k, v in items])
            return 0
        if a.out:
            Path(a.out).write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
            out.emit(
                {"written": a.out, "keys": len(d), "name": d["name"]}, [f"{a.out}: {len(d)} keys, name {d['name']!r}, from {d['from']}"]
            )
        else:
            print(json.dumps(d, indent=1, ensure_ascii=False))
        return 0


@command("recipes", "List the named print recipes")
class Recipes(Command):
    def configure(self, p):
        p.add_argument("--scope", choices=recipes.SCOPES)

    def run(self, a, app, out):
        out.emit([_jsonable(r) for r in recipes.RECIPES if not a.scope or r.scope == a.scope], recipes.describe(a.scope))
        return 0


@command("spec", "Print an example spec or validate one")
class SpecCmd(Command):
    def configure(self, p):
        p.add_argument("file", nargs="?", help="spec JSON to validate; omit for an example")

    def run(self, a, app, out):
        if not a.file:
            example = (sys.modules[Spec.__module__].__doc__ or "").split("JSON shape:", 1)[1].strip()
            out.emit({"example": example}, [example])
            return 0
        s = load_spec_or_error(a.file, out)
        if isinstance(s, int):
            return s
        errs = s.validate()
        warns = list(s.warnings)
        out.emit(
            {"ok": not errs, "problems": errs, "warnings": warns},
            ["spec ok" if not errs else "spec problems:"] + ["- " + e for e in errs] + ["WARNING: " + w for w in warns],
        )
        return 0 if not errs else 1


@command("orient", "Score orientations of an STL: bed contact, overhang shadow, height; optionally write the best as a new STL")
class Orient(Command):
    def configure(self, p):
        p.add_argument("file")
        p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help=THRESHOLD_HELP)
        p.add_argument("--top", type=int, default=12, help="largest flat faces to try, on top of as-is and the six axis faces (default 12)")
        p.add_argument("--write", metavar="DIR", help="write the best orientation as <DIR>/<stem>__<tag>.stl")
        p.add_argument(
            "--pick",
            type=int,
            default=1,
            help="with --write: which row of the table to write (1 = best); with --native, a row of Studio's table (1 keeps its export)",
        )
        p.add_argument(
            "--native",
            action="store_true",
            help="print Studio's own auto-orient table (--orient 1) instead of shambu's; with --write, keep Studio's export in DIR",
        )

    def run(self, a, app, out):
        from . import mesh as M

        if a.native:
            return self._native(a, app, out)
        facets = M.read_stl(a.file)
        if not facets:
            return usage(out, f"{a.file}: the STL has no facets")
        cands = M.candidates(facets, a.threshold, a.top)
        summary = M.Summary(Path(a.file), len(facets), len(M.split_bodies(facets)), cands)
        lines = [f"{a.file}: {len(facets)} facets, {summary.bodies} body(ies); overhang threshold {a.threshold:g} deg from vertical", ""]
        warnings = [METRES_WARNING] if M.looks_like_metres(facets) else []
        for w in warnings:
            lines += [f"WARNING: {w}", ""]
        lines += M.describe_candidates(cands)
        lines += [
            "",
            'spec: add "lay_on_face": [nx, ny, nz] (and optionally "rotate": [x, y, z]) to the part; `shambu sweep --orientations <part>` slices the best few.',
        ]
        data = {**summary.to_json(), "warnings": warnings}
        if a.write:
            c = cands[max(0, min(len(cands), a.pick) - 1)]
            path = M.oriented_copy(a.file, M.Orientation(lay_on_face=c.normal), a.write)
            data["written"] = str(path)
            lines.append(f"written: {path} ({c.label})")
        out.emit(data, lines)
        return 0

    @staticmethod
    def _native(a, app, out) -> int:

        from . import mesh as M
        from .sweep import unit_scale

        src = Path(a.file)
        if not src.is_file():
            return usage(out, f"{a.file}: no such file")
        if a.pick < 1:
            return usage(out, f"--pick {a.pick}: rows start at 1")
        st = app.studio_cli()
        with tempfile.TemporaryDirectory(prefix="shambu-orient-") as tmp:
            res = st.orient([src], Path(a.write) if a.write and a.pick == 1 else Path(tmp))  # row N > 1 is baked below
            k = unit_scale(M.read_stl(src), M.read_stl(res.files[0])) if a.write and a.pick > 1 and res.files else 1.0
        best = res.best
        cands = res.candidates
        if a.pick > len(cands):
            return usage(out, f"--pick {a.pick}: Studio's table has {len(cands)} row(s)")
        data: dict = {
            "path": str(src),
            "source": "studio",
            "seconds": round(res.seconds, 2),
            "best": best.to_json() if best else None,
            "candidates": [c.to_json() for c in cands],
        }
        lines = [
            f"{a.file}: Studio auto-orient, {len(cands)} candidates sorted by unprintability (lower is better); {res.seconds:.2f} s",
            "",
            f"{'#':>2} {'vector (up)':28s} {'overhang':>9} {'bottom':>8} {'bot_hull':>8} {'contour':>8} {'area_laf':>8} {'area_prj':>8} "
            f"{'unprint':>9}  lay_on_face [nx, ny, nz]",
        ]

        def vec(v) -> str:
            return "[" + ", ".join(f"{x:.4f}" for x in v) + "]"

        for i, c in enumerate(cands, 1):
            is_best = best is not None and all(abs(x - y) < 1e-3 for x, y in zip(c.vector, best.vector, strict=True))
            lines.append(
                f"{i:>2} {vec(c.vector):28s} {c.overhang:9.1f} {c.bottom:8.1f} {c.bottom_hull:8.1f} {c.contour:8.1f} {c.area_laf:8.1f} "
                f"{c.area_projected:8.1f} {c.unprintability:9.1f}  {vec(c.lay_on_face)}" + ("  BEST" if is_best else "")
            )
        lines += [
            "",
            'spec: "orientation": "auto" lets Studio pick at build time; "lay_on_face": <a row\'s lay_on_face> fixes one row. '
            "`shambu sweep --orientations <part> --source studio` slices the best few.",
        ]
        if a.write and a.pick == 1:
            data["written"] = str(res.files[0])
            lines.append(f"written: {res.files[0]} (Studio's best, unit converted)")
        elif a.write:
            # Row N > 1: bake its lay_on_face into the file (unit-converted first, as Studio's export is)
            c = cands[a.pick - 1]
            base = src
            try:
                if k != 1.0:
                    base = M.write_stl(Path(a.write) / f"{src.stem}__mm.stl", M.scale(M.read_stl(src), k), f"{src.stem} mm")
                path = M.oriented_copy(base, M.from_studio_vector(c.vector), a.write)
            except OSError as e:
                raise StudioError(f"cannot write row {a.pick} into {a.write}: {e}") from e
            data["written"] = str(path)
            data["picked"] = {"row": a.pick, **c.to_json(), **({"unit_scale": k} if k != 1.0 else {})}
            lines.append(f"written: {path} (row {a.pick}, lay_on_face {vec(c.lay_on_face)}" + (f", scaled by {k:g})" if k != 1.0 else ")"))
        out.emit(data, lines)
        return 0


@command("rotate", "Write a rotated copy of an STL (lay a face on the bed and/or Euler angles), dropped onto z = 0")
class Rotate(Command):
    def configure(self, p):
        p.add_argument("file")
        p.add_argument("-o", "--outdir", required=True)
        p.add_argument("--lay-on-face", metavar="NX,NY,NZ", help="outward normal of the face to put on the bed")
        p.add_argument("--rotate", metavar="X,Y,Z", help="degrees about X, then Y, then Z (applied after --lay-on-face)")
        unit = p.add_mutually_exclusive_group()
        unit.add_argument(
            "--scale",
            type=float,
            default=1.0,
            help="multiply every coordinate first, e.g. 1000 for a mesh in metres (alone: scale and drop)",
        )
        unit.add_argument(
            "--convert-unit",
            action="store_true",
            help="let Studio detect metres or inches and convert to mm first (its --convert-unit); alone: convert and drop",
        )

    def run(self, a, app, out):
        from . import mesh as M

        d: dict = {}
        try:
            if a.lay_on_face:
                d["lay_on_face"] = [float(v) for v in a.lay_on_face.split(",")]
            if a.rotate:
                d["rotate"] = [float(v) for v in a.rotate.split(",")]
            o = M.Orientation.from_json(d)
        except (ValueError, BuildError) as e:
            return usage(out, f"bad --lay-on-face or --rotate: {e}")
        if o.is_identity and a.scale == 1.0 and not a.convert_unit:
            return usage(out, "nothing to do: give --lay-on-face, --rotate, --scale or --convert-unit")
        if a.scale <= 0:
            return usage(out, "--scale must be positive")
        src = Path(a.file)
        note = ""
        if a.convert_unit:  # Studio's unit fix first, so the oriented copy (and its hash) start from mm
            import tempfile

            if not src.is_file():
                return usage(out, f"{a.file}: no such file")
            before = M.bbox(M.read_stl(src))
            with tempfile.TemporaryDirectory(prefix="shambu-unit-") as tmp:
                converted = M.read_stl(app.studio_cli().normalise([src], Path(tmp))[0])
            lo, hi = M.bbox(converted)
            k = max(hi[i] - lo[i] for i in range(3)) / max(*(before[1][i] - before[0][i] for i in range(3)), 1e-12)
            note = f"Studio scaled by {k:g}" if abs(k - 1) > 1e-6 else "Studio judged the file to be in mm already: unchanged"
            converted = M.translate(converted, (-lo[0], -lo[1], -lo[2]))  # dropped onto z = 0 at x, y >= 0
            src = M.write_stl(Path(a.outdir) / f"{src.stem}__mm.stl", converted, f"{src.stem} mm")
        elif a.scale != 1.0:  # a scaled copy first, so the oriented copy (and its hash) start from mm
            scaled = M.scale(M.read_stl(src), a.scale)
            lo, _ = M.bbox(scaled)
            scaled = M.translate(scaled, (-lo[0], -lo[1], -lo[2]))  # dropped onto z = 0 at x, y >= 0, as an oriented copy is
            src = M.write_stl(Path(a.outdir) / f"{src.stem}__x{a.scale:g}.stl", scaled, f"{src.stem} x{a.scale:g}")
        path = M.oriented_copy(src, o, a.outdir)  # the identity orientation (--scale or --convert-unit alone) returns that copy
        m = M.metrics(M.read_stl(path))
        out.emit(
            {"written": str(path), **m.to_json(), **({"unit": note} if note else {})},
            [
                f"written {path}: {m.size[0]:.1f} x {m.size[1]:.1f} x {m.size[2]:.1f} mm, contact {m.contact_mm2:.0f} mm2, support shadow {m.shadow_mm2:.0f} mm2"
                + (f" ({note})" if note else "")
            ],
        )
        return 0


@command("split", "Split an STL with several bodies into one STL per body (Studio's 'Split to objects')")
class Split(Command):
    def configure(self, p):
        p.add_argument("file")
        p.add_argument("-o", "--outdir", required=True)

    def run(self, a, app, out):
        from . import mesh as M

        facets = M.read_stl(a.file)
        bodies = M.split_bodies(facets)
        n_cav = M.cavities(facets)
        stem = Path(a.file).stem
        written = [str(M.write_stl(Path(a.outdir) / f"{stem}_body{i}.stl", b, f"{stem} body {i}")) for i, b in enumerate(bodies, 1)]
        lines = [f"{len(bodies)} body(ies):", *written]
        if n_cav:
            lines.append(
                f"{n_cav} inward shell(s) (cavities inside a body) left out of the body files; slice the original file to keep them"
            )
        out.emit({"bodies": len(bodies), "cavities": n_cav, "written": written}, lines)
        return 0


def _studio_version(app: Bambu) -> str | None:
    """Studio's version for the sweep provenance, or None with a WARN on stderr naming why (never silent)."""
    try:
        v = app.studio_cli().version()
    except BambuError as e:
        log.warning("Bambu Studio's version is unknown (%s): the written spec's provenance records none", e)
        return None
    if v is None:
        log.warning("Bambu Studio did not report its version: the written spec's provenance records none")
    return v


def _sweep_command(a: argparse.Namespace) -> list[str]:
    """The sweep command line that reproduces the measured variants (paths absolute; not --write-spec or --pick)."""
    cmd = ["shambu", "sweep", str(Path(a.spec).expanduser().resolve())]
    if a.variants:
        cmd += ["--variants", str(Path(a.variants).expanduser().resolve())]
    for key in a.orientations:
        cmd += ["--orientations", key]
    if a.orientations:
        cmd += ["--top", str(a.top), "--source", a.source, "--threshold", f"{a.threshold:g}"]
    for flag in ("by_object", "no_slice", "no_baseline", "probe_supports", "no_probe", "allow_filament_mismatch"):
        if getattr(a, flag, False):
            cmd.append("--" + flag.replace("_", "-"))
    return cmd


@command("sweep", "Build (and slice) variants of a spec and compare time and filament: orientations, by-object, settings")
class Sweep(Command):
    def configure(self, p):
        p.add_argument("spec")
        p.add_argument("--variants", metavar="JSON", help="a JSON file: a list of variants (see `shambu spec` and sweep.py)")
        p.add_argument("--orientations", metavar="PART", action="append", default=[], help="try the best orientations of this part key")
        p.add_argument("--top", type=int, default=4, help="orientations per part for --orientations (default 4)")
        p.add_argument(
            "--source",
            choices=("own", "studio"),
            default="own",
            help="candidates for --orientations: shambu's own score (default) or Studio's auto-orient table, ranked by unprintability",
        )
        p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help=THRESHOLD_HELP)
        p.add_argument("--by-object", action="store_true", help="add a variant with every plate on print_sequence 'by object'")
        p.add_argument("--no-slice", action="store_true", help="only plan and export; no time or filament numbers")
        p.add_argument("--no-baseline", action="store_true")
        p.add_argument("--timeout", type=int, default=1200)
        p.add_argument(
            "--write-spec",
            metavar="OUT.json",
            help="write the chosen variant as a full spec (the input's outdir, a `_sweep` provenance key); build that file",
        )
        p.add_argument("--pick", metavar="NAME", help="with --write-spec: the variant to write (default: the table's first row)")
        _printer_flags(p)
        _mismatch_flag(p)
        _no_probe_flag(p)

    def run(self, a, app, out):
        from . import sweep as S

        app.printer_ip = a.printer_ip
        if not a.no_printer:
            stop = _use_printer(a, app, out, serial=a.serial)
            if stop is not None:
                return stop

        try:  # a bad spec, variant file or part key is a failed check (exit 1), not an environment error
            try:
                spec_json = json.loads(Path(a.spec).read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise BuildError(f"{a.spec}: cannot read the spec: {e}") from e
            variants: list[dict] = S.load_variants(a.variants) if a.variants else []
            for key in a.orientations:
                variants += S.orientation_variants(
                    spec_json, key, a.top, a.threshold, source=a.source, studio=app.studio_cli() if a.source == "studio" else None
                )
            if a.by_object:
                variants.append(S.by_object_variant(spec_json))
            if not variants and a.no_baseline:
                return usage(out, "no variants: give --variants, --orientations or --by-object")
            S.check_names(variants, baseline=not a.no_baseline)
        except BuildError as e:
            out.emit({"error": f"BuildError: {e}"}, [f"BuildError: {e}"])
            return 1
        except (KeyError, TypeError, ValueError, IndexError, AttributeError) as e:  # a structurally bad spec or variant
            msg = f"BuildError: {a.spec}: bad spec ({type(e).__name__}: {e})"
            out.emit({"error": msg}, [msg])
            return 1
        names = ([S.BASELINE] if not a.no_baseline else []) + [str(v["name"]) for v in variants]
        if a.pick is not None and not a.write_spec:
            return usage(out, "--pick needs --write-spec OUT.json")
        if a.write_spec:
            same = S.check_destination(a.write_spec, a.spec, a.variants)
            if same:
                return usage(out, same)
            cannot = S.writable_folder(a.write_spec)
            if cannot:  # the environment (exit 3), found before any slicing
                raise BuildError(cannot)
            if a.pick is not None and a.pick not in names:
                return usage(out, f"--pick {a.pick!r}: no such variant (variants: {', '.join(names)})")
            if a.no_slice and a.pick is None:
                return usage(out, "--write-spec with --no-slice needs --pick NAME: without a slice there are no numbers to rank")
        results = S.run(
            spec_json,
            variants,
            lambda spec, do_slice: app.build(
                spec,
                timeout=a.timeout,
                do_slice=do_slice,
                printer=not a.no_printer,
                allow_filament_mismatch=a.allow_filament_mismatch,
                probe_dir=Path(str(spec_json.get("outdir") or ".")).expanduser() / ".probe",
                **({"no_probe": True} if a.no_probe else {}),
            ),
            do_slice=not a.no_slice,
            baseline=not a.no_baseline,
        )
        all_ok = all(r.ok for r in results)
        if not a.write_spec:
            out.emit([r.to_json() for r in results], S.table(results))
            return 0 if all_ok else 1  # one failed variant is a failed check (exit 1), as for `build`
        failed = [r.name for r in results if not r.ok]
        chosen = S.choose(results, a.pick)
        written = None
        if chosen is None:
            last = "nothing written: no variant passed"
        elif not chosen.ok:
            why = chosen.problems[0] if chosen.problems else "failed"
            last = f"nothing written: variant {chosen.name} failed ({why})"
        else:
            by_name = {S.BASELINE: {"name": S.BASELINE}, **{str(v["name"]): v for v in variants}}
            base = next((r for r in results if r.name == S.BASELINE), None)
            spec_out = S.written_spec(
                spec_json,
                by_name[chosen.name],
                chosen,
                str(Path(a.spec).expanduser().resolve()),
                base,
                studio_version=_studio_version(app),
                command=_sweep_command(a),
            )
            try:
                spec_out = S.relocate_meshes(spec_out, a.write_spec)
                path = S.write_spec(a.write_spec, spec_out)
            except BambuError as e:  # the measured table is the sweep's result: print it, then the error (exit 3)
                msg = f"{type(e).__name__}: {e}"
                out.emit({"results": [r.to_json() for r in results], "written": None, "error": msg}, S.table(results) + [msg])
                return 3
            written = {"path": str(path), **{k: v for k, v in spec_out["_sweep"].items() if k != "from"}}
            last = S.written_line(a.write_spec, spec_out["_sweep"], failed)
        out.emit({"results": [r.to_json() for r in results], "written": written}, S.table(results) + [last])
        return 0 if all_ok and written is not None else 1


@command("check", "Plan a spec: read the printer's spools, resolve presets, measure STLs, place parts, check limits. No export.")
class Check(Command):
    def configure(self, p):
        p.add_argument("spec")
        _printer_flags(p)
        _mismatch_flag(p)
        _probe_flag(p)
        _no_probe_flag(p)

    def run(self, a, app, out):
        spec = load_spec_or_error(a.spec, out)
        if isinstance(spec, int):
            return spec
        app.printer_ip = a.printer_ip
        if not a.no_printer:
            stop = _use_printer(a, app, out, serial=a.serial)
            if stop is not None:
                return stop
        r = app.build(
            spec,
            export=False,
            printer=not a.no_printer,
            allow_filament_mismatch=a.allow_filament_mismatch,
            probe_supports=a.probe_supports,
            **({"no_probe": True} if a.no_probe else {}),
        )
        out.emit(r.to_json(), r.report.splitlines())
        return 0 if r.plan.ok else 1


@command("build", "Read the printer's spools, plan, export the .3mf with Studio's CLI, patch it, verify it, write the report")
class Build(Command):
    def configure(self, p):
        p.add_argument("spec")
        p.add_argument("--timeout", type=int, default=1200, help="seconds for the CLI export")
        p.add_argument("--keep-going", action="store_true", help="export even when a check failed")
        p.add_argument("--open", action="store_true", help="open the result in Bambu Studio afterwards")
        p.add_argument(
            "--slice", action="store_true", help="also slice headless: writes <name>.gcode.3mf with G-code and time/filament estimates"
        )
        p.add_argument("--no-check", action="store_true", help=NO_CHECK_HELP)
        _printer_flags(p)
        _mismatch_flag(p)
        _probe_flag(p)
        _no_probe_flag(p)

    def run(self, a, app, out):
        if a.no_check and not a.slice:
            return usage(out, "--no-check needs --slice: it is a second slicing pass")
        spec = load_spec_or_error(a.spec, out)
        if isinstance(spec, int):
            return spec
        app.listeners.append(lambda ev, d: log.info("%s %s", ev, {k: v for k, v in d.items() if k != "argv"}))
        app.printer_ip = a.printer_ip
        if not a.no_printer:
            stop = _use_printer(a, app, out, serial=a.serial)
            if stop is not None:
                return stop
        kw = {"no_check": True} if a.no_check else {}
        r = app.build(
            spec,
            timeout=a.timeout,
            keep_going=a.keep_going,
            do_slice=a.slice,
            printer=not a.no_printer,
            allow_filament_mismatch=a.allow_filament_mismatch,
            probe_supports=a.probe_supports,
            **kw,
            **({"no_probe": True} if a.no_probe else {}),
        )
        doc, lines = r.to_json(), r.report.splitlines()
        if a.open and r.run and r.run.ok:
            try:  # before the output, so a failed open lands in the one JSON document
                opened = app.open_in_studio(r.project)
            except StudioError as e:
                doc["open_error"] = f"StudioError: {e}"
                out.emit(doc, [*lines, f"StudioError: {e}"])
                return 3
            doc["open_already_running"] = opened.already_running
            warn_open_behind(opened)
        out.emit(doc, lines)
        return 0 if r.ok else 1


@command("inspect", "Summarise a .3mf: plates, objects, parts, extruders, settings, bed, map, pauses")
class Inspect(Command):
    def configure(self, p):
        p.add_argument("file")

    def run(self, a, app, out):
        s = app.inspect(a.file, features=out.as_json)  # only --json shows grams by feature, so only it parses the G-code
        book = s.get("preset_bookkeeping") or []
        listed = book[0]["keys"] if book else []
        proc = f"{s['process']} (+{len(listed)} overrides: {';'.join(listed)})" if listed else f"{s['process']}"
        lines = [
            f"{a.file}: {s['application']}, {'SLICED' if s['sliced'] else 'unsliced'}",
            f"printer {s['printer']}; process {proc}; bed {s['bed_type']}; map {s['filament_map']} ({s['filament_map_mode']}); "
            f"print sequence {s.get('print_sequence') or 'not set (by layer)'}",
        ]
        lines += [f"not re-sliceable by the CLI: {x}" for x in s.get("reslice_problems") or []]
        ns = s.get("nozzle_stats")
        for f in s["filaments"]:
            if ns is None:
                stat = "nozzle stats from extruder_max_nozzle_count (the file has no stats key)"
            elif f.get("nozzle_stats") is None:
                stat = f"{ns['key']} {ns['value']!r} has no entry for extruder {f.get('extruder')}"
            else:
                stat = f"{ns['key']} {f['nozzle_stats']}"
            n_keys = next((len(b["keys"]) for b in book if b["kind"] == "filament" and b["slot"] == f["slot"]), 0)
            over = f" (+{n_keys} overrides)" if n_keys else ""
            lines.append(
                f"filament {f['slot']}: {f['preset']}{over} ({f['type']}) {f['colour'] or ''}, extruder {f.get('extruder')}, {stat}"
            )
        for p in s["plates"]:
            lines.append(
                f"plate {p['id']} '{p['name']}': bed {p['bed_type'] or 'project default'}, map {p['filament_maps'] or 'project default'}, "
                f"print sequence {p.get('print_sequence') or 'project default'}, objects {p['objects']}"
            )
            if p.get("slice"):
                sl = p["slice"]
                lines.append(
                    f"  sliced: {sl['prediction_s'] // 3600} h {sl['prediction_s'] % 3600 // 60} min, {sl['weight_g']:.1f} g, "
                    + ", ".join(f"filament {f['id']} {f['type']} {f['used_g']:.1f} g" for f in sl["filaments"])
                    + (f", warnings {sl['warnings']}" if sl["warnings"] else "")
                )
        for o in s["objects"]:
            parts = ", ".join(f"{p['name']} (filament {p['extruder'] or o['extruder']}, {p['subtype']})" for p in o["parts"])
            pos = o.get("position") or {}
            where = (
                f"plate {pos['plate']} min corner ({pos['min'][0]:g}, {pos['min'][1]:g}), size {' x '.join(f'{v:g}' for v in pos['size'])} mm, "
                if "min" in pos
                else (f"position unknown ({pos['error']}), " if "error" in pos else "")
            )
            lines.append(
                f"object {o['name']}: {where}filament {o['extruder']}, settings {o['settings'] or '{}'}, parts [{parts}]"
                + (f", height ranges {o['height_ranges']}" if o["height_ranges"] else "")
            )
        for c in s["custom_gcode"]:
            lines.append(
                f"plate {c['plate']}: {c['type']} at z {c['top_z']}" + (f" filament {c['extruder']}" if c["type"] == "color_change" else "")
            )
        out.emit(s, lines)
        return 0


@command("patch", "Change a .3mf in place (or to -o): bed, colours, nozzle map, object settings, pauses, names")
class PatchCmd(Command):
    def configure(self, p):
        p.add_argument("file")
        p.add_argument("-o", "--out")
        p.add_argument("--bed-type", help="e.g. 'Textured PEI Plate' (project and every plate)")
        p.add_argument("--plate-bed-type", action="append", default=[], metavar="PLATE:TYPE")
        p.add_argument(
            "--print-sequence",
            action="append",
            default=[],
            metavar="[PLATE:]SEQUENCE",
            help="'by object' or 'by layer', per plate or project-wide",
        )
        p.add_argument("--colours", help="comma list, one #RRGGBB per filament")
        p.add_argument("--map", help="comma list of extruder per filament, e.g. 1,2")
        p.add_argument("--set", action="append", default=[], metavar="OBJECT:KEY=VALUE", help="per-object setting")
        p.add_argument("--unset", action="append", default=[], metavar="OBJECT:KEY")
        p.add_argument(
            "--process-set",
            action="append",
            default=[],
            metavar="KEY=VALUE",
            help="project-wide process setting, listed in the process preset's overrides so Studio keeps it on open",
        )
        p.add_argument(
            "--process-unset",
            action="append",
            default=[],
            metavar="KEY",
            help="restore a process setting to the base preset's value (needs the base preset in the Studio library)",
        )
        p.add_argument("--extruder", action="append", default=[], metavar="OBJECT[/PART]:N")
        p.add_argument("--range", action="append", default=[], metavar="OBJECT:MIN-MAX:KEY=VALUE[,KEY=VALUE]")
        p.add_argument("--pause", action="append", default=[], metavar="PLATE:Z")
        p.add_argument("--colour-change", action="append", default=[], metavar="PLATE:Z:FILAMENT")
        p.add_argument("--custom-gcode", action="append", default=[], metavar="PLATE:Z:GCODE")
        p.add_argument("--rename-plate", action="append", default=[], metavar="PLATE:NAME")
        p.add_argument("--rename-object", action="append", default=[], metavar="OLD:NEW")
        p.add_argument(
            "--repair-nozzle-stats",
            action="store_true",
            help="make an unsliced CLI export re-sliceable: extruder_nozzle_stats, filament_extruder_variant blocks, filament maps",
        )
        g = p.add_argument_group("project edits (geometry and filaments)")
        g.add_argument(
            "--replace-mesh",
            action="append",
            default=[],
            metavar="OBJECT[/PART]=STL",
            help="swap a part's mesh; keeps the name, settings and plate, checks the box (refuses a size change, a no-op, paint)",
        )
        g.add_argument("--rotate-mesh", metavar="X,Y,Z", help="turn the new STL first (degrees, as `shambu rotate`)")
        g.add_argument("--lay-on-face", metavar="NX,NY,NZ", help="lay this face of the new STL on the bed first")
        g.add_argument(
            "--anchor",
            choices=("frame", "centre", "min"),
            default="frame",
            help="frame: the STL frames coincide (Studio's GUI rule, default); centre: keep the old bbox centre; min: keep the min corner",
        )
        g.add_argument("--allow-size-change", action="store_true", help="accept a new mesh whose box differs by more than 0.05 mm")
        g.add_argument("--drop-paint", action="store_true", help="replace a painted mesh and lose its paint")
        g.add_argument(
            "--move",
            action="append",
            default=[],
            metavar="OBJECT=X,Y[@PLATE]",
            help="put the object's bbox MIN corner at X,Y (plate-local mm, as spec `at`), or +DX,+DY to nudge; @PLATE moves it there",
        )
        g.add_argument("--strict", action="store_true", help="a --move that breaks a bed, reach or overlap rule fails (default: FAIL line)")
        g.add_argument("--tower", action="append", default=[], metavar="X,Y[@PLATE]", help="prime tower MIN corner, every plate or one")
        g.add_argument(
            "--add-filament",
            action="append",
            default=[],
            metavar="PRESET[:#RRGGBB][@EXTRUDER]",
            help="append filament slot N+1 (slots 1..N unchanged); colour: the preset's, else the next unused default colour (WARN); extruder 1",
        )
        g.add_argument("--allow-duplicate", action="store_true", help="--add-filament may add a preset and colour that a slot already has")
        g.add_argument("--no-verify", action="store_true", help="skip the verify slice after --add-filament (about 20 s on a full plate)")
        p.add_argument("--dry-run", action="store_true")

    def run(self, a, app, out):
        try:
            ps = self._parse(a, app)
        except ValueError as e:
            out.emit({"error": f"bad patch option: {e}"}, [f"bad patch option: {e}; see --help for the formats"])
            return 2
        except ProjectError as e:  # EditError is one: a refused patch is exit 1 whichever module refused it
            out.emit({"error": f"{type(e).__name__}: {e}"}, [f"{type(e).__name__}: {e}"])
            return 1
        if not ps.patches:
            out.emit({"patched": []}, ["nothing to do: give at least one patch option"])
            return 2
        try:
            if a.dry_run:
                notes = ps.preview(a.file)
                out.emit(
                    {"would_patch": ps.describe(), "notes": notes},
                    ["would patch:"] + ["- " + d for d in ps.describe()] + notes,
                )
                return 0
            done = app.edit(a.file, ps, a.out, verify=not a.no_verify)
        except ProjectError as e:  # an edits.py or patches.py refusal, or an undeclared entry change at save
            out.emit({"error": f"{type(e).__name__}: {e}"}, [f"{type(e).__name__}: {e} (the input file is unchanged)"])
            return 1
        dest = a.out or a.file
        out.emit(
            {"file": dest, "patched": done, "notes": ps.notes},
            [f"patched {dest}:"] + ["- " + d for d in done] + ps.notes,
        )
        return 0

    @staticmethod
    def _parse(a, app=None) -> EditSet:
        ps = EditSet()
        if len(a.add_filament) > 1:
            raise ValueError("one --add-filament per call (each new slot is normalised against the project that holds the slots before it)")
        for x in a.add_filament:  # first, so --extruder, --set and --tower in the same call can use the new slot
            ps.add(app.add_filament_patch(a.file, x, a.allow_duplicate))
        if a.bed_type:
            ps.add(SetBedTypeEverywhere(a.bed_type))
        for x in a.plate_bed_type:
            n, t = x.split(":", 1)
            ps.add(SetBedType(t, int(n)))
        for x in a.print_sequence:
            n, _, seq = x.rpartition(":")
            ps.add(SetPrintSequence(seq.strip().replace("_", " "), int(n) if n else None))
        if a.colours:
            ps.add(SetFilamentColours(tuple(c.strip() for c in a.colours.split(","))))
        if a.map:
            ps.add(SetFilamentMap(tuple(int(v) for v in a.map.split(","))))
        for x in a.set:
            obj, kv = x.split(":", 1)
            k, v = kv.split("=", 1)
            ps.add(SetObjectSetting(obj, k, v))
        for x in a.unset:
            obj, k = x.split(":", 1)
            ps.add(ClearObjectSetting(obj, k))
        if a.process_set or a.process_unset:
            name, base = PatchCmd._process_base(a.file, app)
            other = PatchCmd._other_preset_keys(a.file, app) if a.process_set else None
            if a.process_set and other is None:
                log.warning("no printer or filament preset of %s resolves: --process-set cannot tell a process key from them", a.file)
            for x in a.process_set:
                k, v = x.split("=", 1)
                k = k.strip()
                if other is not None and k in other and k not in (base or {}) and k not in recipes.all_keys():
                    raise ProjectError(f"{k} is a filament/printer setting; `--process-set` takes process keys")
                ps.add(SetProcessSetting(k, v, base))
            for k in a.process_unset:
                k = k.strip()
                if base is None:
                    raise ProjectError(f"process preset {name} is not in the Studio library; cannot restore {k}")
                if k not in base:
                    raise ProjectError(f"process preset {name} does not define {k}; cannot restore {k}")
                v = base[k]
                ps.add(UnsetProcessSetting(k, tuple(str(e) for e in v) if isinstance(v, list) else str(v), base))
        for x in a.extruder:
            obj, n = x.rsplit(":", 1)
            o, _, part = obj.partition("/")
            ps.add(SetExtruder(o, int(n), part or None))
        for x in a.range:
            obj, rng, kvs = x.split(":", 2)
            lo, hi = rng.split("-", 1)
            ps.add(SetHeightRange(obj, float(lo), float(hi), tuple(tuple(kv.split("=", 1)) for kv in kvs.split(","))))
        for x in a.pause:
            n, z = x.split(":", 1)
            ps.add(AddCustomGcode(int(n), float(z), "pause"))
        for x in a.colour_change:
            n, z, f = x.split(":", 2)
            ps.add(AddCustomGcode(int(n), float(z), "color_change", extruder=int(f)))
        for x in a.custom_gcode:
            n, z, g = x.split(":", 2)
            ps.add(AddCustomGcode(int(n), float(z), "custom", extra=g))
        for x in a.rename_plate:
            n, name = x.split(":", 1)
            ps.add(RenamePlate(int(n), name))
        for x in a.rename_object:
            old, new = x.split(":", 1)
            ps.add(RenameObject(old, new))
        if a.repair_nozzle_stats:
            ps.add(RepairNozzleStats())
        rot: tuple[float, float, float] = _floats(a.rotate_mesh, 3, "--rotate-mesh") if a.rotate_mesh else (0.0, 0.0, 0.0)
        face: tuple[float, float, float] | None = _floats(a.lay_on_face, 3, "--lay-on-face") if a.lay_on_face else None
        for x in a.replace_mesh:
            target, eq, stl = x.partition("=")
            if not eq or not stl or not target:
                raise ValueError(f"--replace-mesh {x!r}: use OBJECT[/PART]=PATH.stl")
            obj, _, part = target.partition("/")
            ps.add(ReplaceMesh(obj, stl, part or None, rot, face, a.anchor, a.allow_size_change, a.drop_paint))
        for x in a.move:
            obj, eq, rest = x.rpartition("=")
            if not eq or not obj:
                raise ValueError(f"--move {x!r}: use OBJECT=X,Y[@PLATE] or OBJECT=+DX,+DY")
            pos, _, plate = rest.partition("@")
            if pos.count(",") != 1:
                raise ValueError(f"--move {x!r}: needs two comma-separated numbers after '=' (X,Y or +DX,+DY)")
            xs, ys = (v.strip() for v in pos.split(","))
            signed = [v[:1] in ("+", "-") for v in (xs, ys)]
            if any(signed) and not all(signed):
                raise ValueError(f"--move {x!r}: a relative move needs a sign on both values (+5,-5); absolute values are the min corner")
            try:
                mx, my = _floats(pos, 2, "--move")
            except ValueError as e:  # quote what the user typed, not a rebuilt "X,Y"
                raise ValueError(f"--move {x!r}: {str(e).split(': ', 1)[-1]}") from e
            ps.add(MoveObject(obj, mx, my, all(signed), _plate_arg(plate, "--move"), a.strict))
        for x in a.tower:
            ps.add(SetTower(*_tower_arg(x)))
        return ps

    @staticmethod
    def _process_base(file: str, app) -> tuple[str, dict | None]:
        """The preset Studio resets the file's process to on open: `inherits_group[0]` when it is set and resolves
        (Preset.cpp:2258-2264: with `inherits` the reset target is that parent), else `print_settings_id` (a system
        preset, :2266-2269). (the name, None) when neither resolves."""
        slot = Project(file).preset_bookkeeping()[0]
        names = [n for n in (slot["inherits"], str(slot["id"] or "")) if n]
        first = names[0] if names else "?"
        resolver = PatchCmd._resolver(app)
        if resolver is None:
            return first, None
        for n in names:
            try:
                return n, resolver.resolve("process", n)
            except PresetError:
                log.warning("process preset %s is not in the Studio library", n)
        return first, None

    @staticmethod
    def _resolver(app):
        if app is None:
            return None
        try:
            return app.presets()
        except PresetError as e:  # no profile tree at all: nothing resolves
            log.warning("no Studio presets to resolve against: %s", e)
            return None

    @staticmethod
    def _other_preset_keys(file: str, app) -> set[str] | None:
        """The keys of the file's printer and filament presets as the Studio library resolves them (each by name; one
        that does not resolve is skipped). None when none resolves."""
        resolver = PatchCmd._resolver(app)
        if resolver is None:
            return None
        ps = Project(file).project_settings
        wanted = [("machine", ps.get("printer_settings_id"))] + [("filament", n) for n in ps.get("filament_settings_id") or []]
        keys: set[str] = set()
        found = False
        for kind, name in wanted:
            if not name:
                continue
            try:
                keys |= set(resolver.resolve(kind, str(name)))
                found = True
            except PresetError:
                log.warning("%s preset %s is not in the Studio library", kind, name)
        return keys if found else None


NO_CHECK_HELP = (
    "with --slice: when the checked slice exits -101 (G-code path conflicts) or -100 (empty layers, overlap), slice again with "
    "Studio's --no-check, as the GUI accepts the plate with a serious warning; the report names what the check found"
)


def _floats(text: str, n: int, flag: str) -> tuple:
    """`n` finite comma-separated numbers, else ValueError (a usage error, exit 2)."""
    vals = [v.strip() for v in text.split(",")]
    if len(vals) != n:
        raise ValueError(f"{flag} {text!r}: needs {n} comma-separated numbers")
    try:
        out = tuple(float(v) for v in vals)
    except ValueError as e:
        raise ValueError(f"{flag} {text!r}: {e}") from e
    if not all(math.isfinite(v) for v in out):
        raise ValueError(f"{flag} {text!r}: the values must be finite numbers")
    return out


def _plate_arg(text: str, flag: str) -> int | None:
    if not text:
        return None
    if not text.isdigit() or int(text) < 1:
        raise ValueError(f"{flag} @{text}: the plate must be a whole number from 1")
    return int(text)


def _tower_arg(text: str) -> tuple[float, float, int | None]:
    pos, _, plate = text.partition("@")
    x, y = _floats(pos, 2, "--tower")
    return (x, y, _plate_arg(plate, "--tower"))


@command("slice", "Slice a saved .3mf headless (Studio's CLI) and report time and filament; --no-check as a second pass")
class Slice(Command):
    def configure(self, p):
        p.add_argument("file")
        p.add_argument("-o", "--out", help="the .gcode.3mf to write (default: <name>.gcode.3mf next to the input)")
        p.add_argument("--plate", type=int, default=0, help="plate to slice (default 0: every plate)")
        p.add_argument("--no-check", action="store_true", help=NO_CHECK_HELP.removeprefix("with --slice: "))
        p.add_argument("--tower", metavar="X,Y[@PLATE]", help="prime tower min corner for this slice (the input file is not changed)")
        p.add_argument(
            "--settings-from",
            metavar="SPEC.json",
            help="re-apply the spec's resolved machine and process presets (--load-settings); the project's filaments stay",
        )
        p.add_argument("--timeout", type=int, default=1200)

    def run(self, a, app, out):
        try:
            tower = _tower_arg(a.tower) if a.tower else None
        except ValueError as e:
            return usage(out, str(e))
        spec = None
        if a.settings_from:
            spec = load_spec_or_error(a.settings_from, out)
            if isinstance(spec, int):
                return spec
        try:
            r = app.slice_project(a.file, a.out, plate=a.plate, no_check=a.no_check, tower=tower, settings_from=spec, timeout=a.timeout)
        except ValueError as e:
            return usage(out, str(e))
        except EditError as e:
            out.emit({"error": f"EditError: {e}"}, [f"EditError: {e}"])
            return 1
        lines = [f"slice {r.file.name}: {r.run.summary()}"]
        for est in r.estimates:
            lines += estimate_lines(est)
        lines += checks_skipped_lines(r.checks_skipped)
        lines += ["- FAIL: " + x for x in r.fails]
        lines += ["- VERIFY FAIL: " + x for x in r.verify_problems]
        lines += ["- " + n.strip() for n in r.notes]
        if r.ok:
            lines.append(f"- output {r.output}")
        out.emit(r.to_json(), lines)
        return 0 if r.ok else 1


@command("studio", "Last resort: run Bambu Studio's CLI with raw arguments (`shambu studio -- ARGS`), ruled, logged, unverified")
class StudioHatch(Command):
    def configure(self, p):
        p.add_argument("--out", default=".", help="the run folder goes under DIR/studio-runs/<UTC stamp>/ (default: .)")
        p.add_argument("--timeout", type=int, default=1200)
        p.add_argument("raw", nargs=argparse.REMAINDER, help="Studio arguments after --; --export-3mf takes a bare name")

    def run(self, a, app, out):
        raw = list(a.raw)
        if raw and raw[0] == "--":
            raw = raw[1:]
        if not raw:
            return usage(out, "give Studio's arguments after --, e.g. shambu studio -- --slice 1 --export-3mf out.3mf in.3mf")
        try:
            CliCommand.parse(raw, Path.cwd())  # the rules first: a refusal is a usage error before Studio is needed
        except StudioError as e:
            return usage(out, f"refused: {e}")
        d = app.studio_raw(raw, a.out, a.timeout)  # a missing binary stays an environment error (exit 3)
        line = f"studio: exit {d['exit']} ({d['meaning']}); run folder {d['run_dir']}; unverified: run `shambu inspect` on the output"
        out.emit(d, [line])
        return 0 if d["exit"] == 0 else 1


@command("open", "Open a project in Bambu Studio for the user")
class Open(Command):
    def configure(self, p):
        p.add_argument("file")

    def run(self, a, app, out):
        opened = app.open_in_studio(a.file)
        warn_open_behind(opened)
        out.emit({"opened": a.file, "already_running": opened.already_running}, [f"opened {a.file} in Bambu Studio"])
        return 0


OPEN_ALREADY_RUNNING = "WARN: Bambu Studio was already running: check that the front window shows {name}"


def warn_open_behind(opened: OpenResult) -> None:
    """The one WARN line on stderr (stdout keeps the one document) when Studio ran before `open`: the project can open
    in a window behind the current one (a running process proves no window order), and on 29 October the user read
    the old window's grams as the new file's."""
    if opened.already_running:
        print(OPEN_ALREADY_RUNNING.format(name=Path(opened.path).name), file=sys.stderr)


@command("printer", "Read the printer over LAN MQTT (read-only): status | filaments | watch | discover | raw | login | list | forget")
class Printer(Command):
    def configure(self, p):
        p.add_argument("action", choices=("status", "filaments", "watch", "discover", "raw", "login", "list", "forget"))
        p.add_argument("--ip")
        p.add_argument("--serial")
        _profile_flags(p, serial=False)
        p.add_argument("--wait", type=float, default=8.0)
        p.add_argument(
            "--every", type=int, default=300, help="watch interval in seconds (at most 60 near the start and the end of a print)"
        )
        p.add_argument("--log", help="watch: append one JSON line per poll to this file (no IP, no serial)")
        p.add_argument("--gcode3mf", help="watch --log: the sliced .gcode.3mf being printed; its M73 track goes in the log's header line")
        p.add_argument("--plate", type=int, help="watch --gcode3mf: the plate number (default: the only sliced plate)")
        p.add_argument("--max-errors", type=int, default=10, help="watch: stop after this many failed reads in a row (default 10)")
        p.add_argument("--max-polls", type=int, default=2000, help="watch: stop after this many polls (default 2000)")
        p.add_argument(
            "--wait-for-start",
            action="store_true",
            help="watch: when the printer is idle or shows the last job's end, poll every 60 s until a job starts (bounded by --max-polls)",
        )
        p.add_argument("--ssdp", type=float, default=0, help="discover: also listen for SSDP this many seconds")
        p.add_argument("--printer-preset", help="filaments: the machine preset to name presets for (default: the GUI's current one)")
        p.add_argument(
            "--insecure",
            action="store_true",
            help="skip TLS verification against Bambu's CA and the serial (the access code then goes to an unverified endpoint)",
        )
        p.add_argument("--name", help="login: the profile name (default: the profile's own, else 'printer')")
        p.add_argument("--default", action="store_true", help="login: make this profile the default")
        p.add_argument("--mode", choices=("lan", "cloud"), help="login: the printer's connection mode, as set on its touchscreen")
        p.add_argument("--from-studio", action="store_true", help="login: take the serial and the code from Bambu Studio's config")
        p.add_argument("--keep-code", action="store_true", help="forget: keep the stored access code")

    def run(self, a, app, out):
        from .printer import READ_TIMEOUT_S

        # one read's timeout: 15 s at the default --wait 8, and --wait plus 7 s (connect and grace) above that
        app.printer_timeout = max(READ_TIMEOUT_S, a.wait + 7)
        if a.action == "discover":
            return _discover(a, out)
        if a.action == "list":
            return _list(out)
        if a.action == "forget":
            return _forget(a, app, out)
        if a.action == "login":
            return _login(a, app, out)
        stop = _use_printer(a, app, out, serial=a.serial, insecure=a.insecure)
        if stop is not None:
            return stop
        svc = app.printer(a.ip, a.serial, a.wait, insecure=a.insecure)
        if a.action == "raw":
            Output(True).emit(svc.public_report())  # always JSON; scrubbed as every other document
            return 0
        if a.action == "status":
            st = svc.status()
            out.emit(st.to_json(), st.lines())
            return 0
        if a.action == "filaments":
            lf = svc.filaments(app.presets(), a.printer_preset or app.gui_machine_preset())
            out.emit(lf.to_json(), lf.lines())
            return 0
        return _watch(a, svc, out)


def _discover(a, out: Output) -> int:
    """`printer discover`: the ARP table, then the default-route /24, then (with --ssdp) an SSDP listen. No credential."""
    from .printer import ArpProbe, SsdpListen, SubnetProbe

    hits = ArpProbe().find() + SubnetProbe().find() + (SsdpListen(a.ssdp).find() if a.ssdp else [])
    out.emit(
        hits,
        [f"{h['ip']}  ({h['method']}" + (f", {h.get('model')} {h.get('connect')}" if h.get("model") else "") + ")" for h in hits]
        or ["no printer found"],
    )
    return 0 if hits else 1


def _hhmm(stamp: str | None) -> str:
    """The HH:MM of a UTC ISO stamp, labelled ("08:00 UTC"), or "never"."""
    return f"{stamp[11:16]} UTC" if stamp else "never"


def _list(out: Output) -> int:
    """`printer list`: one line per profile. The one place shambu prints a printer's IP."""
    ps = load_profiles_named()
    doc = {"config_dir": str(conf.PRINTERS.parent), "default": ps.default, "printers": [p.public() for p in ps.printers]}
    if not ps.printers:
        out.emit(doc, ["no printer profiles: run shambu printer login"])
        return 1
    lines = [
        f"{p.name}  {conf.mask(p.serial)}  {p.ip or '-'}  {p.mode}  verified {_hhmm(p.verified)}"
        + ("  (default)" if p.name == ps.default else "")
        for p in ps.printers
    ]
    out.emit(doc, lines)
    return 0


def _forget(a, app: Bambu, out: Output) -> int:
    if not a.printer_profile:
        return usage(out, "forget needs --printer NAME (shambu printer list shows the names)")
    ps = load_profiles_named()  # a damaged file: exit 3, naming it
    try:  # before the lock: an unknown name writes nothing (no config dir, no lock file)
        conf.select_profile(a.printer_profile, ps)
    except ConfError as e:
        return usage(out, str(e))
    r = app.forget(a.printer_profile, a.keep_code)
    what = (
        f"code: {r['note']}"
        if r["note"]
        else ("code removed" if r["code_removed"] else "code kept" if a.keep_code else "no code was stored")
    )
    out.emit(r, [f"forgot profile {r['name']} (serial {r['serial_masked']}); {what}"])
    return 3 if r["note"] else 0  # the profile is gone but the code is not: the environment failed


LOGIN_FORMS = (
    "login needs a printer: shambu printer login --ip IP [--serial SERIAL], shambu printer login --printer NAME [--ip IP], "
    "or shambu printer login --from-studio"
)


def _login(a, app: Bambu, out: Output) -> int:
    """`printer login`: check the flags, read the code, read the printer once with it, then store the profile and the
    code. Exit 0, 2 for bad usage, 3 when the printer refuses the code or cannot be read (nothing is stored then).
    The serial checked here (the flag, else the profile's) goes to `Bambu.login` as the pin."""
    from .profiles import valid_name

    if a.name is not None and not valid_name(a.name):
        return usage(
            out,
            "--name: a profile name is 1 to 32 letters, digits, '.', '_' or '-', starts with a letter or a digit, "
            "and holds no IP address, host name or serial",
        )
    if a.from_studio and (a.serial or a.access_code_file or a.access_code_stdin):
        return usage(out, "--from-studio takes the serial and the code from Bambu Studio's config: drop --serial and the access code flags")
    if a.from_studio and a.printer_profile is not None:
        return usage(out, "--from-studio takes Bambu Studio's selected printer and --printer names a profile: give one of them")
    ps = load_profiles_named()
    profile = None
    if a.printer_profile is not None:
        try:
            profile = conf.select_profile(a.printer_profile, ps)
        except ConfError as e:
            return usage(out, str(e))
    elif not a.from_studio and a.ip is None:
        profile = ps.selected(None)
        if profile is None:
            return usage(out, LOGIN_FORMS)
    if profile is not None and not a.from_studio and a.serial and a.serial.casefold() != profile.serial.casefold():
        return usage(
            out, f"--serial does not match profile {profile.name}: enrol another printer with --ip IP --serial SERIAL --name OTHER"
        )
    if a.insecure and not a.from_studio and not a.serial and profile is None:
        return usage(out, "--insecure cannot read the printer's certificate: give --serial")
    code = None
    if not a.from_studio:
        got = read_access_code(a, out)
        if isinstance(got, int):
            return got
        code = got
    app.printer_profile = a.printer_profile
    app.printer_wait = a.wait
    r = app.login(
        ip=a.ip,
        serial=a.serial,
        code=code,
        from_studio=a.from_studio,
        name=a.name,
        make_default=a.default,
        insecure=a.insecure,
        mode=a.mode,
        pin=None if a.from_studio else (a.serial or (profile.serial if profile is not None else None)),
    )
    line = f"profile {r.name}: serial {r.serial_masked}, code stored, mode {r.mode}, read at {_hhmm(r.verified)}" + (
        " (default)" if r.default else ""
    )
    out.emit(r, [line])
    return 0


def _gcode_track(path: str, plate: int | None):
    """(M73Track, entry name) of a sliced project's plate G-code, streamed from the zip: no other entry is read.
    Raises ProjectError when the plate's G-code is missing, CalibrationError when it holds no usable M73 track."""
    from .calibrate import parse_m73
    from .errors import ProjectError
    from .threemf import open_text_entry, plate_gcode_names

    names = plate_gcode_names(path)
    if plate is not None:
        names = [n for n in names if n == f"Metadata/plate_{plate}.gcode"]
    if len(names) != 1:
        which = f"plate {plate}" if plate is not None else "one sliced plate (pass --plate N)"
        raise ProjectError(f"{path}: expected the G-code of {which}, found {names or 'none'} (export it with Export Plate Sliced File)")
    with open_text_entry(path, names[0]) as f:
        return parse_m73(f), names[0]


def _watch(a, svc, out: Output) -> int:
    """`printer watch`: one document per poll on stdout; with --log, one JSON line per poll (and a header line) in the
    file. Exit 0 when the print reached a final state or the poll limit, 3 after --max-errors failed reads in a row or
    when the log cannot be opened or written."""
    from .calibrate import CalibrationError
    from .conf import DataFileError
    from .errors import ProjectError
    from .printer import WatchError, outward_secrets, scrub_tree, utc_iso

    if a.every < 1 or a.max_errors < 1 or a.max_polls < 1:
        return usage(out, "--every, --max-errors and --max-polls must be 1 or more")
    if a.gcode3mf and not a.log:
        return usage(out, "--gcode3mf ties a --log to the file it watches: give --log too")
    try:  # register the code the chain holds (the credentials file too) before the header text is scrubbed
        svc.prepare_secrets()
    except (PrinterError, ConfError):
        pass  # a missing serial or code: the first poll reports it (the setup record)
    header: dict | None = None
    if a.log:
        header = {"kind": "header", "shambu_version": __version__, "started_at": utc_iso(time.time()), "every_s": a.every}
        if a.gcode3mf:
            if not Path(a.gcode3mf).is_file():
                return usage(out, f"--gcode3mf {a.gcode3mf}: no such file")
            try:
                track, entry = _gcode_track(a.gcode3mf, a.plate)
            except (ProjectError, CalibrationError) as e:
                msg = f"{type(e).__name__}: {e}"
                out.emit({"error": msg}, [msg])
                return 1
            header.update(gcode3mf=Path(a.gcode3mf).name, gcode=entry, track=track.to_json())
            # the file name and the G-code's labels are free text: the printer boundary's sanitiser applies to them too
            header = scrub_tree(header, outward_secrets(getattr(svc, "serial", None) or "", getattr(svc, "ip", None) or ""))
    log_f = None
    code = 0

    def write(rec: dict) -> None:
        if log_f is None:
            return
        try:
            log_f.write(json.dumps(rec, allow_nan=False) + "\n")
            log_f.flush()
        except OSError as e:
            raise DataFileError(f"--log {a.log}: cannot write it ({e.strerror})") from e

    try:
        if a.log:
            try:
                log_f = open(a.log, "a", encoding="utf-8")  # closed below, on both paths
            except OSError as e:
                raise DataFileError(f"--log {a.log}: cannot open it for appending ({e.strerror})") from e
            if header is not None:
                write(header)
        for item in svc.watch(a.every, max_polls=a.max_polls, max_errors=a.max_errors, wait_for_start=a.wait_for_start):
            if isinstance(item, WatchError):
                log.warning("read failed at %s (%d of %d in a row): %s", item.read_at, item.consecutive, a.max_errors, item.error)
                if out.as_json:
                    out.emit(item.log_poll())
                write({"kind": "error", **item.log_poll()})
                if item.gave_up:
                    code = 3
            else:
                rec = {"kind": "poll", **item.log_poll()}
                if item.done:
                    rec["final"] = True
                out.emit(item.to_json(), item.lines() + [""])
                write(rec)
            sys.stdout.flush()
    except BaseException:
        if log_f is not None:  # keep the error in flight: a second failure on close must not replace it
            try:
                log_f.close()
            except OSError as e:
                log.warning("closing --log after an error failed too: %s", e.strerror)
        raise
    if log_f is not None:
        try:
            log_f.close()
        except OSError as e:
            raise DataFileError(f"--log {a.log}: cannot write it ({e.strerror})") from e
    return code


@command("calibrate", "Compare watch logs with Studio's estimate; store measured prints and show the fit per printer and material")
class Calibrate(Command):
    def configure(self, p):
        p.add_argument(
            "--log", action="append", default=[], help="a `printer watch --log` file made with --gcode3mf (repeat for several prints)"
        )
        p.add_argument("--add", action="store_true", help="add the logs' prints to the calibration file")
        p.add_argument("--show", action="store_true", help="print the stored prints and the fit per printer model and material")

    def run(self, a, app, out):
        from . import calibrate as C
        from . import conf

        fits, prints, errors, notes = [], [], [], []
        for path in a.log:
            name = Path(path).name
            try:
                with open(path, encoding="utf-8") as f:
                    sessions = C.read_log(f)
            except OSError as e:
                errors.append(f"{name}: cannot read it ({e.strerror})")
                continue
            except C.CalibrationError as e:
                errors.append(f"{name}: {e}")
                continue
            found = 0
            for s in sessions:  # each session on its own: one bad or empty session does not hide the others
                try:
                    if not any(r["state"] == "RUNNING" for _, r in s.polls):
                        notes.append(f"{name}: the session at line {s.line} has no RUNNING poll (no print): skipped")
                        continue
                    if not s.header or not s.header.get("track"):
                        raise C.CalibrationError(
                            f"the session at line {s.line} is not tied to a G-code file: watch with --log and --gcode3mf"
                        )
                    track = C.M73Track.from_json(s.header["track"])
                    fit = C.fit(C.polls_from_session(s, track), s, track)
                except C.CalibrationError as e:
                    errors.append(f"{name}: {e}")
                    continue
                found += 1
                fits.append(fit)
                prints.append(fit.to_print(C.session_fingerprint(s.header, s.polls[0][1], s.polls[-1][1])))
            if not found and not any(e.startswith(f"{name}: ") for e in errors):
                errors.append(f"{name}: no session with a RUNNING poll: the log holds no print")
        added = 0
        if a.add and prints and not errors:

            def add(stored: list[dict]) -> list[dict]:
                for p in prints:  # one session is one print, whatever the log file is called
                    stored = [s for s in stored if s.get("session") != p["session"]]
                    stored.append(p)
                return stored

            conf.update_calibration(add)
            added = len(prints)
        show = a.show or not a.log
        stored = conf.load_calibration() if show else []
        combined = (
            [C.combine(stored, m, mat) for m, mat in dict.fromkeys((s.get("printer_model"), s.get("material")) for s in stored)]
            if show
            else []
        )
        lines = [f"ERROR: {e}" for e in errors] + [f"note: {n}" for n in notes]
        for fit in fits:
            lines += fit.lines()
        if a.add:
            lines.append(
                f"added {added} print(s) to the calibration file"
                if added
                else "added nothing: " + ("a log failed" if errors else "no print")
            )
        if show:
            lines.append(f"{len(stored)} stored print(s)")
            lines += [
                f"  {s['date']}  {s['printer_model']}  {s['material']}  Studio {C.hm(s['studio_total_min'])}  {s['kind']}  "
                f"rate {s['rate'] if s['rate'] is not None else '-'}  offset {s['startup_offset_min'] if s['startup_offset_min'] is not None else '-'}  "
                f"flags {', '.join(s['flags']) or 'none'}"
                for s in stored
            ]
            for c in combined:
                lines += c.lines()
        doc = {
            "prints": prints,
            "errors": errors,
            "notes": notes,
            "added": added,
            "stored": stored if show else None,
            "fits": combined if show else None,
        }
        secrets = conf.sensitive_values()  # file names and G-code labels are free text: the masks go too, not only `emit`'s list
        out.emit(_scrub_tree(_jsonable(doc), secrets), [conf.scrub_text(x, secrets) for x in lines])
        return 1 if errors else 0


# ---------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="shambu", description="Bambu Studio projects, 3MF patching and Bambu printer status from the command line"
    )
    p.add_argument("--json", action="store_true", help="machine-readable output (one JSON document; `printer watch` prints one per poll)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"shambu {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, c in _REGISTRY.items():
        sp = sub.add_parser(name, help=c.help, description=c.help)
        c.configure(sp)
        _add_json_flag(sp)
    return p


def _add_json_flag(p: argparse.ArgumentParser) -> None:
    """`--json` after the command name too (`shambu info --json f.stl`), in nested subcommands as well. The default
    is SUPPRESS, so a subcommand that does not see the flag keeps the global one's value."""
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output (as the global --json)")
    for act in p._actions:
        if isinstance(act, argparse._SubParsersAction):
            for child in act.choices.values():
                _add_json_flag(child)


def _scrub_values() -> list[str]:
    """What `Output.emit` and `main` scrub from every output: every known code (`conf.secret_values`) and every raw serial (the
    `conf.sensitive_values` entries that are not a mask), longest first. A masked serial (`00X…999`) is not scrubbed."""
    vals = set(conf.secret_values()) | {v for v in conf.sensitive_values() if "\u2026" not in v}
    return sorted(vals, key=len, reverse=True)


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    out = Output(a.json)
    try:
        app = Bambu()
        return _REGISTRY[a.cmd].run(a, app, out)
    except BambuError as e:  # the text must never hold a code or a raw serial; an IP and a masked serial stay, as in 0.4.0
        msg = conf.scrub_text(f"{type(e).__name__}: {e}", _scrub_values())
        if a.json:
            print(json.dumps({"error": msg}))
        else:
            print(msg, file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130


# ---------------------------------------------------------------------------- verify (registered at import, like the rest)
@command(
    "verify",
    "Compare a project (a Bambu Studio save) with the spec it was built from: preset ids and override lists, every spec "
    "setting, filaments, maps, bed type, print sequence, per-object settings and positions. No Studio run, no STL needed",
)
class Verify(Command):
    def configure(self, p):
        p.add_argument("file", help="the .3mf or .gcode.3mf to check, e.g. the project after a save in Bambu Studio")
        p.add_argument(
            "--against",
            required=True,
            metavar="SPEC",
            help="the spec JSON the project was built from (or the build's shambu/spec.json); presets are composed from it, as the build does",
        )
        p.epilog = (
            "Save the project in Studio first (File > Save project as): changes made on open live in memory until then. "
            "One line per item that is not as expected: FAIL a value differs or Studio resets it on open, WARN a key the "
            "spec does not set changed in the GUI (with its file value) or other bookkeeping, INFO a fact the spec does "
            "not decide. Exit 0 no FAIL, 1 any FAIL or a spec that does not load, 2 usage, 3 a project file that does "
            "not read or a preset the Studio library lacks."
        )

    def run(self, a, app, out):
        spec = load_spec_or_error(a.against, out)
        if isinstance(spec, int):
            return spec
        rep = app.verify(a.file, spec, a.against)
        out.emit(rep.to_json(), [x.text() for x in rep.lines] + [rep.last_line()])
        return 0 if rep.ok else 1
