"""Dashed-line reconstruction.

A hidden or below-slab pipe is drawn with a dashed linetype, and a CAD exporter
usually writes every dash as its own path rather than using a PDF dash pattern -
the reference sheet emits 25 225 drawings whose dash patterns are all ``[] 0``.
Without reassembly there are no pipes at all: only thousands of 10 pt stubs.

Two dashes belong to one line when the gap between their facing ends is small,
lies along both of their outward directions, and the pen has not changed
(width, colour, dash declaration and layer all equal).  The permitted gap is
**derived from the drawing itself, separately for every pen** - the mode of
that pen's own end-to-end gap histogram - so a sheet drawn at a different
scale, or one mixing several linetypes, needs no new constant.  Estimating one
gap for the whole sheet does not work: annotation strokes vastly outnumber
pipe dashes and would set the threshold from lettering.

Ends are matched by *mutual best partner*, exactly as pipe runs are chained, so
the reconstruction cannot depend on the order the dashes were emitted in, and a
branch point breaks the chain instead of picking a direction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from ..canonical import canonical_sort, qc, qs
from ..geometry.index import SpatialIndex
from ..geometry.primitives import BBox, dist
from ..model import VectorObject

Pt = tuple[float, float]


@dataclass(frozen=True, slots=True)
class DashConfig:
    max_probe_gap_pt: float = 40.0
    gap_histogram_bin_pt: float = 0.5
    gap_mode_multiple: float = 2.2
    gap_floor_pt: float = 2.0
    gap_ceiling_pt: float = 24.0
    alignment_tolerance_rad: float = math.radians(14.0)
    touching_pt: float = 0.4
    # Measured on the reference sheet: a dashed pipe chain has at least 5 dashes
    # (10th percentile 5, median 10) while a chain that forms inside lettering
    # has at most 5 (90th percentile 5).  Combined with the span rule below this
    # separates the two populations almost completely.
    min_members: int = 5
    min_gap_samples: int = 12
    min_pen_objects: int = 4
    # A reassembled chain is only accepted as linework if it is far longer than
    # any character could be.  Annotation strokes chain too - they are short
    # collinear runs inside letters - and letting them through would hand the
    # text stage's geometry to the pipe stages.
    min_span_page_ratio: float = 0.008
    # A reassembled dashed line is dead straight: every dash sits on the line
    # joining the chain's ends.  A run of collinear strokes that happens to
    # occur inside lettering wanders off it, so straightness separates linework
    # from text without needing to know the text size first.
    # Straightness is a weak guard, not the discriminator: a pipe run bends at
    # elbows (median deviation 0.12, 99th percentile 0.43) while a chain inside
    # lettering wanders far more (median 0.92).  The cut is set to keep every
    # real run rather than to be tight.
    max_straightness_deviation: float = 0.5  # of the chain span
    # ...and it is long relative to what this pen normally draws.  A pipe pen's
    # objects are dashes and its chains are whole runs; a text pen's objects are
    # sub-strokes and its longest chain is one word.  Scaling the threshold to
    # the pen's own median object size separates the two without needing to know
    # the text height first.
    min_span_object_multiple: float = 8.0


@dataclass(frozen=True, slots=True)
class DashChain:
    polyline: tuple[Pt, ...]
    object_ids: tuple[str, ...]
    stroke_width: float | None
    color: tuple[float, float, float] | None
    dashes: str | None
    layer: str | None
    member_count: int
    bridged_gaps: int
    ink_length_pt: float

    @property
    def length_pt(self) -> float:
        return sum(
            dist(self.polyline[i], self.polyline[i + 1]) for i in range(len(self.polyline) - 1)
        )

    def key(self) -> tuple:
        fwd = tuple((qc(x), qc(y)) for x, y in self.polyline)
        rev = tuple(reversed(fwd))
        return fwd if fwd <= rev else rev


def _pen(o: VectorObject) -> tuple:
    return (
        qc(o.stroke_width) if o.stroke_width is not None else -1.0,
        o.stroke_color or (-1.0, -1.0, -1.0),
        o.dashes or "",
        o.layer or "",
    )


def _ends(o: VectorObject) -> list[tuple[Pt, float]]:
    p = o.points
    return [
        (p[0], math.atan2(p[0][1] - p[1][1], p[0][0] - p[1][0])),
        (p[-1], math.atan2(p[-1][1] - p[-2][1], p[-1][0] - p[-2][0])),
    ]


def _angle_delta(a: float, b: float) -> float:
    return abs(((a - b + math.pi) % (2 * math.pi)) - math.pi)


def estimate_gap(
    objects: Sequence[VectorObject], cfg: DashConfig
) -> tuple[float, float, int]:
    """Derive one pen's permitted bridging gap from its own dash rhythm.

    Returns ``(gap_max, mode, sample_count)``.  The mode of the aligned
    end-to-end gap histogram is that linetype's own gap; anything up to a small
    multiple of it belongs to the same line.  With too few samples to see a
    rhythm the floor is used, which bridges only touching ends.
    """
    ends = [(i, p, d) for i, o in enumerate(objects) for p, d in _ends(o)]
    if len(ends) < 4:
        return cfg.gap_floor_pt, 0.0, 0
    index: SpatialIndex[int] = SpatialIndex.for_items(
        [
            (BBox(p[0], p[1], p[0], p[1]).expanded(cfg.max_probe_gap_pt), k)
            for k, (_i, p, _d) in enumerate(ends)
        ]
    )
    samples: list[float] = []
    for k, (i, p, d) in enumerate(ends):
        for m in index.query_box(BBox(p[0], p[1], p[0], p[1]).expanded(cfg.max_probe_gap_pt)):
            if m <= k:
                continue
            j, q, e = ends[m]
            if j == i:
                continue
            g = dist(p, q)
            if not (cfg.touching_pt < g <= cfg.max_probe_gap_pt):
                continue
            gd = math.atan2(q[1] - p[1], q[0] - p[0])
            if _angle_delta(gd, d) > cfg.alignment_tolerance_rad:
                continue
            if _angle_delta(gd + math.pi, e) > cfg.alignment_tolerance_rad:
                continue
            samples.append(g)
    if len(samples) < cfg.min_gap_samples:
        return cfg.gap_floor_pt, 0.0, len(samples)
    bins: dict[int, int] = {}
    for g in samples:
        b = int(g / cfg.gap_histogram_bin_pt)
        bins[b] = bins.get(b, 0) + 1
    best_bin = min(bins, key=lambda b: (-bins[b], b))
    mode = (best_bin + 0.5) * cfg.gap_histogram_bin_pt
    gap = min(cfg.gap_ceiling_pt, max(cfg.gap_floor_pt, mode * cfg.gap_mode_multiple))
    return gap, mode, len(samples)


def reconstruct_dashes(
    objects: Sequence[VectorObject],
    page_box: BBox,
    cfg: DashConfig | None = None,
) -> tuple[tuple[DashChain, ...], frozenset[str], dict[str, object]]:
    """Reassemble dashed lines. Returns (chains, consumed ids, diagnostics)."""
    cfg = cfg or DashConfig()
    page_span_floor = math.hypot(page_box.width, page_box.height) * cfg.min_span_page_ratio
    usable = canonical_sort(
        [o for o in objects if o.is_stroked and len(o.points) >= 2 and o.length > 0],
        key=lambda o: o.canonical_key(),
    )
    groups: dict[tuple, list[VectorObject]] = {}
    for o in usable:
        groups.setdefault(_pen(o), []).append(o)

    chains: list[DashChain] = []
    consumed: set[str] = set()
    pens: list[dict[str, object]] = []
    for pen in sorted(groups, key=lambda k: (str(k[3]), k[0], str(k[1]), str(k[2]))):
        objs = groups[pen]
        if len(objs) < cfg.min_pen_objects:
            continue
        gap_max, mode, samples = estimate_gap(objs, cfg)
        extents = sorted(max(o.bbox.width, o.bbox.height) for o in objs)
        pen_scale = extents[len(extents) // 2]
        min_span = max(page_span_floor, cfg.min_span_object_multiple * pen_scale)
        pen_chains = [
            c
            for c in _chain_pen(objs, gap_max, cfg)
            if _span(c) >= min_span and _straightness(c) <= cfg.max_straightness_deviation
        ]
        pens.append(
            {
                "strokeWidth": pen[0],
                "layer": pen[3],
                "objects": len(objs),
                "gapMaxPt": qs(gap_max),
                "gapModePt": qs(mode),
                "gapSamples": samples,
                "penScalePt": qs(pen_scale),
                "minSpanPt": qs(min_span),
                "chains": len(pen_chains),
            }
        )
        for c in pen_chains:
            chains.append(c)
            consumed.update(c.object_ids)

    chains = canonical_sort(chains, key=lambda c: c.key())
    diagnostics: dict[str, object] = {
        "pageSpanFloorPt": qs(page_span_floor),
        "chains": len(chains),
        "consumedObjects": len(consumed),
        "pens": pens,
    }
    return tuple(chains), frozenset(consumed), diagnostics


def _span(chain: DashChain) -> float:
    b = BBox.from_points(chain.polyline)
    return math.hypot(b.width, b.height)


def _straightness(chain: DashChain) -> float:
    """Max deviation from the straight line joining the chain's ends, over span."""
    a, b = chain.polyline[0], chain.polyline[-1]
    span = dist(a, b)
    if span <= 1e-9:
        return 1.0
    ux, uy = (b[0] - a[0]) / span, (b[1] - a[1]) / span
    nx, ny = -uy, ux
    worst = 0.0
    for p in chain.polyline:
        worst = max(worst, abs((p[0] - a[0]) * nx + (p[1] - a[1]) * ny))
    return worst / span


