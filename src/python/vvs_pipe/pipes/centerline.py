"""Double-line pipe pairing and centerline reconstruction.

A pipe drawn to scale appears as two parallel strokes.  Pairing them is a
matching problem, and the matching must not depend on the order the strokes
happened to appear in the content stream.  So:

1. candidate pairs are found with a spatial index (never an O(n^2) sweep);
2. every candidate pair is scored purely geometrically;
3. the pairs are consumed in a canonical order - score first, then the pair's
   own geometry key - so equal-scoring pairs are ordered by *where they are*,
   not by which stroke was parsed first.

The centerline of a pair is the midline of the mutually overlapping part of
the two strokes; the pipe's width is their perpendicular separation.  A width
is never mistaken for a length: the separation is measured perpendicular to
the shared axis and the length along it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from ..canonical import canonical_sort, qc, qs
from ..geometry.index import SpatialIndex
from ..geometry.primitives import (
    Segment,
    angle_diff,
    perpendicular_offset,
    project_scalar,
    segment_overlap_along,
)

Pt = tuple[float, float]


@dataclass(frozen=True, slots=True)
class SegmentRef:
    segment: Segment
    object_id: str
    stroke_width: float | None
    color: tuple[float, float, float] | None
    dashes: str | None


@dataclass(frozen=True, slots=True)
class DoubleLinePair:
    centerline: tuple[Pt, Pt]
    width_pt: float
    overlap_fraction: float
    score: float
    left: SegmentRef
    right: SegmentRef
    left_index: int
    right_index: int

    def key(self) -> tuple:
        a, b = self.centerline
        ka, kb = (qc(a[0]), qc(a[1])), (qc(b[0]), qc(b[1]))
        return (ka, kb) if ka <= kb else (kb, ka)


@dataclass(frozen=True, slots=True)
class PairingConfig:
    max_angle_diff_rad: float = math.radians(2.0)
    min_overlap_fraction: float = 0.55
    min_width_pt: float = 0.8
    max_width_pt: float = 90.0
    width_relative_tolerance: float = 0.06
    require_same_stroke_width: bool = True
    require_same_colour: bool = True


def _compatible(a: SegmentRef, b: SegmentRef, cfg: PairingConfig) -> bool:
    if a.object_id == b.object_id:
        # Two sides of one stroke are not two walls of a pipe.  Without this a
        # letter such as "M" or "N", or any zig-zag symbol, pairs its own
        # parallel limbs into a phantom pipe.
        return False
    if cfg.require_same_colour and a.color != b.color:
        return False
    if cfg.require_same_stroke_width:
        aw = -1.0 if a.stroke_width is None else qc(a.stroke_width)
        bw = -1.0 if b.stroke_width is None else qc(b.stroke_width)
        if abs(aw - bw) > 1e-3:
            return False
    if (a.dashes or "") != (b.dashes or ""):
        return False
    return True


def pair_double_lines(
    refs: Sequence[SegmentRef], cfg: PairingConfig | None = None
) -> tuple[list[DoubleLinePair], set[str]]:
    """Return accepted pairs and the ids of the segments they consumed."""
    cfg = cfg or PairingConfig()
    refs = canonical_sort(
        list(refs), key=lambda r: (r.segment.key(), r.object_id)
    )
    if len(refs) < 2:
        return [], set()

    index: SpatialIndex[int] = SpatialIndex.for_items(
        [(r.segment.bbox.expanded(cfg.max_width_pt), i) for i, r in enumerate(refs)]
    )
    candidates: list[DoubleLinePair] = []
    seen_pairs: set[tuple[int, int]] = set()
    for i, a in enumerate(refs):
        window = a.segment.bbox.expanded(cfg.max_width_pt)
        for j in index.query_box(window):
            if j == i:
                continue
            lo, hi = (i, j) if i < j else (j, i)
            if (lo, hi) in seen_pairs:
                continue
            seen_pairs.add((lo, hi))
            pair = _score_pair(refs[lo], refs[hi], lo, hi, cfg)
            if pair is not None:
                candidates.append(pair)

    # Consume in a canonical order: best evidence first, then by geometry.
    ordered = canonical_sort(
        candidates, key=lambda p: (-qs(p.score), p.key(), p.left.object_id, p.right.object_id)
    )
    used: set[int] = set()
    accepted: list[DoubleLinePair] = []
    consumed: set[str] = set()
    for p in ordered:
        li, ri = p.left_index, p.right_index
        if li in used or ri in used:
            continue
        used.add(li)
        used.add(ri)
        accepted.append(p)
        consumed.add(p.left.object_id)
        consumed.add(p.right.object_id)
    return canonical_sort(accepted, key=lambda p: p.key()), consumed


def _score_pair(
    a: SegmentRef, b: SegmentRef, ai: int, bi: int, cfg: PairingConfig
) -> DoubleLinePair | None:
    if not _compatible(a, b, cfg):
        return None
    sa, sb = a.segment, b.segment
    if sa.length <= 0 or sb.length <= 0:
        return None
    if angle_diff(sa.angle, sb.angle) > cfg.max_angle_diff_rad:
        return None

    # Perpendicular separation, measured at both ends so a slight fan is caught.
    off_a = perpendicular_offset(sa, sb.a)
    off_b = perpendicular_offset(sa, sb.b)
    if off_a * off_b <= 0 and abs(off_a) > 1e-6 and abs(off_b) > 1e-6:
        return None  # b straddles a's axis: not a parallel neighbour
    width = 0.5 * (abs(off_a) + abs(off_b))
    if not (cfg.min_width_pt <= width <= cfg.max_width_pt):
        return None
    if abs(abs(off_a) - abs(off_b)) > cfg.width_relative_tolerance * max(width, 1e-6) + 0.05:
        return None

    ov1, frac1 = segment_overlap_along(sa, sb)
    ov2, frac2 = segment_overlap_along(sb, sa)
    frac = min(frac1, frac2)
    if frac < cfg.min_overlap_fraction:
        return None

    # Midline of the mutually overlapping part, expressed on a's axis.
    u = sa.unit
    lo = max(0.0, min(project_scalar(sa.a, u, sb.a), project_scalar(sa.a, u, sb.b)))
    hi = min(sa.length, max(project_scalar(sa.a, u, sb.a), project_scalar(sa.a, u, sb.b)))
    if hi - lo <= 1e-9:
        return None
    sign = 1.0 if (off_a + off_b) >= 0 else -1.0
    nx, ny = -u[1], u[0]
    half = sign * width / 2.0
    p0 = (sa.a[0] + u[0] * lo + nx * half, sa.a[1] + u[1] * lo + ny * half)
    p1 = (sa.a[0] + u[0] * hi + nx * half, sa.a[1] + u[1] * hi + ny * half)

    length = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
    slenderness = length / max(width, 1e-6)
    score = (
        0.45 * frac
        + 0.30 * (1.0 - min(1.0, angle_diff(sa.angle, sb.angle) / cfg.max_angle_diff_rad))
        + 0.25 * min(1.0, slenderness / 6.0)
    )
    if a.segment.key() <= b.segment.key():
        left, right, li, ri = a, b, ai, bi
    else:
        left, right, li, ri = b, a, bi, ai
    return DoubleLinePair(
        centerline=(p0, p1),
        width_pt=width,
        overlap_fraction=frac,
        score=score,
        left=left,
        right=right,
        left_index=li,
        right_index=ri,
    )
