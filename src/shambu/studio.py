"""Adapter over the Bambu Studio binary, plus a Builder for its argument list.

Facts this module encodes (source v02.08.02.61, src/BambuStudio.cpp):
- Any action flag makes the run headless. With no action the GUI starts. The builder refuses to
  emit a command without an action.
- Bool flags never take a separate value: `--min-save 1` makes `1` an input file. Use `--flag=0|1`.
- `--export-3mf` is joined to `--outputdir`: pass a bare file name and an existing output dir.
- Actions run in argv order: `--no-check`, `--mstpp`, `--mtcpp`, `--export-slicedata` must come
  before `--slice`. The builder orders them.
- `--pipe` is Linux-only. Progress comes from stderr at `--debug=4`.
- `result.json` lands in `--outputdir`; it is absent after a parse error or a crash.
- The shell exit status is the return code modulo 256 (-17 shows as 239).
- `--orient 1` replaces each object's config with the loaded process only when `--load-settings` loaded one, and that
  hurts only objects that carry per-object keys (assemble-list `print_params`). `orient()` runs it alone on positional
  STLs (pass 1); the slicing run with the assemble list (pass 2) never passes `--orient`. Transforms run in argv order,
  so `--convert-unit` comes before `--orient`. The candidate table goes to stdout at any debug level.
- After any Studio crash, macOS shows a modal "reopen windows?" alert inside the CLI process, which then
  blocks for ever at 0 % CPU (that was the "5 minute export"). `CFFIXED_USER_HOME` pointed at a private
  empty directory removes the alert without touching the real ~/Library. Every call here sets it.

Error policy: `run()` reports a failure in its RunResult (a timeout is code 124). `info()` and
`export_settings()` return parsed data, so they raise StudioError for a timeout, a non-zero exit, or
output they cannot parse. Both paths go through `_exec()`.
"""

from __future__ import annotations

import json
import logging
import os
import plistlib
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from .errors import StudioError

log = logging.getLogger(__name__)

APP = Path("/Applications/BambuStudio.app")
BINARY = APP / "Contents/MacOS/BambuStudio"
DEFAULT_SCRATCH = Path(os.environ.get("TMPDIR", "/tmp")) / "shambu"

RETURN_CODES = {
    0: "success",
    -1: "environment error",
    -2: "invalid params or combination",
    -3: "file not found",
    -4: "Bambu 3MF must be the first input",
    -5: "preset / assemble-list / custom-gcode JSON invalid",
    -6: "model file unreadable (or unknown key in assemble *_params)",
    -7: "not FFF",
    -8: "unsupported operation",
    -13: "3MF or PNG write failed (check --outputdir exists and --export-3mf is a bare name)",
    -14: "out of memory",
    -17: "process preset not compatible with the printer (compatible_printers)",
    -18: "invalid values in config (validate failed)",
    -19: "post_process not supported",
    -21: "arrange failed: an object spilled off the plate",
    -22: "orient failed",
    -24: "3MF version not supported (try --allow-newer-file)",
    -50: "no object fully inside the plate",
    -51: "Print::validate error",
    -52: "object partly outside the bed",
    -58: "slicing time over --mstpp",
    -59: "triangle count over --mtcpp",
    -60: "all objects skipped",
    -61: "filament bed temperature is 0 for this bed type",
    -62: "filaments with very different temperatures (--allow-mix-temp)",
    -63: "object collision in sequential print",
    -64: "object collision (height) in by-layer print",
    -65: "spiral mode params invalid",
    -66: "a filament cannot be mapped to a reachable extruder",
    -67: "only one TPU filament supported",
    -68: "filament not supported by its extruder",
    -100: "slicing error or critical warning (empty layers / overlap)",
    -101: "G-code path conflicts",
    -102: "G-code path in unprintable area",
    -103: "filament unprintable on first layer",
    -104: "G-code path outside bed",
    -105: "G-code in wrapping detect area",
    -139: "crashed (signal)",
    124: "timed out",
}
# Options the parser treats as actions, in the order they must appear before `--slice`.
_PRE_SLICE_ACTIONS = ("--no-check", "--mtcpp", "--mstpp", "--export-slicedata", "--load-slicedata")
_ACTIONS = {
    "--export-3mf",
    "--slice",
    "--export-png",
    "--export-settings",
    "--export-stl",
    "--export-stls",
    "--info",
    "--uptodate",
    "--min-save",
    "--load-defaultfila",
    "--help",
    *_PRE_SLICE_ACTIONS,
}


