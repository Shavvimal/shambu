"""Shared fakes and fixtures. Everything here is offline: no Bambu Studio binary, no network, no paho."""

from __future__ import annotations

import copy
import json
import os
import shutil
import types
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from shambu import conf, vault
from shambu import presets as P
from shambu.errors import PrinterError
from shambu.facade import Bambu
from shambu.studio import BINARY, CliCommand, MeshInfo, OrientCandidate, OrientResult, OrientTable, RunResult

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path_factory):
    """No test touches the LAN or this Mac's Bambu Studio config: the facade's printer read fails as an unreachable
    printer does (the WARN fallback), BambuStudio.conf is absent, printers.json and credentials.json are missing files
    in a config dir that does not exist yet (so its 0700 creation runs), `vault.default_vault()` returns one
    `MemoryVault` per test and the runtime secret and identifier sets start and end empty. A test that wants a report or
    a config monkeypatches `Bambu.loaded_filaments`, `conf.CONF` or `conf.PRINTERS` again on top of this; a test that
    requests `offline` by name seeds the vault through `offline.vault`."""

    def unreachable(self, *a, **k):
        raise PrinterError("no printer in tests")

    monkeypatch.setattr(Bambu, "loaded_filaments", unreachable)
    monkeypatch.setattr(conf, "CONF", tmp_path_factory.mktemp("conf") / "no-such-BambuStudio.conf")
    monkeypatch.setattr(conf, "CALIBRATION", tmp_path_factory.mktemp("calibration") / "no-such-calibration.json")
    config = tmp_path_factory.mktemp("config") / "shambu"  # not created: the first write makes it 0700
    monkeypatch.setattr(conf, "PRINTERS", config / "printers.json")
    monkeypatch.setattr(conf, "CREDENTIALS", config / "credentials.json")
    fake = vault.MemoryVault()
    monkeypatch.setattr(vault, "default_vault", lambda: fake)
    conf.forget_secrets()
    yield types.SimpleNamespace(vault=fake)
    conf.forget_secrets()


MACHINE = "Test X2D 0.4 nozzle"
PROCESS = "0.20mm Test"
PROCESS_NAME = "Test Process"  # the name the fixture 3MF carries in print_settings_id
LONG_START = "G28 ; home\n" * 200  # > 1000 chars, like a resolved include template
LONG_END = "M104 S0 ; off\n" * 100
# Tests that run the real Bambu Studio binary (about 0.1 s per orient run): they run only with SHAMBU_INTEGRATION=1 and
# the binary installed (CONTRIBUTING: the default suite never calls Studio). They also carry the `integration` marker.
requires_studio = pytest.mark.skipif(
    os.environ.get("SHAMBU_INTEGRATION") != "1" or not BINARY.is_file(),
    reason=f"set SHAMBU_INTEGRATION=1 with Bambu Studio installed at {BINARY}",
)


# ---------------------------------------------------------------------------- 3MF fixture
MODEL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">
 <metadata name="Application">BambuStudio-02.08.02.61</metadata>
 <resources>
  <object id="2" type="model"><components><component p:path="/3D/Objects/object_1.model" objectid="1"/></components></object>
  <object id="4" type="model"><components><component p:path="/3D/Objects/object_2.model" objectid="3"/></components></object>
 </resources>
 <build>
  <item objectid="4" printable="1"/>
  <item objectid="2" printable="1"/>
 </build>
</model>
"""

MODEL_SETTINGS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <object id="2">
    <metadata key="name" value="{name1}"/>
    <metadata key="extruder" value="1"/>
    <part id="1" subtype="normal_part">
      <metadata key="name" value="body_1"/>
      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>
      <metadata key="source_file" value="body.stl"/>
    </part>
  </object>
  <object id="4">
    <metadata key="name" value="{name2}"/>
    <metadata key="extruder" value="1"/>
    <part id="3" subtype="normal_part">
      <metadata key="name" value="label_base"/>
      <metadata key="source_file" value="base.stl"/>
      <metadata key="extruder" value="1"/>
    </part>
    <part id="5" subtype="normal_part">
      <metadata key="name" value="label_text"/>
      <metadata key="source_file" value="text.stl"/>
      <metadata key="extruder" value="2"/>
    </part>
  </object>
  <plate>
    <metadata key="plater_id" value="1"/>
    <metadata key="plater_name" value=""/>
    <metadata key="locked" value="false"/>
    {gcode_meta}
    <model_instance>
      <metadata key="object_id" value="2"/>
      <metadata key="instance_id" value="0"/>
    </model_instance>
    <model_instance>
      <metadata key="object_id" value="4"/>
      <metadata key="instance_id" value="0"/>
    </model_instance>
  </plate>
</config>
"""


