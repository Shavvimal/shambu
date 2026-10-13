"""Command line: `bambu <command> ...`. Each subcommand is a small class registered with @command.

Output is human text by default and JSON with `--json`, so an agent can parse it. Exit codes:
0 ok, 1 a check or verification failed, 2 bad usage, 3 an environment error (binary, config).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from pathlib import Path

from . import __version__, recipes
from .errors import BambuError, BuildError
from .facade import Bambu
from .patches import (
    AddCustomGcode,
    ClearObjectSetting,
    PatchSet,
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
from .spec import Spec

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
@command("doctor", "Check the Studio install, profiles, config and the X2D machine facts")
class Doctor(Command):
    def run(self, a, app, out):
        d = app.doctor()
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
        lines.append("recipes: " + ", ".join(d["recipes"]))
        out.emit(d, lines)
        return 0


@command("info", "Measure STL/OBJ/3MF files with Studio's --info")
class Info(Command):
    def configure(self, p):
        p.add_argument("files", nargs="+")

    def run(self, a, app, out):
        infos = [app.info(f) for f in a.files]
        out.emit(infos, [i.describe() for i in infos])
        return 0 if all(i.manifold for i in infos) else 1


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
        from .build import load_spec

        s = load_spec(a.file)
        errs = s.validate()
        out.emit({"ok": not errs, "problems": errs}, ["spec ok" if not errs else "spec problems:"] + ["- " + e for e in errs])
        return 0 if not errs else 1


@command("orient", "Score orientations of an STL: bed contact, overhang shadow, height; optionally write the best as a new STL")
class Orient(Command):
    def configure(self, p):
        p.add_argument("file")
        p.add_argument("--threshold", type=float, default=45.0, help="overhang angle from vertical that needs support (default 45)")
        p.add_argument("--top", type=int, default=12, help="how many of the largest flat faces to try (default 12)")
        p.add_argument("--write", metavar="DIR", help="write the best orientation as <DIR>/<stem>__<tag>.stl")
        p.add_argument("--pick", type=int, default=1, help="with --write: which row of the table to write (1 = best)")

    def run(self, a, app, out):
        from . import mesh as M

        facets = M.read_stl(a.file)
        if not facets:
            return usage(out, f"{a.file}: the STL has no facets")
        cands = M.candidates(facets, a.threshold, a.top)
        summary = M.Summary(Path(a.file), len(facets), len(M.split_bodies(facets)), cands)
        lines = [f"{a.file}: {len(facets)} facets, {summary.bodies} body(ies); overhang threshold {a.threshold:g} deg from vertical", ""]
        lines += M.describe_candidates(cands)
        lines += [
            "",
            'spec: add "lay_on_face": [nx, ny, nz] (and optionally "rotate": [x, y, z]) to the part; `shambu sweep --orientations <part>` slices the best few.',
        ]
        data = summary.to_json()
        if a.write:
            c = cands[max(0, min(len(cands), a.pick) - 1)]
            path = M.oriented_copy(a.file, M.Orientation(lay_on_face=c.normal), a.write)
            data["written"] = str(path)
            lines.append(f"written: {path} ({c.label})")
        out.emit(data, lines)
        return 0


@command("rotate", "Write a rotated copy of an STL (lay a face on the bed and/or Euler angles), dropped onto z = 0")
class Rotate(Command):
    def configure(self, p):
        p.add_argument("file")
        p.add_argument("-o", "--outdir", required=True)
        p.add_argument("--lay-on-face", metavar="NX,NY,NZ", help="outward normal of the face to put on the bed")
        p.add_argument("--rotate", metavar="X,Y,Z", help="degrees about X, then Y, then Z (applied after --lay-on-face)")

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
        if o.is_identity:
            return usage(out, "nothing to do: give --lay-on-face and/or --rotate")
        path = M.oriented_copy(a.file, o, a.outdir)
        m = M.metrics(M.read_stl(path))
        out.emit(
            {"written": str(path), **m.to_json()},
            [
                f"written {path}: {m.size[0]:.1f} x {m.size[1]:.1f} x {m.size[2]:.1f} mm, contact {m.contact_mm2:.0f} mm2, support shadow {m.shadow_mm2:.0f} mm2"
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

        bodies = M.split_bodies(M.read_stl(a.file))
        stem = Path(a.file).stem
        written = [str(M.write_stl(Path(a.outdir) / f"{stem}_body{i}.stl", b, f"{stem} body {i}")) for i, b in enumerate(bodies, 1)]
        out.emit({"bodies": len(bodies), "written": written}, [f"{len(bodies)} body(ies):", *written])
        return 0


@command("sweep", "Build (and slice) variants of a spec and compare time and filament: orientations, by-object, settings")
class Sweep(Command):
    def configure(self, p):
        p.add_argument("spec")
        p.add_argument("--variants", metavar="JSON", help="a JSON file: a list of variants (see `shambu spec` and sweep.py)")
        p.add_argument("--orientations", metavar="PART", action="append", default=[], help="try the best orientations of this part key")
        p.add_argument("--top", type=int, default=4, help="orientations per part for --orientations (default 4)")
        p.add_argument("--threshold", type=float, default=45.0)
        p.add_argument("--by-object", action="store_true", help="add a variant with every plate on print_sequence 'by object'")
        p.add_argument("--no-slice", action="store_true", help="only plan and export; no time or filament numbers")
        p.add_argument("--no-baseline", action="store_true")
        p.add_argument("--timeout", type=int, default=1200)

    def run(self, a, app, out):
        from . import sweep as S

        try:
            spec_json = json.loads(Path(a.spec).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise BuildError(f"{a.spec}: cannot read the spec: {e}") from e
        variants: list[dict] = S.load_variants(a.variants) if a.variants else []
        for key in a.orientations:
            variants += S.orientation_variants(spec_json, key, a.top, a.threshold)
        if a.by_object:
            variants.append(S.by_object_variant(spec_json))
        if not variants and a.no_baseline:
            return usage(out, "no variants: give --variants, --orientations or --by-object")
        S.check_names(variants, baseline=not a.no_baseline)
        results = S.run(
            spec_json,
            variants,
            lambda spec, do_slice: app.build(spec, timeout=a.timeout, do_slice=do_slice),
            do_slice=not a.no_slice,
            baseline=not a.no_baseline,
        )
        out.emit([r.to_json() for r in results], S.table(results))
        return 0 if all(r.ok for r in results) else 1  # one failed variant is a failed check (exit 1), as for `build`


@command("check", "Plan a spec: resolve presets, measure STLs, place parts, check limits. No export.")
class Check(Command):
    def configure(self, p):
        p.add_argument("spec")

    def run(self, a, app, out):
        r = app.build(a.spec, export=False)
        out.emit(r.to_json(), r.report.splitlines())
        return 0 if r.plan.ok else 1


@command("build", "Plan, export the .3mf with Studio's CLI, patch it, verify it, write the report")
class Build(Command):
    def configure(self, p):
        p.add_argument("spec")
        p.add_argument("--timeout", type=int, default=1200, help="seconds for the CLI export")
        p.add_argument("--keep-going", action="store_true", help="export even when a check failed")
        p.add_argument("--open", action="store_true", help="open the result in Bambu Studio afterwards")
        p.add_argument(
            "--slice", action="store_true", help="also slice headless: writes <name>.gcode.3mf with G-code and time/filament estimates"
        )

    def run(self, a, app, out):
        app.listeners.append(lambda ev, d: log.info("%s %s", ev, {k: v for k, v in d.items() if k != "argv"}))
        r = app.build(a.spec, timeout=a.timeout, keep_going=a.keep_going, do_slice=a.slice)
        out.emit(r.to_json(), r.report.splitlines())
        if a.open and r.run and r.run.ok:
            app.open_in_studio(r.project)
        return 0 if r.ok else 1


@command("inspect", "Summarise a .3mf: plates, objects, parts, extruders, settings, bed, map, pauses")
class Inspect(Command):
    def configure(self, p):
        p.add_argument("file")

    def run(self, a, app, out):
        s = app.inspect(a.file)
        lines = [
            f"{a.file}: {s['application']}, {'SLICED' if s['sliced'] else 'unsliced'}",
            f"printer {s['printer']}; process {s['process']}; bed {s['bed_type']}; map {s['filament_map']} ({s['filament_map_mode']})",
        ]
        for f in s["filaments"]:
            lines.append(f"filament {f['slot']}: {f['preset']} ({f['type']}) {f['colour'] or ''}")
        for p in s["plates"]:
            lines.append(
                f"plate {p['id']} '{p['name']}': bed {p['bed_type'] or 'project default'}, map {p['filament_maps'] or 'project default'}, objects {p['objects']}"
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
        p.add_argument("--dry-run", action="store_true")

    def run(self, a, app, out):
        try:
            ps = self._parse(a)
        except ValueError as e:
            out.emit({"error": f"bad patch option: {e}"}, [f"bad patch option: {e}; see --help for the formats"])
            return 2
        if not ps.patches:
            out.emit({"patched": []}, ["nothing to do: give at least one patch option"])
            return 2
        if a.dry_run:
            out.emit({"would_patch": ps.describe()}, ["would patch:"] + ["- " + d for d in ps.describe()])
            return 0
        done = app.patch(a.file, ps, a.out)
        out.emit({"file": a.out or a.file, "patched": done}, [f"patched {a.out or a.file}:"] + ["- " + d for d in done])
        return 0

    @staticmethod
    def _parse(a) -> PatchSet:
        ps = PatchSet()
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
        return ps


@command("open", "Open a project in Bambu Studio for the user")
class Open(Command):
    def configure(self, p):
        p.add_argument("file")

    def run(self, a, app, out):
        app.open_in_studio(a.file)
        out.emit({"opened": a.file}, [f"opened {a.file} in Bambu Studio"])
        return 0


@command("printer", "Read the printer over LAN MQTT (read-only): status | watch | discover | raw")
class Printer(Command):
    def configure(self, p):
        p.add_argument("action", choices=("status", "watch", "discover", "raw"))
        p.add_argument("--ip")
        p.add_argument("--serial")
        p.add_argument("--wait", type=float, default=8.0)
        p.add_argument("--every", type=int, default=300, help="watch interval in seconds")
        p.add_argument("--ssdp", type=float, default=0, help="discover: also listen for SSDP this many seconds")
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
    return p


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
