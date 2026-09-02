"""Open-world designation discovery.

There is no list of known codes anywhere in this module.  A text item is
scored for each role from the *shape of its token structure* and from the
geometry around it:

* token structure - runs of letters/digits joined by separators, how many runs,
  whether a run is a plausible nominal size, whether the string is a ratio, an
  elevation, or plain words;
* geometry - whether the text sits inside a legend/title panel, whether a
  leader line starts at it, how much drawing geometry runs nearby, and how many
  times the same string occurs on the sheet.

A drawing whose codes have never been seen before scores exactly the same way.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Mapping, Sequence

from ..canonical import canonical_sort, entity_id, qs
from ..geometry.index import SpatialIndex
from ..geometry.primitives import BBox, Segment, dist, point_segment_distance
from ..model import (
    Confidence,
    Designation,
    Provenance,
    TextItem,
    TokenStructure,
    VectorObject,
)
from ..states import DesignationTier, IdentityState, Reason, TextRole
from ..text_reconstruction.tokens import token_structure
from .legend import Panel

# Nominal pipe sizes are bounded by physics, not by a catalogue: from the
# smallest pipe a building service uses to a large culvert.  The bound only
# rejects values that cannot be a nominal size at all; every candidate it does
# admit is still reconciled against the *measured* drawn width later, and the
# measurement wins on disagreement (see vvs_pipe.dimensions.parser).
MIN_NOMINAL_MM = 10.0
MAX_NOMINAL_MM = 3000.0

STACK_MAX_VERTICAL_GAP_RATIO = 1.6   # of cap height, between the two lines
STACK_MIN_HORIZONTAL_OVERLAP = 0.30  # of the narrower line
LEADER_MIN_LENGTH_RATIO = 1.2   # of cap height
LEADER_ATTACH_RATIO = 0.9       # of cap height
NEAR_GEOMETRY_RATIO = 6.0       # of cap height

_RATIO_RE = re.compile(r"(?<![\d.])1\s*:\s*(\d{1,5})(?![\d.])")
_ELEVATION_RE = re.compile(r"^([A-ZÅÄÖ]{0,4})\s*([+\-])\s*(\d{1,3})[.,](\d{1,3})$")
_EXPLICIT_DIAMETER_RE = re.compile(r"^(?:Ø|DN|D)\s*(\d{1,4})$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class StackedLabel:
    """A label written on two lines, the way CAD annotation usually is.

    The code sits on the upper line and the nominal size on the lower one,
    separated by a rule - ``S3-R8`` over ``75`` means ``S3-R8-75``.  The lower
    line may instead be a level note (``S3-R8-110`` over ``VG+1.67``), which is
    not part of the code but is that pipe's invert level.
    """

    upper_text_id: str
    lower_text_id: str
    kind: str  # "size" | "elevation"
    value: float


@dataclass(frozen=True, slots=True)
class DesignationDiscovery:
    designations: tuple[Designation, ...]
    scale_notes: tuple[tuple[str, float], ...]
    elevation_notes: tuple[tuple[str, float, BBox], ...]
    leaders: tuple[tuple[str, Segment], ...]
    leader_object_ids: frozenset[str]
    stacked: tuple[StackedLabel, ...]


def _leader_candidates(objects: Sequence[VectorObject], cap: float) -> list[tuple[str, Segment]]:
    out: list[tuple[str, Segment]] = []
    for o in objects:
        if not o.is_stroked or len(o.points) != 2:
            continue
        seg = Segment(o.points[0], o.points[1])
        if seg.length >= LEADER_MIN_LENGTH_RATIO * cap:
            out.append((o.object_id, seg))
    return out


def parse_ratio(text: str) -> float | None:
    """Find a ``1:N`` drawing ratio anywhere in a string.

    The note is normally embedded in other words ("SKALA 1:50", "SCALE 1:100"),
    so the ratio is searched for rather than required to be the whole string.
    The prefix word is never interpreted, so any language works.
    """
    m = _RATIO_RE.search(text)
    if not m:
        return None
    den = int(m.group(1))
    return float(den) if den > 0 else None


def parse_elevation(text: str) -> float | None:
    """Parse a height note such as ``VG+2.800`` into metres.

    The alphabetic prefix is *not* interpreted - any prefix is accepted, so a
    drawing that writes ``FG``, ``OK`` or nothing at all parses identically.
    """
    m = _ELEVATION_RE.match(text.replace(" ", "").upper())
    if not m:
        return None
    sign = -1.0 if m.group(2) == "-" else 1.0
    whole, frac = m.group(3), m.group(4)
    return sign * (int(whole) + int(frac) / (10 ** len(frac)))


def parse_nominal_size(token: str) -> float | None:
    if not token.isdigit():
        return None
    v = float(token)
    if MIN_NOMINAL_MM <= v <= MAX_NOMINAL_MM:
        return v
    return None


def _code_like(parts: Sequence[tuple[str, str]]) -> bool:
    """Does the token structure look like an engineering code?

    Two or more alphanumeric runs joined by separators, at least one of which
    contains a digit.  ``S1-P2-110``, ``KV1-X7``, ``ABC-17-X-250`` and
    ``VP-003-A`` all satisfy this; ``TVATT`` and ``TECKENFORKLARING`` do not.
    """
    runs = [p for p in parts if p[0] in ("L", "D")]
    seps = [p for p in parts if p[0] == "S" and p[1].strip()]
    if len(runs) < 2 or not seps:
        return False
    return any(p[0] == "D" for p in runs)


def discover_designations(
    text_items: Sequence[TextItem],
    objects: Sequence[VectorObject],
    panels: Sequence[Panel],
    page_box: BBox,
    page: int,
    exclude_object_ids: frozenset[str] = frozenset(),
    traced_leaders: Mapping[str, "object"] | None = None,
) -> DesignationDiscovery:
    """Read the sheet's text and score what each string could be.

    ``traced_leaders`` maps a text item to the leader
    :mod:`vvs_pipe.association.leaders` actually traced for it.  When it is
    given, that is what "this label has a leader" means here too - a single
    two-point stroke touching a label is not a leader, and treating it as one
    was how the role scores came to rest on evidence the drawing never gave.
    """
    items = canonical_sort(list(text_items), key=lambda t: t.canonical_key())
    caps = sorted(max(t.height, 1e-3) for t in items) or [7.0]
    median_cap = caps[len(caps) // 2]

    leaders = [
        (oid, seg)
        for oid, seg in _leader_candidates(objects, median_cap)
        if oid not in exclude_object_ids
    ] if traced_leaders is None else []
    leader_index: SpatialIndex[int] = SpatialIndex.for_items(
        [(s.bbox, i) for i, (_oid, s) in enumerate(leaders)]
    )
    attached_leaders: set[str] = set()
    attached_pairs: list[tuple[str, Segment]] = []
    geom_index: SpatialIndex[int] = SpatialIndex.for_items(
        [(o.bbox, i) for i, o in enumerate(objects) if o.is_stroked]
    )

    stacked = _detect_stacked_labels(items)
    size_below = {st.upper_text_id: st for st in stacked if st.kind == "size"}
    elevation_below = {st.upper_text_id: st for st in stacked if st.kind == "elevation"}
    attached_lower = {st.lower_text_id for st in stacked}

    def display_text(t: TextItem) -> str:
        st = size_below.get(t.text_id)
        return f"{t.text}-{st.value:g}" if st else t.text

    occurrences: dict[str, int] = {}
    for t in items:
        occurrences[display_text(t)] = occurrences.get(display_text(t), 0) + 1

    panel_of: dict[str, Panel] = {}
    for p in panels:
        for tid in p.text_item_ids:
            panel_of[tid] = p

    # A panel holding a scale ratio is a title block; a panel holding repeated
    # code-like strings is a legend.  Both are derived, never assumed.
    title_panels: set[str] = set()
    for p in panels:
        for tid in p.text_item_ids:
            item = next((t for t in items if t.text_id == tid), None)
            if item is not None and parse_ratio(item.text) is not None:
                title_panels.add(p.panel_id)

    scale_notes: list[tuple[str, float]] = []
    elevation_notes: list[tuple[str, float, BBox]] = []
    designations: list[Designation] = []

    for t in items:
        text = display_text(t)
        parts, pattern = token_structure(text)
        structure = TokenStructure(parts=parts, pattern=pattern)
        cap = max(t.height, 1e-3)
        panel = panel_of.get(t.text_id)
        in_panel = panel is not None
        in_title = bool(panel and panel.panel_id in title_panels)

        ratio = parse_ratio(text)
        elevation = parse_elevation(text)
        if ratio is not None:
            scale_notes.append((text, ratio))
        if elevation is not None:
            elevation_notes.append((text, elevation, t.bbox))

        # A leader points at something *readable*.  An unresolved mark is not
        # a label, so geometry touching it is not a leader - without this a
        # pipe ending at a riser symbol would be re-read as an annotation.
        probe = t.bbox.expanded(LEADER_ATTACH_RATIO * cap)
        readable = t.state is not IdentityState.UNRESOLVED and len(t.text.strip()) >= 2
        has_leader = False
        leader_len = 0.0
        traced = (traced_leaders or {}).get(t.text_id)
        if traced is not None:
            has_leader = True
            leader_len = float(getattr(traced, "length", 0.0))
            for oid in getattr(traced, "object_ids", ()):  # keep them out of pipe detection
                attached_leaders.add(oid)
            attached_pairs.append((t.text_id, Segment(traced.polyline[0], traced.polyline[-1])))
        for li in (leader_index.query_box(probe) if readable and traced_leaders is None else ()):
            oid, s = leaders[li]
            for end, other in ((s.a, s.b), (s.b, s.a)):
                if probe.contains_point(end) and not t.bbox.expanded(-0.1).contains_point(other):
                    has_leader = True
                    leader_len = max(leader_len, s.length)
                    attached_leaders.add(oid)
                    attached_pairs.append((t.text_id, s))
        near_r = NEAR_GEOMETRY_RATIO * cap
        near_geometry = 0
        for oi in geom_index.query_box(t.bbox.expanded(near_r)):
            o = objects[oi]
            if o.bbox.area > page_box.area * 0.25:
                continue
            if any(point_segment_distance(t.bbox.center, s) <= near_r for s in o.segments()):
                near_geometry += 1

        # A reading that still contains an unresolved character is not a
        # designation, whatever its shape: publishing "L?.??S?8" as a pipe name
        # asserts something nobody wrote, and it competes with the correct
        # reading of the same pipe.
        unresolved_text = "\ufffd" in text
        code_like = _code_like(parts) and not unresolved_text
        letters_only = all(p[0] == "L" or not p[1].strip() for p in parts)
        digits_only = all(p[0] == "D" or not p[1].strip() for p in parts)
        explicit_d = _EXPLICIT_DIAMETER_RE.match(text.replace(" ", ""))
        repetition = occurrences.get(text, 1)

        scores: dict[TextRole, float] = {r: 0.0 for r in TextRole}
        scores[TextRole.IRRELEVANT] = 0.12
        if ratio is not None:
            scores[TextRole.SCALE_NOTE] = 0.97
        if elevation is not None:
            scores[TextRole.ELEVATION] = 0.95
        if explicit_d or (digits_only and parse_nominal_size(t.text.strip()) is not None and len(t.text.strip()) >= 2):
            scores[TextRole.DIMENSION] = 0.8 if explicit_d else 0.45
        if code_like:
            base = 0.45
            base += 0.22 if has_leader else 0.0
            base += 0.12 if near_geometry else 0.0
            base += 0.08 if repetition > 1 else 0.0
            base -= 0.30 if in_panel else 0.0
            scores[TextRole.PIPE_DESIGNATION] = max(0.0, min(0.99, base))
            legend_score = 0.30
            legend_score += 0.45 if in_panel and not in_title else 0.0
            legend_score += 0.10 if repetition > 1 else 0.0
            legend_score -= 0.25 if has_leader else 0.0
            scores[TextRole.LEGEND_ENTRY] = max(0.0, min(0.99, legend_score))
            if in_title:
                scores[TextRole.REFERENCE] = 0.55
        elif letters_only and len(text.strip()) >= 2:
            scores[TextRole.ROOM_LABEL] = 0.55 if not in_panel else 0.2
            if in_panel:
                scores[TextRole.TITLE_BLOCK if in_title else TextRole.LEGEND_ENTRY] = 0.6
        elif in_panel:
            scores[TextRole.TITLE_BLOCK if in_title else TextRole.LEGEND_ENTRY] = 0.5

        if t.text_id in attached_lower:
            # The lower half of a stacked label is not an independent item; its
            # value has already been folded into the code above it.
            scores = {r: 0.0 for r in TextRole}
            scores[TextRole.DIMENSION if t.text.strip().isdigit() else TextRole.ELEVATION] = 0.6

        if t.state is IdentityState.UNRESOLVED:
            scores = {r: 0.0 for r in TextRole}
            scores[TextRole.IRRELEVANT] = 0.9

        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0].value))
        role, role_score = ranked[0]
        role_scores = tuple((r.value, qs(v)) for r, v in ranked if v > 0.0)

        if code_like or explicit_d:
            diameter, diameter_reason, system_token = _dimension_from_structure(parts, explicit_d)
        else:
            diameter, diameter_reason, system_token = None, None, None

        reasons: list[Reason] = []
        if role is TextRole.LEGEND_ENTRY:
            reasons.append(Reason.LEGEND_INSTANCE)
        if diameter is None and role is TextRole.PIPE_DESIGNATION:
            reasons.append(diameter_reason or Reason.NO_DIMENSION_EVIDENCE)

        # Deliberately no CONFIRMED here.  Confirmation means a pipe accepted
        # this label, and no pipe has been looked at yet: that decision belongs
        # to vvs_pipe.designations.promote, after the geometry has spoken.  The
        # ceiling at this stage is HIGH_CONFIDENCE however well the string is
        # spelled.
        if role_score >= 0.5:
            state = IdentityState.HIGH_CONFIDENCE
        elif role_score > 0.0:
            state = IdentityState.AMBIGUOUS
        else:
            state = IdentityState.UNRESOLVED

        designations.append(
            Designation(
                designation_id=entity_id("des", (page, t.bbox.key(), text)),
                page=page,
                text=text,
                bbox=t.bbox,
                rotation=t.rotation,
                role=role,
                role_scores=role_scores,
                is_legend=role is TextRole.LEGEND_ENTRY,
                structure=structure,
                diameter_mm=diameter,
                diameter_reason=diameter_reason,
                system_token=system_token,
                text_item_id=t.text_id,
                glyph_ids=t.glyph_ids,
                source_object_ids=t.source_object_ids,
                confidence=Confidence(
                    text=t.confidence,
                    geometry=qs(min(1.0, 0.4 + 0.15 * near_geometry + (0.3 if has_leader else 0.0))),
                    dimension=None if diameter is None else 0.9,
                ),
                state=state,
                reasons=tuple(reasons),
                associated_physical_pipe_ids=(),
                tier=(
                    DesignationTier.DESIGNATION_CANDIDATE
                    if role is TextRole.PIPE_DESIGNATION and role is not TextRole.LEGEND_ENTRY
                    else DesignationTier.TEXT_ONLY
                ),
                provenance=Provenance(
                    stage="designation",
                    rule="open-world token structure + local geometry scoring",
                    inputs=(t.text_id,),
                    source_object_ids=t.source_object_ids,
                    notes=(
                        f"pattern={pattern}",
                        f"inPanel={in_panel}",
                        f"hasLeader={has_leader}",
                        f"nearGeometry={near_geometry}",
                        f"occurrences={repetition}",
                        f"stackedSize={size_below[t.text_id].value:g}" if t.text_id in size_below else "stackedSize=none",
                        f"stackedElevation={elevation_below[t.text_id].value:g}"
                        if t.text_id in elevation_below
                        else "stackedElevation=none",
                    ),
                ),
            )
        )

    designations = canonical_sort(designations, key=lambda d: d.canonical_key())
    return DesignationDiscovery(
        designations=tuple(designations),
        scale_notes=tuple(sorted(set(scale_notes))),
        elevation_notes=tuple(sorted(elevation_notes, key=lambda e: (e[2].key(), e[0]))),
        leaders=tuple(sorted(attached_pairs, key=lambda kv: (kv[1].key(), kv[0]))),
        leader_object_ids=frozenset(attached_leaders),
        stacked=stacked,
    )


def _detect_stacked_labels(items: Sequence[TextItem]) -> tuple[StackedLabel, ...]:
    """Pair each code-like line with the line written directly beneath it.

    Purely geometric and generic: the lower line must sit within a line-height
    below the upper one and overlap it horizontally, and must itself parse as
    either a bare nominal size or a level note.  Each line takes part in at most
    one pairing, chosen by proximity, so a column of labels cannot cross-link.
    """
    ordered = canonical_sort(list(items), key=lambda t: t.canonical_key())
    uppers = []
    for t in ordered:
        parts, _pattern = token_structure(t.text)
        if _code_like(parts):
            uppers.append(t)
    if not uppers:
        return ()

    index: SpatialIndex[int] = SpatialIndex.for_items(
        [(t.bbox, i) for i, t in enumerate(ordered)]
    )
    used_lower: set[str] = set()
    out: list[StackedLabel] = []
    for u in uppers:
        cap = max(u.height, 1e-3)
        window = BBox(
            u.bbox.x0 - cap,
            u.bbox.y1 - 0.3 * cap,
            u.bbox.x1 + cap,
            u.bbox.y1 + STACK_MAX_VERTICAL_GAP_RATIO * cap,
        )
        best: tuple[float, TextItem, str, float] | None = None
        for i in index.query_box(window):
            low = ordered[i]
            if low.text_id == u.text_id or low.text_id in used_lower:
                continue
            gap = low.bbox.y0 - u.bbox.y1
            if not (-0.3 * cap <= gap <= STACK_MAX_VERTICAL_GAP_RATIO * cap):
                continue
            overlap = min(u.bbox.x1, low.bbox.x1) - max(u.bbox.x0, low.bbox.x0)
            narrower = min(u.bbox.width, low.bbox.width)
            if narrower <= 0 or overlap / narrower < STACK_MIN_HORIZONTAL_OVERLAP:
                continue
            token = low.text.strip()
            elevation = parse_elevation(token)
            if elevation is not None:
                kind, value = "elevation", elevation
            else:
                size = parse_nominal_size(token)
                if size is None:
                    continue
                kind, value = "size", size
            score = abs(gap)
            if best is None or score < best[0]:
                best = (score, low, kind, value)
        if best is None:
            continue
        _score, low, kind, value = best
        used_lower.add(low.text_id)
        out.append(
            StackedLabel(
                upper_text_id=u.text_id, lower_text_id=low.text_id, kind=kind, value=value
            )
        )
    return tuple(sorted(out, key=lambda st: (st.upper_text_id, st.lower_text_id)))


def _dimension_from_structure(
    parts: Sequence[tuple[str, str]], explicit
) -> tuple[float | None, Reason | None, str | None]:
    """Derive a nominal size from the token structure, generically.

    The rule is positional, not lexical: the *last* purely numeric run of a
    code-like string is the nominal size when its value is physically
    plausible.  ``S1-P2-110`` yields 110, ``KV1-X7`` yields nothing (7 is below
    any plausible nominal size), ``ABC-17-X-250`` yields 250.  No token value
    is special-cased anywhere.
    """
    if explicit:
        return float(explicit.group(1)), None, None
    runs = [p for p in parts if p[0] in ("L", "D")]
    if not runs:
        return None, Reason.NO_DIMENSION_EVIDENCE, None
    system = runs[0][1] if runs[0][0] == "L" or any(c.isalpha() for c in runs[0][1]) else None
    numeric_runs = [p[1] for p in runs if p[0] == "D"]
    if not numeric_runs:
        return None, Reason.NO_DIMENSION_EVIDENCE, system
    size = parse_nominal_size(numeric_runs[-1])
    if size is None:
        return None, Reason.NO_DIMENSION_EVIDENCE, system
    return size, None, system