def project_settings(**over) -> dict:
    d = {
        "curr_bed_type": "Cool Plate",
        "filament_settings_id": ["Test PLA", "Test PLA"],
        "filament_colour": ["#00AE42"],
        "filament_map": ["1"],
        "filament_map_mode": "Auto For Flush",
        "nozzle_diameter": ["0.4", "0.4"],
        "extruder_type": ["Direct Drive", "Bowden"],
        "nozzle_volume_type": ["Standard", "Standard"],
        "filament_extruder_variant": ["Direct Drive Standard", "Direct Drive Standard"],
        "machine_pause_gcode": "M400 U1",
        "printer_settings_id": MACHINE,
        "print_settings_id": PROCESS_NAME,
        "filament_type": ["PLA", "PLA"],
        "machine_start_gcode": LONG_START,
        "machine_end_gcode": LONG_END,
        "wall_loops": "2",
        "layer_height": "0.2",
    }
    d.update(over)
    return d


def make_3mf(
    path: Path, *, sliced: bool = False, with_model_settings: bool = True, object_names: tuple[str, str] = ("body", "label"), **ps_over
) -> Path:
    """A minimal Bambu project. Entry order and compression mimic a CLI export."""
    gcode_meta = (
        '<metadata key="gcode_file" value="Metadata/plate_1.gcode"/>\n    <metadata key="prediction" value="1234"/>' if sliced else ""
    )
    entries: list[tuple[str, bytes, int]] = [
        ("[Content_Types].xml", b'<?xml version="1.0"?><Types/>', zipfile.ZIP_DEFLATED),
        ("Metadata/plate_1.png", b"\x89PNG fake", zipfile.ZIP_STORED),
        ("3D/3dmodel.model", MODEL_XML.encode(), zipfile.ZIP_DEFLATED),
        ("3D/Objects/object_1.model", b"<model/>", zipfile.ZIP_DEFLATED),
        ("Metadata/project_settings.config", (json.dumps(project_settings(**ps_over), indent=4) + "\n").encode(), zipfile.ZIP_DEFLATED),
    ]
    if with_model_settings:
        entries.append(
            (
                "Metadata/model_settings.config",
                MODEL_SETTINGS_XML.format(gcode_meta=gcode_meta, name1=object_names[0], name2=object_names[1]).encode(),
                zipfile.ZIP_DEFLATED,
            )
        )
    entries += [
        (
            "Metadata/slice_info.config",
            b'<?xml version="1.0"?>\n<config>\n  <header/>\n'
            + (
                b'  <plate>\n    <metadata key="index" value="1"/>\n    <metadata key="prediction" value="1234"/>\n'
                b'    <metadata key="weight" value="10.0"/>\n  </plate>\n'
                if sliced
                else b""
            )
            + b"</config>\n",
            zipfile.ZIP_DEFLATED,
        ),
        ("Metadata/filament_sequence.json", b'{"plate_1":{"sequence":[]}}', zipfile.ZIP_DEFLATED),
    ]
    if sliced:
        entries += [
            ("Metadata/plate_1.gcode", b"G28\n", zipfile.ZIP_DEFLATED),
            ("Metadata/plate_1.gcode.md5", b"abc", zipfile.ZIP_DEFLATED),
        ]
    entries.append(("_rels/.rels", b"<Relationships/>", zipfile.ZIP_DEFLATED))
    with zipfile.ZipFile(path, "w") as z:
        for name, data, comp in entries:
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 12, 0, 0, 0))
            info.compress_type = comp
            z.writestr(info, data)
    return path


@pytest.fixture
def project_file(tmp_path) -> Path:
    return make_3mf(tmp_path / "fixture.3mf")


@pytest.fixture
def sliced_file(tmp_path) -> Path:
    return make_3mf(tmp_path / "sliced.3mf", sliced=True)


# ---------------------------------------------------------------------------- preset tree
def _w(root: Path, kind: str, name: str, d: dict) -> None:
    p = root / kind / f"{name}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"name": name, "type": kind, **d}), encoding="utf-8")


