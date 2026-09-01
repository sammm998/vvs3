"""L. Dimension test, N. Scale test."""

from __future__ import annotations

import pytest

from vvs_pipe.dimensions import resolve_diameter
from vvs_pipe.measurement.scale import POINT_IN_METRES
from vvs_pipe.states import Reason, ScaleState


def test_label_and_measurement_agreeing_keeps_the_nominal_figure():
    mpp = POINT_IN_METRES * 50.0
    width_pt = 0.110 / mpp
    r = resolve_diameter(110.0, width_pt, mpp)
    assert r.diameter_mm == pytest.approx(110.0)
    assert r.source == "label+measured"
    assert r.reasons == ()


def test_label_and_measurement_disagreeing_reports_the_measurement_and_the_conflict():
    mpp = POINT_IN_METRES * 100.0
    width_pt = 0.040 / mpp  # the drawing says 40 mm
    r = resolve_diameter(13.0, width_pt, mpp)  # the label says 13
    assert r.diameter_mm == pytest.approx(40.0, abs=0.2)
    assert Reason.DIMENSION_CONFLICT in r.reasons
    assert r.label_mm == 13.0


def test_no_scale_means_the_label_alone_and_a_reason():
    r = resolve_diameter(110.0, 6.24, None)
    assert r.diameter_mm == 110.0
    assert Reason.SCALE_UNKNOWN in r.reasons


def test_no_evidence_means_no_diameter():
    r = resolve_diameter(None, None, None)
    assert r.diameter_mm is None
    assert Reason.NO_DIMENSION_EVIDENCE in r.reasons


def test_scale_is_read_from_the_sheet_and_cross_checked(analysis_a, specs_by_stem):
    spec = specs_by_stem["drawing_a"]
    scale = analysis_a.pages[0].scale
    # Two different kinds of source agreeing is the strongest available
    # outcome; one alone would only be RESOLVED.
    assert scale.state is ScaleState.SCALE_CONFIRMED
    assert scale.ratio_denominator == pytest.approx(spec.scale_denominator)
    assert scale.metres_per_point == pytest.approx(spec.metres_per_point, rel=1e-3)
    names = {k.split("[")[0] for k, _v in scale.sources}
    assert "ratioNote" in names
    assert "scaleBar" in names, "the scale bar should corroborate the note"
    assert ("agreeingSources", 2.0) in scale.sources


def test_a_different_sheet_gets_its_own_scale(analysis_b, specs_by_stem):
    spec = specs_by_stem["drawing_b"]
    scale = analysis_b.pages[0].scale
    assert scale.ratio_denominator == pytest.approx(spec.scale_denominator)


def test_without_a_scale_nothing_is_presented_as_measured(tmp_path):
    """A sheet carrying no scale evidence must refuse to state metres."""
    import fitz

    from tests.fixtures.stroke_font import text_strokes
    from vvs_pipe.pipeline import analyse

    doc = fitz.open()
    page = doc.new_page(width=400, height=300)

    def stroke(points, width):
        shape = page.new_shape()
        pts = [fitz.Point(*p) for p in points]
        for i in range(len(pts) - 1):
            shape.draw_line(pts[i], pts[i + 1])
        shape.finish(color=(0, 0, 0), width=width, closePath=False)
        shape.commit()

    stroke([(60, 196), (340, 196)], 0.35)
    stroke([(60, 204), (340, 204)], 0.35)
    for poly in text_strokes("AB1-C2-110", (120, 150), 7.0):
        stroke(poly, 0.25)
    stroke([(155, 152), (190, 192)], 0.2)  # leader

    out = tmp_path / "no_scale.pdf"
    doc.save(str(out))
    doc.close()

    result = analyse(out, blind=True)
    scale = result.pages[0].scale
    assert scale.state is ScaleState.SCALE_UNKNOWN
    assert scale.metres_per_point is None
    assert result.quantities, "the pipe should still be found, just not measured"
    for q in result.quantities:
        assert q.total_m is None
        assert q.horizontal_m is None
        assert Reason.SCALE_UNKNOWN in q.reasons
    # ... and the pipe itself is present, with its length only in points.
    pipes = [p for page in result.pages for p in page.physical_pipes]
    assert pipes and all(p.length_pt > 0 for p in pipes)