def _chain_pen(objs: Sequence[VectorObject], gap_max: float, cfg: DashConfig) -> list[DashChain]:
    ends = [(i, p, d) for i, o in enumerate(objs) for p, d in _ends(o)]
    index: SpatialIndex[int] = SpatialIndex.for_items(
        [
            (BBox(p[0], p[1], p[0], p[1]).expanded(gap_max), k)
            for k, (_i, p, _d) in enumerate(ends)
        ]
    )

    # Best partner for each end, then keep only mutual choices.
    best: dict[int, tuple[float, int] | None] = {}
    for k, (i, p, d) in enumerate(ends):
        candidates: list[tuple[float, int]] = []
        for m in index.query_box(BBox(p[0], p[1], p[0], p[1]).expanded(gap_max)):
            if m == k:
                continue
            j, q, e = ends[m]
            if j == i:
                continue
            g = dist(p, q)
            if g > gap_max:
                continue
            if g > cfg.touching_pt:
                gd = math.atan2(q[1] - p[1], q[0] - p[0])
                if _angle_delta(gd, d) > cfg.alignment_tolerance_rad:
                    continue
                if _angle_delta(gd + math.pi, e) > cfg.alignment_tolerance_rad:
                    continue
            candidates.append((g, m))
        if not candidates:
            best[k] = None
            continue
        candidates.sort(key=lambda t: (qs(t[0]), ends[t[1]][2], ends[t[1]][1]))
        # An ambiguous junction - two partners the same distance away - is a
        # branch, not a dash gap, so the chain is left to end there.
        if len(candidates) > 1 and abs(candidates[0][0] - candidates[1][0]) < 1e-6:
            best[k] = None
        else:
            best[k] = candidates[0]

    link: dict[int, int] = {}
    for k, choice in best.items():
        if choice is None:
            continue
        m = choice[1]
        other = best.get(m)
        if other is not None and other[1] == k:
            link[k] = m

    def other_end(end_index: int) -> int:
        return end_index ^ 1

    visited: set[int] = set()
    out: list[DashChain] = []
    for start in range(len(objs)):
        if start in visited:
            continue
        head = 2 * start
        guard = 0
        while head in link and guard < 2 * len(objs):
            nxt = link[head]
            if ends[nxt][0] == start:
                break
            head = other_end(nxt)
            guard += 1
        order: list[tuple[int, bool]] = []
        cur = head
        guard = 0
        while guard < 2 * len(objs):
            guard += 1
            obj_i = ends[cur][0]
            if obj_i in visited:
                break
            visited.add(obj_i)
            order.append((obj_i, cur % 2 == 1))
            nxt = link.get(other_end(cur))
            if nxt is None:
                break
            cur = nxt
        if len(order) < cfg.min_members:
            continue
        points: list[Pt] = []
        ink = 0.0
        bridged = 0
        for obj_i, rev in order:
            pts = list(objs[obj_i].points)
            if rev:
                pts.reverse()
            ink += objs[obj_i].length
            if points:
                gap = dist(points[-1], pts[0])
                if gap > cfg.touching_pt:
                    bridged += 1
                elif gap <= 1e-9:
                    pts = pts[1:]
            points.extend(pts)
        if len(points) < 2:
            continue
        first = objs[order[0][0]]
        out.append(
            DashChain(
                polyline=tuple(points),
                object_ids=tuple(sorted(objs[i].object_id for i, _r in order)),
                stroke_width=first.stroke_width,
                color=first.stroke_color,
                dashes=first.dashes,
                layer=first.layer,
                member_count=len(order),
                bridged_gaps=bridged,
                ink_length_pt=ink,
            )
        )
    return out
