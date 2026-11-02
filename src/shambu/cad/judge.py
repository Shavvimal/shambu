"""The verdicts of the clearance and sweep gates, in pure Python: the geometry modules (`cad/geom`, the `geom` extra)
measure, and these functions turn the numbers into JSON rows and findings. Keeping the judgement here lets the base
test suite check every exemption, cap, margin and travel rule with plain numbers, without numpy.

Clearance rules: an overlap above the floor is `clash.overlap` (FAIL) unless an exemption covers the pair (and its
zone holds the overlap): within its cap `clash.exempt` (INFO), over it `clash.over_cap` (FAIL). A sampled pair (no
volume) under a cap is `cad.incomplete.no_volume`. A failed Boolean is `clash.boolean_failed` (FAIL) and is never
exempt. With no overlap, a distance under the margin is `clash.margin` (FAIL) unless an exemption covers the closest
point.

Sweep rules: contact before `first_contact_after` is `sweep.early_contact` (FAIL), after it `sweep.first_contact`
(INFO); with no such bound, contact before the end of travel is `sweep.margin` or `sweep.overlap` (FAIL) and contact
at the end is `sweep.first_contact` (INFO); an end-of-travel overlap over `max_overlap_mm3` is `sweep.overlap`; no
contact at all is `sweep.no_contact` (INFO).

What it does not do: no geometry, no file reading, no printing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..findings import FAIL, INFO, MEASURED, Finding, Fix, Location
from .spec import ClearanceRuleSpec, PartSpec, SweepRuleSpec, Zone

Vec3 = tuple[float, float, float]
FLOOR_MM3 = 1e-3
FLOOR_SHARE = 1e-6
MIN_REPORT_MM = 1.0


def overlap_floor(volume_a: float, volume_b: float) -> float:
    """max(1e-3 mm3, 1e-6 x the smaller volume). Two parts that share a face give a Boolean volume of float noise, not
    zero; 1e-6 of a part's volume is far above that noise and far below any real interference."""
    return max(FLOOR_MM3, FLOOR_SHARE * min(abs(volume_a), abs(volume_b)))


def sample_spacing(margin_mm: float) -> float:
    """Surface samples for the sampled containment test: a quarter of the margin apart, 0.05 to 1 mm."""
    return min(1.0, max(0.05, margin_mm / 4 if margin_mm > 0 else 0.25))


def report_bound(margin_mm: float) -> float:
    """Distances are reported up to max(2 x margin, 1 mm); a pair beyond that is clear by a factor of two."""
    return max(2 * margin_mm, MIN_REPORT_MM)


@dataclass(frozen=True)
class Overlap:
    method: str  # "boolean" or "sampled"
    volume_mm3: float | None
    bounds: tuple[Vec3, Vec3] | None
    inside_points: tuple[Vec3, ...] = ()


def _centre(b: tuple[Vec3, Vec3]) -> Vec3:
    return ((b[0][0] + b[1][0]) / 2, (b[0][1] + b[1][1]) / 2, (b[0][2] + b[1][2]) / 2)


def _vol(v: float | None) -> str:
    return "an unknown volume (sampled: the pair is not closed)" if v is None else f"{v:.3f} mm3"


