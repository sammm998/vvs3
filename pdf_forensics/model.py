"""The canonical intermediate representation.

Every record carries the identifiers of the records it was built from, so any
answer the engine gives can be walked back to the bytes of the PDF.  Nothing in
this module knows anything about a particular drawing, a particular naming
convention or a particular set of pipe codes.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional, Sequence

from .canonical import entity_id, q, qa, qbbox, qpoly, undirected

BBox = tuple[float, float, float, float]
Point = tuple[float, float]
Polyline = tuple[Point, ...]


# ---------------------------------------------------------------------------
# states - every entity says what it knows and why
# ---------------------------------------------------------------------------

class State:
    CONFIRMED = "CONFIRMED"
    AMBIGUOUS = "AMBIGUOUS"
    UNRESOLVED = "UNRESOLVED"
    CANDIDATE = "CANDIDATE"
    REJECTED = "REJECTED"


class Reason:
    NO_LEADER = "NO_LEADER"
    NO_GEOMETRY_AT_LEADER_END = "NO_GEOMETRY_AT_LEADER_END"
    COMPETING_PIPES_EQUALLY_SUPPORTED = "COMPETING_PIPES_EQUALLY_SUPPORTED"
    COMPETING_DESIGNATIONS_EQUALLY_SUPPORTED = "COMPETING_DESIGNATIONS_EQUALLY_SUPPORTED"
    ONE_DIRECTION_ONLY = "ONE_DIRECTION_ONLY"
    UNRESOLVED_GLYPH = "UNRESOLVED_GLYPH"
    NO_DIMENSION_EVIDENCE = "NO_DIMENSION_EVIDENCE"
    DIMENSION_CONFLICT = "DIMENSION_CONFLICT"
    SCALE_UNKNOWN = "SCALE_UNKNOWN"
    SCALE_CONFLICT = "SCALE_CONFLICT"
    VERTICAL_HEIGHT_UNKNOWN = "VERTICAL_HEIGHT_UNKNOWN"
    NO_DESIGNATION = "NO_DESIGNATION"
    TEXT_ONLY = "TEXT_ONLY"


# ---------------------------------------------------------------------------
# level 1 - raw PDF objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PdfObject:
    """One object as the PDF itself contains it.

    ``kind`` is what the file says it is, never what we hope it means.  No
    interpretation happens at this level: a leader line and a hatch line are
    both ``path``, and the drawing role is decided much later from geometry.
    """

    object_id: str
    page: int
    kind: str                      # text_span | glyph | path | image | annotation | form
    subtype: str                   # stroke | fill | stroke_fill | clip | char | ...
    bbox: BBox
    coordinates: tuple             # polylines for paths, origin for glyphs
    transform: tuple[float, ...]   # the 6-element matrix in effect
    style: dict[str, Any]
    source: dict[str, Any]         # provenance inside the file

    def to_json(self) -> dict:
        return {
            "objectId": self.object_id,
            "page": self.page,
            "kind": self.kind,
            "subtype": self.subtype,
            "bbox": list(self.bbox),
            "coordinates": _json_coords(self.coordinates),
            "transform": list(self.transform),
            "style": self.style,
            "source": self.source,
        }


def _json_coords(coords) -> Any:
    if isinstance(coords, (list, tuple)):
        return [_json_coords(c) for c in coords]
    return coords


@dataclass(frozen=True)
class Glyph:
    """One character-shaped mark.

    ``source`` records where the shape came from: a real text object
    (``text``), or a cluster of drawing paths that turned out to be lettering
    exported as outlines or single strokes (``path``).  ``alternatives`` keeps
    the competing readings so a later stage can recover from one wrong
    character instead of inheriting it.
    """

    glyph_id: str
    page: int
    character: str
    bbox: BBox
    origin: Point
    width: float
    height: float
    baseline: float
    rotation: float
    font: str
    size: float
    transform: tuple[float, ...]
    source: str                    # text | path
    source_object_ids: tuple[str, ...]
    alternatives: tuple[tuple[str, float], ...] = ()
    confidence: float = 1.0

    def to_json(self) -> dict:
        return {
            "glyphId": self.glyph_id,
            "page": self.page,
            "character": self.character,
            "bbox": list(self.bbox),
            "origin": list(self.origin),
            "width": self.width,
            "height": self.height,
            "baseline": self.baseline,
            "rotation": self.rotation,
            "font": self.font,
            "size": self.size,
            "transform": list(self.transform),
            "source": self.source,
            "sourceObjectIds": list(self.source_object_ids),
            "alternatives": [[c, s] for c, s in self.alternatives],
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class Segment:
    """A straight piece of a path, in the drawing's own coordinates."""

    segment_id: str
    page: int
    a: Point
    b: Point
    length: float
    angle: float                   # degrees, direction-independent [0, 180)
    width: float
    style_key: str
    path_id: str
    dashed: bool = False

    def to_json(self) -> dict:
        return {
            "segmentId": self.segment_id,
            "page": self.page,
            "a": list(self.a),
            "b": list(self.b),
            "length": self.length,
            "angle": self.angle,
            "width": self.width,
            "styleKey": self.style_key,
            "pathId": self.path_id,
            "dashed": self.dashed,
        }


