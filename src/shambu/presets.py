"""Bambu Studio presets: find them by name and flatten them for the command line.

Why this exists: the CLI (`--load-settings`, `--load-filaments`) does not resolve `inherits` or
`include` (src/libslic3r/Config.cpp load_from_json; the `*_full/` fallback folders are not in the
macOS bundle). It wants one complete JSON per preset with `type`, `name` and `from`. A machine
preset whose `include` templates are not merged yields a project with the generic 577-character
start G-code instead of the X2D's 14848-character one.

Design:
- `PresetSource` (Protocol) + `DirectorySource`: where preset files live.
- `ChainedSource`: Chain of Responsibility over sources, first hit wins (user dir, then the
  Application Support copy that Studio's profile updates refresh, then the app bundle).
- `PresetResolver`: flattens `inherits` and `include`, and shapes the result for the CLI.
Module-level helpers keep the common calls short.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .errors import PresetError

log = logging.getLogger(__name__)

KINDS = ("machine", "process", "filament")
APP = Path("/Applications/BambuStudio.app")
BUNDLE_PROFILES = APP / "Contents/Resources/profiles/BBL"
APPSUPPORT = Path.home() / "Library/Application Support/BambuStudio"
SYSTEM_PROFILES = APPSUPPORT / "system/BBL"
ID_KEY = {"machine": "printer_settings_id", "process": "print_settings_id", "filament": "filament_settings_id"}
_TEMPLATE_META = {"name", "type", "from", "instantiation", "inherits", "include"}


class PresetSource(Protocol):
    def find(self, kind: str, name: str) -> Path | None: ...
    def names(self, kind: str) -> Iterable[Path]: ...


@dataclass(frozen=True)
class DirectorySource:
    """A root holding `<kind>/<name>.json` files."""

    root: Path
    label: str = ""

    def find(self, kind: str, name: str) -> Path | None:
        p = self.root / kind / f"{name}.json"
        return p if p.is_file() else None

    def names(self, kind: str) -> Iterable[Path]:
        d = self.root / kind
        return sorted(d.glob("*.json")) if d.is_dir() else []


@dataclass
class ChainedSource:
    """Chain of Responsibility: ask each source in order, the first that has the file wins."""

    sources: list[PresetSource] = field(default_factory=list)

    def find(self, kind: str, name: str) -> Path | None:
        for s in self.sources:
            p = s.find(kind, name)
            if p is not None:
                return p
        return None

    def names(self, kind: str) -> Iterable[Path]:
        seen: dict[str, Path] = {}
        for s in reversed(self.sources):  # later (lower-priority) first so higher priority overwrites
            for p in s.names(kind):
                seen[p.stem] = p
        return seen.values()


def default_source() -> ChainedSource:
    chain = ChainedSource()
    user_base = APPSUPPORT / "user"
    if user_base.is_dir():
        for d in sorted(user_base.iterdir()):
            if d.is_dir():
                chain.sources.append(DirectorySource(d, f"user {d.name[:2]}…"))  # the Bambu account id is masked in output
    for root, label in ((SYSTEM_PROFILES, "Application Support system profiles"), (BUNDLE_PROFILES, "app bundle profiles")):
        if (root / "machine").is_dir():
            chain.sources.append(DirectorySource(root, label))
    if not any(isinstance(s, DirectorySource) and s.label.endswith("profiles") for s in chain.sources):
        raise PresetError(f"no Bambu profiles found under {SYSTEM_PROFILES} or {BUNDLE_PROFILES}")
    return chain


@dataclass
class PresetResolver:
    source: ChainedSource = field(default_factory=default_source)

    def load_raw(self, kind: str, name: str) -> dict:
        if kind not in KINDS:
            raise PresetError(f"kind must be one of {KINDS}, not {kind!r}")
        p = self.source.find(kind, name)
        if p is None:
            raise PresetError(f"{kind} preset '{name}' not found in {[getattr(s, 'label', s) for s in self.source.sources]}")
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def resolve(self, kind: str, name: str, _seen: tuple[str, ...] = ()) -> dict:
        """Flatten the `inherits` chain (parent first, child overrides) and merge `include` templates."""
        if name in _seen:
            raise PresetError(f"inherits loop: {' -> '.join(_seen + (name,))}")
        d = self.load_raw(kind, name)
        parent = d.pop("inherits", None)
        if parent:
            base = self.resolve(kind, parent, _seen + (name,))
            base.update(d)
            d = base
        for inc in d.pop("include", []) or []:
            t = self.load_raw(kind, inc)
            d.update({k: v for k, v in t.items() if k not in _TEMPLATE_META})
        return d

    def for_cli(self, kind: str, name: str, overrides: dict | None = None, new_name: str | None = None) -> dict:
        """A complete preset JSON the CLI accepts.

        Overrides are print setting keys with the string values Studio stores (`"6"`, `"40%"`).
        Any override or a new name turns the preset into `from: User` with `inherits` = the system
        preset, as Studio does for a modified preset.
        """
        d = self.resolve(kind, name)
        d["type"] = kind
        for k in ("instantiation", "setting_id"):
            d.pop(k, None)
        overrides = dict(overrides or {})
        if overrides or new_name:
            for k, v in overrides.items():
                d[k] = v if isinstance(v, (list, str)) else str(v)
            d["name"] = new_name or f"{name} (modified)"
            d["from"] = "User"
            d["inherits"] = name
            d["is_custom_defined"] = "0"
        else:
            d["name"] = name
            d["from"] = "system"
        d[ID_KEY[kind]] = [d["name"]] if kind == "filament" else d["name"]
        return d

    def list(self, kind: str, contains: str | None = None, printer: str | None = None) -> list[dict]:
        """Instantiable presets of a kind; `printer` keeps only those compatible with that machine."""
        out = []
        for p in self.source.names(kind):
            try:
                with open(p, encoding="utf-8") as f:
                    raw = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if raw.get("instantiation", "true") == "false":
                continue
            name = raw.get("name", p.stem)
            if contains and contains.lower() not in name.lower():
                continue
            cp = raw.get("compatible_printers")
            if printer and kind != "machine":
                if cp is None:
                    try:
                        cp = self.resolve(kind, name).get("compatible_printers")
                    except PresetError:
                        continue
                if cp is not None and printer not in cp:
                    continue
            out.append(
                {
                    "name": name,
                    "kind": kind,
                    "path": str(p),
                    "inherits": raw.get("inherits"),
                    "from": raw.get("from", "system"),
                    "compatible_printers": cp,
                }
            )
        return sorted(out, key=lambda e: e["name"])


def write(preset: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(preset, indent=1, ensure_ascii=False), encoding="utf-8")
    return path


def check_compatible(machine: dict, process: dict, filaments: Iterable[dict]) -> list[str]:
    """Mirror the CLI's compatibility rules (return codes -17 and -5) before running it."""
    problems = []
    sys_name = machine["name"] if machine.get("from") == "system" else machine.get("inherits", machine["name"])
    cp = process.get("compatible_printers") or []
    if sys_name not in cp:
        problems.append(f"process '{process['name']}' compatible_printers {cp} does not list '{sys_name}'")
    filaments = list(filaments)
    for f in filaments:
        fcp = f.get("compatible_printers") or []
        if fcp and sys_name not in fcp:
            problems.append(f"filament '{f['name']}' compatible_printers does not list '{sys_name}'")
    for p in (machine, process, *filaments):
        for k in ("type", "name", "from"):
            if not p.get(k):
                problems.append(f"preset '{p.get('name', '?')}' lacks required key {k}")
    for k in ("machine_start_gcode", "machine_end_gcode", "change_filament_gcode"):
        if not machine.get(k):
            problems.append(f"machine preset lacks {k}: the `include` templates were not resolved")
    return problems


