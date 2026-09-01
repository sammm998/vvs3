"""No metres without a scale, and no vertical metres without heights."""

from __future__ import annotations

import math

from pdf_forensics.measurement import aggregate, measure
from pdf_forensics.model import Reason, State
from pdf_forensics.scale import metres_per_point


def test_scale_is_resolved_from_more_than_one_signal(analysis_a, analysis_b):
    for _, report in (analysis_a, analysis_b):
        scale = report["stages"]["scale"]
        assert scale["state"] == State.CONFIRMED
        assert len({h["source"] for h in scale["hypotheses"]}) >= 2
        assert "AGREEING_INDEPENDENT_SOURCES" in scale["reasons"]


def test_a_stated_ratio_is_used_exactly(analysis_a):
    _, report = analysis_a
    assert report["stages"]["scale"]["denominator"] == 50.0
    assert math.isclose(report["stages"]["scale"]["metresPerPoint"],
                        metres_per_point(50.0), rel_tol=1e-12)


def test_no_scale_means_no_metres(analysis_a):
    workspace, _ = analysis_a
    without = measure(workspace.physical_pipes, workspace.risers, None, State.UNRESOLVED)
    assert all(m.horizontal_metres is None for m in without)
    assert all(m.total_metres is None for m in without)
    assert all(Reason.SCALE_UNKNOWN in m.reasons for m in without)
    rows = aggregate(workspace.physical_pipes, without)
    assert all(row["totalMetres"] == 0.0 and row["notMeasurableCount"] > 0 for row in rows)


def test_one_elevation_is_not_a_height(analysis_a):
    workspace, _ = analysis_a
    single = [r for r in workspace.risers if len(r.elevations) == 1]
    assert single, "drawing A has a riser with only one level"
    for riser in single:
        assert riser.height_metres is None
        assert Reason.VERTICAL_HEIGHT_UNKNOWN in riser.reasons


def test_two_elevations_give_the_difference(analysis_a):
    workspace, _ = analysis_a
    pairs = [r for r in workspace.risers if len(r.elevations) >= 2]
    assert pairs
    for riser in pairs:
        assert math.isclose(riser.height_metres,
                            max(riser.elevations) - min(riser.elevations), rel_tol=1e-9)


def test_horizontal_and_vertical_are_reported_separately(analysis_a):
    _, report = analysis_a
    rows = {row["designation"]: row for row in report["quantities"]}
    vertical_rows = [r for r in rows.values() if r["verticalMetres"] > 0]
    assert vertical_rows
    for row in vertical_rows:
        assert math.isclose(row["totalMetres"],
                            row["horizontalMetres"] + row["verticalMetres"], rel_tol=1e-9)
