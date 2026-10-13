"""Shared fakes and fixtures. Everything here is offline: no Bambu Studio binary, no network, no paho."""

from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from shambu import presets as P
from shambu.studio import CliCommand, MeshInfo, RunResult

MACHINE = "Test X2D 0.4 nozzle"
PROCESS = "0.20mm Test"
PROCESS_NAME = "Test Process"  # the name the fixture 3MF carries in print_settings_id
LONG_START = "G28 ; home\n" * 200  # > 1000 chars, like a resolved include template
LONG_END = "M104 S0 ; off\n" * 100


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
            b'<?xml version="1.0"?>\n<config>\n  <header/>\n' + (b'  <plate id="1"/>\n' if sliced else b"") + b"</config>\n",
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
    # filament
    _w(root, "filament", "fdm_filament_common", {"from": "system", "instantiation": "false", "filament_type": ["PLA"]})
    _w(
        root,
        "filament",
        "Test PLA",
        {"from": "system", "instantiation": "true", "inherits": "fdm_filament_common", "compatible_printers": [MACHINE]},
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
        return RunResult(
            ["BambuStudio", *args],
            self.code % 256,
            self.code,
            0.1,
            log,
            {"return_code": self.code, **self.result}
            if self.result is not None
            else ({"return_code": self.code} if self.code == 0 else None),
            expect_output,
        )

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
