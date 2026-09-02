"""Vector leaders, traced rather than guessed.

A leader is the drawing *stating* which pipe a label belongs to, and it is the
only statement of that kind a plan sheet contains.  Everything else - how close
a label sits to a line, which way round it is set - is inference about a
statement that was never made.

The rule this module replaces accepted a leader only when it was a single
two-point stroked object.  Real CAD leaders are not that: they are a shoulder
and a slant, drawn as a polyline or as several objects that meet end to end, and
often continued through a bend.  Requiring one object threw most of them away,
and the association stage then fell back on proximity - which is how notes,
dates and title-block strings ended up naming pipes.

Tracing keeps the same conservatism the rest of the engine has:

* one end must touch the label and the other must leave it;
* two different lines touching a label equally are not a leader, because the
  drawing is then not stating anything unambiguous;
* the trace stops at a fork, at a right-angle turn, and at a change to a
  heavier pen - each of those is where a leader ends and other linework begins;
* every object the trace passed through is recorded, so the chain can be
  replayed from the PDF's own objects.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from ..canonical import canonical_sort, entity_id, qc, qs
from ..geometry.index import SpatialIndex
from ..geometry.primitives import (
    BBox,
    Pt,
    Segment,
    angle_diff,
    dist,
    point_segment_distance,
)
from ..model import TextItem, VectorObject
from ..states import IdentityState

# How far outside a label's box a leader may start, in cap heights.
ATTACH_RATIO = 0.9
# A leader is at least this long, in cap heights; shorter marks are lettering.
MIN_LENGTH_RATIO = 0.6
# Endpoints closer than this are the same place.
JOIN_TOLERANCE_PT = 1.2
# A turn sharper than this ends the leader.
MAX_TURN_DEGREES = 68.0
# A leader does not become a heavier line half way along.
MAX_WIDTH_GROWTH = 1.75
MAX_HOPS = 14


@dataclass(frozen=True, slots=True)
class VectorLeader:
    """One traced leader, with everything needed to replay it."""

    leader_id: str
    page: int
    text_id: str
    object_ids: tuple[str, ...]
    polyline: tuple[Pt, ...]
    root: Pt
    tip: Pt
    length: float
    hops: int

    @property
    def bbox(self) -> BBox:
        return BBox.from_points(self.polyline)

    def canonical_key(self) -> tuple:
        return (self.page, self.text_id, tuple((qc(x), qc(y)) for x, y in self.polyline))

    def to_canonical(self) -> dict:
        return {
            "leaderId": self.leader_id,
            "page": self.page,
            "textId": self.text_id,
            "objectIds": list(self.object_ids),
            "polyline": [[qc(x), qc(y)] for x, y in self.polyline],
            "root": [qc(self.root[0]), qc(self.root[1])],
            "tip": [qc(self.tip[0]), qc(self.tip[1])],
            "lengthPt": qs(self.length),
            "hops": self.hops,
        }


@dataclass(frozen=True, slots=True)
class _Piece:
    """A straight piece of an eligible object, with its object's identity."""

    object_id: str
    segment: Segment
    width: float


def _pieces(objects: Sequence[VectorObject], exclude: frozenset[str]) -> list[_Piece]:
    out: list[_Piece] = []
    for o in objects:
        if not o.is_stroked or o.object_id in exclude:
            continue
        width = float(o.stroke_width or 0.0)
        for s in o.segments():
            if s.length > 0.0:
                out.append(_Piece(o.object_id, s, width))
    return out


def _turn(a: Segment, b: Segment) -> float:
    return math.degrees(angle_diff(a.angle, b.angle))


