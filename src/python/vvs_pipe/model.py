"""Canonical intermediate representation shared by every pipeline stage.

Every entity exposes:

* ``canonical_key()`` - a total-ordering key derived from geometry/evidence
  only (never an id, counter or array position);
* ``to_canonical()`` - deterministic JSON-ready form;
* ``provenance`` - the source object ids and the rule that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .canonical import ql, qc, qs, entity_id, polyline_key
from .geometry.primitives import BBox, Segment, polyline_length
from .states import IdentityState, Reason, ScaleState, TextRole

Pt = tuple[float, float]


# --------------------------------------------------------------------------
# Provenance & confidence
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Provenance:
    """Why an entity exists, in machine-readable form."""

    stage: str
    rule: str
    source_object_ids: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_canonical(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "rule": self.rule,
            "sourceObjectIds": sorted(self.source_object_ids),
            "inputs": sorted(self.inputs),
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class Confidence:
    """Decomposable confidence. ``overall`` is the min of the present parts.

    The minimum (not the mean) is used deliberately: a chain of inferences is
    no stronger than its weakest link, and averaging lets a strong geometric
    signal mask an unresolved association.
    """

    geometry: float | None = None
    text: float | None = None
    association: float | None = None
    topology: float | None = None
    dimension: float | None = None
    vertical: float | None = None

    @property
    def overall(self) -> float:
        parts = [
            p
            for p in (
                self.geometry,
                self.text,
                self.association,
                self.topology,
                self.dimension,
                self.vertical,
            )
            if p is not None
        ]
        if not parts:
            return 0.0
        return min(parts)

    def to_canonical(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in ("geometry", "text", "association", "topology", "dimension", "vertical"):
            v = getattr(self, name)
            if v is not None:
                out[name] = qs(v)
        out["overall"] = qs(self.overall)
        return out


# --------------------------------------------------------------------------
# Vector layer
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VectorObject:
    """One drawable extracted from the PDF content stream.

    ``kind`` is one of ``line``, ``curve``, ``rect``, ``quad``.  Curves are
    flattened to polylines at extraction time (the flattening tolerance is
    recorded in provenance) so that all downstream stages work on polylines.
    """

    object_id: str
    page: int
    kind: str
    points: tuple[Pt, ...]
    closed: bool
    stroke_color: tuple[float, float, float] | None
    fill_color: tuple[float, float, float] | None
    stroke_width: float | None
    dashes: str | None
    layer: str | None
    even_odd: bool
    from_annotation: bool
    clip_bbox: tuple[float, float, float, float] | None = None
    transform: tuple[float, float, float, float, float, float] | None = None

    @property
    def bbox(self) -> BBox:
        return BBox.from_points(self.points)

    @property
    def length(self) -> float:
        return polyline_length(self.points)

    @property
    def is_stroked(self) -> bool:
        return self.stroke_color is not None

    @property
    def is_filled(self) -> bool:
        return self.fill_color is not None

    def segments(self) -> list[Segment]:
        pts = list(self.points)
        if self.closed and len(pts) > 2 and pts[0] != pts[-1]:
            pts.append(pts[0])
        out = []
        for i in range(len(pts) - 1):
            if pts[i] != pts[i + 1]:
                out.append(Segment(pts[i], pts[i + 1]))
        return out

    def canonical_key(self) -> tuple:
        return (
            self.page,
            polyline_key(self.points),
            self.kind,
            bool(self.closed),
            qc(self.stroke_width) if self.stroke_width is not None else -1.0,
            self.stroke_color or (-1.0, -1.0, -1.0),
            self.fill_color or (-1.0, -1.0, -1.0),
            self.dashes or "",
            self.layer or "",
            bool(self.from_annotation),
        )

    def to_canonical(self) -> dict[str, Any]:
        return {
            "objectId": self.object_id,
            "page": self.page,
            "kind": self.kind,
            "points": [[qc(x), qc(y)] for x, y in self.points],
            "closed": self.closed,
            "strokeColor": list(self.stroke_color) if self.stroke_color else None,
            "fillColor": list(self.fill_color) if self.fill_color else None,
            "strokeWidth": qc(self.stroke_width) if self.stroke_width is not None else None,
            "dashes": self.dashes,
            "layer": self.layer,
            "evenOdd": self.even_odd,
            "fromAnnotation": self.from_annotation,
            "bbox": self.bbox.to_canonical(),
        }


@dataclass(frozen=True, slots=True)
class TextSpan:
    """Native PDF text, when the producer left a text layer behind."""

    span_id: str
    page: int
    text: str
    bbox: BBox
    font: str
    size: float
    rotation: float
    from_annotation: bool

    def canonical_key(self) -> tuple:
        return (self.page, self.bbox.key(), self.text, qc(self.size))

    def to_canonical(self) -> dict[str, Any]:
        return {
            "spanId": self.span_id,
            "page": self.page,
            "text": self.text,
            "bbox": self.bbox.to_canonical(),
            "font": self.font,
            "size": qc(self.size),
            "rotation": qc(self.rotation),
            "fromAnnotation": self.from_annotation,
        }


@dataclass(frozen=True, slots=True)
class PageInfo:
    page: int
    width: float
    height: float
    rotation: int
    media_box: tuple[float, float, float, float]
    crop_box: tuple[float, float, float, float]

    def to_canonical(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "width": qc(self.width),
            "height": qc(self.height),
            "rotation": self.rotation,
            "mediaBox": [qc(v) for v in self.media_box],
            "cropBox": [qc(v) for v in self.crop_box],
        }


@dataclass(slots=True)
class VectorDocument:
    """The full extraction result - the pipeline's only view of the PDF."""

    source_name: str
    sha256: str
    pages: list[PageInfo]
    objects: list[VectorObject]
    text_spans: list[TextSpan]
    excluded_annotation_objects: int
    excluded_annotation_spans: int

    def objects_on(self, page: int) -> list[VectorObject]:
        return [o for o in self.objects if o.page == page]

    def spans_on(self, page: int) -> list[TextSpan]:
        return [s for s in self.text_spans if s.page == page]


