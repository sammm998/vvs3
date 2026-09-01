"""Glyphs -> characters -> strings, plus generic token structure.

Two text sources are merged here:

* glyph groups recovered from vector geometry (the primary source - CAD
  exports routinely have no text layer at all);
* native PDF text spans, when the producer left one.

Where both describe the same place on the sheet the native span wins for the
*string* and the glyph evidence is kept as corroboration, because a real text
layer is exact.  Where they disagree the item is flagged; where only glyphs
exist the reconstruction stands on its own.

``token_structure`` decomposes a string into LETTER / DIGIT / SEPARATOR / OTHER
runs.  It has no notion of which codes exist - it only reports the shape of the
string, which is what the open-world designation scoring consumes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from ..canonical import canonical_sort, entity_id, qs
from ..geometry.primitives import BBox
from ..glyph.alphabet import AlphabetAssignment, GlyphObservation, resolve_alphabet
from ..glyph.candidates import GlyphSegmentation, TextLine
from ..glyph.prototypes import Prototype
from ..glyph.features import glyph_features, rasterise_polylines
from ..model import GlyphCandidate, Provenance, TextItem, TextSpan
from ..states import IdentityState, Reason

TOKEN_CLASSES = ("L", "D", "S", "O")
SPACE_GAP_FACTOR = 2.2   # of the line's median inter-glyph gap
SPACE_GAP_CAP_RATIO = 0.45  # floor, as a fraction of cap height
SPAN_MERGE_IOU = 0.35


@dataclass(frozen=True, slots=True)
class ReconstructedText:
    glyphs: tuple[GlyphCandidate, ...]
    items: tuple[TextItem, ...]
    consumed_object_ids: frozenset[str]


def token_structure(text: str) -> tuple[tuple[tuple[str, str], ...], str]:
    """Split a string into class runs. Returns (parts, pattern)."""

    def cls(ch: str) -> str:
        if ch.isalpha():
            return "L"
        if ch.isdigit():
            return "D"
        if ch in "-/._:+ ":
            return "S"
        return "O"

    parts: list[tuple[str, str]] = []
    for ch in text:
        c = cls(ch)
        if parts and parts[-1][0] == c:
            parts[-1] = (c, parts[-1][1] + ch)
        else:
            parts.append((c, ch))
    pattern = "".join(f"{c}{len(v)}" for c, v in parts)
    return tuple(parts), pattern


def _bbox_iou(a: BBox, b: BBox) -> float:
    inter = a.intersection_area(b)
    union = a.area + b.area - inter
    return inter / union if union > 0 else 0.0


def _observation(line: TextLine, glyph) -> GlyphObservation:
    return GlyphObservation(
        key=(glyph.bbox.key(), glyph.object_ids),
        raster=rasterise_polylines(glyph.polylines, glyph.filled),
        rel_height=glyph.bbox.height / max(line.cap_height, 1e-6),
        rel_base=(line.baseline_y - glyph.bbox.y1) / max(line.cap_height, 1e-6),
    )


def _line_string(
    line: TextLine, assignment: AlphabetAssignment
) -> tuple[str, list[str | None], float]:
    """Assemble the line's string, inserting spaces at wide inter-glyph gaps."""
    theta = math.radians(line.rotation_deg)
    ux, uy = math.cos(theta), math.sin(theta)
    ordered = sorted(
        line.glyphs,
        key=lambda g: (
            qs(g.bbox.center[0] * ux + g.bbox.center[1] * uy),
            g.bbox.key(),
        ),
    )
    extents: list[tuple[float, float]] = []
    for g in ordered:
        corners = [
            (g.bbox.x0, g.bbox.y0),
            (g.bbox.x1, g.bbox.y0),
            (g.bbox.x0, g.bbox.y1),
            (g.bbox.x1, g.bbox.y1),
        ]
        proj = [p[0] * ux + p[1] * uy for p in corners]
        extents.append((min(proj), max(proj)))

    # A word gap is judged against the line's own typical inter-glyph gap, not
    # against a fixed fraction of the cap height: narrow characters such as
    # "." and ":" leave a wide *ink* gap without any space being present.
    gaps = sorted(max(0.0, extents[i + 1][0] - extents[i][1]) for i in range(len(extents) - 1))
    median_gap = gaps[len(gaps) // 2] if gaps else 0.0
    threshold = max(SPACE_GAP_FACTOR * median_gap, SPACE_GAP_CAP_RATIO * line.cap_height)

    chars: list[str | None] = []
    out: list[str] = []
    confidences: list[float] = []
    prev_hi: float | None = None
    for g, (lo, hi) in zip(ordered, extents):
        key = (g.bbox.key(), g.object_ids)
        ch = assignment.character(key)
        confidences.append(assignment.confidence(key))
        if prev_hi is not None and lo - prev_hi > threshold:
            out.append(" ")
            chars.append(" ")
        out.append(ch if ch else "�")
        chars.append(ch)
        prev_hi = hi
    conf = min(confidences) if confidences else 0.0
    return "".join(out), chars, conf


def reconstruct_text(
    segmentation: GlyphSegmentation,
    spans: Sequence[TextSpan],
    page: int,
    bank: Sequence[Prototype] | None = None,
) -> ReconstructedText:
    multi: list[GlyphObservation] = []
    single: list[GlyphObservation] = []
    for line in segmentation.lines:
        target = multi if len(line.glyphs) >= 2 else single
        for g in line.glyphs:
            target.append(_observation(line, g))
    assignment = resolve_alphabet(multi, single, bank)

    glyph_candidates: list[GlyphCandidate] = []
    items: list[TextItem] = []
    consumed: set[str] = set()

    for line in segmentation.lines:
        text, chars, conf = _line_string(line, assignment)
        line_glyph_ids: list[str] = []
        for g in line.glyphs:
            key = (g.bbox.key(), g.object_ids)
            ch = assignment.character(key)
            gc_conf = assignment.confidence(key)
            raster = rasterise_polylines(g.polylines, g.filled)
            feats = glyph_features(raster)
            reasons: tuple[Reason, ...] = ()
            state = IdentityState.CONFIRMED
            if ch is None:
                state = IdentityState.UNRESOLVED
                reasons = (Reason.UNRESOLVED_GLYPH,)
            elif gc_conf < 0.55:
                state = IdentityState.AMBIGUOUS
                reasons = (Reason.LOW_GLYPH_MARGIN,)
            elif gc_conf < 0.8:
                state = IdentityState.HIGH_CONFIDENCE
            gid = entity_id("gly", (page, g.bbox.key(), g.object_ids))
            line_glyph_ids.append(gid)
            glyph_candidates.append(
                GlyphCandidate(
                    glyph_id=gid,
                    page=page,
                    bbox=g.bbox,
                    source_object_ids=g.object_ids,
                    stroke_count=len(g.polylines),
                    holes=int(feats["holes"]),
                    aspect=feats["aspect"],
                    complexity=feats["complexity"],
                    contour_signature=raster.signature(),
                    character=ch,
                    alternatives=assignment.ranked(key),
                    confidence=gc_conf,
                    state=state,
                    reasons=reasons,
                    provenance=Provenance(
                        stage="glyph",
                        rule="shape-cluster + exclusive alphabet assignment",
                        source_object_ids=g.object_ids,
                        notes=(f"clusterSize={assignment.cluster_size(key)}",),
                    ),
                )
            )

        object_ids = tuple(sorted({oid for g in line.glyphs for oid in g.object_ids}))
        unresolved = sum(1 for c in chars if c is None)
        if unresolved == 0:
            state = IdentityState.CONFIRMED if conf >= 0.8 else IdentityState.HIGH_CONFIDENCE
            reasons = ()
        elif unresolved == len(line.glyphs):
            state = IdentityState.UNRESOLVED
            reasons = (Reason.UNRESOLVED_GLYPH,)
        else:
            state = IdentityState.AMBIGUOUS
            reasons = (Reason.UNRESOLVED_GLYPH,)
        tid = entity_id("txt", (page, line.bbox.key(), text))
        items.append(
            TextItem(
                text_id=tid,
                page=page,
                text=text,
                bbox=line.bbox,
                rotation=line.rotation_deg,
                height=line.cap_height,
                origin="glyph",
                glyph_ids=tuple(line_glyph_ids),
                source_object_ids=object_ids,
                confidence=conf,
                state=state,
                reasons=reasons,
                provenance=Provenance(
                    stage="text",
                    rule="projection-profile segmentation + alphabet assignment",
                    source_object_ids=object_ids,
                    notes=(f"glyphs={len(line.glyphs)}", f"unresolved={unresolved}"),
                ),
            )
        )
        # Only a multi-glyph, fully resolved run of text is allowed to *consume*
        # its geometry.  A single mark stays available to the pipe stages, so a
        # short stub is never swallowed by a mis-read character.
        if len(line.glyphs) >= 2 and unresolved < len(line.glyphs):
            consumed.update(object_ids)

    # Native text spans, when present, are authoritative for their own strings.
    # Where a span covers the same place as a reconstructed line - the common
    # "searchable CAD PDF" pattern, where the same text exists both as vector
    # outlines and as invisible text - the two are merged into one item so the
    # sheet is not counted twice, and the agreement is recorded.
    for sp in spans:
        overlapping = [
            it
            for it in items
            if it.origin == "glyph"
            and it.page == sp.page
            and _bbox_iou(it.bbox, sp.bbox) >= SPAN_MERGE_IOU
        ]
        overlapping = canonical_sort(overlapping, key=lambda t: t.canonical_key())
        glyph_ids: tuple[str, ...] = ()
        obj_ids: tuple[str, ...] = ()
        notes: list[str] = []
        if overlapping:
            merged = overlapping[0]
            glyph_ids = merged.glyph_ids
            obj_ids = merged.source_object_ids
            agree = merged.text.replace(" ", "") == sp.text.replace(" ", "")
            notes.append("glyphAgreement=" + ("yes" if agree else "no"))
            notes.append(f"glyphText={merged.text!r}")
            items = [it for it in items if it is not merged]
        tid = entity_id("txt", (sp.page, sp.bbox.key(), sp.text))
        items.append(
            TextItem(
                text_id=tid,
                page=sp.page,
                text=sp.text,
                bbox=sp.bbox,
                rotation=sp.rotation,
                height=sp.size,
                origin="span+glyph" if overlapping else "span",
                glyph_ids=glyph_ids,
                source_object_ids=obj_ids,
                confidence=1.0,
                state=IdentityState.CONFIRMED,
                reasons=(),
                provenance=Provenance(
                    stage="text",
                    rule="native PDF text span" + (" merged with reconstructed glyphs" if overlapping else ""),
                    inputs=(sp.span_id,),
                    source_object_ids=obj_ids,
                    notes=tuple(notes),
                ),
            )
        )

    glyph_candidates = canonical_sort(glyph_candidates, key=lambda g: g.canonical_key())
    items = canonical_sort(items, key=lambda t: t.canonical_key())
    return ReconstructedText(tuple(glyph_candidates), tuple(items), frozenset(consumed))