def write_profiles(root: Path) -> Path:
    # machine: common base -> instantiable printer, which includes a G-code template
    _w(
        root,
        "machine",
        "fdm_machine_common",
        {
            "from": "system",
            "instantiation": "false",
            "printable_height": "325",
            "printable_area": ["0x0", "256x0", "256x256", "0x256"],
            "nozzle_diameter": ["0.4", "0.4"],
            "extruder_type": ["Direct Drive", "Bowden"],
            "change_filament_gcode": "M600",
            "machine_start_gcode": "short generic start",
        },
    )
    _w(
        root,
        "machine",
        "x2d_gcode_template",
        {"from": "system", "instantiation": "false", "machine_start_gcode": LONG_START, "machine_end_gcode": LONG_END},
    )
    _w(
        root,
        "machine",
        MACHINE,
        {
            "from": "system",
            "instantiation": "true",
            "inherits": "fdm_machine_common",
            "include": ["x2d_gcode_template"],
            "printer_model": "Test X2D",
            "setting_id": "GM1",
            "extruder_printable_area": ["0x0,256x0,256x256,0x256", "20.5x0,256x0,256x256,20.5x256"],
            "extruder_printable_height": ["320", "325"],
        },
    )
    # process
    _w(
        root,
        "process",
        "fdm_process_common",
        {
            "from": "system",
            "instantiation": "false",
            "wall_loops": "2",
            "sparse_infill_density": "15%",
            "brim_type": "auto_brim",
            "brim_width": "5",
        },
    )
    _w(
        root,
        "process",
        PROCESS,
        {
            "from": "system",
            "instantiation": "true",
            "inherits": "fdm_process_common",
            "layer_height": "0.2",
            "compatible_printers": [MACHINE],
        },
    )
    _w(
        root,
        "process",
        "0.20mm Wrong",
        {"from": "system", "instantiation": "true", "inherits": "fdm_process_common", "compatible_printers": ["Some Other Printer"]},
    )
    # filament. `filament_id` is what a tray's `tray_info_idx` names (GFA00 Bambu PLA Basic, GFG00 Bambu PETG Basic,
    # GFG99 Generic PETG); the X2D-specific presets inherit it from the @base one, as the real tree does.
    _w(root, "filament", "fdm_filament_common", {"from": "system", "instantiation": "false", "filament_type": ["PLA"]})
    _w(
        root,
        "filament",
        "Test PLA",
        {
            "from": "system",
            "instantiation": "true",
            "inherits": "fdm_filament_common",
            "compatible_printers": [MACHINE],
            "filament_id": "GFA00",
        },
    )
    _w(
        root,
        "filament",
        "Generic PETG @base",
        {"from": "system", "instantiation": "false", "inherits": "fdm_filament_common", "filament_type": ["PETG"], "filament_id": "GFG99"},
    )
    _w(
        root,
        "filament",
        "Generic PETG @Test X2D",
        {"from": "system", "instantiation": "true", "inherits": "Generic PETG @base", "compatible_printers": [MACHINE]},
    )
    _w(
        root,
        "filament",
        "Test PETG Basic",
        {
            "from": "system",
            "instantiation": "true",
            "inherits": "fdm_filament_common",
            "filament_type": ["PETG"],
            "filament_id": "GFG00",
            "compatible_printers": [MACHINE],
        },
    )
    _w(
        root,
        "filament",
        "Test PETG",
        {
            "from": "system",
            "instantiation": "true",
            "inherits": "fdm_filament_common",
            "filament_type": ["PETG"],
            "compatible_printers": ["Some Other Printer"],
        },
    )
    _w(
        root,
        "filament",
        "Test TPU",
        {
            "from": "system",
            "instantiation": "true",
            "inherits": "fdm_filament_common",
            "filament_type": ["TPU"],
            "compatible_printers": [MACHINE],
        },
    )
    return root


@pytest.fixture
def profiles(tmp_path) -> Path:
    return write_profiles(tmp_path / "profiles")


@pytest.fixture
def resolver(profiles) -> P.PresetResolver:
    return P.PresetResolver(P.ChainedSource([P.DirectorySource(profiles, "test profiles")]))


# ---------------------------------------------------------------------------- fakes
def mesh(path: Path | str = "part.stl", size=(20.0, 20.0, 10.0), lo=(0.0, 0.0, 0.0), manifold: bool = True) -> MeshInfo:
    return MeshInfo(
        Path(path),
        tuple(size),
        tuple(lo),
        tuple(a + b for a, b in zip(lo, size, strict=True)),
        manifold,
        12,
        1,
        size[0] * size[1] * size[2],
    )


