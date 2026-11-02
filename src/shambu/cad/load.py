"""The file side of `shambu cad`: STL files and 3MF objects to `Subject`s, gate specs and records from disk, and the
`Scene` a gate run reads its parts through.

What it handles: STL through `mesh.read_stl`, a 3MF object through `edits.object_meshes` (its normal parts in
plate-local mm), SHA-256 of each file, spec paths relative to the spec file, and caching (one read per part per
run). A subject's hash, its facets and the bytes `--promote` publishes all come from one read of the file. What it does not do: no checking and no printing. Every error is a `CadError`: `MeshMissing` for a file that is
not there, `CadError` for one that does not read, `CadSpecError` for a spec that breaks its schema.
"""

from __future__ import annotations

import glob
import hashlib
from pathlib import Path

from ..errors import BambuError, CadError, CadSpecError, MeshMissing
from . import provenance as P
from . import spec as S
from .jsonin import loads
from .report import Subject


def load_subject(path: str | Path, name: str | None = None, obj: str | None = None) -> Subject:
    """One mesh: an STL, or one object of a 3MF (`obj`; may be omitted when the project holds one object)."""
    p = Path(path)
    if not p.exists():
        raise MeshMissing(f"{path}: no such file")
    low = p.name.lower()
    try:
        raw = p.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if low.endswith(".stl"):
            from ..mesh import parse_stl

            facets = parse_stl(raw, str(path))
            if not facets:
                raise CadError(f"{path}: the STL has no facets")
            return Subject(name or p.stem, str(path), facets, sha, "stl", raw=raw)
        if low.endswith(".3mf"):
            from ..edits import object_meshes
            from ..threemf import Project

            project = Project(str(p))
            if P.sha256_file(p) != sha:
                raise CadError(f"{path}: the file changed while it was read; read it again when the writer is done")
            meshes = object_meshes(project)
            names = [m.name for m in meshes]
            if obj is None:
                if len(meshes) != 1:
                    raise CadError(f"{path}: name the object (--object); objects: {names}")
                chosen = meshes[0]
            else:
                found = [m for m in meshes if m.name == obj]
                if not found:
                    raise CadError(f"{path}: no object named {obj!r}; objects: {names}")
                chosen = found[0]
            if not chosen.facets:
                raise CadError(f"{path}: object {chosen.name!r} has no mesh")
            return Subject(name or chosen.name, str(path), list(chosen.facets), sha, "3mf", chosen.name, raw)
    except OSError as e:
        raise CadError(f"cannot read {path}: {e}") from e
    except CadError:
        raise
    except BambuError as e:  # BuildError from read_stl, ProjectError and EditError from the 3MF reader
        raise CadError(f"{path}: {e}") from e
    raise CadError(f"{path}: shambu cad reads .stl and .3mf files")


def load_spec(path: str | Path) -> S.CadSpec:
    p = Path(path)
    try:
        doc = loads(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise CadError(f"cannot read spec {path}: {e}") from e
    except ValueError as e:  # not UTF-8, not JSON, or a non-finite number
        raise CadSpecError(f"{path}: not JSON: {e}") from e
    try:
        return S.parse(doc, str(path), str(p.parent))
    except (TypeError, ValueError, KeyError) as e:
        if isinstance(e, CadSpecError):
            raise
        raise CadSpecError(f"{path}: bad spec ({type(e).__name__}: {e})") from e


def load_json(path: str | Path, what: str) -> object:
    try:
        return loads(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise CadError(f"cannot read {what} {path}: {e}") from e
    except ValueError as e:  # not UTF-8, not JSON, or a non-finite number
        raise CadSpecError(f"{path}: {what} is not JSON: {e}") from e


class Scene:
    """The parts of a spec, read on first use and kept for the run."""

    def __init__(self, spec: S.CadSpec):
        self.spec = spec
        self.base = Path(spec.base)
        self._subjects: dict[str, Subject] = {}

    def path(self, part: S.PartSpec) -> Path:
        return self.base / part.mesh

    def subject(self, part: S.PartSpec) -> Subject:
        """Raises MeshMissing or CadError."""
        if part.name not in self._subjects:
            self._subjects[part.name] = load_subject(self.path(part), part.name, part.object)
        return self._subjects[part.name]

    def loaded(self) -> dict[str, Subject]:
        """The subjects read so far, by part name."""
        return dict(self._subjects)

    def record(self, part: S.PartSpec) -> P.Record | None:
        """The part's record, None when the spec declares none. Raises MeshMissing when a declared record is missing."""
        if part.record is None:
            return None
        rp = self.base / part.record
        if not rp.exists():
            raise MeshMissing(f"{part.record}: the declared record is missing (run shambu cad stamp)")
        return P.load_record(rp)

    def build_id(self) -> str | None:
        """The build id of the spec's `provenance.inputs` (globs expanded, sorted), or None without inputs."""
        if not self.spec.provenance_inputs:
            return None
        files: list[tuple[str, Path]] = []
        for pat in self.spec.provenance_inputs:
            hits = sorted(glob.glob(str(self.base / pat)))
            if not hits:
                files.append((pat, self.base / pat))
            for h in hits:
                files.append((P.relative_to(h, self.base), Path(h)))
        return P.build_id(files, self.spec.rev)
