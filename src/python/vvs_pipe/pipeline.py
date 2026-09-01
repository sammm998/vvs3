"""The blind analysis pipeline.

This module - and everything it imports - constitutes the *blind* engine.  It
receives a PDF path and nothing else: no expected designations, no ground
truth, no fixture labels, no spreadsheet.  ``tests/python/test_blind_leakage.py``
walks this module's transitive import closure and fails if it can reach the
evaluator, the fixtures package, or a spreadsheet library.

Stage order follows the specification:

    forensics -> vector extraction -> glyph segmentation -> glyph
    reconstruction -> designation discovery -> pipe geometry -> dimension
    reconciliation -> leader/association analysis -> topology -> pipe runs ->
    vertical analysis -> measurement -> quantity aggregation -> annotated
    drawing -> report
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .association import associate_designations
from .canonical import canonical_json, canonical_sort, digest, ql, qs
from .designations import detect_panels, discover_designations
from .dimensions import resolve_diameter
from .geometry.primitives import BBox, Segment
from .glyph import segment_glyphs
from .measurement import aggregate_quantities, detect_scale
from .model import (
    Designation,
    GlyphCandidate,
    PhysicalPipe,
    PipeCandidate,
    PipeRun,
    QuantityRow,
    ScaleResult,
    TextItem,
    VectorDocument,
    VerticalSegment,
)
from .pdf_forensics import ForensicReport, forensic_report
from .pipes import detect_pipes, single_line_candidates
from .states import IdentityState, Reason, ScaleState
from .text_reconstruction import reconstruct_text
from .topology import build_graph, build_physical_pipes, build_runs
from .topology.graph_build import PipeGraph
from .topology.physical import RunAssignment
from .validation.reconcile import ReconciliationReport, reconcile
from .vector_extraction import ExtractionConfig, extract_document
from .vertical import analyse_verticals

SCHEMA = "vvs-pipe/analysis/1"


@dataclass(slots=True)
class PageResult:
    page: int
    box: BBox
    text_cap_height: float
    glyphs: tuple[GlyphCandidate, ...]
    text_items: tuple[TextItem, ...]
    designations: tuple[Designation, ...]
    candidates: tuple[PipeCandidate, ...]
    graph: PipeGraph
    runs: tuple[PipeRun, ...]
    physical_pipes: tuple[PhysicalPipe, ...]
    verticals: tuple[VerticalSegment, ...]
    scale: ScaleResult
    panels: tuple[Any, ...]
    diagnostics: dict[str, Any]


@dataclass(slots=True)
class AnalysisResult:
    source_path: str
    forensics: ForensicReport
    document: VectorDocument
    pages: list[PageResult]
    quantities: tuple[QuantityRow, ...]
    reconciliation: ReconciliationReport
    blind: bool

    # ---------------------------------------------------------------- output
    def to_canonical(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "drawing": {
                "file": self.document.source_name,
                "pdfSha256": self.document.sha256,
                "pages": [p.to_canonical() for p in self.document.pages],
                "vectorObjectCount": len(self.document.objects),
                "textSpanCount": len(self.document.text_spans),
                "excludedAnnotationObjects": self.document.excluded_annotation_objects,
            },
            "forensicsDigest": self.forensics.report_digest,
            "glyphs": [g.to_canonical() for p in self.pages for g in p.glyphs],
            "textItems": [t.to_canonical() for p in self.pages for t in p.text_items],
            "designations": [d.to_canonical() for p in self.pages for d in p.designations],
            "pipeCandidates": [c.to_canonical() for p in self.pages for c in p.candidates],
            "graph": [p.graph.to_canonical() for p in self.pages],
            "pipeRuns": [r.to_canonical() for p in self.pages for r in p.runs],
            "physicalPipes": [pp.to_canonical() for p in self.pages for pp in p.physical_pipes],
            "verticals": [v.to_canonical() for p in self.pages for v in p.verticals],
            "scale": [p.scale.to_canonical() for p in self.pages],
            "quantities": [q.to_canonical() for q in self.quantities],
            "diagnostics": {
                "pages": [p.diagnostics for p in self.pages],
                "reconciliation": self.reconciliation.to_canonical(),
            },
            "determinism": {
                "canonicalDigest": self.canonical_digest(),
                "quantitiesDigest": digest([q.to_canonical() for q in self.quantities]),
                "physicalPipesDigest": digest(
                    [pp.to_canonical() for p in self.pages for pp in p.physical_pipes]
                ),
            },
            "blind": {"facitUsedDuringDetection": False, "mode": "blind" if self.blind else "normal"},
        }

    def canonical_digest(self) -> str:
        payload = {
            "glyphs": [g.to_canonical() for p in self.pages for g in p.glyphs],
            "designations": [d.to_canonical() for p in self.pages for d in p.designations],
            "candidates": [c.to_canonical() for p in self.pages for c in p.candidates],
            "runs": [r.to_canonical() for p in self.pages for r in p.runs],
            "physical": [pp.to_canonical() for p in self.pages for pp in p.physical_pipes],
            "verticals": [v.to_canonical() for p in self.pages for v in p.verticals],
            "quantities": [q.to_canonical() for q in self.quantities],
        }
        return digest(payload)

    def write_json(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(canonical_json(self.to_canonical(), indent=2), encoding="utf-8")
        return p


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    extraction: ExtractionConfig = ExtractionConfig()
    accept_single_line_pipes: bool = True


def analyse(
    pdf_path: str | Path,
    cfg: PipelineConfig | None = None,
    blind: bool | None = None,
) -> AnalysisResult:
    cfg = cfg or PipelineConfig()
    blind = os.environ.get("RUN_BLIND_TEST") == "1" if blind is None else blind
    pdf_path = Path(pdf_path)

    forensics = forensic_report(pdf_path, cfg.extraction)
    doc = extract_document(pdf_path, cfg.extraction)

    pages: list[PageResult] = []
    for page_info in doc.pages:
        pages.append(_analyse_page(doc, page_info.page, page_info, cfg))

    all_pipes = [pp for p in pages for pp in p.physical_pipes]
    quantities = aggregate_quantities(all_pipes)
    report = reconcile(
        [c for p in pages for c in p.candidates],
        [r for p in pages for r in p.runs],
        all_pipes,
        quantities,
    )
    return AnalysisResult(
        source_path=str(pdf_path),
        forensics=forensics,
        document=doc,
        pages=pages,
        quantities=quantities,
        reconciliation=report,
        blind=blind,
    )


def _analyse_page(doc: VectorDocument, page: int, page_info, cfg: PipelineConfig) -> PageResult:
    objects = doc.objects_on(page)
    spans = doc.spans_on(page)
    box = BBox(0.0, 0.0, page_info.width, page_info.height)

    segmentation = segment_glyphs(objects, box)
    caps = sorted(l.cap_height for l in segmentation.lines)
    cap = caps[len(caps) // 2] if caps else 7.0

    text = reconstruct_text(segmentation, spans, page)
    panels = detect_panels(objects, text.items, box, page)
    panel_boxes = [p.bbox for p in panels]

    detection = detect_pipes(
        objects, box, page, text.consumed_object_ids, panel_boxes, cap
    )
    discovery = discover_designations(
        text.items, objects, panels, box, page, exclude_object_ids=detection.consumed_object_ids
    )
    singles = (
        single_line_candidates(detection.leftover, page, cap, discovery.leader_object_ids)
        if cfg.accept_single_line_pipes
        else ()
    )
    candidates = tuple(
        canonical_sort(list(detection.candidates) + list(singles), key=lambda c: c.canonical_key())
    )

    scale = detect_scale(text.items, objects, box)
    mpp = scale.metres_per_point

    graph = build_graph(candidates, page)
    runs = build_runs(graph, page)

    measured_diameter: dict[str, float | None] = {}
    for r in runs:
        measured_diameter[r.pipe_run_id] = (
            None if (r.width_pt is None or mpp is None) else round(r.width_pt * mpp * 1000.0, 1)
        )

    leaders_by_designation: dict[str, tuple[Segment, ...]] = {}
    text_to_designation = {d.text_item_id: d.designation_id for d in discovery.designations}
    for text_id, seg in discovery.leaders:
        did = text_to_designation.get(text_id)
        if did is None:
            continue
        leaders_by_designation.setdefault(did, ())
        leaders_by_designation[did] = leaders_by_designation[did] + (seg,)

    association = associate_designations(
        discovery.designations, runs, leaders_by_designation, measured_diameter, cap
    )

    verticals = analyse_verticals(
        detection.symbol_boxes, discovery.elevation_notes, runs, cap, page
    )

    label_of_run: dict[str, float | None] = {}
    for rid, a in association.assignments.items():
        label = None
        if a.designation:
            for d in discovery.designations:
                if d.text == a.designation and d.diameter_mm is not None:
                    label = d.diameter_mm
                    break
        label_of_run[rid] = label

    resolved: dict[str, RunAssignment] = {}
    dimension_notes: list[dict[str, Any]] = []
    for r in runs:
        a = association.assignments.get(r.pipe_run_id)
        dim = resolve_diameter(label_of_run.get(r.pipe_run_id), r.width_pt, mpp)
        dimension_notes.append(
            {
                "pipeRunId": r.pipe_run_id,
                "labelMm": dim.label_mm,
                "measuredMm": dim.measured_mm,
                "resolvedMm": dim.diameter_mm,
                "source": dim.source,
                "reasons": [x.value for x in dim.reasons],
            }
        )
        if a is None:
            resolved[r.pipe_run_id] = RunAssignment(
                designation=None,
                designation_ids=(),
                diameter_mm=dim.diameter_mm,
                state=IdentityState.INSUFFICIENT,
                reasons=(Reason.NO_DESIGNATION,) + dim.reasons,
                association_confidence=0.0,
                evidence=(("dimensionConfidence", qs(dim.confidence)),),
            )
        else:
            resolved[r.pipe_run_id] = RunAssignment(
                designation=a.designation,
                designation_ids=a.designation_ids,
                diameter_mm=dim.diameter_mm,
                state=a.state,
                reasons=tuple(sorted(set(a.reasons + dim.reasons), key=lambda x: x.value)),
                association_confidence=a.association_confidence,
                evidence=a.evidence + (("dimensionConfidence", qs(dim.confidence)),),
            )

    pipes = build_physical_pipes(runs, resolved, page, mpp, verticals.by_run)

    diagnostics = {
        "page": page,
        "textCapHeightPt": qs(cap),
        "glyphLines": len(segmentation.lines),
        "panels": [p.to_canonical() for p in panels],
        "excludedObjects": len(detection.excluded_object_ids),
        "consumedByText": len(text.consumed_object_ids),
        "consumedByPipes": len(detection.consumed_object_ids),
        "leaderObjects": len(discovery.leader_object_ids),
        "symbolBoxes": [b.to_canonical() for b in detection.symbol_boxes],
        "associationDiagnostics": [list(x) for x in association.diagnostics],
        "dimensionReconciliation": dimension_notes,
        "scaleState": scale.state.value,
        "unresolvedGlyphs": sum(1 for g in text.glyphs if g.character is None),
    }

    return PageResult(
        page=page,
        box=box,
        text_cap_height=cap,
        glyphs=text.glyphs,
        text_items=text.items,
        designations=discovery.designations,
        candidates=candidates,
        graph=graph,
        runs=runs,
        physical_pipes=pipes,
        verticals=verticals.verticals,
        scale=scale,
        panels=panels,
        diagnostics=diagnostics,
    )
