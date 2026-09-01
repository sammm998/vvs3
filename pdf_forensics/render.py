"""Sixteenth requirement: local rendering, and the marked drawing.

Rendering is a *check*, never a source.  Nothing in the pipeline reads a
pixel to decide anything; crops exist so that a candidate can be looked at, and
the marked drawing exists so that what the engine found can be seen exactly
where it found it - the reconstructed centerline itself, not a box around the
neighbourhood and not an approximation of it.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import fitz

from .canonical import q
from .loader import LoadedPdf

MARK_PIPE = (0.85, 0.10, 0.10)
MARK_LEADER = (0.10, 0.45, 0.85)
MARK_TEXT = (0.10, 0.60, 0.20)
MARK_UNRESOLVED = (0.95, 0.60, 0.10)


def render_crop(pdf: LoadedPdf, page: int, bbox: Sequence[float], out_path: str | Path,
                pad: float = 12.0, dpi: int = 220, max_side: int = 1400) -> dict:
    """Render a small area of the sheet to a PNG, for visual confirmation."""
    info = pdf.pages[page]
    clip = fitz.Rect(max(0.0, bbox[0] - pad), max(0.0, bbox[1] - pad),
                     min(info.width, bbox[2] + pad), min(info.height, bbox[3] + pad))
    if clip.is_empty:
        clip = fitz.Rect(0, 0, info.width, info.height)
    zoom = dpi / 72.0
    side = max(clip.width, clip.height) * zoom
    if side > max_side:
        zoom *= max_side / side
    pix = pdf.page(page).get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(out_path))
    return {"file": str(out_path), "page": page, "clip": [q(v) for v in clip],
            "zoom": q(zoom), "width": pix.width, "height": pix.height}


def mark_drawing(pdf: LoadedPdf, out_path: str | Path, pipes: Sequence[Any],
                 leaders: Sequence[Any] = (), candidates: Sequence[Any] = (),
                 designations_by_pipe: Optional[dict[str, str]] = None) -> dict:
    """Draw what was found on top of the sheet, at its own coordinates."""
    designations_by_pipe = designations_by_pipe or {}
    doc = fitz.open(str(pdf.path))
    counts = {"pipes": 0, "leaders": 0, "designations": 0}
    for pipe in pipes:
        page = doc.load_page(pipe.page)
        shape = page.new_shape()
        for part in (pipe.parts or (pipe.centerline,)):
            points = [fitz.Point(*p) for p in part]
            for a, b in zip(points, points[1:]):
                shape.draw_line(a, b)
        named = bool(designations_by_pipe.get(pipe.pipe_id))
        # closePath would draw a line from the last point back to the first:
        # geometry that is not on the sheet and that the engine never found.
        shape.finish(color=MARK_PIPE if named else MARK_UNRESOLVED, width=1.4, closePath=False)
        shape.commit()
        counts["pipes"] += 1
    for leader in leaders:
        page = doc.load_page(leader.page)
        shape = page.new_shape()
        points = [fitz.Point(*p) for p in leader.polyline]
        for a, b in zip(points, points[1:]):
            shape.draw_line(a, b)
        shape.finish(color=MARK_LEADER, width=0.6, dashes="[2 2] 0", closePath=False)
        shape.commit()
        counts["leaders"] += 1
    for candidate in candidates:
        page = doc.load_page(candidate.page)
        shape = page.new_shape()
        shape.draw_rect(fitz.Rect(*candidate.bbox))
        shape.finish(color=MARK_TEXT, width=0.5)
        shape.commit()
        counts["designations"] += 1
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path), deflate=True, garbage=3, no_new_id=True)
    doc.close()
    return {"file": str(out_path), **counts}
