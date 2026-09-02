"""Promotion: text becomes a designation only when a pipe vouches for it.

This is the stage the engine was missing.  Reading text and deciding what it
means used to be one step, so a string was a pipe designation because of the way
it was spelled - two alphanumeric runs and a hyphen was enough - and it was
*confirmed* if any stroke happened to lie near it.  On a real sheet that
promoted dates, drawing cross-references, duct schedules and mis-read notes into
the quantity list, because all of those are spelled like codes and all of them
sit near linework.

Here the order is inverted.  Pipes are detected from geometry, without reading
anything.  Text is read, and the most a reading can earn on its own is
DESIGNATION_CANDIDATE - a claim, not a fact.  A candidate becomes a
CONFIRMED_DESIGNATION only when the association stage, working from geometry,
actually bound it to pipe runs that survived into a physical pipe.  Text that no
pipe accepted stays a candidate for ever and is reported as one; it never
reaches a quantity row.

The consequence worth stating plainly: a drawing whose lettering is unreadable
loses its *labels* and keeps its pipes, and a drawing full of code-like notes
gains no pipes from them.  Neither was true before.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping, Sequence

from ..canonical import canonical_sort, qs
from ..model import Designation, PhysicalPipe
from ..topology.physical import RunAssignment
from ..states import DesignationTier, IdentityState, Reason, TextRole

# A promoted designation must have reached pipes carrying at least this much
# drawn length in total.  A code-like note that a single stub of geometry
# happened to accept is not evidence that the note names a pipe.
MIN_PROMOTED_LENGTH_PT = 1.0

# Kept for the report's vocabulary only: alignment is measured and published as
# evidence, but it no longer promotes anything on its own.
MIN_ALIGNMENT_FOR_CONFIRMATION = 0.75


def promote_designations(
    designations: Sequence[Designation],
    designation_to_runs: Mapping[str, Sequence[str]],
    pipes: Sequence[PhysicalPipe],
    assignments: Mapping[str, RunAssignment] | None = None,
) -> tuple[Designation, ...]:
    """Set each designation's tier from what the geometry actually accepted."""
    pipes_by_run: dict[str, list[PhysicalPipe]] = {}
    for p in pipes:
        for rid in p.pipe_run_ids:
            pipes_by_run.setdefault(rid, []).append(p)
    assignments = assignments or {}

    # How often each token structure recurs among the sheet's own candidates.
    # A real designation belongs to a family - a drawing that has S3-R8-75 has
    # S3-R8-110 and S1-P2-75 written the same way - while a note or a date
    # matches nothing else.  This is recorded as evidence and deliberately not
    # used as a gate: where the cut would have to fall to separate them on one
    # sheet is a fact about that sheet, and a drawing carrying a single system
    # would fail any such test.  A reviewer can see it; the engine does not act
    # on it.
    pattern_counts: dict[str, int] = {}
    for d in designations:
        if d.role is TextRole.PIPE_DESIGNATION and not d.is_legend:
            pattern_counts[d.structure.pattern] = pattern_counts.get(d.structure.pattern, 0) + 1

    out: list[Designation] = []
    for d in canonical_sort(list(designations), key=lambda x: x.canonical_key()):
        run_ids = list(designation_to_runs.get(d.designation_id, ()))
        reached: dict[str, PhysicalPipe] = {}
        for rid in run_ids:
            for p in pipes_by_run.get(rid, ()):
                # The pipe must agree that this is its designation; a run can be
                # scored against a label and still end up carrying another one.
                if p.designation == d.text:
                    reached[p.physical_pipe_id] = p
        length = sum(p.length_pt for p in reached.values())
        pointed = _pointed_at_a_pipe(d, run_ids, assignments)

        candidate = d.role is TextRole.PIPE_DESIGNATION and not d.is_legend
        evidence = (
            ("runsAssociated", float(len(run_ids))),
            ("physicalPipesReached", float(len(reached))),
            ("pipeLengthPt", qs(length)),
            ("pointsAtItsPipe", 1.0 if pointed else 0.0),
            ("sharesItsStructureWith", float(pattern_counts.get(d.structure.pattern, 0) - 1)),
        )

        if candidate and reached and pointed and length >= MIN_PROMOTED_LENGTH_PT:
            tier = DesignationTier.CONFIRMED_DESIGNATION
            reasons = tuple(r for r in d.reasons if r is not Reason.NO_PIPE_EVIDENCE)
            state = d.state
        elif candidate:
            tier = DesignationTier.DESIGNATION_CANDIDATE
            reasons = tuple(sorted(set(d.reasons) | {Reason.NO_PIPE_EVIDENCE}, key=lambda r: r.value))
            # Without a pipe the reading cannot be better than ambiguous,
            # whatever the text stage thought of its own spelling.
            state = (
                IdentityState.AMBIGUOUS
                if d.state in (IdentityState.CONFIRMED, IdentityState.HIGH_CONFIDENCE)
                else d.state
            )
        else:
            tier = DesignationTier.TEXT_ONLY
            reasons = d.reasons
            state = d.state

        out.append(
            replace(
                d,
                tier=tier,
                state=state,
                reasons=reasons,
                pipe_evidence=evidence,
                associated_physical_pipe_ids=tuple(sorted(reached)),
            )
        )
    return tuple(canonical_sort(out, key=lambda x: x.canonical_key()))


def _pointed_at_a_pipe(
    designation: Designation,
    run_ids: Sequence[str],
    assignments: Mapping[str, RunAssignment],
) -> bool:
    """Did the drawing point this label at this pipe?

    There is now exactly one thing that means yes: a leader was traced from the
    label and its endpoint was verified against pipe geometry.  Alignment used
    to count as well, because a label written along a pipe was taken to be an
    inline label naming it - and that, together with proximity, is what
    promoted notes and dates.  A pipe with no leader keeps its geometry and is
    reported unnamed; nothing is inferred from where a string happens to sit.
    """
    for rid in run_ids:
        a = assignments.get(rid)
        if a is None or designation.designation_id not in a.designation_ids:
            continue
        if dict(a.evidence).get("leaderTraced", 0.0) > 0.0:
            return True
    return False


def tier_counts(designations: Sequence[Designation]) -> dict[str, int]:
    out = {t.value: 0 for t in DesignationTier}
    for d in designations:
        out[d.tier.value] += 1
    return out