def signed(status: int) -> int:
    """Shell exit status -> the CLI's negative return code."""
    return status - 256 if status > 127 else status


def _q(a: str) -> str:
    return f'"{a}"' if " " in a or ";" in a else a


@dataclass
class CliCommand:
    """Builder for one CLI invocation. Enforces the ordering and value rules above."""

    inputs: list[str] = field(default_factory=list)
    _opts: list[tuple[str, str | None]] = field(default_factory=list)
    _pre: list[tuple[str, str | None]] = field(default_factory=list)
    _slice: int | None = None

    def option(self, name: str, value: str | int | float | None = None) -> CliCommand:
        name = "--" + name.lstrip("-").replace("_", "-")
        if name == "--slice":
            self._slice = int(value or 0)
        elif name in _PRE_SLICE_ACTIONS:
            self._pre.append((name, None if value is None else str(value)))
        else:
            self._opts.append((name, None if value is None else str(value)))
        return self

    def flag(self, name: str, on: bool = True) -> CliCommand:
        return self.option(name, "1" if on else "0")

    def setting(self, key: str, value) -> CliCommand:
        """A print setting as `--key-with-dashes=value` (highest priority). Vectors join with ','."""
        v = ",".join(str(x) for x in value) if isinstance(value, (list, tuple)) else str(value)
        return self.option(key, v)

    def load_settings(self, *paths: str | Path) -> CliCommand:
        return self.option("load-settings", ";".join(str(Path(p).resolve()) for p in paths))

    def load_filaments(self, *paths: str | Path) -> CliCommand:
        return self.option("load-filaments", ";".join(str(Path(p).resolve()) for p in paths))

    def load_assemble_list(self, path: str | Path) -> CliCommand:
        return self.option("load-assemble-list", str(Path(path).resolve()))

    def export_3mf(self, bare_name: str, outputdir: str | Path) -> CliCommand:
        if "/" in bare_name:
            raise StudioError("--export-3mf wants a bare file name; the CLI joins it to --outputdir")
        return self.option("export-3mf", bare_name).option("outputdir", str(Path(outputdir).resolve()))

    def slice(self, plate: int = 0, no_check: bool = True, max_seconds: int | None = None) -> CliCommand:
        if no_check:
            self.option("no-check")
        if max_seconds:
            self.option("mstpp", max_seconds)
        return self.option("slice", plate)

    def has_action(self) -> bool:
        names = {n for n, _ in self._opts} | {n for n, _ in self._pre}
        return self._slice is not None or bool(names & _ACTIONS)

    def args(self) -> list[str]:
        if not self.has_action():
            raise StudioError("no action option: the CLI would open the GUI. Add --export-3mf, --info, ...")
        out: list[str] = []
        for n, v in self._opts:
            out += (
                [n]
                if v is None
                else (
                    [f"{n}={v}"]
                    if n.startswith("--")
                    and v
                    and not v.startswith("/")
                    and ";" not in v
                    and n not in ("--export-3mf", "--outputdir", "--datadir")
                    else [n, v]
                )
            )
        for n, v in self._pre:
            out += [n] if v is None else [f"{n}={v}"]
        if self._slice is not None:
            out += [f"--slice={self._slice}"]
        return out + list(self.inputs)


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class SlicedFilament:
    """One filament of a sliced plate in result.json. `main_g` is model plus support; `total_g` adds flush and prime tower."""

    id: int  # 1-based slot
    filament_id: str  # e.g. "GFA00"
    total_g: float
    main_g: float

    @property
    def waste_g(self) -> float:
        """Flush plus prime tower: total minus main (BS.cpp:7308-7330)."""
        return max(self.total_g - self.main_g, 0.0)


