"""Leader endpoint -> pipe-carrying layer -> physical pipe geometry.

The chain the drawing supports is:

    designation -> its vector leader -> the leader's endpoint
                -> geometry on a layer that carries pipes -> the run there

The last link is the one this module adds.  A leader tip that merely lands
*near something* proves nothing: a plan sheet is full of linework, and the
nearest stroke to a tip is as likely to be a wall, a door swing or a hatch as a
pipe.  The tip has to land on geometry the engine already accepted as pipework,
and - where the file declares layers - on a layer that carries pipework.

The set of pipe-carrying layers is *discovered*, never named: the layers are
ranked by how much accepted pipe centerline they produced, and those carrying a
real share of it qualify.  A file with no layers at all still works; the gate
then reports itself inactive rather than silently passing everything as if it
had checked something.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from ..canonical import canonical_sort, entity_id, qc, qs
from ..geometry.index import SpatialIndex
from ..geometry.primitives import BBox, Pt, Segment, dist, point_segment_distance, segments_of_polyline
from ..model import PipeCandidate, PipeRun, VectorObject
from .leaders import VectorLeader

# A leader points at the pipe's wall, so the tolerance grows with the drawn
# width, exactly as the old leader test did.
TIP_FLOOR_PT = 4.0
TIP_WIDTH_FACTOR = 0.75
TIP_CAP_FACTOR = 1.2
# Two runs within this of each other at the tip are not distinguishable.
TIE_EPSILON_PT = 0.75
# A layer must carry at least this share of the accepted pipe length to count
# as a pipe-carrying layer.
LAYER_MIN_SHARE = 0.02


@dataclass(frozen=True, slots=True)
class PipeLayers:
    """Which of the drawing's layers actually carry pipework."""

    names: frozenset[str]
    shares: tuple[tuple[str, float], ...]
    active: bool                      # False when the file declares no layers

    def accepts(self, layers: Sequence[str | None]) -> bool:
        if not self.active:
            return True
        return any((name or "") in self.names for name in layers)

    def to_canonical(self) -> dict:
        return {
            "active": self.active,
            "layers": sorted(self.names),
            "shares": [[name, qs(share)] for name, share in self.shares],
        }


@dataclass(frozen=True, slots=True)
class FeAttachment:
    """A verified leader -> pipe attachment."""

    attachment_id: str
    leader_id: str
    text_id: str
    run_id: str
    fe_object_id: str
    fe_layer: str | None
    tip: Pt
    distance_pt: float

    def canonical_key(self) -> tuple:
        return (self.text_id, self.run_id, (qc(self.tip[0]), qc(self.tip[1])))

    def to_canonical(self) -> dict:
        return {
            "attachmentId": self.attachment_id,
            "leaderId": self.leader_id,
            "textId": self.text_id,
            "pipeRunId": self.run_id,
            "feObjectId": self.fe_object_id,
            "feLayer": self.fe_layer,
            "tip": [qc(self.tip[0]), qc(self.tip[1])],
            "distancePt": qs(self.distance_pt),
        }


@dataclass(frozen=True, slots=True)
class AttachmentFailure:
    """Why a traced leader did not reach a pipe - kept for the debug crops."""

    leader_id: str
    text_id: str
    tip: Pt
    reason: str
    detail: tuple[tuple[str, str], ...] = ()

    def canonical_key(self) -> tuple:
        return (self.text_id, self.reason, (qc(self.tip[0]), qc(self.tip[1])))

    def to_canonical(self) -> dict:
        return {
            "leaderId": self.leader_id,
            "textId": self.text_id,
            "tip": [qc(self.tip[0]), qc(self.tip[1])],
            "reason": self.reason,
            "detail": {k: v for k, v in self.detail},
        }


def discover_pipe_layers(
    candidates: Sequence[PipeCandidate],
    objects_by_id: Mapping[str, VectorObject],
    min_share: float = LAYER_MIN_SHARE,
) -> PipeLayers:
    """Rank the drawing's layers by how much accepted pipe they carry."""
    by_layer: dict[str, float] = {}
    total = 0.0
    saw_layer = False
    for c in candidates:
        if not c.accepted:
            continue
        length = c.length_pt
        layers = {objects_by_id[o].layer for o in c.source_object_ids if o in objects_by_id}
        named = sorted(x for x in layers if x)
        if named:
            saw_layer = True
        for name in named or [""]:
            by_layer[name] = by_layer.get(name, 0.0) + length / max(1, len(named or [""]))
        total += length
    if not saw_layer or total <= 0.0:
        return PipeLayers(frozenset(), (), active=False)
    shares = [(name, value / total) for name, value in by_layer.items() if name]
    shares.sort(key=lambda kv: (-kv[1], kv[0]))
    names = frozenset(name for name, share in shares if share >= min_share)
    return PipeLayers(names=names, shares=tuple((n, qs(s)) for n, s in shares), active=bool(names))


def _run_distance(run: PipeRun, point: Pt) -> float:
    return min(
        (point_segment_distance(point, s) for s in segments_of_polyline(run.centerline)),
        default=math.inf,
    )


