"""O. Measurement test, P. duplicate-prevention test."""

from __future__ import annotations

import pytest


def _truth_index(truth) -> dict[tuple[str, int], dict]:
    return {(r["designation"], round(r["diameterMm"])): r for r in truth["quantities"]}


def _detected_index(result) -> dict[tuple[str, int], dict]:
    out: dict[tuple[str, int], dict] = {}
    for q in result.quantities:
        if q.designation is None or q.diameter_mm is None:
            continue
        key = (q.designation, round(q.diameter_mm))
        row = out.setdefault(key, {"horizontalM": 0.0, "verticalM": 0.0, "totalM": 0.0})
        row["horizontalM"] += q.horizontal_m or 0.0
        row["verticalM"] += q.vertical_m or 0.0
        row["totalM"] += q.total_m or 0.0
    return out


@pytest.mark.parametrize("which", ["a", "b"])
def test_measured_lengths_match_the_drawn_geometry(which, request):
    result = request.getfixturevalue(f"analysis_{which}")
    truth = request.getfixturevalue(f"drawing_{which}")
    want = _truth_index(truth)
    got = _detected_index(result)
    assert set(want) == set(got), (sorted(want), sorted(got))
    for key, expected in want.items():
        actual = got[key]
        assert actual["horizontalM"] == pytest.approx(expected["horizontalM"], abs=0.01)
        assert actual["verticalM"] == pytest.approx(expected["verticalM"], abs=0.01)
        assert actual["totalM"] == pytest.approx(expected["totalM"], abs=0.01)


@pytest.mark.parametrize("which", ["a", "b"])
def test_reconciliation_shows_no_double_counting(which, request):
    result = request.getfixturevalue(f"analysis_{which}")
    report = result.reconciliation
    assert report.ok, report.problems
    assert report.duplicate_centerlines == 0
    assert report.runs_in_multiple_pipes == 0
    assert report.pipes_in_multiple_rows == 0
    assert report.run_length_pt == pytest.approx(report.physical_length_pt, abs=0.01)


def test_a_physical_pipe_appears_in_exactly_one_quantity_row(analysis_a):
    seen: dict[str, int] = {}
    for q in analysis_a.quantities:
        for pid in q.physical_pipe_ids:
            seen[pid] = seen.get(pid, 0) + 1
    all_ids = {p.physical_pipe_id for page in analysis_a.pages for p in page.physical_pipes}
    assert set(seen) == all_ids
    assert all(v == 1 for v in seen.values())


def test_total_detected_length_equals_the_sum_of_the_rows(analysis_a):
    rows = sum(q.total_m or 0.0 for q in analysis_a.quantities)
    pipes = sum(
        p.total_length_m or 0.0 for page in analysis_a.pages for p in page.physical_pipes
    )
    assert rows == pytest.approx(pipes, abs=1e-4)