# result.json `sliced_plates[]` as Studio 02.08.02.61 wrote it for a two-object by-object plate (PLAN-native-cli §2.5,
# LIVE), with filament 2 given 0.5 g of flush and tower so total_used_g and main_used_g differ.
SLICED_PLATES: list[dict] = [
    {
        "feature_type_times": {
            "Bottom surface": 33.55055236816406,
            "Bridge": 34.165504455566406,
            "Custom": 287.0181884765625,
            "Flush": 63.0,
            "Inner wall": 95.69586181640625,
            "Internal solid infill": 109.47734069824219,
            "Outer wall": 147.86422729492188,
            "Sparse infill": 225.83209228515625,
            "Top surface": 18.776708602905273,
            "Travel": 57.97702407836914,
            "Undefined": 50.51105499267578,
        },
        "filament_change_times": 0,
        "filaments": [
            {"filament_id": "GFA00", "id": 1, "main_used_g": 2.119844436645508, "total_used_g": 2.119844436645508},
            {"filament_id": "GFA00", "id": 2, "main_used_g": 2.119844436645508, "total_used_g": 2.619844436645508},
        ],
        "generate_support_material_time": 1,
        "id": 1,
        "infill_time": 3,
        "layer_filament_change": 0,
        "main_predication": 694.3615112304688,
        "make_perimeters_time": 4,
        "obj_cached_cnt": 0,
        "objects": [
            {
                "bbox": {"depth": 20.0, "height": 10.0, "width": 20.0, "x": 40.0, "y": 60.0, "z": 0.0},
                "id": 15,
                "name": "a_1",
                "triangle_count": 12,
            },
            {
                "bbox": {"depth": 20.0, "height": 10.0, "width": 20.0, "x": 160.0, "y": 60.0, "z": 0.0},
                "id": 35,
                "name": "b_1",
                "triangle_count": 12,
            },
        ],
        "sliced_time": 48,
        "sliced_time_with_cache": 36,
        "total_predication": 1065.9136962890625,
        "triangle_count": 24,
        "warning_message": "",
    }
]


TOWER_PROCESS_KEYS = ("prime_tower_width", "prime_tower_rib_wall", "prime_tower_brim_width", "enable_prime_tower")


def write_cli_tower(project: Path, args: list[str]) -> None:
    """What the real CLI writes into the exported project for the tower: `--wipe-tower-x/y` vectors and the tower
    keys of the loaded process JSON. Without it a fixture could not pass the tower verification."""
    from shambu.threemf import PROJECT_SETTINGS, Project

    found: dict = {}
    for a in args:
        for axis in ("x", "y"):
            if a.startswith(f"--wipe-tower-{axis}="):
                found[f"wipe_tower_{axis}"] = a.split("=", 1)[1].split(",")
    for i, a in enumerate(args):
        if a == "--load-settings" and i + 1 < len(args):
            value = args[i + 1]
        elif a.startswith("--load-settings="):
            value = a.split("=", 1)[1]
        else:
            continue
        for path in value.split(";"):
            if path.endswith("process.json") and Path(path).is_file():
                proc = json.loads(Path(path).read_text(encoding="utf-8"))
                found.update({k: proc[k] for k in TOWER_PROCESS_KEYS if k in proc})
    if not found:
        return
    p = Project(str(project))
    p.project_settings.update(found)
    p._dirty.add(PROJECT_SETTINGS)  # no slice invalidation: the fake writes what the export itself holds
    p.save(str(project))


