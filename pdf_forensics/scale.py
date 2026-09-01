"""Twenty-first search: what is this drawing's scale.

Several independent signals are collected and then compared.  A scale that two
sources agree on is CONFIRMED; sources that disagree are a CONFLICT, which is
reported rather than resolved by preferring one; a single source is usable but
says so; and no source at all is SCALE_UNKNOWN, which means no metres are
reported anywhere in the analysis.

Reading the ratio note is done over the text item's *alternative* readings too,
because one mis-read colon must not be allowed to destroy the measurement of a
whole sheet.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from .canonical import entity_id, q, qs, sort_canonical
from .model import Reason, Segment, State, TextItem

MM_PER_POINT = 25.4 / 72.0

# ISO A-series sheets, in millimetres.  A property of paper, not of a drawing.
A_SERIES = {
    "A0": (1189.0, 841.0), "A1": (841.0, 594.0), "A2": (594.0, 420.0),
    "A3": (420.0, 297.0), "A4": (297.0, 210.0),
}

_RATIO = re.compile(r"(?<!\d)1\s*[:;.]\s*(\d{1,5})(?!\d)")


@dataclass(frozen=True)
class ScaleHypothesis:
    hypothesis_id: str
    source: str                    # ratio_note | scale_bar | sheet_size_pairing
    denominator: Optional[float]
    metres_per_point: float
    confidence: float
    evidence: dict[str, Any]

    def to_json(self) -> dict:
        return {"hypothesisId": self.hypothesis_id, "source": self.source,
                "denominator": self.denominator, "metresPerPoint": self.metres_per_point,
                "confidence": self.confidence, "evidence": self.evidence}


@dataclass
class ScaleResult:
    state: str
    metres_per_point: Optional[float]
    denominator: Optional[float]
    hypotheses: list[ScaleHypothesis]
    reasons: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {
            "state": self.state,
            "metresPerPoint": self.metres_per_point,
            "denominator": self.denominator,
            "reasons": list(self.reasons),
            "hypotheses": [h.to_json() for h in self.hypotheses],
        }


def metres_per_point(denominator: float) -> float:
    return qs(MM_PER_POINT * denominator / 1000.0)


def _sheet_name(width_pt: float, height_pt: float, tolerance_mm: float = 6.0) -> Optional[str]:
    w_mm, h_mm = width_pt * MM_PER_POINT, height_pt * MM_PER_POINT
    long_side, short_side = max(w_mm, h_mm), min(w_mm, h_mm)
    for name, (nominal_long, nominal_short) in sorted(A_SERIES.items()):
        if (abs(long_side - nominal_long) <= tolerance_mm
                and abs(short_side - nominal_short) <= tolerance_mm):
            return name
    return None


def ratio_hypotheses(text_items: Sequence[TextItem], page_size: tuple[float, float]
                     ) -> list[ScaleHypothesis]:
    """Ratios written on the sheet, including in a text item's other readings."""
    out: list[ScaleHypothesis] = []
    sheet = _sheet_name(*page_size)
    for item in sort_canonical(text_items, key=lambda t: (t.page, t.bbox, t.text_id)):
        readings = [(item.text, 1.0)] + [(alt, score) for alt, score in item.alternatives]
        for reading, weight in readings:
            ratios = [float(m.group(1)) for m in _RATIO.finditer(reading)]
            if not ratios:
                continue
            sheets = re.findall(r"\bA[0-4]\b", reading.upper())
            chosen: Optional[float] = None
            rule = "SINGLE_RATIO_ON_SHEET"
            if len(ratios) == 1:
                chosen = ratios[0]
            elif sheets and sheet in sheets and len(sheets) == len(ratios):
                # "A1 (A3) 1:50 (1:100)" - one drawing issued at two sizes, and
                # the sheet in hand selects its own ratio.
                chosen = ratios[sheets.index(sheet)]
                rule = "RATIO_SELECTED_BY_SHEET_SIZE"
            if chosen is None:
                continue
            out.append(
                ScaleHypothesis(
                    hypothesis_id=entity_id("scale", {"s": "ratio", "d": chosen,
                                                      "t": item.text_id, "r": reading}),
                    source="ratio_note",
                    denominator=q(chosen),
                    metres_per_point=metres_per_point(chosen),
                    confidence=q(min(1.0, 0.55 + 0.35 * weight * item.confidence)),
                    evidence={"textId": item.text_id, "reading": reading,
                              "isPrimaryReading": reading == item.text,
                              "sheet": sheet, "rule": rule},
                )
            )
            break
    return out


