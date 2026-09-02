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
from ..geometry.primitives import (BBox, Pt, Segment, angle_diff, dist,
                                   point_segment_distance, segments_of_polyline)
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
# A leader *ends at* pipework; it does not run along it.  When most of a traced
# line lies on the run it would attach to, the line is that pipe, not a leader
# pointing at it - which is what happens when a pipe passes beside a label and
# the two are drawn with similar pens.
MAX_SHARE_ALONG_RUN = 0.5
ALONG_FLOOR_PT = 2.0
# How many verified leaders must land on a layer before it counts as carrying
# pipework, and what share of them.
MIN_ATTESTING_ATTACHMENTS = 3
MIN_ATTESTED_SHARE = 0.05


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
    # The style of the run this landed on.  A pipe whose bore the drawing
    # actually draws - two walls, or a dash chain - is evidence about its
    # layer; a bare stroke that nothing else corroborates is not.
    run_style: str = ""

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
            "runStyle": self.run_style,
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


@dataclass(frozen=True, slots=True)
class PipePens:
    """The pen weights the drawing draws its pipes with."""

    widths: tuple[float, ...]
    shares: tuple[tuple[float, float], ...]
    active: bool

    def accepts(self, width: float | None, tolerance: float = 0.15) -> bool:
        if not self.active:
            return True
        if width is None:
            return False
        return any(abs(width - w) <= tolerance * max(w, 1e-6) for w in self.widths)

    def to_canonical(self) -> dict:
        return {"active": self.active, "widths": [qs(w) for w in self.widths],
                "shares": [[qs(w), qs(share)] for w, share in self.shares]}


def attested_pipe_pens(
    attachments: Sequence[FeAttachment],
    pen_of_run: Mapping[str, float | None],
    min_share: float = 0.10,
    excluded_pens: Sequence[float] = (),
) -> PipePens:
    """The pen weights the drawing points its leaders at.

    A plan sheet draws its services heavy and its background light, and it says
    which is which by pointing a labelled leader at one of them.  Without this,
    a hatch boundary on a pipe layer, or the annotation strokes on the layer the
    leaders themselves live on, are counted as pipe: on the production sheet
    that was 9 921 pt of 0.36 pt hatch and 6 975 pt of 0.48-0.72 pt lettering
    lines, against pipes drawn at 1.44 and 2.04.

    Like the layers, the weights are taken from the drawing rather than named:
    a sheet drawn at any other weight moves them with it.
    """
    weight: dict[float, float] = {}
    for a in attachments:
        if a.run_style not in ("double_line", "dashed_line"):
            continue          # a bare stroke says nothing about the pipe pen
        pen = pen_of_run.get(a.run_id)
        if pen is None or pen <= 0.0:
            continue
        if any(abs(pen - x) <= 0.01 for x in excluded_pens if x):
            continue          # the weight this sheet letters with
        key = qc(pen)
        weight[key] = weight.get(key, 0.0) + 1.0
    if not weight:
        return PipePens((), (), active=False)
    total = sum(weight.values())
    shares = sorted(((w, n / total) for w, n in weight.items()), key=lambda kv: (-kv[1], kv[0]))
    widths = tuple(w for w, share in shares if share >= min_share)
    if not widths:
        return PipePens((), (), active=False)
    return PipePens(widths=widths,
                    shares=tuple((qs(w), qs(share)) for w, share in shares),
                    active=True)


