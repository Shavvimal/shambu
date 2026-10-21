"""Sweep: build several variants of one spec and compare the slicer's own numbers.

Not here: no judgement of which variant is "best" beyond sorting by time (strength and finish are the user's
call), no parallel builds (one Studio process at a time), and no reading of G-code beyond what `threemf`
already extracts.

The slicer is the only honest judge of print time and filament, and headless slicing takes about a second
per plate, so an agent can try orientations, print sequences, layer heights and support choices and read
the answer off a table instead of guessing. A variant is a small JSON patch over the spec:

{
  "name": "flat-by-object",                       # becomes <outdir>/sweep/<name>/<spec name>.gcode.3mf
  "process": {"set": {"layer_height": "0.28"}, "recipes": ["coarse-fast"]},   # set merges, recipes REPLACE the list
  "plates": {"jaws": {"print_sequence": "by object"}},                        # by plate name or 1-based index
  "parts": {"jaw_print": {"lay_on_face": [0, 0, 1], "set": {"enable_support": "0"}}},  # by part key, every plate
  "merge": {"bed_type": "Cool Plate"}             # RFC 7386 merge patch over the whole spec, applied last
}

`orientation_variants()` turns the best candidates of `mesh.candidates()` (or, with `source="studio"`, of Studio's
own auto-orient table) into variants for one part, and
`by_object_variant()` flips every plate to print-by-object, so the two hacks the skill leans on most are one
flag each on the command line.

A plate's `checks` are `[ok, text]` pairs with a parallel `check_kinds` list; a WARN (kind margin or brim) is ok=True
and never counts as a failed variant.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import stat
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import mesh as M
from .errors import BambuError, BuildError
from .spec import Spec

if TYPE_CHECKING:
    from .studio import StudioCLI

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
BASELINE = "baseline"


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386: dicts merge recursively, None deletes, anything else replaces."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge_patch(out.get(k), v)
    return out


def _part_key(p: dict) -> str:
    if p.get("name"):
        return str(p["name"])
    path = p.get("path") or (p.get("parts") or [{}])[0].get("path")
    return Path(path).stem if path else ""


def _patch_part(part: dict, change: dict) -> dict:
    """`set` merges; every other key (including `recipes`) replaces, so a variant that adds a recipe must repeat the
    part's existing ones. A None value deletes the key (an orientation variant drops `"orientation": "auto"`)."""
    out = dict(part)
    for k, v in change.items():
        if v is None:
            out.pop(k, None)
        elif k == "set":
            out["set"] = {**out.get("set", {}), **v}
        else:
            out[k] = copy.deepcopy(v)
    return out


def apply_variant(spec_json: dict, variant: dict) -> dict:
    """A new spec dict with the variant applied. The spec passed in is not changed."""
    d = copy.deepcopy(spec_json)
    pr = variant.get("process") or {}
    if pr:
        proc = d.setdefault("process", {})
        if "set" in pr:
            proc["set"] = {**proc.get("set", {}), **pr["set"]}
        for k in ("recipes", "base", "name"):
            if k in pr:
                proc[k] = copy.deepcopy(pr[k])
    for sel, change in (variant.get("plates") or {}).items():
        # a plate NAME wins over a 1-based index, so "1" with a plate named "1" is not ambiguous
        plates = d.get("plates", [])
        hits = [pl for pl in plates if pl.get("name") == sel] or [pl for i, pl in enumerate(plates, 1) if str(sel) == str(i)]
        if not hits:
            raise BuildError(f"variant {variant.get('name')!r}: no plate named or numbered {sel!r}")
        for pl in hits:
            for k, v in change.items():
                if k == "parts" and isinstance(v, dict):
                    keys = {_part_key(p) for p in pl.get("parts", [])}
                    missing = sorted(set(v) - keys)
                    if missing:
                        raise BuildError(f"variant {variant.get('name')!r}: plate {sel!r} has no part {missing[0]!r}")
                    pl["parts"] = [_patch_part(p, v[_part_key(p)]) if _part_key(p) in v else p for p in pl.get("parts", [])]
                else:
                    pl[k] = copy.deepcopy(v)
    for key, change in (variant.get("parts") or {}).items():
        hit = False
        for pl in d.get("plates", []):
            for i, p in enumerate(pl.get("parts", [])):
                if _part_key(p) == key:
                    pl["parts"][i] = _patch_part(p, change)
                    hit = True
        if not hit:
            raise BuildError(f"variant {variant.get('name')!r}: no part with key {key!r}")
    if variant.get("merge"):
        d = merge_patch(d, variant["merge"])
    return d


