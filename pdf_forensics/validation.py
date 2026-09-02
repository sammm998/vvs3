"""Validation, and the refusal to claim more than is known.

Two jobs.

* **Gates.**  Conservation between the file and the model, reconciliation of
  the entity sets, no confirmed entity without the evidence its state requires,
  and no metre reported without a scale.  A failed gate makes the analysis
  ``INVALID`` - it is not a note beside the numbers.
* **An honest summary.**  Confirmed, ambiguous and unresolved are counted
  separately for designations, pipes, diameters and measurements.  "100 %
  measurable" is only ever printed when it is true of every pipe, and the
  numbers that say otherwise are printed beside it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from .canonical import canonical_json, digest, q, sort_canonical
from .model import Association, DesignationCandidate, PhysicalPipe, Reason, State

VALID = "VALID"
INVALID = "INVALID"


@dataclass
class Check:
    name: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"check": self.name, "ok": self.ok, "detail": self.detail}


def coverage(pipes: Sequence[PhysicalPipe], candidates: Sequence[DesignationCandidate],
             associations: Sequence[Association],
             measurements: Sequence[Any]) -> dict[str, Any]:
    """The counts that stop a run from claiming to be complete."""
    def tally(values: Iterable[str]) -> dict[str, int]:
        out: dict[str, int] = {}
        for value in values:
            out[value] = out.get(value, 0) + 1
        return {k: out[k] for k in sorted(out)}

    named = [p for p in pipes if p.designation]
    measurable = [m for m in measurements if getattr(m, "horizontal_metres", None) is not None]
    return {
        "designations": {
            "candidates": len(candidates),
            "confirmed": len({a.candidate_id for a in associations if a.state == State.CONFIRMED}),
            "ambiguous": len({a.candidate_id for a in associations if a.state == State.AMBIGUOUS}),
            "unresolved": len(candidates) - len({a.candidate_id for a in associations}),
        },
        "pipes": {
            "total": len(pipes),
            "named": len(named),
            "unnamed": len(pipes) - len(named),
            "designationStates": tally(p.designation_state for p in pipes),
            "diameterStates": tally(p.diameter_state for p in pipes),
        },
        "measurement": {
            "measurable": len(measurable),
            "notMeasurable": len(measurements) - len(measurable),
            "fractionMeasurable": q(len(measurable) / len(measurements)) if measurements else 0.0,
        },
    }


def run_checks(conservation: dict, reconciliation: dict, pipes: Sequence[PhysicalPipe],
               associations: Sequence[Association], measurements: Sequence[Any],
               scale_state: str, metres_per_point: Optional[float]) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("conservation", bool(conservation.get("ok")), conservation))
    checks.append(Check("reconciliation", bool(reconciliation.get("ok")),
                        {k: reconciliation[k] for k in sorted(reconciliation)}))

    # What confirms an association is the chain the drawing drew: the label's
    # leader, and the pipe geometry at that leader's end.  The backward
    # direction - is the label in the pipe's neighbourhood - corroborates it and
    # raises confidence, but proximity may never create or confirm an
    # association, so the gate is on the forward evidence.
    unsupported = []
    for association in associations:
        if association.state != State.CONFIRMED:
            continue
        if not association.forward or not association.forward.get("leaderId"):
            unsupported.append(association.association_id)
    checks.append(Check("no_confirmation_without_a_leader_chain", not unsupported,
                        {"offenders": sorted(unsupported)}))

    named_without_association = sorted(
        p.pipe_id for p in pipes
        if p.designation and p.designation_state == State.CONFIRMED
        and not any(a.pipe_id == p.pipe_id and a.state == State.CONFIRMED for a in associations)
    )
    checks.append(Check("no_named_pipe_without_association", not named_without_association,
                        {"offenders": named_without_association}))

    metres_without_scale = sorted(
        getattr(m, "pipe_id", "") for m in measurements
        if getattr(m, "horizontal_metres", None) is not None and metres_per_point is None
    )
    checks.append(Check("no_metres_without_scale", not metres_without_scale,
                        {"scaleState": scale_state, "offenders": metres_without_scale}))

    confirmed_diameters_without_evidence = sorted(
        p.pipe_id for p in pipes
        if p.diameter_state == State.CONFIRMED and p.diameter_mm is None
    )
    checks.append(Check("no_confirmed_diameter_without_value",
                        not confirmed_diameters_without_evidence,
                        {"offenders": confirmed_diameters_without_evidence}))
    return checks


def status(checks: Sequence[Check]) -> str:
    return VALID if all(check.ok for check in checks) else INVALID


def report(checks: Sequence[Check], coverage_payload: dict) -> dict:
    return {
        "status": status(checks),
        "checks": [check.to_json() for check in checks],
        "coverage": coverage_payload,
        "failed": sorted(check.name for check in checks if not check.ok),
    }


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------

def result_digest(payload: Any) -> str:
    """A digest of an analysis, ignoring anything that measures the run itself."""
    return digest(_strip_volatile(payload), length=16)


def _strip_volatile(payload: Any) -> Any:
    volatile = {"elapsedSeconds", "generatedAt", "durationSeconds", "timings"}
    if isinstance(payload, dict):
        return {k: _strip_volatile(v) for k, v in sorted(payload.items()) if k not in volatile}
    if isinstance(payload, (list, tuple)):
        return [_strip_volatile(v) for v in payload]
    return payload


def determinism_check(run, orders: Sequence[str] = ("normal", "reversed",
                                                    "permuted:1", "permuted:2")) -> dict:
    """Run the same analysis over permuted input orders and compare digests.

    ``run(order)`` must return the analysis payload.  Identical digests are the
    only acceptable outcome: if the order objects arrive in can change an
    answer, then something in the engine is using position as evidence.
    """
    digests: dict[str, str] = {}
    for order in orders:
        digests[order] = result_digest(run(order))
    values = sorted(set(digests.values()))
    return {
        "orders": {k: digests[k] for k in sorted(digests)},
        "identical": len(values) == 1,
        "distinctResults": len(values),
    }
