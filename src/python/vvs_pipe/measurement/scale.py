"""Drawing scale inference.

Estimates come from :mod:`vvs_pipe.measurement.hypotheses`, which offers each
independent source the drawing might supply: the ratio note read exactly, the
ratio note recovered through the glyph classifier's own alternatives, dimension
annotations, and a scale bar.  This module does the deciding.

Four outcomes, and the distinctions between them matter:

* **SCALE_CONFIRMED** - two or more *different kinds* of source agree.
* **RESOLVED** - one kind of source, uncontradicted.  Usable, uncorroborated.
* **SCALE_CONFLICT** - sources disagree beyond tolerance.  No number is
  produced: the engine has two honest readings and no basis for preferring
  one, and quietly taking the heavier is how a detectable problem becomes a
  wrong answer.
* **SCALE_UNKNOWN** - the drawing said nothing this engine can hear.

Never a default, in any of the four.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from ..canonical import canonical_sort, qs
from ..geometry.primitives import BBox
from ..model import GlyphCandidate, Provenance, ScaleResult, TextItem, VectorObject
from ..states import Reason, ScaleState
from .hypotheses import (
    POINT_IN_METRES as _POINT_IN_METRES,
    ScaleHypothesis,
    dimension_hypotheses,
    ratio_note_hypotheses,
    tolerant_ratio_hypotheses,
)

POINT_IN_METRES = _POINT_IN_METRES
CROSS_CHECK_TOLERANCE = 0.05
# A rival cluster carrying at least this share of the winner's weight is a real
# disagreement, not noise, and the engine refuses to choose between them.
CONFLICT_WEIGHT_FRACTION = 0.5
BAR_MIN_CELLS = 3
BAR_CELL_TOLERANCE_PT = 0.75
METRE_TOKENS = frozenset({"M", "m"})


@dataclass(frozen=True, slots=True)
class ScaleBar:
    bbox: BBox
    cells: int
    length_pt: float


def _find_scale_bars(objects: Sequence[VectorObject]) -> list[ScaleBar]:
    rects = [
        o
        for o in objects
        if o.closed and len(o.points) in (4, 5) and o.bbox.width > 0 and o.bbox.height > 0
    ]
    rects = canonical_sort(rects, key=lambda o: o.canonical_key())
    bars: list[ScaleBar] = []
    used: set[str] = set()
    for i, r in enumerate(rects):
        if r.object_id in used:
            continue
        row = [r]
        changed = True
        while changed:
            changed = False
            for c in rects:
                if c.object_id in used or any(c.object_id == m.object_id for m in row):
                    continue
                if abs(c.bbox.height - r.bbox.height) > BAR_CELL_TOLERANCE_PT:
                    continue
                if abs(c.bbox.width - r.bbox.width) > BAR_CELL_TOLERANCE_PT:
                    continue
                if abs(c.bbox.y0 - r.bbox.y0) > BAR_CELL_TOLERANCE_PT:
                    continue
                if any(
                    abs(c.bbox.x0 - m.bbox.x1) <= BAR_CELL_TOLERANCE_PT
                    or abs(c.bbox.x1 - m.bbox.x0) <= BAR_CELL_TOLERANCE_PT
                    for m in row
                ):
                    row.append(c)
                    changed = True
        if len(row) >= BAR_MIN_CELLS:
            for m in row:
                used.add(m.object_id)
            box = BBox.union_all([m.bbox for m in row])
            bars.append(ScaleBar(bbox=box, cells=len(row), length_pt=box.width))
    return canonical_sort(bars, key=lambda b: b.bbox.key())


def infer_scale(
    text_items: Sequence[TextItem],
    objects: Sequence[VectorObject],
    page_box: BBox,
    glyphs: Sequence[GlyphCandidate] = (),
    cap_height: float = 7.0,
) -> ScaleResult:
    """Weigh every independent estimate of the drawing's scale against the rest.

    A single source is usable but uncorroborated, so it resolves rather than
    confirms.  Two or more sources agreeing to within tolerance is the strongest
    outcome available and is what SCALE_CONFIRMED means.  Sources that disagree
    beyond tolerance produce SCALE_CONFLICT and *no* number: the engine has two
    honest readings of the drawing and no basis for preferring one, and picking
    the heavier would be exactly the kind of quiet tie-break that turns a
    detectable problem into a wrong answer.
    """
    glyph_map = {g.glyph_id: g for g in glyphs}
    hypotheses: list[ScaleHypothesis] = []
    hypotheses += ratio_note_hypotheses(text_items)
    hypotheses += tolerant_ratio_hypotheses(text_items, glyph_map)
    hypotheses += dimension_hypotheses(text_items, objects, cap_height)
    for bar in _find_scale_bars(objects):
        h = _scale_bar_hypothesis(bar, text_items)
        if h is not None:
            hypotheses.append(h)

    hypotheses = canonical_sort(
        hypotheses, key=lambda h: (h.source, qs(h.metres_per_point), h.note)
    )
    sources = tuple(
        (f"{h.source}[{i}]", qs(h.metres_per_point / POINT_IN_METRES))
        for i, h in enumerate(hypotheses)
    )
    notes = tuple(f"{h.source}: {h.note}" for h in hypotheses)
    provenance = Provenance(
        stage="scale",
        rule="independent hypotheses, clustered and cross-checked",
        notes=notes,
    )

    if not hypotheses:
        return ScaleResult(
            state=ScaleState.SCALE_UNKNOWN,
            metres_per_point=None,
            ratio_denominator=None,
            sources=(),
            reasons=(Reason.SCALE_UNKNOWN,),
            provenance=provenance,
        )

    clusters = _cluster(hypotheses)
    best = max(clusters, key=lambda c: (qs(sum(h.weight for h in c)), -qs(c[0].metres_per_point)))
    rivals = [
        c
        for c in clusters
        if c is not best
        and sum(h.weight for h in c) >= CONFLICT_WEIGHT_FRACTION * sum(h.weight for h in best)
    ]
    if rivals:
        return ScaleResult(
            state=ScaleState.SCALE_CONFLICT,
            metres_per_point=None,
            ratio_denominator=None,
            sources=sources,
            reasons=(Reason.SCALE_CONFLICT,),
            provenance=provenance,
        )

    # Weighted mean inside the winning cluster: the members already agree to
    # within tolerance, so this refines the estimate rather than choosing.
    total = sum(h.weight for h in best)
    mpp = sum(h.metres_per_point * h.weight for h in best) / total
    denominators = sorted({h.ratio_denominator for h in best if h.ratio_denominator})
    distinct_sources = {h.source for h in best}
    confirmed = len(distinct_sources) >= 2
    return ScaleResult(
        state=ScaleState.SCALE_CONFIRMED if confirmed else ScaleState.RESOLVED,
        metres_per_point=mpp,
        ratio_denominator=denominators[0] if len(denominators) == 1 else None,
        sources=sources + (("agreeingSources", float(len(distinct_sources))),),
        reasons=(),
        provenance=provenance,
    )


def _cluster(hypotheses: Sequence[ScaleHypothesis]) -> list[list[ScaleHypothesis]]:
    """Group estimates that agree to within tolerance.

    Agreement is relative, not absolute, because the same relative error means
    the same thing at 1:50 and at 1:500.
    """
    clusters: list[list[ScaleHypothesis]] = []
    for h in hypotheses:
        for c in clusters:
            reference = sum(x.metres_per_point * x.weight for x in c) / sum(x.weight for x in c)
            if abs(h.metres_per_point - reference) <= CROSS_CHECK_TOLERANCE * reference:
                c.append(h)
                break
        else:
            clusters.append([h])
    return clusters


def _scale_bar_hypothesis(
    bar: ScaleBar, text_items: Sequence[TextItem]
) -> ScaleHypothesis | None:
    probe = bar.bbox.expanded(max(12.0, bar.bbox.height * 3.0))
    numbers: list[float] = []
    has_metre_token = False
    for t in text_items:
        if not probe.intersects(t.bbox):
            continue
        token = t.text.strip()
        if token in METRE_TOKENS:
            has_metre_token = True
        elif token.replace(".", "", 1).isdigit():
            numbers.append(float(token))
    if len(numbers) < 2 or bar.length_pt <= 0:
        return None
    span = max(numbers) - min(numbers)
    if span <= 0:
        return None
    return ScaleHypothesis(
        source="scaleBar",
        metres_per_point=span / bar.length_pt,
        # Without a unit token beside it the bar's numbers could be metres or
        # millimetres; it is still offered, but it cannot carry the decision on
        # its own and its weight says so.
        weight=0.7 if has_metre_token else 0.25,
        ratio_denominator=None,
        evidence=(("cells", float(bar.cells)), ("lengthPt", qs(bar.length_pt)), ("span", qs(span))),
        note=f"bar of {bar.cells} cells spanning {qs(span)} units",
    )


def detect_scale(
    text_items: Sequence[TextItem],
    objects: Sequence[VectorObject],
    page_box: BBox,
) -> ScaleResult:
    """Infer the scale from text and geometry alone.

    Kept as the name the rest of the engine and its tests use; the work is
    :func:`infer_scale`, which also accepts the glyph evidence needed to read a
    note through the classifier's alternatives.
    """
    return infer_scale(text_items, objects, page_box)
