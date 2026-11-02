"""The result of every `shambu cad` verb: one `CadReport` with rule results, findings, the not-checked list and the
derived gate word, as JSON (one document, schema `shambu.cad.result/1`) and as text lines.

Verdicts use PR 1's finding model and add no severity and no evidence level:

- a rule or area with no FAIL or WARN finding is `no-finding`; one not run is `not-checked` with a reason;
- INCOMPLETE is a FAIL finding whose code starts `cad.incomplete.` (a selector with no match, a missing or unreadable
  mesh, a declared record that is missing, a volume cap on a pair with no volume);
- STALE is a FAIL finding whose code starts `cad.stale.`.

The gate word is derived, never stored: `stale` if any `cad.stale.*`, else `fail` if any other FAIL, else `incomplete`
if any `cad.incomplete.*`, else `pass`. A pass means no blocking finding in the rules that ran; the not-checked list
says what that excludes. The text never says "ready" or "OK to print".

What it does not do: no checking (rules.py), no file I/O and no printing (cli.py prints `lines()`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..findings import FAIL, INFO, WARN, Finding, tally
from ..printability import ATTENTION, NO_FINDING, NOT_CHECKED_STATUS, AreaResult

SCHEMA = "shambu.cad.result/1"
STALE, FAILED, INCOMPLETE, PASS = "stale", "fail", "incomplete", "pass"

NOT_CHECKED: tuple[dict[str, str], ...] = (
    {"what": "strength under load", "why": "a geometry gate only", "use": "the user's analysis or a test print"},
    {"what": "printed size of fits", "why": "a mesh is the CAD size, not the printed size", "use": "a coupon print"},
    {
        "what": "overhangs, bridges, walls and tip-over",
        "why": "printability owns them",
        "use": "shambu printability PART.stl --lay-on-face=...",
    },
    {"what": "faces, edges and tolerances a STEP B-rep carries", "why": "the gates read meshes", "use": "the CAD session"},
)


@dataclass
class RuleResult:
    id: str
    type: str
    status: str  # attention | no-finding | not-checked
    reason: str | None = None
    seconds: float = 0.0
    data: dict = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    text: list[str] = field(default_factory=list)  # summary lines for the text output (not in the JSON)

    def to_json(self) -> dict:
        d: dict = {"id": self.id, "type": self.type, "status": self.status}
        if self.reason:
            d["reason"] = self.reason
        d["seconds"] = round(self.seconds, 3)
        d.update(self.data)
        return d


def status_of(findings: list[Finding]) -> str:
    return ATTENTION if any(f.severity in (FAIL, WARN) for f in findings) else NO_FINDING


def areas_json(areas: dict[str, AreaResult]) -> dict:
    return {k: v.to_json() for k, v in areas.items()}


def area_findings(areas: dict[str, AreaResult]) -> list[Finding]:
    return [f for a in areas.values() for f in a.findings]


def _key(f: Finding) -> tuple:
    p = f.location.point if f.location else (0.0, 0.0, 0.0)
    return (f.code, f.object or "", round(p[0], 3), round(p[1], 3), round(p[2], 3), f.message)


@dataclass
class CadReport:
    verb: str
    rules: list[RuleResult]
    spec: str | None = None
    build_id: str | None = None
    inputs: list[dict] = field(default_factory=list)
    spec_notes: dict | None = None
    extra: dict = field(default_factory=dict)
    not_checked: list[dict] = field(default_factory=lambda: [dict(x) for x in NOT_CHECKED])
    elapsed_s: float = 0.0
    text: list[str] = field(default_factory=list)  # verb summary lines for the text output (not in the JSON)

    def ordered(self) -> list[tuple[str, Finding]]:
        """(rule id, finding) sorted by rule order, then code and location: shuffled facets give the same JSON."""
        out = []
        for r in self.rules:
            out += [(r.id, f) for f in sorted(r.findings, key=_key)]
        return out

    @property
    def findings(self) -> list[Finding]:
        return [f for _, f in self.ordered()]

    @property
    def counts(self) -> dict[str, int]:
        return tally(self.findings)

    @property
    def gate(self) -> str:
        fails = [f for f in self.findings if f.severity == FAIL]
        if any(f.code.startswith("cad.stale.") for f in fails):
            return STALE
        if any(not f.code.startswith("cad.incomplete.") for f in fails):
            return FAILED
        if fails:
            return INCOMPLETE
        return PASS

    @property
    def exit_code(self) -> int:
        return 0 if self.gate == PASS else 1

    def to_json(self) -> dict:
        d: dict = {
            "schema": SCHEMA,
            "verb": self.verb,
            "spec": self.spec,
            "build_id": self.build_id,
            "gate": self.gate,
            "inputs": self.inputs,
        }
        if self.spec_notes is not None:
            d["spec_notes"] = self.spec_notes
        d["rules"] = [r.to_json() for r in self.rules]
        d.update(self.extra)
        d["findings"] = [{**f.to_json(), "rule": rid} for rid, f in self.ordered()]
        d["not_checked"] = self.not_checked
        d["counts"] = self.counts
        d["elapsed_s"] = round(self.elapsed_s, 2)
        return d

    def lines(self) -> list[str]:
        out: list[str] = []
        for i in self.inputs:
            bits = [f"{i.get('part')}: {i.get('path')}"]
            if i.get("facets") is not None:
                bits.append(f"{i['facets']} facets")
            if i.get("fresh"):
                bits.append(f"fresh {i['fresh']}")
            if i.get("role"):
                bits.append(f"role {i['role']} ({i.get('role_from')})")
            out.append(", ".join(bits))
        notes = self.spec_notes or {}
        if notes.get("unknown_keys"):
            out.append("spec: unknown keys " + ", ".join(notes["unknown_keys"]))
        if notes.get("unread"):
            out.append("spec: not read by any rule: " + ", ".join(notes["unread"]))
        for r in self.rules:
            head = f"rule {r.id} ({r.type}): {r.status}"
            if r.reason:
                head += f" ({r.reason})"
            out.append(head)
            out += ["  " + line for line in r.text]
            out += ["  " + f.text() for f in sorted(r.findings, key=_key)]
        out += self.text
        out.append("not checked: " + "; ".join(f"{x['what']} ({x.get('use') or x.get('why')})" for x in self.not_checked))
        c = self.counts
        skipped = sum(1 for r in self.rules if r.status == NOT_CHECKED_STATUS)
        out.append(f"cad {self.verb}: {self.gate} ({c[FAIL]} FAIL, {c[WARN]} WARN, {c[INFO]} INFO; {skipped} rules not checked)")
        return out


@dataclass
class Subject:
    """One loaded part: its mesh in the file's frame (a 3MF object: plate-local mm), where it came from and its hash."""

    name: str
    path: str
    facets: list
    sha256: str
    kind: str = "stl"  # "stl" or "3mf"
    object: str | None = None