# --------------------------------------------------------------------------
# Glyph / text layer
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GlyphCandidate:
    glyph_id: str
    page: int
    bbox: BBox
    source_object_ids: tuple[str, ...]
    stroke_count: int
    holes: int
    aspect: float
    complexity: float
    contour_signature: tuple[float, ...]
    character: str | None
    alternatives: tuple[tuple[str, float], ...]
    confidence: float
    state: IdentityState
    reasons: tuple[Reason, ...]
    provenance: Provenance

    def canonical_key(self) -> tuple:
        return (self.page, self.bbox.key(), tuple(sorted(self.source_object_ids)))

    def to_canonical(self) -> dict[str, Any]:
        return {
            "glyphId": self.glyph_id,
            "page": self.page,
            "bbox": self.bbox.to_canonical(),
            "character": self.character,
            "alternatives": [[c, qs(s)] for c, s in self.alternatives],
            "confidence": qs(self.confidence),
            "state": self.state.value,
            "reasons": [r.value for r in self.reasons],
            "strokeCount": self.stroke_count,
            "holes": self.holes,
            "aspect": qs(self.aspect),
            "complexity": qs(self.complexity),
            "sourceObjectIds": sorted(self.source_object_ids),
            "provenance": self.provenance.to_canonical(),
        }


@dataclass(frozen=True, slots=True)
class TextItem:
    """A reconstructed text string (from glyphs, or from a native text span)."""

    text_id: str
    page: int
    text: str
    bbox: BBox
    rotation: float
    height: float
    origin: str  # "glyph" | "span"
    glyph_ids: tuple[str, ...]
    source_object_ids: tuple[str, ...]
    confidence: float
    state: IdentityState
    reasons: tuple[Reason, ...]
    provenance: Provenance

    def canonical_key(self) -> tuple:
        return (self.page, self.bbox.key(), self.text, self.origin)

    def to_canonical(self) -> dict[str, Any]:
        return {
            "textId": self.text_id,
            "page": self.page,
            "text": self.text,
            "bbox": self.bbox.to_canonical(),
            "rotation": qs(self.rotation),
            "height": qc(self.height),
            "origin": self.origin,
            "glyphIds": sorted(self.glyph_ids),
            "sourceObjectIds": sorted(self.source_object_ids),
            "confidence": qs(self.confidence),
            "state": self.state.value,
            "reasons": [r.value for r in self.reasons],
            "provenance": self.provenance.to_canonical(),
        }