def trace_leaders(
    text_items: Sequence[TextItem],
    objects: Sequence[VectorObject],
    cap_height: float,
    exclude_object_ids: frozenset[str] = frozenset(),
    page: int = 0,
) -> tuple[VectorLeader, ...]:
    """Trace one leader per label that has an unambiguous one."""
    cap = max(cap_height, 1e-3)
    pieces = _pieces(objects, exclude_object_ids)
    if not pieces:
        return ()
    index: SpatialIndex[int] = SpatialIndex.for_items(
        [(p.segment.bbox, i) for i, p in enumerate(pieces)]
    )
    endpoints: SpatialIndex[int] = SpatialIndex.for_items(
        [(BBox.from_points([p.segment.a, p.segment.b]), i) for i, p in enumerate(pieces)]
    )
    attach = ATTACH_RATIO * cap
    minimum = MIN_LENGTH_RATIO * cap

    leaders: list[VectorLeader] = []
    for t in canonical_sort(list(text_items), key=lambda x: x.canonical_key()):
        if t.state is IdentityState.UNRESOLVED or len(t.text.strip()) < 2:
            continue
        probe = t.bbox.expanded(attach)
        inner = t.bbox.expanded(-0.1)
        starts: list[tuple[float, int, Pt, Pt]] = []
        for i in index.query_box(probe):
            piece = pieces[i]
            for near, far in ((piece.segment.a, piece.segment.b),
                              (piece.segment.b, piece.segment.a)):
                if not probe.contains_point(near) or inner.contains_point(far):
                    continue
                starts.append((qs(_box_distance(t.bbox, near)), i, near, far))
        if not starts:
            continue
        starts.sort(key=lambda s: (s[0], pieces[s[1]].segment.angle, s[2], s[3]))
        best = starts[0]
        # Two different lines touching the label equally is not a statement.
        for rival in starts[1:]:
            if abs(rival[0] - best[0]) > 1e-9:
                break
            if _turn(pieces[rival[1]].segment, pieces[best[1]].segment) > 5.0:
                best = None
                break
        if best is None:
            continue
        _, start_index, root, far = best
        polyline, object_ids, hops = _follow(pieces, endpoints, start_index, root, far)
        length = sum(dist(polyline[i], polyline[i + 1]) for i in range(len(polyline) - 1))
        if length < minimum:
            continue
        leaders.append(
            VectorLeader(
                leader_id=entity_id("leader", (page, t.text_id,
                                               tuple((qc(x), qc(y)) for x, y in polyline))),
                page=page,
                text_id=t.text_id,
                object_ids=tuple(sorted(set(object_ids))),
                polyline=tuple(polyline),
                root=polyline[0],
                tip=polyline[-1],
                length=qs(length),
                hops=hops,
            )
        )
    return tuple(canonical_sort(leaders, key=lambda l: l.canonical_key()))


def _follow(
    pieces: Sequence[_Piece],
    endpoints: SpatialIndex[int],
    start_index: int,
    root: Pt,
    far: Pt,
) -> tuple[list[Pt], list[str], int]:
    """Walk on from the label while the line keeps going one way."""
    polyline = [root, far]
    used = {start_index}
    object_ids = [pieces[start_index].object_id]
    current = pieces[start_index]
    tip = far
    hops = 0
    for _ in range(MAX_HOPS):
        options: list[tuple[float, int, Pt]] = []
        probe = BBox(tip[0] - JOIN_TOLERANCE_PT, tip[1] - JOIN_TOLERANCE_PT,
                     tip[0] + JOIN_TOLERANCE_PT, tip[1] + JOIN_TOLERANCE_PT)
        for i in endpoints.query_box(probe):
            if i in used:
                continue
            piece = pieces[i]
            if current.width > 0.0 and piece.width > current.width * MAX_WIDTH_GROWTH:
                continue
            for near, other in ((piece.segment.a, piece.segment.b),
                                (piece.segment.b, piece.segment.a)):
                if dist(near, tip) > JOIN_TOLERANCE_PT:
                    continue
                turn = _turn(current.segment, piece.segment)
                if turn > MAX_TURN_DEGREES:
                    continue
                options.append((qs(turn), i, other))
        if not options:
            break
        options.sort(key=lambda o: (o[0], pieces[o[1]].object_id, o[2]))
        if len(options) > 1 and abs(options[1][0] - options[0][0]) < 1e-9:
            break                       # a fork: the drawing is not stating one path
        _, index, other = options[0]
        used.add(index)
        object_ids.append(pieces[index].object_id)
        polyline.append(other)
        current = pieces[index]
        tip = other
        hops += 1
    return polyline, object_ids, hops


def _box_distance(box: BBox, point: Pt) -> float:
    dx = max(box.x0 - point[0], point[0] - box.x1, 0.0)
    dy = max(box.y0 - point[1], point[1] - box.y1, 0.0)
    return math.hypot(dx, dy)


def leaders_by_text_item(leaders: Sequence[VectorLeader]) -> Mapping[str, VectorLeader]:
    out: dict[str, VectorLeader] = {}
    for leader in leaders:
        out.setdefault(leader.text_id, leader)
    return {k: out[k] for k in sorted(out)}
