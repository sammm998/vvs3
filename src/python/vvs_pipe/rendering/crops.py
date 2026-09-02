"""Local crops of the places where the evidence chain broke.

A count of failures is not debuggable; a picture of each one is.  For every
label that did not complete

    glyphs -> designation + DN -> vector leader -> endpoint -> FE geometry -> pipe

this writes a small render of that part of the sheet, named after the stage
that stopped it, together with an index of what was expected there.  The crop
is a *check*: the vectors remain the evidence, and nothing in the pipeline reads
a pixel.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import fitz

from ..canonical import qs
from ..geometry.primitives import BBox

SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
DEFAULT_DPI = 220
MAX_SIDE_PX = 1500


@dataclass(frozen=True, slots=True)
class CropRequest:
    page: int
    bbox: BBox
    stage: str
    reason: str
    label: str
    identifier: str
    extra: tuple[tuple[str, str], ...] = ()


def _name(request: CropRequest, index: int) -> str:
    label = SAFE.sub("_", request.label.strip())[:32] or "unnamed"
    return f"{index:03d}__{request.stage}__{request.reason}__{label}.png"


def _pad_for(bbox: BBox, cap: float) -> float:
    return max(24.0, 6.0 * max(cap, 1.0), 0.25 * max(bbox.width, bbox.height))


def render_crops(
    source_path: str | Path,
    requests: Sequence[CropRequest],
    out_dir: str | Path,
    cap_height: float = 7.0,
    dpi: int = DEFAULT_DPI,
) -> list[dict[str, Any]]:
    """Render one crop per request and return an index of what was written."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[dict[str, Any]] = []
    doc = fitz.open(str(source_path))
    try:
        for index, request in enumerate(requests):
            page = doc[request.page]
            pad = _pad_for(request.bbox, cap_height)
            clip = fitz.Rect(
                max(page.rect.x0, request.bbox.x0 - pad),
                max(page.rect.y0, request.bbox.y0 - pad),
                min(page.rect.x1, request.bbox.x1 + pad),
                min(page.rect.y1, request.bbox.y1 + pad),
            )
            if clip.is_empty or clip.width <= 0 or clip.height <= 0:
                continue
            zoom = dpi / 72.0
            longest = max(clip.width, clip.height) * zoom
            if longest > MAX_SIDE_PX:
                zoom *= MAX_SIDE_PX / longest
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
            name = _name(request, index)
            pix.save(str(out_dir / name))
            written.append(
                {
                    "file": name,
                    "page": request.page,
                    "stage": request.stage,
                    "reason": request.reason,
                    "label": request.label,
                    "id": request.identifier,
                    "clip": [qs(v) for v in clip],
                    "detail": {k: v for k, v in request.extra},
                }
            )
    finally:
        doc.close()
    (out_dir / "index.json").write_text(json.dumps(written, indent=2, sort_keys=True),
                                        encoding="utf-8")
    return written


def requests_from_result(result) -> list[CropRequest]:
    """One crop for every stage of the chain that failed, on every page."""
    out: list[CropRequest] = []
    for page_result in result.pages:
        designations = {d.designation_id: d for d in page_result.designations}
        for failure in getattr(page_result, "chain_failures", ()):
            bbox = failure.bbox
            if failure.point is not None:
                # show the label and the place the leader stopped, together
                bbox = BBox(
                    min(bbox.x0, failure.point[0]), min(bbox.y0, failure.point[1]),
                    max(bbox.x1, failure.point[0]), max(bbox.y1, failure.point[1]),
                )
            extra: list[tuple[str, str]] = [("textId", failure.text_id)]
            d = designations.get(failure.designation_id or "")
            if d is not None:
                extra.append(("diameterMm", "" if d.diameter_mm is None else f"{d.diameter_mm:g}"))
                extra.append(("tier", d.tier.value))
            if failure.point is not None:
                extra.append(("leaderTip", f"{failure.point[0]:.1f},{failure.point[1]:.1f}"))
            out.append(
                CropRequest(
                    page=page_result.page,
                    bbox=bbox,
                    stage=failure.stage,
                    reason=failure.reason,
                    label=failure.text,
                    identifier=failure.designation_id or failure.text_id,
                    extra=tuple(extra),
                )
            )
        # a designation that never got a DN is a chain failure at the reading
        # stage, and is worth looking at even though it reached no leader
        for d in page_result.designations:
            if d.role.value != "PIPE_DESIGNATION" or d.diameter_mm is not None:
                continue
            out.append(
                CropRequest(
                    page=page_result.page,
                    bbox=d.bbox,
                    stage="designation_dn",
                    reason="NO_DN_READ",
                    label=d.text,
                    identifier=d.designation_id,
                    extra=(("textId", d.text_item_id), ("tier", d.tier.value)),
                )
            )
    return out
