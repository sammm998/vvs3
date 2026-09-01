"""Nominal size resolution.

Two independent sources are reconciled:

* the **label** - the last numeric run of a code-like designation, parsed
  generically in :mod:`vvs_pipe.designations.discovery` with no catalogue
  behind it;
* the **drawing** - the perpendicular separation of the two walls of the pipe,
  converted to millimetres through the detected scale.

When both are present and agree, the label's value is kept, because a nominal
size is an exact figure and the measurement carries line-width noise.  When
they disagree the *measurement* wins - the geometry is the primary source -
and DIMENSION_CONFLICT is recorded so the disagreement is visible rather than
silently resolved.  With neither source there is no diameter and the reason
says which one was missing.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..canonical import ql
from ..states import Reason

AGREEMENT_RELATIVE_TOLERANCE = 0.15
# A drawn wall separation is known to roughly a tenth of a millimetre at
# building-services scales; reporting more digits than that would be false
# precision, and reporting fewer would be snapping to a catalogue.
MEASURED_DECIMALS = 1


@dataclass(frozen=True, slots=True)
class DimensionResult:
    diameter_mm: float | None
    source: str  # "label" | "measured" | "label+measured" | "none"
    label_mm: float | None
    measured_mm: float | None
    reasons: tuple[Reason, ...]
    confidence: float


def resolve_diameter(
    label_mm: float | None,
    width_pt: float | None,
    metres_per_point: float | None,
) -> DimensionResult:
    measured_mm: float | None = None
    if width_pt is not None and metres_per_point is not None:
        measured_mm = round(width_pt * metres_per_point * 1000.0, MEASURED_DECIMALS)

    if label_mm is not None and measured_mm is not None:
        rel = abs(measured_mm - label_mm) / max(label_mm, 1e-9)
        if rel <= AGREEMENT_RELATIVE_TOLERANCE:
            return DimensionResult(
                diameter_mm=ql(label_mm),
                source="label+measured",
                label_mm=ql(label_mm),
                measured_mm=measured_mm,
                reasons=(),
                confidence=0.97,
            )
        return DimensionResult(
            diameter_mm=measured_mm,
            source="measured",
            label_mm=ql(label_mm),
            measured_mm=measured_mm,
            reasons=(Reason.DIMENSION_CONFLICT,),
            confidence=0.6,
        )
    if measured_mm is not None:
        return DimensionResult(
            diameter_mm=measured_mm,
            source="measured",
            label_mm=None,
            measured_mm=measured_mm,
            reasons=(),
            confidence=0.8,
        )
    if label_mm is not None:
        # No measurement to corroborate with: either the scale is unknown, or -
        # far more commonly on a real sheet - the pipe is drawn as a single
        # dashed centreline that has no width to measure.
        reason = Reason.SCALE_UNKNOWN if metres_per_point is None else Reason.NO_DRAWN_WIDTH
        return DimensionResult(
            diameter_mm=ql(label_mm),
            source="label",
            label_mm=ql(label_mm),
            measured_mm=None,
            reasons=(reason,),
            confidence=0.7 if metres_per_point is not None else 0.55,
        )
    return DimensionResult(
        diameter_mm=None,
        source="none",
        label_mm=None,
        measured_mm=None,
        reasons=(Reason.NO_DIMENSION_EVIDENCE,),
        confidence=0.0,
    )
