"""Designation <-> pipe association.

Every association is backed by geometric evidence and every score is
decomposed, so a decision can be explained.  The rules the specification
forbids - nearest-only, longest-only, first-in-array, object id, arbitrary
tie-break - appear nowhere: when two runs are equally supported the result is
AMBIGUOUS and no quantity is produced from it.

Two mechanisms are used, in order:

1. **direct association** - a label is bound to a run by its leader line, or
   by proximity combined with orientation agreement and size consistency;
2. **propagation** - an unlabelled run inherits a neighbour's designation only
   across a connection whose drawn width matches, and only when exactly one
   designation reaches it first.  Two designations arriving equally far away
   leave the run AMBIGUOUS.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Mapping, Sequence

from ..canonical import canonical_sort, qs
from ..geometry.index import SpatialIndex
from ..geometry.primitives import (
    BBox,
    Segment,
    angle_diff,
    dist,
    point_segment_distance,
    segments_of_polyline,
)
from ..model import Designation, PipeRun
from ..states import IdentityState, Reason, TextRole
from ..topology.physical import RunAssignment

LEADER_HIT_FLOOR_PT = 4.0
LEADER_HIT_WIDTH_FACTOR = 0.75  # a leader points at the pipe *wall*, not its axis
LEADER_SUFFICIENT_SCORE = 0.50
NEUTRAL_EVIDENCE = 0.5
CONFLICTING_SIZE_SCORE = 0.2
PROXIMITY_RADIUS_CAP_FACTOR = 9.0
SCORE_TIE_EPSILON = 0.06
MIN_DIRECT_SCORE = 0.40
WIDTH_RELATIVE_TOLERANCE = 0.12
RUN_JOIN_TOLERANCE_PT = 1.5


@dataclass(frozen=True, slots=True)
class AssociationResult:
    assignments: dict[str, RunAssignment]
    designation_to_runs: dict[str, tuple[str, ...]]
    diagnostics: tuple[tuple[str, str], ...]


def _run_distance(run: PipeRun, point: tuple[float, float]) -> float:
    return min(
        (point_segment_distance(point, s) for s in segments_of_polyline(run.centerline)),
        default=math.inf,
    )


def _run_orientation(run: PipeRun, point: tuple[float, float]) -> float:
    best = math.inf
    angle = 0.0
    for s in segments_of_polyline(run.centerline):
        d = point_segment_distance(point, s)
        if d < best:
            best = d
            angle = s.angle
    return angle


def associate_designations(
    designations: Sequence[Designation],
    runs: Sequence[PipeRun],
    leaders: Mapping[str, tuple[Segment, ...]],
    diameter_of_run: Mapping[str, float | None],
    text_cap_height: float,
) -> AssociationResult:
    runs = canonical_sort(list(runs), key=lambda r: r.canonical_key())
    labels = [
        d
        for d in canonical_sort(list(designations), key=lambda d: d.canonical_key())
        if d.role is TextRole.PIPE_DESIGNATION and not d.is_legend
    ]
    index: SpatialIndex[int] = SpatialIndex.for_items(
        [(BBox.from_points(r.centerline), i) for i, r in enumerate(runs)]
    )
    radius = PROXIMITY_RADIUS_CAP_FACTOR * max(text_cap_height, 1.0)

    assignments: dict[str, RunAssignment] = {}
    designation_to_runs: dict[str, list[str]] = {}
    diagnostics: list[tuple[str, str]] = []

    for d in labels:
        anchor = d.bbox.center
        scored: list[tuple[float, int, tuple[tuple[str, float], ...]]] = []
        for ri in index.query_box(d.bbox.expanded(radius)):
            run = runs[ri]
            evidence: list[tuple[str, float]] = []
            proximity = _run_distance(run, anchor)
            if proximity > radius:
                continue
            prox_score = max(0.0, 1.0 - proximity / radius)
            evidence.append(("proximity", qs(prox_score)))

            # A leader is drawn to the pipe's *wall*, so the tolerance has to
            # grow with the drawn width; measuring to the axis and demanding a
            # fixed few points would reject every leader on a large pipe.
            tol = max(
                LEADER_HIT_FLOOR_PT,
                LEADER_HIT_WIDTH_FACTOR * (run.width_pt or 0.0) + 2.0,
            )
            leader_score = 0.0
            for seg in leaders.get(d.designation_id, ()):
                for tip in (seg.a, seg.b):
                    if d.bbox.expanded(text_cap_height).contains_point(tip):
                        continue
                    hit = _run_distance(run, tip)
                    if hit <= tol:
                        leader_score = max(leader_score, 1.0 - hit / tol)
            evidence.append(("leader", qs(leader_score)))

            # Orientation and size are *corroborating* evidence.  Where they
            # are unavailable they stay neutral rather than counting against an
            # association, so a label set perpendicular to its pipe - which is
            # ordinary practice - is not rejected for that reason alone.
            text_angle = math.radians(d.rotation)
            orient = max(
                0.0, 1.0 - angle_diff(text_angle, _run_orientation(run, anchor)) / (math.pi / 2)
            )
            orient_score = max(orient, NEUTRAL_EVIDENCE if leader_score > 0 else 0.0)
            evidence.append(("orientation", qs(orient_score)))

            size_score = NEUTRAL_EVIDENCE
            measured = diameter_of_run.get(run.pipe_run_id)
            if d.diameter_mm is not None and measured is not None:
                rel = abs(measured - d.diameter_mm) / max(d.diameter_mm, 1e-6)
                size_score = 1.0 if rel <= 0.15 else CONFLICTING_SIZE_SCORE
            evidence.append(("sizeConsistency", qs(size_score)))

            score = (
                0.50 * leader_score
                + 0.25 * prox_score
                + 0.10 * orient_score
                + 0.15 * size_score
            )
            if leader_score >= LEADER_SUFFICIENT_SCORE:
                # A leader that lands on the pipe is direct evidence; it stands
                # on its own even when the corroborating signals are silent.
                score = max(score, MIN_DIRECT_SCORE + 0.5 * leader_score)
            scored.append((score, ri, tuple(evidence)))

        scored.sort(key=lambda t: (-qs(t[0]), runs[t[1]].canonical_key()))
        if not scored or scored[0][0] < MIN_DIRECT_SCORE:
            diagnostics.append((d.designation_id, Reason.NO_ASSOCIATION_EVIDENCE.value))
            continue
        if len(scored) > 1 and scored[0][0] - scored[1][0] < SCORE_TIE_EPSILON:
            diagnostics.append((d.designation_id, Reason.COMPETING_PIPES.value))
            for score, ri, ev in scored[:2]:
                _record(
                    assignments,
                    runs[ri].pipe_run_id,
                    RunAssignment(
                        designation=None,
                        designation_ids=(d.designation_id,),
                        diameter_mm=diameter_of_run.get(runs[ri].pipe_run_id),
                        state=IdentityState.AMBIGUOUS,
                        reasons=(Reason.AMBIGUOUS_ASSOCIATION, Reason.COMPETING_PIPES),
                        association_confidence=qs(score),
                        evidence=ev,
                    ),
                )
            continue

        score, ri, ev = scored[0]
        run = runs[ri]
        _record(
            assignments,
            run.pipe_run_id,
            RunAssignment(
                designation=d.text,
                designation_ids=(d.designation_id,),
                diameter_mm=diameter_of_run.get(run.pipe_run_id),
                state=IdentityState.CONFIRMED if score >= 0.75 else IdentityState.HIGH_CONFIDENCE,
                reasons=(),
                association_confidence=qs(score),
                evidence=ev,
            ),
        )
        designation_to_runs.setdefault(d.designation_id, []).append(run.pipe_run_id)

    _propagate(runs, assignments, diameter_of_run, diagnostics)

    return AssociationResult(
        assignments=assignments,
        designation_to_runs={k: tuple(sorted(v)) for k, v in sorted(designation_to_runs.items())},
        diagnostics=tuple(sorted(set(diagnostics))),
    )


def _record(store: dict[str, RunAssignment], run_id: str, value: RunAssignment) -> None:
    existing = store.get(run_id)
    if existing is None:
        store[run_id] = value
        return
    if existing.designation and value.designation and existing.designation != value.designation:
        store[run_id] = RunAssignment(
            designation=None,
            designation_ids=tuple(sorted(set(existing.designation_ids + value.designation_ids))),
            diameter_mm=existing.diameter_mm,
            state=IdentityState.AMBIGUOUS,
            reasons=(Reason.AMBIGUOUS_ASSOCIATION, Reason.COMPETING_PIPES),
            association_confidence=qs(min(existing.association_confidence, value.association_confidence)),
            evidence=existing.evidence,
        )
        return
    if value.association_confidence > existing.association_confidence:
        store[run_id] = value


def _width_compatible(a: PipeRun, b: PipeRun) -> bool:
    if a.width_pt is None or b.width_pt is None:
        return False
    m = max(a.width_pt, b.width_pt, 1e-9)
    return abs(a.width_pt - b.width_pt) / m <= WIDTH_RELATIVE_TOLERANCE


def _propagate(
    runs: Sequence[PipeRun],
    assignments: dict[str, RunAssignment],
    diameter_of_run: Mapping[str, float | None],
    diagnostics: list[tuple[str, str]],
) -> None:
    """Spread a designation along width-compatible connections.

    A breadth-first sweep from every labelled run at once.  A run is claimed by
    the designation that reaches it in the fewest hops; if two designations
    reach it at the same depth the run is marked AMBIGUOUS rather than being
    awarded to either.
    """
    adjacency: dict[str, list[str]] = {r.pipe_run_id: [] for r in runs}
    for i in range(len(runs)):
        for j in range(i + 1, len(runs)):
            a, b = runs[i], runs[j]
            if not _width_compatible(a, b):
                continue
            if any(
                dist(pa, pb) <= RUN_JOIN_TOLERANCE_PT
                for pa in (a.centerline[0], a.centerline[-1])
                for pb in (b.centerline[0], b.centerline[-1])
            ):
                adjacency[a.pipe_run_id].append(b.pipe_run_id)
                adjacency[b.pipe_run_id].append(a.pipe_run_id)

    by_id = {r.pipe_run_id: r for r in runs}
    depth: dict[str, int] = {}
    claim: dict[str, set[str]] = {}
    queue: deque[tuple[str, str, int]] = deque()
    for rid in sorted(assignments):
        a = assignments[rid]
        if a.designation:
            depth[rid] = 0
            claim[rid] = {a.designation}
            queue.append((rid, a.designation, 0))

    while queue:
        rid, label, d = queue.popleft()
        for nxt in sorted(adjacency.get(rid, ())):
            if nxt in assignments and assignments[nxt].designation:
                continue
            nd = d + 1
            if nxt not in depth or nd < depth[nxt]:
                depth[nxt] = nd
                claim[nxt] = {label}
                queue.append((nxt, label, nd))
            elif depth[nxt] == nd:
                claim[nxt].add(label)

    for rid in sorted(claim):
        if depth.get(rid, 0) == 0:
            continue
        labels = sorted(claim[rid])
        run = by_id[rid]
        if len(labels) == 1:
            assignments[rid] = RunAssignment(
                designation=labels[0],
                designation_ids=(),
                diameter_mm=diameter_of_run.get(rid),
                state=IdentityState.HIGH_CONFIDENCE,
                reasons=(),
                association_confidence=qs(max(0.35, 0.85 - 0.15 * depth[rid])),
                evidence=(("propagatedHops", float(depth[rid])),),
            )
        else:
            diagnostics.append((rid, Reason.COMPETING_PIPES.value))
            assignments[rid] = RunAssignment(
                designation=None,
                designation_ids=(),
                diameter_mm=diameter_of_run.get(rid),
                state=IdentityState.AMBIGUOUS,
                reasons=(Reason.AMBIGUOUS_ASSOCIATION, Reason.COMPETING_PIPES),
                association_confidence=0.0,
                evidence=(("competingLabels", float(len(labels))),),
            )