SOURCES = ("own", "studio")


def orientation_variants(
    spec_json: dict,
    part_key: str,
    top: int = 4,
    threshold_deg: float = M.DEFAULT_THRESHOLD,
    source: str = "own",
    studio: StudioCLI | None = None,
) -> list[dict]:
    """One variant per best orientation of the part (its first file). `source="own"`: `mesh.candidates()`, by
    shambu's score. `source="studio"`: Studio's auto-orient table (`StudioCLI.orient()`, unit fix first), each vector
    negated into a `lay_on_face`, kept when shambu's `stands` check passes on the converted mesh, top `top` by
    Studio's unprintability (a tie goes to its `best:` row); the variant still bakes the turn with
    `mesh.oriented_copies()`. When Studio scales the part (metres, inches), every variant also points the part's
    `path` at a mm copy under `<outdir>/sweep/_units/`. A variant drops a part's `"orientation": "auto"`."""
    if source not in SOURCES:
        raise BuildError(f"orientation source {source!r} not in {SOURCES}")
    path = None
    part: dict = {}
    for pl in spec_json.get("plates", []):
        for p in pl.get("parts", []):
            if _part_key(p) == part_key:
                part = p
                path = p.get("path") or (p.get("parts") or [{}])[0].get("path")
    if not path:
        raise BuildError(f"no part with key {part_key!r} in the spec")
    facets = M.read_stl(Path(path).expanduser())
    if source == "studio":
        return _studio_variants(part_key, spec_json, part, Path(path).expanduser(), facets, top, threshold_deg, studio)
    cands = [c for c in M.candidates(facets, threshold_deg) if c.metrics.stands][:top]
    if not cands:
        raise BuildError(
            f"{part_key}: no orientation puts a flat face on the bed (an organic part); orient it by hand or add a flat base in CAD"
        )
    out = []
    for i, c in enumerate(cands, 1):
        label = re.sub(r"[^A-Za-z0-9._-]", "", c.label.replace("+", "p").replace("-", "m").replace(" ", "-"))
        out.append(
            {
                "name": f"{part_key}-{i}-{label}",
                "parts": {part_key: {"lay_on_face": [round(v, 6) for v in c.normal], "rotate": [0, 0, 0], "orientation": None}},
                "orientation": c.to_json(),
            }
        )
    return out


_SCALES = (1.0, 1000.0, 25.4)  # mm, metres, inches: the factors Studio's --convert-unit applies


def unit_scale(original: Sequence[M.Facet], converted: Sequence[M.Facet]) -> float:
    """The uniform factor Studio applied between two copies of one mesh, whatever rotation and translation it also
    applied: the square root of the surface-area ratio, snapped to 1, 1000 or 25.4 when within 0.1 %."""
    a0 = sum(M.facet_area(f) for f in original)
    a1 = sum(M.facet_area(f) for f in converted)
    if a0 <= 0 or a1 <= 0:
        return 1.0
    k = math.sqrt(a1 / a0)
    return next((s for s in _SCALES if abs(k / s - 1) < 1e-3), k)


