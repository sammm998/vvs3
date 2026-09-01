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
        # A row is "how much of this designation, at this nominal size".  When
        # the label states its size, that is the size of the row: splitting one
        # stated size across every width the geometry happened to measure would
        # assert several different pipe sizes for a label that names one, and on
        # a real sheet it turned a single S3-R8-75 into three rows at 61.4, 63.5
        # and 75 mm.  Any disagreement between the two is still reported, per
        # pipe, by the dimension stage - it just does not fragment the take-off.
        size = p.nominal_diameter_mm if p.nominal_diameter_mm is not None else p.diameter_mm
        key = (
            p.designation or "",
            ql(size) if size is not None else -1.0,
            p.identity_state.value,
        )
        groups.setdefault(key, []).append(p)

    rows: list[QuantityRow] = []
    for key in sorted(groups):
        members = groups[key]
        designation = members[0].designation
        diameter, size_disagreement = _row_diameter(members)
        horiz = [m.horizontal_length_m for m in members]
        vert = [m.vertical_length_m for m in members]
        total = [m.total_length_m for m in members]
        state = IdentityState.CONFIRMED
        reasons: set[Reason] = set()
        for m in members:
            state = worst(state, m.identity_state)
            reasons.update(m.reasons)
        if size_disagreement:
            reasons.add(Reason.DIMENSION_CONFLICT)
            state = worst(state, IdentityState.AMBIGUOUS)
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


def _row_diameter(members: Sequence[PhysicalPipe]) -> tuple[float | None, bool]:
    """The size to publish for a row, and whether its members disagreed.

    The resolved diameter already embodies the label-versus-measurement
    reconciliation, and where the members agree it is simply reported: a code
    like ``VS1-S13`` carries a "13" that is part of the system name rather than
    a size, and the drawn width is what corrects it.

    Where the members *disagree* - the same stated size measured at several
    different widths - reporting any one of them would pick a winner the
    evidence does not support, so the stated size is published and the row is
    marked with a dimension conflict.  What is not done is splitting the row:
    that would assert several pipe sizes for a label naming one.
    """
    resolved = sorted({ql(m.diameter_mm) for m in members if m.diameter_mm is not None})
    nominal = members[0].nominal_diameter_mm
    if len(resolved) == 1:
        return resolved[0], False
    if not resolved:
        return nominal, False
    return (nominal if nominal is not None else resolved[0]), True
