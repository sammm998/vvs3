"""Fourth search: glyphs become text, by position.

The PDF may hand over a string, a set of characters in an arbitrary order, or
nothing at all.  None of those is trusted for *order*: a text item is built by
projecting its glyphs onto the baseline direction they share and reading them
along it.  Word breaks come from the gap distribution of the line itself, so a
tightly set CAD font and a loosely set one both split in the right places.

Where a real text layer exists its characters are used as the reading and the
reconstruction is used as the check; where the lettering is geometry, the
reading comes from the glyph bank and every item keeps the alternative strings
that its uncertain characters allow.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from .canonical import canonical_json, entity_id, q, qa, qbbox, sort_canonical
from .model import Glyph, PdfObject, TextItem
from .spatial_index import SpatialIndex, bbox_distance

MAX_STRING_ALTERNATIVES = 8


def _rot(point: Sequence[float], angle_deg: float) -> tuple[float, float]:
    a = math.radians(angle_deg)
    cos_a, sin_a = math.cos(a), math.sin(a)
    return (point[0] * cos_a - point[1] * sin_a, point[0] * sin_a + point[1] * cos_a)


def _projection(glyph: Glyph) -> tuple[float, float]:
    """(along, across) of a glyph's centre in its own baseline frame."""
    cx = (glyph.bbox[0] + glyph.bbox[2]) / 2.0
    cy = (glyph.bbox[1] + glyph.bbox[3]) / 2.0
    return _rot((cx, cy), glyph.rotation)


def _baseline_key(glyph: Glyph) -> tuple:
    return (glyph.page, qa(glyph.rotation))


def group_glyphs(glyphs: Sequence[Glyph]) -> list[list[Glyph]]:
    """Collect glyphs that sit on one baseline, next to one another.

    Two glyphs join when they share a rotation, sit at the same height in that
    rotated frame, are comparable in size, and the space between them is small
    against their own size.  Nothing here looks at how the PDF ordered them.
    """
    if not glyphs:
        return []
    index = SpatialIndex([(g.glyph_id, g.page, g.bbox) for g in glyphs])
    by_id = {g.glyph_id: g for g in glyphs}
    parent = {g.glyph_id: g.glyph_id for g in glyphs}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rb < ra:
            ra, rb = rb, ra
        parent[rb] = ra

    for glyph in sort_canonical(glyphs, key=lambda g: (g.page, g.bbox, g.glyph_id)):
        size = max(glyph.height, glyph.size, 1.0)
        for key in index.within_distance(glyph.page, glyph.bbox, size * 1.6):
            other = by_id[key]
            if key == glyph.glyph_id or _baseline_key(other) != _baseline_key(glyph):
                continue
            other_size = max(other.height, other.size, 1.0)
            if max(size, other_size) / min(size, other_size) > 2.6:
                continue
            a_along, a_across = _projection(glyph)
            b_along, b_across = _projection(other)
            if abs(a_across - b_across) > 0.6 * max(size, other_size):
                continue
            gap = bbox_distance(glyph.bbox, other.bbox)
            if gap <= 1.1 * max(size, other_size):
                union(glyph.glyph_id, key)
    groups: dict[str, list[Glyph]] = {}
    for key in sorted(parent):
        groups.setdefault(find(key), []).append(by_id[key])
    return [groups[k] for k in sorted(groups)]


def _order_along_baseline(group: Sequence[Glyph]) -> list[Glyph]:
    return sorted(group, key=lambda g: (round(_projection(g)[0], 4), g.bbox, g.glyph_id))