# ---------------------------------------------------------------------------
# level 2 - reconstruction
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TextItem:
    """A string assembled from glyphs by geometry."""

    text_id: str
    page: int
    text: str
    bbox: BBox
    origin: Point
    rotation: float
    cap_height: float
    glyph_ids: tuple[str, ...]
    source: str                    # native | reconstructed | path
    confidence: float
    alternatives: tuple[tuple[str, float], ...] = ()

    def to_json(self) -> dict:
        return {
            "textId": self.text_id,
            "page": self.page,
            "text": self.text,
            "bbox": list(self.bbox),
            "origin": list(self.origin),
            "rotation": self.rotation,
            "capHeight": self.cap_height,
            "glyphIds": list(self.glyph_ids),
            "source": self.source,
            "confidence": self.confidence,
            "alternatives": [[t, s] for t, s in self.alternatives],
        }


@dataclass(frozen=True)
class DesignationCandidate:
    """A piece of text that *could* name a pipe.

    A candidate is never a designation.  Only association with real pipe
    geometry can promote one, and that happens in :mod:`pdf_forensics.pipes`.
    """

    candidate_id: str
    page: int
    text: str
    bbox: BBox
    rotation: float
    text_id: str
    glyph_ids: tuple[str, ...]
    source_object_ids: tuple[str, ...]
    structure: dict[str, Any]
    signals: dict[str, float]
    score: float
    state: str = State.CANDIDATE
    reasons: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {
            "candidateId": self.candidate_id,
            "page": self.page,
            "text": self.text,
            "bbox": list(self.bbox),
            "rotation": self.rotation,
            "textId": self.text_id,
            "glyphIds": list(self.glyph_ids),
            "sourceObjectIds": list(self.source_object_ids),
            "structure": self.structure,
            "signals": self.signals,
            "score": self.score,
            "state": self.state,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class Leader:
    """A pointing line that ties a piece of text to a place in the drawing."""

    leader_id: str
    page: int
    polyline: Polyline
    text_end: Point
    target_end: Point
    length: float
    segment_ids: tuple[str, ...]
    candidate_id: Optional[str]
    confidence: float

    def to_json(self) -> dict:
        return {
            "leaderId": self.leader_id,
            "page": self.page,
            "polyline": [list(p) for p in self.polyline],
            "textEnd": list(self.text_end),
            "targetEnd": list(self.target_end),
            "length": self.length,
            "segmentIds": list(self.segment_ids),
            "candidateId": self.candidate_id,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class PipeCandidate:
    """A centerline the geometry supports, with the evidence that produced it.

    ``kind`` is ``double_line`` when two parallel walls were paired,
    ``single_line`` when one stroke stands for the pipe, ``dashed`` when a dash
    chain was joined.  Nothing here has a name yet.
    """

    candidate_id: str
    page: int
    centerline: Polyline
    kind: str
    wall_separation: Optional[float]
    width: float
    style_key: str
    segment_ids: tuple[str, ...]
    source_object_ids: tuple[str, ...]
    length: float
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "candidateId": self.candidate_id,
            "page": self.page,
            "centerline": [list(p) for p in self.centerline],
            "kind": self.kind,
            "wallSeparation": self.wall_separation,
            "width": self.width,
            "styleKey": self.style_key,
            "segmentIds": list(self.segment_ids),
            "sourceObjectIds": list(self.source_object_ids),
            "length": self.length,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class PipeRun:
    """A chain of pipe candidates that continue into one another."""

    run_id: str
    page: int
    centerline: Polyline
    member_ids: tuple[str, ...]
    length: float
    wall_separation: Optional[float]
    node_ids: tuple[str, ...]

    def to_json(self) -> dict:
        return {
            "runId": self.run_id,
            "page": self.page,
            "centerline": [list(p) for p in self.centerline],
            "memberIds": list(self.member_ids),
            "length": self.length,
            "wallSeparation": self.wall_separation,
            "nodeIds": list(self.node_ids),
        }


@dataclass(frozen=True)
class PhysicalPipe:
    """One pipe in the building.

    Its identity is geometric: the runs it is made of and where they are.  The
    designation is *attached* afterwards and may be absent - a pipe exists
    whether or not the text stage managed to read its label.
    """

    pipe_id: str
    page: int
    run_ids: tuple[str, ...]
    centerline: Polyline             # every point, for bounds and identity
    parts: tuple[Polyline, ...]      # the runs themselves, for drawing and distance
    designation: Optional[str]
    designation_state: str
    designation_reasons: tuple[str, ...]
    diameter_mm: Optional[float]
    diameter_state: str
    diameter_reasons: tuple[str, ...]
    horizontal_points: float
    vertical_metres: Optional[float]
    vertical_state: str
    measurement: dict[str, Any] = field(default_factory=dict)
    confidence: dict[str, float] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "pipeId": self.pipe_id,
            "page": self.page,
            "runIds": list(self.run_ids),
            "centerline": [list(p) for p in self.centerline],
            "parts": [[list(p) for p in part] for part in self.parts],
            "designation": self.designation,
            "designationState": self.designation_state,
            "designationReasons": list(self.designation_reasons),
            "diameterMm": self.diameter_mm,
            "diameterState": self.diameter_state,
            "diameterReasons": list(self.diameter_reasons),
            "horizontalPoints": self.horizontal_points,
            "verticalMetres": self.vertical_metres,
            "verticalState": self.vertical_state,
            "measurement": self.measurement,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class Association:
    """A designation/pipe pair, with the direction each piece of support came from."""

    association_id: str
    candidate_id: str
    pipe_id: str
    forward: dict[str, Any]        # designation -> leader -> geometry -> pipe
    backward: dict[str, Any]       # pipe -> neighbourhood -> text -> designation
    score: float
    state: str
    reasons: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {
            "associationId": self.association_id,
            "candidateId": self.candidate_id,
            "pipeId": self.pipe_id,
            "forward": self.forward,
            "backward": self.backward,
            "score": self.score,
            "state": self.state,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class Hit:
    """One search result.  Every search in the package returns these."""

    page: int
    object_id: str
    type: str
    bbox: BBox
    coordinates: Any
    transform: tuple[float, ...]
    source: dict[str, Any]
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "page": self.page,
            "objectId": self.object_id,
            "type": self.type,
            "bbox": list(self.bbox),
            "coordinates": _json_coords(self.coordinates),
            "transform": list(self.transform),
            "source": self.source,
            "detail": self.detail,
        }

    def sort_key(self):
        return (self.page, self.bbox, self.type, self.object_id)
