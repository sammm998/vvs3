"""Pure geometric primitives.

Everything here is side-effect free and independent of input ordering.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from ..canonical import qc

Pt = tuple[float, float]


@dataclass(frozen=True, slots=True)
class BBox:
    x0: float
    y0: float
    x1: float
    y1: float

    @staticmethod
    def from_points(points: Iterable[Sequence[float]]) -> "BBox":
        xs: list[float] = []
        ys: list[float] = []
        for p in points:
            xs.append(float(p[0]))
            ys.append(float(p[1]))
        if not xs:
            raise ValueError("empty point set")
        return BBox(min(xs), min(ys), max(xs), max(ys))

    @staticmethod
    def union_all(boxes: Iterable["BBox"]) -> "BBox":
        boxes = list(boxes)
        if not boxes:
            raise ValueError("empty box set")
        return BBox(
            min(b.x0 for b in boxes),
            min(b.y0 for b in boxes),
            max(b.x1 for b in boxes),
            max(b.y1 for b in boxes),
        )

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    @property
    def center(self) -> Pt:
        return ((self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0)

    def expanded(self, m: float) -> "BBox":
        return BBox(self.x0 - m, self.y0 - m, self.x1 + m, self.y1 + m)

    def contains_point(self, p: Sequence[float]) -> bool:
        return self.x0 <= p[0] <= self.x1 and self.y0 <= p[1] <= self.y1

    def contains_box(self, o: "BBox") -> bool:
        return self.x0 <= o.x0 and self.y0 <= o.y0 and self.x1 >= o.x1 and self.y1 >= o.y1

    def intersects(self, o: "BBox") -> bool:
        return not (o.x0 > self.x1 or o.x1 < self.x0 or o.y0 > self.y1 or o.y1 < self.y0)

    def intersection_area(self, o: "BBox") -> float:
        w = min(self.x1, o.x1) - max(self.x0, o.x0)
        h = min(self.y1, o.y1) - max(self.y0, o.y0)
        if w <= 0 or h <= 0:
            return 0.0
        return w * h

    def distance_to_point(self, p: Sequence[float]) -> float:
        dx = max(self.x0 - p[0], 0.0, p[0] - self.x1)
        dy = max(self.y0 - p[1], 0.0, p[1] - self.y1)
        return math.hypot(dx, dy)

    def key(self) -> tuple[float, float, float, float]:
        return (qc(self.x0), qc(self.y0), qc(self.x1), qc(self.y1))

    def to_canonical(self) -> list[float]:
        return [qc(self.x0), qc(self.y0), qc(self.x1), qc(self.y1)]


@dataclass(frozen=True, slots=True)
class Segment:
    a: Pt
    b: Pt

    @property
    def length(self) -> float:
        return math.hypot(self.b[0] - self.a[0], self.b[1] - self.a[1])

    @property
    def midpoint(self) -> Pt:
        return ((self.a[0] + self.b[0]) / 2.0, (self.a[1] + self.b[1]) / 2.0)

    @property
    def bbox(self) -> BBox:
        return BBox(
            min(self.a[0], self.b[0]),
            min(self.a[1], self.b[1]),
            max(self.a[0], self.b[0]),
            max(self.a[1], self.b[1]),
        )

    @property
    def angle(self) -> float:
        """Undirected orientation in [0, pi)."""
        return normalise_angle(math.atan2(self.b[1] - self.a[1], self.b[0] - self.a[0]))

    @property
    def unit(self) -> Pt:
        ln = self.length
        if ln == 0.0:
            return (0.0, 0.0)
        return ((self.b[0] - self.a[0]) / ln, (self.b[1] - self.a[1]) / ln)

    def reversed(self) -> "Segment":
        return Segment(self.b, self.a)

    def canonical(self) -> "Segment":
        ka = (qc(self.a[0]), qc(self.a[1]))
        kb = (qc(self.b[0]), qc(self.b[1]))
        return Segment(ka, kb) if ka <= kb else Segment(kb, ka)

    def key(self) -> tuple[tuple[float, float], tuple[float, float]]:
        c = self.canonical()
        return (c.a, c.b)


def dist(a: Sequence[float], b: Sequence[float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def normalise_angle(theta: float) -> float:
    """Fold a direction onto the undirected orientation range [0, pi)."""
    t = theta % math.pi
    if t < 0:
        t += math.pi
    if abs(t - math.pi) < 1e-12:
        t = 0.0
    return t


def angle_of(a: Sequence[float], b: Sequence[float]) -> float:
    return normalise_angle(math.atan2(b[1] - a[1], b[0] - a[0]))


def angle_diff(t1: float, t2: float) -> float:
    """Smallest difference between two undirected orientations, in [0, pi/2]."""
    d = abs(normalise_angle(t1) - normalise_angle(t2)) % math.pi
    return min(d, math.pi - d)


def bbox_of_points(points: Iterable[Sequence[float]]) -> BBox:
    return BBox.from_points(points)


def polyline_length(points: Sequence[Sequence[float]]) -> float:
    total = 0.0
    for i in range(len(points) - 1):
        total += dist(points[i], points[i + 1])
    return total


def segments_of_polyline(points: Sequence[Sequence[float]]) -> list[Segment]:
    out: list[Segment] = []
    for i in range(len(points) - 1):
        a = (float(points[i][0]), float(points[i][1]))
        b = (float(points[i + 1][0]), float(points[i + 1][1]))
        if a != b:
            out.append(Segment(a, b))
    return out


def project_scalar(origin: Sequence[float], unit: Sequence[float], p: Sequence[float]) -> float:
    return (p[0] - origin[0]) * unit[0] + (p[1] - origin[1]) * unit[1]


def point_segment_distance(p: Sequence[float], s: Segment) -> float:
    ax, ay = s.a
    bx, by = s.b
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom == 0.0:
        return dist(p, s.a)
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / denom
    t = max(0.0, min(1.0, t))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def segment_overlap_along(s1: Segment, s2: Segment) -> tuple[float, float]:
    """Overlap of ``s2`` projected onto the axis of ``s1``.

    Returns ``(overlap_length, overlap_fraction_of_s1)``.
    """
    u = s1.unit
    if u == (0.0, 0.0):
        return (0.0, 0.0)
    a1 = 0.0
    b1 = s1.length
    a2 = project_scalar(s1.a, u, s2.a)
    b2 = project_scalar(s1.a, u, s2.b)
    lo2, hi2 = (a2, b2) if a2 <= b2 else (b2, a2)
    lo = max(a1, lo2)
    hi = min(b1, hi2)
    ov = max(0.0, hi - lo)
    return (ov, ov / b1 if b1 > 0 else 0.0)


def perpendicular_offset(s1: Segment, p: Sequence[float]) -> float:
    """Signed perpendicular distance from the infinite line of ``s1`` to ``p``."""
    u = s1.unit
    if u == (0.0, 0.0):
        return 0.0
    nx, ny = -u[1], u[0]
    return (p[0] - s1.a[0]) * nx + (p[1] - s1.a[1]) * ny


def segment_intersection(s1: Segment, s2: Segment, eps: float = 1e-9) -> Pt | None:
    x1, y1 = s1.a
    x2, y2 = s1.b
    x3, y3 = s2.a
    x4, y4 = s2.b
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < eps:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
    u = ((x1 - x3) * (y1 - y2) - (y1 - y3) * (x1 - x2)) / den
    if -eps <= t <= 1 + eps and -eps <= u <= 1 + eps:
        return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))
    return None