def _studio_variants(
    part_key: str, spec_json: dict, part: dict, path: Path, facets: list[M.Facet], top: int, threshold_deg: float, studio: StudioCLI | None
) -> list[dict]:
    if studio is None:
        from .studio import StudioCLI

        studio = StudioCLI()
    # Pass 1 as the build runs it: --convert-unit first, then --orient. The vectors are in the file's own frame (a
    # uniform scale does not turn a direction); the stands check runs on the converted mesh, Studio's export scaled
    # back into that frame.
    with tempfile.TemporaryDirectory(prefix="shambu-orient-") as tmp:
        res = studio.orient([path], Path(tmp), convert_unit=True)
        k = unit_scale(facets, M.read_stl(res.files[0])) if res.files else 1.0
    converted = M.scale(facets, k) if k != 1.0 else facets
    src: Path | None = None
    if k != 1.0:
        # Every variant must carry the unit fix: point it at a mm copy (same stem, so the part key and object name stay)
        if part.get("parts"):
            raise BuildError(
                f"{part_key}: Studio scales this part by {k:g} (not mm) and it merges several files; "
                "convert each file with `shambu rotate FILE -o DIR --convert-unit` first"
            )
        if not spec_json.get("outdir"):
            raise BuildError(f"{part_key}: Studio scales this part by {k:g}; the spec needs 'outdir' for the mm copy")
        sub = re.sub(r"[^A-Za-z0-9._-]", "_", part_key) or "part"
        src = M.write_stl(
            Path(spec_json["outdir"]).expanduser() / "sweep" / "_units" / sub / f"{path.stem}.stl", converted, f"{path.stem} mm"
        )
    seen: list[M.Vec] = []
    picked = []
    from .studio import rank_candidates

    for c in rank_candidates(res.candidates, res.best):  # by unprintability, a tie goes to Studio's best row
        o = M.from_studio_vector(c.vector)
        n = M.unit(o.lay_on_face)  # type: ignore[arg-type]
        if any(M._dot(s, n) > math.cos(math.radians(1.0)) for s in seen):
            continue
        seen.append(n)
        m = M.metrics(M.transform(converted, M.lay_on_face_matrix(n)), threshold_deg)
        if m.stands:
            picked.append((c, o, m))
        if len(picked) >= top:
            break
    if not picked:
        raise BuildError(
            f"{part_key}: none of Studio's {len(res.candidates)} orientations puts a flat face on the bed; orient it by hand or add a flat base in CAD"
        )
    out = []
    for i, (c, o, m) in enumerate(picked, 1):
        change: dict[str, Any] = {"lay_on_face": [round(v, 6) for v in o.lay_on_face or ()], "rotate": [0, 0, 0], "orientation": None}
        if src is not None:
            change["path"] = str(src)
        out.append(
            {
                "name": f"{part_key}-{i}-studio-{o.tag()}"[:80],
                "parts": {part_key: change},
                "orientation": {
                    "label": f"studio {i}",
                    "source": "studio",
                    **c.to_json(),
                    **m.to_json(),
                    **({"unit_scale": k, "converted": str(src)} if src is not None else {}),
                },
            }
        )
    return out


def by_object_variant(spec_json: dict, name: str = "by-object") -> dict:
    return {"name": name, "plates": {str(i): {"print_sequence": "by object"} for i in range(1, len(spec_json.get("plates", [])) + 1)}}


def load_variants(path: str | Path) -> list[dict]:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BuildError(f"{path}: cannot read variants: {e}") from e
    vs = d.get("variants", d) if isinstance(d, dict) else d
    if not isinstance(vs, list) or not all(isinstance(v, dict) and v.get("name") for v in vs):
        raise BuildError(f"{path}: expected a list of variants (objects with a 'name')")
    return vs


def check_names(variants: Sequence[dict], baseline: bool = True) -> None:
    """Names become directories under <outdir>/sweep/, so they must be plain, unique and not the reserved baseline."""
    seen: set[str] = {BASELINE} if baseline else set()
    for v in variants:
        name = str(v.get("name", ""))
        if not _NAME.match(name):
            raise BuildError(f"variant name {name!r}: use letters, digits, '.', '_' or '-' (no path separators), at most 80 characters")
        if name in seen:
            raise BuildError(f"variant name {name!r} is used twice" + (" (it is the reserved baseline)" if name == BASELINE else ""))
        seen.add(name)


TIME_ROLES = ("Support", "Prime tower", "Flush", "Travel")  # result.json feature_type_times keys shown in the table


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 1)


