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


class ScaleState(str, Enum):
    RESOLVED = "RESOLVED"
    SCALE_UNKNOWN = "SCALE_UNKNOWN"
    SCALE_AMBIGUOUS = "SCALE_AMBIGUOUS"


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