def attested_pipe_layers(
    attachments: Sequence[FeAttachment],
    fallback: "PipeLayers | None" = None,
) -> PipeLayers:
    """The layers the drawing's own leaders point at.

    This is the strongest statement a sheet makes about what is pipework: a
    draughtsman drew a line from a pipe designation to *this* geometry.  Layers
    ranked by how much accepted centerline they carry cannot say that - on a
    plan sheet the architectural background carries more line than the piping
    does, and a wall pair at a pipe-like separation is indistinguishable from a
    pipe until something points at one of them.

    Where no attachment was verified there is nothing to attest, and the
    fallback (or an inactive gate) is returned rather than an empty answer that
    would silently exclude everything.
    """
    counts: dict[str, int] = {}
    for a in attachments:
        # Only geometry whose bore the drawing draws attests a layer: a pair of
        # walls or a dash chain.  A single unpaired stroke is the weakest thing
        # the detector accepts, and one leader landing near one of them is not
        # evidence that a whole layer is pipework.
        if a.fe_layer and a.run_style in ("double_line", "dashed_line"):
            counts[a.fe_layer] = counts.get(a.fe_layer, 0) + 1
    if not counts:
        return fallback or PipeLayers(frozenset(), (), active=False)
    total = float(sum(counts.values()))
    shares = sorted(((name, n / total) for name, n in counts.items()),
                    key=lambda kv: (-kv[1], kv[0]))
    # A layer the drawing points at once, among a hundred, is a mis-traced
    # leader rather than a system.  A layer it points at repeatedly is a system.
    names = frozenset(
        name for name, share in shares
        if counts[name] >= MIN_ATTESTING_ATTACHMENTS and share >= MIN_ATTESTED_SHARE
    )
    if not names:
        return fallback or PipeLayers(frozenset(), (), active=False)
    return PipeLayers(
        names=names,
        shares=tuple((name, qs(share)) for name, share in shares),
        active=True,
    )


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
    symbol_boxes: Sequence[BBox] = (),
) -> tuple[tuple[FeAttachment, ...], tuple[AttachmentFailure, ...]]:
    """Verify each leader against the pipe geometry at its endpoint.

    A leader may point at the pipe itself or at a symbol on it - a gully, a
    cleaning eye, a floor drain - and pointing at the symbol is pointing at the
    pipe that terminates there.  Where the tip lands inside a symbol, the pipe
    reached *through* that symbol counts, provided exactly one pipe touches it:
    two make the symbol a junction, and a junction identifies nothing.
    """
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
        symbol = _symbol_at(symbol_boxes, leader.tip)
        if symbol is not None:
            probe = BBox(symbol.x0 - tolerance, symbol.y0 - tolerance,
                         symbol.x1 + tolerance, symbol.y1 + tolerance)
        hits: list[tuple[float, int]] = []
        off_layer: list[tuple[float, int]] = []
        for ri in index.query_box(probe):
            run = runs[ri]
            # A leader ends in an arrowhead, and the trace stops where the
            # barbs fork - a few points short of the pipe.  The tolerance
            # therefore has to allow the annotation's own scale as well as the
            # pipe's drawn width; both are the drawing's, neither is a constant.
            tol = max(TIP_FLOOR_PT,
                      TIP_WIDTH_FACTOR * (run.width_pt or 0.0) + 2.0,
                      TIP_CAP_FACTOR * max(cap_height, 1.0))
            distance = _run_distance(run, leader.tip)
            if symbol is not None:
                distance = min(distance, _distance_to_box(symbol, run))
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
        # "Runs along" means coincident with the pipe, not merely near it: a
        # short leader approaching at a shallow angle is close to its pipe for
        # most of its length and is still a leader.
        hits = [
            h for h in hits
            if _share_along_run(leader, runs[h[1]],
                                max(ALONG_FLOOR_PT, TIP_CAP_FACTOR * max(cap_height, 1.0)))
            <= MAX_SHARE_ALONG_RUN
        ]
        if not hits:
            failures.append(
                AttachmentFailure(leader.leader_id, leader.text_id, leader.tip,
                                  "LEADER_RUNS_ALONG_THE_PIPE")
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
                run_style=run.style,
            )
        )
    return (
        tuple(canonical_sort(attachments, key=lambda a: a.canonical_key())),
        tuple(canonical_sort(failures, key=lambda f: f.canonical_key())),
    )


def _share_along_run(leader: VectorLeader, run: PipeRun, tolerance: float,
                     parallel_degrees: float = 6.0) -> float:
    """How much of a traced line lies *on* the run it would attach to.

    Both conditions matter.  Near is not enough: a short leader approaching at a
    shallow angle is near its pipe for most of its length and is still a
    leader.  What marks a line as being the pipe rather than pointing at it is
    that it stays near *and* parallel - which is exactly what a wall of a
    double-line pipe, or a second stroke of the same run, does.
    """
    total = 0.0
    along = 0.0
    run_segments = segments_of_polyline(run.centerline)
    if not run_segments:
        return 0.0
    for i in range(len(leader.polyline) - 1):
        a, b = leader.polyline[i], leader.polyline[i + 1]
        length = dist(a, b)
        if length <= 0.0:
            continue
        total += length
        midpoint = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        if not (_run_distance(run, a) <= tolerance and _run_distance(run, b) <= tolerance
                and _run_distance(run, midpoint) <= tolerance):
            continue
        piece = Segment(a, b)
        nearest = min(run_segments, key=lambda s: point_segment_distance(midpoint, s))
        if math.degrees(angle_diff(piece.angle, nearest.angle)) <= parallel_degrees:
            along += length
    return along / total if total > 0.0 else 0.0


def _symbol_at(symbol_boxes: Sequence[BBox], point: Pt) -> BBox | None:
    """The symbol a leader tip landed in, if it landed in one."""
    found = [b for b in symbol_boxes if b.contains_point(point)]
    if len(found) != 1:
        return None
    return found[0]


def _distance_to_box(box: BBox, run: PipeRun) -> float:
    """How far a run passes from a symbol's box."""
    best = math.inf
    for segment in segments_of_polyline(run.centerline):
        for corner in ((box.x0, box.y0), (box.x1, box.y0), (box.x1, box.y1), (box.x0, box.y1),
                       ((box.x0 + box.x1) / 2.0, (box.y0 + box.y1) / 2.0)):
            best = min(best, point_segment_distance(corner, segment))
    return best


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