@dataclass(frozen=True, slots=True)
class TokenStructure:
    """Generic token decomposition of a text string (no code whitelist)."""

    parts: tuple[tuple[str, str], ...]  # (class, value); class in L/D/S/O
    pattern: str

    def to_canonical(self) -> dict[str, Any]:
        return {"parts": [[c, v] for c, v in self.parts], "pattern": self.pattern}


@dataclass(frozen=True, slots=True)
class Designation:
    designation_id: str
    page: int
    text: str
    bbox: BBox
    role: TextRole
    role_scores: tuple[tuple[str, float], ...]
    is_legend: bool
    structure: TokenStructure
    diameter_mm: float | None
    diameter_reason: Reason | None
    system_token: str | None
    text_item_id: str
    glyph_ids: tuple[str, ...]
    source_object_ids: tuple[str, ...]
    confidence: Confidence
    state: IdentityState
    reasons: tuple[Reason, ...]
    associated_physical_pipe_ids: tuple[str, ...]
    provenance: Provenance

    def canonical_key(self) -> tuple:
        return (self.page, self.bbox.key(), self.text)

    def to_canonical(self) -> dict[str, Any]:
        return {
            "designationId": self.designation_id,
            "page": self.page,
            "designation": self.text,
            "bbox": self.bbox.to_canonical(),
            "role": self.role.value,
            "roleScores": [[k, qs(v)] for k, v in self.role_scores],
            "isLegend": self.is_legend,
            "structure": self.structure.to_canonical(),
            "diameterMm": ql(self.diameter_mm) if self.diameter_mm is not None else None,
            "diameterReason": self.diameter_reason.value if self.diameter_reason else None,
            "systemToken": self.system_token,
            "textItemId": self.text_item_id,
            "glyphs": sorted(self.glyph_ids),
            "sourceObjects": sorted(self.source_object_ids),
            "confidence": self.confidence.to_canonical(),
            "state": self.state.value,
            "reasons": [r.value for r in self.reasons],
            "associatedPhysicalPipeIds": sorted(self.associated_physical_pipe_ids),
            "provenance": self.provenance.to_canonical(),
        }


# --------------------------------------------------------------------------
# Pipe layer
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PipeCandidate:
    candidate_id: str
    page: int
    centerline: tuple[Pt, ...]
    style: str  # "double_line" | "single_line"
    width_pt: float | None
    stroke_width: float | None
    color: tuple[float, float, float] | None
    dashes: str | None
    source_object_ids: tuple[str, ...]
    accepted: bool
    rejection_reason: Reason | None
    confidence: Confidence
    evidence: tuple[tuple[str, float], ...]
    provenance: Provenance

    @property
    def length_pt(self) -> float:
        return polyline_length(self.centerline)

    def canonical_key(self) -> tuple:
        return (self.page, polyline_key(self.centerline), self.style)

    def to_canonical(self) -> dict[str, Any]:
        return {
            "candidateId": self.candidate_id,
            "page": self.page,
            "centerline": [[qc(x), qc(y)] for x, y in self.centerline],
            "style": self.style,
            "widthPt": qc(self.width_pt) if self.width_pt is not None else None,
            "strokeWidth": qc(self.stroke_width) if self.stroke_width is not None else None,
            "color": list(self.color) if self.color else None,
            "dashes": self.dashes,
            "lengthPt": ql(self.length_pt),
            "accepted": self.accepted,
            "rejectionReason": self.rejection_reason.value if self.rejection_reason else None,
            "confidence": self.confidence.to_canonical(),
            "evidence": [[k, qs(v)] for k, v in self.evidence],
            "sourceObjectIds": sorted(self.source_object_ids),
            "provenance": self.provenance.to_canonical(),
        }


