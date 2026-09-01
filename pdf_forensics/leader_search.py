"""Eleventh search: leaders.

A designation is tied to a pipe by a line the draughtsman drew on purpose.
Following that line is far stronger evidence than "the nearest pipe", which is
wrong whenever two systems run side by side - exactly the case where being
wrong costs the most.

A leader is traced, not guessed: one end must touch the text, the trace follows
connected geometry while it keeps going in roughly one direction, and the far
end is reported with whatever it lands on.  If nothing is found the answer is
"no leader", which the association stage treats as missing evidence rather than
as permission to pick something nearby.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from .canonical import entity_id, q, sort_canonical
from .geometry_search import (GeometryModel, angle_difference, point_segment_distance,
                              seg_bbox)
from .model import Leader, Segment, TextItem
from .spatial_index import SpatialIndex, bbox_distance, expand

MAX_TRACE_HOPS = 12


def _attach_distance(cap_height: float) -> float:
    return max(1.5, 0.9 * max(cap_height, 1.0))


def _bbox_of_text(item: TextItem) -> tuple[float, float, float, float]:
    return item.bbox


def find_leaders(text_items: Sequence[TextItem], geometry: GeometryModel,
                 eligible_ids: Optional[set[str]] = None,
                 reach_factors: Sequence[float] = (2.5, 5.0, 9.0)) -> list[Leader]:
    """Trace a leader for every text item that has one.

    The search is adaptive: it starts close to the text and widens only when it
    found nothing, so a label with a leader beside it is never explained by a
    line on the other side of the room.
    """
    leaders: list[Leader] = []
    for item in sort_canonical(text_items, key=lambda t: (t.page, t.bbox, t.text_id)):
        cap = max(item.cap_height, 1.0)
        found: Optional[Leader] = None
        for factor in reach_factors:
            found = _trace_from_text(item, geometry, cap * factor, eligible_ids)
            if found is not None:
                break
        if found is not None:
            leaders.append(found)
    return sort_canonical(leaders, key=lambda l: (l.page, l.text_end, l.target_end, l.leader_id))


def _trace_from_text(item: TextItem, geometry: GeometryModel, reach: float,
                     eligible_ids: Optional[set[str]]) -> Optional[Leader]:
    cap = max(item.cap_height, 1.0)
    attach = _attach_distance(cap)
    box = item.bbox
    starts: list[tuple[float, Segment, tuple[float, float], tuple[float, float]]] = []
    for segment in geometry.near_bbox(item.page, box, reach):
        if eligible_ids is not None and segment.segment_id not in eligible_ids:
            continue
        if segment.length < 0.35 * cap:
            continue
        for near, far in ((segment.a, segment.b), (segment.b, segment.a)):
            if _inside(box, far, 0.2 * cap):
                continue                                  # both ends in the text: lettering
            distance = _distance_to_box(box, near)
            if distance > attach:
                continue
            if _inside(box, near, -0.05 * cap) and _inside(box, far, -0.05 * cap):
                continue
            starts.append((q(distance), segment, near, far))
    if not starts:
        return None
    starts.sort(key=lambda s: (s[0], s[1].segment_id))
    best = starts[0]
    if len(starts) > 1 and abs(starts[1][0] - best[0]) < 1e-6 and starts[1][1].angle != best[1].angle:
        # two different lines touch the label equally: not a leader we can trust
        return None
    distance, segment, near, far = best
    polyline, used = _follow(geometry, segment, near, far, eligible_ids)
    length = q(sum(math.dist(polyline[i], polyline[i + 1]) for i in range(len(polyline) - 1)))
    if length < 0.5 * cap:
        return None
    payload = {"p": item.page, "t": list(polyline[0]), "e": list(polyline[-1]),
               "x": item.text_id}
    confidence = q(max(0.0, 1.0 - distance / max(attach, 1e-6)) * 0.5 + 0.5)
    return Leader(
        leader_id=entity_id("leader", payload),
        page=item.page,
        polyline=tuple(polyline),
        text_end=polyline[0],
        target_end=polyline[-1],
        length=length,
        segment_ids=tuple(sorted(used)),
        candidate_id=None,
        confidence=confidence,
    )


def _follow(geometry: GeometryModel, segment: Segment, near: tuple[float, float],
            far: tuple[float, float], eligible_ids: Optional[set[str]]) -> tuple[list, set[str]]:
    """Walk on from the far end while the line keeps going one way."""
    polyline = [near, far]
    used = {segment.segment_id}
    current = segment
    tip = far
    for _ in range(MAX_TRACE_HOPS):
        heading = (tip[0] - polyline[-2][0], tip[1] - polyline[-2][1])
        norm = math.hypot(*heading) or 1.0
        heading = (heading[0] / norm, heading[1] / norm)
        options = []
        for other in geometry.connected_to(current, tolerance=0.8):
            if other.segment_id in used:
                continue
            if eligible_ids is not None and other.segment_id not in eligible_ids:
                continue
            if other.width > current.width * 1.6:
                continue                                   # a heavier pen is other linework
            other_near = other.a if math.dist(other.a, tip) <= math.dist(other.b, tip) else other.b
            other_far = other.b if other_near == other.a else other.a
            if math.dist(other_near, tip) > 0.8:
                continue
            turn = angle_difference(current.angle, other.angle)
            options.append((q(turn), other.segment_id, other, other_far))
        if not options:
            break
        options.sort(key=lambda o: (o[0], o[1]))
        if options[0][0] > 65.0:
            break                                          # a right-angle turn ends a leader
        if len(options) > 1 and abs(options[1][0] - options[0][0]) < 1e-6:
            break                                          # a fork: stop rather than choose
        _, _, chosen, tip_next = options[0]
        used.add(chosen.segment_id)
        polyline.append(tip_next)
        current, tip = chosen, tip_next
    return polyline, used


def _inside(box: Sequence[float], point: Sequence[float], pad: float = 0.0) -> bool:
    return (box[0] - pad <= point[0] <= box[2] + pad) and (box[1] - pad <= point[1] <= box[3] + pad)


def _distance_to_box(box: Sequence[float], point: Sequence[float]) -> float:
    dx = max(box[0] - point[0], point[0] - box[2], 0.0)
    dy = max(box[1] - point[1], point[1] - box[3], 0.0)
    return math.hypot(dx, dy)


def leaders_by_text(leaders: Sequence[Leader], text_items: Sequence[TextItem]) -> dict[str, Leader]:
    """Map each text item to its leader, by where the leader starts."""
    index = SpatialIndex([(t.text_id, t.page, t.bbox) for t in text_items])
    by_id = {t.text_id: t for t in text_items}
    out: dict[str, Leader] = {}
    for leader in leaders:
        best: Optional[tuple[float, str]] = None
        for key in index.near_point(leader.page, leader.text_end, 12.0):
            item = by_id[key]
            distance = _distance_to_box(item.bbox, leader.text_end)
            if best is None or (distance, key) < (best[0], best[1]):
                best = (distance, key)
        if best is not None and best[0] <= _attach_distance(by_id[best[1]].cap_height):
            out.setdefault(best[1], leader)
    return {k: out[k] for k in sorted(out)}


def to_json(leaders: Sequence[Leader]) -> dict:
    return {
        "leaders": len(leaders),
        "meanLength": q(sum(l.length for l in leaders) / max(1, len(leaders))),
        "meanConfidence": q(sum(l.confidence for l in leaders) / max(1, len(leaders))),
    }
