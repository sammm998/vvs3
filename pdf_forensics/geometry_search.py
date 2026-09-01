"""Geometric searches over the segment model.

Every question a later stage asks about linework is asked here, and each one is
answered from the geometry itself: what runs parallel to this, what continues
it, what touches it, what crosses it, what stands vertically.  None of these
functions knows what a pipe is.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from .canonical import entity_id, q, qa, sort_canonical
from .model import Segment
from .spatial_index import SpatialIndex, bbox_distance, expand

Point = tuple[float, float]


def seg_bbox(segment: Segment) -> tuple[float, float, float, float]:
    return (min(segment.a[0], segment.b[0]), min(segment.a[1], segment.b[1]),
            max(segment.a[0], segment.b[0]), max(segment.a[1], segment.b[1]))


def direction(segment: Segment) -> Point:
    dx, dy = segment.b[0] - segment.a[0], segment.b[1] - segment.a[1]
    length = math.hypot(dx, dy) or 1.0
    return (dx / length, dy / length)


def angle_difference(a: float, b: float) -> float:
    """Difference between two direction-independent angles, in degrees."""
    diff = abs(a - b) % 180.0
    return min(diff, 180.0 - diff)


def point_line_distance(point: Sequence[float], a: Sequence[float], b: Sequence[float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return math.dist(point, a)
    return abs((point[0] - a[0]) * dy - (point[1] - a[1]) * dx) / length


def point_segment_distance(point: Sequence[float], a: Sequence[float], b: Sequence[float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-12:
        return math.dist(point, a)
    t = max(0.0, min(1.0, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length_sq))
    return math.dist(point, (a[0] + t * dx, a[1] + t * dy))


def segment_distance(s: Segment, t: Segment) -> float:
    if segments_cross(s, t) is not None:
        return 0.0
    return min(
        point_segment_distance(s.a, t.a, t.b),
        point_segment_distance(s.b, t.a, t.b),
        point_segment_distance(t.a, s.a, s.b),
        point_segment_distance(t.b, s.a, s.b),
    )


def segments_cross(s: Segment, t: Segment) -> Optional[Point]:
    """Intersection point of two segments, or None."""
    x1, y1 = s.a; x2, y2 = s.b
    x3, y3 = t.a; x4, y4 = t.b
    denominator = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denominator) < 1e-12:
        return None
    ua = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denominator
    ub = ((x1 - x3) * (y1 - y2) - (y1 - y3) * (x1 - x2)) / denominator
    if -1e-9 <= ua <= 1 + 1e-9 and -1e-9 <= ub <= 1 + 1e-9:
        return (q(x1 + ua * (x2 - x1)), q(y1 + ua * (y2 - y1)))
    return None


def projection_overlap(s: Segment, t: Segment) -> float:
    """How much of the shorter segment lies beside the other, 0..1."""
    ux, uy = direction(s)
    def project(point):
        return (point[0] - s.a[0]) * ux + (point[1] - s.a[1]) * uy
    s0, s1 = 0.0, s.length
    t0, t1 = sorted((project(t.a), project(t.b)))
    overlap = max(0.0, min(s1, t1) - max(s0, t0))
    shorter = max(1e-9, min(s.length, t.length))
    return q(min(1.0, overlap / shorter))


class GeometryModel:
    """Indexed segments plus the searches the pipeline needs."""

    def __init__(self, segments: Sequence[Segment]) -> None:
        self.segments = sort_canonical(segments, key=lambda s: (s.page, s.a, s.b, s.segment_id))
        self.by_id = {s.segment_id: s for s in self.segments}
        self.index = SpatialIndex([(s.segment_id, s.page, seg_bbox(s)) for s in self.segments])
        self.endpoint_index = SpatialIndex(
            [(f"{s.segment_id}#a", s.page, (s.a[0], s.a[1], s.a[0], s.a[1])) for s in self.segments]
            + [(f"{s.segment_id}#b", s.page, (s.b[0], s.b[1], s.b[0], s.b[1])) for s in self.segments]
        )

    # -- basic region searches -------------------------------------------
    def in_region(self, page: int, bbox: Sequence[float], pad: float = 0.0) -> list[Segment]:
        return [self.by_id[k] for k in self.index.intersecting_bbox(page, bbox, pad)]

    def near_point(self, page: int, point: Sequence[float], radius: float) -> list[Segment]:
        candidates = [self.by_id[k] for k in self.index.near_point(page, point, radius)]
        return [s for s in candidates
                if point_segment_distance(point, s.a, s.b) <= radius + 1e-9]

    def near_bbox(self, page: int, bbox: Sequence[float], distance: float) -> list[Segment]:
        return [self.by_id[k] for k in self.index.within_distance(page, bbox, distance)]

    # -- relational searches ---------------------------------------------
    def parallel_to(self, segment: Segment, angle_tol: float = 2.0,
                    max_distance: float = 40.0, min_overlap: float = 0.25) -> list[Segment]:
        out = []
        for other in self.near_bbox(segment.page, seg_bbox(segment), max_distance):
            if other.segment_id == segment.segment_id:
                continue
            if angle_difference(segment.angle, other.angle) > angle_tol:
                continue
            if projection_overlap(segment, other) < min_overlap:
                continue
            if segment_distance(segment, other) > max_distance:
                continue
            out.append(other)
        return sort_canonical(out, key=lambda s: (s.a, s.b, s.segment_id))

    def collinear_with(self, segment: Segment, angle_tol: float = 2.0,
                       offset_tol: float = 0.6, max_gap: float = 24.0) -> list[Segment]:
        out = []
        for other in self.near_bbox(segment.page, seg_bbox(segment), max_gap):
            if other.segment_id == segment.segment_id:
                continue
            if angle_difference(segment.angle, other.angle) > angle_tol:
                continue
            offset = max(point_line_distance(other.a, segment.a, segment.b),
                         point_line_distance(other.b, segment.a, segment.b))
            if offset > offset_tol:
                continue
            out.append(other)
        return sort_canonical(out, key=lambda s: (s.a, s.b, s.segment_id))

    def connected_to(self, segment: Segment, tolerance: float = 0.6) -> list[Segment]:
        out: dict[str, Segment] = {}
        for point in (segment.a, segment.b):
            for key in self.endpoint_index.near_point(segment.page, point, tolerance):
                other_id = key.split("#")[0]
                if other_id != segment.segment_id:
                    out[other_id] = self.by_id[other_id]
        return [out[k] for k in sorted(out)]

    def continuing_from(self, page: int, point: Sequence[float], heading: Sequence[float],
                        tolerance: float = 1.5, angle_tol: float = 12.0,
                        reach: float = 40.0) -> list[Segment]:
        """Segments that carry on from a point in roughly a given direction."""
        heading_angle = qa(math.degrees(math.atan2(heading[1], heading[0])) % 180.0) % 180.0
        out = []
        for segment in self.near_point(page, point, reach):
            if angle_difference(segment.angle, heading_angle) > angle_tol:
                continue
            near_end = min(math.dist(point, segment.a), math.dist(point, segment.b))
            if near_end > reach:
                continue
            forward = ((segment.a[0] + segment.b[0]) / 2.0 - point[0]) * heading[0] + \
                      ((segment.a[1] + segment.b[1]) / 2.0 - point[1]) * heading[1]
            if forward <= 0:
                continue
            if point_segment_distance(point, segment.a, segment.b) > max(tolerance, near_end):
                continue
            out.append(segment)
        return sort_canonical(out, key=lambda s: (s.a, s.b, s.segment_id))

    def intersections(self, page: int, bbox: Sequence[float]) -> list[dict]:
        found: dict[tuple, dict] = {}
        segments = self.in_region(page, bbox)
        for segment in segments:
            for other in self.near_bbox(page, seg_bbox(segment), 0.5):
                if other.segment_id <= segment.segment_id:
                    continue
                point = segments_cross(segment, other)
                if point is None:
                    continue
                found[(point, segment.segment_id, other.segment_id)] = {
                    "point": list(point),
                    "segmentIds": [segment.segment_id, other.segment_id],
                }
        return [found[k] for k in sorted(found)]

    def vertical(self, page: Optional[int] = None, tolerance: float = 2.0) -> list[Segment]:
        return [s for s in self.segments
                if (page is None or s.page == page) and angle_difference(s.angle, 90.0) <= tolerance]

    def horizontal(self, page: Optional[int] = None, tolerance: float = 2.0) -> list[Segment]:
        return [s for s in self.segments
                if (page is None or s.page == page) and angle_difference(s.angle, 0.0) <= tolerance]

    def angle_histogram(self, bucket: float = 5.0) -> dict[str, int]:
        hist: dict[str, int] = {}
        for s in self.segments:
            key = f"{int(s.angle // bucket) * bucket:.0f}"
            hist[key] = hist.get(key, 0) + 1
        return {k: hist[k] for k in sorted(hist, key=float)}

    def to_json(self) -> dict:
        return {
            "segments": len(self.segments),
            "vertical": len(self.vertical()),
            "horizontal": len(self.horizontal()),
            "angleHistogram": self.angle_histogram(),
            "index": self.index.to_json(),
        }
