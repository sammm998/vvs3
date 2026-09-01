"""Spatial index.

A sheet holds tens of thousands of objects, and the local searches that make
this engine work ask thousands of questions about small regions.  Doing that by
scanning every object would be O(N^2) and would make the local searches
unaffordable, so everything is indexed once and queried by region afterwards.

Two interchangeable backends are provided: shapely's ``STRtree`` when shapely
is installed, and a uniform grid otherwise.  They answer identically - the
query result is defined by bbox intersection, and both backends are filtered
through the same exact test before returning - and
``tests/pdf_forensics/test_spatial_index.py`` asserts that on real drawings.
Every result is returned in canonical key order, never in tree order.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Optional, Sequence

try:  # pragma: no cover - exercised by whichever backend is installed
    from shapely import STRtree
    from shapely.geometry import box as _shapely_box
    _HAVE_SHAPELY = True
except Exception:  # pragma: no cover
    _HAVE_SHAPELY = False

from .canonical import q, qbbox

BBox = tuple[float, float, float, float]


def bbox_intersects(a: Sequence[float], b: Sequence[float], pad: float = 0.0) -> bool:
    return not (
        a[2] + pad < b[0] or b[2] + pad < a[0] or a[3] + pad < b[1] or b[3] + pad < a[1]
    )


def bbox_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """Distance between two axis-aligned boxes; zero when they touch."""
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def expand(bbox: Sequence[float], pad: float) -> BBox:
    return (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)


def point_bbox(point: Sequence[float], radius: float = 0.0) -> BBox:
    return (point[0] - radius, point[1] - radius, point[0] + radius, point[1] + radius)


class SpatialIndex:
    """Bounding-box index over any keyed collection."""

    def __init__(self, entries: Iterable[tuple[str, int, Sequence[float]]], backend: str = "auto") -> None:
        self.entries: dict[str, tuple[int, BBox]] = {}
        for key, page, bbox in entries:
            self.entries[key] = (int(page), qbbox(bbox))
        self.backend = "grid" if backend == "grid" or (backend == "auto" and not _HAVE_SHAPELY) else "strtree"
        if backend == "strtree" and not _HAVE_SHAPELY:
            raise RuntimeError("shapely is not installed; use backend='grid'")
        self._keys_by_page: dict[int, list[str]] = {}
        for key in sorted(self.entries):
            page, _ = self.entries[key]
            self._keys_by_page.setdefault(page, []).append(key)
        self._grid: dict[tuple[int, int, int], list[str]] = {}
        self._trees: dict[int, Any] = {}
        self._tree_keys: dict[int, list[str]] = {}
        self.cell = self._choose_cell()
        if self.backend == "strtree":
            self._build_trees()
        else:
            self._build_grid()

    # -- construction -----------------------------------------------------
    def _choose_cell(self) -> float:
        extents = sorted(
            max(b[2] - b[0], b[3] - b[1]) for _, b in self.entries.values()
        )
        if not extents:
            return 32.0
        median = extents[len(extents) // 2]
        return max(4.0, round(median * 4.0, 3))

    def _cells(self, bbox: Sequence[float]) -> Iterable[tuple[int, int]]:
        x0 = int(math.floor(bbox[0] / self.cell))
        x1 = int(math.floor(bbox[2] / self.cell))
        y0 = int(math.floor(bbox[1] / self.cell))
        y1 = int(math.floor(bbox[3] / self.cell))
        for cx in range(x0, x1 + 1):
            for cy in range(y0, y1 + 1):
                yield cx, cy

    def _build_grid(self) -> None:
        for key in sorted(self.entries):
            page, bbox = self.entries[key]
            for cx, cy in self._cells(bbox):
                self._grid.setdefault((page, cx, cy), []).append(key)

    def _build_trees(self) -> None:
        for page, keys in sorted(self._keys_by_page.items()):
            geoms = [_shapely_box(*self.entries[k][1]) for k in keys]
            self._trees[page] = STRtree(geoms)
            self._tree_keys[page] = keys

    # -- queries ----------------------------------------------------------
    def keys_on_page(self, page: int) -> list[str]:
        return list(self._keys_by_page.get(page, ()))

    def _candidates(self, page: int, bbox: Sequence[float]) -> list[str]:
        if self.backend == "strtree":
            tree = self._trees.get(page)
            if tree is None:
                return []
            keys = self._tree_keys[page]
            hits = tree.query(_shapely_box(*bbox))
            return [keys[int(i)] for i in hits]
        seen: set[str] = set()
        for cx, cy in self._cells(bbox):
            for key in self._grid.get((page, cx, cy), ()):  # pragma: no branch
                seen.add(key)
        return list(seen)

    def intersecting_bbox(self, page: int, bbox: Sequence[float], pad: float = 0.0) -> list[str]:
        """Keys whose bbox intersects ``bbox`` (optionally grown by ``pad``)."""
        query = expand(bbox, pad)
        out = [k for k in self._candidates(page, query)
               if bbox_intersects(self.entries[k][1], query)]
        return sorted(out)

    def within_distance(self, page: int, bbox: Sequence[float], distance: float) -> list[str]:
        """Keys whose bbox lies within ``distance`` of ``bbox``."""
        query = expand(bbox, distance)
        out = [k for k in self._candidates(page, query)
               if bbox_distance(self.entries[k][1], bbox) <= distance + 1e-9]
        return sorted(out)

    def near_point(self, page: int, point: Sequence[float], radius: float) -> list[str]:
        return self.within_distance(page, point_bbox(point), radius)

    def nearest(self, page: int, point: Sequence[float], k: int = 1, max_radius: float = 1e6) -> list[str]:
        """The ``k`` nearest keys, ties broken by key so the answer is stable."""
        radius = max(self.cell, 1.0)
        found: list[str] = []
        while radius <= max_radius:
            found = self.near_point(page, point, radius)
            if len(found) >= k:
                break
            radius *= 2.0
        scored = sorted(
            ((bbox_distance(self.entries[key][1], point_bbox(point)), key) for key in found),
            key=lambda pair: (round(pair[0], 6), pair[1]),
        )
        return [key for _, key in scored[:k]]

    def all_pairs_within(self, distance: float) -> list[tuple[str, str]]:
        """Every pair of keys closer than ``distance``, computed by region.

        The point of the index: this is the operation a naive implementation
        writes as a double loop.
        """
        pairs: set[tuple[str, str]] = set()
        for key in sorted(self.entries):
            page, bbox = self.entries[key]
            for other in self.within_distance(page, bbox, distance):
                if other != key:
                    pairs.add((key, other) if key < other else (other, key))
        return sorted(pairs)

    def to_json(self) -> dict:
        return {
            "backend": self.backend,
            "entries": len(self.entries),
            "cell": self.cell,
            "pages": sorted(self._keys_by_page),
        }