def clearance_verdict(
    rule: ClearanceRuleSpec,
    a: PartSpec,
    b: PartSpec,
    zones: dict[str, Zone],
    overlap: Overlap | None = None,
    floor_mm3: float = FLOOR_MM3,
    error: str | None = None,
    distance: tuple[float, Vec3, Vec3] | None = None,
    sampling_mm: float | None = None,
    beyond_mm: float | None = None,
    outside: Callable[[Zone], tuple[float, Vec3, Vec3, float] | None] | None = None,
) -> tuple[dict, list[Finding]]:
    """The JSON row and findings of one pair from its measurements: a Boolean error, or an overlap, or (with no overlap)
    the closest pair of points at `distance` (None: beyond `beyond_mm`). A zone exemption covers a distance under the
    margin only when `outside(zone)` (the closest pair under the margin whose midpoint lies outside the zone, and the
    sample spacing it was found at) finds none: one exempt closest point must not exempt the rest of the pair."""
    an, bn = a.name, b.name
    row: dict = {"a": an, "b": bn, "overlap": None, "distance_mm": None}
    fs: list[Finding] = []
    exempt = [e for e in rule.exempt if e.covers(a, b)]
    if error is not None:
        row["error"] = error
        fs.append(
            Finding(
                "clash.boolean_failed",
                FAIL,
                rule.id,
                f"{an} x {bn}: {error}",
                MEASURED,
                an,
                measure={"partner": bn},
                fix=Fix("re-export both parts as closed solids and re-run; a failed Boolean is never exempt"),
            )
        )
        return row, fs
    if overlap is not None:
        ov = overlap
        row["overlap"] = {
            "method": ov.method,
            "volume_mm3": ov.volume_mm3,
            "bounds": [list(ov.bounds[0]), list(ov.bounds[1])] if ov.bounds else None,
            "floor_mm3": floor_mm3,
        }
        if ov.method == "sampled":
            row["overlap"]["inside_points"] = len(ov.inside_points)
        overlapping = (ov.volume_mm3 is not None and ov.volume_mm3 > floor_mm3) or (ov.volume_mm3 is None and bool(ov.inside_points))
        if overlapping:
            row["distance_mm"] = 0.0
            where = Location("shared", _centre(ov.bounds), None, ov.bounds) if ov.bounds else None
            hit = next(
                (e for e in exempt if e.zone is None or (ov.bounds is not None and zones[e.zone].contains_box(ov.bounds[0], ov.bounds[1]))),
                None,
            )
            if hit is None:
                fs.append(
                    Finding(
                        "clash.overlap",
                        FAIL,
                        rule.id,
                        f"{an} and {bn} overlap by {_vol(ov.volume_mm3)}",
                        MEASURED,
                        an,
                        where,
                        {"partner": bn, "volume_mm3": ov.volume_mm3, "floor_mm3": floor_mm3, "method": ov.method},
                        Fix("remove the interference in CAD, or add an exemption with a reason and a cap"),
                    )
                )
                return row, fs
            row["exemption"] = {"reason": hit.reason, "cap_mm3": hit.max_overlap_mm3, "zone": hit.zone}
            if hit.max_overlap_mm3 is not None and ov.volume_mm3 is None:
                fs.append(
                    Finding(
                        "cad.incomplete.no_volume",
                        FAIL,
                        "inputs",
                        f"{an} x {bn}: the pair is not closed, so the sampled check has no volume to hold against the {hit.max_overlap_mm3:g} mm3 cap",
                        MEASURED,
                        an,
                        where,
                        {"partner": bn, "inside_points": len(ov.inside_points)},
                        Fix("re-export a closed solid"),
                    )
                )
            elif hit.max_overlap_mm3 is not None and ov.volume_mm3 is not None and ov.volume_mm3 > hit.max_overlap_mm3:
                fs.append(
                    Finding(
                        "clash.over_cap",
                        FAIL,
                        rule.id,
                        f"{an} x {bn} overlap {ov.volume_mm3:.3f} mm3, over the {hit.max_overlap_mm3:g} mm3 cap of the exemption ({hit.reason})",
                        MEASURED,
                        an,
                        where,
                        {"partner": bn, "volume_mm3": ov.volume_mm3, "cap_mm3": hit.max_overlap_mm3, "expected_from": "spec"},
                        Fix("reduce the interference in CAD, or raise the cap with a reason"),
                    )
                )
            else:
                fs.append(
                    Finding(
                        "clash.exempt",
                        INFO,
                        rule.id,
                        f"{an} x {bn} overlap {_vol(ov.volume_mm3)}, exempt: {hit.reason}",
                        MEASURED,
                        an,
                        where,
                        {"partner": bn, "volume_mm3": ov.volume_mm3, "cap_mm3": hit.max_overlap_mm3},
                    )
                )
            return row, fs
    if sampling_mm is not None:
        row["sampling_mm"] = round(sampling_mm, 4)
    if distance is None:
        row["beyond_mm"] = beyond_mm
        return row, fs
    d, p, q = distance
    row["distance_mm"] = d
    row["closest"] = [list(p), list(q)]
    if d < rule.margin_mm:
        mid = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2, (p[2] + q[2]) / 2)
        hit = next((e for e in exempt if e.zone is None or zones[e.zone].contains(mid)), None)
        where = Location("shared", mid)
        if hit is not None and hit.zone is not None and outside is not None:
            out = outside(zones[hit.zone])
            if out is not None:
                d, p, q, spacing = out
                mid = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2, (p[2] + q[2]) / 2)
                where = Location("shared", mid)
                row["outside_zone"] = {"zone": hit.zone, "distance_mm": d, "closest": [list(p), list(q)], "sampling_mm": spacing}
                hit = None
                sampling_mm = spacing
        if hit is not None:
            row["exemption"] = {"reason": hit.reason, "cap_mm3": hit.max_overlap_mm3, "zone": hit.zone}
            fs.append(
                Finding(
                    "clash.exempt",
                    INFO,
                    rule.id,
                    f"{an} x {bn} {d:.3f} mm apart, exempt: {hit.reason}",
                    MEASURED,
                    an,
                    where,
                    {"partner": bn, "distance_mm": d},
                )
            )
        else:
            fs.append(
                Finding(
                    "clash.margin",
                    FAIL,
                    rule.id,
                    f"{an} and {bn} are {d:.3f} mm apart, under the {rule.margin_mm:g} mm margin"
                    + (f" (outside exempt zone {row['outside_zone']['zone']})" if "outside_zone" in row else ""),
                    MEASURED,
                    an,
                    where,
                    {"partner": bn, "distance_mm": d, "margin_mm": rule.margin_mm, "sampling_mm": sampling_mm, "expected_from": "spec"},
                    Fix("open the gap in CAD"),
                )
            )
    return row, fs


