"""Traced-artwork suppression.

A real sheet carries a company logo, a north arrow or a scanned detail that was
vectorised from a bitmap.  Such a region holds thousands of tiny filled paths
packed into a few square centimetres - on the reference sheet, a logo of 13 963
objects at 968 objects per 1000 pt^2, where every other region of the drawing
sits below 36.  Left in, it dominates the text stage's height statistics, forms
spurious glyph clusters and costs most of the analysis time, while carrying no
drawing information at all.

The detector is deliberately statistical and layer-independent: it grids the
sheet, finds cells whose object density is far above the sheet's own norm *and*
whose objects are overwhelmingly filled, and merges those cells into regions.
Nothing about a particular logo, layer name or corner of the sheet is assumed,
and the regions are reported in the diagnostics so the exclusion is auditable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .canonical import canonical_sort, qs
from .geometry.index import connected_components
from .geometry.primitives import BBox
from .model import VectorObject


@dataclass(frozen=True, slots=True)
class ArtworkConfig:
    cell_pt: float = 24.0
    density_multiple: float = 25.0   # times the median non-empty cell density
    min_cell_objects: int = 40
    min_filled_fraction: float = 0.60
    max_region_area_ratio: float = 0.06  # of the page


@dataclass(frozen=True, slots=True)
class ArtworkRegion:
    bbox: BBox
    object_count: int
    density_per_1000pt2: float
    filled_fraction: float

    def to_canonical(self) -> dict:
        return {
            "bbox": self.bbox.to_canonical(),
            "objectCount": self.object_count,
            "densityPer1000Pt2": qs(self.density_per_1000pt2),
            "filledFraction": qs(self.filled_fraction),
        }


def detect_artwork(
    objects: Sequence[VectorObject],
    page_box: BBox,
    cfg: ArtworkConfig | None = None,
) -> tuple[tuple[ArtworkRegion, ...], frozenset[str]]:
    """Return the artwork regions and the ids of the objects inside them."""
    cfg = cfg or ArtworkConfig()
    if not objects:
        return (), frozenset()

    cells: dict[tuple[int, int], list[int]] = {}
    for i, o in enumerate(objects):
        cx = int(o.bbox.center[0] // cfg.cell_pt)
        cy = int(o.bbox.center[1] // cfg.cell_pt)
        cells.setdefault((cx, cy), []).append(i)
    if not cells:
        return (), frozenset()

    counts = sorted(len(v) for v in cells.values())
    median = counts[len(counts) // 2]
    threshold = max(cfg.min_cell_objects, median * cfg.density_multiple)

    hot = []
    for cell in sorted(cells):
        members = cells[cell]
        if len(members) < threshold:
            continue
        filled = sum(1 for i in members if objects[i].is_filled) / len(members)
        if filled < cfg.min_filled_fraction:
            continue
        hot.append(cell)
    if not hot:
        return (), frozenset()

    # Merge touching hot cells into regions.
    pos = {cell: k for k, cell in enumerate(hot)}
    edges: list[tuple[int, int]] = []
    for cell in hot:
        for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
            other = (cell[0] + dx, cell[1] + dy)
            if other in pos:
                edges.append((pos[cell], pos[other]))
    comps = connected_components(len(hot), edges)

    regions: list[ArtworkRegion] = []
    excluded: set[str] = set()
    for comp in comps:
        members = [i for k in comp for i in cells[hot[k]]]
        box = BBox.union_all([objects[i].bbox for i in members])
        if box.area > cfg.max_region_area_ratio * page_box.area:
            continue  # too big to be a badge; leave it to the normal stages
        inside = [i for i, o in enumerate(objects) if box.contains_box(o.bbox)]
        filled = sum(1 for i in inside if objects[i].is_filled) / max(1, len(inside))
        regions.append(
            ArtworkRegion(
                bbox=box,
                object_count=len(inside),
                density_per_1000pt2=len(inside) / max(box.area, 1.0) * 1000.0,
                filled_fraction=filled,
            )
        )
        excluded.update(objects[i].object_id for i in inside)

    regions = canonical_sort(regions, key=lambda r: r.bbox.key())
    return tuple(regions), frozenset(excluded)
