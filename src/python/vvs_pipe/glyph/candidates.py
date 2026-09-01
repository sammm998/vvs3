"""Vector objects -> glyph candidates -> text lines.

Nothing in this module knows what the characters will turn out to be.  It
performs the two classical steps of vector-text recovery:

1. **blobbing** - small stroked objects are grouped by adaptive proximity.  On
   a real sheet a blob is usually a whole *label*: several lines of text inside
   a box, with a rule between them;
2. **furniture removal** - inside a blob, a fragment far longer than the blob's
   own typical fragment is a box edge or an underline, not part of a letter.
   Left in, a single underline spans every character in projection and the
   whole label collapses into one glyph;
3. **line splitting** - the remaining fragments are projected onto the axis
   *across* the text and split at empty coverage, separating the lines of a
   multi-line label;
4. **character splitting** - inside each line the objects are projected along
   the text and split wherever coverage is empty.  Non-touching CAD fonts are
   separated exactly by this, with no per-character knowledge at all.

The blob's principal axis also yields the text rotation, so rotated annotation
text is handled by the same code path.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from ..canonical import canonical_sort, qc
from ..geometry.index import SpatialIndex, connected_components
from ..geometry.primitives import BBox
from ..model import VectorObject

Pt = tuple[float, float]


@dataclass(frozen=True, slots=True)
class GlyphGroup:
    """One character's worth of geometry, before recognition."""

    object_ids: tuple[str, ...]
    polylines: tuple[tuple[Pt, ...], ...]
    bbox: BBox
    filled: bool
    order: int  # position along the line axis


@dataclass(frozen=True, slots=True)
class TextLine:
    glyphs: tuple[GlyphGroup, ...]
    bbox: BBox
    rotation_deg: float
    cap_height: float
    baseline_offset: float
    baseline_y: float


@dataclass(frozen=True, slots=True)
class GlyphSegmentation:
    lines: tuple[TextLine, ...]
    fragment_object_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class SegmentationConfig:
    max_fragment_diag_ratio: float = 0.02  # of page diagonal
    min_fragment_extent_pt: float = 0.05
    max_straight_stroke_ratio: float = 2.0  # of the estimated text cap height
    cap_percentile: float = 0.90
    blob_gap_ratio: float = 0.55  # of the larger fragment height
    glyph_split_epsilon_ratio: float = 0.02  # of cap height
    min_cap_height_pt: float = 1.2
    max_cap_height_pt: float = 60.0
    solid_dot_cap_ratio: float = 0.25
    furniture_extent_ratio: float = 4.0     # of the blob's median fragment extent
    furniture_min_fragments: int = 6
    line_split_gap_ratio: float = 0.18      # of the blob's fragment extent scale


def _fragment_height(o: VectorObject) -> float:
    b = o.bbox
    return max(b.height, b.width * 0.25)


def _principal_angle(points: Sequence[Pt]) -> float:
    """Undirected principal axis of a point set, in radians, folded to [0, pi)."""
    arr = np.asarray(points, dtype=np.float64)
    if len(arr) < 2:
        return 0.0
    centred = arr - arr.mean(axis=0)
    cov = centred.T @ centred
    # closed-form principal direction of a 2x2 symmetric matrix - no eig, so
    # the result is bit-identical everywhere
    a, b, d = float(cov[0, 0]), float(cov[0, 1]), float(cov[1, 1])
    theta = 0.5 * math.atan2(2.0 * b, a - d)
    return theta % math.pi


