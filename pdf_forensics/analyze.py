"""The orchestrator: one clean PDF in, an auditable analysis out.

    python -m pdf_forensics.analyze drawing.pdf --out artifacts/run

Nobody has to call the searches by hand.  ``analyse`` runs them in order, feeds
each stage the last one's output, and widens a search when a stage comes back
empty - a designation with no pipe makes the local geometry search try harder,
a pipe with no designation makes the text search reach further.  Every stage
writes its intermediate result, and every derived entity is linked into the
evidence graph as it is created, so the finished report can answer *why*.

The blindness rule is enforced at the entrance: exactly one clean drawing goes
in, and no facit, marked-up copy or spreadsheet can reach any stage.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from . import designation_search, dimensions, leader_search, measurement, pipes as pipes_mod
from . import scale as scale_mod
from . import topology as topology_mod
from . import validation
from .canonical import canonical_json, q, sort_canonical
from .evidence import EvidenceGraph
from .fragment_search import Fragment
from .geometry_search import GeometryModel, point_segment_distance, seg_bbox
from .glyphs import (CharacterBank, GlyphModel, build_bank, embedded_font_buffers,
                     extract_path_glyphs, extract_text_glyphs, group_ink_lines, ink_components)
from .loader import LoadedPdf, load
from .model import (Association, DesignationCandidate, Glyph, Leader, PdfObject, PhysicalPipe,
                    PipeCandidate, PipeRun, Reason, Segment, State, TextItem)
from .neighbourhood import inspect_neighbourhood
from .objects import ObjectStore, extract
from .paths import PathModel
from .spatial_index import SpatialIndex
from .text_reconstruction import merge_duplicate_readings, reconstruct, token_structure


def _reorder(items: Sequence[Any], order: str) -> list[Any]:
    """Present a collection to the next stage in a different order.

    Used by the determinism harness.  Every stage sorts its input canonically,
    so this must make no difference to any output - which is exactly what the
    harness asserts.
    """
    values = list(items)
    if order == "normal":
        return values
    if order == "reversed":
        return list(reversed(values))
    if order.startswith("permuted"):
        seed = int(order.split(":", 1)[1]) if ":" in order else 0
        rng = random.Random(seed)
        rng.shuffle(values)
        return values
    raise ValueError(f"unknown order {order!r}")


@dataclass
class Workspace:
    """Everything the analysis has found so far, addressable by id."""

    pdf: LoadedPdf
    store: Optional[ObjectStore] = None
    path_model: Optional[PathModel] = None
    geometry: Optional[GeometryModel] = None
    glyph_model: Optional[GlyphModel] = None
    text_items: list[TextItem] = field(default_factory=list)
    text_notes: list[dict] = field(default_factory=list)
    panels: list[pipes_mod.Panel] = field(default_factory=list)
    roles: dict[str, pipes_mod.RoleRecord] = field(default_factory=dict)
    candidates: list[DesignationCandidate] = field(default_factory=list)
    leaders: list[Leader] = field(default_factory=list)
    leaders_by_text: dict[str, Leader] = field(default_factory=dict)
    fragments: list[Fragment] = field(default_factory=list)
    pipe_candidates: list[PipeCandidate] = field(default_factory=list)
    graph: Optional[topology_mod.Graph] = None
    runs: list[PipeRun] = field(default_factory=list)
    physical_pipes: list[PhysicalPipe] = field(default_factory=list)
    associations: list[Association] = field(default_factory=list)
    resolved: dict[str, Association] = field(default_factory=dict)
    dimension_tokens: list[dimensions.DimensionToken] = field(default_factory=list)
    scale: Optional[scale_mod.ScaleResult] = None
    elevations: list[measurement.Elevation] = field(default_factory=list)
    risers: list[measurement.Riser] = field(default_factory=list)
    measurements: list[measurement.PipeMeasurement] = field(default_factory=list)
    quantities: list[dict] = field(default_factory=list)
    evidence: EvidenceGraph = field(default_factory=EvidenceGraph)
    adaptive: list[dict] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)

    # -- lookups ----------------------------------------------------------
    def collections(self) -> dict[str, Sequence[Any]]:
        return {
            "objects": self.store.objects if self.store else [],
            "glyphs": self.glyph_model.glyphs if self.glyph_model else [],
            "segments": self.geometry.segments if self.geometry else [],
            "textItems": self.text_items,
            "designationCandidates": self.candidates,
            "leaders": self.leaders,
            "pipeCandidates": self.pipe_candidates,
            "pipeRuns": self.runs,
            "physicalPipes": self.physical_pipes,
            "nodes": list(self.graph.nodes.values()) if self.graph else [],
            "edges": list(self.graph.edges.values()) if self.graph else [],
            "dimensionTokens": self.dimension_tokens,
            "panels": self.panels,
            "risers": self.risers,
        }

    def find(self, entity_id: str) -> Optional[Any]:
        for items in self.collections().values():
            for item in items:
                for attribute in ("object_id", "glyph_id", "segment_id", "text_id",
                                  "candidate_id", "leader_id", "pipe_id", "run_id",
                                  "node_id", "edge_id", "token_id", "panel_id", "riser_id"):
                    if getattr(item, attribute, None) == entity_id:
                        return item
        return None

    def neighbourhood(self, **kwargs) -> Any:
        return inspect_neighbourhood(self, **kwargs)


def analyse(path: str | Path, order: str = "normal", *, with_bank: bool = True,
            progress: Optional[Any] = None) -> tuple[Workspace, dict]:
    """Run every stage over one clean PDF and return the workspace and report."""
    started = time.time()
    pdf = load(path)
    workspace = Workspace(pdf=pdf)
    graph = workspace.evidence

    def stage(name: str, function):
        begin = time.time()
        result = function()
        workspace.timings[name] = q(time.time() - begin)
        if progress:
            progress(name, workspace.timings[name])
        return result

    page_sizes = {p.number: (p.width, p.height) for p in pdf.pages}

    # 1 - every object in the file ----------------------------------------
    store = stage("objects", lambda: extract(pdf))
    workspace.store = store
    for obj in store.objects:
        graph.declare(obj.object_id, obj.kind)

    # 2 - text objects, as a search index only ------------------------------
    native_spans = [o for o in _reorder(store.objects, order) if o.kind == "text_span"]

    # 3/4 - paths and their segments ---------------------------------------
    path_model = stage("paths", lambda: PathModel(_reorder(store.drawing_objects(), order)))
    workspace.path_model = path_model
    geometry = stage("geometry", lambda: GeometryModel(_reorder(path_model.segments, order)))
    workspace.geometry = geometry
    for segment in geometry.segments:
        graph.declare(segment.segment_id, "segment")
        for path_id in path_model.paths_by_segment.get(segment.segment_id, []):
            graph.link(path_id, segment.segment_id, "PATH_YIELDS_SEGMENT")

    # 5 - glyphs, from the text layer and from the lettering geometry --------
    def build_glyphs() -> GlyphModel:
        text_glyphs = extract_text_glyphs(_reorder(store.objects, order))
        components = ink_components(_reorder(path_model.segments, order))
        page_height = max((p.height for p in pdf.pages), default=1.0)
        lines = group_ink_lines(_reorder(components, order), page_height)
        bank = build_bank(embedded_font_buffers(pdf)) if with_bank else build_bank(())
        path_glyphs = extract_path_glyphs(lines, components, bank)
        workspace.bank_summary = bank.to_json()
        return GlyphModel(text_glyphs + path_glyphs)

    glyph_model = stage("glyphs", build_glyphs)
    workspace.glyph_model = glyph_model
    for glyph in glyph_model.glyphs:
        graph.declare(glyph.glyph_id, "glyph", glyph.character)
        for source in glyph.source_object_ids:
            graph.link(source, glyph.glyph_id, "OBJECT_YIELDS_GLYPH",
                       {"source": glyph.source, "confidence": glyph.confidence})

    # 6 - glyphs become text ------------------------------------------------
    def build_text() -> tuple[list[TextItem], list[dict]]:
        items = reconstruct(_reorder(glyph_model.glyphs, order))
        return merge_duplicate_readings(items)

    workspace.text_items, workspace.text_notes = stage("text", build_text)
    for item in workspace.text_items:
        graph.declare(item.text_id, "text", item.text)
        for glyph_id in item.glyph_ids:
            graph.link(glyph_id, item.text_id, "GLYPH_IN_TEXT")

    # 7 - panels and drawing roles -----------------------------------------
    lettering_paths = frozenset(pid for g in glyph_model.glyphs for pid in g.source_object_ids)
    workspace.panels = stage("panels", lambda: pipes_mod.detect_panels(
        _reorder(store.objects, order), workspace.text_items, page_sizes,
        _reorder(geometry.segments, order), lettering_paths))
    workspace.roles = stage("roles", lambda: pipes_mod.classify_roles(
        _reorder(geometry.segments, order), glyph_model.glyphs, workspace.text_items,
        workspace.panels, store.by_id, page_sizes))
    eligible = [s for s in geometry.segments
                if workspace.roles[s.segment_id].role == pipes_mod.LINEWORK]
    lettering_ids = {s.segment_id for s in geometry.segments
                     if workspace.roles[s.segment_id].role == pipes_mod.LETTERING}

    # 8 - leaders ----------------------------------------------------------
    leader_pool = {s.segment_id for s in geometry.segments
                   if workspace.roles[s.segment_id].role in (pipes_mod.LINEWORK,)}
    workspace.leaders = stage("leaders", lambda: leader_search.find_leaders(
        _reorder(workspace.text_items, order), geometry, leader_pool))
    workspace.leaders_by_text = leader_search.leaders_by_text(workspace.leaders,
                                                              workspace.text_items)
    for text_id, leader in workspace.leaders_by_text.items():
        graph.declare(leader.leader_id, "leader")
        graph.link(text_id, leader.leader_id, "TEXT_HAS_LEADER",
                   {"targetEnd": list(leader.target_end), "length": leader.length})
        for segment_id in leader.segment_ids:
            graph.link(segment_id, leader.leader_id, "SEGMENT_IN_LEADER")

    # 9 - designation candidates -------------------------------------------
    panel_text_ids = {t for panel in workspace.panels for t in panel.text_ids}
    workspace.candidates = stage("designations", lambda: designation_search.find_candidates(
        _reorder(workspace.text_items, order), panel_text_ids, workspace.leaders_by_text))
    for candidate in workspace.candidates:
        graph.declare(candidate.candidate_id, "designationCandidate", candidate.text)
        graph.link(candidate.text_id, candidate.candidate_id, "TEXT_PROPOSES_DESIGNATION",
                   {"score": candidate.score, "signals": candidate.signals})

    # 10/11 - pipe geometry, fragments, candidates ---------------------------
    closed_paths = frozenset(k for k, facts in path_model.facts.items() if facts.closed)

    def build_pipe_candidates(support: Optional[dict[str, dict]] = None):
        pairs, ambiguous = pipes_mod.double_line_pairs(geometry, _reorder(eligible, order),
                                                       page_sizes, closed_paths)
        fragments = pipes_mod.wall_fragments(pairs, geometry, path_model.paths_by_segment)
        used = {sid for fragment in fragments for sid in fragment.segment_ids}
        fragments += pipes_mod.dashed_fragments(_reorder(eligible, order), used,
                                                path_model.paths_by_segment)
        used |= {sid for fragment in fragments for sid in fragment.segment_ids}
        if support:
            fragments += pipes_mod.supported_single_fragments(
                _reorder(eligible, order), used, path_model.paths_by_segment, support)
        candidates = pipes_mod.build_candidates(_reorder(fragments, order))
        candidates, tee_notes = pipes_mod.split_at_tees(candidates)
        candidates, dropped = pipes_mod.deduplicate(candidates)
        return fragments, candidates, ambiguous, dropped + list(tee_notes)

    fragments, pipe_candidates, ambiguous_pairs, dropped = stage(
        "pipe_candidates", build_pipe_candidates)

    # adaptive: a leader that ends on a stroke nothing else explains promotes
    # that stroke to a candidate.  A designation with no pipe is a reason to
    # look harder, never a reason to attach it to the nearest line.
    support = _leader_support(workspace, geometry, pipe_candidates, eligible)
    if support:
        fragments, pipe_candidates, ambiguous_pairs, dropped = stage(
            "pipe_candidates_adaptive", lambda: build_pipe_candidates(support))
        workspace.adaptive.append({
            "stage": "pipe_candidates",
            "trigger": "LEADER_ENDS_ON_UNEXPLAINED_STROKE",
            "promotedSegments": len(support),
        })
    workspace.fragments = fragments
    workspace.pipe_candidates = pipe_candidates
    for candidate in pipe_candidates:
        graph.declare(candidate.candidate_id, "pipeCandidate")
        for segment_id in candidate.segment_ids:
            graph.link(segment_id, candidate.candidate_id, "SEGMENT_IN_PIPE_CANDIDATE",
                       {"kind": candidate.kind})

    # 12 - topology and physical pipes --------------------------------------
    workspace.graph = stage("topology", lambda: topology_mod.Graph(_reorder(pipe_candidates, order)))
    workspace.runs = stage("runs", lambda: topology_mod.build_runs(workspace.graph,
                                                                   pipe_candidates))
    workspace.physical_pipes = stage("physical_pipes", lambda: topology_mod.build_physical_pipes(
        workspace.graph, _reorder(workspace.runs, order)))
    for run in workspace.runs:
        graph.declare(run.run_id, "pipeRun")
        for member in run.member_ids:
            graph.link(member, run.run_id, "CANDIDATE_IN_RUN")
    for pipe in workspace.physical_pipes:
        graph.declare(pipe.pipe_id, "physicalPipe")
        for run_id in pipe.run_ids:
            graph.link(run_id, pipe.pipe_id, "RUN_IN_PHYSICAL_PIPE")
    reconciliation = topology_mod.reconcile(pipe_candidates, workspace.runs,
                                            workspace.physical_pipes)

    # 13 - scale -------------------------------------------------------------
    first_page = (pdf.pages[0].width, pdf.pages[0].height) if pdf.pages else (1.0, 1.0)
    workspace.scale = stage("scale", lambda: scale_mod.resolve(
        _reorder(workspace.text_items, order), _reorder(geometry.segments, order), first_page))

    # 14 - dimensions --------------------------------------------------------
    workspace.dimension_tokens = stage("dimensions", lambda: dimensions.find_dimension_tokens(
        _reorder(workspace.text_items, order)))
    token_index = dimensions.token_spatial_index(workspace.dimension_tokens)

    # 15 - association, both directions --------------------------------------
    workspace.associations = stage("association", lambda: designation_search.associate(
        _reorder(workspace.candidates, order), _reorder(workspace.physical_pipes, order),
        workspace.leaders_by_text, workspace.text_items))
    workspace.resolved, rejected = designation_search.resolve(workspace.associations)
    for association in workspace.associations:
        graph.declare(association.association_id, "association")
        graph.link(association.candidate_id, association.association_id, "CANDIDATE_IN_ASSOCIATION",
                   {"forward": association.forward, "backward": association.backward})
        graph.link(association.pipe_id, association.association_id, "PIPE_IN_ASSOCIATION")
    for association in rejected:
        graph.reject(association.pipe_id, association.candidate_id,
                     ";".join(association.reasons) or Reason.COMPETING_DESIGNATIONS_EQUALLY_SUPPORTED,
                     {"score": association.score})

    # 16 - attach designations and diameters to pipes ------------------------
    metres_per_point = workspace.scale.metres_per_point if workspace.scale else None
    named: list[PhysicalPipe] = []
    by_candidate = {c.candidate_id: c for c in workspace.candidates}
    for pipe in workspace.physical_pipes:
        association = workspace.resolved.get(pipe.pipe_id)
        designation = None
        designation_state = State.UNRESOLVED
        reasons: tuple[str, ...] = (Reason.NO_DESIGNATION,)
        if association is not None:
            candidate = by_candidate[association.candidate_id]
            designation = candidate.text
            designation_state = association.state
            reasons = association.reasons or ()
            graph.link(association.association_id, pipe.pipe_id, "ASSOCIATION_NAMES_PIPE",
                       {"designation": designation, "state": designation_state})
        result = dimensions.resolve_diameter(pipe, workspace.dimension_tokens, token_index,
                                             metres_per_point, designation)
        named.append(
            PhysicalPipe(
                pipe_id=pipe.pipe_id, page=pipe.page, run_ids=pipe.run_ids,
                centerline=pipe.centerline, parts=pipe.parts, designation=designation,
                designation_state=designation_state, designation_reasons=tuple(reasons),
                diameter_mm=result.diameter_mm, diameter_state=result.state,
                diameter_reasons=result.reasons,
                horizontal_points=pipe.horizontal_points, vertical_metres=None,
                vertical_state=State.UNRESOLVED,
                measurement={**pipe.measurement, "diameterEvidence": result.evidence},
                confidence=dict(pipe.confidence),
            )
        )
    workspace.physical_pipes = sort_canonical(named, key=lambda p: (p.page, p.centerline, p.pipe_id))

    # adaptive: a pipe nothing named makes the text search reach further
    unnamed = [p for p in workspace.physical_pipes if not p.designation]
    if unnamed and workspace.candidates:
        widened = _widened_backward(workspace, unnamed)
        if widened:
            workspace.adaptive.append({
                "stage": "association",
                "trigger": "PIPE_WITHOUT_DESIGNATION",
                "pipesReconsidered": len(unnamed),
                "newAssociations": len(widened),
            })

    # 17 - measurement --------------------------------------------------------
    workspace.elevations = stage("elevations", lambda: measurement.find_elevations(
        _reorder(workspace.text_items, order)))
    workspace.risers = stage("risers", lambda: measurement.find_risers(
        _reorder(workspace.physical_pipes, order), workspace.elevations))
    scale_state = workspace.scale.state if workspace.scale else State.UNRESOLVED
    workspace.measurements = stage("measurement", lambda: measurement.measure(
        _reorder(workspace.physical_pipes, order), workspace.risers,
        metres_per_point if scale_state in (State.CONFIRMED, State.AMBIGUOUS) else None,
        scale_state))
    measured_by_pipe = {m.pipe_id: m for m in workspace.measurements}
    workspace.physical_pipes = [
        PhysicalPipe(
            pipe_id=p.pipe_id, page=p.page, run_ids=p.run_ids, centerline=p.centerline,
            parts=p.parts, designation=p.designation, designation_state=p.designation_state,
            designation_reasons=p.designation_reasons, diameter_mm=p.diameter_mm,
            diameter_state=p.diameter_state, diameter_reasons=p.diameter_reasons,
            horizontal_points=p.horizontal_points,
            vertical_metres=measured_by_pipe[p.pipe_id].vertical_metres,
            vertical_state=(State.CONFIRMED
                            if measured_by_pipe[p.pipe_id].vertical_metres is not None
                            else State.UNRESOLVED),
            measurement={**p.measurement, **measured_by_pipe[p.pipe_id].to_json()},
            confidence=_confidence(p, measured_by_pipe[p.pipe_id], workspace),
        )
        for p in workspace.physical_pipes
    ]
    workspace.quantities = measurement.aggregate(workspace.physical_pipes, workspace.measurements)

    # 18 - validation ---------------------------------------------------------
    checks = validation.run_checks(store.conservation(), reconciliation,
                                   workspace.physical_pipes, workspace.associations,
                                   workspace.measurements, scale_state, metres_per_point)
    coverage = validation.coverage(workspace.physical_pipes, workspace.candidates,
                                   workspace.associations, workspace.measurements)
    report = _build_report(workspace, store, path_model, geometry, glyph_model,
                           reconciliation, checks, coverage, ambiguous_pairs, dropped,
                           time.time() - started)
    return workspace, report


def _confidence(pipe: PhysicalPipe, measured: measurement.PipeMeasurement,
                workspace: Workspace) -> dict[str, float]:
    """Confidence per aspect; the overall figure is the weakest of them."""
    association = workspace.resolved.get(pipe.pipe_id)
    parts = {
        "geometry": q(pipe.confidence.get("geometry", 0.5)),
        "text": q(next((c.score for c in workspace.candidates
                        if association and c.candidate_id == association.candidate_id), 0.0)),
        "association": q(association.score if association else 0.0),
        "dimension": q(1.0 if pipe.diameter_state == State.CONFIRMED
                       else (0.5 if pipe.diameter_mm is not None else 0.0)),
        "measurement": q(1.0 if measured.horizontal_metres is not None else 0.0),
    }
    parts["overall"] = q(min(parts.values()))
    return parts


def _leader_support(workspace: Workspace, geometry: GeometryModel,
                    candidates: Sequence[PipeCandidate],
                    eligible: Sequence[Segment]) -> dict[str, dict]:
    """Strokes that a leader ends on and no pipe candidate explains."""
    explained = {sid for candidate in candidates for sid in candidate.segment_ids}
    eligible_ids = {s.segment_id for s in eligible}
    support: dict[str, dict] = {}
    for text_id, leader in sorted(workspace.leaders_by_text.items()):
        for segment in geometry.near_point(leader.page, leader.target_end, 2.5):
            if segment.segment_id in explained or segment.segment_id not in eligible_ids:
                continue
            if segment.segment_id in leader.segment_ids:
                continue
            support[segment.segment_id] = {
                "rule": "LEADER_ENDS_ON_THIS_STROKE",
                "leaderId": leader.leader_id,
                "textId": text_id,
                "distance": q(point_segment_distance(leader.target_end, segment.a, segment.b)),
            }
    return support


def _widened_backward(workspace: Workspace, unnamed: Sequence[PhysicalPipe]) -> list[Association]:
    """Second look at pipes nothing named, with a wider text search.

    Anything found this way is still subject to the same rules: it must beat
    its competitors, and a single direction of support keeps it AMBIGUOUS.
    """
    extra = designation_search.associate(workspace.candidates, list(unnamed),
                                         workspace.leaders_by_text, workspace.text_items)
    fresh = [a for a in extra
             if not any(existing.candidate_id == a.candidate_id
                        and existing.pipe_id == a.pipe_id
                        for existing in workspace.associations)]
    if not fresh:
        return []
    workspace.associations = sort_canonical(list(workspace.associations) + fresh,
                                            key=lambda a: (a.candidate_id, a.pipe_id))
    workspace.resolved, _ = designation_search.resolve(workspace.associations)
    return fresh


def _build_report(workspace: Workspace, store: ObjectStore, path_model: PathModel,
                  geometry: GeometryModel, glyph_model: GlyphModel, reconciliation: dict,
                  checks: Sequence[validation.Check], coverage: dict,
                  ambiguous_pairs: Sequence[dict], dropped: Sequence[dict],
                  elapsed: float) -> dict:
    from . import text_reconstruction as text_mod

    scale = workspace.scale
    report = {
        "schema": "pdf-forensics/analysis/1",
        "file": workspace.pdf.to_json(),
        "status": validation.status(checks),
        "stages": {
            "objects": store.to_json(),
            "paths": path_model.to_json(),
            "geometry": geometry.to_json(),
            "glyphs": {**glyph_model.to_json(), "bank": getattr(workspace, "bank_summary", {})},
            "text": text_mod.to_json(workspace.text_items, workspace.text_notes),
            "designations": designation_search.to_json(workspace.candidates,
                                                       workspace.associations,
                                                       workspace.resolved),
            "leaders": leader_search.to_json(workspace.leaders),
            "pipes": pipes_mod.to_json(workspace.pipe_candidates, workspace.roles,
                                       workspace.panels, ambiguous_pairs),
            "topology": topology_mod.to_json(workspace.graph, workspace.runs),
            "physicalPipes": {
                "count": len(workspace.physical_pipes),
                "named": len([p for p in workspace.physical_pipes if p.designation]),
                "withDiameter": len([p for p in workspace.physical_pipes
                                     if p.diameter_mm is not None]),
            },
            "dimensions": dimensions.to_json(workspace.dimension_tokens),
            "scale": scale.to_json() if scale else {},
            "measurement": measurement.to_json(workspace.measurements, workspace.risers,
                                               workspace.quantities),
            "evidence": workspace.evidence.to_json(),
        },
        "reconciliation": reconciliation,
        "validation": validation.report(checks, coverage),
        "adaptive": workspace.adaptive,
        "duplicatesDropped": list(dropped),
        "quantities": workspace.quantities,
        "physicalPipes": [p.to_json() for p in workspace.physical_pipes],
        "designationCandidates": [c.to_json() for c in workspace.candidates],
        "timings": workspace.timings,
        "elapsedSeconds": q(elapsed),
    }
    return report


def write_outputs(workspace: Workspace, report: dict, out_dir: str | Path,
                  crops: int = 0, mark: bool = True) -> dict:
    """Write the report, the intermediates and (optionally) the marked drawing."""
    out = Path(out_dir)
    (out / "forensics").mkdir(parents=True, exist_ok=True)
    (out / "analysis.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    intermediates = {
        "objects.json": [o.to_json() for o in workspace.store.objects],
        "glyphs.json": [g.to_json() for g in workspace.glyph_model.glyphs],
        "text.json": [t.to_json() for t in workspace.text_items],
        "segments.json": [s.to_json() for s in workspace.geometry.segments],
        "leaders.json": [l.to_json() for l in workspace.leaders],
        "designation_candidates.json": [c.to_json() for c in workspace.candidates],
        "pipe_candidates.json": [c.to_json() for c in workspace.pipe_candidates],
        "pipe_runs.json": [r.to_json() for r in workspace.runs],
        "physical_pipes.json": [p.to_json() for p in workspace.physical_pipes],
        "associations.json": [a.to_json() for a in workspace.associations],
        "measurements.json": [m.to_json() for m in workspace.measurements],
        "risers.json": [r.to_json() for r in workspace.risers],
        "evidence.json": workspace.evidence.to_json(include_edges=True),
        "roles.json": [r.to_json() for r in
                       sorted(workspace.roles.values(), key=lambda r: r.segment_id)],
        "panels.json": [p.to_json() for p in workspace.panels],
    }
    for name, payload in intermediates.items():
        (out / "forensics" / name).write_text(json.dumps(payload, indent=2, sort_keys=True),
                                              encoding="utf-8")
    written = {"analysis": str(out / "analysis.json"),
               "forensics": str(out / "forensics")}
    if mark:
        from .render import mark_drawing
        designations = {p.pipe_id: p.designation for p in workspace.physical_pipes if p.designation}
        written["marked"] = mark_drawing(
            workspace.pdf, out / "marked.pdf", workspace.physical_pipes,
            list(workspace.leaders_by_text.values()),
            [c for c in workspace.candidates if c.candidate_id in
             {a.candidate_id for a in workspace.resolved.values()}],
            designations)["file"]
    if crops:
        from .render import render_crop
        crop_dir = out / "crops"
        made = []
        for pipe in workspace.physical_pipes[:crops]:
            xs = [p[0] for p in pipe.centerline]
            ys = [p[1] for p in pipe.centerline]
            if not xs:
                continue
            made.append(render_crop(workspace.pdf, pipe.page,
                                    (min(xs), min(ys), max(xs), max(ys)),
                                    crop_dir / f"{pipe.pipe_id.replace(':', '_')}.png"))
        written["crops"] = [m["file"] for m in made]
    quantities_csv = ["designation,diameterMm,pipes,horizontalM,verticalM,totalM,state"]
    for row in report["quantities"]:
        quantities_csv.append(
            f"{row['designation'] or ''},{row['diameterMm'] if row['diameterMm'] is not None else ''},"
            f"{row['pipeCount']},{row['horizontalMetres']},{row['verticalMetres']},"
            f"{row['totalMetres']},{row['designationState']}"
        )
    (out / "quantities.csv").write_text("\n".join(quantities_csv) + "\n", encoding="utf-8")
    written["quantities"] = str(out / "quantities.csv")
    return written


def summarise(report: dict) -> str:
    """The chain the acceptance test asks to see, in one screen."""
    stages = report["stages"]
    coverage = report["validation"]["coverage"]
    scale = stages["scale"]
    lines = [
        f"file                {report['file']['file']}  ({report['file']['pageCount']} page(s))",
        f"status              {report['status']}",
        "",
        f"PDF OBJECTS         {stages['objects']['inventory']['totalObjects']:>8}"
        f"   {stages['objects']['inventory']['byKind']}",
        f"  conservation      {'OK' if stages['objects']['conservation']['ok'] else 'FAILED':>8}",
        f"GLYPHS              {stages['glyphs']['glyphs']:>8}"
        f"   {stages['glyphs']['bySource']}  unresolved={stages['glyphs']['unresolved']}",
        f"TEXT                {stages['text']['textItems']:>8}   {stages['text']['bySource']}",
        f"DESIGNATIONS        {stages['designations']['designationCandidates']:>8} candidates"
        f"   confirmed={coverage['designations']['confirmed']}"
        f" ambiguous={coverage['designations']['ambiguous']}"
        f" unresolved={coverage['designations']['unresolved']}",
        f"LEADERS             {stages['leaders']['leaders']:>8}",
        f"PIPE GEOMETRY       {stages['pipes']['pipeCandidates']:>8} candidates"
        f"   {stages['pipes']['byKind']}",
        f"PIPE RUNS           {stages['topology']['pipeRuns']:>8}"
        f"   nodes={stages['topology']['nodes']} junctions={stages['topology']['junctions']}",
        f"PHYSICAL PIPES      {stages['physicalPipes']['count']:>8}"
        f"   named={stages['physicalPipes']['named']}"
        f" withDiameter={stages['physicalPipes']['withDiameter']}",
        f"DIMENSIONS          {stages['dimensions']['dimensionTokens']:>8} tokens"
        f"   {stages['dimensions']['byRule']}",
        f"SCALE               {scale.get('state', 'UNRESOLVED'):>8}"
        f"   1:{scale.get('denominator')}  ({len(scale.get('hypotheses', []))} hypotheses)",
        f"MEASUREMENTS        {stages['measurement']['pipesMeasured']:>8} measurable,"
        f" {stages['measurement']['pipesNotMeasurable']} not"
        f"   risers={stages['measurement']['risers']}"
        f" withHeight={stages['measurement']['risersWithHeight']}",
        "",
        f"reconciliation      {'OK' if report['reconciliation']['ok'] else 'FAILED'}",
        f"validation          {report['validation']['status']}"
        f"   failed={report['validation']['failed']}",
        f"evidence            {stages['evidence']['entities']} entities,"
        f" {stages['evidence']['edges']} edges, {stages['evidence']['rejections']} rejections",
        f"elapsed             {report['elapsedSeconds']}s",
    ]
    if report["quantities"]:
        lines.append("")
        lines.append("QUANTITIES")
        lines.append(f"  {'designation':<18}{'DN':>8}{'pipes':>7}{'horiz m':>10}{'vert m':>9}{'total m':>10}")
        for row in report["quantities"]:
            diameter = f"{row['diameterMm']:.0f}" if row["diameterMm"] is not None else "-"
            lines.append(
                f"  {(row['designation'] or '(unnamed)'):<18}{diameter:>8}"
                f"{row['pipeCount']:>7}{row['horizontalMetres']:>10.3f}"
                f"{row['verticalMetres']:>9.3f}{row['totalMetres']:>10.3f}"
            )
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pdf_forensics.analyze",
        description="Analyse one clean vector PDF and report what is in it.")
    parser.add_argument("pdf")
    parser.add_argument("--out", default=None, help="write the report and intermediates here")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--why", default=None, help="explain one entity id and exit")
    parser.add_argument("--order", default="normal",
                        help="normal | reversed | permuted:<seed> (determinism harness)")
    parser.add_argument("--crops", type=int, default=0, help="render N pipe crops")
    parser.add_argument("--no-mark", action="store_true", help="skip the marked drawing")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    def progress(name: str, seconds: float) -> None:
        if not args.quiet:
            print(f"  ... {name:<24}{seconds:>8.2f}s", file=sys.stderr)

    workspace, report = analyse(args.pdf, order=args.order, progress=progress)
    if args.out:
        written = write_outputs(workspace, report, args.out, crops=args.crops,
                                mark=not args.no_mark)
        report["outputs"] = written
    if args.why:
        print(json.dumps(workspace.evidence.why(args.why), indent=2, sort_keys=True))
        return 0
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(summarise(report))
        if args.out:
            print("\nwritten:")
            for key, value in sorted(report.get("outputs", {}).items()):
                print(f"  {key:<12} {value}")
    return 0 if report["status"] == validation.VALID else 2


if __name__ == "__main__":
    raise SystemExit(main())
