"""Independent estimates of how many metres one PDF point represents.

The old scale stage had one usable source - a ``1:N`` note read perfectly - and
one unread colon therefore voided every quantity on the sheet.  That is the
wrong shape for the problem: a drawing states its scale in several ways, and an
engine that can only hear one of them is brittle for no good reason.

Each function here produces zero or more :class:`ScaleHypothesis` values.  A
hypothesis carries its estimate, how strongly its own evidence supports it, and
what that evidence was.  Nothing here decides anything; the decision - agree,
conflict, or unknown - belongs to :mod:`vvs_pipe.measurement.scale`, which sees
all of them at once.

Two of the sources are text-based and two are not, which is the point: a sheet
whose lettering reconstructs badly can still be measured from its dimension
annotations, and a sheet with no annotations can still be measured from its
note.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from ..canonical import canonical_sort, qs
from ..geometry.index import SpatialIndex
from ..geometry.primitives import BBox, Segment, angle_diff, dist
from ..model import GlyphCandidate, TextItem, VectorObject

POINT_IN_METRES = 25.4 / 72.0 / 1000.0

# A drawn ratio is bounded by what a sheet can usefully carry: below 1:1 the
# drawing is larger than the thing, above 1:5000 nothing legible fits on paper.
MIN_RATIO = 1.0
MAX_RATIO = 5000.0

_RATIO_RE = re.compile(r"(?<![\d.])1\s*[:;]\s*(\d{1,5})(?![\d.])")

# Dimension annotation geometry, all in units of the sheet's own cap height.
DIM_TICK_MAX_CAPS = 1.4          # the slash or arrow at each end of the line
DIM_TEXT_OFFSET_CAPS = 2.2       # how far the number sits from the line
DIM_MIN_LENGTH_CAPS = 4.0        # shorter than this and the number would not fit
DIM_PARALLEL_TOLERANCE_RAD = math.radians(8.0)
DIM_MIN_VALUE_MM = 50.0
DIM_MAX_VALUE_MM = 200000.0


@dataclass(frozen=True, slots=True)
class ScaleHypothesis:
    """One independent estimate, with the evidence that produced it."""

    source: str
    metres_per_point: float
    weight: float
    ratio_denominator: float | None
    evidence: tuple[tuple[str, float], ...]
    note: str

    def to_canonical(self) -> dict:
        return {
            "source": self.source,
            "metresPerPoint": qs(self.metres_per_point * 1e6) / 1e6,
            "impliedRatio": qs(self.metres_per_point / POINT_IN_METRES),
            "weight": qs(self.weight),
            "ratioDenominator": None if self.ratio_denominator is None else qs(self.ratio_denominator),
            "evidence": [[k, qs(v)] for k, v in self.evidence],
            "note": self.note,
        }


def _ratio_to_mpp(denominator: float) -> float:
    """A PDF point is a fixed physical length on the paper, so 1:N follows."""
    return POINT_IN_METRES * denominator


# ---------------------------------------------------------------------------
# 1. The ratio note, read exactly


def ratio_note_hypotheses(
    text_items: Sequence[TextItem], page_box: BBox | None = None
) -> list[ScaleHypothesis]:
    """Ratios stated in the sheet's own text, qualified by paper size.

    A note commonly states more than one ratio because the same drawing is
    issued at one sheet size and printed at another: "SKALA A1 (A3) 1:50
    (1:100)" is one drawing at two scales, not a contradiction, and which ratio
    applies depends on the size of the sheet in hand.  Where the note names
    paper sizes alongside its ratios, they are paired in reading order and the
    one matching this sheet's actual dimensions is the one that applies.

    Paper sizes are matched against the ISO A series, which is a published
    standard rather than anything about this drawing; a sheet that is not a
    standard size, or a note whose sizes cannot be paired with its ratios,
    simply leaves every ratio standing - and several standing ratios is a
    conflict, which is the honest answer.
    """
    sheet = _iso_a_series(page_box) if page_box is not None else None
    items = canonical_sort(list(text_items), key=lambda t: t.canonical_key())
    out: list[ScaleHypothesis] = []
    for t in items:
        ratios = [
            float(m.group(1))
            for m in _RATIO_RE.finditer(t.text)
            if MIN_RATIO <= float(m.group(1)) <= MAX_RATIO
        ]
        if not ratios:
            continue
        chosen = _qualify_by_paper(t, items, ratios, sheet)
        for den in chosen if chosen is not None else ratios:
            out.append(
                ScaleHypothesis(
                    source="ratioNote",
                    metres_per_point=_ratio_to_mpp(den),
                    weight=0.9 * max(0.1, t.confidence),
                    ratio_denominator=den,
                    evidence=(
                        ("textConfidence", qs(t.confidence)),
                        ("denominator", den),
                        ("ratiosInNote", float(len(ratios))),
                        ("paperQualified", 1.0 if chosen is not None else 0.0),
                    ),
                    note=(
                        f"read {t.text!r}"
                        if chosen is None
                        else f"read {t.text!r}, {sheet} sheet selects 1:{den:g}"
                    ),
                )
            )
    return out


# The ISO A series, derived rather than tabulated: A0 is one square metre with
# sides in the ratio 1:sqrt(2), and each size after it is the previous one
# halved across its long side.
_PAPER_TOLERANCE = 0.02
# A trailing word boundary would not survive an unresolved character - the mark
# after the "A" is often exactly the one the classifier could not place - so the
# token is bounded by explicit lookaround instead, and the placeholder set is
# closed rather than "any non-word character", which would match a layer code.
_PAPER_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])A([0-9\uFFFD?])(?![A-Za-z0-9])")


def _iso_a_series(page_box: BBox) -> str | None:
    long_mm = max(page_box.width, page_box.height) * 25.4 / 72.0
    short_mm = min(page_box.width, page_box.height) * 25.4 / 72.0
    for n in range(0, 8):
        w = 1000.0 / (2 ** ((2 * n - 1) / 4))
        h = 1000.0 / (2 ** ((2 * n + 1) / 4))
        if (
            abs(long_mm - w) <= _PAPER_TOLERANCE * w
            and abs(short_mm - h) <= _PAPER_TOLERANCE * h
        ):
            return f"A{n}"
    return None


def _qualify_by_paper(
    item: TextItem,
    items: Sequence[TextItem],
    ratios: Sequence[float],
    sheet: str | None,
) -> list[float] | None:
    """Pick the one ratio this sheet's paper size selects, or nothing.

    Returns ``None`` whenever the pairing is not unambiguous - a different
    number of sizes than ratios, no size matching this sheet, more than one
    matching - so that an unresolvable note leaves all its ratios standing
    instead of being resolved by preference.
    """
    if sheet is None or len(ratios) < 2:
        return None
    text = item.text
    for neighbour in _adjacent_lines(item, items):
        text = f"{text} {neighbour.text}"
    tokens = _PAPER_TOKEN_RE.findall(text)
    if len(tokens) != len(ratios):
        return None
    matches = [i for i, tok in enumerate(tokens) if f"A{tok}" == sheet]
    if len(matches) != 1:
        return None
    return [ratios[matches[0]]]


def _adjacent_lines(item: TextItem, items: Sequence[TextItem]) -> list[TextItem]:
    """Lines set directly above or below this one and overlapping it.

    A scale note is routinely split across two lines, the sizes on one and the
    ratios on the other, so the pairing has to be able to see both.
    """
    reach = max(item.height, 1.0) * 1.5
    out: list[TextItem] = []
    for other in items:
        if other.text_id == item.text_id:
            continue
        gap = max(item.bbox.y0, other.bbox.y0) - min(item.bbox.y1, other.bbox.y1)
        if gap > reach:
            continue
        overlap = min(item.bbox.x1, other.bbox.x1) - max(item.bbox.x0, other.bbox.x0)
        if overlap <= 0:
            continue
        out.append(other)
    return sorted(out, key=lambda t: (qs(t.bbox.y0), qs(t.bbox.x0)))


# ---------------------------------------------------------------------------
# 2. The ratio note, read through the classifier's own alternatives


def tolerant_ratio_hypotheses(
    text_items: Sequence[TextItem],
    glyphs: Mapping[str, GlyphCandidate],
    max_substitutions: int = 2,
) -> list[ScaleHypothesis]:
    """Recover a ratio note that one or two mis-read characters hid.

    This is not guessing at the missing characters.  Every substitution tried
    comes from the *classifier's own* ranked alternatives for that glyph - the
    shapes it already judged plausible for that mark and merely ranked second.
    A reading is only offered when the substituted characters were genuinely
    close calls, and the hypothesis is weighted down by how much margin the
    classifier had, so a confident mis-read contributes almost nothing while a
    coin-flip contributes nearly as much as a clean read.
    """
    out: list[ScaleHypothesis] = []
    for t in canonical_sort(list(text_items), key=lambda t: t.canonical_key()):
        if _RATIO_RE.search(t.text):
            continue  # already read exactly; nothing to recover
        ordered = _glyphs_in_reading_order(t, glyphs)
        if not ordered or len(ordered) != len(t.text):
            continue
        for reading, cost, swapped in _alternative_readings(
            t.text, ordered, max_substitutions
        ):
            m = _RATIO_RE.search(reading)
            if not m:
                continue
            den = float(m.group(1))
            if not (MIN_RATIO <= den <= MAX_RATIO):
                continue
            out.append(
                ScaleHypothesis(
                    source="ratioNoteFromAlternatives",
                    metres_per_point=_ratio_to_mpp(den),
                    # Cost is the total margin the classifier had over the
                    # alternatives used, so a reading it nearly chose anyway
                    # keeps most of its weight.
                    weight=qs(max(0.05, 0.55 * math.exp(-2.0 * cost))),
                    ratio_denominator=den,
                    evidence=(
                        ("substitutions", float(swapped)),
                        ("marginCost", qs(cost)),
                        ("denominator", den),
                    ),
                    note=f"read {t.text!r} as {reading!r}",
                )
            )
            break  # the cheapest reading of this item is the only one worth having
    return out


def _glyphs_in_reading_order(
    item: TextItem, glyphs: Mapping[str, GlyphCandidate]
) -> list[GlyphCandidate]:
    found = [glyphs[g] for g in item.glyph_ids if g in glyphs]
    return sorted(found, key=lambda g: (qs(g.bbox.x0), qs(g.bbox.y0)))


def _alternative_readings(
    text: str, ordered: Sequence[GlyphCandidate], max_substitutions: int
) -> Iterable[tuple[str, float, int]]:
    """Cheapest readings first, substituting only ranked alternatives.

    The search is breadth-limited on purpose: allowing every character to change
    would let any string become any other, which is exactly the guessing this
    engine is not allowed to do.
    """
    swaps: list[tuple[float, int, str]] = []
    for i, g in enumerate(ordered):
        if i >= len(text) or g.character is None:
            continue
        best = None
        for alt, score in g.alternatives:
            if alt == g.character or len(alt) != 1:
                continue
            margin = max(0.0, g.confidence - score)
            if best is None or margin < best[0]:
                best = (margin, i, alt)
        if best is not None:
            swaps.append(best)
    swaps.sort(key=lambda s: (qs(s[0]), s[1], s[2]))

    for k in range(1, min(max_substitutions, len(swaps)) + 1):
        chosen = swaps[:k]
        chars = list(text)
        for margin, i, alt in chosen:
            chars[i] = alt
        yield "".join(chars), sum(m for m, _i, _a in chosen), k


# ---------------------------------------------------------------------------
# 3. Dimension annotations - no text reading of the *scale* required at all


def dimension_hypotheses(
    text_items: Sequence[TextItem],
    objects: Sequence[VectorObject],
    cap_height: float,
) -> list[ScaleHypothesis]:
    """A dimension line of length L labelled ``2400`` fixes the scale directly.

    This is the strongest source on a real sheet because it does not depend on
    finding or reading the scale note: it needs only a number, which is the part
    of a drawing's lettering that reconstructs most reliably, and a line whose
    two ends are marked.  Many such lines agreeing is a far better argument than
    one note read once.
    """
    cap = max(cap_height, 0.5)
    numbers = [
        (t, float(t.text.strip()))
        for t in canonical_sort(list(text_items), key=lambda t: t.canonical_key())
        if t.text.strip().isdigit()
        and DIM_MIN_VALUE_MM <= float(t.text.strip()) <= DIM_MAX_VALUE_MM
    ]
    if not numbers:
        return []

    lines = [
        (o, Segment(o.points[0], o.points[1]))
        for o in objects
        if not o.closed and len(o.points) == 2
    ]
    long_lines = [(o, s) for o, s in lines if s.length >= DIM_MIN_LENGTH_CAPS * cap]
    if not long_lines:
        return []

    tick_index: SpatialIndex[int] = SpatialIndex.for_items(
        [
            (s.bbox, i)
            for i, (_o, s) in enumerate(lines)
            if 0 < s.length <= DIM_TICK_MAX_CAPS * cap
        ]
    )
    text_index: SpatialIndex[int] = SpatialIndex.for_items(
        [(t.bbox, i) for i, (t, _v) in enumerate(numbers)]
    )

    out: list[ScaleHypothesis] = []
    for _o, seg in canonical_sort(long_lines, key=lambda p: p[1].key()):
        if not _has_end_ticks(seg, lines, tick_index, cap):
            continue
        label = _label_for(seg, numbers, text_index, cap)
        if label is None:
            continue
        item, value_mm = label
        mpp = (value_mm / 1000.0) / seg.length
        ratio = mpp / POINT_IN_METRES
        if not (MIN_RATIO <= ratio <= MAX_RATIO):
            continue
        out.append(
            ScaleHypothesis(
                source="dimensionAnnotation",
                metres_per_point=mpp,
                weight=0.6 * max(0.1, item.confidence),
                ratio_denominator=None,
                evidence=(
                    ("valueMm", value_mm),
                    ("drawnLengthPt", qs(seg.length)),
                    ("textConfidence", qs(item.confidence)),
                ),
                note=f"dimension {item.text!r} over {qs(seg.length)}pt",
            )
        )
    return out


def _has_end_ticks(
    seg: Segment,
    lines: Sequence[tuple[VectorObject, Segment]],
    tick_index: SpatialIndex[int],
    cap: float,
) -> bool:
    """Both ends carry a short mark that is not parallel to the line itself."""
    reach = DIM_TICK_MAX_CAPS * cap
    for end in (seg.a, seg.b):
        probe = BBox(end[0], end[1], end[0], end[1]).expanded(reach)
        found = False
        for i in tick_index.query_box(probe):
            tick = lines[i][1]
            if min(dist(end, tick.a), dist(end, tick.b)) > reach:
                continue
            if angle_diff(seg.angle, tick.angle) <= DIM_PARALLEL_TOLERANCE_RAD:
                continue  # a continuation of the same line, not an end mark
            found = True
            break
        if not found:
            return False
    return True


def _label_for(
    seg: Segment,
    numbers: Sequence[tuple[TextItem, float]],
    text_index: SpatialIndex[int],
    cap: float,
) -> tuple[TextItem, float] | None:
    """The one number sitting on this line's midpoint, or nothing.

    Two candidate numbers means the association is ambiguous, and an ambiguous
    dimension is discarded rather than resolved by picking the nearer one.
    """
    mid = seg.midpoint
    reach = DIM_TEXT_OFFSET_CAPS * cap
    probe = BBox(mid[0], mid[1], mid[0], mid[1]).expanded(reach)
    hits = []
    for i in text_index.query_box(probe):
        item, value = numbers[i]
        if dist(mid, item.bbox.center) > reach:
            continue
        hits.append((item, value))
    return hits[0] if len(hits) == 1 else None


# ---------------------------------------------------------------------------
# 4. Repeated round dimensions - the drawing's own modular grid


def grid_module_hypotheses(
    objects: Sequence[VectorObject], drawn: BBox, cap_height: float
) -> list[ScaleHypothesis]:
    """Deliberately not implemented.

    A structural grid spaced at a round number of metres would fix the scale,
    but only if the module were known, and it is not: 1.2 m, 2.4 m, 3.0 m and
    6.0 m are all common and all consistent with the same drawn spacing at
    different scales.  Choosing one would be guessing, which this engine does
    not do, so this source contributes nothing rather than contributing a
    number that looks like evidence.
    """
    return []