# ---------------------------------------------------------------------------- sweeps
@dataclass
class Contact:
    tau: float  # travel from the start (mm or deg)
    kind: str  # "distance", "overlap", "crossing" or "bound" (an interval the gap bound cannot prove clear)
    partner: str
    part: str
    point: Vec3
    distance_mm: float | None = None
    volume_mm3: float | None = None


@dataclass
class SweepRun:
    steps: int = 0
    evaluated_points: int = 0
    culled_points: int = 0
    first: Contact | None = None
    zone_contact: Contact | None = None
    end_overlap: Contact | None = None
    sampling_mm: float = 0.0  # how far high a distance can read; 0 with the exact edge pass
    speed: float = 0.0  # the most the gap changes per unit of travel (mm per mm or per degree): the proof's bound


def sweep_verdict(rule: SweepRuleSpec, run: SweepRun, total: float, angle_of: Callable[[float], float]) -> tuple[dict, list[Finding]]:
    """The rule's JSON block and findings. `total` is the travel; `angle_of(travel)` the absolute angle (rotations)."""
    unit = rule.unit

    def where_txt(c: Contact) -> str:
        return f"{angle_of(c.tau):.2f} deg" if unit == "deg" else f"{c.tau:.3f} mm of travel"

    def at_json(c: Contact) -> dict:
        d: dict = {
            "partner": c.partner,
            "part": c.part,
            "kind": c.kind,
            "travel_" + unit: round(c.tau, 4),
            "point": [round(x, 4) for x in c.point],
        }
        if unit == "deg":
            d["at_deg"] = round(angle_of(c.tau), 4)
        if c.distance_mm is not None:
            d["distance_mm"] = c.distance_mm
        if c.volume_mm3 is not None:
            d["volume_mm3"] = c.volume_mm3
        return d

    block: dict = {
        "motion": rule.motion,
        "steps": run.steps,
        "evaluated_points": run.evaluated_points,
        "culled_points": run.culled_points,
        "first_contact": at_json(run.first) if run.first else None,
        "zone_contact": {**at_json(run.zone_contact), "zone": rule.contact_zone} if run.zone_contact else None,
        "end_overlap": at_json(run.end_overlap) if run.end_overlap else None,
        "resolution_" + unit: rule.resolution,
        "margin_mm": rule.margin_mm,
        "sampling_mm": run.sampling_mm,
        "gap_speed_mm_per_" + unit: round(run.speed, 6),
    }
    fs: list[Finding] = []
    if run.first is not None:
        c = run.first
        loc = Location("shared", c.point)
        meas: dict = {"travel_" + unit: c.tau, "resolution_" + unit: rule.resolution, "partner": c.partner, "expected_from": "spec"}
        allowed = rule.first_contact_after
        at_end = c.tau >= total - rule.resolution
        if allowed is not None:
            allowed_travel = abs(allowed - rule.start) if unit == "deg" else allowed
            if c.tau < allowed_travel:
                fs.append(
                    Finding(
                        "sweep.early_contact",
                        FAIL,
                        rule.id,
                        f"{c.part} meets {c.partner} at {where_txt(c)}, before the {allowed:g} {unit} the spec allows",
                        MEASURED,
                        c.part,
                        loc,
                        {**meas, "allowed_after_" + unit: allowed},
                        Fix(
                            "add clearance where the parts meet or move the stop; re-export and re-run",
                            command=f"shambu cad gate SPEC --only {rule.id}",
                        ),
                    )
                )
            else:
                fs.append(
                    Finding(
                        "sweep.first_contact",
                        INFO,
                        rule.id,
                        f"{c.part} first meets {c.partner} at {where_txt(c)} ({c.kind})",
                        MEASURED,
                        c.part,
                        loc,
                        meas,
                    )
                )
        elif at_end and (c.kind != "overlap" or rule.max_overlap_mm3 is not None):
            # contact at the end of travel: a stop, or an overlap the end-of-travel cap judges below
            fs.append(
                Finding(
                    "sweep.first_contact",
                    INFO,
                    rule.id,
                    f"{c.part} meets {c.partner} at the end of travel ({c.kind})",
                    MEASURED,
                    c.part,
                    loc,
                    meas,
                )
            )
        else:
            code = "sweep.overlap" if c.kind == "overlap" else "sweep.margin"
            if c.kind == "overlap":
                what = f"overlaps {c.partner} by {c.volume_mm3 or 0:.3f} mm3"
            elif c.kind == "distance":
                what = f"comes within {c.distance_mm or 0:.3f} mm of {c.partner} (margin {rule.margin_mm:g})"
            elif c.kind == "bound":
                what = (
                    f"cannot be proven clear of {c.partner} between two poses: the gap may fall to {c.distance_mm or 0:.4f} mm"
                    f" (margin {rule.margin_mm:g})"
                )
            else:
                what = f"passes through {c.partner} between two steps"
            fs.append(
                Finding(
                    code,
                    FAIL,
                    rule.id,
                    f"{c.part} {what} at {where_txt(c)}",
                    MEASURED,
                    c.part,
                    loc,
                    {**meas, "distance_mm": c.distance_mm, "volume_mm3": c.volume_mm3, "margin_mm": rule.margin_mm},
                    Fix("open the path in CAD (chamfer the lead-in, add clearance) and re-run"),
                )
            )
    else:
        fs.append(
            Finding(
                "sweep.no_contact",
                INFO,
                rule.id,
                "the moving parts met nothing over the whole travel (a seat with no stop is worth knowing)",
                MEASURED,
            )
        )
    if run.zone_contact is not None:
        z = run.zone_contact
        fs.append(
            Finding(
                "sweep.first_contact",
                INFO,
                rule.id,
                f"{z.part} meets {z.partner} in zone {rule.contact_zone} at {where_txt(z)}",
                MEASURED,
                z.part,
                Location("shared", z.point),
                {"zone": rule.contact_zone},
            )
        )
    if rule.contact_zone is not None:
        fs.append(
            Finding(
                "sweep.zone_sampled",
                INFO,
                rule.id,
                f"zone {rule.contact_zone} exempts geometry by where it sits at the poses evaluated; between two poses the "
                "clearance proof trusts those exemptions",
                MEASURED,
                measure={"zone": rule.contact_zone, "resolution_" + unit: rule.resolution},
            )
        )
    e = run.end_overlap
    cap = rule.max_overlap_mm3
    if e is not None and e.volume_mm3 is not None and cap is not None and e.volume_mm3 > cap:
        fs.append(
            Finding(
                "sweep.overlap",
                FAIL,
                rule.id,
                f"{e.part} overlaps {e.partner} by {e.volume_mm3:.3f} mm3 at the end of travel, over the {cap:g} mm3 cap",
                MEASURED,
                e.part,
                Location("shared", e.point),
                {"volume_mm3": e.volume_mm3, "cap_mm3": cap, "expected_from": "spec"},
                Fix("reduce the interference of the snap or press in CAD"),
            )
        )
    return block, fs


def pairs_of(rule: ClearanceRuleSpec, parts: Sequence[PartSpec]) -> list[tuple[PartSpec, PartSpec]]:
    """Unordered pairs from the rule's two selectors, a part never against itself, groups skipped when asked."""
    a_side = [p for p in parts if rule.a.match(p)]
    b_side = [p for p in parts if rule.b.match(p)]
    seen: set[tuple[str, str]] = set()
    out = []
    for pa in a_side:
        for pb in b_side:
            if pa.name == pb.name:
                continue
            key = (min(pa.name, pb.name), max(pa.name, pb.name))
            if key in seen or (rule.skip_same_group and pa.group and pa.group == pb.group):
                continue
            seen.add(key)
            out.append((pa, pb))
    return out
