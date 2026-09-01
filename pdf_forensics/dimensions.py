"""Twentieth search: what size is this pipe.

Two kinds of evidence, kept apart on purpose:

* **label evidence** - a token beside the pipe that states a size.  A prefix or
  a unit is required.  The bare string ``100`` is never DN100: on a VVS sheet
  it is as likely to be a room number, a level, a length or part of a drawing
  number, and a wrong diameter is a wrong quantity.
* **measured evidence** - the distance between the two walls the pipe is drawn
  with, converted through the sheet's scale.  This exists whether or not
  anything is written down.

They are reconciled, never merged.  Agreement confirms; disagreement is
reported as a conflict rather than resolved by preferring one of them.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from .canonical import entity_id, q, qs, sort_canonical
from .model import PhysicalPipe, Reason, State, TextItem
from .spatial_index import SpatialIndex

# Structural patterns, not a catalogue of sizes: each requires the sheet to say
# what the number means.
_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"^DN\s*(\d{2,4})$", "DN_PREFIX"),
    (r"^(?:Ø|O/|D)\s*(\d{2,4})$", "DIAMETER_SIGN"),
    (r"^(\d{2,4})\s*[x×]\s*(\d{2,4})$", "RECTANGULAR"),
    (r"^DN\s*(\d{2,4})\s*/\s*(\d{2,4})$", "DN_PAIR"),
)


@dataclass(frozen=True)
class DimensionToken:
    """A size somebody wrote down, with the rule that recognised it."""

    token_id: str
    page: int
    text_id: str
    text: str
    bbox: tuple[float, float, float, float]
    values_mm: tuple[float, ...]
    rule: str

    def to_json(self) -> dict:
        return {"tokenId": self.token_id, "page": self.page, "textId": self.text_id,
                "text": self.text, "bbox": list(self.bbox),
                "valuesMm": list(self.values_mm), "rule": self.rule}


def find_dimension_tokens(text_items: Sequence[TextItem]) -> list[DimensionToken]:
    """Every string that states a size, in the sheet's own words."""
    out: list[DimensionToken] = []
    for item in sort_canonical(text_items, key=lambda t: (t.page, t.bbox, t.text_id)):
        for word in _words(item.text):
            for pattern, rule in _PATTERNS:
                match = re.match(pattern, word, flags=re.IGNORECASE)
                if not match:
                    continue
                values = tuple(q(float(g)) for g in match.groups() if g)
                out.append(
                    DimensionToken(
                        token_id=entity_id("dim", {"p": item.page, "b": list(item.bbox),
                                                   "t": word, "r": rule}),
                        page=item.page,
                        text_id=item.text_id,
                        text=word,
                        bbox=item.bbox,
                        values_mm=values,
                        rule=rule,
                    )
                )
                break
    return sort_canonical(out, key=lambda d: (d.page, d.bbox, d.text, d.token_id))


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[\s,;]+", text.strip()) if w]


def trailing_size_evidence(designation: Optional[str]) -> Optional[float]:
    """A size that the *designation itself* ends with, e.g. ``...-110``.

    This is offered only as corroboration.  It is never enough on its own,
    because a trailing number in a code can equally be a sequence number - the
    engine has no way to know which, and guessing would be inventing a size.
    """
    if not designation:
        return None
    match = re.search(r"[-/](\d{2,4})$", designation)
    if not match:
        return None
    return q(float(match.group(1)))


def measured_diameter_mm(wall_separation_points: Optional[float],
                         metres_per_point: Optional[float]) -> Optional[float]:
    if wall_separation_points is None or metres_per_point is None:
        return None
    return qs(wall_separation_points * metres_per_point * 1000.0)


@dataclass
class DimensionResult:
    diameter_mm: Optional[float]
    state: str
    reasons: tuple[str, ...]
    evidence: dict[str, Any]


def resolve_diameter(pipe: PhysicalPipe, tokens: Sequence[DimensionToken],
                     token_index: SpatialIndex, metres_per_point: Optional[float],
                     designation: Optional[str],
                     relative_tolerance: float = 0.25) -> DimensionResult:
    """Decide a pipe's diameter from the evidence there actually is."""
    by_id = {t.token_id: t for t in tokens}
    xs = [p[0] for p in pipe.centerline]
    ys = [p[1] for p in pipe.centerline]
    box = (min(xs), min(ys), max(xs), max(ys)) if xs else (0.0, 0.0, 0.0, 0.0)
    nearby = [by_id[k] for k in token_index.within_distance(pipe.page, box, 24.0)]
    label_values = sorted({v for token in nearby for v in token.values_mm})
    measured = measured_diameter_mm(pipe.measurement.get("wallSeparationPoints"), metres_per_point)
    trailing = trailing_size_evidence(designation)
    evidence: dict[str, Any] = {
        "labelTokens": [t.to_json() for t in nearby],
        "measuredMm": measured,
        "designationTrailingNumber": trailing,
    }
    if label_values and measured is not None:
        agreeing = [v for v in label_values
                    if abs(v - measured) <= relative_tolerance * max(v, measured)]
        if len(agreeing) == 1:
            return DimensionResult(agreeing[0], State.CONFIRMED,
                                   ("LABEL_AGREES_WITH_MEASURED_WALLS",), evidence)
        if not agreeing:
            return DimensionResult(None, State.AMBIGUOUS, (Reason.DIMENSION_CONFLICT,), evidence)
        return DimensionResult(None, State.AMBIGUOUS,
                               (Reason.DIMENSION_CONFLICT, "SEVERAL_LABELS_AGREE"), evidence)
    if label_values and len(label_values) == 1:
        return DimensionResult(label_values[0], State.CONFIRMED, ("LABEL_ONLY",), evidence)
    if measured is not None:
        if trailing is not None and abs(trailing - measured) <= relative_tolerance * max(trailing, measured):
            # the code's trailing number and the drawn walls agree: two
            # independent statements of the same size
            return DimensionResult(trailing, State.CONFIRMED,
                                   ("DESIGNATION_AGREES_WITH_MEASURED_WALLS",), evidence)
        return DimensionResult(measured, State.AMBIGUOUS, ("MEASURED_WALLS_ONLY",), evidence)
    return DimensionResult(None, State.UNRESOLVED, (Reason.NO_DIMENSION_EVIDENCE,), evidence)


def token_spatial_index(tokens: Sequence[DimensionToken]) -> SpatialIndex:
    return SpatialIndex([(t.token_id, t.page, t.bbox) for t in tokens])


def to_json(tokens: Sequence[DimensionToken]) -> dict:
    rules: dict[str, int] = {}
    for token in tokens:
        rules[token.rule] = rules.get(token.rule, 0) + 1
    return {"dimensionTokens": len(tokens), "byRule": {k: rules[k] for k in sorted(rules)}}
