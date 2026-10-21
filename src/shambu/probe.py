"""Measure how far a part's support reaches with a solo slice (opt-in: `shambu check|build --probe-supports`).

Why: the mesh does not tell which side tree supports grow on. On the printed plate 14 the housing's support reached
20.9 mm past its box on -x and at most 2.9 mm on the other sides, so an estimate has to assume the worst on all four
sides (reference 08 section 11.2). A probe slices one supported part alone, in its final orientation, and reads the
`; FEATURE: Support*` extrusions out of the G-code: per side, how far they reach past the part's own model
extrusions (centrelines). layout.py only receives the resulting `Spread`, so the planning core stays pure.

Cache: `<outdir>/.probe/<mesh sha256>-<orientation>-<support keys hash>.json`, so a second build of the same part,
orientation and support settings does not slice again. A probe slice passes `--no-check` and has no prime tower:
it measures the support, it is not a clash test (the clash test is the plate's own `build --slice`).

Not here: per-z bands (the widest band, z 0..5, is the one that meets a neighbour standing on the bed), merged
parts (they keep the estimate), and rotation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import assemble as A
from . import presets as P
from .errors import StudioError
from .layout import Spread, is_tree
from .studio import CliCommand, MeshInfo, StudioCLI

log = logging.getLogger(__name__)

SUPPORT_FEATURES = frozenset({"Support", "Support interface", "Support transition"})
NOT_MODEL = SUPPORT_FEATURES | {"Brim", "Skirt", "Prime tower", "Wipe tower", "Custom"}
# Settings that change where support goes; their values are part of the cache key.
SUPPORT_KEY_PARTS = ("support", "raft", "tree", "layer_height", "bridge", "overhang")
_NUM = re.compile(r"([XYEIJ])(-?\d+(?:\.\d*)?|-?\.\d+)")


def parse_support_spread(lines: Iterable[str], arc_step: float = 1.0) -> tuple[float, float, float, float] | None:
    """Per side (-x, +x, -y, +y): how far support extrusions reach past the box of the model extrusions, mm, 0 when
    the support stays inside. With object labels (`; start printing object` / `; stop printing object`, written when
    the plate holds two or more objects) only moves inside them count. A solo slice has no labels (live, Studio
    02.08.02.61): then every move after the first `; CHANGE_LAYER` counts. `Custom` moves (start G-code, purge line)
    never count. Arcs (G2/G3) are sampled every `arc_step` mm. None: no model extrusion."""
    feat = ""
    inside = labelled = layered = False
    x: float | None = None
    y: float | None = None
    boxes = {k: [math.inf, math.inf, -math.inf, -math.inf] for k in ("model@obj", "support@obj", "model@layer", "support@layer")}

    def grow(box: list[float], px: float, py: float) -> None:
        box[0], box[1] = min(box[0], px), min(box[1], py)
        box[2], box[3] = max(box[2], px), max(box[3], py)

    for line in lines:
        if line.startswith(";"):
            if line.startswith("; FEATURE:"):
                feat = line.split(":", 1)[1].strip()
            elif line.startswith("; start printing object"):
                inside = labelled = True
            elif line.startswith("; stop printing object"):
                inside = False
            elif line.startswith("; CHANGE_LAYER"):
                layered = True
            continue
        code = line[:3]
        if code not in ("G0 ", "G1 ", "G2 ", "G3 "):
            continue
        d = {k: float(v) for k, v in _NUM.findall(line.split(";", 1)[0][3:])}
        nx, ny = d.get("X", x), d.get("Y", y)
        cls = "support" if feat in SUPPORT_FEATURES else ("model" if feat not in NOT_MODEL else None)
        if cls and (inside or layered) and d.get("E", 0.0) > 0 and x is not None and y is not None and nx is not None and ny is not None:
            pts = [(x, y), (nx, ny)]
            if code in ("G2 ", "G3 ") and ("I" in d or "J" in d):
                cx, cy = x + d.get("I", 0.0), y + d.get("J", 0.0)
                r = math.hypot(x - cx, y - cy)
                a0, a1 = math.atan2(y - cy, x - cx), math.atan2(ny - cy, nx - cx)
                if code == "G2 ":
                    while a1 >= a0:
                        a1 -= 2 * math.pi
                else:
                    while a1 <= a0:
                        a1 += 2 * math.pi
                n = max(2, int(abs(a1 - a0) * r / arc_step))
                pts += [(cx + r * math.cos(a0 + (a1 - a0) * k / n), cy + r * math.sin(a0 + (a1 - a0) * k / n)) for k in range(1, n)]
            for where, on in (("obj", inside), ("layer", layered)):
                if on:
                    for px, py in pts:
                        grow(boxes[f"{cls}@{where}"], px, py)
        x, y = nx, ny
    where = "obj" if labelled else "layer"
    model, support = boxes[f"model@{where}"], boxes[f"support@{where}"]
    if model[0] == math.inf:
        return None
    if support[0] == math.inf:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        max(0.0, model[0] - support[0]),
        max(0.0, support[2] - model[2]),
        max(0.0, model[1] - support[1]),
        max(0.0, support[3] - model[3]),
    )


@dataclass(frozen=True)
class ProbeRequest:
    """One supported part to probe: the STL the CLI loads (already oriented), its Studio box, its filament slot and
    its own object settings (`set` plus recipes)."""

    key: str
    stl: Path
    info: MeshInfo
    filament: int
    settings: dict[str, str] = field(default_factory=dict)
    orientation: str = "as-is"


class SpreadProbe(Protocol):
    """Measures one part's support spread. StudioProbe slices with Bambu Studio; tests use a fake. Raises a
    BambuError when it cannot measure (the pipeline then keeps the estimate, with a warning)."""

    def measure(self, req: ProbeRequest) -> Spread: ...


@dataclass
class StudioProbe:
    """A solo `--slice` per part through StudioCLI, cached on disk (see the module docstring)."""

    studio: StudioCLI
    cache: Path
    machine: dict
    process: dict
    filaments: list[dict]
    bed_type: str = "Textured PEI Plate"
    filament_map: list[int] = field(default_factory=list)
    timeout: int = 900
    slices: int = 0  # solo slices run by this probe (the cache answered the rest)

    def _eff(self, req: ProbeRequest) -> dict:
        return {**self.process, **req.settings}

    def cache_path(self, req: ProbeRequest) -> Path:
        mesh = hashlib.sha256(req.stl.read_bytes()).hexdigest()
        eff = self._eff(req)
        keys = {k: str(v) for k, v in sorted(eff.items()) if any(t in k for t in SUPPORT_KEY_PARTS)}
        keys["@machine"] = str(self.machine.get("name", ""))
        keys["@filament"] = str(req.filament)
        sh = hashlib.sha1(json.dumps(keys, sort_keys=True).encode("utf-8")).hexdigest()[:12]
        orient = re.sub(r"[^A-Za-z0-9_.+-]", "_", req.orientation or "as-is")
        return self.cache / f"{mesh}-{orient}-{sh}.json"

    def measure(self, req: ProbeRequest) -> Spread:
        eff = self._eff(req)
        cp = self.cache_path(req)
        sides: tuple[float, float, float, float] | None = None
        if cp.is_file():
            try:
                raw = json.loads(cp.read_text(encoding="utf-8"))["sides"]
                sides = (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
            except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
                log.warning("unreadable probe cache %s: %s", cp, e)
        if sides is None:
            sides = self._slice(req, cp)
            cp.write_text(json.dumps({"key": req.key, "stl": req.stl.name, "sides": list(sides)}, indent=1), encoding="utf-8")
        return Spread(*sides, source="probe", tree=is_tree(eff), note="support measured by a solo slice")

    def _slice(self, req: ProbeRequest, cp: Path) -> tuple[float, float, float, float]:
        work = cp.with_suffix("")
        work.mkdir(parents=True, exist_ok=True)
        process = {**self.process, "enable_prime_tower": "0"}  # one object, no tower: nothing but the part and its support
        n_ext = len(P.machine_facts(self.machine)["extruders"])
        fils = [P.write(f, work / f"filament-{i}.json") for i, f in enumerate(self.filaments, 1)]
        bed = P.machine_facts(self.machine)["bed"]
        cx, cy = (bed["x"][0] + bed["x"][1]) / 2, (bed["y"][0] + bed["y"][1]) / 2
        i = req.info
        pos = (cx - i.size[0] / 2 - i.min[0], cy - i.size[1] / 2 - i.min[1], -i.min[2])
        params = {"curr_bed_type": self.bed_type}
        fmap = self.filament_map or [min(k, n_ext) for k in range(1, len(self.filaments) + 1)]
        if len(self.filaments) > 1:
            params.update({"filament_map_mode": "Manual", "filament_map": ",".join(str(v) for v in fmap)})
        plate = A.Plate("probe", [A.Item(req.stl, req.filament, 1, "normal_part", 0, [pos], dict(req.settings))], plate_params=params)
        alist = A.AssembleList([plate]).write(work / "assemble.json")
        cmd = (
            CliCommand()
            .load_settings(P.write(self.machine, work / "machine.json"), P.write(process, work / "process.json"))
            .load_filaments(*fils)
            .load_assemble_list(alist)
            .setting("curr_bed_type", self.bed_type)
            .setting("nozzle_volume_type", ["Standard"] * n_ext)
            .setting("filament_colour", ";".join(f["filament_colour"][0] for f in self.filaments))
            .flag("allow-rotations", False)
        )
        if len(self.filaments) > 1:
            cmd.setting("filament_map_mode", "Manual").setting("filament_map", fmap)
        cmd.slice(0, no_check=True, max_seconds=600).export_3mf("probe.gcode.3mf", work)
        out = work / "probe.gcode.3mf"
        out.unlink(missing_ok=True)
        rr = self.studio.run(cmd, name="probe", outputdir=work, timeout=self.timeout, expect_output=out)
        self.slices += 1
        if not rr.ok:
            raise StudioError(f"the support probe of {req.key} did not slice: {rr.summary()}")
        try:
            with zipfile.ZipFile(out) as z:
                name = next((n for n in z.namelist() if n.startswith("Metadata/") and n.endswith(".gcode")), None)
                if name is None:
                    raise StudioError(f"the support probe of {req.key} wrote no G-code ({out})")
                with z.open(name) as fh:
                    sides = parse_support_spread(line.decode("utf-8", "replace") for line in fh)
        except (OSError, zipfile.BadZipFile) as e:
            raise StudioError(f"the support probe of {req.key}: cannot read {out}: {e}") from e
        if sides is None:
            raise StudioError(f"the support probe of {req.key}: the G-code holds no model extrusion")
        return sides