def scale_bar_hypotheses(segments: Sequence[Segment], text_items: Sequence[TextItem],
                         page_size: tuple[float, float]) -> list[ScaleHypothesis]:
    """A scale bar states the scale by drawing it.

    The bar is found as a row of equal, adjacent cells with a number at each
    end; the value the numbers differ by, over the distance between them, is
    metres per point.
    """
    from .spatial_index import SpatialIndex, bbox_distance

    numbers: list[tuple[float, TextItem]] = []
    for item in text_items:
        token = item.text.strip().replace(",", ".")
        if re.fullmatch(r"\d{1,4}(\.\d+)?", token):
            numbers.append((float(token), item))
    if len(numbers) < 2:
        return []
    out: list[ScaleHypothesis] = []
    for i, (value_a, item_a) in enumerate(sorted(numbers, key=lambda n: (n[1].bbox, n[0]))):
        for value_b, item_b in sorted(numbers, key=lambda n: (n[1].bbox, n[0]))[i + 1:]:
            if item_a.page != item_b.page or value_b <= value_a:
                continue
            if abs(item_a.bbox[3] - item_b.bbox[3]) > 2.0 * max(item_a.cap_height, 1.0):
                continue
            distance = abs(((item_b.bbox[0] + item_b.bbox[2]) / 2.0)
                           - ((item_a.bbox[0] + item_a.bbox[2]) / 2.0))
            if distance < 20.0:
                continue
            span = value_b - value_a
            box = (min(item_a.bbox[0], item_b.bbox[0]), min(item_a.bbox[1], item_b.bbox[1]) - 20.0,
                   max(item_a.bbox[2], item_b.bbox[2]), max(item_a.bbox[3], item_b.bbox[3]) + 20.0)
            cells = _count_bar_cells(segments, item_a.page, box)
            if cells < 3:
                continue
            candidate = qs(span / distance)
            out.append(
                ScaleHypothesis(
                    hypothesis_id=entity_id("scale", {"s": "bar", "a": item_a.text_id,
                                                      "b": item_b.text_id}),
                    source="scale_bar",
                    denominator=qs(candidate * 1000.0 / MM_PER_POINT),
                    metres_per_point=candidate,
                    confidence=0.7,
                    evidence={"fromTextId": item_a.text_id, "toTextId": item_b.text_id,
                              "metresSpanned": q(span), "pointsSpanned": q(distance),
                              "cells": cells},
                )
            )
    return out


def _count_bar_cells(segments: Sequence[Segment], page: int, box: Sequence[float]) -> int:
    verticals = []
    for segment in segments:
        if segment.page != page:
            continue
        if not (box[0] <= segment.a[0] <= box[2] and box[1] <= segment.a[1] <= box[3]):
            continue
        if abs(segment.a[0] - segment.b[0]) < 0.6 and abs(segment.a[1] - segment.b[1]) > 2.0:
            verticals.append(q(segment.a[0]))
    spacing = sorted(set(verticals))
    if len(spacing) < 4:
        return 0
    gaps = [q(b - a) for a, b in zip(spacing, spacing[1:])]
    typical = sorted(gaps)[len(gaps) // 2]
    regular = [g for g in gaps if abs(g - typical) <= 0.15 * max(typical, 1e-6)]
    return len(regular)


def resolve(text_items: Sequence[TextItem], segments: Sequence[Segment],
            page_size: tuple[float, float], agreement: float = 0.02) -> ScaleResult:
    """Collect every signal, then let them agree, disagree, or fall silent."""
    hypotheses = ratio_hypotheses(text_items, page_size)
    hypotheses += scale_bar_hypotheses(segments, text_items, page_size)
    hypotheses = sort_canonical(hypotheses, key=lambda h: (h.source, h.metres_per_point,
                                                           h.hypothesis_id))
    if not hypotheses:
        return ScaleResult(State.UNRESOLVED, None, None, [], (Reason.SCALE_UNKNOWN,))
    clusters: list[list[ScaleHypothesis]] = []
    for hypothesis in sorted(hypotheses, key=lambda h: (h.metres_per_point, h.hypothesis_id)):
        placed = False
        for cluster in clusters:
            reference = cluster[0].metres_per_point
            if abs(hypothesis.metres_per_point - reference) <= agreement * max(reference, 1e-9):
                cluster.append(hypothesis)
                placed = True
                break
        if not placed:
            clusters.append([hypothesis])
    clusters.sort(key=lambda c: (-len({h.source for h in c}), -len(c), c[0].metres_per_point))
    best = clusters[0]
    distinct_sources = len({h.source for h in best})
    competing = [c for c in clusters[1:] if len({h.source for h in c}) >= distinct_sources
                 and len(c) >= len(best)]
    if competing:
        return ScaleResult(State.AMBIGUOUS, None, None, hypotheses, (Reason.SCALE_CONFLICT,))
    # A ratio the sheet states is exact; a scale bar is a measurement of it.
    # When they agree, the stated value is the answer and the bar is the
    # corroboration - averaging them would corrupt an exact number with the
    # error of an approximate one.
    stated = [h for h in best if h.source == "ratio_note"]
    chosen = stated or best
    value = qs(sum(h.metres_per_point for h in chosen) / len(chosen))
    denominators = [h.denominator for h in chosen if h.denominator is not None]
    denominator = qs(sum(denominators) / len(denominators)) if denominators else None
    if distinct_sources >= 2:
        return ScaleResult(State.CONFIRMED, value, denominator, hypotheses,
                           ("AGREEING_INDEPENDENT_SOURCES",))
    return ScaleResult(State.AMBIGUOUS, value, denominator, hypotheses, ("SINGLE_SOURCE",))
