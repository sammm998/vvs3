"""Quantity aggregation.

Rows are grouped by (designation, nominal size).  A physical pipe contributes
to exactly one row, so the reconciliation invariant - every metre of detected
centerline appears once and only once - is preserved by construction and
re-checked in :mod:`vvs_pipe.validation.reconcile`.

Pipes whose identity is not settled are not silently folded into a neighbouring
row: they get their own rows carrying the state and the reason, so an
UNRESOLVED length is visible in the take-off rather than absent from it.
"""

from __future__ import annotations

from typing import Sequence

from ..canonical import canonical_sort, ql, qs
from ..model import Confidence, PhysicalPipe, QuantityRow
from ..states import IdentityState, Reason, worst


def aggregate_quantities(pipes: Sequence[PhysicalPipe]) -> tuple[QuantityRow, ...]:
    groups: dict[tuple, list[PhysicalPipe]] = {}
    for p in canonical_sort(list(pipes), key=lambda p: p.canonical_key()):
        key = (
            p.designation or "",
            ql(p.diameter_mm) if p.diameter_mm is not None else -1.0,
            p.identity_state.value,
        )
        groups.setdefault(key, []).append(p)

    rows: list[QuantityRow] = []
    for key in sorted(groups):
        members = groups[key]
        designation = members[0].designation
        diameter = members[0].diameter_mm
        horiz = [m.horizontal_length_m for m in members]
        vert = [m.vertical_length_m for m in members]
        total = [m.total_length_m for m in members]
        state = IdentityState.CONFIRMED
        reasons: set[Reason] = set()
        for m in members:
            state = worst(state, m.identity_state)
            reasons.update(m.reasons)
        rows.append(
            QuantityRow(
                designation=designation,
                diameter_mm=diameter,
                horizontal_m=None if any(h is None for h in horiz) else ql(sum(horiz)),
                vertical_m=None if all(v is None for v in vert) else ql(sum(v or 0.0 for v in vert)),
                total_m=None if any(t is None for t in total) else ql(sum(total)),
                pipe_count=len(members),
                physical_pipe_ids=tuple(sorted(m.physical_pipe_id for m in members)),
                state=state,
                reasons=tuple(sorted(reasons, key=lambda r: r.value)),
                confidence=Confidence(
                    geometry=qs(min((m.confidence.geometry or 0.0) for m in members)),
                    association=qs(min((m.confidence.association or 0.0) for m in members)),
                    dimension=None
                    if any(m.confidence.dimension is None for m in members)
                    else qs(min(m.confidence.dimension for m in members)),
                    vertical=None
                    if all(m.confidence.vertical is None for m in members)
                    else qs(min((m.confidence.vertical or 0.0) for m in members)),
                ),
            )
        )
    return tuple(canonical_sort(rows, key=lambda r: r.canonical_key()))
