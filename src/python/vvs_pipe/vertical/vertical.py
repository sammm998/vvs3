"""Vertical pipe (riser / drop) analysis.

A vertical length cannot be measured from a plan; it has to be *inferred* from
elevation evidence.  The rule implemented here is deliberately strict:

* a riser is a drawing symbol - a small closed contour excluded from the pipe
  geometry - sitting at the end of a run;
* the elevation notes near it are parsed generically (any alphabetic prefix,
  a sign, a decimal number - see ``parse_elevation``);
* **two distinct elevations** give a vertical length, namely their difference;
* **one elevation, or none, gives VERTICAL_HEIGHT_UNKNOWN** - the riser is
  reported, its length is not invented, and the pipe carrying it is downgraded
  so no quantity is presented as verified.

There is no default storey height and no assumed floor level anywhere.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from ..canonical import canonical_sort, entity_id, ql, qs
from ..geometry.primitives import BBox, dist
from ..model import Confidence, PipeRun, Provenance, VerticalSegment
from ..states import IdentityState, Reason

ELEVATION_SEARCH_CAP_FACTOR = 6.0
RISER_ATTACH_TOLERANCE_PT = 12.0
MIN_DISTINCT_ELEVATION_M = 0.01


@dataclass(frozen=True, slots=True)
class VerticalAnalysis:
    verticals: tuple[VerticalSegment, ...]
    by_run: dict[str, tuple[str, float | None]]


def analyse_verticals(
    symbol_boxes: Sequence[BBox],
    elevation_notes: Sequence[tuple[str, float, BBox]],
    runs: Sequence[PipeRun],
    text_cap_height: float,
    page: int,
) -> VerticalAnalysis:
    runs = canonical_sort(list(runs), key=lambda r: r.canonical_key())
    boxes = canonical_sort(list(symbol_boxes), key=lambda b: b.key())
    radius = ELEVATION_SEARCH_CAP_FACTOR * max(text_cap_height, 1.0)

    verticals: list[VerticalSegment] = []
    by_run: dict[str, tuple[str, float | None]] = {}

    for box in boxes:
        centre = box.center
        attached = [
            r
            for r in runs
            if min(dist(centre, r.centerline[0]), dist(centre, r.centerline[-1]))
            <= RISER_ATTACH_TOLERANCE_PT + max(box.width, box.height) / 2.0
        ]
        if not attached:
            continue
        near = [
            (text, value)
            for text, value, nbox in elevation_notes
            if nbox.expanded(radius).intersects(box)
        ]
        values = sorted({ql(v) for _t, v in near})
        evidence: list[tuple[str, float]] = [
            ("elevationNotes", float(len(near))),
            ("attachedRuns", float(len(attached))),
        ]
        vid = entity_id("vt", (page, (round(centre[0], 4), round(centre[1], 4))))
        if len(values) >= 2:
            lo, hi = values[0], values[-1]
            length = ql(hi - lo)
            if length < MIN_DISTINCT_ELEVATION_M:
                state, reasons, length_out = (
                    IdentityState.INSUFFICIENT,
                    (Reason.VERTICAL_HEIGHT_UNKNOWN,),
                    None,
                )
                lo_out = hi_out = None
            else:
                state, reasons, length_out = IdentityState.CONFIRMED, (), length
                lo_out, hi_out = lo, hi
        else:
            state = IdentityState.INSUFFICIENT
            reasons = (Reason.VERTICAL_HEIGHT_UNKNOWN,)
            length_out = None
            lo_out = hi_out = None

        verticals.append(
            VerticalSegment(
                vertical_id=vid,
                page=page,
                point=(centre[0], centre[1]),
                attached_run_ids=tuple(sorted(r.pipe_run_id for r in attached)),
                from_elevation_m=lo_out,
                to_elevation_m=hi_out,
                length_m=length_out,
                evidence=tuple(evidence),
                state=state,
                reasons=reasons,
                confidence=Confidence(
                    geometry=0.9,
                    vertical=qs(0.95 if length_out is not None else 0.1),
                ),
                provenance=Provenance(
                    stage="vertical",
                    rule="riser symbol at a run end + distinct elevation notes",
                    inputs=tuple(sorted(r.pipe_run_id for r in attached)),
                    notes=tuple(f"{t}={qs(v)}" for t, v in sorted(near)),
                ),
            )
        )
        # A riser belongs to exactly one run: the one whose end is nearest.
        best = min(
            attached,
            key=lambda r: (
                qs(min(dist(centre, r.centerline[0]), dist(centre, r.centerline[-1]))),
                r.canonical_key(),
            ),
        )
        by_run[best.pipe_run_id] = (vid, length_out)

    return VerticalAnalysis(
        verticals=tuple(canonical_sort(verticals, key=lambda v: v.canonical_key())),
        by_run=by_run,
    )
