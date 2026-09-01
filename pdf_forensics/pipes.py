"""Pipe geometry, found from geometry.

Two rules shape this module, both from the specification:

* a pipe is never "the longest line", "the nearest line" or "the first
  candidate".  It is a piece of geometry with a reason - two walls that run
  parallel at a constant separation and so describe a bore, a dash chain with
  one rhythm, or a stroke that some other evidence attaches a pipe to;
* text plays no part in finding it.  A pipe exists whether or not anything on
  the sheet names it, and its identity is geometric.  Designations are attached
  afterwards, in :mod:`pdf_forensics.association` terms, and may be absent.

Everything a candidate is built from is kept, so any answer can be walked back
to the segments and the PDF objects that produced it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from .canonical import canonical_json, entity_id, q, sort_canonical
from .fragment_search import (Fragment, chain_polyline, continuation_search,
                              make_fragment, polyline_length)
from .geometry_search import (GeometryModel, angle_difference, direction,
                              point_line_distance, point_segment_distance, projection_overlap,
                              seg_bbox)
from .model import Glyph, PdfObject, PipeCandidate, Segment, TextItem
from .spatial_index import SpatialIndex, bbox_distance, expand

# Roles a piece of linework can have.  Only LINEWORK is eligible to be a pipe.
LETTERING = "LETTERING"
SHEET_FRAME = "SHEET_FRAME"
PANEL = "PANEL"
LINEWORK = "LINEWORK"


@dataclass(frozen=True)
class RoleRecord:
    segment_id: str
    role: str
    reason: str

    def to_json(self) -> dict:
        return {"segmentId": self.segment_id, "role": self.role, "reason": self.reason}


@dataclass(frozen=True)
class Panel:
    """A boxed area of the sheet that holds text: a title block, a legend."""

    panel_id: str
    page: int
    bbox: tuple[float, float, float, float]
    text_ids: tuple[str, ...]
    text_count: int
    area_fraction: float

    def to_json(self) -> dict:
        return {"panelId": self.panel_id, "page": self.page, "bbox": list(self.bbox),
                "textIds": list(self.text_ids), "textCount": self.text_count,
                "areaFraction": self.area_fraction}


def detect_panels(objects: Sequence[PdfObject], text_items: Sequence[TextItem],
                  page_sizes: dict[int, tuple[float, float]],
                  segments: Sequence[Segment] = (),
                  lettering_paths: frozenset[str] = frozenset(),
                  min_lettering_fraction: float = 0.3) -> list[Panel]:
    """Rectangles that box up text: legends, title blocks, revision tables.

    Found from what they contain rather than from where they sit, so a legend
    in the middle of a sheet is still a legend - but a box is only a panel when
    a large share of the ink inside it is *lettering*.  Without that second
    condition a room, a building outline or a sheet border with a label in it
    would be classified as a panel, and every pipe inside it would stop being
    linework.  On real sheets the two populations are far apart: a title block
    and a legend run above a third lettering by ink length, a border or a plan
    area well below a fifth.
    """
    panels: list[Panel] = []
    text_index = SpatialIndex([(t.text_id, t.page, t.bbox) for t in text_items])
    by_id = {t.text_id: t for t in text_items}
    # A panel is measured against the lettering it holds, not against the sheet:
    # a legend is the same size whether it is drawn on an A3 or on a plot ten
    # times that size, and a page-relative floor would miss it on the large one.
    caps = sorted(t.cap_height for t in text_items if t.cap_height > 0.0)
    cap_height = caps[len(caps) // 2] if caps else 6.0
    minimum_side = 4.0 * cap_height
    segment_index = SpatialIndex([(s.segment_id, s.page, seg_bbox(s)) for s in segments])
    segments_by_id = {s.segment_id: s for s in segments}
    for obj in objects:
        if obj.kind != "path" or not obj.coordinates:
            continue
        width, height = page_sizes.get(obj.page, (1.0, 1.0))
        page_area = max(1.0, width * height)
        box = obj.bbox
        area = (box[2] - box[0]) * (box[3] - box[1])
        fraction = area / page_area
        if fraction > 0.40:
            continue
        if (box[2] - box[0]) < minimum_side or (box[3] - box[1]) < minimum_side:
            continue
        if not _is_rectangular(obj):
            continue
        inside = [by_id[k] for k in text_index.intersecting_bbox(obj.page, box)
                  if _contains(box, by_id[k].bbox)]
        if len(inside) < 2:
            continue
        if segments:
            total_ink = 0.0
            lettering_ink = 0.0
            for key in segment_index.intersecting_bbox(obj.page, box):
                segment = segments_by_id[key]
                if not _contains(box, seg_bbox(segment)):
                    continue
                total_ink += segment.length
                if segment.path_id in lettering_paths:
                    lettering_ink += segment.length
            if total_ink <= 0.0 or lettering_ink / total_ink < min_lettering_fraction:
                continue
        panels.append(
            Panel(
                panel_id=entity_id("panel", {"p": obj.page, "b": list(box)}),
                page=obj.page,
                bbox=box,
                text_ids=tuple(sorted(t.text_id for t in inside)),
                text_count=len(inside),
                area_fraction=q(fraction),
            )
        )
    return sort_canonical(panels, key=lambda p: (p.page, p.bbox, p.panel_id))


def _is_rectangular(obj: PdfObject) -> bool:
    for poly in obj.coordinates:
        if len(poly) < 4:
            continue
        angles = set()
        for i in range(len(poly) - 1):
            dx = poly[i + 1][0] - poly[i][0]
            dy = poly[i + 1][1] - poly[i][1]
            if abs(dx) < 1e-6 and abs(dy) < 1e-6:
                continue
            angles.add(round(math.degrees(math.atan2(dy, dx)) % 180.0, 1))
        if angles and all(min(abs(a), abs(a - 90.0), abs(a - 180.0)) <= 1.0 for a in angles):
            return True
    return False


def _contains(outer: Sequence[float], inner: Sequence[float]) -> bool:
    return (outer[0] - 0.5 <= inner[0] and outer[1] - 0.5 <= inner[1]
            and inner[2] <= outer[2] + 0.5 and inner[3] <= outer[3] + 0.5)


def classify_roles(segments: Sequence[Segment], glyphs: Sequence[Glyph],
                   text_items: Sequence[TextItem], panels: Sequence[Panel],
                   objects_by_id: dict[str, PdfObject],
                   page_sizes: dict[int, tuple[float, float]]) -> dict[str, RoleRecord]:
    """Say what each segment is, before anything asks what it means."""
    lettering_paths = {pid for g in glyphs for pid in g.source_object_ids}
    text_index = SpatialIndex([(t.text_id, t.page, t.bbox) for t in text_items])
    panel_index = SpatialIndex([(p.panel_id, p.page, p.bbox) for p in panels])
    panels_by_id = {p.panel_id: p for p in panels}
    roles: dict[str, RoleRecord] = {}
    for segment in segments:
        width, height = page_sizes.get(segment.page, (1.0, 1.0))
        box = seg_bbox(segment)
        role, reason = LINEWORK, "DEFAULT"
        obj = objects_by_id.get(segment.path_id)
        if segment.path_id in lettering_paths:
            role, reason = LETTERING, "PATH_PRODUCED_A_GLYPH"
        elif obj is not None and _covers_sheet(obj.bbox, width, height):
            role, reason = SHEET_FRAME, "PATH_SPANS_THE_SHEET"
        else:
            containing = [panels_by_id[k] for k in panel_index.intersecting_bbox(segment.page, box)
                          if _contains(panels_by_id[k].bbox, box)]
            if containing:
                role, reason = PANEL, "INSIDE_A_TEXT_PANEL"
            else:
                covered = [k for k in text_index.intersecting_bbox(segment.page, box)
                           if _contains(expand(_text_box(text_index, k), 0.6), box)]
                if covered:
                    role, reason = LETTERING, "INSIDE_A_TEXT_ITEM"
        roles[segment.segment_id] = RoleRecord(segment.segment_id, role, reason)
    return roles


def _text_box(index: SpatialIndex, key: str) -> tuple[float, float, float, float]:
    return index.entries[key][1]


def _covers_sheet(bbox: Sequence[float], width: float, height: float) -> bool:
    return ((bbox[2] - bbox[0]) >= 0.85 * width) and ((bbox[3] - bbox[1]) >= 0.85 * height)


# ---------------------------------------------------------------------------
# double-line pipes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WallPair:
    """Two strokes that describe one bore."""

    pair_id: str
    page: int
    separation: float
    a_id: str
    b_id: str
    centre_a: tuple[float, float]
    centre_b: tuple[float, float]
    overlap: float

    def to_json(self) -> dict:
        return {"pairId": self.pair_id, "page": self.page, "separation": self.separation,
                "segmentIds": [self.a_id, self.b_id],
                "centerline": [list(self.centre_a), list(self.centre_b)],
                "overlap": self.overlap}


def double_line_pairs(geometry: GeometryModel, eligible: Sequence[Segment],
                      page_sizes: dict[int, tuple[float, float]],
                      closed_paths: frozenset[str] = frozenset(),
                      min_separation: float = 0.4) -> tuple[list[WallPair], list[dict]]:
    """Pair walls that run parallel at a constant distance.

    Pairing is *mutual*: a wall is paired with another only when each is the
    other's closest parallel partner.  Two candidates at the same distance are
    reported as ambiguous and neither is used - the sheet is telling us
    something the geometry alone cannot settle.

    The two sides of one closed shape are never a pair.  A scale bar's cell, a
    hatched box and an equipment outline all have two parallel edges at a fixed
    distance; what makes a pipe is that its walls are drawn as two separate
    strokes running alongside each other.
    """
    eligible_ids = {s.segment_id for s in eligible}
    best: dict[str, tuple[float, str, float]] = {}
    ambiguous: list[dict] = []
    for segment in sort_canonical(eligible, key=lambda s: (s.page, s.a, s.b, s.segment_id)):
        width, height = page_sizes.get(segment.page, (1000.0, 1000.0))
        max_separation = 0.05 * min(width, height)
        partners: list[tuple[float, str, float]] = []
        for other in geometry.parallel_to(segment, angle_tol=1.5,
                                          max_distance=max_separation, min_overlap=0.55):
            if other.segment_id not in eligible_ids:
                continue
            if other.style_key != segment.style_key or abs(other.width - segment.width) > 0.01:
                continue
            if other.path_id == segment.path_id and segment.path_id in closed_paths:
                continue
            # Two closed rectangles nested inside each other - a sheet border, a
            # box around a legend - run parallel at a constant distance exactly
            # as a pipe's walls do.  A pipe is drawn with open strokes.
            if segment.path_id in closed_paths and other.path_id in closed_paths:
                continue
            separation = q(0.5 * (point_line_distance(other.a, segment.a, segment.b)
                                  + point_line_distance(other.b, segment.a, segment.b)))
            if separation < min_separation or separation > max_separation:
                continue
            partners.append((separation, other.segment_id, projection_overlap(segment, other)))
        if not partners:
            continue
        partners.sort(key=lambda p: (p[0], p[1]))
        if len(partners) > 1 and abs(partners[0][0] - partners[1][0]) < 0.05:
            ambiguous.append({
                "segmentId": segment.segment_id,
                "reason": "TWO_EQUALLY_CLOSE_PARALLEL_PARTNERS",
                "separations": [partners[0][0], partners[1][0]],
                "partnerIds": [partners[0][1], partners[1][1]],
            })
            continue
        best[segment.segment_id] = partners[0]
    pairs: list[WallPair] = []
    seen: set[tuple[str, str]] = set()
    by_id = geometry.by_id
    for segment_id in sorted(best):
        separation, partner_id, overlap = best[segment_id]
        mutual = best.get(partner_id)
        if mutual is None or mutual[1] != segment_id:
            continue
        key = tuple(sorted((segment_id, partner_id)))
        if key in seen:
            continue
        seen.add(key)
        a, b = by_id[key[0]], by_id[key[1]]
        centre_a, centre_b = _midline(a, b)
        # A pipe is longer than it is wide.  Two short edges facing each other
        # across a gap - the ends of a scale-bar cell, the sides of a box - are
        # a shape, not a bore.
        if math.dist(centre_a, centre_b) < separation:
            ambiguous.append({
                "segmentId": segment_id,
                "reason": "PAIR_SHORTER_THAN_ITS_SEPARATION",
                "separations": [separation],
                "partnerIds": [partner_id],
            })
            continue
        pairs.append(
            WallPair(
                pair_id=entity_id("pair", {"p": a.page, "a": list(centre_a), "b": list(centre_b),
                                           "s": separation}),
                page=a.page,
                separation=separation,
                a_id=key[0],
                b_id=key[1],
                centre_a=centre_a,
                centre_b=centre_b,
                overlap=q(overlap),
            )
        )
    return (sort_canonical(pairs, key=lambda p: (p.page, p.centre_a, p.centre_b, p.pair_id)),
            sort_canonical(ambiguous, key=lambda a: (a["segmentId"],)))


def _midline(a: Segment, b: Segment) -> tuple[tuple[float, float], tuple[float, float]]:
    """The centre line of two parallel walls, over the length they share."""
    ux, uy = direction(a)
    origin = a.a

    def project(point):
        return (point[0] - origin[0]) * ux + (point[1] - origin[1]) * uy

    a0, a1 = sorted((project(a.a), project(a.b)))
    b0, b1 = sorted((project(b.a), project(b.b)))
    lo, hi = max(a0, b0), min(a1, b1)
    if hi <= lo:
        lo, hi = min(a0, b0), max(a1, b1)

    def on_line(segment: Segment, t: float) -> tuple[float, float]:
        s0 = project(segment.a)
        s1 = project(segment.b)
        if abs(s1 - s0) < 1e-9:
            return segment.a
        ratio = (t - s0) / (s1 - s0)
        ratio = max(0.0, min(1.0, ratio))
        return (segment.a[0] + ratio * (segment.b[0] - segment.a[0]),
                segment.a[1] + ratio * (segment.b[1] - segment.a[1]))

    pa1, pa2 = on_line(a, lo), on_line(a, hi)
    pb1, pb2 = on_line(b, lo), on_line(b, hi)
    return ((q((pa1[0] + pb1[0]) / 2.0), q((pa1[1] + pb1[1]) / 2.0)),
            (q((pa2[0] + pb2[0]) / 2.0), q((pa2[1] + pb2[1]) / 2.0)))


def wall_fragments(pairs: Sequence[WallPair], geometry: GeometryModel,
                   segment_paths: dict[str, list[str]]) -> list[Fragment]:
    out: list[Fragment] = []
    for pair in pairs:
        a, b = geometry.by_id[pair.a_id], geometry.by_id[pair.b_id]
        sources = sorted(set(segment_paths.get(pair.a_id, []) + segment_paths.get(pair.b_id, [])))
        out.append(
            make_fragment(
                page=pair.page, a=pair.centre_a, b=pair.centre_b, width=a.width,
                style_key=a.style_key, kind="double_line", separation=pair.separation,
                segment_ids=(pair.a_id, pair.b_id), source_object_ids=sources,
                dashed=a.dashed or b.dashed,
                evidence={"pairId": pair.pair_id, "overlap": pair.overlap,
                          "rule": "PARALLEL_WALLS_MUTUALLY_CLOSEST"},
            )
        )
    return sort_canonical(out, key=lambda f: (f.page, f.a, f.b, f.fragment_id))


def dashed_fragments(eligible: Sequence[Segment], used_segment_ids: set[str],
                     segment_paths: dict[str, list[str]]) -> list[Fragment]:
    """Dashed single strokes: a dash pattern is itself a statement of intent."""
    out: list[Fragment] = []
    for segment in eligible:
        if segment.segment_id in used_segment_ids or not segment.dashed:
            continue
        out.append(
            make_fragment(
                page=segment.page, a=segment.a, b=segment.b, width=segment.width,
                style_key=segment.style_key, kind="dashed", separation=None,
                segment_ids=(segment.segment_id,),
                source_object_ids=segment_paths.get(segment.segment_id, []),
                dashed=True,
                evidence={"rule": "DASHED_STROKE"},
            )
        )
    return sort_canonical(out, key=lambda f: (f.page, f.a, f.b, f.fragment_id))


def supported_single_fragments(eligible: Sequence[Segment], used_segment_ids: set[str],
                               segment_paths: dict[str, list[str]],
                               support: dict[str, dict]) -> list[Fragment]:
    """Single strokes that some *other* evidence says are pipes.

    A bare stroke is not a pipe.  It becomes a candidate only when something
    independent points at it - a leader that ends on it, or a double-line pipe
    that continues into it - and the reason is recorded on the fragment.
    """
    out: list[Fragment] = []
    for segment in eligible:
        if segment.segment_id in used_segment_ids:
            continue
        reason = support.get(segment.segment_id)
        if not reason:
            continue
        out.append(
            make_fragment(
                page=segment.page, a=segment.a, b=segment.b, width=segment.width,
                style_key=segment.style_key, kind="single_line", separation=None,
                segment_ids=(segment.segment_id,),
                source_object_ids=segment_paths.get(segment.segment_id, []),
                dashed=segment.dashed,
                evidence=dict(reason),
            )
        )
    return sort_canonical(out, key=lambda f: (f.page, f.a, f.b, f.fragment_id))


def build_candidates(fragments: Sequence[Fragment]) -> list[PipeCandidate]:
    """Join fragments into candidate centerlines."""
    candidates: list[PipeCandidate] = []
    for group in continuation_search(fragments):
        polyline = chain_polyline(group)
        if len(polyline) < 2:
            continue
        separations = [f.separation for f in group if f.separation is not None]
        separation = q(sum(separations) / len(separations)) if separations else None
        kinds = sorted({f.kind for f in group})
        payload = {"p": group[0].page, "g": [list(p) for p in polyline], "k": kinds,
                   "s": separation}
        candidates.append(
            PipeCandidate(
                candidate_id=entity_id("pipe", payload),
                page=group[0].page,
                centerline=polyline,
                kind=kinds[0] if len(kinds) == 1 else "mixed",
                wall_separation=separation,
                width=q(sum(f.width for f in group) / len(group)),
                style_key=group[0].style_key,
                segment_ids=tuple(sorted({s for f in group for s in f.segment_ids})),
                source_object_ids=tuple(sorted({o for f in group for o in f.source_object_ids})),
                length=polyline_length(polyline),
                evidence={
                    "fragmentIds": sorted(f.fragment_id for f in group),
                    "fragmentCount": len(group),
                    "rules": sorted({str(f.evidence.get("rule", "")) for f in group}),
                    "separationSpread": q(max(separations) - min(separations)) if separations else None,
                },
            )
        )
    return sort_canonical(candidates, key=lambda c: (c.page, c.centerline, c.candidate_id))


def split_at_tees(candidates: Sequence[PipeCandidate],
                  tolerance_factor: float = 0.75) -> tuple[list[PipeCandidate], list[dict]]:
    """Cut a candidate where another one ends against its side.

    A branch that meets a main halfway along it is a junction of the piping,
    but nothing in the PDF says so - the main was drawn as one uninterrupted
    line.  Splitting it there is what turns two crossing lines into a graph
    with a node, and the split keeps every source id, so no length is created
    or lost.
    """
    from .geometry_search import point_segment_distance

    entries = []
    for candidate in candidates:
        xs = [p[0] for p in candidate.centerline]
        ys = [p[1] for p in candidate.centerline]
        entries.append((candidate.candidate_id, candidate.page,
                        (min(xs), min(ys), max(xs), max(ys))))
    index = SpatialIndex(entries)
    by_id = {c.candidate_id: c for c in candidates}
    cuts: dict[str, set[tuple[float, float]]] = {}
    notes: list[dict] = []
    for candidate in sort_canonical(candidates, key=lambda c: (c.page, c.centerline, c.candidate_id)):
        for end in (candidate.centerline[0], candidate.centerline[-1]):
            reach = max(1.6, tolerance_factor * (candidate.wall_separation or 2.0))
            for key in index.near_point(candidate.page, end, reach * 2.0):
                if key == candidate.candidate_id:
                    continue
                other = by_id[key]
                tolerance = max(reach, tolerance_factor * (other.wall_separation or 2.0))
                for i in range(len(other.centerline) - 1):
                    a, b = other.centerline[i], other.centerline[i + 1]
                    if point_segment_distance(end, a, b) > tolerance:
                        continue
                    if (math.dist(end, a) <= tolerance or math.dist(end, b) <= tolerance):
                        continue                      # already an endpoint: not a tee
                    foot = _foot_of(end, a, b)
                    cuts.setdefault(key, set()).add(foot)
                    notes.append({"candidateId": key, "at": list(foot),
                                  "becauseOf": candidate.candidate_id, "rule": "SPLIT_AT_TEE"})
    if not cuts:
        return list(candidates), []
    out: list[PipeCandidate] = []
    for candidate in candidates:
        points = cuts.get(candidate.candidate_id)
        if not points:
            out.append(candidate)
            continue
        for piece in _cut_polyline(candidate.centerline, sorted(points)):
            if len(piece) < 2:
                continue
            payload = {"p": candidate.page, "g": [list(x) for x in piece],
                       "k": [candidate.kind], "s": candidate.wall_separation}
            out.append(
                PipeCandidate(
                    candidate_id=entity_id("pipe", payload),
                    page=candidate.page,
                    centerline=tuple(piece),
                    kind=candidate.kind,
                    wall_separation=candidate.wall_separation,
                    width=candidate.width,
                    style_key=candidate.style_key,
                    segment_ids=candidate.segment_ids,
                    source_object_ids=candidate.source_object_ids,
                    length=polyline_length(piece),
                    evidence={**candidate.evidence, "splitFrom": candidate.candidate_id,
                              "rule": "SPLIT_AT_TEE"},
                )
            )
    return (sort_canonical(out, key=lambda c: (c.page, c.centerline, c.candidate_id)),
            sort_canonical(notes, key=lambda n: (n["candidateId"], n["at"], n["becauseOf"])))


def _foot_of(point, a, b) -> tuple[float, float]:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-12:
        return (q(a[0]), q(a[1]))
    t = max(0.0, min(1.0, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length_sq))
    return (q(a[0] + t * dx), q(a[1] + t * dy))


def _cut_polyline(polyline, points) -> list[list[tuple[float, float]]]:
    """Split a polyline at points that lie on it, keeping every millimetre."""
    pieces: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = [polyline[0]]
    remaining = list(points)
    for i in range(len(polyline) - 1):
        a, b = polyline[i], polyline[i + 1]
        on_this = sorted((p for p in remaining if _between(p, a, b)),
                         key=lambda p: math.dist(a, p))
        for cut in on_this:
            if math.dist(cut, current[-1]) > 1e-6:
                current.append(cut)
            if len(current) >= 2:
                pieces.append(current)
            current = [cut]
            remaining.remove(cut)
        if math.dist(b, current[-1]) > 1e-6:
            current.append(b)
    if len(current) >= 2:
        pieces.append(current)
    return pieces


def _between(point, a, b, tolerance: float = 0.25) -> bool:
    from .geometry_search import point_segment_distance
    if point_segment_distance(point, a, b) > tolerance:
        return False
    return math.dist(point, a) > tolerance and math.dist(point, b) > tolerance


def deduplicate(candidates: Sequence[PipeCandidate]) -> tuple[list[PipeCandidate], list[dict]]:
    """One centerline, one candidate.

    A drawing that emits the same line twice must not be measured twice; the
    identity is the content, so the duplicate is dropped and recorded.
    """
    seen: dict[str, PipeCandidate] = {}
    dropped: list[dict] = []
    for candidate in sort_canonical(candidates, key=lambda c: (c.page, c.centerline, c.candidate_id)):
        key = canonical_json({"p": candidate.page, "g": [list(p) for p in candidate.centerline]})
        if key in seen:
            dropped.append({"keptId": seen[key].candidate_id, "droppedId": candidate.candidate_id,
                            "reason": "IDENTICAL_CENTERLINE"})
            continue
        seen[key] = candidate
    return ([seen[k] for k in sorted(seen)], dropped)


def to_json(candidates: Sequence[PipeCandidate], roles: dict[str, RoleRecord],
            panels: Sequence[Panel], ambiguous: Sequence[dict]) -> dict:
    by_role: dict[str, int] = {}
    for record in roles.values():
        by_role[record.role] = by_role.get(record.role, 0) + 1
    by_kind: dict[str, int] = {}
    for candidate in candidates:
        by_kind[candidate.kind] = by_kind.get(candidate.kind, 0) + 1
    return {
        "pipeCandidates": len(candidates),
        "byKind": {k: by_kind[k] for k in sorted(by_kind)},
        "segmentRoles": {k: by_role[k] for k in sorted(by_role)},
        "panels": len(panels),
        "ambiguousPairings": len(ambiguous),
        "totalCenterlineLength": q(sum(c.length for c in candidates)),
    }