def machine_facts(machine: dict) -> dict:
    """Bed, height and per-extruder reach from a flattened machine preset."""

    def pts(s: str) -> list[tuple[float, float]]:
        out = []
        for p in s.split(","):
            if p.strip():
                a, b = (float(x) for x in p.strip().split("x"))
                out.append((a, b))
        return out

    def box(points: list[tuple[float, float]]) -> dict:
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        return {"x": (min(xs), max(xs)), "y": (min(ys), max(ys))}

    bed_pts: list[tuple[float, float]] = []
    for p in machine.get("printable_area", []):
        bed_pts += pts(p)
    bed = box(bed_pts) if bed_pts else {"x": (0.0, 0.0), "y": (0.0, 0.0)}
    heights = [float(h) for h in machine.get("extruder_printable_height", [])]
    areas = [box(pts(a)) for a in machine.get("extruder_printable_area", [])]
    n = len(machine.get("nozzle_diameter", ["0.4"]))
    extruders = []
    for i in range(n):
        area = areas[i] if i < len(areas) else bed
        extruders.append(
            {
                "index": i + 1,
                "x": area["x"],
                "y": area["y"],
                "z_max": heights[i] if i < len(heights) else float(machine.get("printable_height", 0)),
                "type": (machine.get("extruder_type") or ["?"] * n)[i],
                "nozzle": (machine.get("nozzle_diameter") or ["?"] * n)[i],
            }
        )
    return {
        "name": machine.get("name"),
        "printer_model": machine.get("printer_model"),
        "bed": bed,
        "z_max": float(machine.get("printable_height", 0)),
        "extruders": extruders,
    }


# Short module-level helpers over a shared default resolver.
_default: PresetResolver | None = None


def resolver() -> PresetResolver:
    global _default
    if _default is None:
        _default = PresetResolver()
    return _default


def resolve(kind: str, name: str) -> dict:
    return resolver().resolve(kind, name)


def resolved_for_cli(kind: str, name: str, overrides: dict | None = None, new_name: str | None = None) -> dict:
    return resolver().for_cli(kind, name, overrides, new_name)


def list_names(kind: str, contains: str | None = None, printer: str | None = None) -> list[dict]:
    return resolver().list(kind, contains, printer)
