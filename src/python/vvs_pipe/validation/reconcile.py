"""Reconciliation: every metre is accounted for exactly once.

The chain source geometry -> candidates -> runs -> physical pipes -> quantity
rows is checked at each hand-off:

* no run belongs to two physical pipes;
* no physical pipe appears in two quantity rows;
* the summed physical length equals the summed run length to within the
  quantisation grid;
* no two accepted candidates describe the same centerline.

A failure here is a defect in the engine, not a property of the drawing, so it
is reported as an explicit list rather than being smoothed over.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..canonical import ql
from ..geometry.primitives import polyline_length
from ..model import PhysicalPipe, PipeCandidate, PipeRun, QuantityRow

LENGTH_TOLERANCE_M = 0.002


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    ok: bool
    problems: tuple[str, ...]
    run_length_pt: float
    physical_length_pt: float
    duplicate_centerlines: int
    runs_in_multiple_pipes: int
    pipes_in_multiple_rows: int

    def to_canonical(self) -> dict:
        return {
            "ok": self.ok,
            "problems": list(self.problems),
            "runLengthPt": ql(self.run_length_pt),
            "physicalLengthPt": ql(self.physical_length_pt),
            "duplicateCenterlines": self.duplicate_centerlines,
            "runsInMultiplePipes": self.runs_in_multiple_pipes,
            "pipesInMultipleRows": self.pipes_in_multiple_rows,
        }


def reconcile(
    candidates: Sequence[PipeCandidate],
    runs: Sequence[PipeRun],
    pipes: Sequence[PhysicalPipe],
    rows: Sequence[QuantityRow],
) -> ReconciliationReport:
    problems: list[str] = []

    seen_centerlines: dict[tuple, int] = {}
    for c in candidates:
        if not c.accepted:
            continue
        key = c.canonical_key()
        seen_centerlines[key] = seen_centerlines.get(key, 0) + 1
    duplicates = sum(v - 1 for v in seen_centerlines.values() if v > 1)
    if duplicates:
        problems.append(f"{duplicates} accepted candidates share a centerline")

    run_owner: dict[str, int] = {}
    for p in pipes:
        for rid in p.pipe_run_ids:
            run_owner[rid] = run_owner.get(rid, 0) + 1
    multi_runs = sum(1 for v in run_owner.values() if v > 1)
    if multi_runs:
        problems.append(f"{multi_runs} runs belong to more than one physical pipe")

    pipe_owner: dict[str, int] = {}
    for r in rows:
        for pid in r.physical_pipe_ids:
            pipe_owner[pid] = pipe_owner.get(pid, 0) + 1
    multi_pipes = sum(1 for v in pipe_owner.values() if v > 1)
    if multi_pipes:
        problems.append(f"{multi_pipes} physical pipes appear in more than one quantity row")

    run_len = sum(polyline_length(r.centerline) for r in runs)
    phys_len = sum(p.length_pt for p in pipes)
    covered = {rid for p in pipes for rid in p.pipe_run_ids}
    missing = [r.pipe_run_id for r in runs if r.pipe_run_id not in covered]
    if missing:
        problems.append(f"{len(missing)} runs are not part of any physical pipe")
    elif abs(run_len - phys_len) > 0.01:
        problems.append(
            f"physical length {ql(phys_len)}pt does not match run length {ql(run_len)}pt"
        )

    return ReconciliationReport(
        ok=not problems,
        problems=tuple(problems),
        run_length_pt=run_len,
        physical_length_pt=phys_len,
        duplicate_centerlines=duplicates,
        runs_in_multiple_pipes=multi_runs,
        pipes_in_multiple_rows=multi_pipes,
    )
