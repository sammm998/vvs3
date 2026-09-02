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
# A label on a real sheet is a *block*: the designation, the size or elevation
# written under it, and the rules drawn under each line.  The draughtsman
# attaches the leader to the block - very often to the end of the lower rule -
# so a probe around the designation alone misses it by the height of the line
# below.  These bound what counts as one block: the lines sit within this many
# cap heights of each other and overlap this much horizontally.
# Lines closer than this, in cap heights, are one label block: a code and the
# size or level written under it.  The next label down is not part of it -
# merging two labels made each one's leader indistinguishable from its
# neighbour's, and both were then refused.
BLOCK_GAP_CAPS = 0.8
BLOCK_OVERLAP = 0.35
# A leader is at least this long, in cap heights; shorter marks are lettering.
MIN_LENGTH_RATIO = 0.6
# Endpoints closer than this are the same place.
JOIN_TOLERANCE_PT = 1.2
# A turn sharper than this ends the leader.
MAX_TURN_DEGREES = 68.0
# A leader does not become a heavier line half way along.
MAX_WIDTH_GROWTH = 1.75
# A leader is drawn with the annotation pen - the same weight as the lettering
# it belongs to, or finer.  A pipe passing behind a label is much heavier, and
# this is what stops one being read as that label's leader.
START_WIDTH_FACTOR = 1.6
MAX_HOPS = 14
# How many strokes touching a label are worth tracing before deciding which of
# them is its leader.
MAX_START_CANDIDATES = 8
# A label may offer more than one line as a possible leader; each is kept and
# verified, and the chain decides which of them is one.
MAX_LEADERS_PER_LABEL = 3
# A stroke the text stage claimed as part of the lettering may still be the
# leader: on many sheets the rule under a label simply keeps going until it
# reaches the pipe.  Such a stroke is allowed to start a trace only if it
# escapes the label block by this many cap heights, which is what separates a
# leader from an underline - and never if it is short enough to be a character.
ESCAPE_CAPS = 1.5
MIN_ANNOTATION_LENGTH_CAPS = 1.5


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
class LeaderRefusal:
    """Why a label got no leader - the first thing to look at when one is missing."""

    text_id: str
    text: str
    reason: str
    detail: tuple[tuple[str, float], ...] = ()

    def to_canonical(self) -> dict:
        return {"textId": self.text_id, "text": self.text, "reason": self.reason,
                "detail": {k: qs(v) for k, v in self.detail}}


@dataclass(frozen=True, slots=True)
class _Piece:
    """A straight piece of an eligible object, with its object's identity."""

    object_id: str
    segment: Segment
    width: float
    # True when the text stage claimed this object as lettering.  Such a stroke
    # is admitted only under the escape test below.
    lettering: bool = False


def _pieces(objects: Sequence[VectorObject], exclude: frozenset[str],
            soft_exclude: frozenset[str] = frozenset()) -> list[_Piece]:
    out: list[_Piece] = []
    for o in objects:
        if not o.is_stroked or o.object_id in exclude:
            continue
        width = float(o.stroke_width or 0.0)
        lettering = o.object_id in soft_exclude
        for s in o.segments():
            if s.length > 0.0:
                out.append(_Piece(o.object_id, s, width, lettering))
    return out


def _turn(a: Segment, b: Segment) -> float:
    return math.degrees(angle_diff(a.angle, b.angle))