def segment_glyphs(
    objects: Sequence[VectorObject],
    page_box: BBox,
    cfg: SegmentationConfig | None = None,
) -> GlyphSegmentation:
    cfg = cfg or SegmentationConfig()
    page_diag = math.hypot(page_box.width, page_box.height)
    max_diag = page_diag * cfg.max_fragment_diag_ratio

    frags = [
        o
        for o in objects
        if o.is_stroked
        and math.hypot(o.bbox.width, o.bbox.height) <= max_diag
        and max(o.bbox.width, o.bbox.height) >= cfg.min_fragment_extent_pt
    ]
    frags = canonical_sort(frags, key=lambda o: o.canonical_key())
    if not frags:
        return GlyphSegmentation((), frozenset())

    # Estimate the drawing's text cap height.  A *median* fragment height is the
    # wrong statistic: a real CAD exporter splits each character into several
    # sub-strokes, so most fragments are far shorter than the cap and the median
    # lands among them.  On the reference sheet that median is 1.4 pt against a
    # 6.4 pt cap, which would classify every full-height stem as a leader and
    # gut the alphabet.  A high percentile of the fragment *extent*
    # tracks the cap instead, because the largest fragments of a text-bearing
    # sheet are its full-height strokes; the extent rather than the height is
    # used so rotated text is measured the same way.
    heights = sorted(max(o.bbox.width, o.bbox.height) for o in frags)
    cap_estimate = max(
        heights[min(len(heights) - 1, int(len(heights) * cfg.cap_percentile))],
        cfg.min_cap_height_pt,
    )
    floor_h = max(cfg.min_cap_height_pt, 0.5 * cap_estimate)

    # A single straight segment much longer than the local text height is a
    # leader, a hatch or a symbol stroke, not part of a letter.  Excluding it
    # matters because such a segment would otherwise *bridge* a label to the
    # geometry it points at and destroy the blob boundaries.
    max_straight = cfg.max_straight_stroke_ratio * cap_estimate
    frags = [
        o
        for o in frags
        if not (len(o.points) == 2 and o.length > max_straight)
    ]
    if not frags:
        return GlyphSegmentation((), frozenset())

    idx: SpatialIndex[int] = SpatialIndex.for_items(
        [(o.bbox.expanded(cfg.blob_gap_ratio * max(_fragment_height(o), floor_h)), i) for i, o in enumerate(frags)]
    )
    edges: list[tuple[int, int]] = []
    for i, o in enumerate(frags):
        hi = max(_fragment_height(o), floor_h)
        tol_i = cfg.blob_gap_ratio * hi
        window = o.bbox.expanded(tol_i)
        for j in idx.query_box(window):
            if j <= i:
                continue
            oj = frags[j]
            hj = max(_fragment_height(oj), floor_h)
            tol = cfg.blob_gap_ratio * max(hi, hj)
            if o.bbox.expanded(tol).intersects(oj.bbox.expanded(tol)):
                edges.append((i, j))
    comps = connected_components(len(frags), edges)

    lines: list[TextLine] = []
    used: set[str] = set()
    for comp in comps:
        members = [frags[i] for i in comp]

        # Furniture removal: a box edge or an underline is far longer than the
        # strokes of the characters it encloses.
        extents = sorted(max(m.bbox.width, m.bbox.height) for m in members)
        scale = extents[len(extents) // 2] if extents else 0.0
        if len(members) >= cfg.furniture_min_fragments and scale > 0:
            limit = cfg.furniture_extent_ratio * scale
            members = [m for m in members if max(m.bbox.width, m.bbox.height) <= limit]
        if not members:
            continue

        pts: list[Pt] = [p for m in members for p in m.points]
        theta = _principal_angle(pts) if len(members) > 1 else 0.0
        # Fold the undirected principal axis into (-90, 90] so that projection
        # increases in the reading direction for any near-horizontal text.
        if theta > math.pi / 2:
            theta -= math.pi
        ux, uy = math.cos(theta), math.sin(theta)
        vx, vy = -uy, ux

        def proj(p: Pt) -> tuple[float, float]:
            return (p[0] * ux + p[1] * uy, p[0] * vx + p[1] * vy)

        # Line splitting: split across the text where the coverage is empty.
        bands: list[list[VectorObject]] = []
        line_gap = max(cfg.line_split_gap_ratio * max(scale, cfg.min_cap_height_pt), 0.2)
        spans: list[tuple[float, float, VectorObject]] = []
        for m in members:
            ps = [proj(p) for p in m.points]
            spans.append((min(p[1] for p in ps), max(p[1] for p in ps), m))
        spans.sort(key=lambda t: (qc(t[0]), qc(t[1]), t[2].canonical_key()))
        cur_band: list[VectorObject] = []
        band_hi = -math.inf
        for lo, hi, m in spans:
            if cur_band and lo > band_hi + line_gap:
                bands.append(cur_band)
                cur_band = []
                band_hi = -math.inf
            cur_band.append(m)
            band_hi = max(band_hi, hi)
        if cur_band:
            bands.append(cur_band)

        for band in bands:
            band_pts = [proj(p) for m in band for p in m.points]
            cap = max(p[1] for p in band_pts) - min(p[1] for p in band_pts)
            if cap > cfg.max_cap_height_pt:
                continue
            cap = max(cap, cfg.min_cap_height_pt)

            intervals: list[tuple[float, float, VectorObject]] = []
            for m in band:
                ps = [proj(p) for p in m.points]
                intervals.append((min(p[0] for p in ps), max(p[0] for p in ps), m))
            intervals.sort(key=lambda t: (qc(t[0]), qc(t[1]), t[2].canonical_key()))

            eps = cfg.glyph_split_epsilon_ratio * cap
            groups: list[list[VectorObject]] = []
            cur: list[VectorObject] = []
            cur_hi = -math.inf
            for lo, hi, m in intervals:
                if cur and lo > cur_hi + eps:
                    groups.append(cur)
                    cur = []
                    cur_hi = -math.inf
                cur.append(m)
                cur_hi = max(cur_hi, hi)
            if cur:
                groups.append(cur)

            glyphs: list[GlyphGroup] = []
            for order, g in enumerate(groups):
                polys = tuple(tuple(o.points) for o in g)
                gbox = BBox.union_all([o.bbox for o in g])
                # A closed contour far smaller than the cap height reads as
                # solid ink at drawing scale (a period, a comma, a bullet), so
                # it is rasterised filled.  Without this a stroked 1-unit square
                # would present an enclosed hole and never match a full stop.
                tiny_closed = all(o.closed for o in g) and gbox.height <= cfg.solid_dot_cap_ratio * cap
                glyphs.append(
                    GlyphGroup(
                        object_ids=tuple(sorted(o.object_id for o in g)),
                        polylines=polys,
                        bbox=gbox,
                        filled=any(o.is_filled for o in g) or tiny_closed,
                        order=order,
                    )
                )
            if not glyphs:
                continue
            box = BBox.union_all([g.bbox for g in glyphs])
            heights = sorted(g.bbox.height for g in glyphs)
            robust_cap = heights[int(0.75 * (len(heights) - 1))] if heights else 0.0
            if robust_cap >= cfg.min_cap_height_pt:
                cap = robust_cap
            tall = [g for g in glyphs if g.bbox.height >= 0.7 * cap]
            bottoms = sorted((g.bbox.y1 for g in tall) or (g.bbox.y1 for g in glyphs))
            baseline_y = bottoms[len(bottoms) // 2] if bottoms else box.y1
            lines.append(
                TextLine(
                    glyphs=tuple(glyphs),
                    bbox=box,
                    rotation_deg=math.degrees(theta),
                    cap_height=cap,
                    baseline_offset=max(p[1] for p in band_pts),
                    baseline_y=baseline_y,
                )
            )
            for g in glyphs:
                used.update(g.object_ids)

    lines = canonical_sort(lines, key=lambda l: (l.bbox.key(), len(l.glyphs)))
    return GlyphSegmentation(tuple(lines), frozenset(used))
