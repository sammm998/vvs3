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
    max_fragment_diag_ratio: float = 0.02   # of page diagonal
    min_fragment_extent_pt: float = 0.05
    cap_bin_pt: float = 0.25                # cap-height histogram resolution
    cap_min_pt: float = 1.2                 # ignore sub-stroke bins below this
    cap_fallback_percentile: float = 0.90
    # Measured on the reference sheet: the strokes of one character touch (gap
    # ~0) while neighbouring characters sit 0.16-0.20 cap apart, so any
    # threshold in between recovers the characters exactly - at 0.12 cap the
    # label "S3-R8-110" splits into precisely its nine characters, and at 0.20
    # into one blob.
    char_gap_ratio: float = 0.10            # of cap, joining a character's strokes
    furniture_extent_ratio: float = 2.6     # of cap, above which it is a rule or box
    line_band_ratio: float = 1.35           # of cap, the height a line may occupy
    line_gap_ratio: float = 1.10            # of cap, gap between characters
    line_containment_ratio: float = 0.18    # of cap, slack when containing a character
    line_local_floor_ratio: float = 0.55    # of cap, floor for the local height scale
    stack_gap_ratio: float = 0.55           # of cap, rejoining : ; i j
    stack_overlap_ratio: float = 0.45       # of the narrower part's width
    min_cap_height_pt: float = 1.2
    max_cap_height_pt: float = 60.0
    solid_dot_cap_ratio: float = 0.25


def _fragment_extent(o: VectorObject) -> float:
    return max(o.bbox.width, o.bbox.height)


def _principal_angle(points: Sequence[Pt]) -> float:
    """Undirected principal axis of a point set, in radians, folded to [0, pi).

    Closed form for the 2x2 symmetric covariance, so there is no eigensolver
    and the result is bit-identical on every platform.
    """
    arr = np.asarray(points, dtype=np.float64)
    if len(arr) < 2:
        return 0.0
    centred = arr - arr.mean(axis=0)
    cov = centred.T @ centred
    a, b, d = float(cov[0, 0]), float(cov[0, 1]), float(cov[1, 1])
    return (0.5 * math.atan2(2.0 * b, a - d)) % math.pi


def estimate_cap_height(fragments: Sequence[VectorObject], cfg: SegmentationConfig) -> float:
    """The drawing's text cap height, as the mode of the fragment heights.

    A CAD exporter splits each character into several sub-strokes, so the
    *median* fragment is far shorter than a character and a percentile is
    easily dragged around by whatever else is on the sheet.  Lettering is
    repetitive, though, so the full-height stems form a sharp peak: on the
    reference sheet 1 597 fragments sit in the 6.25 pt bin against 1 041 in the
    next-largest.  Bins below ``cap_min_pt`` are ignored because they hold the
    sub-strokes, not the stems.
    """
    if not fragments:
        return cfg.min_cap_height_pt
    bins: dict[int, int] = {}
    for o in fragments:
        h = o.bbox.height
        if h < cfg.cap_min_pt:
            continue
        b = int(h / cfg.cap_bin_pt)
        bins[b] = bins.get(b, 0) + 1
    if bins:
        best = min(bins, key=lambda b: (-bins[b], b))
        return max((best + 0.5) * cfg.cap_bin_pt, cfg.min_cap_height_pt)
    heights = sorted(_fragment_extent(o) for o in fragments)
    idx = min(len(heights) - 1, int(len(heights) * cfg.cap_fallback_percentile))
    return max(heights[idx], cfg.min_cap_height_pt)


def _select_fragments(
    objects: Sequence[VectorObject], page_box: BBox, cfg: SegmentationConfig
) -> list[VectorObject]:
    max_diag = math.hypot(page_box.width, page_box.height) * cfg.max_fragment_diag_ratio
    frags = [
        o
        for o in objects
        if o.is_stroked
        and math.hypot(o.bbox.width, o.bbox.height) <= max_diag
        and _fragment_extent(o) >= cfg.min_fragment_extent_pt
    ]
    return canonical_sort(frags, key=lambda o: o.canonical_key())


