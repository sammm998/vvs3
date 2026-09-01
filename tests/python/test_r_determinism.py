"""R/S/T. Order independence and repeat determinism.

Every stage is exercised with the original order, the reversed order and two
seeded permutations, and the canonical digests must be identical.
"""

from __future__ import annotations

import pytest

from vvs_pipe.designations import detect_panels, discover_designations
from vvs_pipe.geometry.primitives import BBox
from vvs_pipe.glyph import segment_glyphs
from vvs_pipe.measurement import aggregate_quantities, detect_scale
from vvs_pipe.pipeline import PipelineConfig, analyse, analyse_extracted
from vvs_pipe.pipes import detect_pipes
from vvs_pipe.text_reconstruction import reconstruct_text
from vvs_pipe.topology import build_graph, build_runs
from vvs_pipe.validation.determinism import digest_of_stage, permutations_of
from vvs_pipe.vector_extraction import extract_document


@pytest.fixture(scope="module")
def doc(drawing_a):
    return extract_document(drawing_a["files"]["clean"])


def _page_box(doc):
    p = doc.pages[0]
    return BBox(0, 0, p.width, p.height)


def _assert_invariant(build) -> None:
    baseline: str | None = None
    for name, digest in build():
        if baseline is None:
            baseline = digest
        else:
            assert digest == baseline, f"{name} differs from original"


def test_glyph_segmentation_is_order_independent(doc):
    box = _page_box(doc)

    def build():
        for name, permuted in permutations_of(doc.objects_on(0)):
            seg = segment_glyphs(permuted, box)
            yield name, digest_of_stage(
                seg.lines,
                lambda l: {
                    "bbox": l.bbox.to_canonical(),
                    "glyphs": [g.bbox.to_canonical() for g in l.glyphs],
                },
            )

    _assert_invariant(build)


def test_text_reconstruction_is_order_independent(doc):
    box = _page_box(doc)

    def build():
        for name, permuted in permutations_of(doc.objects_on(0)):
            seg = segment_glyphs(permuted, box)
            text = reconstruct_text(seg, doc.spans_on(0), 0)
            yield name, digest_of_stage(text.items)

    _assert_invariant(build)


def test_designation_discovery_is_order_independent(doc):
    box = _page_box(doc)
    seg = segment_glyphs(doc.objects_on(0), box)
    text = reconstruct_text(seg, doc.spans_on(0), 0)

    def build():
        for name, permuted in permutations_of(doc.objects_on(0)):
            panels = detect_panels(permuted, text.items, box, 0)
            discovery = discover_designations(text.items, permuted, panels, box, 0)
            yield name, digest_of_stage(discovery.designations)

    _assert_invariant(build)


def test_pipe_detection_is_order_independent(doc):
    box = _page_box(doc)
    seg = segment_glyphs(doc.objects_on(0), box)
    text = reconstruct_text(seg, doc.spans_on(0), 0)
    panels = detect_panels(doc.objects_on(0), text.items, box, 0)
    boxes = [p.bbox for p in panels]

    def build():
        for name, permuted in permutations_of(doc.objects_on(0)):
            det = detect_pipes(permuted, box, 0, text.consumed_object_ids, boxes, 7.0)
            yield name, digest_of_stage(det.candidates)

    _assert_invariant(build)


def test_graph_and_runs_are_order_independent(analysis_a):
    page = analysis_a.pages[0]

    def build_graph_digests():
        for name, permuted in permutations_of(list(page.candidates)):
            graph = build_graph(permuted, page.page)
            yield name, digest_of_stage([graph], lambda g: g.to_canonical())

    def build_run_digests():
        for name, permuted in permutations_of(list(page.candidates)):
            runs = build_runs(build_graph(permuted, page.page), page.page)
            yield name, digest_of_stage(runs)

    _assert_invariant(build_graph_digests)
    _assert_invariant(build_run_digests)


def test_quantity_aggregation_is_order_independent(analysis_a):
    pipes = [p for page in analysis_a.pages for p in page.physical_pipes]

    def build():
        for name, permuted in permutations_of(pipes):
            yield name, digest_of_stage(aggregate_quantities(permuted))

    _assert_invariant(build)


def test_scale_detection_is_order_independent(doc):
    box = _page_box(doc)
    seg = segment_glyphs(doc.objects_on(0), box)
    text = reconstruct_text(seg, doc.spans_on(0), 0)

    def build():
        for name, permuted in permutations_of(doc.objects_on(0)):
            scale = detect_scale(text.items, permuted, box)
            yield name, digest_of_stage([scale], lambda s: s.to_canonical())

    _assert_invariant(build)


def test_the_whole_pipeline_is_order_independent(doc, drawing_a):
    """Original, reversed and two seeded permutations of the object list."""
    from dataclasses import replace

    from vvs_pipe.pdf_forensics import forensic_report

    forensics = forensic_report(drawing_a["files"]["clean"])
    baseline = None
    for name, permuted in permutations_of(doc.objects):
        shuffled = replace(doc, objects=list(permuted)) if hasattr(doc, "__dataclass_fields__") else None
        assert shuffled is not None
        result = analyse_extracted(
            shuffled, forensics, drawing_a["files"]["clean"], PipelineConfig(), True
        )
        d = result.canonical_digest()
        if baseline is None:
            baseline = d
        else:
            assert d == baseline, f"pipeline digest differs for {name}"


def test_repeated_runs_are_identical(drawing_a):
    first = analyse(drawing_a["files"]["clean"], blind=True)
    second = analyse(drawing_a["files"]["clean"], blind=True)
    third = analyse(drawing_a["files"]["clean"], blind=True)
    assert first.canonical_digest() == second.canonical_digest() == third.canonical_digest()
    assert first.to_canonical() == second.to_canonical()