@dataclass
class VariantResult:
    name: str
    ok: bool
    project: str
    sliced: bool
    plates: list[dict] = field(default_factory=list)  # {"plate", "prediction_s", "weight_g", "filaments": [...]}
    problems: list[str] = field(default_factory=list)
    report: str = ""
    orientation: dict | None = None
    warnings: list[str] = field(default_factory=list)  # the build's warnings (the printer fallback WARNING among them)
    filaments: str | None = None  # the build's `Filaments:` headline: where each preset came from
    filament_sources: list[dict] = field(default_factory=list)  # per slot: the preset and colour the build used, and why

    @property
    def total_s(self) -> int:
        return sum(int(p.get("prediction_s") or 0) for p in self.plates)

    @property
    def total_g(self) -> float:
        return sum(float(p.get("weight_g") or 0) for p in self.plates)

    def feature_total(self, key: str) -> float:
        """A total from the G-code parser's `features` (0 for a plate without them)."""
        return sum(float((p.get("features") or {}).get(key) or 0) for p in self.plates)

    @property
    def has_result(self) -> bool:
        """Every plate carries result.json numbers (seconds per feature, tower plus flush grams)."""
        return bool(self.plates) and all("feature_times_s" in p for p in self.plates)

    def support_g(self) -> float | None:
        """Support grams from the G-code parser; 0 on a plate where the slicer used no support; None when unknown."""
        total = 0.0
        for p in self.plates:
            if p.get("features"):
                total += float(p["features"].get("support_g") or 0)
            elif p.get("support_used") != "false":
                return None
        return total

    def waste_g(self) -> float | None:
        """Prime tower plus flush grams summed per plate: result.json (total minus main per filament) on a plate that
        carries it, else that plate's G-code parser value; None when a plate has neither."""
        total = 0.0
        for p in self.plates:
            native = [float(f["waste_g"]) for f in p.get("filaments") or [] if f.get("waste_g") is not None]
            if native:
                total += sum(native)
            elif p.get("features"):
                total += float(p["features"].get("waste_g") or 0)
            else:
                return None
        return total if self.plates else None

    def filament_changes(self) -> int | None:
        """Tool switches from the G-code parser (0 on a one-filament plate); None when a plate lacks the count.
        Not result.json `filament_change_times`: it counts filament loads and gave 0 against 41 nozzle switches on an
        X2D plate (live 2026-10-13), and the nozzle switch is the swap that print-by-object removes."""
        total = 0
        for p in self.plates:
            if p.get("filament_changes") is not None:
                total += int(p["filament_changes"])
            elif p.get("features"):
                total += int(p["features"].get("filament_changes") or 0)
            else:
                return None
        return total

    def feature_time(self, role: str) -> float | None:
        """Seconds spent on one ExtrusionRole (or "Travel") over all plates, from result.json; None without it."""
        if not self.has_result:
            return None
        return sum(float((p.get("feature_times_s") or {}).get(role) or 0) for p in self.plates)

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "project": self.project,
            "sliced": self.sliced,
            "total_s": self.total_s,
            "total_g": round(self.total_g, 1),
            "support_g": _round(self.support_g()),
            "waste_g": _round(self.waste_g()),
            "filament_changes": self.filament_changes(),
            "feature_times_s": {k: round(v, 1) for k in TIME_ROLES if (v := self.feature_time(k)) is not None},
            "plates": self.plates,
            "problems": self.problems,
            "warnings": self.warnings,
            "filaments": self.filaments,
            "report": self.report,
            **({"orientation": self.orientation} if self.orientation else {}),
        }


Builder = Callable[[Spec, bool], Any]  # (spec, do_slice) -> BuildResult


