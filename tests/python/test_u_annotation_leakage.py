"""U. Annotation leakage test - CLEAN_PDF_NO_ANNOTATION_LEAKAGE."""

from __future__ import annotations

import fitz
import pytest

from vvs_pipe.pdf_forensics import forensic_report
from vvs_pipe.pipeline import analyse
from vvs_pipe.vector_extraction import extract_document


def test_the_fixture_really_carries_previous_take_off_markup(drawing_a):
    marked = drawing_a["files"]["with_takeoff"]
    report = forensic_report(marked).data
    assert report["annotationCount"] >= 2
    subtypes = {name for name, _count in report["annotationSubtypes"]}
    assert subtypes, "the marked-up fixture must contain annotations"
    # PyMuPDF walks annotation appearance streams as drawings; the extractor has
    # to suppress them, and the forensic report says how many it suppressed.
    assert report["annotationAppearanceObjectCount"] >= 2


def test_extraction_of_the_marked_pdf_equals_extraction_of_the_clean_pdf(drawing_a):
    clean = extract_document(drawing_a["files"]["clean"])
    marked = extract_document(drawing_a["files"]["with_takeoff"])
    assert marked.excluded_annotation_objects >= 2
    assert [o.canonical_key() for o in clean.objects] == [
        o.canonical_key() for o in marked.objects
    ]


def test_analysis_is_identical_with_and_without_previous_take_off(drawing_a):
    """CLEAN_PDF_NO_ANNOTATION_LEAKAGE."""
    clean = analyse(drawing_a["files"]["clean"], blind=True)
    marked = analyse(drawing_a["files"]["with_takeoff"], blind=True)
    assert clean.canonical_digest() == marked.canonical_digest()


def test_no_extracted_object_comes_from_an_annotation(drawing_a):
    doc = extract_document(drawing_a["files"]["with_takeoff"])
    assert all(not o.from_annotation for o in doc.objects)


def test_flattened_markup_matching_an_annotation_is_dropped(drawing_a, tmp_path):
    """Markup baked into the content stream is still removed when the
    annotation that describes it is present."""
    src = fitz.open(drawing_a["files"]["clean"])
    page = src[0]
    pts = [fitz.Point(200, 500), fitz.Point(400, 500), fitz.Point(400, 560)]
    shape = page.new_shape()
    shape.draw_line(pts[0], pts[1])
    shape.draw_line(pts[1], pts[2])
    shape.finish(color=(1, 0, 0), width=2.0, closePath=False)
    shape.commit()
    annot = page.add_polyline_annot(pts)
    annot.set_colors(stroke=(1, 0, 0))
    annot.update()
    out = tmp_path / "flattened.pdf"
    src.save(str(out))
    src.close()

    doc = extract_document(out)
    assert doc.excluded_annotation_objects >= 1
    for o in doc.objects:
        assert o.stroke_color != (1.0, 0.0, 0.0), "red markup leaked into the geometry"
