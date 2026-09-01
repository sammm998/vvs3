"""PipeRuns -> PhysicalPipes.

A PhysicalPipe is the unit a quantity surveyor counts: one continuous length
of one pipe of one size, however many fragments the CAD file happened to split
it into.  Runs are merged when they share a designation *and* a size *and*
touch end to end.

Double counting is prevented structurally: every run belongs to exactly one
physical pipe (the merge is a partition of the run set), and the reconciliation
in :mod:`vvs_pipe.validation.reconcile` re-checks that invariant on the way
out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from ..canonical import canonical_sort, entity_id, ql, qs
from ..geometry.index import connected_components
from ..geometry.primitives import dist, polyline_length
from ..model import Confidence, PhysicalPipe, PipeRun, Provenance
from ..states import IdentityState, Reason, worst

Pt = tuple[float, float]

JOIN_TOLERANCE_PT = 1.5


@dataclass(frozen=True, slots=True)
class RunAssignment:
    """What the association stage decided for one run."""

    designation: str | None
    designation_ids: tuple[str, ...]
    diameter_mm: float | None
    state: IdentityState
    reasons: tuple[Reason, ...]
    association_confidence: float
    evidence: tuple[tuple[str, float], ...]
    # The size the *label* states, kept apart from the size the geometry was
    # measured at.  They are different facts about the same pipe and the
    # quantity list needs the first; the dimension stage reports the second and
    # any disagreement between them.
    label_diameter_mm: float | None = None


def build_physical_pipes(
    runs: Sequence[PipeRun],
    assignments: Mapping[str, RunAssignment],
    page: int,
    metres_per_point: float | None,
    vertical_by_run: Mapping[str, tuple[str, float | None]] | None = None,
) -> tuple[PhysicalPipe, ...]:
    ordered = canonical_sort(list(runs), key=lambda r: r.canonical_key())
    if not ordered:
        return ()
    vertical_by_run = vertical_by_run or {}

    def group_key(r: PipeRun) -> tuple:
        a = assignments.get(r.pipe_run_id)
        return (
            a.designation if a and a.designation else "",
            ql(a.diameter_mm) if a and a.diameter_mm is not None else -1.0,
        )

    # Endpoints are bucketed on a grid whose cell is the join tolerance, so
    # only runs that could actually touch are ever compared.  Complexity is
    # O(n) buckets plus the pairs inside them, not O(n^2).
    buckets: dict[tuple, list[int]] = {}
    for i, r in enumerate(ordered):
        for p in (r.centerline[0], r.centerline[-1]):
            cell = (
                int(math.floor(p[0] / JOIN_TOLERANCE_PT)),
                int(math.floor(p[1] / JOIN_TOLERANCE_PT)),
            )
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    buckets.setdefault((group_key(r), cell[0] + dx, cell[1] + dy), []).append(i)

    edges: set[tuple[int, int]] = set()
    for key in sorted(buckets, key=lambda k: (str(k[0]), k[1], k[2])):
        members = sorted(set(buckets[key]))
        for a_pos in range(len(members)):
            for b_pos in range(a_pos + 1, len(members)):
                i, j = members[a_pos], members[b_pos]
                if group_key(ordered[i]) != group_key(ordered[j]):
                    continue
                if _touching(ordered[i].centerline, ordered[j].centerline):
                    edges.add((i, j))
    comps = connected_components(len(ordered), sorted(edges))

    out: list[PhysicalPipe] = []
    for comp in comps:
        members = [ordered[i] for i in comp]
        geoms = tuple(
            canonical_sort([m.centerline for m in members], key=lambda p: tuple((round(x, 4), round(y, 4)) for x, y in p))
        )
        length_pt = sum(polyline_length(m.centerline) for m in members)
        assigns = [assignments.get(m.pipe_run_id) for m in members]
        present = [a for a in assigns if a is not None]
        designation = present[0].designation if present and present[0].designation else None
        diameter = present[0].diameter_mm if present else None
        nominal = present[0].label_diameter_mm if present else None
        designation_ids = tuple(sorted({d for a in present for d in a.designation_ids}))
        state = IdentityState.HIGH_CONFIDENCE
        reasons: list[Reason] = []
        for a in present:
            state = worst(state, a.state)
            reasons.extend(a.reasons)
        if not present:
            state = IdentityState.INSUFFICIENT
            reasons.append(Reason.NO_ASSOCIATION_EVIDENCE)
        for m in members:
            state = worst(state, m.state)
            reasons.extend(m.reasons)

        vertical_ids: list[str] = []
        vertical_m: float | None = None
        vertical_unknown = False
        for m in members:
            vid, vlen = vertical_by_run.get(m.pipe_run_id, (None, None))
            if vid is None:
                continue
            vertical_ids.append(vid)
            if vlen is None:
                vertical_unknown = True
            else:
                vertical_m = (vertical_m or 0.0) + vlen
        if vertical_unknown:
            reasons.append(Reason.VERTICAL_HEIGHT_UNKNOWN)
            state = worst(state, IdentityState.AMBIGUOUS)

        if metres_per_point is None:
            horizontal_m = None
            total_m = None
            reasons.append(Reason.SCALE_UNKNOWN)
            state = worst(state, IdentityState.INSUFFICIENT)
        else:
            horizontal_m = length_pt * metres_per_point
            total_m = horizontal_m + (vertical_m or 0.0)

        evidence = tuple(
            sorted(
                {
                    ("runs", float(len(members))),
                    ("lengthPt", qs(length_pt)),
                }
            )
        )
        conf = Confidence(
            geometry=qs(min(m.confidence.geometry or 0.0 for m in members)) if members else 0.0,
            topology=qs(min(m.confidence.topology or 0.0 for m in members)) if members else 0.0,
            association=qs(min([a.association_confidence for a in present], default=0.0)),
            dimension=None if diameter is None else 0.9,
            vertical=None if not vertical_ids else (0.9 if not vertical_unknown else 0.2),
        )
        pid = entity_id("pp", (page, geoms))
        out.append(
            PhysicalPipe(
                physical_pipe_id=pid,
                page=page,
                pipe_run_ids=tuple(sorted(m.pipe_run_id for m in members)),
                centerline=geoms,
                source_object_ids=tuple(sorted({o for m in members for o in m.source_object_ids})),
                horizontal_length_m=None if horizontal_m is None else ql(horizontal_m),
                vertical_length_m=None if vertical_m is None else ql(vertical_m),
                total_length_m=None if total_m is None else ql(total_m),
                length_pt=ql(length_pt),
                diameter_mm=diameter,
                nominal_diameter_mm=nominal,
                designation=designation,
                designation_ids=designation_ids,
                vertical_ids=tuple(sorted(vertical_ids)),
                identity_state=state,
                reasons=tuple(sorted(set(reasons), key=lambda r: r.value)),
                confidence=conf,
                evidence=evidence,
                provenance=Provenance(
                    stage="physical-pipe",
                    rule="merge runs sharing designation and size that touch end to end",
                    inputs=tuple(sorted(m.pipe_run_id for m in members)),
                ),
            )
        )
    return tuple(canonical_sort(out, key=lambda p: p.canonical_key()))


def _touching(a: Sequence[Pt], b: Sequence[Pt]) -> bool:
    for pa in (a[0], a[-1]):
        for pb in (b[0], b[-1]):
            if dist(pa, pb) <= JOIN_TOLERANCE_PT:
                return True
    return False
