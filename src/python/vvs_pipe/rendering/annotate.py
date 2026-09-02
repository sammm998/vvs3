"""Marked and debug drawings.

The marked drawing is what a quantity surveyor would produce by hand: the
original sheet, untouched and still fully legible underneath, with every pipe
the engine measured highlighted and captioned with its designation, size,
length and state.

The debug drawing adds every intermediate the engine formed - glyph boxes,
designation boxes, leaders, centerlines, graph nodes, physical pipes, verticals
and the geometry it rejected - so any decision can be traced back to the
geometry that caused it.

Overlays are drawn into the page content, not as annotations, so re-running the
engine on its own output would not mistake the markup for drawing content -
and, if it were saved as annotations, the extractor would strip them anyway.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Sequence

import fitz

from ..canonical import ql
from ..states import IdentityState

STATE_COLORS: dict[str, tuple[float, float, float]] = {
    IdentityState.CONFIRMED.value: (0.00, 0.55, 0.20),
    IdentityState.HIGH_CONFIDENCE.value: (0.00, 0.40, 0.85),
    IdentityState.AMBIGUOUS.value: (0.95, 0.60, 0.00),
    IdentityState.INSUFFICIENT.value: (0.85, 0.35, 0.00),
    IdentityState.UNRESOLVED.value: (0.80, 0.00, 0.10),
}
FALLBACK_COLOR = (0.45, 0.45, 0.45)

DEBUG_GLYPH = (0.55, 0.55, 0.95)
DEBUG_DESIGNATION = (0.10, 0.45, 0.95)
DEBUG_LEADER = (0.95, 0.35, 0.75)
DEBUG_ATTACHMENT = (0.10, 0.55, 0.95)
DEBUG_NODE = (0.10, 0.10, 0.10)
DEBUG_REJECTED = (0.70, 0.70, 0.70)
DEBUG_VERTICAL = (0.60, 0.10, 0.75)


def _color(state: str) -> tuple[float, float, float]:
    return STATE_COLORS.get(state, FALLBACK_COLOR)


def _polyline(shape, points: Sequence[Sequence[float]]) -> None:
    pts = [fitz.Point(float(x), float(y)) for x, y in points]
    for i in range(len(pts) - 1):
        shape.draw_line(pts[i], pts[i + 1])


def _label(page: "fitz.Page", origin: tuple[float, float], text: str, color,
           size: float = 6.0, oc: int | None = None) -> None:
    """Small caption, on an optional-content layer when one is given."""
    try:
        if oc is None:
            page.insert_text(fitz.Point(*origin), text, fontname="helv", fontsize=size,
                             color=color)
        else:
            page.insert_text(fitz.Point(*origin), text, fontname="helv", fontsize=size,
                             color=color, oc=oc)
    except Exception:  # pragma: no cover - never let a caption break a render
        pass


def _layers(doc: "fitz.Document", names: Sequence[str]) -> dict[str, int]:
    """Optional-content groups, so each kind of mark can be switched off.

    The quantity overlay and the association evidence answer different
    questions, and a reader needs to see them apart: the first is "which
    geometry did the engine measure", the second is "why does it carry that
    name".  Drawing them into one inseparable layer is what made the overlay
    read as long rays across the sheet.
    """
    out: dict[str, int] = {}
    for name in names:
        try:
            out[name] = doc.add_ocg(name, on=1)
        except Exception:                     # pragma: no cover - older PyMuPDF
            out[name] = None
    return out


def render_marked(result, out_path: str | Path) -> Path:
    """Original sheet + the pipe geometry the take-off measured.

    Only geometry the engine owns is drawn: each physical pipe's own
    centerline, in place.  No line is drawn between a label and a pipe here -
    association evidence belongs to the debug overlay, on its own layer.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(result.source_path)
    layers = _layers(doc, ["Pipe geometry", "Pipe captions", "Vertical pipe", "Legend key"])
    try:
        for page_result in result.pages:
            page = doc[page_result.page]
            _strip_annotations(page)
            for pipe in page_result.physical_pipes:
                color = _color(pipe.identity_state.value)
                shape = page.new_shape()
                for poly in pipe.centerline:
                    _polyline(shape, poly)
                shape.finish(color=color, width=2.2, stroke_opacity=0.65,
                             closePath=False, oc=layers["Pipe geometry"])
                shape.commit()

                anchor, normal = _anchor_of(pipe)
                caption = _caption(pipe)
                _label(
                    page,
                    (anchor[0] + normal[0] * 7.0 + 3.0, anchor[1] + normal[1] * 7.0 - 3.0),
                    caption,
                    color,
                    size=6.5,
                    oc=layers["Pipe captions"],
                )

            for v in page_result.verticals:
                color = _color(v.state.value)
                shape = page.new_shape()
                shape.draw_circle(fitz.Point(*v.point), 9.0)
                shape.finish(color=color, width=1.4, oc=layers["Vertical pipe"])
                shape.commit()
                text = "VERT " + (f"{ql(v.length_m)} m" if v.length_m is not None else "UNKNOWN")
                _label(page, (v.point[0] + 11.0, v.point[1] + 12.0), text, color, size=6.0,
                       oc=layers["Vertical pipe"])

            _draw_state_legend(page, page_result, oc=layers["Legend key"])
        doc.save(str(out_path), deflate=True, garbage=3)
    finally:
        doc.close()
    return out_path


