"""Provenance for CAD exports: the record `shambu cad stamp` writes beside an STL (`<part>.cad.json`, schema
`shambu.cad.record/1`), the build id, and the stale-export refusal `shambu cad check` applies.

The build id is SHA-256 over the declared inputs (the saved CAD script and its parameter file) in sorted path order,
each hashed as the label's length and bytes, then a presence tag: `F` with the content's length and bytes, or `M` for
a missing file. The tag and the lengths keep a missing file apart from an empty one and from one whose bytes read
`MISSING`, and no two (label, content) lists run together into one byte stream. The id is `rev@` and the first 12 hex digits. Paths in a record are relative to the record's own folder, so a
project moved as a whole keeps its ids; each input also keeps the label it was hashed under, so a promoted record in
another folder recomputes the same id.

`check` refuses a stale export on any of three mismatches: the build id recomputed from the inputs, the SHA-256 of
the STL being judged, and the SHA-256 of the STEP the record names. Comparing the STL's own hash closes the gap where
a replaced STL with an old record passed because only the build id matched.

What it handles: hashing, the record's JSON, freshness findings and `promote` (copy a checked candidate to a NEW file
and write a NEW record beside it; it never overwrites, so the last good export is never replaced by an unchecked
one). What it does not do: no mesh analysis (solid.py), no printing.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..errors import CadError
from ..findings import FAIL, MEASURED, WARN, Finding, Fix
from .jsonin import loads

SCHEMA = "shambu.cad.record/1"


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _framed(data: bytes) -> bytes:
    return len(data).to_bytes(8, "big") + data


def build_id(inputs: Iterable[tuple[str, Path]], rev: str | None) -> str:
    """`rev@` + 12 hex of SHA-256 over (label, file) pairs in sorted label order: each label length-prefixed, then
    `F` and the length-prefixed content, or `M` for a missing file. Raises CadError for an input that is there and
    does not read (a folder)."""
    h = hashlib.sha256()
    for label, path in sorted(inputs, key=lambda x: x[0]):
        h.update(_framed(label.encode()))
        try:
            h.update(b"F" + _framed(path.read_bytes()))
        except FileNotFoundError:
            h.update(b"M")
        except OSError as e:  # a folder, a file with no read permission
            raise CadError(f"input {label}: cannot read {path}: {e.strerror or e}") from e
    return f"{rev or '-'}@{h.hexdigest()[:12]}"


def relative_to(path: str | Path, folder: Path) -> str:
    """`path` relative to `folder`, with forward slashes (the form a record stores)."""
    return Path(os.path.relpath(Path(path).resolve(), folder.resolve())).as_posix()


@dataclass
class Record:
    path: Path
    data: dict

    @property
    def folder(self) -> Path:
        return self.path.parent

    def resolve(self, rel: str) -> Path:
        return (self.folder / rel).resolve()

    @property
    def inputs(self) -> list[tuple[str, Path]]:
        """(label, file) pairs: the label is the path as stamped (the build id hashes it); `path` finds the file and is
        rewritten when a promoted record lives in another folder."""
        return [(str(i.get("label", i["path"])), self.resolve(str(i["path"]))) for i in self.data.get("inputs", [])]

    def section(self, key: str) -> dict:
        v = self.data.get(key)
        return dict(v) if isinstance(v, Mapping) else {}


def load_record(path: str | Path) -> Record:
    """Raises CadError when the record does not read or is not a `shambu.cad.record/1` document."""
    p = Path(path)
    try:
        data = loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:  # ValueError: not UTF-8, not JSON, or a non-finite number
        raise CadError(f"cannot read record {path}: {e}") from e
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise CadError(f"{path}: not a {SCHEMA} record (write one with `shambu cad stamp`)")
    for key in ("part", "build_id", "stl"):
        if key not in data:
            raise CadError(f"{path}: the record has no {key!r}")
    _check_record(data, str(path))
    return Record(p, data)


def _is_num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_record(data: dict, where: str) -> None:
    """Raises CadError when a field a verb reads has the wrong type or range (the JSON reader already refuses NaN)."""

    def bad(field: str, want: str, got: object) -> CadError:
        return CadError(f"{where}: {field} must be {want}, got {got!r} (stamp the export again)")

    def section(key: str, required: bool = False) -> dict:
        v = data.get(key)
        if v is None and not required:
            return {}
        if not isinstance(v, dict):
            raise bad(key, "an object", v)
        return v

    def num(sec: dict, key: str, field: str, low: float | None = None, strict: bool = False) -> None:
        v = sec.get(key)
        if v is None:
            return
        if not _is_num(v) or (low is not None and (v <= low if strict else v < low)):
            raise bad(field, "a number" + (f" {'above' if strict else 'at least'} {low:g}" if low is not None else ""), v)

    def vec3(sec: dict, key: str, field: str) -> None:
        v = sec.get(key)
        if v is not None and not (isinstance(v, list) and len(v) == 3 and all(_is_num(x) for x in v)):
            raise bad(field, "[x, y, z]", v)

    inputs = data.get("inputs") or []
    if not isinstance(inputs, list) or not all(isinstance(i, dict) and isinstance(i.get("path"), str) for i in inputs):
        raise bad("inputs", 'a list of {"label", "path", "sha256"} objects', inputs)
    stl = section("stl", required=True)
    if not isinstance(stl.get("path"), str) or not stl["path"]:
        raise bad("stl.path", "a path", stl.get("path"))
    num(stl, "faceting_tol_mm", "stl.faceting_tol_mm", 0.0, strict=True)
    num(stl, "angle_tol_deg", "stl.angle_tol_deg", 0.0)
    cad = section("cad")
    num(cad, "volume_mm3", "cad.volume_mm3")
    num(cad, "area_mm2", "cad.area_mm2", 0.0)
    vec3(cad, "cog", "cad.cog")
    step = section("step")
    if step.get("path") is not None and not isinstance(step["path"], str):
        raise bad("step.path", "a path or null", step["path"])
    num(step, "reimport_volume_mm3", "step.reimport_volume_mm3")
    vec3(section("print"), "bed_face", "print.bed_face")


def write_new(path: Path, text: str) -> None:
    """Write `text` to `path`, which must not exist: through a temp file in the same folder and a hard link, so a
    concurrent writer cannot be overwritten either. Raises CadError (exit 3) on any OSError, FileExistsError included."""
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp", encoding="utf-8") as fh:
            tmp = Path(fh.name)
            fh.write(text)
        os.link(tmp, path)
    except FileExistsError as e:
        raise CadError(f"{path} exists: shambu cad writes only new files and never overwrites one") from e
    except OSError as e:
        raise CadError(f"cannot write {path}: {e}") from e
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def stamp_record(
    stl: Path,
    out: Path,
    part: str,
    rev: str | None,
    inputs: Iterable[str | Path],
    mesh: dict,
    cad: dict | None,
    step: Path | None,
    step_volume_mm3: float | None,
    faceting_tol_mm: float | None,
    angle_tol_deg: float | None,
    bed_face: list[float] | None,
    version: str,
    stl_sha: str,
) -> dict:
    """The record for `stl`, written to `out` by the caller. `stl_sha` is the hash of the bytes the mesh facts came
    from. `inputs` are paths as the caller gives them; the record keeps them relative to `out`'s folder. Raises
    CadError when the STEP does not read."""
    folder = out.parent
    try:
        step_sha = sha256_file(step) if step is not None else None
    except OSError as e:
        raise CadError(f"cannot read {e.filename}: {e.strerror}") from e
    labelled = sorted((relative_to(p, folder), Path(p).resolve()) for p in inputs)
    rec: dict = {
        "schema": SCHEMA,
        "part": part,
        "rev": rev,
        "build_id": build_id(labelled, rev),
        "inputs": [{"label": label, "path": label, "sha256": sha256_file(p) if p.is_file() else None} for label, p in labelled],
        "stl": {"path": relative_to(stl, folder), "sha256": stl_sha, "faceting_tol_mm": faceting_tol_mm, "angle_tol_deg": angle_tol_deg},
        "mesh": mesh,
        "cad": cad,
        "step": {"path": relative_to(step, folder), "sha256": step_sha, "reimport_volume_mm3": step_volume_mm3}
        if step is not None
        else None,
        "print": {"bed_face": bed_face},
        "stamped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "shambu": version,
    }
    if step is None and step_volume_mm3 is not None:
        rec["step"] = {"path": None, "sha256": None, "reimport_volume_mm3": step_volume_mm3}
    return rec


def freshness(record: Record, stl: Path, stl_sha: str) -> tuple[dict, list[Finding]]:
    """(measures, findings) of the `provenance` area: the build id, the STL hash and the STEP hash against the record."""
    fs: list[Finding] = []
    part = str(record.data.get("part"))
    rerun = "re-export, then `shambu cad stamp` again and re-run the gate"
    now = build_id(record.inputs, record.data.get("rev")) if record.data.get("inputs") else None
    want = record.data.get("build_id")
    build_ok = now is None or now == want
    if not build_ok:
        missing = [label for label, p in record.inputs if not p.is_file()]
        fs.append(
            Finding(
                "cad.stale.build_id",
                FAIL,
                "provenance",
                f"the inputs now hash to {now}, the record says {want}: the export is from another build"
                + (f" (missing: {', '.join(missing)})" if missing else ""),
                MEASURED,
                part,
                measure={"build_id_now": now, "build_id_record": want, "expected_from": "record"},
                fix=Fix(rerun),
            )
        )
    stl_rec = record.section("stl")
    stl_ok = stl_rec.get("sha256") == stl_sha
    if not stl_ok:
        fs.append(
            Finding(
                "cad.stale.stl_sha",
                FAIL,
                "provenance",
                "the STL's SHA-256 is not the one stamped: the file changed after the record was written",
                MEASURED,
                part,
                measure={"stl_sha256": stl_sha[:12], "record_sha256": str(stl_rec.get("sha256"))[:12], "expected_from": "record"},
                fix=Fix(rerun),
            )
        )
    step_ok: bool | None = None
    step_rec = record.section("step")
    if step_rec.get("path"):
        sp = record.resolve(str(step_rec["path"]))
        now_step = sha256_file(sp) if sp.is_file() else None
        step_ok = now_step is not None and now_step == step_rec.get("sha256")
        if not step_ok:
            fs.append(
                Finding(
                    "cad.stale.step_sha",
                    FAIL,
                    "provenance",
                    "the STEP the record names is missing" if now_step is None else "the STEP changed after the record was written",
                    MEASURED,
                    part,
                    measure={"step_path": str(step_rec["path"]), "expected_from": "record"},
                    fix=Fix(rerun),
                )
            )
    if stl_rec.get("path") and record.resolve(str(stl_rec["path"])) != stl.resolve():
        fs.append(
            Finding(
                "cad.record_path",
                WARN,
                "provenance",
                f"the record names {stl_rec['path']}, not this file; the SHA-256 decides freshness",
                MEASURED,
                part,
                measure={"record_stl_path": str(stl_rec["path"])},
            )
        )
    measures = {"build_id": want, "build_id_ok": build_ok, "stl_sha_ok": stl_ok, "step_sha_ok": step_ok}
    return measures, fs


def promote(src: Path, data: bytes, checked_sha: str, dest: Path, record: Record | None) -> dict:
    """Publish `data`, the bytes the check read from `src` and judged, as the NEW file `dest` and, with a record,
    write a NEW record beside it (`<dest stem>.cad.json`, with `stl.path` pointing at `dest`; the SHA stays). The
    source is never read again, so a writer that replaces it after the check cannot slip unchecked bytes through.
    Refuses (CadError, exit 3) when the bytes do not hash to `checked_sha` or when either destination exists. The
    copy goes through a temp file and a hard link (never over an existing file), then its SHA-256 is compared with
    `checked_sha`."""
    if hashlib.sha256(data).hexdigest() != checked_sha:
        raise CadError(f"{src}: the bytes to promote are not the ones the check hashed; nothing was written")
    rec_dest = dest.with_name(dest.stem + ".cad.json") if record is not None else None
    if dest.exists() or (rec_dest is not None and rec_dest.exists()):
        raise CadError(f"{dest if dest.exists() else rec_dest} exists: --promote writes only a new file; give a new revision name")
    tmp = None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("wb", dir=dest.parent, delete=False, suffix=".tmp") as fh:
            tmp = Path(fh.name)
            fh.write(data)
        os.link(tmp, dest)
    except FileExistsError as e:
        raise CadError(f"{dest} exists: --promote writes only a new file") from e
    except OSError as e:
        raise CadError(f"cannot promote {src} to {dest}: {e}") from e
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    if sha256_file(dest) != checked_sha:
        raise CadError(f"{dest}: the copy does not match the checked bytes of {src}")
    out: dict = {"stl": str(dest), "sha256": checked_sha, "record": None}
    if record is not None and rec_dest is not None:
        new = json.loads(json.dumps(record.data))
        old_folder = record.folder.resolve()
        new_folder = rec_dest.parent.resolve()
        for item in new.get("inputs", []):
            item["path"] = relative_to(old_folder / item["path"], new_folder)
        new["stl"]["path"] = relative_to(dest, new_folder)
        if isinstance(new.get("step"), dict) and new["step"].get("path"):
            new["step"]["path"] = relative_to(old_folder / new["step"]["path"], new_folder)
        new["promoted_from"] = relative_to(src, new_folder)
        write_new(rec_dest, json.dumps(new, indent=1, allow_nan=False) + "\n")
        out["record"] = str(rec_dest)
    return out