def run(spec_json: dict, variants: Sequence[dict], build: Builder, *, do_slice: bool = True, baseline: bool = True) -> list[VariantResult]:
    """Build every variant (plus the untouched spec as "baseline") into <outdir>/sweep/<name>/."""
    check_names(variants, baseline)
    todo = ([{"name": BASELINE}] if baseline else []) + list(variants)
    out: list[VariantResult] = []
    for v in todo:
        try:
            try:  # a structurally bad spec or variant (e.g. "process": {}) fails this variant, not the whole sweep
                d = apply_variant(spec_json, v)
                if "outdir" not in d:
                    raise BuildError("spec lacks 'outdir'")
                d["outdir"] = str(Path(d["outdir"]).expanduser() / "sweep" / v["name"])
                spec = Spec.from_json(d)
            except (KeyError, TypeError, ValueError, IndexError, AttributeError) as e:
                raise BuildError(f"bad spec for variant '{v['name']}' ({type(e).__name__}: {e})") from e
            r = build(spec, do_slice)
        except BambuError as e:  # a preset, mesh or Studio failure of ONE variant: record it, keep sweeping
            out.append(VariantResult(v["name"], False, "", do_slice, problems=[str(e)], orientation=v.get("orientation")))
            continue
        j = r.to_json()
        problems = list(j.get("problems") or []) + [f"verify: {x}" for x in (j.get("verify_problems") or [])]
        problems += [f"check: {text}" for pl in j.get("plates") or [] for ok, text in pl.get("checks") or [] if not ok]
        if j.get("cli") and r.run is not None and not r.run.ok:
            problems.append(f"export failed: {r.run.summary()}")
        plates = [
            {
                "plate": e["plate"],
                "prediction_s": e.get("prediction_s", 0),
                "weight_g": e.get("weight_g", 0.0),
                "filaments": e.get("filaments", []),
                "features": e.get("features"),
                "support_used": e.get("support_used"),
                **{k: e[k] for k in ("feature_times_s", "main_prediction_s", "filament_changes", "studio_filament_change_times") if k in e},
            }
            for e in j.get("estimates", [])
        ]
        out.append(
            VariantResult(
                v["name"],
                bool(j.get("ok")),
                j.get("project", ""),
                do_slice,
                plates,
                problems,
                j.get("report") or "",
                v.get("orientation"),
                list(j.get("warnings") or []),
                (j.get("filaments") or {}).get("headline"),
                [
                    {k: s.get(k) for k in ("slot", "preset", "colour", "source")}
                    for s in (j.get("filaments") or {}).get("sources") or []
                    if isinstance(s, dict)
                ],
            )
        )
    return out


def _mins(s: int) -> int:
    return round(s / 60)


def _hms(s: int) -> str:
    m = _mins(s)
    return f"{m // 60}h{m % 60:02d}"


def ranked(results: Sequence[VariantResult]) -> list[VariantResult]:
    """The variants that passed and sliced, fastest first, then fewest grams: the table's order."""
    return sorted((r for r in results if r.ok and r.plates), key=lambda r: (r.total_s, r.total_g))


def table(results: Sequence[VariantResult]) -> list[str]:
    """Sorted by time, fastest first; variants that failed sink to the bottom with their first problem."""
    ok = ranked(results)
    rest = [r for r in results if not (r.ok and r.plates)]
    best = ok[0] if ok else None
    mins = "".join(f" {h:>10}" for h in _TIME_HEADS)
    lines = list(dict.fromkeys(w for r in results for w in r.warnings if w.startswith("WARNING")))  # the printer fallback, once
    lines += list(dict.fromkeys(r.filaments for r in results if r.filaments))  # the filament sources (one line when all agree)
    lines.append(
        f"{'variant':34s} {'time':>7} {'grams':>7} {'vs best':>8} {'support':>8} {'waste':>6} {'swaps':>5}{mins}  filament grams per slot / problems"
    )
    for r in ok:
        delta = f"+{_hms(60 * (_mins(r.total_s) - _mins(best.total_s)))}" if best and r is not best else ("best" if best else "")
        fil = ", ".join(f"{f['id']}:{f['used_g']:.0f}" for p in r.plates for f in p["filaments"])
        sup, waste, swaps = r.support_g(), r.waste_g(), r.filament_changes()
        cells = f"{_cell(sup, 8)} {_cell(waste, 6)} {_cell(swaps, 5, '5d')}"
        times = "".join(f" {_cell(None if (t := r.feature_time(k)) is None else t / 60, 10)}" for k in TIME_ROLES)
        lines.append(f"{r.name:34s} {_hms(r.total_s):>7} {r.total_g:7.1f} {delta:>8} {cells}{times}  {fil}")
    for r in rest:
        why = r.problems[0] if r.problems else ("not sliced" if not r.plates else "failed")
        dashes = "".join(f" {'-':>10}" for _ in TIME_ROLES)
        lines.append(f"{r.name:34s} {'-':>7} {'-':>7} {'FAIL':>8} {'-':>8} {'-':>6} {'-':>5}{dashes}  {why}")
    return lines