@dataclass
class FakeStudio:
    """Same surface as StudioCLI. `run()` copies `fixture` to the expected output, or fails with `code`; `result` adds
    keys to the run's result.json (as Studio writes `error_string` and `warning_message`)."""

    fixture: Path | None = None
    code: int = 0
    sizes: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    los: dict[str, tuple[float, float, float]] = field(default_factory=dict)  # STL bbox minimum per file name (default origin)
    scratch: Path = Path("/nonexistent")
    binary: Path = Path("/Applications/FakeStudio.app/Contents/MacOS/BambuStudio")
    calls: list[tuple[str, object]] = field(default_factory=list)
    result: dict | None = None  # extra result.json keys (error_string, warning_message); None keeps the default result
    # orient(): the table it returns (Studio's bracket best by default); it copies each input to obj_<i>_<stem>.stl
    orient_table: list[OrientCandidate] = field(
        default_factory=lambda: [OrientCandidate((-1.0, 0.0, 0.0), 3790.4, 4314.0, 9010.0, 262.7, 485.1, 0.0, 0.6)]
    )

    orient_best: OrientCandidate | None = None  # the `best:` row; None: the first row after sorting

    def orient(self, paths, outdir, *, settings=None, convert_unit=True, timeout=300, name="orient") -> OrientResult:
        """Copies each input to obj_<i>_<stem>.stl, scaled by 1000 when `convert_unit` and the mesh is under 1 mm."""
        from shambu import mesh as M

        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        self.calls.append(("orient", ([Path(p) for p in paths], settings, convert_unit)))
        files = []
        for i, p in enumerate(paths, 1):
            f = outdir / f"obj_{i}_{Path(p).stem}.stl"
            facets = M.read_stl(p) if convert_unit else []
            if facets and M.looks_like_metres(facets):
                M.write_stl(f, M.scale(facets, 1000))
            else:
                shutil.copyfile(p, f)
            files.append(f)
        log = outdir / f"{name}.log"
        log.write_text("$ fake orient\n", encoding="utf-8")
        best = self.orient_best
        cands = sorted(self.orient_table, key=lambda c: c.unprintability)  # Studio's order on a tie: as given
        table = OrientTable(cands, best or (cands[0] if cands else None))  # not ranked toward best: callers must do it
        return OrientResult(files, [table] * len(files), RunResult(["BambuStudio", "--orient=1"], 0, 0, 0.05, log, {"return_code": 0}))

    def normalise(self, paths, outdir, *, timeout=300, name="convert-unit") -> list[Path]:
        """Copies every input as obj_<i>_<stem>.stl, scaled by 1000 when the whole mesh is under 1 mm (Studio's metres guess)."""
        from shambu import mesh as M

        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        self.calls.append(("normalise", [Path(p) for p in paths]))
        out = []
        for i, p in enumerate(paths, 1):
            facets = M.read_stl(p)
            out.append(
                M.write_stl(outdir / f"obj_{i}_{Path(p).stem}.stl", M.scale(facets, 1000) if M.looks_like_metres(facets) else facets)
            )
        return out

    sliced_plates: list[dict] | None = None  # result.json sliced_plates[] (e.g. SLICED_PLATES); None leaves it out

    def info(self, path) -> MeshInfo:
        path = Path(path)
        self.calls.append(("info", path))
        return mesh(path, self.sizes.get(path.name, (20.0, 20.0, 10.0)), self.los.get(path.name, (0.0, 0.0, 0.0)))

    def run(
        self, cmd: CliCommand, *, name: str = "run", outputdir: Path | None = None, timeout: int = 1200, expect_output: Path | None = None
    ) -> RunResult:
        args = cmd.args()
        self.calls.append(("run", args))
        log = Path(outputdir or self.scratch) / f"{name}.log"
        if outputdir:
            log.write_text("$ fake\n", encoding="utf-8")
        if self.code == 0 and expect_output is not None and self.fixture is not None:
            shutil.copyfile(self.fixture, expect_output)
            write_cli_tower(expect_output, args)
        res = (
            {"return_code": self.code, **self.result}
            if self.result is not None
            else ({"return_code": self.code} if self.code == 0 else None)
        )
        if self.sliced_plates is not None:
            res = {**(res or {"return_code": self.code}), "sliced_plates": copy.deepcopy(self.sliced_plates)}
        return RunResult(["BambuStudio", *args], self.code % 256, self.code, 0.1, log, res, expect_output)

    def version(self) -> str:
        return "02.08.02.61"

    def running_pids(self) -> list[int]:
        return []

    def datadir(self, name: str) -> Path:
        return self.scratch / f"datadir-{name}"


