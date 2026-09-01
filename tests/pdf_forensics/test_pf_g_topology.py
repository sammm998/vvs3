"""Elbows join, tees make nodes, and a metre is counted once."""

from __future__ import annotations

import math

from pdf_forensics.topology import reconcile


def test_an_elbow_becomes_one_run(analysis_a):
    workspace, _ = analysis_a
    bends = [r for r in workspace.runs if len(r.centerline) > 2]
    assert bends, "drawing A has an L-shaped branch"
    for run in bends:
        for a, b in zip(run.centerline, run.centerline[1:]):
            assert math.dist(a, b) > 0.0


def test_a_branch_makes_a_junction(analysis_a):
    workspace, report = analysis_a
    assert report["stages"]["topology"]["junctions"] >= 1


def test_reconciliation_is_a_gate(analysis_a, analysis_b):
    for workspace, report in (analysis_a, analysis_b):
        result = reconcile(workspace.pipe_candidates, workspace.runs, workspace.physical_pipes)
        assert result["ok"], result
        assert report["validation"]["status"] == "VALID"


def test_pipe_identity_does_not_depend_on_a_label(analysis_a):
    workspace, _ = analysis_a
    unnamed = [p for p in workspace.physical_pipes if not p.designation]
    named = [p for p in workspace.physical_pipes if p.designation]
    assert named
    for pipe in unnamed + named:
        assert pipe.run_ids and pipe.centerline
        assert pipe.pipe_id.startswith("pipe:")


def test_no_run_belongs_to_two_pipes(analysis_b):
    workspace, _ = analysis_b
    owners: dict[str, list[str]] = {}
    for pipe in workspace.physical_pipes:
        for run_id in pipe.run_ids:
            owners.setdefault(run_id, []).append(pipe.pipe_id)
    assert all(len(v) == 1 for v in owners.values())