_TIME_HEADS = ("supp min", "tower min", "flush min", "travel min")  # TIME_ROLES in minutes, from result.json


def _cell(v: float | None, width: int, fmt: str = "") -> str:
    if v is None:
        return f"{'-':>{width}}"
    return f"{int(v):{fmt}}" if fmt else f"{v:{width}.1f}"


# ---------------------------------------------------------------------------- --write-spec
def choose(results: Sequence[VariantResult], pick: str | None = None) -> VariantResult | None:
    """The variant to write: `pick` by name (passed or not; the caller refuses a failed one), else the table's first row."""
    if pick is not None:
        return next((r for r in results if r.name == pick), None)
    best = ranked(results)
    return best[0] if best else None


def _paths(d: dict) -> list[str]:
    out = []
    for pl in d.get("plates", []):
        for p in pl.get("parts", []):
            out += [str(q["path"]) for q in [p, *(p.get("parts") or [])] if isinstance(q, dict) and q.get("path")]
    return out


def written_spec(
    spec_json: dict,
    variant: dict,
    result: VariantResult,
    source: str,
    baseline: VariantResult | None = None,
    *,
    studio_version: str | None = None,
    command: Sequence[str] | None = None,
) -> dict:
    """The chosen variant applied to the input spec as a full spec, with the input's own `outdir` (not
    `<outdir>/sweep/<name>`), so `shambu build` on it writes beside the original project. `_sweep` records where it came
    from, the numbers that chose it, the filament presets and colours the measurement used (the spec keeps `"loaded"`:
    the next build reads the printer again), the Studio version and the sweep command that reproduces it.
    `Spec.from_json` ignores the key. A part path the variant changed (a mm copy under `sweep/_units/`) is listed in
    `_sweep.paths_changed`; `relocate_meshes` moves those next to the written spec."""
    d = apply_variant(spec_json, variant)
    if "outdir" in spec_json:
        d["outdir"] = spec_json["outdir"]
    sliced = result.sliced and bool(result.plates)
    prov: dict[str, Any] = {
        "from": source,
        "variant": result.name,
        "studio_estimate_s": result.total_s if sliced else None,
        "grams": round(result.total_g, 1) if sliced else None,
        "baseline_s": baseline.total_s if baseline is not None and baseline.ok and baseline.sliced and baseline.plates else None,
        "measured_with": [dict(s) for s in result.filament_sources],
        "studio_version": studio_version,
        "command": list(command) if command else None,
    }
    before = set(_paths(spec_json))
    changed = [p for p in _paths(d) if p not in before]
    if changed:
        prov["paths_changed"] = changed
    if variant.get("orientation"):
        prov["orientation"] = variant["orientation"]
    d["_sweep"] = prov
    return d


def _hm(s: int) -> str:
    m = _mins(s)
    return f"{m // 60} h {m % 60} min"


def written_line(path: str, prov: dict, failed: Sequence[str] = ()) -> str:
    """`wrote OUT.json: variant <name> (Studio estimate H h M min, G g; baseline H h M min), measured with <preset 1> /
    <preset 2>`, plus the failed variants."""
    if prov.get("studio_estimate_s") is not None:
        nums = f"Studio estimate {_hm(prov['studio_estimate_s'])}, {prov['grams']:.1f} g"
    else:
        nums = "not sliced"
    base = f"baseline {_hm(prov['baseline_s'])}" if prov.get("baseline_s") is not None else "no baseline numbers"
    used = [str(s.get("preset")) for s in prov.get("measured_with") or [] if s.get("preset")]
    measured = f", measured with {' / '.join(used)}" if used else ""
    tail = f"; failed: {', '.join(failed)}" if failed else ""
    return f"wrote {path}: variant {prov['variant']} ({nums}; {base}){measured}{tail}"


