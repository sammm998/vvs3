"""Pipe candidate detection from vector geometry.

Objects reach this stage only after the text stages have consumed the geometry
that turned out to be lettering, so annotation strokes are not mistaken for
pipework.  Three further classes of geometry are excluded, each by a *generic*
rule rather than by position on the sheet:

* **sheet frame** - a closed axis-aligned rectangle covering most of the page,
  and any stroke that runs along it;
* **panels** - anything inside a detected legend or title block, including the
  sample lines a legend draws next to each code;
* **symbols** - closed contours whose extent is small compared with the
  drawing's text height (riser circles, valve symbols).

What survives is split into:

* **dashed pipes** - runs already reassembled by
  :mod:`vvs_pipe.pipes.dashes` from a dashed linetype's individual dashes.
  Below-slab and concealed pipework is drawn this way, so on a real sheet this
  is the majority;
* **double-line pipes** - two parallel strokes, see
  :mod:`vvs_pipe.pipes.centerline`, which carry a drawn width;
* **single-line pipes** - a stroke no partner claimed that is long and slender
  enough to be pipework in a schematic.

Only a double-line pipe has a drawn width; the other two carry their size in
the label alone, which :mod:`vvs_pipe.dimensions` handles explicitly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from ..canonical import canonical_sort, entity_id, qs
from ..geometry.primitives import BBox, Segment
from ..model import Confidence, PipeCandidate, Provenance, VectorObject
from ..states import Reason
from .centerline import DoubleLinePair, PairingConfig, SegmentRef, pair_double_lines
from .dashes import DashChain

Pt = tuple[float, float]


@dataclass(frozen=True, slots=True)
class DetectionConfig:
    sheet_frame_area_ratio: float = 0.40
    exclude_closed_contours: bool = True
    frame_band_pt: float = 3.0
    min_segment_length_pt: float = 2.0
    single_line_min_length_ratio: float = 3.0  # of the median text cap height
    accept_single_lines: bool = True
    pairing: PairingConfig = PairingConfig()


@dataclass(frozen=True, slots=True)
class PipeDetection:
    candidates: tuple[PipeCandidate, ...]
    consumed_object_ids: frozenset[str]
    excluded_object_ids: frozenset[str]
    leftover: tuple[SegmentRef, ...]
    symbol_boxes: tuple[BBox, ...]


def _sheet_frames(objects: Sequence[VectorObject], page_box: BBox, cfg: DetectionConfig) -> list[BBox]:
    out: list[BBox] = []
    for o in objects:
        if not o.closed:
            continue
        if o.bbox.area >= cfg.sheet_frame_area_ratio * page_box.area:
            out.append(o.bbox)
    return out


def _on_frame(seg: Segment, frames: Sequence[BBox], cfg: DetectionConfig) -> bool:
    for f in frames:
        for p in (seg.a, seg.b, seg.midpoint):
            near = (
                abs(p[0] - f.x0) <= cfg.frame_band_pt
                or abs(p[0] - f.x1) <= cfg.frame_band_pt
                or abs(p[1] - f.y0) <= cfg.frame_band_pt
                or abs(p[1] - f.y1) <= cfg.frame_band_pt
            )
            if not near:
                break
        else:
            return True
    return False


def detect_pipes(
    objects: Sequence[VectorObject],
    page_box: BBox,
    page: int,
    consumed_by_text: frozenset[str],
    panel_boxes: Sequence[BBox],
    text_cap_height: float,
    cfg: DetectionConfig | None = None,
    explained_object_ids: frozenset[str] = frozenset(),
    include_single_lines: bool = False,
    dash_chains: Sequence[DashChain] = (),
) -> PipeDetection:
    """Find double-line pipes, and optionally accept leftover single strokes.

    ``explained_object_ids`` are strokes an earlier stage already accounted for
    - a leader line attached to a label, for instance.  Geometry with a
    non-pipe explanation is never offered to the pipe stages again.

    Double-line pairing runs *before* leader detection in the pipeline, because
    a stroke that pairs into a pipe wall is pipework by strong geometric
    evidence and must not be re-read as an annotation leader.  Single-line
    acceptance therefore runs in a second call, once the leaders are known.
    """
    cfg = cfg or DetectionConfig()
    ordered = canonical_sort(list(objects), key=lambda o: o.canonical_key())
    frames = _sheet_frames(ordered, page_box, cfg)

    # Pre-pass: every closed contour small enough to be a drawing symbol.  This
    # is collected before any filtering so that whether a stroke is recognised
    # as symbol furniture cannot depend on the order objects are visited in.
    #
    # Geometry the text stages already claimed is excluded: characters such as
    # O, 0, 8 and Ø are closed contours of exactly this size, and letting them
    # through would present every one of them to the vertical analysis as a
    # riser symbol.
    symbol_boxes = [
        o.bbox
        for o in ordered
        if o.closed
        and o.object_id not in consumed_by_text
        and o.object_id not in explained_object_ids
        and not any(p.contains_box(o.bbox) for p in panel_boxes)
        and o.bbox.area < cfg.sheet_frame_area_ratio * page_box.area
        and max(o.bbox.width, o.bbox.height) <= 6.0 * max(text_cap_height, 1.0)
    ]

    excluded: set[str] = set()
    refs: list[SegmentRef] = []

    # Dashed runs come pre-assembled; their member dashes are spoken for and
    # must not also be offered to the pairing or single-line stages.
    dash_candidates: list[PipeCandidate] = []
    dash_consumed: set[str] = set()
    for chain in dash_chains:
        if any(oid in consumed_by_text or oid in explained_object_ids for oid in chain.object_ids):
            continue
        if any(p.contains_box(BBox.from_points(chain.polyline)) for p in panel_boxes):
            continue
        dash_candidates.append(_candidate_from_chain(chain, page))
        dash_consumed.update(chain.object_ids)
    for o in ordered:
        if (
            o.object_id in consumed_by_text
            or o.object_id in explained_object_ids
            or o.object_id in dash_consumed
        ):
            excluded.add(o.object_id)
            continue
        if not o.is_stroked:
            excluded.add(o.object_id)
            continue
        if any(p.contains_box(o.bbox) for p in panel_boxes):
            excluded.add(o.object_id)
            continue
        if o.bbox.area >= cfg.sheet_frame_area_ratio * page_box.area:
            excluded.add(o.object_id)
            continue
        if o.closed:
            # A pipe in plan is drawn as two independent strokes.  A closed
            # contour is a symbol, a hatch, a panel or a scale bar - never a
            # pipe wall - so it is excluded here rather than being allowed to
            # pair its own opposite sides into a phantom pipe.  Pipework drawn
            # as a single closed outline is a documented limitation.
            excluded.add(o.object_id)
            continue
        # A stroke that lies wholly inside a symbol's own footprint belongs to
        # that symbol (the cross through a riser circle, a valve's stem).
        if any(b.expanded(0.6 * max(b.width, b.height)).contains_box(o.bbox) for b in symbol_boxes):
            excluded.add(o.object_id)
            continue
        kept = False
        for seg in o.segments():
            if seg.length < cfg.min_segment_length_pt:
                continue
            if _on_frame(seg, frames, cfg):
                continue
            refs.append(
                SegmentRef(
                    segment=seg,
                    object_id=o.object_id,
                    stroke_width=o.stroke_width,
                    color=o.stroke_color,
                    dashes=o.dashes,
                )
            )
            kept = True
        if not kept:
            excluded.add(o.object_id)

    pairs, consumed = pair_double_lines(refs, cfg.pairing)
    consumed = set(consumed) | dash_consumed

    candidates: list[PipeCandidate] = list(dash_candidates)
    for p in pairs:
        candidates.append(
            _candidate_from_pair(p, page, cfg)
        )

    if cfg.accept_single_lines and include_single_lines:
        min_len = cfg.single_line_min_length_ratio * max(text_cap_height, 1.0)
        leftovers = [r for r in refs if r.object_id not in consumed]
        for r in canonical_sort(leftovers, key=lambda r: (r.segment.key(), r.object_id)):
            if r.segment.length < min_len:
                continue
            candidates.append(_candidate_from_single(r, page))

    candidates = canonical_sort(candidates, key=lambda c: c.canonical_key())
    leftover = tuple(
        canonical_sort(
            [r for r in refs if r.object_id not in consumed],
            key=lambda r: (r.segment.key(), r.object_id),
        )
    )
    return PipeDetection(
        candidates=tuple(candidates),
        consumed_object_ids=frozenset(consumed),
        excluded_object_ids=frozenset(excluded),
        leftover=leftover,
        symbol_boxes=tuple(canonical_sort(symbol_boxes, key=lambda b: b.key())),
    )


def single_line_candidates(
    leftover: Sequence[SegmentRef],
    page: int,
    text_cap_height: float,
    explained_object_ids: frozenset[str],
    cfg: DetectionConfig | None = None,
) -> tuple[PipeCandidate, ...]:
    """Accept unpaired strokes as single-line (schematic) pipes.

    Called after leader detection, so a stroke that has already been explained
    as a leader is not offered twice.  A single-line candidate carries no drawn
    width, so it is created with INSUFFICIENT_GEOMETRY recorded against it and
    can never contribute a measured diameter.
    """
    cfg = cfg or DetectionConfig()
    min_len = cfg.single_line_min_length_ratio * max(text_cap_height, 1.0)
    out = [
        _candidate_from_single(r, page)
        for r in canonical_sort(list(leftover), key=lambda r: (r.segment.key(), r.object_id))
        if r.object_id not in explained_object_ids and r.segment.length >= min_len
    ]
    return tuple(canonical_sort(out, key=lambda c: c.canonical_key()))


def _candidate_from_pair(p: DoubleLinePair, page: int, cfg: DetectionConfig) -> PipeCandidate:
    evidence = (
        ("overlapFraction", qs(p.overlap_fraction)),
        ("pairScore", qs(p.score)),
        ("separationPt", qs(p.width_pt)),
    )
    sources = tuple(sorted({p.left.object_id, p.right.object_id}))
    return PipeCandidate(
        candidate_id=entity_id("pc", (page, p.key(), "double_line")),
        page=page,
        centerline=p.centerline,
        style="double_line",
        width_pt=p.width_pt,
        stroke_width=p.left.stroke_width,
        color=p.left.color,
        dashes=p.left.dashes,
        source_object_ids=sources,
        accepted=True,
        rejection_reason=None,
        confidence=Confidence(geometry=qs(min(0.99, 0.55 + 0.45 * p.score))),
        evidence=evidence,
        provenance=Provenance(
            stage="pipe-detection",
            rule="parallel stroke pair -> midline of mutual overlap",
            source_object_ids=sources,
            notes=(f"widthPt={qs(p.width_pt)}",),
        ),
    )


def _candidate_from_chain(chain: DashChain, page: int) -> PipeCandidate:
    """One reassembled dashed run becomes one pipe candidate.

    It carries no drawn width - a dashed centreline is a single stroke - so the
    size has to come from the label, and the evidence records how many dashes
    and how many bridged gaps went into it so the reassembly can be audited.
    """
    duty = chain.ink_length_pt / max(chain.length_pt, 1e-9)
    return PipeCandidate(
        candidate_id=entity_id("pc", (page, chain.key(), "dashed_line")),
        page=page,
        centerline=chain.polyline,
        style="dashed_line",
        width_pt=None,
        stroke_width=chain.stroke_width,
        color=chain.color,
        dashes=chain.dashes,
        source_object_ids=chain.object_ids,
        accepted=True,
        rejection_reason=None,
        confidence=Confidence(geometry=qs(min(0.95, 0.55 + 0.05 * min(8, chain.member_count)))),
        evidence=(
            ("dashes", float(chain.member_count)),
            ("bridgedGaps", float(chain.bridged_gaps)),
            ("inkDutyCycle", qs(min(1.0, duty))),
            ("lengthPt", qs(chain.length_pt)),
        ),
        provenance=Provenance(
            stage="pipe-detection",
            rule="dashed linetype reassembled from its dashes",
            source_object_ids=chain.object_ids,
            notes=(f"layer={chain.layer}", f"members={chain.member_count}"),
        ),
    )


def _candidate_from_single(r: SegmentRef, page: int) -> PipeCandidate:
    seg = r.segment
    return PipeCandidate(
        candidate_id=entity_id("pc", (page, seg.key(), "single_line")),
        page=page,
        centerline=(seg.a, seg.b),
        style="single_line",
        width_pt=None,
        stroke_width=r.stroke_width,
        color=r.color,
        dashes=r.dashes,
        source_object_ids=(r.object_id,),
        accepted=True,
        rejection_reason=Reason.INSUFFICIENT_GEOMETRY,
        confidence=Confidence(geometry=0.45),
        evidence=(("lengthPt", qs(seg.length)),),
        provenance=Provenance(
            stage="pipe-detection",
            rule="unpaired stroke accepted as single-line pipe (no drawn width)",
            source_object_ids=(r.object_id,),
        ),
    )