def source_objects_of_run(
    run: PipeRun,
    candidates_by_id: Mapping[str, PipeCandidate],
    candidate_ids_of_run: Mapping[str, Sequence[str]],
) -> tuple[str, ...]:
    """The PDF objects a run is actually drawn from.

    A run records the graph edges it is made of, not the ink; the candidates
    behind those edges are what carry the source objects, and the source
    objects are what carry the layer.  Without this hop the layer gate would
    have nothing to test and would pass everything - which is exactly the
    silent no-op the specification warns about.
    """
    out: set[str] = set()
    for cid in candidate_ids_of_run.get(run.pipe_run_id, ()):
        candidate = candidates_by_id.get(cid)
        if candidate is not None:
            out.update(candidate.source_object_ids)
    out.update(run.source_object_ids)
    return tuple(sorted(out))


def attach_leaders(
    leaders: Sequence[VectorLeader],
    runs: Sequence[PipeRun],
    objects_by_id: Mapping[str, VectorObject],
    pipe_layers: PipeLayers,
    cap_height: float,
    source_objects: Mapping[str, Sequence[str]] | None = None,
) -> tuple[tuple[FeAttachment, ...], tuple[AttachmentFailure, ...]]:
    """Verify each leader against the pipe geometry at its endpoint."""
    runs = canonical_sort(list(runs), key=lambda r: r.canonical_key())
    if not runs:
        return (), tuple(
            AttachmentFailure(l.leader_id, l.text_id, l.tip, "NO_PIPE_GEOMETRY")
            for l in leaders
        )
    index: SpatialIndex[int] = SpatialIndex.for_items(
        [(BBox.from_points(r.centerline), i) for i, r in enumerate(runs)]
    )
    attachments: list[FeAttachment] = []
    failures: list[AttachmentFailure] = []
    for leader in canonical_sort(list(leaders), key=lambda l: l.canonical_key()):
        tolerance = TIP_FLOOR_PT + TIP_CAP_FACTOR * max(cap_height, 1.0)
        probe = BBox(leader.tip[0] - tolerance, leader.tip[1] - tolerance,
                     leader.tip[0] + tolerance, leader.tip[1] + tolerance)
        hits: list[tuple[float, int]] = []
        off_layer: list[tuple[float, int]] = []
        for ri in index.query_box(probe):
            run = runs[ri]
            tol = max(TIP_FLOOR_PT, TIP_WIDTH_FACTOR * (run.width_pt or 0.0) + 2.0)
            distance = _run_distance(run, leader.tip)
            if distance > tol:
                continue
            run_objects = (source_objects or {}).get(run.pipe_run_id) or run.source_object_ids
            layers = [objects_by_id[o].layer for o in run_objects if o in objects_by_id]
            if pipe_layers.accepts(layers):
                hits.append((qs(distance), ri))
            else:
                off_layer.append((qs(distance), ri))
        if not hits:
            reason = "TIP_ON_NON_PIPE_LAYER" if off_layer else "TIP_REACHES_NO_PIPE"
            failures.append(
                AttachmentFailure(
                    leader.leader_id, leader.text_id, leader.tip, reason,
                    detail=(("candidatesOffLayer", str(len(off_layer))),),
                )
            )
            continue
        hits.sort(key=lambda h: (h[0], runs[h[1]].canonical_key()))
        if len(hits) > 1 and hits[1][0] - hits[0][0] < TIE_EPSILON_PT:
            failures.append(
                AttachmentFailure(
                    leader.leader_id, leader.text_id, leader.tip, "TIP_BETWEEN_TWO_PIPES",
                    detail=(("runs", f"{runs[hits[0][1]].pipe_run_id},{runs[hits[1][1]].pipe_run_id}"),),
                )
            )
            continue
        distance, ri = hits[0]
        run = runs[ri]
        run_objects = (source_objects or {}).get(run.pipe_run_id) or run.source_object_ids
        fe_object_id, fe_layer = _nearest_source_object(run_objects, objects_by_id, leader.tip)
        attachments.append(
            FeAttachment(
                attachment_id=entity_id("attach", (leader.text_id, run.pipe_run_id,
                                                   (qc(leader.tip[0]), qc(leader.tip[1])))),
                leader_id=leader.leader_id,
                text_id=leader.text_id,
                run_id=run.pipe_run_id,
                fe_object_id=fe_object_id,
                fe_layer=fe_layer,
                tip=leader.tip,
                distance_pt=distance,
            )
        )
    return (
        tuple(canonical_sort(attachments, key=lambda a: a.canonical_key())),
        tuple(canonical_sort(failures, key=lambda f: f.canonical_key())),
    )


def _nearest_source_object(
    object_ids: Sequence[str], objects_by_id: Mapping[str, VectorObject], tip: Pt
) -> tuple[str, str | None]:
    """The actual PDF object the leader landed on, for the evidence chain."""
    best: tuple[float, str, str | None] | None = None
    for oid in sorted(object_ids):
        o = objects_by_id.get(oid)
        if o is None:
            continue
        d = min((point_segment_distance(tip, s) for s in o.segments()), default=math.inf)
        key = (qs(d), oid, o.layer)
        if best is None or key < best:
            best = key
    if best is None:
        return ("", None)
    return (best[1], best[2])