def _part_dicts(d: dict) -> list[dict]:
    out: list[dict] = []
    for pl in d.get("plates", []):
        for p in pl.get("parts", []):
            out.append(p)
            out += [q for q in p.get("parts") or [] if isinstance(q, dict)]
    return out


def relocate_meshes(spec: dict, out_path: str | Path) -> dict:
    """Copies every mesh in `_sweep.paths_changed` (a variant's file under `<outdir>/sweep/`) to `<OUT stem>_meshes/`
    beside the written spec and points the spec at the copy, so the spec does not depend on the sweep folder.
    `_sweep.paths_changed` then maps each old path to its copy."""
    import shutil

    changed = (spec.get("_sweep") or {}).get("paths_changed") or []
    if not changed:
        return spec
    out_path = Path(out_path).expanduser()
    dest = out_path.parent / f"{out_path.stem}_meshes"
    moved: dict[str, str] = {}
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for i, old in enumerate(changed):
            src = Path(old).expanduser()
            target = dest / src.name
            if target.exists() and str(target) not in moved.values() and target.read_bytes() != src.read_bytes():
                target = dest / f"{i}-{src.name}"  # two different files of one name
            shutil.copyfile(src, target)
            moved[old] = str(target.resolve())
    except OSError as e:
        raise BuildError(f"cannot copy the variant's mesh into {dest}: {e.strerror or e}") from e
    for p in _part_dicts(spec):
        if p.get("path") in moved:
            p["path"] = moved[p["path"]]
    spec["_sweep"]["paths_changed"] = [{"from": k, "to": v} for k, v in moved.items()]
    return spec


def _umask() -> int:
    u = os.umask(0)
    os.umask(u)
    return u


def check_destination(out: str | Path, spec_path: str | Path, variants_path: str | Path | None = None) -> str | None:
    """Why `out` must not be written (the input spec or the variants file under another name, a symlink or hard
    link of one of them), or None. Runs before the sweep, so a refusal costs no slicing."""
    o = Path(out).expanduser()
    for label, other in (("the input spec", spec_path), ("the --variants file", variants_path)):
        if other is None:
            continue
        q = Path(other).expanduser()
        if o.resolve() == q.resolve() or (o.exists() and q.exists() and os.path.samefile(o, q)):
            return f"--write-spec {out}: that is {label}; write the result to another file"
    return None


def writable_folder(out: str | Path) -> str | None:
    """Why the folder for `out` cannot take the file, or None: the nearest existing folder up the path must be
    writable. Checked before the sweep, so a full sweep is never thrown away at the last write."""
    d = Path(out).expanduser().resolve().parent
    while not d.exists() and d != d.parent:
        d = d.parent
    if not d.is_dir():
        return f"--write-spec {out}: {d} is not a folder"
    if not os.access(d, os.W_OK | os.X_OK):
        return f"--write-spec {out}: cannot write in {d} (permission denied)"
    return None


def write_spec(path: str | Path, spec: dict) -> Path:
    """Writes through a new, uniquely named temporary file in the same folder (created exclusively, so it can never be
    an existing file such as the input spec), then renames it over `path`. A crash never leaves half a spec. A failed
    write or rename removes the temporary file and raises BuildError naming the destination and the reason. The file
    gets the mode of the file it replaces, else 0666 under the umask (a temporary file starts at 0600)."""
    p = Path(path).expanduser()
    tmp: Path | None = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IMODE(p.stat().st_mode) if p.exists() else 0o666 & ~_umask()
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=p.parent, prefix=f".{p.name}.", suffix=".tmp", delete=False) as f:
            tmp = Path(f.name)
            f.write(json.dumps(spec, indent=2) + "\n")
        os.chmod(tmp, mode)
        tmp.replace(p)
    except OSError as e:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        raise BuildError(f"cannot write the spec to {p}: {e.strerror or e}") from e
    return p