@dataclass(frozen=True)
class SlicedPlate:
    """One entry of result.json `sliced_plates[]` (BS.cpp:500-600, 7265-7330). Times are seconds in Normal mode."""

    id: int  # 1-based plate
    total_s: float  # total_predication
    main_s: float  # main_predication: total minus prepare, prime tower and flush time
    filament_change_times: int  # Studio counts filament loads, not nozzle switches: 0 on a two-nozzle X2D plate
    feature_times_s: dict[str, float]  # feature_type_times: ExtrusionRole name (and "Travel") -> seconds
    objects: list[dict]  # {id, name, triangle_count, bbox{x, y, z, width, depth, height}} after arrange
    filaments: list[SlicedFilament]
    warning_message: str = ""


@dataclass
class RunResult:
    cmd: list[str]
    status: int
    code: int
    seconds: float
    log: Path
    result: dict | None
    output: Path | None = None

    @property
    def ok(self) -> bool:
        return self.code == 0 and (self.output is None or self.output.exists())

    def meaning(self) -> str:
        return RETURN_CODES.get(self.code, f"return code {self.code}")

    def plates(self) -> list[SlicedPlate]:
        """`sliced_plates[]` from result.json with typed fields. Empty when the run did not slice, Studio wrote no
        result.json (a crash, a parse error), or the file is from an orient-only or export-only run."""
        raw = (self.result or {}).get("sliced_plates")
        if not isinstance(raw, list):
            return []
        out = []
        for p in raw:
            if not isinstance(p, dict):
                continue
            times = p.get("feature_type_times")
            fils = p.get("filaments")
            out.append(
                SlicedPlate(
                    id=int(_num(p.get("id"))),
                    total_s=_num(p.get("total_predication")),
                    main_s=_num(p.get("main_predication")),
                    filament_change_times=int(_num(p.get("filament_change_times"))),
                    feature_times_s={str(k): _num(v) for k, v in times.items()} if isinstance(times, dict) else {},
                    objects=[o for o in p.get("objects") or [] if isinstance(o, dict)],
                    filaments=[
                        SlicedFilament(
                            int(_num(f.get("id"))), str(f.get("filament_id") or ""), _num(f.get("total_used_g")), _num(f.get("main_used_g"))
                        )
                        for f in (fils if isinstance(fils, list) else [])
                        if isinstance(f, dict)
                    ],
                    warning_message=str(p.get("warning_message") or ""),
                )
            )
        return out

    def summary(self) -> str:
        s = f"exit {self.code} ({self.meaning()}), {self.seconds:.1f} s, log {self.log}"
        if self.result:
            s += f", prepare {self.result.get('prepare_time')} ms, export {self.result.get('export_time')} ms"
        if self.output:
            s += f", output {'written' if self.output.exists() else 'MISSING'}: {self.output}"
        return s


@dataclass
class MeshInfo:
    path: Path
    size: tuple[float, float, float]
    min: tuple[float, float, float]
    max: tuple[float, float, float]
    manifold: bool
    facets: int
    parts: int
    volume_mm3: float
    raw: dict = field(default_factory=dict, repr=False)
    objects: int = 1  # blocks in Studio's --info output: one per object of a 3MF, 1 for an STL

    @property
    def centre_xy(self) -> tuple[float, float]:
        return ((self.min[0] + self.max[0]) / 2, (self.min[1] + self.max[1]) / 2)

    def on_bed(self, tol: float = 1e-3) -> bool:
        return abs(self.min[2]) <= tol

    def describe(self) -> str:
        return (
            f"{self.path.name}: {self.size[0]:.1f} x {self.size[1]:.1f} x {self.size[2]:.1f} mm, "
            f"x {self.min[0]:.1f}..{self.max[0]:.1f}, y {self.min[1]:.1f}..{self.max[1]:.1f}, z {self.min[2]:.2f}..{self.max[2]:.1f}, "
            f"{self.facets} facets, {self.parts} part(s), {self.volume_mm3 / 1000:.1f} cm3, manifold {'yes' if self.manifold else 'NO'}"
            + (f" ({self.objects} objects: summed, bbox is the union of their own frames)" if self.objects > 1 else "")
        )


_INFO_KEYS = ("size_x", "size_y", "size_z", "min_x", "max_x", "min_y", "max_y", "min_z", "max_z", "manifold")
_PCT = re.compile(r"percent=(\d+)")

ProgressListener = Callable[[str, dict], None]