def _assemble_characters(
    frags: Sequence[VectorObject], cap: float, cfg: SegmentationConfig
) -> list[list[VectorObject]]:
    """Join the strokes of one character.

    The gap inside a character is a small fraction of the cap height while the
    gap between characters is several times larger, so a tight proximity join
    recovers characters exactly - and, unlike a loose blob, it cannot swallow
    the neighbouring label.
    """
    tol = cfg.char_gap_ratio * cap
    index: SpatialIndex[int] = SpatialIndex.for_items(
        [(o.bbox.expanded(tol), i) for i, o in enumerate(frags)]
    )
    edges: list[tuple[int, int]] = []
    for i, o in enumerate(frags):
        window = o.bbox.expanded(tol)
        for j in index.query_box(window):
            if j <= i:
                continue
            # Expand one box only.  Expanding both doubles the effective
            # tolerance, which is enough to fuse adjacent characters: at a
            # nominal 0.10 cap it joined the A and the L of "SKALA".
            if window.intersects(frags[j].bbox):
                edges.append((i, j))
    return [[frags[i] for i in comp] for comp in connected_components(len(frags), edges)]


def _merge_stacked_parts(
    chars: list[tuple[BBox, list[VectorObject]]], cap: float, cfg: SegmentationConfig
) -> list[tuple[BBox, list[VectorObject]]]:
    """Rejoin the parts of a character written above one another.

    A colon is two dots, an ``i`` is a stem and a dot: vertically separated by
    more than the gap that joins a character's strokes, so the tight proximity
    pass splits them.  They are put back together when they line up
    horizontally and sit within half a cap of each other vertically.
    """
    gap_tol = cfg.stack_gap_ratio * cap
    index: SpatialIndex[int] = SpatialIndex.for_items(
        [(b.expanded(gap_tol), i) for i, (b, _o) in enumerate(chars)]
    )
    edges: list[tuple[int, int]] = []
    for i, (bi, _oi) in enumerate(chars):
        for j in index.query_box(bi.expanded(gap_tol)):
            if j <= i:
                continue
            bj = chars[j][0]
            gap = max(bi.y0, bj.y0) - min(bi.y1, bj.y1)
            if gap > gap_tol:
                continue
            overlap = min(bi.x1, bj.x1) - max(bi.x0, bj.x0)
            narrower = min(bi.width, bj.width)
            if narrower <= 0 or overlap / narrower < cfg.stack_overlap_ratio:
                continue
            if max(bi.y1, bj.y1) - min(bi.y0, bj.y0) > cap * 1.25:
                continue
            edges.append((i, j))
    out: list[tuple[BBox, list[VectorObject]]] = []
    for comp in connected_components(len(chars), edges):
        members = [o for k in comp for o in chars[k][1]]
        out.append((BBox.union_all([chars[k][0] for k in comp]), members))
    return out


def _assemble_lines(
    chars: Sequence[tuple[BBox, list[VectorObject]]], cap: float, cfg: SegmentationConfig
) -> list[list[tuple[BBox, list[VectorObject]]]]:
    """Join characters that share a line of text.

    Two characters belong to the same line when they overlap vertically, both
    fit inside a band about one cap high, and the gap between them is at most
    about one cap wide.  A band rather than a shared baseline is what makes
    this work: a hyphen sits at mid height and has no baseline at all, and
    testing baselines silently drops every separator in the drawing.  The band
    is what separates two labels stacked a few points apart, which plain
    proximity cannot - that distance is the same as the distance between
    neighbouring characters.
    """
    # Tolerances scale with the characters actually being compared, not with
    # the sheet-wide cap: a drawing mixes text sizes, and judging a 5.6 pt
    # scale-bar label by a 7 pt cap merges it with its neighbour.
    height_floor = cfg.line_containment_ratio * cap
    local_floor = cfg.line_local_floor_ratio * cap
    window = max(cfg.line_gap_ratio, cfg.line_band_ratio) * cap
    index: SpatialIndex[int] = SpatialIndex.for_items(
        [(b.expanded(window), i) for i, (b, _o) in enumerate(chars)]
    )
    edges: list[tuple[int, int]] = []
    for i, (bi, _oi) in enumerate(chars):
        for j in index.query_box(bi.expanded(window)):
            if j <= i:
                continue
            bj = chars[j][0]
            local = max(bi.height, bj.height, local_floor)
            if max(bi.y1, bj.y1) - min(bi.y0, bj.y0) > cfg.line_band_ratio * local:
                continue
            # The shorter character must sit inside the taller one's span.  An
            # overlap *fraction* cannot be used: a hyphen has zero height, so
            # its overlap with any letter is exactly zero and every separator
            # in the drawing would be dropped from its line.
            lo, hi = (bi, bj) if bi.height >= bj.height else (bj, bi)
            if hi.y0 < lo.y0 - height_floor or hi.y1 > lo.y1 + height_floor:
                continue
            if max(bi.x0, bj.x0) - min(bi.x1, bj.x1) > cfg.line_gap_ratio * local:
                continue
            edges.append((i, j))
    return [[chars[i] for i in comp] for comp in connected_components(len(chars), edges)]


