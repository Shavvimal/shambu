"""Measure where a part's support goes with a solo slice (opt-in: `shambu check|build --probe-supports`).

Why: the mesh does not tell which side tree supports grow on. On the printed plate 14 the housing's support reached
20.9 mm past its box on -x and at most 2.9 mm on the other sides, so an estimate has to assume the worst on all four
sides (reference 08 section 11.2). A probe slices one supported part alone, in its final orientation, at the bed
centre (Studio clips tree support at the bed border, TreeSupport.cpp:626-629, so a probe at the edge would
under-measure), and reads the `; FEATURE: Support*` extrusions out of the G-code. It returns two things:
- the four spreads past the part's own model extrusions (centrelines), for the report and the packer;
- a support mask: the 0.5 mm cells the support centrelines touch, in the part's footprint frame. The pair rule uses
  the mask, because support inside a U-shaped notch stays inside the box (all four spreads 0) and still meets a part
  nested in the notch.
layout.py only receives the resulting `Spread` and mask, so the planning core stays pure.

Cache: `<outdir>/.probe/<mesh sha256>-<orientation>-<settings hash>.json`. The hash covers the Studio version, the
machine, the filament slot and every resolved setting that changes support. Each probe slices in its own directory
`<outdir>/.probe/<same name>/` with its own log, never in the plan's export directory. A probe slice passes
`--no-check` and has no prime tower: it measures the support, it is not a clash test (the clash test is the plate's
own `build --slice`).

Design: `SpreadProbe` is the port the pipeline calls (`measure(ProbeRequest) -> ProbeResult`); `StudioProbe` is
its adapter over `StudioCLI` (the only implementation that runs Studio), and the tests use a fake probe and a fake
StudioCLI that writes fixture G-code. `parse_support` is a pure function over G-code lines. Every failure (no
slice, no G-code, a bad archive, no model extrusion, a file that cannot be read or written) raises a `StudioError`
that names the path; the pipeline then keeps the estimate and warns. An unreadable cache entry is logged and sliced
again.

Not here: per-z bands (the mask is the projection of every layer), merged parts (they keep the estimate), rotation.
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
from .errors import BambuError, StudioError
from .layout import SUPPORT_MASK_CELL, Footprint, RasterFootprint, Spread, clipped_at_bed, mask_from_points
from .studio import CliCommand, MeshInfo, StudioCLI

log = logging.getLogger(__name__)

SUPPORT_FEATURES = frozenset({"Support", "Support interface", "Support transition"})
NOT_MODEL = SUPPORT_FEATURES | {"Brim", "Skirt", "Prime tower", "Wipe tower", "Custom"}
# Bump when a cached measurement may be wrong or its fields change. 3: E-only unretractions no longer count as
# extrusion (they moved the model box to the travel destination: the cradle's -x spread read 9.1 for 14.9 mm).
CACHE_FORMAT = 3
_NUM = re.compile(r"([XYEIJ])(-?\d+(?:\.\d*)?|-?\.\d+)")


@dataclass
class ParsedSupport:
    """What the G-code says: the model extrusions' box (bed coordinates), the support points sampled at least every
    `step` mm, and the four spreads past the model box (None when there is no model extrusion)."""

    sides: tuple[float, float, float, float] | None
    model_box: tuple[float, float, float, float] | None
    support_points: list[tuple[float, float]]


def parse_support(lines: Iterable[str], step: float = 0.4) -> ParsedSupport:
    """Read a G-code. With object labels (`; start printing object` / `; stop printing object`, written when the
    plate holds two or more objects) only moves inside them count. A solo slice has no labels (live, Studio
    02.08.02.61): then every move after the first `; CHANGE_LAYER` counts. `Custom` moves (start G-code, purge line)
    never count. Lines and arcs (G2/G3) are sampled every `step` mm. A move with a positive E but no XY path (the
    unretraction `G1 E.8` after a travel, before the next feature comment) is not an extrusion: it would put the
    previous feature at the travel destination. An arc whose endpoints coincide is a full circle and counts."""
    feat = ""
    inside = labelled = layered = False
    x: float | None = None
    y: float | None = None
    boxes = {k: [math.inf, math.inf, -math.inf, -math.inf] for k in ("model@obj", "support@obj", "model@layer", "support@layer")}
    points: dict[str, list[tuple[float, float]]] = {"obj": [], "layer": []}

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
                n = max(2, math.ceil(abs(a1 - a0) * r / step))
                pts = [(cx + r * math.cos(a0 + (a1 - a0) * k / n), cy + r * math.sin(a0 + (a1 - a0) * k / n)) for k in range(n + 1)]
            elif nx == x and ny == y:  # an unretraction or a pure E move: no path on the bed
                continue
            else:
                n = max(1, math.ceil(math.hypot(nx - x, ny - y) / step)) if cls == "support" else 1
                pts = [(x + (nx - x) * k / n, y + (ny - y) * k / n) for k in range(n + 1)]
            for where, on in (("obj", inside), ("layer", layered)):
                if on:
                    for px, py in pts:
                        grow(boxes[f"{cls}@{where}"], px, py)
                    if cls == "support":
                        points[where] += pts
        x, y = nx, ny
    where = "obj" if labelled else "layer"
    model, support = boxes[f"model@{where}"], boxes[f"support@{where}"]
    if model[0] == math.inf:
        return ParsedSupport(None, None, [])
    mb = (model[0], model[1], model[2], model[3])
    if support[0] == math.inf:
        return ParsedSupport((0.0, 0.0, 0.0, 0.0), mb, [])
    sides = (
        max(0.0, model[0] - support[0]),
        max(0.0, support[2] - model[2]),
        max(0.0, model[1] - support[1]),
        max(0.0, support[3] - model[3]),
    )
    return ParsedSupport(sides, mb, points[where])


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


@dataclass(frozen=True)
class ProbeResult:
    spread: Spread
    mask: Footprint | None = None  # support centreline cells in the part's footprint frame (bbox minimum at 0, 0)


class SpreadProbe(Protocol):
    """Measures one part's support. StudioProbe slices with Bambu Studio; tests use a fake. Raises a BambuError when
    it cannot measure (the pipeline then keeps the estimate, with a warning)."""

    def measure(self, req: ProbeRequest) -> ProbeResult: ...


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
    _version: str | None = None

    def _eff(self, req: ProbeRequest) -> dict:
        return {**self.process, **req.settings}

    def studio_version(self) -> str:
        if self._version is None:
            try:
                self._version = str(self.studio.version())
            except (BambuError, OSError) as e:
                log.warning("Bambu Studio's version is unknown (%s): the support probe cache keys on 'unknown'", e)
                self._version = "unknown"
        return self._version

    def cache_path(self, req: ProbeRequest) -> Path:
        """`<cache>/<mesh sha256>-<orientation>-<hash>.json`. The hash covers everything that feeds the slice: the
        Studio version, the complete resolved machine preset, every resolved filament preset, the process with the
        part's own settings, the filament slot and the cache format."""
        try:
            mesh = hashlib.sha256(req.stl.read_bytes()).hexdigest()
        except OSError as e:
            raise StudioError(f"the support probe of {req.key}: cannot read {req.stl}: {e}") from e
        feed = {
            "studio": self.studio_version(),
            "machine": self.machine,
            "filaments": self.filaments,
            "settings": self._eff(req),
            "slot": req.filament,
            "map": self.filament_map,
            "bed": self.bed_type,
            "format": CACHE_FORMAT,
        }
        sh = hashlib.sha1(json.dumps(feed, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
        orient = re.sub(r"[^A-Za-z0-9_.+-]", "_", req.orientation or "as-is")
        return self.cache / f"{mesh}-{orient}-{sh}.json"

    def measure(self, req: ProbeRequest) -> ProbeResult:
        try:
            return self._measure(req)
        except OSError as e:  # a directory or file the probe cannot create, read or write
            raise StudioError(f"the support probe of {req.key}: {getattr(e, 'filename', None) or self.cache}: {e}") from e

    def _measure(self, req: ProbeRequest) -> ProbeResult:
        eff = self._eff(req)
        cp = self.cache_path(req)
        got: tuple[tuple[float, float, float, float], RasterFootprint | None] | None = None
        if cp.is_file():
            try:
                d = json.loads(cp.read_text(encoding="utf-8"))
                raw = d["sides"]
                got = (
                    (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3])),
                    (RasterFootprint.from_json(d["mask"]) if d.get("mask") else None),
                )
            except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
                log.warning("unreadable probe cache %s: %s", cp, e)
        if got is None:
            got = self._slice(req, cp)
            cp.write_text(
                json.dumps(
                    {
                        "key": req.key,
                        "stl": req.stl.name,
                        "studio": self.studio_version(),
                        "sides": list(got[0]),
                        "mask": got[1].to_json() if got[1] else None,
                    },
                    indent=1,
                ),
                encoding="utf-8",
            )
        spread = Spread(*got[0], source="probe", tree=clipped_at_bed(eff), note="support measured by a solo slice")
        return ProbeResult(spread, got[1])

    def _slice(self, req: ProbeRequest, cp: Path) -> tuple[tuple[float, float, float, float], RasterFootprint | None]:
        work = cp.with_suffix("")  # one directory per cache key: probes never share a log or an output
        work.mkdir(parents=True, exist_ok=True)
        process = {**self.process, "enable_prime_tower": "0"}  # one object, no tower: nothing but the part and its support
        facts = P.machine_facts(self.machine)
        n_ext = len(facts["extruders"])
        fils = [P.write(f, work / f"filament-{i}.json") for i, f in enumerate(self.filaments, 1)]
        bed = facts["bed"]
        cx, cy = (bed["x"][0] + bed["x"][1]) / 2, (bed["y"][0] + bed["y"][1]) / 2
        i = req.info
        origin = (cx - i.size[0] / 2, cy - i.size[1] / 2)  # where the part's bbox minimum lands: the footprint frame
        pos = (origin[0] - i.min[0], origin[1] - i.min[1], -i.min[2])
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
        rr = self.studio.run(cmd, name=f"probe-{work.name[:16]}", outputdir=work, timeout=self.timeout, expect_output=out)
        self.slices += 1
        if not rr.ok:
            raise StudioError(f"the support probe of {req.key} did not slice: {rr.summary()}")
        try:
            with zipfile.ZipFile(out) as z:
                name = next((n for n in z.namelist() if n.startswith("Metadata/") and n.endswith(".gcode")), None)
                if name is None:
                    raise StudioError(f"the support probe of {req.key} wrote no G-code ({out})")
                with z.open(name) as fh:
                    parsed = parse_support(line.decode("utf-8", "replace") for line in fh)
        except (OSError, zipfile.BadZipFile) as e:
            raise StudioError(f"the support probe of {req.key}: cannot read {out}: {e}") from e
        if parsed.sides is None:
            raise StudioError(f"the support probe of {req.key}: the G-code holds no model extrusion")
        mask = mask_from_points(((px - origin[0], py - origin[1]) for px, py in parsed.support_points), SUPPORT_MASK_CELL)
        return parsed.sides, mask
