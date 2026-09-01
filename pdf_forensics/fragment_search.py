"""Fourteenth search: what continues this piece of line.

A CAD export splits one physical run into as many fragments as the drawing had
operators: a tee splits it, a crossing symbol splits it, a dash pattern splits
it into every dash.  This module joins fragments back together, and only for
reasons the geometry supports - the pieces are collinear and the gap between
them is small against their own scale, or they meet end to end at a shared
point, or they belong to one dash chain with a regular rhythm.

Length is never a reason to join or to prefer anything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from .canonical import entity_id, q, sort_canonical, undirected
from .geometry_search import (angle_difference, point_line_distance, seg_bbox)
from .spatial_index import SpatialIndex

Point = tuple[float, float]
Polyline = tuple[Point, ...]


@dataclass
class Fragment:
    """A straight piece of a possible pipe, with where it came from."""

    fragment_id: str
    page: int
    a: Point
    b: Point
    width: float
    style_key: str
    kind: str
    separation: Optional[float]
    segment_ids: tuple[str, ...]
    source_object_ids: tuple[str, ...]
    dashed: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def length(self) -> float:
        return q(math.dist(self.a, self.b))

    @property
    def angle(self) -> float:
        return q(math.degrees(math.atan2(self.b[1] - self.a[1], self.b[0] - self.a[0])) % 180.0)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (min(self.a[0], self.b[0]), min(self.a[1], self.b[1]),
                max(self.a[0], self.b[0]), max(self.a[1], self.b[1]))


def make_fragment(page: int, a: Point, b: Point, width: float, style_key: str, kind: str,
                  separation: Optional[float], segment_ids: Sequence[str],
                  source_object_ids: Sequence[str], dashed: bool = False,
                  evidence: Optional[dict] = None) -> Fragment:
    geom = undirected([a, b])
    payload = {"p": page, "g": [list(geom[0]), list(geom[1])], "k": kind,
               "s": q(separation) if separation is not None else None}
    return Fragment(
        fragment_id=entity_id("frag", payload),
        page=page,
        a=geom[0],
        b=geom[1],
        width=q(width),
        style_key=style_key,
        kind=kind,
        separation=q(separation) if separation is not None else None,
        segment_ids=tuple(sorted(set(segment_ids))),
        source_object_ids=tuple(sorted(set(source_object_ids))),
        dashed=dashed,
        evidence=evidence or {},
    )


def _compatible(a: Fragment, b: Fragment, angle_tol: float, separation_tol: float) -> bool:
    if a.page != b.page or a.kind != b.kind:
        return False
    if angle_difference(a.angle, b.angle) > angle_tol:
        return False
    if a.separation is not None and b.separation is not None:
        if abs(a.separation - b.separation) > separation_tol:
            return False
    elif (a.separation is None) != (b.separation is None):
        return False
    return True


def continuation_search(fragments: Sequence[Fragment], angle_tol: float = 4.0,
                        offset_tol: float = 1.2, gap_factor: float = 1.5,
                        separation_tol: float = 1.0) -> list[list[Fragment]]:
    """Group fragments that are pieces of one line.

    ``gap_factor`` is relative to the pieces' own separation (for a double-line
    pipe, the pipe's own diameter) or to the drawing's pen width, never to an
    absolute distance, so the same rule works at 1:50 and at 1:200.
    """
    if not fragments:
        return []
    index = SpatialIndex([(f.fragment_id, f.page, f.bbox) for f in fragments])
    by_id = {f.fragment_id: f for f in fragments}
    parent = {f.fragment_id: f.fragment_id for f in fragments}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rb < ra:
            ra, rb = rb, ra
        parent[rb] = ra

    for fragment in sort_canonical(fragments, key=lambda f: (f.page, f.a, f.b, f.fragment_id)):
        scale = fragment.separation if fragment.separation else max(fragment.width, 1.0)
        reach = max(1.0, gap_factor * max(scale, 1.0))
        for key in index.within_distance(fragment.page, fragment.bbox, reach):
            if key == fragment.fragment_id:
                continue
            other = by_id[key]
            if not _compatible(fragment, other, angle_tol, separation_tol):
                continue
            offset = max(point_line_distance(other.a, fragment.a, fragment.b),
                         point_line_distance(other.b, fragment.a, fragment.b))
            endpoint_gap = min(
                math.dist(fragment.a, other.a), math.dist(fragment.a, other.b),
                math.dist(fragment.b, other.a), math.dist(fragment.b, other.b),
            )
            collinear = offset <= offset_tol and endpoint_gap <= reach
            touching = endpoint_gap <= max(0.75, 0.35 * max(scale, 1.0))
            if collinear or touching:
                union(fragment.fragment_id, key)
    groups: dict[str, list[Fragment]] = {}
    for key in sorted(parent):
        groups.setdefault(find(key), []).append(by_id[key])
    return [sort_canonical(groups[k], key=lambda f: (f.a, f.b, f.fragment_id))
            for k in sorted(groups)]


def chain_polyline(group: Sequence[Fragment], join_tolerance: float = 1.5) -> Polyline:
    """Walk a group of fragments end to end and return one polyline.

    The walk starts at the endpoint that is furthest from the group's centre -
    a geometric choice - so the result does not depend on which fragment came
    first in the list.
    """
    if not group:
        return ()
    if len(group) == 1:
        return (group[0].a, group[0].b)
    points: dict[Point, list[int]] = {}
    for index, fragment in enumerate(group):
        for point in (fragment.a, fragment.b):
            points.setdefault(_snap(point, join_tolerance, points), []).append(index)
    centre = (
        sum(p[0] for p in points) / len(points),
        sum(p[1] for p in points) / len(points),
    )
    ends = sorted(
        (p for p, members in points.items() if len(members) == 1),
        key=lambda p: (-round(math.dist(p, centre), 4), p),
    )
    start = ends[0] if ends else sorted(points)[0]
    used: set[int] = set()
    walk = [start]
    current = start
    while True:
        nxt = None
        for index in sorted(points.get(current, [])):
            if index in used:
                continue
            fragment = group[index]
            other = fragment.b if _close(fragment.a, current, join_tolerance) else fragment.a
            if not (_close(fragment.a, current, join_tolerance)
                    or _close(fragment.b, current, join_tolerance)):
                continue
            used.add(index)
            nxt = _snap(other, join_tolerance, points)
            break
        if nxt is None:
            break
        walk.append(nxt)
        current = nxt
    if len(used) < len(group):
        # a branching group: fall back to the ordered hull of its endpoints so
        # nothing is silently dropped
        remaining = [i for i in range(len(group)) if i not in used]
        for index in remaining:
            for point in (group[index].a, group[index].b):
                snapped = _snap(point, join_tolerance, points)
                if snapped not in walk:
                    walk.append(snapped)
    return tuple(walk)


def _close(a: Sequence[float], b: Sequence[float], tolerance: float) -> bool:
    return math.dist(a, b) <= tolerance


def _snap(point: Point, tolerance: float, known: dict) -> Point:
    for candidate in known:
        if _close(candidate, point, tolerance):
            return candidate
    return (q(point[0]), q(point[1]))


def polyline_length(points: Sequence[Point]) -> float:
    return q(sum(math.dist(points[i], points[i + 1]) for i in range(len(points) - 1)))


def dash_chains(fragments: Sequence[Fragment], gap_multiple: float = 4.0) -> list[list[Fragment]]:
    """Join the dashes of one dashed line, using the rhythm of the dashes."""
    dashed = [f for f in fragments if f.dashed]
    if not dashed:
        return []
    lengths = sorted(f.length for f in dashed)
    typical = lengths[len(lengths) // 2] if lengths else 1.0
    return continuation_search(dashed, angle_tol=3.0, offset_tol=0.8,
                               gap_factor=max(1.0, gap_multiple * typical / max(1.0, typical)))
