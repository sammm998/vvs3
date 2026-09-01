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

from .artwork import detect_artwork
from .association import associate_designations
from .canonical import canonical_json, canonical_sort, digest, ql, qs
from .designations import detect_panels, discover_designations, promote_designations, tier_counts
from .dimensions import resolve_diameter
from .geometry.primitives import BBox, Segment
from .glyph import segment_glyphs
from .glyph.prototypes import combined_bank, embedded_prototypes
from .measurement import aggregate_quantities, infer_scale
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
from .roles import classify_roles, non_pipe_objects, role_coverage
from .pipes import dedupe_candidates, detect_pipes, reconstruct_dashes, single_line_candidates
from .states import AnalysisStatus, DesignationTier, IdentityState, Reason, ScaleState
from .text_reconstruction import reconstruct_text
from .topology import build_graph, build_physical_pipes, build_runs
from .topology.graph_build import PipeGraph
from .topology.physical import RunAssignment
from .validation.reconcile import ReconciliationReport, reconcile
from .vector_extraction import ExtractionConfig, extract_document
from .vertical import analyse_verticals

SCHEMA = "vvs-pipe/analysis/1"


def _mean(values) -> float | None:
    """Mean of the values that exist, or nothing when none do.

    Reporting 0.0 for "there was nothing to average" would read as "we are
    certain of nothing", which is a different claim from "this does not apply".
    """
    present = [v for v in values if v is not None]
    return qs(sum(present) / len(present)) if present else None


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

    # ------------------------------------------------------------ hard gate
    @property
    def status(self) -> AnalysisStatus:
        """Whether this run may be read as a quantity take-off.

        Reconciliation is a gate, not a note.  If a metre is counted twice or a
        run belongs to two pipes, an internal invariant has broken and nothing
        downstream can be trusted, so the whole run is INVALID - the numbers are
        still published, because hiding them would hide the defect, but they are
        published as broken.  A run whose invariants hold but which could not be
        measured (no scale, say) is INCOMPLETE, not INVALID: the engine did its
        job and the drawing did not supply what was needed.
        """
        if not self.reconciliation.ok:
            return AnalysisStatus.INVALID
        if any(p.scale.metres_per_point is None for p in self.pages):
            return AnalysisStatus.INCOMPLETE
        if any(q.total_m is None for q in self.quantities):
            return AnalysisStatus.INCOMPLETE
        return AnalysisStatus.VALID

    # --------------------------------------------------------------- metrics
    def metrics(self) -> dict[str, Any]:
        """What the run actually achieved, stated as coverage rather than as a score.

        A single "accuracy" number would be a claim this engine has no way to
        make: it does not know what is on the drawing, only what it found.  What
        it can state honestly is how much of the geometry it placed, how much of
        the lettering resolved, how many of its own predictions are still
        ambiguous, and how confident each part of the chain is - each reported
        separately, because a run can have excellent geometry and no scale, or a
        perfect scale and unreadable labels, and averaging those into one figure
        hides exactly the thing a reader needs to know.
        """
        glyphs = [g for p in self.pages for g in p.glyphs]
        designations = [d for p in self.pages for d in p.designations]
        pipes = [pp for p in self.pages for pp in p.physical_pipes]
        runs = [r for p in self.pages for r in p.runs]

        unresolved_glyphs = sum(1 for g in glyphs if g.character is None)
        ambiguous = sum(1 for d in designations if d.state is IdentityState.AMBIGUOUS)
        ambiguous += sum(1 for pp in pipes if pp.identity_state is IdentityState.AMBIGUOUS)
        tiers = {t.value: 0 for t in DesignationTier}
        for d in designations:
            tiers[d.tier.value] += 1

        named = [pp for pp in pipes if pp.designation]
        measured = [pp for pp in pipes if pp.total_length_m is not None]
        return {
            "detectionCoverage": {
                "vectorObjects": len(self.document.objects),
                "pipeRuns": len(runs),
                "physicalPipes": len(pipes),
                "physicalPipesNamed": len(named),
                "physicalPipesMeasured": len(measured),
                "namedFraction": qs(len(named) / len(pipes)) if pipes else 0.0,
                "measuredFraction": qs(len(measured) / len(pipes)) if pipes else 0.0,
            },
            "identity": {
                "glyphs": len(glyphs),
                "unresolvedGlyphs": unresolved_glyphs,
                "glyphResolvedFraction": (
                    qs((len(glyphs) - unresolved_glyphs) / len(glyphs)) if glyphs else 0.0
                ),
                "ambiguousEntities": ambiguous,
            },
            "designationTiers": tiers,
            "confidence": {
                "geometry": _mean(pp.confidence.geometry for pp in pipes),
                "association": _mean(pp.confidence.association for pp in pipes),
                "designation": _mean(d.confidence.overall for d in designations if d.tier
                                     is DesignationTier.CONFIRMED_DESIGNATION),
                "measurement": _mean(q.confidence.overall for q in self.quantities),
            },
            "scale": [p.scale.state.value for p in self.pages],
            "reconciliationStatus": "OK" if self.reconciliation.ok else "FAILED",
            "analysisStatus": self.status.value,
        }

    # ---------------------------------------------------------------- output
    def to_canonical(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "analysisStatus": self.status.value,
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
            "metrics": self.metrics(),
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
    return analyse_extracted(doc, forensics, str(pdf_path), cfg, blind)


def analyse_extracted(
    doc: VectorDocument,
    forensics: ForensicReport,
    source_path: str,
    cfg: PipelineConfig | None = None,
    blind: bool = False,
) -> AnalysisResult:
    """Analyse an already-extracted document.

    Split out from :func:`analyse` so the order-independence tests can feed the
    same document with its object list permuted and compare canonical digests.
    """
    cfg = cfg or PipelineConfig()
    # The drawing's own typeface is the best prototype for its own lettering;
    # rendered once per document and shared by every page.
    bank = combined_bank(embedded_prototypes(doc.embedded_fonts))
    pages: list[PageResult] = []
    for page_info in doc.pages:
        pages.append(_analyse_page(doc, page_info.page, page_info, cfg, bank))

    all_pipes = [pp for p in pages for pp in p.physical_pipes]
    quantities = aggregate_quantities(all_pipes)
    report = reconcile(
        [c for p in pages for c in p.candidates],
        [r for p in pages for r in p.runs],
        all_pipes,
        quantities,
    )
    return AnalysisResult(
        source_path=source_path,
        forensics=forensics,
        document=doc,
        pages=pages,
        quantities=quantities,
        reconciliation=report,
        blind=blind,
    )


def _analyse_page(doc: VectorDocument, page: int, page_info, cfg: PipelineConfig, bank=None) -> PageResult:
    all_objects = doc.objects_on(page)
    spans = doc.spans_on(page)
    box = BBox(0.0, 0.0, page_info.width, page_info.height)

    # Traced artwork (a vectorised logo or badge) is thousands of tiny filled
    # paths carrying no drawing information.  It is removed first: left in, it
    # sets the text stage's size statistics and dominates the run time.
    artwork_regions, artwork_ids = detect_artwork(all_objects, box)
    objects = [o for o in all_objects if o.object_id not in artwork_ids]

    # Dashed linework is reassembled *before* the text stage.  A dash and a
    # glyph stroke are the same size, so a blobber cannot tell them apart; once
    # the dashes of a bundle of parallel pipes are in play they merge laterally
    # with each other and with any label they pass, and the text stage collapses.
    # Only chains far longer than any character can be are taken, so lettering
    # is left for the text stage.
    dash_chains, dash_consumed, dash_diagnostics = reconstruct_dashes(objects, box)
    for_text = [o for o in objects if o.object_id not in dash_consumed]

    segmentation = segment_glyphs(for_text, box)
    caps = sorted(l.cap_height for l in segmentation.lines)
    cap = caps[len(caps) // 2] if caps else 7.0

    text = reconstruct_text(segmentation, spans, page, bank)
    panels = detect_panels(objects, text.items, box, page)
    panel_boxes = [p.bbox for p in panels]

    # What each piece of geometry *is*, decided from the drawing's own grouping
    # and the shape of what each group contains - never from a layer's name.
    # Only confident, unambiguous verdicts remove anything: building fabric on
    # this sheet is long strokes that never join end to end, the opposite of a
    # pipe network, and that alone is enough to keep a wall cavity out of the
    # take-off.  A weak verdict leaves the geometry in play, because a pipe
    # wrongly dropped is invisible while a wall wrongly kept is arguable.
    roles = classify_roles(objects, box, cap, text.consumed_object_ids)
    not_pipework = non_pipe_objects(roles)

    detection = detect_pipes(
        objects,
        box,
        page,
        text.consumed_object_ids | not_pipework,
        panel_boxes,
        cap,
        dash_chains=dash_chains,
    )
    discovery = discover_designations(
        text.items, objects, panels, box, page, exclude_object_ids=detection.consumed_object_ids
    )
    singles = (
        single_line_candidates(detection.leftover, page, cap, discovery.leader_object_ids)
        if cfg.accept_single_line_pipes
        else ()
    )
    # Two candidates with the same page, centerline and style are the same
    # entity under the engine's own content-addressed identity, however many
    # times the CAD file drew the line.  Collapsing them here is what keeps a
    # metre from being counted twice and a run from landing in two pipes.
    candidates, duplicate_candidates, concentric_candidates = dedupe_candidates(
        list(detection.candidates) + list(singles)
    )

    scale = infer_scale(text.items, objects, box, text.glyphs, cap)
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

    # Only now can text be told from a designation.  Everything above read the
    # sheet; this reads back what the geometry accepted, and nothing that no
    # pipe accepted is published as a designation.
    designations = promote_designations(
        discovery.designations, association.designation_to_runs, pipes, association.assignments
    )

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
        "prototypeBank": {
            "embedded": sum(1 for p in (bank or ()) if p.source == "embedded"),
            "total": len(bank or ()),
        },
        "artworkRegions": [r.to_canonical() for r in artwork_regions],
        "artworkObjectsExcluded": len(artwork_ids),
        "dashReconstruction": dash_diagnostics,
        "designationTiers": tier_counts(designations),
        "drawingRoles": roles.to_canonical()["counts"],
        "roleCoverage": role_coverage(roles),
        "excludedAsNotPipework": len(not_pipework),
        "duplicateCandidatesMerged": duplicate_candidates,
        "concentricCandidatesMerged": concentric_candidates,
    }

    return PageResult(
        page=page,
        box=box,
        text_cap_height=cap,
        glyphs=text.glyphs,
        text_items=text.items,
        designations=designations,
        candidates=candidates,
        graph=graph,
        runs=runs,
        physical_pipes=pipes,
        verticals=verticals.verticals,
        scale=scale,
        panels=panels,
        diagnostics=diagnostics,
    )
