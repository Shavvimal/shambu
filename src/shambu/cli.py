"""Command line: `shambu <command> ...`. Each subcommand is a small class registered with @command.

Output is human text by default and JSON with `--json` (before or after the command name), so an agent can parse
it. Exit codes: 0 ok, 1 a check or verification failed or the spec is invalid, 2 bad usage, 3 an environment error
(binary, config).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from pathlib import Path

from . import __version__, recipes
from .edits import (
    EditError,
    EditSet,
    MoveObject,
    ReplaceMesh,
    SetTower,
)
from .errors import BambuError, BuildError, StudioError
from .facade import Bambu
from .mesh import DEFAULT_THRESHOLD
from .patches import (
    AddCustomGcode,
    ClearObjectSetting,
    RenameObject,
    RenamePlate,
    SetBedType,
    SetBedTypeEverywhere,
    SetExtruder,
    SetFilamentColours,
    SetFilamentMap,
    SetHeightRange,
    SetObjectSetting,
    SetPrintSequence,
)
from .report import estimate_lines
from .spec import Spec
from .studio import CliCommand

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
    def __init__(self, as_json: bool):
        self.as_json = as_json

    def emit(self, data, text_lines: list[str] | None = None) -> None:
        if self.as_json:
            print(json.dumps(_jsonable(data), indent=1, default=str))
        else:
            for line in text_lines if text_lines is not None else _default_lines(data):
                print(line)


def usage(out: Output, msg: str) -> int:
    """A usage error: one error document (or one line) and exit code 2."""
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


def _mismatch_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--allow-filament-mismatch",
        action="store_true",
        help="keep a pinned filament preset that differs from the spool in that nozzle (otherwise a FAIL); the report records it",
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


@command("doctor", "Check the Studio install, profiles, config, the X2D machine facts and the spools loaded on the printer")
class Doctor(Command):
    def configure(self, p):
        _printer_flags(p)

    def run(self, a, app, out):
        app.printer_ip = a.printer_ip
        d = app.doctor(printer=not a.no_printer)
        lines = [
            f"Bambu Studio {d['studio']['version']} at {d['studio']['binary']} (running pids: {d['studio']['running_pids'] or 'none'})",
            "profile sources: " + "; ".join(d["profiles"]),
        ]
        c = d.get("config", {})
        lines.append(
            f"GUI presets: machine {c.get('gui_machine_preset')}, process {c.get('gui_process_preset')}, filaments {c.get('gui_filament_presets')}, bed {c.get('gui_bed_type')}"
        )
        lines.append(
            f"selected printer {c.get('selected_printer_serial')}, access code stored: {c.get('has_access_code')}, single_instance {c.get('single_instance')}"
        )
        m = d.get("machine", {})
        if "bed" in m:
            lines.append(
                f"machine {m['name']}: bed {m['bed']['x'][1]:.0f} x {m['bed']['y'][1]:.0f} x {m['z_max']:.0f}; start G-code {m['start_gcode_chars']} chars"
            )
            for e in m["extruders"]:
                lines.append(
                    f"  extruder {e['index']} ({e['type']}, {e['nozzle']} mm): x {e['x'][0]}..{e['x'][1]}, z up to {e['z_max']:.0f}"
                )
        lines += printer_lines(d.get("printer", {}), c)
        lines.append("recipes: " + ", ".join(d["recipes"]))
        out.emit(d, lines)
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
        out.emit({"ok": not errs, "problems": errs}, ["spec ok" if not errs else "spec problems:"] + ["- " + e for e in errs])
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
        _printer_flags(p)
        _mismatch_flag(p)

    def run(self, a, app, out):
        from . import sweep as S

        app.printer_ip = a.printer_ip

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
        results = S.run(
            spec_json,
            variants,
            lambda spec, do_slice: app.build(
                spec, timeout=a.timeout, do_slice=do_slice, printer=not a.no_printer, allow_filament_mismatch=a.allow_filament_mismatch
            ),
            do_slice=not a.no_slice,
            baseline=not a.no_baseline,
        )
        out.emit([r.to_json() for r in results], S.table(results))
        return 0 if all(r.ok for r in results) else 1  # one failed variant is a failed check (exit 1), as for `build`


@command("check", "Plan a spec: read the printer's spools, resolve presets, measure STLs, place parts, check limits. No export.")
class Check(Command):
    def configure(self, p):
        p.add_argument("spec")
        _printer_flags(p)
        _mismatch_flag(p)

    def run(self, a, app, out):
        spec = load_spec_or_error(a.spec, out)
        if isinstance(spec, int):
            return spec
        app.printer_ip = a.printer_ip
        r = app.build(spec, export=False, printer=not a.no_printer, allow_filament_mismatch=a.allow_filament_mismatch)
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

    def run(self, a, app, out):
        if a.no_check and not a.slice:
            return usage(out, "--no-check needs --slice: it is a second slicing pass")
        spec = load_spec_or_error(a.spec, out)
        if isinstance(spec, int):
            return spec
        app.listeners.append(lambda ev, d: log.info("%s %s", ev, {k: v for k, v in d.items() if k != "argv"}))
        app.printer_ip = a.printer_ip
        kw = {"no_check": True} if a.no_check else {}
        r = app.build(
            spec,
            timeout=a.timeout,
            keep_going=a.keep_going,
            do_slice=a.slice,
            printer=not a.no_printer,
            allow_filament_mismatch=a.allow_filament_mismatch,
            **kw,
        )
        out.emit(r.to_json(), r.report.splitlines())
        if a.open and r.run and r.run.ok:
            app.open_in_studio(r.project)
        return 0 if r.ok else 1


@command("inspect", "Summarise a .3mf: plates, objects, parts, extruders, settings, bed, map, pauses")
class Inspect(Command):
    def configure(self, p):
        p.add_argument("file")

    def run(self, a, app, out):
        s = app.inspect(a.file, features=out.as_json)  # only --json shows grams by feature, so only it parses the G-code
        lines = [
            f"{a.file}: {s['application']}, {'SLICED' if s['sliced'] else 'unsliced'}",
            f"printer {s['printer']}; process {s['process']}; bed {s['bed_type']}; map {s['filament_map']} ({s['filament_map_mode']}); "
            f"print sequence {s.get('print_sequence') or 'not set (by layer)'}",
        ]
        for f in s["filaments"]:
            lines.append(f"filament {f['slot']}: {f['preset']} ({f['type']}) {f['colour'] or ''}")
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
            lines.append(
                f"object {o['name']}: filament {o['extruder']}, settings {o['settings'] or '{}'}, parts [{parts}]"
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
        p.add_argument("--extruder", action="append", default=[], metavar="OBJECT[/PART]:N")
        p.add_argument("--range", action="append", default=[], metavar="OBJECT:MIN-MAX:KEY=VALUE[,KEY=VALUE]")
        p.add_argument("--pause", action="append", default=[], metavar="PLATE:Z")
        p.add_argument("--colour-change", action="append", default=[], metavar="PLATE:Z:FILAMENT")
        p.add_argument("--custom-gcode", action="append", default=[], metavar="PLATE:Z:GCODE")
        p.add_argument("--rename-plate", action="append", default=[], metavar="PLATE:NAME")
        p.add_argument("--rename-object", action="append", default=[], metavar="OLD:NEW")
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
            help="append filament slot N+1 (slots 1..N unchanged); colour defaults to the preset's, extruder to 1",
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
        except EditError as e:
            out.emit({"error": f"EditError: {e}"}, [f"EditError: {e}"])
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
        except EditError as e:
            out.emit({"error": f"EditError: {e}"}, [f"EditError: {e} (the input file is unchanged)"])
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
            xs, ys = (v.strip() for v in pos.split(",")) if pos.count(",") == 1 else ("", "")
            signed = [v[:1] in ("+", "-") for v in (xs, ys)]
            if any(signed) and not all(signed):
                raise ValueError(f"--move {x!r}: a relative move needs a sign on both values (+5,-5); absolute values are the min corner")
            mx, my = _floats(f"{xs},{ys}", 2, "--move")
            ps.add(MoveObject(obj, mx, my, all(signed), _plate_arg(plate, "--move"), a.strict))
        for x in a.tower:
            ps.add(SetTower(*_tower_arg(x)))
        return ps


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
        if r.checks_skipped:
            lines.append("- " + r.checks_skipped["report_line"])
        for est in r.estimates:
            lines += estimate_lines(est)
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
        app.open_in_studio(a.file)
        out.emit({"opened": a.file}, [f"opened {a.file} in Bambu Studio"])
        return 0


@command("printer", "Read the printer over LAN MQTT (read-only): status | filaments | watch | discover | raw")
class Printer(Command):
    def configure(self, p):
        p.add_argument("action", choices=("status", "filaments", "watch", "discover", "raw"))
        p.add_argument("--ip")
        p.add_argument("--serial")
        p.add_argument("--wait", type=float, default=8.0)
        p.add_argument("--every", type=int, default=300, help="watch interval in seconds")
        p.add_argument("--ssdp", type=float, default=0, help="discover: also listen for SSDP this many seconds")
        p.add_argument("--printer-preset", help="filaments: the machine preset to name presets for (default: the GUI's current one)")
        p.add_argument(
            "--insecure",
            action="store_true",
            help="skip TLS verification against Bambu's CA and the serial (the access code then goes to an unverified endpoint)",
        )

    def run(self, a, app, out):
        from .printer import ArpProbe, SsdpListen

        svc = app.printer(a.ip, a.serial, a.wait, insecure=a.insecure)
        if a.action == "discover":
            hits = ArpProbe().find() + (SsdpListen(a.ssdp).find() if a.ssdp else [])
            out.emit(
                hits,
                [f"{h['ip']}  ({h['method']}" + (f", {h.get('model')} {h.get('connect')}" if h.get("model") else "") + ")" for h in hits]
                or ["no printer found"],
            )
            return 0 if hits else 1
        if a.action == "raw":
            print(json.dumps(svc.raw_report(), indent=1, default=str))
            return 0
        if a.action == "status":
            st = svc.status()
            out.emit(st.to_json(), st.lines())
            return 0
        if a.action == "filaments":
            lf = svc.filaments(app.presets(), a.printer_preset or app.gui_machine_preset())
            out.emit(lf.to_json(), lf.lines())
            return 0
        for st in svc.watch(a.every):
            out.emit(st.to_json(), st.lines() + [""])
            sys.stdout.flush()
        return 0


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


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    out = Output(a.json)
    try:
        app = Bambu()
        return _REGISTRY[a.cmd].run(a, app, out)
    except BambuError as e:
        msg = f"{type(e).__name__}: {e}"
        if a.json:
            print(json.dumps({"error": msg}))
        else:
            print(msg, file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130
