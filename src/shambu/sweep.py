"""Sweep: build several variants of one spec and compare the slicer's own numbers.

Not here: no judgement of which variant is "best" beyond sorting by time (strength and finish are the user's
call), no parallel builds (one Studio process at a time), and no reading of G-code beyond what `threemf`
already extracts.

The slicer is the only honest judge of print time and filament, and headless slicing takes about a second
per plate, so an agent can try orientations, print sequences, layer heights and support choices and read
the answer off a table instead of guessing. A variant is a small JSON patch over the spec:

{
  "name": "flat-by-object",                       # becomes <outdir>/sweep/<name>/<spec name>.gcode.3mf
  "process": {"set": {"layer_height": "0.28"}, "recipes": ["coarse-fast"]},   # set merges, recipes replace
  "plates": {"jaws": {"print_sequence": "by object"}},                        # by plate name or 1-based index
  "parts": {"jaw_print": {"lay_on_face": [0, 0, 1], "set": {"enable_support": "0"}}},  # by part key, every plate
  "merge": {"bed_type": "Cool Plate"}             # RFC 7386 merge patch over the whole spec, applied last
}

`orientation_variants()` turns the best candidates of `mesh.candidates()` into variants for one part, and
`by_object_variant()` flips every plate to print-by-object, so the two hacks the skill leans on most are one
flag each on the command line.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import mesh as M
from .errors import BambuError, BuildError
from .spec import Spec

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
    out = dict(part)
    for k, v in change.items():
        if k == "set":
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


def orientation_variants(spec_json: dict, part_key: str, top: int = 4, threshold_deg: float = 45.0) -> list[dict]:
    """One variant per best orientation of the part (its first file), from `mesh.candidates()`."""
    path = None
    for pl in spec_json.get("plates", []):
        for p in pl.get("parts", []):
            if _part_key(p) == part_key:
                path = p.get("path") or (p.get("parts") or [{}])[0].get("path")
    if not path:
        raise BuildError(f"no part with key {part_key!r} in the spec")
    cands = M.candidates(M.read_stl(Path(path).expanduser()), threshold_deg)[:top]
    out = []
    for i, c in enumerate(cands, 1):
        label = c.label.replace(" ", "-")
        out.append(
            {
                "name": f"{part_key}-{i}-{label}",
                "parts": {part_key: {"lay_on_face": [round(v, 6) for v in c.normal], "rotate": [0, 0, 0]}},
                "orientation": c.to_json(),
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

    @property
    def total_s(self) -> int:
        return sum(int(p.get("prediction_s") or 0) for p in self.plates)

    @property
    def total_g(self) -> float:
        return sum(float(p.get("weight_g") or 0) for p in self.plates)

    def feature_total(self, key: str) -> float:
        return sum(float((p.get("features") or {}).get(key) or 0) for p in self.plates)

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "project": self.project,
            "sliced": self.sliced,
            "total_s": self.total_s,
            "total_g": round(self.total_g, 1),
            "support_g": round(self.feature_total("support_g"), 1),
            "waste_g": round(self.feature_total("waste_g"), 1),
            "filament_changes": int(self.feature_total("filament_changes")),
            "plates": self.plates,
            "problems": self.problems,
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
            d = apply_variant(spec_json, v)
            if "outdir" not in d:
                raise BuildError("spec lacks 'outdir'")
            d["outdir"] = str(Path(d["outdir"]).expanduser() / "sweep" / v["name"])
            spec = Spec.from_json(d)
            r = build(spec, do_slice)
        except BambuError as e:  # a preset, mesh or Studio failure of ONE variant: record it, keep sweeping
            out.append(VariantResult(v["name"], False, "", do_slice, problems=[str(e)], orientation=v.get("orientation")))
            continue
        j = r.to_json()
        problems = list(j.get("problems") or []) + [f"verify: {x}" for x in (j.get("verify_problems") or [])]
        if j.get("cli") and r.run is not None and not r.run.ok:
            problems.append(f"export failed: {r.run.summary()}")
        plates = [
            {
                "plate": e["plate"],
                "prediction_s": e.get("prediction_s", 0),
                "weight_g": e.get("weight_g", 0.0),
                "filaments": e.get("filaments", []),
                "features": e.get("features"),
            }
            for e in j.get("estimates", [])
        ]
        out.append(
            VariantResult(
                v["name"], bool(j.get("ok")), j.get("project", ""), do_slice, plates, problems, j.get("report") or "", v.get("orientation")
            )
        )
    return out


def _hms(s: int) -> str:
    return f"{s // 3600}h{s % 3600 // 60:02d}"


def table(results: Sequence[VariantResult]) -> list[str]:
    """Sorted by time, fastest first; variants that failed sink to the bottom with their first problem."""
    ok = sorted((r for r in results if r.ok and r.plates), key=lambda r: (r.total_s, r.total_g))
    rest = [r for r in results if not (r.ok and r.plates)]
    best = ok[0] if ok else None
    lines = [
        f"{'variant':34s} {'time':>7} {'grams':>7} {'vs best':>8} {'support':>8} {'waste':>6} {'swaps':>5}  filament grams per slot / problems"
    ]
    for r in ok:
        delta = f"+{_hms(r.total_s - best.total_s)}" if best and r is not best else ("best" if best else "")
        fil = ", ".join(f"{f['id']}:{f['used_g']:.0f}" for p in r.plates for f in p["filaments"])
        feats = any(p.get("features") for p in r.plates)
        sup = f"{r.feature_total('support_g'):8.1f}" if feats else f"{'-':>8}"
        waste = f"{r.feature_total('waste_g'):6.1f}" if feats else f"{'-':>6}"
        swaps = f"{int(r.feature_total('filament_changes')):5d}" if feats else f"{'-':>5}"
        lines.append(f"{r.name:34s} {_hms(r.total_s):>7} {r.total_g:7.1f} {delta:>8} {sup} {waste} {swaps}  {fil}")
    for r in rest:
        why = r.problems[0] if r.problems else ("not sliced" if not r.plates else "failed")
        lines.append(f"{r.name:34s} {'-':>7} {'-':>7} {'FAIL':>8} {'-':>8} {'-':>6} {'-':>5}  {why}")
    return lines
