"""Identity states and machine-readable reason codes.

The engine is conservative by construction: every entity carries a state and,
when it is not CONFIRMED, at least one reason code explaining *why*.  Nothing
in the pipeline is allowed to invent a value in order to reach a "nicer"
state - see ``vvs_pipe.validation.reconcile``.
"""

from __future__ import annotations

from enum import Enum


class IdentityState(str, Enum):
    CONFIRMED = "CONFIRMED"
    HIGH_CONFIDENCE = "HIGH_CONFIDENCE"
    AMBIGUOUS = "AMBIGUOUS"
    INSUFFICIENT = "INSUFFICIENT"
    UNRESOLVED = "UNRESOLVED"


# Ordered worst -> best so that a merge can only ever *lower* a state.
STATE_RANK = {
    IdentityState.UNRESOLVED: 0,
    IdentityState.INSUFFICIENT: 1,
    IdentityState.AMBIGUOUS: 2,
    IdentityState.HIGH_CONFIDENCE: 3,
    IdentityState.CONFIRMED: 4,
}


def worst(*states: IdentityState) -> IdentityState:
    return min(states, key=lambda s: STATE_RANK[s])


class Reason(str, Enum):
    """Why an entity is not CONFIRMED."""

    INSUFFICIENT_GEOMETRY = "INSUFFICIENT_GEOMETRY"
    AMBIGUOUS_ASSOCIATION = "AMBIGUOUS_ASSOCIATION"
    NO_ASSOCIATION_EVIDENCE = "NO_ASSOCIATION_EVIDENCE"
    NO_SCALE = "NO_SCALE"
    SCALE_UNKNOWN = "SCALE_UNKNOWN"
    SCALE_AMBIGUOUS = "SCALE_AMBIGUOUS"
    UNSUPPORTED_GLYPH = "UNSUPPORTED_GLYPH"
    UNRESOLVED_GLYPH = "UNRESOLVED_GLYPH"
    LOW_GLYPH_MARGIN = "LOW_GLYPH_MARGIN"
    VERTICAL_HEIGHT_UNKNOWN = "VERTICAL_HEIGHT_UNKNOWN"
    COMPETING_PIPES = "COMPETING_PIPES"
    NOT_MEASURABLE = "NOT_MEASURABLE"
    NO_DIMENSION_EVIDENCE = "NO_DIMENSION_EVIDENCE"
    NO_DRAWN_WIDTH = "NO_DRAWN_WIDTH"
    DIMENSION_CONFLICT = "DIMENSION_CONFLICT"
    LEGEND_INSTANCE = "LEGEND_INSTANCE"
    NO_CENTERLINE = "NO_CENTERLINE"
    DUPLICATE_GEOMETRY = "DUPLICATE_GEOMETRY"
    NO_DESIGNATION = "NO_DESIGNATION"
    OPEN_ENDED_RUN = "OPEN_ENDED_RUN"
    ANNOTATION_EXCLUDED = "ANNOTATION_EXCLUDED"
    NO_PIPE_EVIDENCE = "NO_PIPE_EVIDENCE"
    LEADER_ENDS_NOWHERE = "LEADER_ENDS_NOWHERE"
    TEXT_NOT_CODE_LIKE = "TEXT_NOT_CODE_LIKE"
    SCALE_CONFLICT = "SCALE_CONFLICT"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"


class ScaleState(str, Enum):
    """Outcome of scale inference.

    ``SCALE_CONFIRMED`` means two or more independent hypotheses agreed.
    ``RESOLVED`` means exactly one hypothesis was available and nothing
    contradicted it - usable, but not corroborated.  ``SCALE_CONFLICT`` means
    hypotheses disagreed beyond tolerance and the engine refuses to choose.
    """

    SCALE_CONFIRMED = "SCALE_CONFIRMED"
    RESOLVED = "RESOLVED"
    SCALE_CONFLICT = "SCALE_CONFLICT"
    SCALE_UNKNOWN = "SCALE_UNKNOWN"
    SCALE_AMBIGUOUS = "SCALE_AMBIGUOUS"


class AnalysisStatus(str, Enum):
    """Whether the run may be presented as a quantity take-off at all.

    ``INVALID`` is not a soft warning: it means an internal invariant broke
    (a metre counted twice, a run in two pipes) and no number in the report may
    be relied on.  ``INCOMPLETE`` means the invariants hold but something the
    drawing did not supply - most often the scale - stops the quantities from
    being measurable.
    """

    VALID = "VALID"
    INCOMPLETE = "INCOMPLETE"
    INVALID = "INVALID"


class DrawingRole(str, Enum):
    """What a piece of geometry *is* on the sheet.

    Assigned to every vector object before any text is consulted, so pipe
    geometry is found by looking at the drawing rather than by looking at what
    is left over once the labels have been taken away.
    """

    TEXT = "TEXT"
    PIPE = "PIPE"
    WALL = "WALL"
    SYMBOL = "SYMBOL"
    LEADER = "LEADER"
    DIMENSION = "DIMENSION"
    REFERENCE_LINE = "REFERENCE_LINE"
    HATCH = "HATCH"
    TITLE_BLOCK = "TITLE_BLOCK"
    LEGEND = "LEGEND"
    GRID = "GRID"
    ELEVATION = "ELEVATION"
    EQUIPMENT = "EQUIPMENT"
    UNKNOWN = "UNKNOWN"


class DesignationTier(str, Enum):
    """How far a piece of text got towards being a pipe designation.

    Text is never a designation because of the way it is spelled.  It is a
    designation when there is evidence tying it to a pipe that was detected
    independently of it, and the tier records exactly how far that evidence got.
    """

    TEXT_ONLY = "TEXT_ONLY"
    DESIGNATION_CANDIDATE = "DESIGNATION_CANDIDATE"
    CONFIRMED_DESIGNATION = "CONFIRMED_DESIGNATION"


class TextRole(str, Enum):
    """Open-world classification of a reconstructed text string."""

    PIPE_DESIGNATION = "PIPE_DESIGNATION"
    DIMENSION = "DIMENSION"
    ELEVATION = "ELEVATION"
    SCALE_NOTE = "SCALE_NOTE"
    ROOM_LABEL = "ROOM_LABEL"
    EQUIPMENT_CODE = "EQUIPMENT_CODE"
    LEGEND_ENTRY = "LEGEND_ENTRY"
    TITLE_BLOCK = "TITLE_BLOCK"
    REFERENCE = "REFERENCE"
    IRRELEVANT = "IRRELEVANT"
