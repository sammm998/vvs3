"""Drawing-role classification: what every piece of geometry *is*.

This stage runs before anything reads a character.  Its purpose is to make pipe
detection independent of the text stages: today a pipe is whatever geometry the
glyph stage did not claim, which means a drawing whose lettering reconstructs
badly also loses its pipework.  After this stage a pipe is geometry that looks
like pipework, and text is geometry that looks like lettering, and the two
judgements are made separately from the same evidence.

The unit of classification is the *layer* first and the object second.  A CAD
file already groups geometry by purpose - that is what a layer is - so the
layer's aggregate signature is far better evidence than any single stroke.  The
layer *names* are never interpreted: no rule in this module looks at the text of
a layer name, because a name is drawing-specific and the next office numbers its
layers differently.  What is measured is the shape of what the layer contains:
how long its strokes are relative to the sheet, how many are closed, whether it
carries one dominant dash pattern, whether its strokes chain into a network or
sit as isolated islands, how its extents compare with the sheet's lettering.

An object whose own geometry contradicts its layer keeps its own verdict; a
layer with no clear signature leaves its members UNKNOWN.  UNKNOWN is a real
answer here and is reported as such - it is never quietly folded into PIPE.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .canonical import canonical_sort, qs
from .geometry.index import SpatialIndex, connected_components
from .geometry.primitives import BBox
from .model import VectorObject
from .states import DrawingRole

# ---------------------------------------------------------------------------
# Thresholds.  Every one of these is a ratio against something the drawing
# itself supplies (its own cap height, its own page, its own object count), so
# none of them encodes a fact about any particular sheet.

TEXT_MAX_EXTENT_CAPS = 1.6        # a character is about one cap height across
SYMBOL_MAX_EXTENT_CAPS = 6.0      # riser circles, valve marks
GRID_MIN_SPAN_RATIO = 0.55        # of the drawn extent, for a building grid line
GRID_MIN_FAMILY = 3               # a grid is a set of lines, never a single one
LONG_STROKE_CAPS = 6.0            # "long" for the purpose of network statistics
HATCH_MIN_MEMBERS = 12            # a hatch is many parallel strokes, not three
HATCH_ANGLE_TOLERANCE_RAD = math.radians(4.0)
HATCH_SPACING_VARIATION = 0.25    # of the mean spacing
PANEL_MIN_AREA_RATIO = 0.004      # of the page, for a framed panel
PANEL_MAX_AREA_RATIO = 0.35
PANEL_DENSITY_MULTIPLE = 3.0      # of the sheet's mean object density
DOMINANT_FRACTION = 0.70          # "most of this layer does X"
NETWORK_JOIN_CAPS = 0.25          # endpoint join tolerance, in cap heights
# A layer is evidence because it *divides* the drawing by purpose.  A layer
# holding almost everything divides nothing, so its signature describes the
# sheet rather than its members and must not speak for any of them.  A drawing
# exported without layer information lands here as one undifferentiated group,
# and every object in it falls back to its own geometry.
MAX_LAYER_SHARE_TO_SPEAK = 0.60


@dataclass(frozen=True, slots=True)
class LayerSignature:
    """Aggregate geometry of one layer, with no reference to its name."""

    layer: str
    objects: int
    closed_fraction: float
    filled_fraction: float
    median_extent_caps: float
    long_fraction: float
    dashed_fraction: float
    dominant_dash: str | None
    chain_fraction: float
    span_ratio: float
    role: DrawingRole
    role_confidence: float
    evidence: tuple[tuple[str, float], ...]

    def to_canonical(self) -> dict:
        return {
            "layer": self.layer,
            "objects": self.objects,
            "closedFraction": qs(self.closed_fraction),
            "filledFraction": qs(self.filled_fraction),
            "medianExtentCaps": qs(self.median_extent_caps),
            "longFraction": qs(self.long_fraction),
            "dashedFraction": qs(self.dashed_fraction),
            "dominantDash": self.dominant_dash,
            "chainFraction": qs(self.chain_fraction),
            "spanRatio": qs(self.span_ratio),
            "role": self.role.value,
            "roleConfidence": qs(self.role_confidence),
            "evidence": [[k, qs(v)] for k, v in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class RoleAssignment:
    object_id: str
    role: DrawingRole
    confidence: float
    source: str  # "object" | "layer" | "default"
    evidence: tuple[tuple[str, float], ...]

    def to_canonical(self) -> dict:
        return {
            "objectId": self.object_id,
            "role": self.role.value,
            "confidence": qs(self.confidence),
            "source": self.source,
            "evidence": [[k, qs(v)] for k, v in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class RoleClassification:
    assignments: tuple[RoleAssignment, ...]
    layers: tuple[LayerSignature, ...]
    panels: tuple[BBox, ...]
    by_object: Mapping[str, DrawingRole]

    def objects_with(self, *roles: DrawingRole) -> frozenset[str]:
        wanted = set(roles)
        return frozenset(a.object_id for a in self.assignments if a.role in wanted)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {r.value: 0 for r in DrawingRole}
        for a in self.assignments:
            out[a.role.value] += 1
        return out

    def to_canonical(self) -> dict:
        return {
            "counts": self.counts(),
            "layers": [s.to_canonical() for s in self.layers],
            "panels": [b.to_canonical() for b in self.panels],
        }


# ---------------------------------------------------------------------------


def classify_roles(
    objects: Sequence[VectorObject],
    page_box: BBox,
    cap_height: float,
    text_object_ids: frozenset[str] = frozenset(),
) -> RoleClassification:
    """Assign a drawing role to every object.

    ``text_object_ids`` is the set the geometric grouping stage judged to be
    lettering.  That judgement is itself made without reading anything - it is
    clustering by size and alignment - so passing it in does not make this stage
    depend on the text having been *understood*.
    """
    if not objects:
        return RoleClassification((), (), (), {})

    cap = max(cap_height, 0.5)
    drawn = BBox.union_all([o.bbox for o in objects])
    drawn_span = max(drawn.width, drawn.height, 1e-6)

    panels = _framed_panels(objects, page_box)
    panel_index: SpatialIndex[int] = SpatialIndex.for_items(
        [(b, i) for i, b in enumerate(panels)]
    )
    grid_ids = _grid_lines(objects, drawn_span)
    hatch_ids = _hatch_groups(objects, cap)
    layers = _layer_signatures(objects, cap, drawn_span, text_object_ids)
    layer_role = {s.layer: (s.role, s.role_confidence) for s in layers}

    assignments: list[RoleAssignment] = []
    for o in canonical_sort(list(objects), key=lambda x: x.canonical_key()):
        role, conf, source, evidence = _object_role(
            o,
            cap=cap,
            drawn_span=drawn_span,
            text_object_ids=text_object_ids,
            grid_ids=grid_ids,
            hatch_ids=hatch_ids,
            panels=panels,
            panel_index=panel_index,
            layer_role=layer_role,
        )
        assignments.append(
            RoleAssignment(
                object_id=o.object_id,
                role=role,
                confidence=qs(conf),
                source=source,
                evidence=tuple(sorted(evidence)),
            )
        )

    assignments = canonical_sort(assignments, key=lambda a: (a.object_id,))
    return RoleClassification(
        assignments=tuple(assignments),
        layers=tuple(layers),
        panels=tuple(panels),
        by_object={a.object_id: a.role for a in assignments},
    )


def _object_role(
    o: VectorObject,
    *,
    cap: float,
    drawn_span: float,
    text_object_ids: frozenset[str],
    grid_ids: frozenset[str],
    hatch_ids: frozenset[str],
    panels: Sequence[BBox],
    panel_index: SpatialIndex[int],
    layer_role: Mapping[str, tuple[DrawingRole, float]],
) -> tuple[DrawingRole, float, str, list[tuple[str, float]]]:
    """Per-object verdict, falling back to the layer and then to UNKNOWN.

    The order is deliberate: a judgement made from this object's own geometry is
    stronger evidence than one inherited from the company it keeps, so the
    object-level tests are tried first and only what they leave undecided is
    handed to the layer.
    """
    extent = max(o.bbox.width, o.bbox.height)
    caps = extent / cap
    ev: list[tuple[str, float]] = [("extentCaps", qs(caps))]

    if o.object_id in text_object_ids:
        return DrawingRole.TEXT, 0.95, "object", ev + [("clusteredAsLettering", 1.0)]
    if o.object_id in grid_ids:
        return DrawingRole.GRID, 0.85, "object", ev + [("spansDrawing", qs(extent / drawn_span))]
    if o.object_id in hatch_ids:
        return DrawingRole.HATCH, 0.8, "object", ev + [("parallelGroupMember", 1.0)]

    # A framed panel's contents are the panel's, whatever their own shape.  The
    # panel is found by its frame and its density, never by where it sits.
    for pi in panel_index.query_box(o.bbox):
        if panels[pi].contains_box(o.bbox):
            return DrawingRole.TITLE_BLOCK, 0.7, "object", ev + [("insideFramedPanel", 1.0)]

    if o.closed and caps <= SYMBOL_MAX_EXTENT_CAPS:
        return DrawingRole.SYMBOL, 0.7, "object", ev + [("smallClosedContour", 1.0)]

    role, conf = layer_role.get(o.layer or "", (DrawingRole.UNKNOWN, 0.0))
    if role is not DrawingRole.UNKNOWN:
        return role, conf, "layer", ev + [("layerSignature", qs(conf))]
    return DrawingRole.UNKNOWN, 0.0, "default", ev


# ---------------------------------------------------------------------------
# Layer signatures


def _layer_signatures(
    objects: Sequence[VectorObject],
    cap: float,
    drawn_span: float,
    text_object_ids: frozenset[str],
) -> list[LayerSignature]:
    by_layer: dict[str, list[VectorObject]] = {}
    for o in objects:
        by_layer.setdefault(o.layer or "", []).append(o)

    total = len(objects)
    out: list[LayerSignature] = []
    for layer in sorted(by_layer):
        members = canonical_sort(by_layer[layer], key=lambda x: x.canonical_key())
        n = len(members)
        extents = sorted(max(m.bbox.width, m.bbox.height) for m in members)
        median_extent = extents[n // 2]
        closed_fraction = sum(1.0 for m in members if m.closed) / n
        filled_fraction = sum(1.0 for m in members if m.is_filled) / n
        long_fraction = sum(1.0 for e in extents if e >= LONG_STROKE_CAPS * cap) / n
        text_fraction = sum(1.0 for m in members if m.object_id in text_object_ids) / n

        dashes = [m.dashes for m in members if m.dashes]
        dominant_dash: str | None = None
        if dashes:
            counts: dict[str, int] = {}
            for d in dashes:
                counts[d] = counts.get(d, 0) + 1
            top = max(sorted(counts), key=lambda k: (counts[k], k))
            if counts[top] >= DOMINANT_FRACTION * n:
                dominant_dash = top
        dashed_fraction = len(dashes) / n

        chain_fraction = _chain_fraction(members, cap)
        span = BBox.union_all([m.bbox for m in members])
        span_ratio = max(span.width, span.height) / drawn_span

        share = n / total
        role, conf, ev = _layer_role(
            share=share,
            objects=n,
            closed_fraction=closed_fraction,
            filled_fraction=filled_fraction,
            median_extent_caps=median_extent / cap,
            long_fraction=long_fraction,
            text_fraction=text_fraction,
            dashed_fraction=dashed_fraction,
            dominant_dash=dominant_dash,
            chain_fraction=chain_fraction,
            span_ratio=span_ratio,
        )
        out.append(
            LayerSignature(
                layer=layer,
                objects=n,
                closed_fraction=closed_fraction,
                filled_fraction=filled_fraction,
                median_extent_caps=median_extent / cap,
                long_fraction=long_fraction,
                dashed_fraction=dashed_fraction,
                dominant_dash=dominant_dash,
                chain_fraction=chain_fraction,
                span_ratio=span_ratio,
                role=role,
                role_confidence=conf,
                evidence=tuple(sorted(ev)),
            )
        )
    return out


def _layer_role(
    *,
    share: float,
    objects: int,
    closed_fraction: float,
    filled_fraction: float,
    median_extent_caps: float,
    long_fraction: float,
    text_fraction: float,
    dashed_fraction: float,
    dominant_dash: str | None,
    chain_fraction: float,
    span_ratio: float,
) -> tuple[DrawingRole, float, list[tuple[str, float]]]:
    """Score a layer's aggregate signature against each role it could hold.

    Scores are evidence sums, not tuned weights: each term is a property that
    genuinely distinguishes the role, and the winner must beat the runner-up by
    a clear margin or the layer stays UNKNOWN and its members fall back to their
    own geometry.  Refusing to decide is a supported outcome.
    """
    ev: list[tuple[str, float]] = [
        ("objects", float(objects)),
        ("shareOfDrawing", qs(share)),
        ("closedFraction", qs(closed_fraction)),
        ("medianExtentCaps", qs(median_extent_caps)),
        ("longFraction", qs(long_fraction)),
        ("chainFraction", qs(chain_fraction)),
        ("spanRatio", qs(span_ratio)),
        ("textFraction", qs(text_fraction)),
    ]
    if share > MAX_LAYER_SHARE_TO_SPEAK:
        return DrawingRole.UNKNOWN, 0.0, ev

    scores: dict[DrawingRole, float] = {}

    # Lettering: many small objects, hardly any of them long or closed, and the
    # grouping stage already claimed most of them.
    scores[DrawingRole.TEXT] = (
        0.6 * text_fraction
        + 0.2 * (1.0 if median_extent_caps <= TEXT_MAX_EXTENT_CAPS else 0.0)
        + 0.2 * (1.0 - long_fraction)
    )

    # Pipework: long strokes that chain end to end into a network, spanning much
    # of the drawing, rarely closed.  A single dominant dash pattern is the
    # signature of a linetype, which is how concealed pipework is drawn.
    scores[DrawingRole.PIPE] = (
        0.35 * chain_fraction
        + 0.25 * long_fraction
        + 0.20 * min(1.0, span_ratio)
        + 0.20 * (1.0 if dominant_dash else 0.0)
        - 0.40 * closed_fraction
        - 0.40 * text_fraction
    )

    # Building fabric: long strokes too, but closing into loops and not forming
    # an open network; walls bound rooms, pipes traverse them.
    scores[DrawingRole.WALL] = (
        0.40 * closed_fraction
        + 0.25 * long_fraction
        + 0.20 * min(1.0, span_ratio)
        - 0.35 * chain_fraction
        - 0.40 * text_fraction
    )

    # Repeated small closed marks that neither chain nor span.
    scores[DrawingRole.SYMBOL] = (
        0.45 * closed_fraction
        + 0.30 * (1.0 if median_extent_caps <= SYMBOL_MAX_EXTENT_CAPS else 0.0)
        + 0.25 * (1.0 - min(1.0, span_ratio))
        - 0.40 * long_fraction
        - 0.30 * text_fraction
    )

    # Solid area fill: almost everything filled, nothing long.
    scores[DrawingRole.HATCH] = 0.7 * filled_fraction + 0.3 * (1.0 - long_fraction)

    # Long dashed strokes that do *not* chain are centre lines and section
    # marks, not pipework - the distinction is connectivity, not appearance.
    scores[DrawingRole.REFERENCE_LINE] = (
        0.40 * dashed_fraction
        + 0.30 * long_fraction
        + 0.30 * (1.0 - chain_fraction)
        - 0.40 * closed_fraction
        - 0.30 * text_fraction
    )

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0].value))
    best, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = best_score - runner_up
    ev.append(("bestScore", qs(best_score)))
    ev.append(("margin", qs(margin)))
    if best_score < 0.45 or margin < 0.10:
        return DrawingRole.UNKNOWN, 0.0, ev
    return best, min(0.95, best_score), ev


def _chain_fraction(members: Sequence[VectorObject], cap: float) -> float:
    """Fraction of a layer's strokes that join another end to end.

    This is what separates a network from a set of independent marks, and it is
    the single most useful discriminator between pipework and everything else
    that is also long and thin.
    """
    ends: list[tuple[str, tuple[float, float]]] = []
    for m in members:
        if m.closed or len(m.points) < 2:
            continue
        ends.append((m.object_id, m.points[0]))
        ends.append((m.object_id, m.points[-1]))
    if not ends:
        return 0.0
    tol = max(NETWORK_JOIN_CAPS * cap, 0.1)
    buckets: dict[tuple[int, int], list[str]] = {}
    for oid, p in ends:
        cell = (int(math.floor(p[0] / tol)), int(math.floor(p[1] / tol)))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                buckets.setdefault((cell[0] + dx, cell[1] + dy), []).append(oid)
    joined: set[str] = set()
    for cell in sorted(buckets):
        owners = set(buckets[cell])
        if len(owners) > 1:
            joined |= owners
    distinct = {m.object_id for m in members if not m.closed and len(m.points) >= 2}
    return len(joined) / len(distinct) if distinct else 0.0


# ---------------------------------------------------------------------------
# Object-level detectors


def _framed_panels(objects: Sequence[VectorObject], page_box: BBox) -> list[BBox]:
    """Closed rectangles of panel size that hold far more geometry than average.

    A title block and a legend are both a frame with dense contents; which of
    the two it is depends on what the frame contains, and that is a question for
    a later stage that can read.  Here they are one role, so that neither is
    mistaken for drawing content.
    """
    density = len(objects) / max(page_box.area, 1.0)
    index: SpatialIndex[int] = SpatialIndex.for_items(
        [(o.bbox, i) for i, o in enumerate(objects)]
    )
    out: list[BBox] = []
    for o in canonical_sort(list(objects), key=lambda x: x.canonical_key()):
        if not o.closed or len(o.points) not in (4, 5):
            continue
        area = o.bbox.area
        if not (PANEL_MIN_AREA_RATIO * page_box.area <= area <= PANEL_MAX_AREA_RATIO * page_box.area):
            continue
        inside = sum(
            1 for i in index.query_box(o.bbox) if o.bbox.contains_box(objects[i].bbox)
        )
        if inside >= PANEL_DENSITY_MULTIPLE * density * area and inside >= 8:
            out.append(o.bbox)
    return canonical_sort(_merge_overlapping(out), key=lambda b: b.key())


def _merge_overlapping(boxes: Sequence[BBox]) -> list[BBox]:
    if not boxes:
        return []
    ordered = canonical_sort(list(boxes), key=lambda b: b.key())
    edges: list[tuple[int, int]] = []
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            if ordered[i].intersects(ordered[j]):
                edges.append((i, j))
    comps = connected_components(len(ordered), edges)
    return [BBox.union_all([ordered[i] for i in comp]) for comp in comps]


def _grid_lines(objects: Sequence[VectorObject], drawn_span: float) -> frozenset[str]:
    """Families of long axis-aligned strokes running across the whole drawing.

    Span and alignment alone are not enough: on a schematic, a single long
    straight pipe spans most of the sheet too, and treating it as a grid line
    deletes it from the take-off.  A building grid is never one line - it is a
    set of them, in a shared direction - so a family is required, and a lone
    long stroke is left to be judged on other evidence.
    """
    by_axis: dict[str, list[str]] = {"x": [], "y": []}
    for o in objects:
        if o.closed or len(o.points) != 2:
            continue
        (x0, y0), (x1, y1) = o.points
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        span = max(dx, dy)
        if span < GRID_MIN_SPAN_RATIO * drawn_span:
            continue
        if min(dx, dy) > 0.02 * span:  # axis-aligned to within about a degree
            continue
        by_axis["x" if dx >= dy else "y"].append(o.object_id)

    out: set[str] = set()
    for axis in sorted(by_axis):
        members = by_axis[axis]
        if len(members) >= GRID_MIN_FAMILY:
            out |= set(members)
    return frozenset(out)


def _hatch_groups(objects: Sequence[VectorObject], cap: float) -> frozenset[str]:
    """Runs of many equally spaced parallel strokes sharing one angle.

    Hatching is generated, so its regularity is far tighter than anything drawn
    by hand: that regularity, not the appearance of the strokes, is what is
    tested here.
    """
    by_angle: dict[int, list[VectorObject]] = {}
    for o in objects:
        if o.closed or len(o.points) != 2:
            continue
        (x0, y0), (x1, y1) = o.points
        length = math.hypot(x1 - x0, y1 - y0)
        if length <= 0 or length > LONG_STROKE_CAPS * cap:
            continue
        angle = math.atan2(y1 - y0, x1 - x0) % math.pi
        bucket = int(angle / HATCH_ANGLE_TOLERANCE_RAD)
        by_angle.setdefault(bucket, []).append(o)

    out: set[str] = set()
    for bucket in sorted(by_angle):
        members = canonical_sort(by_angle[bucket], key=lambda x: x.canonical_key())
        if len(members) < HATCH_MIN_MEMBERS:
            continue
        angle = bucket * HATCH_ANGLE_TOLERANCE_RAD
        nx, ny = -math.sin(angle), math.cos(angle)
        offsets = sorted((nx * o.bbox.center[0] + ny * o.bbox.center[1], o.object_id) for o in members)
        gaps = [b[0] - a[0] for a, b in zip(offsets, offsets[1:]) if b[0] - a[0] > 1e-6]
        if len(gaps) < HATCH_MIN_MEMBERS - 1:
            continue
        mean = sum(gaps) / len(gaps)
        if mean <= 0:
            continue
        variation = math.sqrt(sum((g - mean) ** 2 for g in gaps) / len(gaps)) / mean
        if variation <= HATCH_SPACING_VARIATION:
            out |= {oid for _off, oid in offsets}
    return frozenset(out)


# Roles that are definitely not pipework.  TEXT is deliberately absent: the
# layer-level TEXT verdict is unreliable on a sheet where labels and pipework
# share a layer, and lettering is already removed per object by the text stages.
# SYMBOL is absent because detection has its own, better-targeted rule for it,
# and UNKNOWN is absent because not knowing is not a reason to delete geometry.
NOT_PIPEWORK = frozenset(
    {
        DrawingRole.WALL,
        DrawingRole.GRID,
        DrawingRole.HATCH,
        DrawingRole.REFERENCE_LINE,
        DrawingRole.TITLE_BLOCK,
        DrawingRole.LEGEND,
    }
)

# A verdict inherited from a layer removes geometry only when the layer's
# signature was unambiguous.  A weak verdict leaves the object in play: a pipe
# wrongly dropped is invisible in the output, while a wall wrongly kept is
# visible as unnamed geometry and can be argued with.
MIN_LAYER_CONFIDENCE_TO_EXCLUDE = 0.75


def non_pipe_objects(classification: RoleClassification) -> frozenset[str]:
    """Objects this stage is confident are not pipework.

    Used to keep building fabric out of pipe detection.  On a real sheet the
    architectural wall layer is drawn as long strokes that never join end to
    end, which is the opposite of a pipe network and is why the distinction
    survives without reading a single layer name.
    """
    out: set[str] = set()
    for a in classification.assignments:
        if a.role not in NOT_PIPEWORK:
            continue
        if a.source == "layer" and a.confidence < MIN_LAYER_CONFIDENCE_TO_EXCLUDE:
            continue
        out.add(a.object_id)
    return frozenset(out)


def role_coverage(classification: RoleClassification) -> dict[str, float]:
    """What fraction of the geometry the classifier could actually place."""
    total = len(classification.assignments) or 1
    unknown = sum(1 for a in classification.assignments if a.role is DrawingRole.UNKNOWN)
    return {
        "classified": qs((total - unknown) / total),
        "unknown": qs(unknown / total),
        "objects": float(total),
    }


def iter_roles(classification: RoleClassification) -> Iterable[tuple[str, DrawingRole]]:
    for a in classification.assignments:
        yield a.object_id, a.role
