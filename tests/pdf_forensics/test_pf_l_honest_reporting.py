"""The report may not claim more than the engine knows."""

from __future__ import annotations


def test_coverage_counts_add_up(analysis_a, analysis_b):
    for workspace, report in (analysis_a, analysis_b):
        coverage = report["validation"]["coverage"]
        designations = coverage["designations"]
        associated = {a.candidate_id for a in workspace.associations}
        assert designations["unresolved"] == len(workspace.candidates) - len(associated)
        assert designations["confirmed"] <= len(associated)
        pipes = coverage["pipes"]
        assert pipes["named"] + pipes["unnamed"] == pipes["total"]
        measurement = coverage["measurement"]
        assert measurement["measurable"] + measurement["notMeasurable"] == pipes["total"]


def test_unnamed_pipes_are_reported_not_hidden(analysis_a, analysis_b):
    for workspace, report in (analysis_a, analysis_b):
        unnamed = [p for p in workspace.physical_pipes if not p.designation]
        rows = [r for r in report["quantities"] if not r["designation"]]
        # an unnamed pipe still appears in the take-off, as an unnamed row
        assert bool(rows) == bool(unnamed)
        assert sum(r["pipeCount"] for r in rows) == len(unnamed)
        assert report["validation"]["coverage"]["pipes"]["unnamed"] == len(unnamed)
        for pipe in unnamed:
            assert pipe.designation_state != "CONFIRMED"
            assert pipe.designation_reasons


def test_confidence_is_the_weakest_part(analysis_a):
    workspace, _ = analysis_a
    for pipe in workspace.physical_pipes:
        parts = {k: v for k, v in pipe.confidence.items() if k != "overall"}
        assert pipe.confidence["overall"] == min(parts.values())


def test_validation_lists_every_check(analysis_b):
    _, report = analysis_b
    names = {check["check"] for check in report["validation"]["checks"]}
    assert {"conservation", "reconciliation", "no_metres_without_scale",
            "no_confirmation_without_a_leader_chain"} <= names