@dataclass(frozen=True, slots=True)
class GraphNode:
    node_id: str
    page: int
    point: Pt
    degree: int
    kind: str  # "endpoint" | "junction" | "continuation"

    def to_canonical(self) -> dict[str, Any]:
        return {
            "nodeId": self.node_id,
            "page": self.page,
            "point": [qc(self.point[0]), qc(self.point[1])],
            "degree": self.degree,
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class GraphEdge:
    edge_id: str
    page: int
    node_a: str
    node_b: str
    polyline: tuple[Pt, ...]
    candidate_id: str
    width_pt: float | None
    style: str

    @property
    def length_pt(self) -> float:
        return polyline_length(self.polyline)

    def to_canonical(self) -> dict[str, Any]:
        return {
            "edgeId": self.edge_id,
            "page": self.page,
            "nodeA": self.node_a,
            "nodeB": self.node_b,
            "polyline": [[qc(x), qc(y)] for x, y in self.polyline],
            "candidateId": self.candidate_id,
            "widthPt": qc(self.width_pt) if self.width_pt is not None else None,
            "style": self.style,
            "lengthPt": ql(self.length_pt),
        }


@dataclass(frozen=True, slots=True)
class PipeRun:
    pipe_run_id: str
    page: int
    centerline: tuple[Pt, ...]
    edge_ids: tuple[str, ...]
    source_object_ids: tuple[str, ...]
    width_pt: float | None
    style: str
    direction: str  # "horizontal" | "vertical_on_sheet" | "diagonal" | "mixed"
    designation_candidates: tuple[str, ...]
    dimension_candidates: tuple[float, ...]
    vertical_transition_ids: tuple[str, ...]
    confidence: Confidence
    state: IdentityState
    reasons: tuple[Reason, ...]
    provenance: Provenance

    @property
    def length_pt(self) -> float:
        return polyline_length(self.centerline)

    def canonical_key(self) -> tuple:
        return (self.page, polyline_key(self.centerline))

    def to_canonical(self) -> dict[str, Any]:
        return {
            "pipeRunId": self.pipe_run_id,
            "page": self.page,
            "centerline": [[qc(x), qc(y)] for x, y in self.centerline],
            "edgeIds": sorted(self.edge_ids),
            "sourceObjectIds": sorted(self.source_object_ids),
            "widthPt": qc(self.width_pt) if self.width_pt is not None else None,
            "style": self.style,
            "direction": self.direction,
            "lengthPt": ql(self.length_pt),
            "designationCandidates": sorted(self.designation_candidates),
            "dimensionCandidates": sorted(ql(d) for d in self.dimension_candidates),
            "verticalTransitionIds": sorted(self.vertical_transition_ids),
            "confidence": self.confidence.to_canonical(),
            "state": self.state.value,
            "reasons": [r.value for r in self.reasons],
            "provenance": self.provenance.to_canonical(),
        }


@dataclass(frozen=True, slots=True)
class VerticalSegment:
    vertical_id: str
    page: int
    point: Pt
    attached_run_ids: tuple[str, ...]
    from_elevation_m: float | None
    to_elevation_m: float | None
    length_m: float | None
    evidence: tuple[tuple[str, float], ...]
    state: IdentityState
    reasons: tuple[Reason, ...]
    confidence: Confidence
    provenance: Provenance

    def canonical_key(self) -> tuple:
        return (self.page, (qc(self.point[0]), qc(self.point[1])))

    def to_canonical(self) -> dict[str, Any]:
        return {
            "verticalId": self.vertical_id,
            "page": self.page,
            "point": [qc(self.point[0]), qc(self.point[1])],
            "attachedRunIds": sorted(self.attached_run_ids),
            "fromElevationM": ql(self.from_elevation_m) if self.from_elevation_m is not None else None,
            "toElevationM": ql(self.to_elevation_m) if self.to_elevation_m is not None else None,
            "lengthM": ql(self.length_m) if self.length_m is not None else None,
            "evidence": [[k, qs(v)] for k, v in self.evidence],
            "state": self.state.value,
            "reasons": [r.value for r in self.reasons],
            "confidence": self.confidence.to_canonical(),
            "provenance": self.provenance.to_canonical(),
        }


@dataclass(frozen=True, slots=True)
class PhysicalPipe:
    physical_pipe_id: str
    page: int
    pipe_run_ids: tuple[str, ...]
    centerline: tuple[tuple[Pt, ...], ...]
    source_object_ids: tuple[str, ...]
    horizontal_length_m: float | None
    vertical_length_m: float | None
    total_length_m: float | None
    length_pt: float
    diameter_mm: float | None
    designation: str | None
    designation_ids: tuple[str, ...]
    vertical_ids: tuple[str, ...]
    identity_state: IdentityState
    reasons: tuple[Reason, ...]
    confidence: Confidence
    evidence: tuple[tuple[str, float], ...]
    provenance: Provenance

    def canonical_key(self) -> tuple:
        return (self.page, tuple(sorted(polyline_key(p) for p in self.centerline)))

    def to_canonical(self) -> dict[str, Any]:
        return {
            "physicalPipeId": self.physical_pipe_id,
            "page": self.page,
            "pipeRunIds": sorted(self.pipe_run_ids),
            "geometry": [[[qc(x), qc(y)] for x, y in poly] for poly in self.centerline],
            "sourceObjectIds": sorted(self.source_object_ids),
            "lengthPt": ql(self.length_pt),
            "horizontalLengthM": ql(self.horizontal_length_m) if self.horizontal_length_m is not None else None,
            "verticalLengthM": ql(self.vertical_length_m) if self.vertical_length_m is not None else None,
            "totalLengthM": ql(self.total_length_m) if self.total_length_m is not None else None,
            "diameterMm": ql(self.diameter_mm) if self.diameter_mm is not None else None,
            "designation": self.designation,
            "designationIds": sorted(self.designation_ids),
            "verticalIds": sorted(self.vertical_ids),
            "identityState": self.identity_state.value,
            "reasons": [r.value for r in self.reasons],
            "confidence": self.confidence.to_canonical(),
            "evidence": [[k, qs(v)] for k, v in self.evidence],
            "provenance": self.provenance.to_canonical(),
        }


@dataclass(frozen=True, slots=True)
class QuantityRow:
    designation: str | None
    diameter_mm: float | None
    horizontal_m: float | None
    vertical_m: float | None
    total_m: float | None
    pipe_count: int
    physical_pipe_ids: tuple[str, ...]
    state: IdentityState
    reasons: tuple[Reason, ...]
    confidence: Confidence

    def canonical_key(self) -> tuple:
        return (
            self.designation or "￿",
            ql(self.diameter_mm) if self.diameter_mm is not None else -1.0,
            self.state.value,
        )

    def to_canonical(self) -> dict[str, Any]:
        return {
            "designation": self.designation,
            "diameterMm": ql(self.diameter_mm) if self.diameter_mm is not None else None,
            "horizontalM": ql(self.horizontal_m) if self.horizontal_m is not None else None,
            "verticalM": ql(self.vertical_m) if self.vertical_m is not None else None,
            "totalM": ql(self.total_m) if self.total_m is not None else None,
            "pipeCount": self.pipe_count,
            "physicalPipeIds": sorted(self.physical_pipe_ids),
            "state": self.state.value,
            "reasons": [r.value for r in self.reasons],
            "confidence": self.confidence.to_canonical(),
        }


@dataclass(frozen=True, slots=True)
class ScaleResult:
    state: ScaleState
    metres_per_point: float | None
    ratio_denominator: float | None
    sources: tuple[tuple[str, float], ...]
    reasons: tuple[Reason, ...]
    provenance: Provenance

    def to_canonical(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "metresPerPoint": None if self.metres_per_point is None else ql(self.metres_per_point * 1e6) / 1e6,
            "ratioDenominator": None if self.ratio_denominator is None else ql(self.ratio_denominator),
            "sources": [[k, qs(v)] for k, v in self.sources],
            "reasons": [r.value for r in self.reasons],
            "provenance": self.provenance.to_canonical(),
        }