def _word_gap_limit(ordered: Sequence[Glyph]) -> float:
    """The gap that means 'space' on this line, taken from this line."""
    gaps = []
    for a, b in zip(ordered, ordered[1:]):
        gaps.append(max(0.0, _projection(b)[0] - _projection(a)[0]
                        - (a.width + b.width) / 2.0))
    sizes = [max(g.height, g.size, 0.1) for g in ordered]
    typical = sorted(sizes)[len(sizes) // 2]
    if not gaps:
        return typical
    ordered_gaps = sorted(gaps)
    median_gap = ordered_gaps[len(ordered_gaps) // 2]
    return max(0.38 * typical, median_gap * 2.2 + 0.15 * typical)


def _assemble(ordered: Sequence[Glyph]) -> tuple[str, list[Glyph]]:
    limit = _word_gap_limit(ordered)
    text_parts: list[str] = []
    previous: Optional[Glyph] = None
    for glyph in ordered:
        if previous is not None:
            gap = _projection(glyph)[0] - _projection(previous)[0] - (previous.width + glyph.width) / 2.0
            if gap > limit:
                text_parts.append(" ")
        text_parts.append(glyph.character)
        previous = glyph
    return "".join(text_parts), list(ordered)


def _string_alternatives(ordered: Sequence[Glyph], text: str) -> tuple[tuple[str, float], ...]:
    """Readings this line also allows, cheapest substitution first."""
    swaps: list[tuple[float, int, str]] = []
    for index, glyph in enumerate(ordered):
        for character, score in glyph.alternatives[1:]:
            if score >= 0.45 and character != glyph.character:
                swaps.append((score, index, character))
    swaps.sort(key=lambda s: (-s[0], s[1], s[2]))
    out: list[tuple[str, float]] = []
    positions = _character_positions(ordered, text)
    for score, index, character in swaps[:MAX_STRING_ALTERNATIVES]:
        if index >= len(positions):
            continue
        pos = positions[index]
        variant = text[:pos] + character + text[pos + 1:]
        if variant != text:
            out.append((variant, q(score)))
    return tuple(out)


def _character_positions(ordered: Sequence[Glyph], text: str) -> list[int]:
    positions: list[int] = []
    cursor = 0
    for glyph in ordered:
        while cursor < len(text) and text[cursor] == " " and glyph.character != " ":
            cursor += 1
        positions.append(cursor)
        cursor += max(1, len(glyph.character))
    return positions


def reconstruct(glyphs: Sequence[Glyph]) -> list[TextItem]:
    """Every text item the drawing contains, assembled from glyph positions."""
    items: list[TextItem] = []
    for group in group_glyphs(glyphs):
        ordered = _order_along_baseline(group)
        text, used = _assemble(ordered)
        if not text.strip():
            continue
        xs = [v for g in used for v in (g.bbox[0], g.bbox[2])]
        ys = [v for g in used for v in (g.bbox[1], g.bbox[3])]
        bbox = qbbox((min(xs), min(ys), max(xs), max(ys)))
        heights = sorted(g.height for g in used)
        cap = heights[len(heights) // 2] if heights else 0.0
        confidence = q(min(g.confidence for g in used)) if used else 0.0
        source = "reconstructed" if any(g.source == "text" for g in used) else "path"
        if all(g.source == "text" for g in used):
            source = "native"
        payload = {"p": used[0].page, "b": list(bbox), "t": text, "r": used[0].rotation}
        items.append(
            TextItem(
                text_id=entity_id("text", payload),
                page=used[0].page,
                text=text,
                bbox=bbox,
                origin=(q(used[0].bbox[0]), q(used[0].baseline)),
                rotation=qa(used[0].rotation),
                cap_height=q(cap),
                glyph_ids=tuple(g.glyph_id for g in used),
                source=source,
                confidence=confidence,
                alternatives=_string_alternatives(used, text),
            )
        )
    return sort_canonical(items, key=lambda t: (t.page, t.bbox, t.text, t.text_id))


def merge_duplicate_readings(items: Sequence[TextItem]) -> tuple[list[TextItem], list[dict]]:
    """One piece of lettering, one text item.

    A CAD sheet often carries the same label twice: once as a text object and
    once as the outlines that draw it.  Both readings are kept as evidence, but
    only one item survives, and the text layer wins because it is the file's
    own statement rather than our reading of a shape.
    """
    index = SpatialIndex([(t.text_id, t.page, t.bbox) for t in items])
    by_id = {t.text_id: t for t in items}
    superseded: dict[str, str] = {}
    notes: list[dict] = []
    for item in sort_canonical(items, key=lambda t: (t.page, t.bbox, t.text_id)):
        if item.source == "path":
            continue
        for key in index.intersecting_bbox(item.page, item.bbox):
            other = by_id[key]
            if key == item.text_id or other.source == "native" or key in superseded:
                continue
            if _overlap_fraction(item.bbox, other.bbox) < 0.5:
                continue
            superseded[key] = item.text_id
            notes.append({
                "keptTextId": item.text_id,
                "droppedTextId": key,
                "keptText": item.text,
                "droppedText": other.text,
                "agree": item.text == other.text,
                "reason": "TEXT_LAYER_SUPERSEDES_OUTLINE",
            })
    kept = [t for t in items if t.text_id not in superseded]
    return kept, sort_canonical(notes, key=lambda n: (n["keptTextId"], n["droppedTextId"]))


def _overlap_fraction(a: Sequence[float], b: Sequence[float]) -> float:
    ox = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    oy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ox * oy
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return inter / smaller if smaller > 0 else 0.0


# ---------------------------------------------------------------------------
# token structure - shape of a string, not its meaning
# ---------------------------------------------------------------------------

def token_structure(text: str) -> dict[str, Any]:
    """Describe a string structurally.

    No vocabulary, no catalogue, no list of known codes: only what kinds of
    runs the string is made of and how they are joined.  A designation from a
    drawing nobody has ever seen is described by the same numbers.
    """
    runs: list[dict[str, Any]] = []
    current = ""
    kind = ""

    def kind_of(ch: str) -> str:
        if ch.isdigit():
            return "digit"
        if ch.isalpha():
            return "alpha"
        if ch.isspace():
            return "space"
        return "separator"

    for ch in text:
        k = kind_of(ch)
        if k != kind and current:
            runs.append({"kind": kind, "text": current})
            current = ""
        kind = k
        current += ch
    if current:
        runs.append({"kind": kind, "text": current})
    alnum_runs = [r for r in runs if r["kind"] in ("alpha", "digit")]
    separators = [r["text"] for r in runs if r["kind"] == "separator"]
    words = [w for w in text.split() if w]
    return {
        "length": len(text),
        "runs": runs,
        "runCount": len(runs),
        "alnumRuns": len(alnum_runs),
        "separators": separators,
        "separatorCount": len(separators),
        "hasDigit": any(r["kind"] == "digit" for r in runs),
        "hasAlpha": any(r["kind"] == "alpha" for r in runs),
        "digitCount": sum(len(r["text"]) for r in runs if r["kind"] == "digit"),
        "alphaCount": sum(len(r["text"]) for r in runs if r["kind"] == "alpha"),
        "words": len(words),
        "upperFraction": q(sum(1 for c in text if c.isupper()) / max(1, sum(1 for c in text if c.isalpha()))),
        "leadingAlpha": bool(runs and runs[0]["kind"] == "alpha"),
        "trailingDigits": bool(runs and runs[-1]["kind"] == "digit"),
    }


def to_json(items: Sequence[TextItem], notes: Sequence[dict] = ()) -> dict:
    by_source: dict[str, int] = {}
    for item in items:
        by_source[item.source] = by_source.get(item.source, 0) + 1
    return {
        "textItems": len(items),
        "bySource": {k: by_source[k] for k in sorted(by_source)},
        "meanConfidence": q(sum(i.confidence for i in items) / max(1, len(items))),
        "supersededByTextLayer": len(notes),
    }