class Recorder:
    """A Listener that keeps every event."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event: str, data: dict) -> None:
        self.events.append((event, data))

    def names(self) -> list[str]:
        return [e for e, _ in self.events]


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def stls(tmp_path) -> dict[str, Path]:
    d = tmp_path / "stl"
    d.mkdir()
    out = {}
    for n in ("body", "base", "text"):
        p = d / f"{n}.stl"
        p.write_bytes(b"solid x\nendsolid x\n")
        out[n] = p
    return out


# ---------------------------------------------------------------------------- geometry 3MF fixture (project edits)
# A Bambu project with real meshes: two objects, transforms, source offsets, face counts, plates, and the per-filament
# vectors in Studio's block form (six extruder variants per filament, as the X2D presets carry). `layout="gui"` puts the
# placement on the build item (component identity, as a GUI save does); `layout="component"` puts it on the component
# (item identity, as a shambu build exports it).
VARIANTS = (
    "Direct Drive Standard",
    "Direct Drive High Flow",
    "Direct Drive E3D High Flow",
    "Bowden Standard",
    "Bowden High Flow",
    "Bowden E3D High Flow",
)
GEO_BODY = (30.0, 20.0, 10.0)  # body: size; STL frame x 0..30, y 0..20, z 0..10 (source_offset 15, 10, 5)
GEO_BODY_AT = (40.0, 50.0)  # plate-local min corner
GEO_LABEL = (10.0, 10.0, 2.0)  # label: STL frame x -5..5, y -5..5, z 0..2 (source_offset 0, 0, 1)
GEO_LABEL_AT = (150.0, 50.0)


def box_facets(lo, hi) -> list:
    """12 outward-wound triangles of an axis-aligned box."""
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    tris = [
        (0, 2, 1),
        (0, 3, 2),
        (4, 5, 6),
        (4, 6, 7),
        (0, 1, 5),
        (0, 5, 4),
        (1, 2, 6),
        (1, 6, 5),
        (2, 3, 7),
        (2, 7, 6),
        (3, 0, 4),
        (3, 4, 7),
    ]
    return [(v[a], v[b], v[c]) for a, b, c in tris]


def fine_box_facets(lo, hi) -> list:
    """The same box with each side split into 4 triangles around its centre: 24 triangles, 14 vertices."""
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    quads = [
        [(x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (x1, y0, z0)],
        [(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)],
        [(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)],
        [(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)],
        [(x1, y1, z0), (x0, y1, z0), (x0, y1, z1), (x1, y1, z1)],
        [(x0, y1, z0), (x0, y0, z0), (x0, y0, z1), (x0, y1, z1)],
    ]
    out = []
    for q in quads:
        c = tuple(sum(p[i] for p in q) / 4 for i in range(3))
        for i in range(4):
            out.append((q[i], q[(i + 1) % 4], c))
    return out


def write_box_stl(path: Path, lo, hi, fine: bool = False) -> Path:
    from shambu import mesh as M

    return M.write_stl(path, fine_box_facets(lo, hi) if fine else box_facets(lo, hi))


def _mesh_model(sub_id: str, size, paint: bool = False) -> str:
    hx, hy, hz = (s / 2 for s in size)
    index: dict = {}
    verts: list = []
    tris = []
    for f in box_facets((-hx, -hy, -hz), (hx, hy, hz)):
        t = []
        for v in f:
            if v not in index:
                index[v] = len(verts)
                verts.append(v)
            t.append(index[v])
        tris.append(t)
    vx = "".join(f'     <vertex x="{x:g}" y="{y:g}" z="{z:g}"/>\n' for x, y, z in verts)
    tx = "".join(
        f'     <triangle v1="{a}" v2="{b}" v3="{c}"' + (' paint_supports="4"' if paint and i == 0 else "") + "/>\n"
        for i, (a, b, c) in enumerate(tris)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
        'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">\n'
        ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n <resources>\n'
        f'  <object id="{sub_id}" p:UUID="0000000{sub_id}-81cb-4c03-9d28-80fed5dfa1dc" type="model">\n'
        f"   <mesh>\n    <vertices>\n{vx}    </vertices>\n    <triangles>\n{tx}    </triangles>\n   </mesh>\n"
        "  </object>\n </resources>\n <build/>\n</model>\n"
    )


def geo_project_settings(filaments: int = 2, **over) -> dict:
    n = filaments
    d = project_settings(
        filament_settings_id=["Test PLA"] * n,
        filament_colour=["#00AE42", "#FFFFFF", "#0A2989"][:n],
        filament_multi_colour=["#00AE42", "#FFFFFF", "#0A2989"][:n],
        filament_map=[str(min(i, 2)) for i in range(1, n + 1)],
        filament_map_mode="Manual",
        filament_type=["PLA"] * n,
        filament_ids=["GFA00"] * n,
        filament_density=["1.24"] * n,
        filament_nozzle_map=[str(min(i, 2)) for i in range(1, n + 1)],
        filament_volume_map=["0"] * n,
        filament_colour_type=["1"] * n,
        pressure_advance=["0.02"] * n,
        filament_extruder_variant=list(VARIANTS) * n,
        filament_self_index=[str(i) for i in range(1, n + 1) for _ in VARIANTS],
        nozzle_temperature=[str(220 + 5 * i) for _ in range(n) for i in range(6)],
        filament_flush_temp_fast=["0"],
        bridge_speed=["50", "50", "50", "50", "200", "200"],  # a per-process-variant key: never extended by a filament
        different_settings_to_system=[""] * (n + 2),
        inherits_group=["0.20mm Test"] + [""] * (n + 1),
        flush_volumes_matrix={1: ["0", "0"], 2: ["0", "300", "200", "0"] * 2}.get(n, ["0"] * (2 * n * n)),
        flush_volumes_vector=["140"] * 8,
        printable_area=["0x0", "256x0", "256x256", "0x256"],
        printable_height="325",
        extruder_printable_area=["0x0,256x0,256x256,0x256", "20.5x0,256x0,256x256,20.5x256"],
        extruder_printable_height=["320", "325"],
        enable_prime_tower="1",
        prime_tower_width="35",
        wipe_tower_x=["208"],
        wipe_tower_y=["208"],
    )
    d.update(over)
    return d


def geo_plate_origin(plate: int, plates: int) -> tuple[float, float]:
    """PartPlate.cpp: (col * 256 * 1.2, -row * 256 * 1.2), cols = compute_colum_count(plates)."""
    import math

    v = math.sqrt(plates)
    cols = round(v) + 1 if v > round(v) else round(v)
    row, col = divmod(plate - 1, cols)
    return (col * 256 * 1.2, -row * 256 * 1.2)


def make_geo_3mf(
    path: Path,
    *,
    layout: str = "gui",
    plates: int = 1,
    filaments: int = 2,
    paint: bool = False,
    sliced: bool = False,
    ranges: list[tuple[float, float]] | None = None,
    **ps_over,
) -> Path:
    """Body (object id 2, sub-object 1, 30 x 20 x 10, filament 1, plate 1) and label (object id 4, sub-object 3,
    10 x 10 x 2, filament 2 when there are two, on the last plate). `ranges` are height ranges on the body. Entry
    order and compression mimic a CLI export."""
    bw, bh, bz = GEO_BODY
    lw, lh, lz = GEO_LABEL
    ox, oy = geo_plate_origin(plates, plates)
    body_c = (GEO_BODY_AT[0] + bw / 2, GEO_BODY_AT[1] + bh / 2, bz / 2)
    label_c = (GEO_LABEL_AT[0] + lw / 2 + ox, GEO_LABEL_AT[1] + lh / 2 + oy, lz / 2)

    def tf(c=None) -> str:
        c = c or (0, 0, 0)
        return f"1 0 0 0 1 0 0 0 1 {c[0]:g} {c[1]:g} {c[2]:g}"

    gui = layout == "gui"
    model = f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">
 <metadata name="Application">BambuStudio-02.08.02.61</metadata>
 <metadata name="BambuStudio:3mfVersion">1</metadata>
 <resources>
  <object id="2" p:UUID="00000001-61cb-4c03-9d28-80fed5dfa1dc" type="model">
   <components>
    <component p:path="/3D/Objects/object_1.model" objectid="1" p:UUID="00010000-b206-40ff-9872-83e8017abed1" transform="{tf(None if gui else body_c)}"/>
   </components>
  </object>
  <object id="4" p:UUID="00000002-61cb-4c03-9d28-80fed5dfa1dc" type="model">
   <components>
    <component p:path="/3D/Objects/object_2.model" objectid="3" p:UUID="00020000-b206-40ff-9872-83e8017abed1" transform="{tf(None if gui else label_c)}"/>
   </components>
  </object>
 </resources>
 <build p:UUID="2c7c17d8-22b5-4d84-8835-1976022ea369">
  <item objectid="2" p:UUID="00000002-b1ec-4553-aec9-835e5b724bb4" transform="{tf(body_c if gui else None)}" printable="1"/>
  <item objectid="4" p:UUID="00000004-b1ec-4553-aec9-835e5b724bb4" transform="{tf(label_c if gui else None)}" printable="1"/>
 </build>
</model>
"""

    def part(pid, name, stl, off, faces):
        return f"""    <part id="{pid}" subtype="normal_part">
      <metadata key="name" value="{name}"/>
      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>
      <metadata key="source_file" value="{stl}"/>
      <metadata key="source_object_id" value="0"/>
      <metadata key="source_volume_id" value="0"/>
      <metadata key="source_offset_x" value="{off[0]:g}"/>
      <metadata key="source_offset_y" value="{off[1]:g}"/>
      <metadata key="source_offset_z" value="{off[2]:g}"/>
      <mesh_stat face_count="{faces}" edges_fixed="0" degenerate_facets="0" facets_removed="0" facets_reversed="0" backwards_edges="0"/>
    </part>"""

    label_ext = 2 if filaments > 1 else 1
    fmaps = " ".join(str(min(i, 2)) for i in range(1, filaments + 1))
    plate_xml = []
    for k in range(1, plates + 1):
        inst = (["2"] if k == 1 else []) + (["4"] if k == plates else [])
        gm = '\n    <metadata key="gcode_file" value="Metadata/plate_1.gcode"/>' if sliced and k == 1 else ""
        mis = "".join(
            f"""
    <model_instance>
      <metadata key="object_id" value="{o}"/>
      <metadata key="instance_id" value="0"/>
    </model_instance>"""
            for o in inst
        )
        plate_xml.append(
            f"""  <plate>
    <metadata key="plater_id" value="{k}"/>
    <metadata key="plater_name" value=""/>
    <metadata key="locked" value="false"/>
    <metadata key="filament_map_mode" value="Manual"/>
    <metadata key="filament_maps" value="{fmaps}"/>{gm}{mis}
  </plate>"""
        )
    nl = "\n"
    ms = f"""<?xml version="1.0" encoding="UTF-8"?>
<config>
  <object id="2">
    <metadata key="name" value="body"/>
    <metadata key="extruder" value="1"/>
    <metadata key="brim_type" value="no_brim"/>
    <metadata face_count="12"/>
{part(1, "body_1", "body.stl", (bw / 2, bh / 2, bz / 2), 12)}
  </object>
  <object id="4">
    <metadata key="name" value="label"/>
    <metadata key="extruder" value="{label_ext}"/>
    <metadata key="brim_type" value="no_brim"/>
    <metadata face_count="12"/>
{part(3, "label_1", "label.stl", (0, 0, lz / 2), 12)}
  </object>
{nl.join(plate_xml)}
</config>
"""
    entries: list[tuple[str, bytes, int]] = [
        ("[Content_Types].xml", b'<?xml version="1.0"?><Types/>', zipfile.ZIP_DEFLATED),
        ("_rels/.rels", b"<Relationships/>", zipfile.ZIP_DEFLATED),
        ("3D/3dmodel.model", model.encode(), zipfile.ZIP_DEFLATED),
        (
            "3D/_rels/3dmodel.model.rels",
            b'<?xml version="1.0" encoding="UTF-8"?>\n<Relationships><Relationship Target="/3D/Objects/object_1.model" Id="rel-1"/>'
            b'<Relationship Target="/3D/Objects/object_2.model" Id="rel-2"/></Relationships>\n',
            zipfile.ZIP_DEFLATED,
        ),
        ("3D/Objects/object_1.model", _mesh_model("1", GEO_BODY, paint).encode(), zipfile.ZIP_DEFLATED),
        ("3D/Objects/object_2.model", _mesh_model("3", GEO_LABEL).encode(), zipfile.ZIP_DEFLATED),
        ("Metadata/plate_1.png", b"\x89PNG fake", zipfile.ZIP_STORED),
        ("Metadata/plate_1.json", b'{"bbox_objects":[]}', zipfile.ZIP_DEFLATED),
        (
            "Metadata/project_settings.config",
            (json.dumps(geo_project_settings(filaments, **ps_over), indent=4) + "\n").encode(),
            zipfile.ZIP_DEFLATED,
        ),
    ]
    if ranges:
        rx = "".join(
            f'  <range min_z="{lo!r}" max_z="{hi!r}">\n   <option opt_key="layer_height">0.2</option>\n  </range>\n' for lo, hi in ranges
        )
        entries.append(
            (
                "Metadata/layer_config_ranges.xml",
                f'<?xml version="1.0" encoding="UTF-8"?>\n<objects>\n <object id="1">\n{rx} </object>\n</objects>\n'.encode(),
                zipfile.ZIP_DEFLATED,
            )
        )
    entries += [
        ("Metadata/model_settings.config", ms.encode(), zipfile.ZIP_DEFLATED),
        (
            "Metadata/slice_info.config",
            b'<?xml version="1.0"?>\n<config>\n  <header/>\n'
            + (
                b'  <plate>\n    <metadata key="index" value="1"/>\n    <metadata key="prediction" value="1234"/>\n'
                b'    <metadata key="weight" value="10.0"/>\n  </plate>\n'
                if sliced
                else b""
            )
            + b"</config>\n",
            zipfile.ZIP_DEFLATED,
        ),
        ("Metadata/filament_sequence.json", b'{"plate_1":{"sequence":[]}}', zipfile.ZIP_DEFLATED),
    ]
    if sliced:
        entries += [
            ("Metadata/plate_1.gcode", b"G28\n", zipfile.ZIP_DEFLATED),
            ("Metadata/plate_1.gcode.md5", b"abc", zipfile.ZIP_DEFLATED),
        ]
    with zipfile.ZipFile(path, "w") as z:
        for name, data, comp in entries:
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 12, 0, 0, 0))
            info.compress_type = comp
            z.writestr(info, data)
    return path


@pytest.fixture
def geo_file(tmp_path) -> Path:
    return make_geo_3mf(tmp_path / "geo.3mf")
