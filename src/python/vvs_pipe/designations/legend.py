"""Legend / title-block panel detection.

A panel is found the way a reader finds one: an axis-aligned closed rectangle,
big enough to be a panel but far too small to be the sheet border, holding
several pieces of text and no through-running geometry.  Nothing about the
panel's *position* on the sheet is assumed - a legend may sit anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..canonical import canonical_sort, entity_id
from ..geometry.primitives import BBox
from ..model import TextItem, VectorObject

AXIS_TOLERANCE_PT = 0.75
MIN_PANEL_AREA_RATIO = 0.004
MAX_PANEL_AREA_RATIO = 0.40
MIN_PANEL_TEXT_ITEMS = 2


@dataclass(frozen=True, slots=True)
class Panel:
    panel_id: str
    page: int
    bbox: BBox
    text_item_ids: tuple[str, ...]
    source_object_id: str

    def to_canonical(self) -> dict:
        return {
            "panelId": self.panel_id,
            "page": self.page,
            "bbox": self.bbox.to_canonical(),
            "textItemIds": sorted(self.text_item_ids),
            "sourceObjectId": self.source_object_id,
        }


def _is_axis_aligned_rect(o: VectorObject) -> bool:
    pts = list(o.points)
    if pts and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) != 4:
        return False
    b = o.bbox
    for x, y in pts:
        near_x = min(abs(x - b.x0), abs(x - b.x1)) <= AXIS_TOLERANCE_PT
        near_y = min(abs(y - b.y0), abs(y - b.y1)) <= AXIS_TOLERANCE_PT
        if not (near_x and near_y):
            return False
    return b.width > 0 and b.height > 0


def detect_panels(
    objects: Sequence[VectorObject],
    text_items: Sequence[TextItem],
    page_box: BBox,
    page: int,
) -> tuple[Panel, ...]:
    page_area = max(page_box.area, 1e-9)
    panels: list[Panel] = []
    for o in objects:
        if not _is_axis_aligned_rect(o):
            continue
        ratio = o.bbox.area / page_area
        if not (MIN_PANEL_AREA_RATIO <= ratio <= MAX_PANEL_AREA_RATIO):
            continue
        inside = [t.text_id for t in text_items if o.bbox.contains_box(t.bbox)]
        if len(inside) < MIN_PANEL_TEXT_ITEMS:
            continue
        panels.append(
            Panel(
                panel_id=entity_id("panel", (page, o.bbox.key())),
                page=page,
                bbox=o.bbox,
                text_item_ids=tuple(sorted(inside)),
                source_object_id=o.object_id,
            )
        )
    # Nested rectangles: keep the innermost panel that still holds the text.
    kept: list[Panel] = []
    for p in panels:
        if any(q is not p and p.bbox.contains_box(q.bbox) and q.bbox.area < p.bbox.area for q in panels):
            continue
        kept.append(p)
    return tuple(canonical_sort(kept, key=lambda p: (p.bbox.key(), p.panel_id)))