def label_blocks(text_items: Sequence[TextItem], cap_height: float) -> dict[str, BBox]:
    """Group stacked text lines into the label block a leader attaches to.

    Grouping is geometric - lines that sit one under another, overlap
    horizontally and share a size - so it does not depend on reading either
    line.  The block is what the leader is drawn from; tracing from the
    designation's own box alone loses every label whose leader leaves from the
    line below it, which on a sheet that writes an elevation under every code
    is most of them.
    """
    items = canonical_sort(list(text_items), key=lambda t: t.canonical_key())
    boxes = {t.text_id: t.bbox for t in items}
    if not items:
        return boxes
    index: SpatialIndex[int] = SpatialIndex.for_items(
        [(t.bbox.expanded(BLOCK_GAP_CAPS * cap_height), i) for i, t in enumerate(items)]
    )
    parent = {t.text_id: t.text_id for t in items}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for i, t in enumerate(items):
        cap = max(t.height, cap_height * 0.5, 1e-3)
        for j in index.query_box(t.bbox.expanded(BLOCK_GAP_CAPS * cap)):
            if j == i:
                continue
            other = items[j]
            if max(t.height, other.height) / max(min(t.height, other.height), 1e-6) > 1.8:
                continue
            overlap = (min(t.bbox.x1, other.bbox.x1) - max(t.bbox.x0, other.bbox.x0))
            narrower = max(1e-6, min(t.bbox.width, other.bbox.width))
            if overlap / narrower < BLOCK_OVERLAP:
                continue
            gap = max(t.bbox.y0, other.bbox.y0) - min(t.bbox.y1, other.bbox.y1)
            if gap > BLOCK_GAP_CAPS * cap:
                continue
            union(t.text_id, other.text_id)

    groups: dict[str, list[BBox]] = {}
    for t in items:
        groups.setdefault(find(t.text_id), []).append(t.bbox)
    merged = {root: BBox.union_all(members) for root, members in groups.items()}
    return {t.text_id: merged[find(t.text_id)] for t in items}


