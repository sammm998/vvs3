"""V. Full end-to-end drawing test, plus the post-hoc comparison (§20)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vvs_pipe.cli import main
from vvs_pipe.evaluation import compare_with_ground_truth, load_ground_truth
from vvs_pipe.rendering import render_debug, render_marked
from vvs_pipe.states import IdentityState


@pytest.mark.parametrize("which", ["a", "b"])
def test_cli_produces_every_artefact(which, request, tmp_path):
    drawing = request.getfixturevalue(f"drawing_{which}")
    out = tmp_path / f"run_{which}"
    code = main(["analyse", drawing["files"]["clean"], "--out", str(out), "--forensics"])
    assert code == 0
    for name in ("forensics.json", "analysis.json", "marked.pdf", "quantities.csv", "debug.pdf"):
        assert (out / name).exists(), name

    payload = json.loads((out / "analysis.json").read_text(encoding="utf-8"))
    assert payload["schema"] == "vvs-pipe/analysis/1"
    for section in (
        "drawing",
        "designations",
        "pipeCandidates",
        "pipeRuns",
        "physicalPipes",
        "quantities",
        "verticals",
        "diagnostics",
        "determinism",
    ):
        assert section in payload, section

    forensic_dir = out / "forensics"
    for name in (
        "raw_vectors",
        "glyph_candidates",
        "designation_candidates",
        "pipe_candidates",
        "centerlines",
        "graph",
        "pipe_runs",
        "physical_pipes",
        "verticals",
        "dimensions",
        "associations",
        "measurement_segments",
    ):
        assert (forensic_dir / f"{name}.json").exists(), name


def test_every_result_carries_provenance(analysis_a):
    payload = analysis_a.to_canonical()
    for section in ("glyphs", "designations", "pipeCandidates", "pipeRuns", "physicalPipes"):
        assert payload[section]
        for entry in payload[section]:
            prov = entry["provenance"]
            assert prov["stage"] and prov["rule"]
    for designation in payload["designations"]:
        assert "sourceObjects" in designation
        assert "glyphs" in designation


def test_nothing_is_confirmed_without_evidence(analysis_a):
    for page in analysis_a.pages:
        for pipe in page.physical_pipes:
            if pipe.identity_state is IdentityState.CONFIRMED:
                assert pipe.designation is not None
                assert pipe.diameter_mm is not None
                assert pipe.total_length_m is not None
            if pipe.total_length_m is None:
                assert pipe.reasons, "an unmeasured pipe must say why"


def test_marked_and_debug_drawings_render(analysis_a, tmp_path):
    import fitz

    marked = render_marked(analysis_a, tmp_path / "m.pdf")
    debug = render_debug(analysis_a, tmp_path / "d.pdf")
    for path in (marked, debug):
        doc = fitz.open(path)
        try:
            assert doc.page_count == 1
            page = doc[0]
            assert len(page.get_drawings()) > len(
                fitz.open(analysis_a.source_path)[0].get_drawings()
            )
        finally:
            doc.close()


@pytest.mark.parametrize("which", ["a", "b"])
def test_post_hoc_comparison_against_the_facit(which, request):
    result = request.getfixturevalue(f"analysis_{which}")
    drawing = request.getfixturevalue(f"drawing_{which}")
    truth_path = Path(drawing["dir"]) / f"drawing_{which}_truth.json"
    report = compare_with_ground_truth(result, truth_path)

    summary = report["summary"]
    assert summary["falsePositives"] == 0
    assert summary["falseNegatives"] == 0
    assert summary["precision"] == 1.0
    assert summary["recall"] == 1.0
    assert summary["f1"] == 1.0
    assert summary["designationCoverage"] == 1.0
    assert summary["measurementAccuracy"] == 1.0
    assert summary["duplicateRate"] == 0.0
    for row in report["lengths"]:
        assert row["totalWithinTolerance"] is True
        assert row["diameterWithinTolerance"] is True


def test_the_comparison_reports_differences_rather_than_hiding_them(analysis_a, tmp_path):
    """A deliberately wrong facit must show up as a difference, not be absorbed."""
    truth = {
        "quantities": [
            {
                "designation": q.designation,
                "diameterMm": q.diameter_mm,
                "horizontalM": (q.horizontal_m or 0.0) - 0.3,
                "verticalM": q.vertical_m or 0.0,
                "totalM": (q.total_m or 0.0) - 0.3,
            }
            for q in analysis_a.quantities
            if q.designation
        ]
    }
    path = tmp_path / "wrong_truth.json"
    path.write_text(json.dumps(truth), encoding="utf-8")
    report = compare_with_ground_truth(analysis_a, path)
    diffs = [r["totalDifferenceM"] for r in report["lengths"]]
    assert diffs and all(d is not None and d > 0.0 for d in diffs)


def test_excel_facit_is_readable_by_the_evaluator(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Beteckning", "Dimension", "Horisontell (m)", "Vertikal (m)", "Totalt (m)"])
    ws.append(["XY1-Z2-110", 110, 12.5, 2.6, 15.1])
    path = tmp_path / "facit.xlsx"
    wb.save(path)

    rows = load_ground_truth(path)
    assert len(rows) == 1
    assert rows[0].designation == "XY1-Z2-110"
    assert rows[0].diameter_mm == 110.0
    assert rows[0].total_m == 15.1
