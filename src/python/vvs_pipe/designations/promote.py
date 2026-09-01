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

# How much the label must be aligned with its pipe before alignment alone counts
# as the label being *about* that pipe.  Below this the association rests on
# proximity, and proximity is what puts a note next to whatever happens to run
# past it.
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
    """Did the label point at its pipe, or merely sit beside it?

    Proximity is the weakest thing the association stage measures and the most
    easily satisfied by accident: every note on a busy sheet is close to some
    pipe.  Confirmation therefore needs one of the two signals that mean the
    label is *about* the pipe - a leader line drawn from the text to it, or the
    text set along its axis, which is how an inline pipe label is written.
    """
    for rid in run_ids:
        a = assignments.get(rid)
        if a is None or designation.designation_id not in a.designation_ids:
            continue
        evidence = dict(a.evidence)
        if evidence.get("leader", 0.0) > 0.0:
            return True
        if evidence.get("orientation", 0.0) >= MIN_ALIGNMENT_FOR_CONFIRMATION:
            return True
    return False


def tier_counts(designations: Sequence[Designation]) -> dict[str, int]:
    out = {t.value: 0 for t in DesignationTier}
    for d in designations:
        out[d.tier.value] += 1
    return out
