"""Drawing scale detection.

Two independent sources, cross-checked against each other:

* **ratio note** - any ``1:N`` found in the sheet's text.  N is not looked up
  anywhere; the note gives metres per point directly, because a PDF point is a
  fixed physical length on the paper.
* **scale bar** - a row of congruent adjacent cells with numeric labels at its
  ends.  The bar gives *units* per point; the unit is only accepted as metres
  when a unit token sits next to the bar, or when the ratio note independently
  agrees.

If the two disagree beyond tolerance the result is SCALE_AMBIGUOUS and no
quantity is presented as verified.  If neither is available the result is
SCALE_UNKNOWN - never a default.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from ..canonical import canonical_sort, qs
from ..designations.discovery import parse_ratio
from ..geometry.primitives import BBox
from ..model import Provenance, ScaleResult, TextItem, VectorObject
from ..states import Reason, ScaleState

POINT_IN_METRES = 25.4 / 72.0 / 1000.0
CROSS_CHECK_TOLERANCE = 0.05
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


def detect_scale(
    text_items: Sequence[TextItem],
    objects: Sequence[VectorObject],
    page_box: BBox,
) -> ScaleResult:
    sources: list[tuple[str, float]] = []
    reasons: list[Reason] = []
    notes: list[str] = []

    ratio_mpp: float | None = None
    ratio_den: float | None = None
    for t in canonical_sort(list(text_items), key=lambda t: t.canonical_key()):
        den = parse_ratio(t.text)
        if den is not None:
            ratio_den = den
            ratio_mpp = POINT_IN_METRES * den
            sources.append(("ratioNote", qs(den)))
            notes.append(f"ratioNote={t.text!r}")
            break

    bar_upp: float | None = None
    bars = _find_scale_bars(objects)
    for bar in bars:
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
        if len(numbers) >= 2 and bar.length_pt > 0:
            span = max(numbers) - min(numbers)
            if span > 0:
                bar_upp = span / bar.length_pt
                sources.append(("scaleBarUnitsPerPoint", qs(bar_upp)))
                notes.append(
                    f"scaleBar cells={bar.cells} lengthPt={qs(bar.length_pt)} "
                    f"span={qs(span)} metreToken={has_metre_token}"
                )
                if not has_metre_token:
                    notes.append("scaleBarUnitAssumedFromRatioAgreement")
                break

    provenance = Provenance(
        stage="scale",
        rule="ratio note and scale bar, cross-checked",
        notes=tuple(notes),
    )

    if ratio_mpp is not None and bar_upp is not None:
        rel = abs(bar_upp - ratio_mpp) / max(ratio_mpp, 1e-12)
        if rel <= CROSS_CHECK_TOLERANCE:
            return ScaleResult(
                state=ScaleState.RESOLVED,
                metres_per_point=ratio_mpp,
                ratio_denominator=ratio_den,
                sources=tuple(sources) + (("crossCheckRelativeError", qs(rel)),),
                reasons=(),
                provenance=provenance,
            )
        return ScaleResult(
            state=ScaleState.SCALE_AMBIGUOUS,
            metres_per_point=None,
            ratio_denominator=ratio_den,
            sources=tuple(sources) + (("crossCheckRelativeError", qs(rel)),),
            reasons=(Reason.SCALE_AMBIGUOUS,),
            provenance=provenance,
        )
    if ratio_mpp is not None:
        return ScaleResult(
            state=ScaleState.RESOLVED,
            metres_per_point=ratio_mpp,
            ratio_denominator=ratio_den,
            sources=tuple(sources),
            reasons=(),
            provenance=provenance,
        )
    return ScaleResult(
        state=ScaleState.SCALE_UNKNOWN,
        metres_per_point=None,
        ratio_denominator=None,
        sources=tuple(sources),
        reasons=(Reason.SCALE_UNKNOWN,),
        provenance=provenance,
    )