def render_debug(result, out_path: str | Path) -> Path:
    """Every intermediate the engine formed, drawn on the sheet."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(result.source_path)
    layers = _layers(doc, ["Glyphs", "Designations", "Pipe geometry", "Rejected candidates",
                           "Graph nodes", "Vertical pipe", "Leaders", "Association",
                           "Panels"])
    try:
        for page_result in result.pages:
            page = doc[page_result.page]
            _strip_annotations(page)

            # Leaders as the drawing drew them, and the attachment each one
            # verified - the association evidence, switchable on its own.
            shape = page.new_shape()
            for leader in getattr(page_result, "leaders", ()):
                _polyline(shape, leader.polyline)
            shape.finish(color=DEBUG_LEADER, width=0.9, closePath=False, oc=layers["Leaders"])
            shape.commit()

            shape = page.new_shape()
            for attachment in getattr(page_result, "attachments", ()):
                shape.draw_circle(fitz.Point(*attachment.tip), 3.0)
            shape.finish(color=DEBUG_ATTACHMENT, width=1.0, oc=layers["Association"])
            shape.commit()

            for chain in getattr(page_result, "chains", ()):
                _label(page, (chain.leader_tip[0] + 4.0, chain.leader_tip[1] - 4.0),
                       f"{chain.designation} -> {chain.pipe_run_id[:8]}", DEBUG_ATTACHMENT,
                       4.5, oc=layers["Association"])

            shape = page.new_shape()
            for g in page_result.glyphs:
                shape.draw_rect(fitz.Rect(*g.bbox.to_canonical()))
            shape.finish(color=DEBUG_GLYPH, width=0.25, oc=layers["Glyphs"])
            shape.commit()

            shape = page.new_shape()
            for d in page_result.designations:
                shape.draw_rect(fitz.Rect(*d.bbox.to_canonical()))
            shape.finish(color=DEBUG_DESIGNATION, width=0.5, oc=layers["Designations"])
            shape.commit()

            shape = page.new_shape()
            for c in page_result.candidates:
                if c.style == "single_line":
                    _polyline(shape, c.centerline)
            shape.finish(color=DEBUG_REJECTED, width=0.8, dashes="[2 2] 0",
                         closePath=False, oc=layers["Rejected candidates"])
            shape.commit()

            shape = page.new_shape()
            for r in page_result.runs:
                _polyline(shape, r.centerline)
            shape.finish(color=(0.0, 0.0, 0.0), width=0.9, closePath=False,
                         oc=layers["Pipe geometry"])
            shape.commit()

            shape = page.new_shape()
            for n in page_result.graph.nodes:
                shape.draw_circle(fitz.Point(*n.point), 2.0)
            shape.finish(color=DEBUG_NODE, width=0.5, oc=layers["Graph nodes"])
            shape.commit()

            shape = page.new_shape()
            for v in page_result.verticals:
                shape.draw_circle(fitz.Point(*v.point), 7.0)
            shape.finish(color=DEBUG_VERTICAL, width=1.0, oc=layers["Vertical pipe"])
            shape.commit()

            for p in page_result.panels:
                shape = page.new_shape()
                shape.draw_rect(fitz.Rect(*p.bbox.to_canonical()))
                shape.finish(color=DEBUG_LEADER, width=0.8, dashes="[4 2] 0",
                             oc=layers["Panels"])
                shape.commit()

            for r in page_result.runs:
                mid = r.centerline[len(r.centerline) // 2]
                _label(page, (mid[0] + 2.0, mid[1] - 2.0), r.pipe_run_id[:10], (0.2, 0.2, 0.2), 4.5)

            _label(
                page,
                (20.0, 14.0),
                f"DEBUG page {page_result.page}  glyphs={len(page_result.glyphs)} "
                f"designations={len(page_result.designations)} candidates={len(page_result.candidates)} "
                f"runs={len(page_result.runs)} physical={len(page_result.physical_pipes)} "
                f"leaders={len(getattr(page_result, 'leaders', ()))} "
                f"attached={len(getattr(page_result, 'attachments', ()))} "
                f"scale={page_result.scale.state.value}",
                (0.0, 0.0, 0.0),
                7.0,
            )
        doc.save(str(out_path), deflate=True, garbage=3)
    finally:
        doc.close()
    return out_path


def _strip_annotations(page: "fitz.Page") -> None:
    try:
        a = page.first_annot
        while a is not None:
            a = page.delete_annot(a)
    except Exception:  # pragma: no cover
        pass


def _anchor_of(pipe) -> tuple[tuple[float, float], tuple[float, float]]:
    """Caption anchor plus the unit normal of the pipe at that point.

    The caption is offset along the normal so it sits beside the pipe rather
    than on top of it - the original geometry has to stay readable.
    """
    poly = pipe.centerline[0]
    i = max(0, len(poly) // 2 - 1)
    a, b = poly[i], poly[min(i + 1, len(poly) - 1)]
    mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    dx, dy = b[0] - a[0], b[1] - a[1]
    ln = math.hypot(dx, dy) or 1.0
    normal = (-dy / ln, dx / ln)
    if normal[1] > 0:  # prefer placing the caption above the pipe
        normal = (-normal[0], -normal[1])
    return (float(mid[0]), float(mid[1])), normal


def _caption(pipe) -> str:
    parts = [pipe.designation or "UNRESOLVED"]
    if pipe.diameter_mm is not None:
        parts.append(f"DN{pipe.diameter_mm:g}")
    if pipe.total_length_m is not None:
        parts.append(f"{pipe.total_length_m:.2f} m")
        if pipe.vertical_length_m:
            parts.append(f"(h {pipe.horizontal_length_m:.2f} + v {pipe.vertical_length_m:.2f})")
    else:
        parts.append("NOT_MEASURABLE")
    parts.append(pipe.identity_state.value)
    return "  ".join(parts)


def _draw_state_legend(page: "fitz.Page", page_result, oc: int | None = None) -> None:
    x, y = 34.0, 46.0
    _label(page, (x, y), "VVS-PIPE AUTOMATIC TAKE-OFF", (0, 0, 0), 7.5, oc=oc)
    for i, (state, color) in enumerate(sorted(STATE_COLORS.items())):
        yy = y + 10.0 + i * 9.0
        shape = page.new_shape()
        shape.draw_line(fitz.Point(x, yy - 2.0), fitz.Point(x + 16.0, yy - 2.0))
        shape.finish(color=color, width=2.2, oc=oc)
        shape.commit()
        _label(page, (x + 20.0, yy), state, color, 6.0, oc=oc)
    _label(
        page,
        (x, y + 10.0 + len(STATE_COLORS) * 9.0 + 2.0),
        f"scale={page_result.scale.state.value}"
        + (
            f" 1:{page_result.scale.ratio_denominator:g}"
            if page_result.scale.ratio_denominator
            else ""
        ),
        (0, 0, 0),
        6.0,
        oc=oc,
    )