@dataclass
class StudioCLI:
    """Adapter: every call to the binary goes through here."""

    binary: Path = BINARY
    scratch: Path = DEFAULT_SCRATCH
    debug: int = 3
    listeners: list[ProgressListener] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.binary.is_file():
            raise StudioError(f"Bambu Studio binary not found at {self.binary}")

    # -- Observer hooks ---------------------------------------------------------------------
    def subscribe(self, fn: ProgressListener) -> None:
        self.listeners.append(fn)

    def _emit(self, event: str, **data) -> None:
        for fn in self.listeners:
            fn(event, data)

    # -- facts ------------------------------------------------------------------------------
    def version(self) -> str:
        with open(self.binary.parents[1] / "Info.plist", "rb") as f:
            return plistlib.load(f)["CFBundleShortVersionString"]

    def running_pids(self) -> list[int]:
        r = subprocess.run(["pgrep", "-f", str(self.binary)], capture_output=True, text=True)
        return [int(p) for p in r.stdout.split() if p.isdigit()]

    def datadir(self, name: str) -> Path:
        d = self.scratch / f"datadir-{name}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def env(self) -> dict[str, str]:
        """Process environment for the binary: a private AppKit home so no crash-history alert can block."""
        home = self.scratch / "home"
        home.mkdir(parents=True, exist_ok=True)
        return {**os.environ, "CFFIXED_USER_HOME": str(home)}

    # -- runs -------------------------------------------------------------------------------
    def _argv(self, datadir: Path, debug: int, args: list[str]) -> list[str]:
        return [str(self.binary), "--datadir", str(datadir), f"--debug={debug}", *args]

    def _exec(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout: float,
        stdout: int | IO[Any] = subprocess.PIPE,
        stderr: int | IO[Any] = subprocess.STDOUT,
    ) -> subprocess.CompletedProcess[str]:
        """Run the binary once with the private environment. Raises subprocess.TimeoutExpired (the
        child is killed first) and StudioError when the binary cannot start."""
        try:
            return subprocess.run(argv, stdout=stdout, stderr=stderr, cwd=cwd, timeout=timeout, env=self.env(), text=True, errors="replace")
        except OSError as e:
            raise StudioError(f"could not start {argv[0]}: {e}") from e

    def run(
        self,
        cmd: CliCommand,
        *,
        name: str = "run",
        outputdir: Path | None = None,
        timeout: int = 1200,
        expect_output: Path | None = None,
        debug: int | None = None,
    ) -> RunResult:
        datadir = self.datadir(name)
        if outputdir is not None:
            outputdir = Path(outputdir).resolve()
            outputdir.mkdir(parents=True, exist_ok=True)
            (outputdir / "result.json").unlink(missing_ok=True)
        log_path = (outputdir or datadir) / f"{name}.log"
        argv = self._argv(datadir, self.debug if debug is None else debug, cmd.args())
        self._emit("start", name=name, argv=argv, log=str(log_path))
        t0 = time.time()
        with open(log_path, "w") as lf:
            lf.write("$ " + " ".join(_q(a) for a in argv) + "\n\n")
            lf.flush()
            try:
                status = self._exec(argv, cwd=datadir, timeout=timeout, stdout=lf).returncode
            except subprocess.TimeoutExpired:
                lf.write(f"\n[timeout after {timeout} s]\n")
                status = 124
        seconds = time.time() - t0
        result = None
        if outputdir and (outputdir / "result.json").is_file():
            try:
                with open(outputdir / "result.json") as f:
                    result = json.load(f)
            except (OSError, ValueError) as e:
                log.warning("unreadable %s: %s", outputdir / "result.json", e)
            if result is not None and not isinstance(result, dict):
                result = None
        if status == 124:
            code = 124
        elif status < 0 or status in (134, 138, 139):
            code = -139
        elif result and isinstance(result.get("return_code"), int):
            code = result["return_code"]
        else:
            code = signed(status)
        rr = RunResult(argv, status, code, seconds, log_path, result, expect_output)
        self._emit("done", name=name, summary=rr.summary(), ok=rr.ok)
        return rr

    def info(self, path: str | os.PathLike, timeout: int = 120) -> MeshInfo:
        """`--info`: bounding box in the file's own coordinates, facet count, manifold flag.
        Raises StudioError on a timeout, a non-zero exit, or output it cannot parse."""
        path = Path(path).resolve()
        if not path.is_file():
            raise StudioError(f"model file not found: {path}")
        datadir = self.datadir("info")
        argv = self._argv(datadir, 0, ["--info", str(path)])
        try:
            r = self._exec(argv, cwd=datadir, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise StudioError(f"--info timed out after {timeout} s for {path}: {' '.join(_q(a) for a in argv)}") from e
        out = r.stdout or ""
        if r.returncode != 0:
            raise StudioError(
                f"--info failed for {path}: exit {signed(r.returncode)} "
                f"({RETURN_CODES.get(signed(r.returncode), 'unknown')}); tail: {out[-500:]}"
            )
        return parse_info(path, out)

    def export_settings(self, cmd: CliCommand, out: Path, name: str = "settings", timeout: int = 300) -> dict:
        """Run `--export-settings` with the given loads: the full merged config as the CLI sees it.
        Any old file at `out` is deleted first, so a failed run can never return stale settings."""
        out = Path(out).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.unlink(missing_ok=True)
        cmd.option("export-settings", str(out))
        t0 = time.time()
        rr = self.run(cmd, name=name, timeout=timeout, expect_output=out)
        if not rr.ok:
            raise StudioError(f"--export-settings failed: {rr.summary()}")
        if out.stat().st_mtime < t0 - 2:  # 2 s slack for coarse filesystem timestamps
            raise StudioError(f"--export-settings left an old file at {out}")
        try:
            with open(out) as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            raise StudioError(f"--export-settings wrote unreadable JSON to {out}: {e}") from e
        if not isinstance(data, dict):
            raise StudioError(f"--export-settings wrote {type(data).__name__}, not a JSON object, to {out}")
        return data

    def orient(
        self,
        paths: list[Path],
        outdir: Path,
        *,
        settings: list[Path] | None = None,
        convert_unit: bool = True,
        timeout: int = 300,
        name: str = "orient",
    ) -> OrientResult:
        """Pass 1 of native orientation: Studio's auto-orient (`--orient 1`) on positional STLs, each one exported as
        `<outdir>/obj_<i>_<stem>.stl` (i = argv order from 1) with the rotation baked in and dropped onto the bed.
        `settings` (machine and process JSON) make Studio use the process's `support_threshold_angle`; without them
        it uses 30 degrees. The candidate table comes from the run's stdout. Raises StudioError when the run fails
        or an exported file is missing."""
        srcs = [Path(p).resolve() for p in paths]
        for p in srcs:
            if not p.is_file():
                raise StudioError(f"model file not found: {p}")
        out = Path(outdir).resolve()
        out.mkdir(parents=True, exist_ok=True)
        files = [out / f"obj_{i}_{p.stem}.stl" for i, p in enumerate(srcs, 1)]
        for f in files:  # a stale export from an earlier run must never pass for this one
            f.unlink(missing_ok=True)
        # debug 1: the stdout table and stderr logging share the log file; few stderr lines keep the table lines whole
        rr = self.run(
            orient_command(srcs, out, settings, convert_unit),
            name=name,
            outputdir=out,
            timeout=timeout,
            expect_output=files[0] if files else None,
            debug=1,
        )
        missing = [f for f in files if not f.is_file()]
        if rr.code != 0 or missing:
            raise StudioError(f"native orient failed: {rr.summary()}" + (f"; missing {[str(f) for f in missing]}" if missing else ""))
        try:
            text = rr.log.read_text(errors="replace")
        except OSError as e:
            raise StudioError(f"native orient: cannot read the log {rr.log}: {e}") from e
        return OrientResult(files, parse_orient(text), rr)

    def normalise(self, paths: list[Path], outdir: Path, *, timeout: int = 300, name: str = "convert-unit") -> list[Path]:
        """`--convert-unit --export-stls`: Studio's own metres/inches guess (Model::looks_like_saved_in_meters, then
        looks_like_imperial_units) and scale, one `<outdir>/obj_<i>_<stem>.stl` per input. A file Studio judges to be
        in mm comes back unscaled. Raises StudioError when the run fails or a file is missing."""
        srcs = [Path(p).resolve() for p in paths]
        for p in srcs:
            if not p.is_file():
                raise StudioError(f"model file not found: {p}")
        out = Path(outdir).resolve()
        out.mkdir(parents=True, exist_ok=True)
        files = [out / f"obj_{i}_{p.stem}.stl" for i, p in enumerate(srcs, 1)]
        for f in files:
            f.unlink(missing_ok=True)
        rr = self.run(orient_command(srcs, out, orient=False), name=name, outputdir=out, timeout=timeout, debug=1)
        missing = [f for f in files if not f.is_file()]
        if rr.code != 0 or missing:
            raise StudioError(f"--convert-unit failed: {rr.summary()}" + (f"; missing {[str(f) for f in missing]}" if missing else ""))
        return files


def _info_blocks(out: str) -> list[dict[str, str]]:
    """`key = value` blocks of --info output. Studio prints one block per object (a `[file name]` line, then the
    keys); a new block starts at a header line or when `size_x` repeats. Log lines (`[2026-...] [trace] ...`) hold no
    `=` before their first `]`, so they are skipped."""
    blocks: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in out.splitlines():
        if line.startswith("[") or "=" not in line:
            continue
        k, v = (x.strip() for x in line.split("=", 1))
        if k == "size_x" and cur:
            blocks.append(cur)
            cur = {}
        cur[k] = v
    if cur:
        blocks.append(cur)
    return blocks


def parse_info(path: Path, out: str) -> MeshInfo:
    """MeshInfo from --info output. A multi-object 3MF prints one block per object: facets, parts and volume are
    summed, the bounding box is the union (each object in its own frame, not its plate position), and the file is
    manifold only if every object is. Raises StudioError when a key is missing or a number is bad."""
    blocks = _info_blocks(out)
    if not blocks:
        raise StudioError(f"--info failed for {path} (missing {list(_INFO_KEYS)}); tail: {out[-500:]}")
    for d in blocks:
        missing = [k for k in _INFO_KEYS if k not in d]
        if missing:
            raise StudioError(f"--info failed for {path} (missing {missing}); tail: {out[-500:]}")
    try:
        lo = tuple(min(float(d[f"min_{a}"]) for d in blocks) for a in "xyz")
        hi = tuple(max(float(d[f"max_{a}"]) for d in blocks) for a in "xyz")
        size = (
            tuple(float(blocks[0][f"size_{a}"]) for a in "xyz")
            if len(blocks) == 1
            else tuple(h - lo_ for lo_, h in zip(lo, hi, strict=True))
        )
        return MeshInfo(
            path,
            size,  # type: ignore[arg-type]
            lo,  # type: ignore[arg-type]
            hi,  # type: ignore[arg-type]
            all(d["manifold"].lower() == "yes" for d in blocks),
            sum(int(d.get("number_of_facets", "0") or 0) for d in blocks),
            sum(int(d.get("number_of_parts", "1") or 1) for d in blocks),
            sum(float(d.get("volume", "0") or 0) for d in blocks),
            blocks[-1] if len(blocks) == 1 else {"blocks": blocks},
            len(blocks),
        )
    except ValueError as e:
        raise StudioError(f"--info output for {path} has a bad number ({e}); tail: {out[-500:]}") from e


def orient_command(
    paths: list[Path], outdir: Path, settings: list[Path] | None = None, convert_unit: bool = True, orient: bool = True
) -> CliCommand:
    """The pass-1 argv: `--load-settings` (machine;process), `--convert-unit`, `--orient=1`, `--export-stls DIR`,
    `--outputdir DIR`, then the STLs. Transforms run in argv order (BS.cpp:4912-4915), so the unit fix comes first.
    `orient=False` gives the unit fix alone (`normalise()`)."""
    out = str(Path(outdir).resolve())
    cmd = CliCommand(inputs=[str(Path(p).resolve()) for p in paths])
    if settings:
        cmd.load_settings(*settings)
    if convert_unit:
        cmd.option("convert-unit")
    if orient:
        cmd.option("orient", 1)
    return cmd.option("export-stls", out).option("outputdir", out)


Vec3 = tuple[float, float, float]
ORIENT_COLUMNS = ("overhang", "bottom", "bottom_hull", "contour", "area_laf", "area_projected", "unprintability")
# Orient.cpp:135-181: `orientation:<x> <y> <z>, cost:<7 numbers>` per candidate, then `best:<x> <y> <z>, costs:<7 numbers>`.
# The vector spacing varies (`1.0000 0.0000 0.0000` vs `-0.0000  1.0000  0.0000`); the numbers are joined by ",\t".
_ORIENT_LINE = re.compile(r"^(orientation|best):\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?),\s*costs?:(.*)$")


@dataclass(frozen=True)
class OrientCandidate:
    """One row of Studio's auto-orient table (Orient.cpp:45-50). `vector` is the mesh direction that ends up pointing
    up after Studio turns the part, so the face laid on the bed has outward normal `-vector`: shambu's `lay_on_face`.
    Lower `unprintability` (Studio's sort key) is better; the other six columns are its inputs."""

    vector: Vec3
    overhang: float
    bottom: float
    bottom_hull: float
    contour: float
    area_laf: float
    area_projected: float
    unprintability: float

    @property
    def lay_on_face(self) -> Vec3:
        return tuple(0.0 if v == 0 else -v for v in self.vector)  # type: ignore[return-value]

    def costs(self) -> dict[str, float]:
        return {k: getattr(self, k) for k in ORIENT_COLUMNS}

    def to_json(self) -> dict:
        return {"vector": list(self.vector), "lay_on_face": list(self.lay_on_face), **self.costs()}


@dataclass(frozen=True)
class OrientTable:
    """The table Studio printed for one object: candidates sorted by unprintability (ties keep Studio's order), and
    the `best:` row (None when the run printed none)."""

    candidates: list[OrientCandidate]
    best: OrientCandidate | None


@dataclass
class OrientResult:
    """`StudioCLI.orient()`: the exported STL per input (argv order) and one table per input in the same order."""

    files: list[Path]
    tables: list[OrientTable]
    run: RunResult

    @property
    def best(self) -> OrientCandidate | None:
        return self.tables[0].best if self.tables else None

    @property
    def candidates(self) -> list[OrientCandidate]:
        return self.tables[0].candidates if self.tables else []

    @property
    def seconds(self) -> float:
        return self.run.seconds


def _orient_row(m: re.Match) -> OrientCandidate | None:
    nums = [x.strip() for x in m.group(5).split(",")]
    try:
        vals = [float(x) for x in nums if x]
        vec = tuple(float(m.group(i)) + 0.0 for i in (2, 3, 4))  # + 0.0 turns -0.0 into 0.0
    except ValueError:
        return None
    if len(vals) != len(ORIENT_COLUMNS):
        return None
    return OrientCandidate(vec, *vals)  # type: ignore[arg-type]


def parse_orient(text: str) -> list[OrientTable]:
    """Studio's auto-orient tables from a CLI log, one per oriented object. A `best:` line closes a table."""
    tables: list[OrientTable] = []
    cur: list[OrientCandidate] = []
    for line in text.splitlines():
        m = _ORIENT_LINE.match(line.strip())
        if not m:
            continue
        row = _orient_row(m)
        if row is None:
            continue
        if m.group(1) == "orientation":
            cur.append(row)
        else:
            tables.append(OrientTable(sorted(cur, key=lambda c: c.unprintability), row))
            cur = []
    if cur:
        tables.append(OrientTable(sorted(cur, key=lambda c: c.unprintability), None))
    return tables


def progress_from_log(log_path: Path) -> dict:
    """Last progress hints from a CLI log: percent lines (debug 4) and the last plate/thumbnail line."""
    pct, last = None, ""
    try:
        for line in open(log_path, errors="replace"):
            m = _PCT.search(line)
            if m:
                pct = int(m.group(1))
            low = line.lower()
            if "finished" in low or "thumbnail" in low or "plate" in low:
                last = line.strip()[-160:]
    except OSError:
        pass
    return {"percent": pct, "last": last}


# Convenience singletons are avoided on purpose; callers construct StudioCLI() once and pass it on.
def info(path: str | os.PathLike) -> MeshInfo:
    return StudioCLI().info(path)
