"""Deterministic uniform-grid spatial index.

Used instead of a bare O(n^2) sweep for every pairwise stage of the pipeline.
A uniform grid is chosen over an R-tree for the primary index because:

* bucket assignment is a pure function of coordinates, so the index is
  invariant under insertion order (an R-tree's node layout is not);
* queries return *sets* of payload keys which the caller sorts canonically,
  so no result ever depends on traversal order;
* CAD geometry is close to uniformly distributed over the sheet, which is the
  case a grid handles well.

Complexity: build O(n + sum(cells_per_item)), query O(cells_in_window + hits).
``shapely.STRtree`` is used separately in
:mod:`vvs_pipe.pipes.detection` for polygon-level queries where a bounding
volume hierarchy is a better fit; its results are likewise canonically sorted.
"""

from __future__ import annotations

import math
from typing import Callable, Generic, Hashable, Iterable, TypeVar

from .primitives import BBox

T = TypeVar("T")


class SpatialIndex(Generic[T]):
    __slots__ = ("_cell", "_buckets", "_items", "_bounds", "_count")

    def __init__(self, cell_size: float) -> None:
        if not (cell_size > 0):
            raise ValueError("cell_size must be > 0")
        self._cell = float(cell_size)
        self._buckets: dict[tuple[int, int], list[int]] = {}
        self._items: list[tuple[BBox, T]] = []
        self._count = 0

    @staticmethod
    def for_items(items: Iterable[tuple[BBox, T]], target_per_cell: float = 4.0) -> "SpatialIndex[T]":
        """Build with a cell size derived from the data (median box extent)."""
        mat = list(items)
        if not mat:
            return SpatialIndex(1.0)
        extents = sorted(max(b.width, b.height, 1e-6) for b, _ in mat)
        median = extents[len(extents) // 2]
        cell = max(median * max(1.0, target_per_cell), 1e-3)
        idx: SpatialIndex[T] = SpatialIndex(cell)
        for b, payload in mat:
            idx.insert(b, payload)
        return idx

    def _cells(self, b: BBox) -> Iterable[tuple[int, int]]:
        c = self._cell
        i0 = math.floor(b.x0 / c)
        i1 = math.floor(b.x1 / c)
        j0 = math.floor(b.y0 / c)
        j1 = math.floor(b.y1 / c)
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                yield (i, j)

    def insert(self, box: BBox, payload: T) -> None:
        idx = len(self._items)
        self._items.append((box, payload))
        for cell in self._cells(box):
            self._buckets.setdefault(cell, []).append(idx)
        self._count += 1

    def __len__(self) -> int:
        return self._count

    def query_box(self, box: BBox) -> list[T]:
        """All payloads whose bbox intersects ``box``.

        The returned list is deduplicated; callers must sort it canonically -
        this method makes no ordering promise beyond determinism for a fixed
        insertion sequence, and the pipeline never relies on that.
        """
        seen: set[int] = set()
        out: list[T] = []
        for cell in self._cells(box):
            for i in self._buckets.get(cell, ()):
                if i in seen:
                    continue
                seen.add(i)
                if self._items[i][0].intersects(box):
                    out.append(self._items[i][1])
        return out

    def query_point(self, p: tuple[float, float], radius: float = 0.0) -> list[T]:
        return self.query_box(BBox(p[0] - radius, p[1] - radius, p[0] + radius, p[1] + radius))

    def pairs(self, expand: float = 0.0) -> set[tuple[int, int]]:
        """Candidate index pairs whose (expanded) boxes intersect.

        Returns a set of ``(i, j)`` with ``i < j`` - a set, so the caller is
        forced to impose a canonical order before using it.
        """
        out: set[tuple[int, int]] = set()
        for _cell, members in self._buckets.items():
            if len(members) < 2:
                continue
            for a_pos in range(len(members)):
                ia = members[a_pos]
                ba = self._items[ia][0].expanded(expand)
                for b_pos in range(a_pos + 1, len(members)):
                    ib = members[b_pos]
                    if ia == ib:
                        continue
                    if ba.intersects(self._items[ib][0].expanded(expand)):
                        out.add((ia, ib) if ia < ib else (ib, ia))
        return out

    def items(self) -> list[tuple[BBox, T]]:
        return list(self._items)


def union_find(n: int) -> tuple[Callable[[int], int], Callable[[int, int], None]]:
    """Union-find over ``n`` elements; deterministic (union by min root)."""
    parent = list(range(n))

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        # union by *value* (min root wins) - independent of call order
        if ra < rb:
            parent[rb] = ra
        else:
            parent[ra] = rb

    return find, union


def connected_components(n: int, edges: Iterable[tuple[int, int]]) -> list[list[int]]:
    """Components as sorted member lists, themselves sorted by first member."""
    find, union = union_find(n)
    for a, b in edges:
        union(a, b)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    comps = [sorted(v) for v in groups.values()]
    comps.sort(key=lambda c: c[0])
    return comps


def group_by_key(items: Iterable[T], key: Callable[[T], Hashable]) -> list[tuple[Hashable, list[T]]]:
    """Group and return groups sorted by key - never by encounter order."""
    buckets: dict[Hashable, list[T]] = {}
    for it in items:
        buckets.setdefault(key(it), []).append(it)
    return sorted(buckets.items(), key=lambda kv: kv[0])  # type: ignore[arg-type]