def lettering_pen(objects: Sequence[VectorObject], text_object_ids: frozenset[str]) -> float:
    """The pen the sheet's lettering is drawn with.

    A leader is an annotation, drawn with the annotation pen - the weight of
    the lettering it belongs to, or finer.  Pipework is drawn heavier.  Taking
    the weight from the sheet's own lettering keeps that comparison open-world:
    a drawing that letters at a different weight moves the threshold with it.
    """
    widths = sorted(
        float(o.stroke_width)
        for o in objects
        if o.object_id in text_object_ids and o.stroke_width and o.stroke_width > 0.0
    )
    return widths[len(widths) // 2] if widths else 0.0


def trace_leaders(
    text_items: Sequence[TextItem],
    objects: Sequence[VectorObject],
    cap_height: float,
    exclude_object_ids: frozenset[str] = frozenset(),
    page: int = 0,
    annotation_pen: float = 0.0,
    soft_exclude_object_ids: frozenset[str] = frozenset(),
) -> tuple[VectorLeader, ...]:
    """Trace one leader per label that has an unambiguous one."""
    cap = max(cap_height, 1e-3)
    pieces = _pieces(objects, exclude_object_ids, soft_exclude_object_ids)
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
    blocks = label_blocks(text_items, cap)

    leaders: list[VectorLeader] = []
    refusals: list[LeaderRefusal] = []
    for t in canonical_sort(list(text_items), key=lambda x: x.canonical_key()):
        if t.state is IdentityState.UNRESOLVED or len(t.text.strip()) < 2:
            continue
        block = blocks.get(t.text_id, t.bbox)
        probe = block.expanded(attach)
        inner = block.expanded(-0.1)
        label_pen = _label_pen(pieces, index, block) or annotation_pen
        starts: list[tuple[float, int, Pt, Pt]] = []
        for i in index.query_box(probe):
            piece = pieces[i]
            if label_pen > 0.0 and piece.width > START_WIDTH_FACTOR * label_pen:
                continue          # a heavier pen than this label is written in
            if piece.lettering and piece.segment.length < MIN_ANNOTATION_LENGTH_CAPS * cap:
                continue          # a character stroke, not a line going anywhere
            for near, far in ((piece.segment.a, piece.segment.b),
                              (piece.segment.b, piece.segment.a)):
                if not probe.contains_point(near) or inner.contains_point(far):
                    continue
                starts.append((qs(_box_distance(block, near)), i, near, far))
        if not starts:
            refusals.append(LeaderRefusal(t.text_id, t.text, "NO_STROKE_TOUCHES_THE_LABEL"))
            continue
        # Several strokes touch a label: its own rules, its characters, and the
        # leader.  Which one is the leader is not decided by which is nearest -
        # the rule under the text is nearer than anything - but by which one
        # actually goes somewhere: each candidate is traced, and the trace that
        # ends furthest from the label block is the leader.  Two candidates that
        # go equally far in different directions are not a statement, and the
        # label is left without a leader.
        starts.sort(key=lambda s: (s[0], pieces[s[1]].segment.angle, s[2], s[3]))
        attempts: list[tuple[float, float, float, int, list, list[str], int]] = []
        seen_starts: set[tuple[int, Pt]] = set()
        for _distance, start_index, root, far in starts[:MAX_START_CANDIDATES]:
            if (start_index, root) in seen_starts:
                continue
            seen_starts.add((start_index, root))
            polyline, object_ids, hops = _follow(pieces, endpoints, start_index, root, far)
            reach = _box_distance(block, polyline[-1])
            if pieces[start_index].lettering and reach < ESCAPE_CAPS * cap:
                continue          # a rule under the label, not a leader from it
            length = sum(dist(polyline[i], polyline[i + 1]) for i in range(len(polyline) - 1))
            # A block can hold two labels, each with its own leader, and the two
            # leaders often run the same way to the same place.  Which one is
            # *this* line's is settled by where each starts: the leader leaves
            # from this line's own rule, not from the line above it.
            own = _box_distance(t.bbox, root)
            attempts.append((qs(own), qs(reach), qs(length), start_index, polyline,
                             object_ids, hops))
        if not attempts:
            continue
        attempts = [a for a in attempts if a[1] > attach]
        if not attempts:
            refusals.append(LeaderRefusal(t.text_id, t.text, "NOTHING_LEFT_THE_LABEL",
                                          (("startsTried", float(len(starts))),)))
            continue
        # Where more than one line leaves a label, the tracer does not choose.
        # Which of them is the leader is a question the *chain* answers - a
        # leader is the line that reaches pipe geometry, and only the
        # attachment stage knows that.  Refusing here discarded most of the
        # labels on a sheet that writes a level under every code, because a
        # neighbouring label's leader passes beside each of them.  Ties are
        # still refused, but where they matter: at the pipe.
        attempts.sort(key=lambda a: (-a[1], a[0], -a[2], pieces[a[3]].object_id))
        chosen: list[tuple] = []
        for attempt in attempts:
            if len(chosen) >= MAX_LEADERS_PER_LABEL:
                break
            if any(
                _turn(pieces[attempt[3]].segment, pieces[other[3]].segment) <= 5.0
                and abs(attempt[1] - other[1]) <= 0.05 * max(other[1], 1.0)
                for other in chosen
            ):
                continue                  # the same line again, from its other end
            chosen.append(attempt)
        for _own, _reach, length, _index, polyline, object_ids, hops in chosen:
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
        if not chosen:
            refusals.append(LeaderRefusal(t.text_id, t.text, "NOTHING_LEFT_THE_LABEL"))
    trace_leaders.refusals = tuple(
        canonical_sort(refusals, key=lambda r: (r.text, r.text_id, r.reason))
    )
    return tuple(canonical_sort(leaders, key=lambda l: l.canonical_key()))


def _label_pen(pieces: Sequence[_Piece], index: "SpatialIndex[int]", block: BBox) -> float:
    """The pen the label itself is written with.

    Taken from the strokes that lie wholly inside the label block - its
    characters and its rules - so it is a fact about this label rather than a
    constant about drawings.
    """
    widths = [
        pieces[i].width
        for i in index.query_box(block)
        if block.contains_point(pieces[i].segment.a) and block.contains_point(pieces[i].segment.b)
        and pieces[i].width > 0.0
    ]
    if not widths:
        return 0.0
    widths.sort()
    return widths[len(widths) // 2]


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