def segment_glyphs(
    objects: Sequence[VectorObject],
    page_box: BBox,
    cfg: SegmentationConfig | None = None,
) -> GlyphSegmentation:
    cfg = cfg or SegmentationConfig()
    frags = _select_fragments(objects, page_box, cfg)
    if not frags:
        return GlyphSegmentation((), frozenset())

    cap = estimate_cap_height(frags, cfg)

    characters: list[tuple[BBox, list[VectorObject]]] = []
    furniture_limit = cfg.furniture_extent_ratio * cap
    for group in _assemble_characters(frags, cap, cfg):
        box = BBox.union_all([o.bbox for o in group])
        # A rule, a box edge or a leader tail is far wider or taller than any
        # character; left in, it spans every character of a label in projection
        # and the whole label collapses into a single glyph.
        if max(box.width, box.height) > furniture_limit:
            continue
        characters.append((box, group))
    if not characters:
        return GlyphSegmentation((), frozenset())
    characters = _merge_stacked_parts(characters, cap, cfg)
    characters = canonical_sort(characters, key=lambda c: (c[0].key(), len(c[1])))

    lines: list[TextLine] = []
    used: set[str] = set()
    for group in _assemble_lines(characters, cap, cfg):
        ordered = sorted(group, key=lambda c: (qc(c[0].x0), c[0].key()))
        glyphs: list[GlyphGroup] = []
        line_cap = max(
            (b.height for b, _o in ordered if b.height >= 0.5 * cap), default=cap
        )
        for order, (box, members) in enumerate(ordered):
            tiny_closed = all(o.closed for o in members) and box.height <= cfg.solid_dot_cap_ratio * line_cap
            glyphs.append(
                GlyphGroup(
                    object_ids=tuple(sorted(o.object_id for o in members)),
                    polylines=tuple(tuple(o.points) for o in members),
                    bbox=box,
                    filled=any(o.is_filled for o in members) or tiny_closed,
                    order=order,
                )
            )
        box = BBox.union_all([g.bbox for g in glyphs])
        if not (cfg.min_cap_height_pt <= line_cap <= cfg.max_cap_height_pt):
            continue
        # Small skew is recovered from the character centroids; a line of one
        # character has no measurable direction and is treated as horizontal.
        centroids = [g.bbox.center for g in glyphs]
        theta = _principal_angle(centroids) if len(centroids) > 1 else 0.0
        if theta > math.pi / 2:
            theta -= math.pi
        tall = [g for g in glyphs if g.bbox.height >= 0.7 * line_cap]
        bottoms = sorted(g.bbox.y1 for g in (tall or glyphs))
        baseline_y = bottoms[len(bottoms) // 2]
        lines.append(
            TextLine(
                glyphs=tuple(glyphs),
                bbox=box,
                rotation_deg=math.degrees(theta),
                cap_height=line_cap,
                baseline_offset=box.y1,
                baseline_y=baseline_y,
            )
        )
        for g in glyphs:
            used.update(g.object_ids)

    lines = canonical_sort(lines, key=lambda l: (l.bbox.key(), len(l.glyphs)))
    return GlyphSegmentation(tuple(lines), frozenset(used))
