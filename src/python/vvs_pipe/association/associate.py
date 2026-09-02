"""Designation <-> pipe association, over the drawing's own evidence chain.

    vector glyphs -> a complete designation (with its DN)
                  -> the vector leader the draughtsman drew
                  -> that leader's endpoint
                  -> geometry on a pipe-carrying layer
                  -> the run, and so the physical pipe

Only that chain binds a label to a pipe.  This module used to offer a second
route - an "inline" argument that bound a label to a run because it sat close to
it, with orientation and size as corroboration - and that route is what turned
dates, `ENL. PM-1`, drawing cross-references and title-block strings into pipe
designations: all of them sit close to linework on a plan sheet, so closeness
identifies nothing.  Proximity is still *measured*, because knowing how many
labels would have been bound by it is worth reporting, but it can no longer
produce an association.

What remains besides the chain is propagation: an unlabelled run inherits a
neighbour's designation across a connection whose drawn width matches, seeded
only from chain-verified assignments, and only when exactly one designation
reaches it first.  That is topology, not proximity - the two runs are the same
pipe - and a propagated run is never CONFIRMED.
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
from .attachment import AttachmentFailure, FeAttachment
from .leaders import VectorLeader

NEUTRAL_EVIDENCE = 0.5
CONFLICTING_SIZE_SCORE = 0.2
# How far a run's measured width may sit from the size its label states before
# the two are treated as describing different pipes.  Generous, because a drawn
# width is measured through the scale and a nominal size is not an outside
# diameter; anything past it is a disagreement no tolerance explains.
SIZE_CONFLICT_RELATIVE = 0.30
# Radius, in cap heights, within which a label is *reported* as having a
# proximity hint.  Nothing is bound by it.
PROXIMITY_RADIUS_CAP_FACTOR = 9.0
WIDTH_RELATIVE_TOLERANCE = 0.12
RUN_JOIN_TOLERANCE_PT = 1.5


@dataclass(frozen=True, slots=True)
class ChainCounts:
    """The stage-by-stage census the acceptance run compares against."""

    designation_occurrences: int
    designations_with_dn: int
    vector_leaders: int
    verified_attachments: int
    confirmed_designations: int
    designated_runs: int

    def to_canonical(self) -> dict:
        return {
            "designationOccurrences": self.designation_occurrences,
            "designationsWithDn": self.designations_with_dn,
            "vectorLeaders": self.vector_leaders,
            "verifiedAttachments": self.verified_attachments,
            "confirmedDesignations": self.confirmed_designations,
            "designatedRuns": self.designated_runs,
        }


@dataclass(frozen=True, slots=True)
class ChainLink:
    """One complete evidence chain, from glyphs to a physical pipe."""

    designation_id: str
    text_id: str
    designation: str
    diameter_mm: float | None
    glyph_ids: tuple[str, ...]
    leader_id: str
    leader_object_ids: tuple[str, ...]
    leader_tip: tuple[float, float]
    fe_object_id: str
    fe_layer: str | None
    pipe_run_id: str
    physical_pipe_id: str | None = None

    def canonical_key(self) -> tuple:
        return (self.designation, self.text_id, self.pipe_run_id)

    def to_canonical(self) -> dict:
        return {
            "designationId": self.designation_id,
            "textId": self.text_id,
            "designation": self.designation,
            "diameterMm": self.diameter_mm,
            "glyphIds": list(self.glyph_ids),
            "leaderId": self.leader_id,
            "leaderObjectIds": list(self.leader_object_ids),
            "leaderTip": [qs(self.leader_tip[0]), qs(self.leader_tip[1])],
            "feObjectId": self.fe_object_id,
            "feLayer": self.fe_layer,
            "pipeRunId": self.pipe_run_id,
            "physicalPipeId": self.physical_pipe_id,
        }


@dataclass(frozen=True, slots=True)
class ChainFailure:
    """A label that did not complete the chain, and where it stopped."""

    text_id: str
    designation_id: str | None
    text: str
    stage: str
    reason: str
    bbox: BBox
    point: tuple[float, float] | None = None

    def canonical_key(self) -> tuple:
        return (self.stage, self.text, self.text_id)

    def to_canonical(self) -> dict:
        return {
            "textId": self.text_id,
            "designationId": self.designation_id,
            "text": self.text,
            "stage": self.stage,
            "reason": self.reason,
            "bbox": self.bbox.to_canonical(),
            "point": [qs(self.point[0]), qs(self.point[1])] if self.point else None,
        }


@dataclass(frozen=True, slots=True)
class AssociationResult:
    assignments: dict[str, RunAssignment]
    designation_to_runs: dict[str, tuple[str, ...]]
    diagnostics: tuple[tuple[str, str], ...]
    chains: tuple[ChainLink, ...] = ()
    failures: tuple[ChainFailure, ...] = ()
    counts: ChainCounts | None = None
    proximity_hints: tuple[tuple[str, str, float], ...] = ()


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
    leaders: Sequence[VectorLeader],
    attachments: Sequence[FeAttachment],
    attachment_failures: Sequence[AttachmentFailure],
    diameter_of_run: Mapping[str, float | None],
    text_cap_height: float,
) -> AssociationResult:
    """Bind labels to pipes, and only where the drawing said so."""
    runs = canonical_sort(list(runs), key=lambda r: r.canonical_key())
    runs_by_id = {r.pipe_run_id: r for r in runs}
    labels = [
        d
        for d in canonical_sort(list(designations), key=lambda d: d.canonical_key())
        if d.role is TextRole.PIPE_DESIGNATION and not d.is_legend
    ]
    by_text_id = {d.text_item_id: d for d in labels}
    leader_by_text = {l.text_id: l for l in leaders}
    failure_by_text: dict[str, AttachmentFailure] = {}
    for f in attachment_failures:
        failure_by_text.setdefault(f.text_id, f)

    assignments: dict[str, RunAssignment] = {}
    designation_to_runs: dict[str, list[str]] = {}
    diagnostics: list[tuple[str, str]] = []
    chains: list[ChainLink] = []
    failures: list[ChainFailure] = []
    attached_text_ids: set[str] = set()

    # A label offers more than one line as a possible leader, and more than one
    # may reach pipework.  Where they reach *connected* geometry they are
    # saying the same thing; where they reach pipes that do not touch, the
    # label is pointing at two different places and the drawing is not telling
    # us which - so it names neither.
    by_label: dict[str, list[FeAttachment]] = {}
    for a in attachments:
        by_label.setdefault(a.text_id, []).append(a)
    contradicting: set[str] = set()
    for text_id, group in sorted(by_label.items()):
        run_ids = sorted({a.run_id for a in group})
        if len(run_ids) > 1 and not _runs_connected(run_ids, runs_by_id):
            contradicting.add(text_id)

    for a in canonical_sort(list(attachments), key=lambda x: x.canonical_key()):
        if a.text_id in contradicting:
            continue
        d = by_text_id.get(a.text_id)
        run = runs_by_id.get(a.run_id)
        if d is None or run is None:
            continue
        leader = leader_by_text.get(a.text_id)
        attached_text_ids.add(a.text_id)
        measured = diameter_of_run.get(run.pipe_run_id)
        size_score = NEUTRAL_EVIDENCE
        if d.diameter_mm is not None and measured is not None:
            rel = abs(measured - d.diameter_mm) / max(d.diameter_mm, 1e-6)
            size_score = CONFLICTING_SIZE_SCORE if rel > SIZE_CONFLICT_RELATIVE else 1.0
        evidence = (
            ("leaderTraced", 1.0),
            ("leaderLengthPt", qs(leader.length if leader else 0.0)),
            ("attachmentDistancePt", qs(a.distance_pt)),
            ("feLayerVerified", 1.0 if a.fe_layer else 0.0),
            ("sizeConsistency", qs(size_score)),
        )
        _record(
            assignments,
            run.pipe_run_id,
            RunAssignment(
                designation=d.text,
                designation_ids=(d.designation_id,),
                diameter_mm=measured,
                state=IdentityState.CONFIRMED,
                reasons=(),
                association_confidence=qs(min(1.0, 0.75 + 0.25 * (1.0 if a.fe_layer else 0.0))),
                evidence=evidence,
            ),
        )
        designation_to_runs.setdefault(d.designation_id, []).append(run.pipe_run_id)
        chains.append(
            ChainLink(
                designation_id=d.designation_id,
                text_id=d.text_item_id,
                designation=d.text,
                diameter_mm=d.diameter_mm,
                glyph_ids=tuple(d.glyph_ids),
                leader_id=a.leader_id,
                leader_object_ids=tuple(leader.object_ids) if leader else (),
                leader_tip=a.tip,
                fe_object_id=a.fe_object_id,
                fe_layer=a.fe_layer,
                pipe_run_id=run.pipe_run_id,
            )
        )

    # Everything that did not complete the chain says where it stopped, so the
    # failure can be looked at rather than argued about.
    proximity_hints: list[tuple[str, str, float]] = []
    index: SpatialIndex[int] = SpatialIndex.for_items(
        [(BBox.from_points(r.centerline), i) for i, r in enumerate(runs)]
    ) if runs else None
    radius = PROXIMITY_RADIUS_CAP_FACTOR * max(text_cap_height, 1.0)
    for d in labels:
        if d.text_item_id in attached_text_ids:
            continue
        if d.text_item_id in contradicting:
            diagnostics.append((d.designation_id, Reason.COMPETING_PIPES.value))
            failures.append(
                ChainFailure(
                    text_id=d.text_item_id, designation_id=d.designation_id, text=d.text,
                    stage="attachment", reason="LEADERS_REACH_UNCONNECTED_PIPES",
                    bbox=d.bbox, point=None,
                )
            )
            continue
        leader = leader_by_text.get(d.text_item_id)
        if leader is None:
            stage, reason, point = "leader", Reason.NO_ASSOCIATION_EVIDENCE.value, None
        else:
            failure = failure_by_text.get(d.text_item_id)
            stage = "attachment"
            reason = failure.reason if failure else "TIP_REACHES_NO_PIPE"
            point = leader.tip
        diagnostics.append((d.designation_id, Reason.NO_ASSOCIATION_EVIDENCE.value))
        failures.append(
            ChainFailure(
                text_id=d.text_item_id,
                designation_id=d.designation_id,
                text=d.text,
                stage=stage,
                reason=reason,
                bbox=d.bbox,
                point=point,
            )
        )
        # measured, never used: how near the closest run happens to be
        if index is not None:
            anchor = d.bbox.center
            best = math.inf
            best_run = ""
            for ri in index.query_box(d.bbox.expanded(radius)):
                distance = _run_distance(runs[ri], anchor)
                if distance < best:
                    best, best_run = distance, runs[ri].pipe_run_id
            if best <= radius:
                proximity_hints.append((d.designation_id, best_run, qs(best)))

    _propagate(runs, assignments, diameter_of_run, diagnostics)

    designated_runs = len([1 for a in assignments.values() if a.designation])
    counts = ChainCounts(
        designation_occurrences=len(labels),
        designations_with_dn=len([d for d in labels if d.diameter_mm is not None]),
        vector_leaders=len({l.text_id for l in leaders if l.text_id in by_text_id}),
        verified_attachments=len(attached_text_ids),
        confirmed_designations=len({c.designation for c in chains}),
        designated_runs=designated_runs,
    )

    return AssociationResult(
        assignments=assignments,
        designation_to_runs={k: tuple(sorted(v)) for k, v in sorted(designation_to_runs.items())},
        diagnostics=tuple(sorted(set(diagnostics))),
        chains=tuple(canonical_sort(chains, key=lambda c: c.canonical_key())),
        failures=tuple(canonical_sort(failures, key=lambda f: f.canonical_key())),
        counts=counts,
        proximity_hints=tuple(sorted(proximity_hints)),
    )


def _runs_connected(run_ids: Sequence[str], runs_by_id: Mapping[str, PipeRun],
                    tolerance: float = 2.5) -> bool:
    """Do these runs form one piece of pipework?

    Runs are connected when their ends meet, directly or through each other.
    Two leaders from one label that land on connected runs are pointing at the
    same pipe; ones that land on separate pipes are a contradiction.
    """
    remaining = [runs_by_id[r] for r in run_ids if r in runs_by_id]
    if len(remaining) < 2:
        return True
    reached = [remaining[0]]
    pool = remaining[1:]
    changed = True
    while changed and pool:
        changed = False
        for run in list(pool):
            for known in reached:
                if any(dist(a, b) <= tolerance
                       for a in (run.centerline[0], run.centerline[-1])
                       for b in (known.centerline[0], known.centerline[-1])):
                    reached.append(run)
                    pool.remove(run)
                    changed = True
                    break
    return not pool


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
    # Bucket run endpoints on a grid of the join tolerance so only runs that
    # could touch are compared - linear in the number of runs, not quadratic.
    buckets: dict[tuple[int, int], list[int]] = {}
    for i, r in enumerate(runs):
        for p in (r.centerline[0], r.centerline[-1]):
            cx = int(math.floor(p[0] / RUN_JOIN_TOLERANCE_PT))
            cy = int(math.floor(p[1] / RUN_JOIN_TOLERANCE_PT))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    buckets.setdefault((cx + dx, cy + dy), []).append(i)

    seen: set[tuple[int, int]] = set()
    for cell in sorted(buckets):
        members = sorted(set(buckets[cell]))
        for a_pos in range(len(members)):
            for b_pos in range(a_pos + 1, len(members)):
                i, j = members[a_pos], members[b_pos]
                if (i, j) in seen:
                    continue
                seen.add((i, j))
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
