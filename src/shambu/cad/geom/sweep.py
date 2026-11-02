"""`shambu cad sweep`: move the moving parts along one translation (an insertion path) or one rotation (a hinge) and
find the first contact with the fixed parts (the `geom` extra).

Travel is measured from the start: millimetres along the insertion axis (the moving parts start `from_mm` back from
their seated placement and end seated) or degrees about the axis (the placement is the part at 0 degrees). At every
step: the minimum distance both ways (samples of the moving parts against the fixed meshes, and samples of the fixed
parts against the moving meshes, in the moving frame, for each part's vertices and points 1 mm apart along its edges;
then every edge against every edge, so the distance is exact), the Boolean overlap when the boxes meet, and a crossing test
(below). The first step that meets the margin, overlaps above the floor or crosses a surface is refined by bisection
between the last clear travel and it, to the rule's resolution. Points inside the rule's `contact_zone` do not count
as contact (a seat), and neither does an overlap whose bounds lie in the zone.

Culling, exact by construction: a point at radius rho from the rotation axis moves at most 2 rho sin(step / 2) in one
step (a translation: the step itself). Each point keeps a lower bound on its distance (its last exact distance minus
the chords since); a point whose bound is still above the margin cannot meet the margin in this step and is not
evaluated. A point can only cross a surface during a step if its distance at the start was at most its chord, so the
crossing test runs on the evaluated points only and misses nothing the full test would find. `--no-cull` evaluates
every point at every step: the slow reference path, which gives the same first contact, partner and step count (a
property test checks it).

The crossing test follows each evaluated point along the straight segment between two steps. A rotating point moves
on an arc, which bulges past that segment by rho (1 - cos(step / 2)); a fixed part thinner than that bulge, met only
by the arc, can be missed. Bisection runs every point without culling.

What it does not do: no two-axis motions, no per-step scripts, no file reading, no printing.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from ...errors import GeomError
from ...findings import Finding
from ..judge import Contact, SweepRun, overlap_floor, sweep_verdict
from ..spec import SweepRuleSpec, Zone
from .clearance import Placed
from .engines import EdgeSet, ManifoldBoolean, PrunedDistance, edge_samples, edges

LOOKAHEAD_STEPS = 3  # each exact distance is kept up to margin + 3 chords, so a far point is re-checked every few steps
CROSS_SPACING_MM = 1.0  # query points along each edge for the crossing test (the distance itself is exact)


def _p(v) -> tuple[float, float, float]:
    return (float(v[0]), float(v[1]), float(v[2]))


@dataclass(frozen=True)
class Motion:
    kind: str
    axis: np.ndarray
    point: np.ndarray
    start: float
    end: float

    @property
    def total(self) -> float:
        return self.start if self.kind == "translate" else abs(self.end - self.start)

    def pose(self, tau: float) -> tuple[np.ndarray, np.ndarray]:
        """(R, t): placed moving coordinates x go to R x + t at travel tau."""
        if self.kind == "translate":
            return np.eye(3), -self.axis * (self.start - tau)
        sign = 1.0 if self.end >= self.start else -1.0
        ang = math.radians(self.start + sign * tau)
        x, y, z = self.axis
        c, s = math.cos(ang), math.sin(ang)
        k = 1 - c
        r = np.array(
            [
                [c + x * x * k, x * y * k - z * s, x * z * k + y * s],
                [y * x * k + z * s, c + y * y * k, y * z * k - x * s],
                [z * x * k - y * s, z * y * k + x * s, c + z * z * k],
            ]
        )
        return r, self.point - r @ self.point

    def angle(self, tau: float) -> float:
        return self.start + (1.0 if self.end >= self.start else -1.0) * tau

    def radius(self, pts: np.ndarray) -> np.ndarray:
        if self.kind == "translate":
            return np.zeros(len(pts))
        d = pts - self.point
        along = d @ self.axis
        return np.linalg.norm(d - np.outer(along, self.axis), axis=1)

    def chord(self, radius: np.ndarray, dtau: float) -> np.ndarray:
        if self.kind == "translate":
            return np.full(len(radius), abs(dtau))
        return 2 * radius * math.sin(math.radians(abs(dtau)) / 2)


@dataclass
class Channel:
    """Query points that move against one target mesh: moving samples against a fixed mesh (`moves` True: the points
    are transformed), or fixed samples against a moving mesh (the points go the other way, into the moving frame)."""

    owner: str  # the part the points belong to
    partner: str  # the part of the target mesh
    moving_owner: str  # the moving part of the pair (the finding's object)
    moves: bool
    points: np.ndarray
    target: PrunedDistance
    radius: np.ndarray
    known: np.ndarray = field(default_factory=lambda: np.zeros(0))

    @property
    def fixed_part(self) -> str:
        return self.partner if self.moves else self.owner

    def query(self, r: np.ndarray, t: np.ndarray) -> np.ndarray:
        if self.moves:
            return self.points @ r.T + t
        return (self.points - t) @ r


class SweepRunner:
    def __init__(self, rule: SweepRuleSpec, moving: Sequence[Placed], fixed: Sequence[Placed], zones: dict[str, Zone], cull: bool = True):
        self.rule = rule
        self.moving = list(moving)
        self.fixed = list(fixed)
        self.zone = zones.get(rule.contact_zone) if rule.contact_zone else None
        self.cull = cull
        self.motion = Motion(
            rule.motion, np.asarray(rule.axis, dtype=np.float64), np.asarray(rule.point, dtype=np.float64), rule.start, rule.end
        )
        self.spacing = CROSS_SPACING_MM
        self.engine = ManifoldBoolean()
        self.channels: list[Channel] = []
        self.edge_sets: dict[str, EdgeSet] = {}

    def _build(self) -> None:
        dist = {p.part.name: PrunedDistance.build(p.mesh) for p in [*self.moving, *self.fixed]}
        pts = {p.part.name: edge_samples(p.mesh, self.spacing) for p in [*self.moving, *self.fixed]}
        self.edge_sets = {f.part.name: EdgeSet.build(f.mesh) for f in self.fixed}
        for m in self.moving:
            for f in self.fixed:
                pts_m = pts[m.part.name]
                pts_f = pts[f.part.name]
                self.channels.append(
                    Channel(m.part.name, f.part.name, m.part.name, True, pts_m, dist[f.part.name], self.motion.radius(pts_m))
                )
                self.channels.append(
                    Channel(f.part.name, m.part.name, m.part.name, False, pts_f, dist[m.part.name], self.motion.radius(pts_f))
                )

    def _in_zone(self, world: np.ndarray) -> np.ndarray:
        if self.zone is None:
            return np.zeros(len(world), dtype=bool)
        return np.array([self.zone.contains(p) for p in world], dtype=bool)

    def _overlap(self, tau: float) -> Contact | None:
        r, t = self.motion.pose(tau)
        best: Contact | None = None
        for m in self.moving:
            mm = m.mesh.moved(r, t)
            for f in self.fixed:
                if np.any(mm.lo > f.mesh.hi + self.rule.margin_mm) or np.any(f.mesh.lo > mm.hi + self.rule.margin_mm):
                    continue
                try:
                    ov = self.engine.overlap(mm, f.mesh)
                except GeomError as e:
                    raise GeomError(f"at {tau:.3f} {self.rule.unit}: {e}") from e
                if ov.volume_mm3 is None or ov.volume_mm3 <= overlap_floor(m.mesh.volume(), f.mesh.volume()) or ov.bounds is None:
                    continue
                if self.zone is not None and self.zone.contains_box(ov.bounds[0], ov.bounds[1]):
                    continue
                c = _p([(ov.bounds[0][i] + ov.bounds[1][i]) / 2 for i in range(3)])
                if best is None or ov.volume_mm3 > (best.volume_mm3 or 0):
                    best = Contact(tau, "overlap", f.part.name, m.part.name, c, None, ov.volume_mm3)
        return best

    def _distance(self, tau: float, prev: float | None, run: SweepRun, full: bool) -> Contact | None:
        """Distance and crossing at `tau`. With `full`, every point; else the culled set. Updates the lower bounds."""
        r, t = self.motion.pose(tau)
        r0, t0 = self.motion.pose(prev) if prev is not None else (r, t)
        margin = self.rule.margin_mm
        best: Contact | None = None
        for ch in self.channels:
            chord = self.motion.chord(ch.radius, tau - prev) if prev is not None else np.zeros(len(ch.points))
            if not len(ch.known):
                ch.known = np.zeros(len(ch.points))
            ch.known = ch.known - chord
            sel = np.arange(len(ch.points)) if (full or not self.cull) else np.nonzero(ch.known <= margin)[0]
            run.evaluated_points += len(sel)
            run.culled_points += len(ch.points) - len(sel)
            if not len(sel):
                continue
            q_now = ch.query(r, t)[sel]
            upper = margin + LOOKAHEAD_STEPS * float(self.motion.chord(ch.radius[sel], self.rule.step).max(initial=0.0))
            res = ch.target.distances(q_now, upper)
            ch.known[sel] = np.where(np.isfinite(res.d), res.d, upper)
            world = q_now if ch.moves else ch.points[sel]
            zone = self._in_zone(world)
            near = np.nonzero((res.d < margin) & ~zone)[0]
            if len(near):
                i = near[np.argmin(res.d[near])]
                if best is None or best.kind != "distance" or res.d[i] < (best.distance_mm or 0):
                    best = Contact(tau, "distance", ch.fixed_part, ch.moving_owner, _p(world[i]), float(res.d[i]))
            zhit = np.nonzero((res.d < margin) & zone)[0]
            if len(zhit) and run.zone_contact is None:
                i = zhit[0]
                run.zone_contact = Contact(tau, "distance", ch.fixed_part, ch.moving_owner, _p(world[i]), float(res.d[i]))
            if prev is not None and best is None:
                q_prev = ch.query(r0, t0)[sel]
                crossed = ch.target.crosses(q_prev, q_now) & ~zone
                if crossed.any():
                    i = int(np.nonzero(crossed)[0][0])
                    best = Contact(tau, "crossing", ch.fixed_part, ch.moving_owner, _p(world[i]))
        if best is None and margin > 0:
            best = self._edges(tau, r, t)
        return best

    def _edges(self, tau: float, r: np.ndarray, t: np.ndarray) -> Contact | None:
        """Edge against edge, every edge at every step (never culled): with the vertex distances this makes the
        distance exact, a contact between two edges included."""
        for m in self.moving:
            e = edges(m.mesh)
            p2, q2 = m.mesh.V[e[:, 0]] @ r.T + t, m.mesh.V[e[:, 1]] @ r.T + t
            for f in self.fixed:
                hit = self.edge_sets[f.part.name].closest(p2, q2, self.rule.margin_mm)
                if hit is None or hit[0] >= self.rule.margin_mm:
                    continue
                mid = (hit[1] + hit[2]) / 2
                if self.zone is not None and self.zone.contains(mid):
                    continue
                return Contact(tau, "distance", f.part.name, m.part.name, (float(mid[0]), float(mid[1]), float(mid[2])), hit[0])
        return None

    def _hit(self, tau: float, prev: float | None, run: SweepRun, full: bool) -> Contact | None:
        d = self._distance(tau, prev, run, full)
        return d if d is not None else self._overlap(tau)

    def run(self) -> SweepRun:
        self._build()
        run = SweepRun()
        total = self.motion.total
        n = max(1, math.ceil(total / self.rule.step - 1e-9))
        taus = [min(total, k * self.rule.step) for k in range(n + 1)]
        prev: float | None = None
        hit_at: Contact | None = None
        for tau in taus:
            run.steps += 1
            hit_at = self._hit(tau, prev, run, False)
            if hit_at is not None:
                break
            prev = tau
        if hit_at is not None:
            lo = prev if prev is not None else 0.0
            hi = hit_at.tau
            best = hit_at
            if prev is not None:
                while hi - lo > self.rule.resolution:
                    mid = (lo + hi) / 2
                    for ch in self.channels:
                        ch.known = np.zeros(len(ch.points))
                    c = self._hit(mid, lo, run, True)
                    if c is not None:
                        hi, best = mid, c
                    else:
                        lo = mid
            best.tau = hi
            run.first = best
        end_ov = self._overlap(total)
        run.end_overlap = end_ov
        return run


def judge(rule: SweepRuleSpec, run: SweepRun, motion: Motion) -> tuple[dict, list[Finding]]:
    return sweep_verdict(rule, run, motion.total, motion.angle)
