"""Seventeenth search: everything around this object.

``inspect_neighbourhood`` is the tool the whole engine leans on.  Given an
object, a point or a region it returns every other thing the drawing has there:
text, glyphs, paths, segments, leaders, pipe candidates, graph nodes and edges,
dimensions, panels.  Local searches are what turn a global detection into an
argument about a particular place on a particular sheet.

The workspace it reads is duck-typed on purpose: the orchestrator fills it
stage by stage, and a neighbourhood asked early simply reports the stages that
exist so far.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from .canonical import q, qbbox, sort_canonical
from .model import Hit
from .spatial_index import SpatialIndex, bbox_distance, expand


def _box_of(entity: Any) -> Optional[tuple[float, float, float, float]]:
    bbox = getattr(entity, "bbox", None)
    if bbox is not None:
        return tuple(bbox)
    centerline = getattr(entity, "centerline", None) or getattr(entity, "polyline", None)
    if centerline:
        xs = [p[0] for p in centerline]
        ys = [p[1] for p in centerline]
        return (min(xs), min(ys), max(xs), max(ys))
    for attr in ("a", "point", "origin"):
        point = getattr(entity, attr, None)
        if point is not None:
            other = getattr(entity, "b", point)
            return (min(point[0], other[0]), min(point[1], other[1]),
                    max(point[0], other[0]), max(point[1], other[1]))
    return None


def _identity(entity: Any) -> str:
    for attr in ("object_id", "glyph_id", "segment_id", "text_id", "candidate_id",
                 "leader_id", "pipe_id", "run_id", "node_id", "edge_id", "panel_id",
                 "fragment_id", "evidence_id"):
        value = getattr(entity, attr, None)
        if value:
            return str(value)
    return repr(entity)


class Neighbourhood:
    """A region of one page and everything in it."""

    def __init__(self, page: int, bbox: Sequence[float], radius: float) -> None:
        self.page = page
        self.bbox = qbbox(bbox)
        self.radius = q(radius)
        self.groups: dict[str, list[Any]] = {}

    def add(self, name: str, entities: Sequence[Any]) -> None:
        if entities:
            self.groups[name] = list(entities)

    def counts(self) -> dict[str, int]:
        return {k: len(self.groups[k]) for k in sorted(self.groups)}

    def to_json(self, detail: bool = True) -> dict:
        payload: dict[str, Any] = {
            "page": self.page,
            "bbox": list(self.bbox),
            "radius": self.radius,
            "counts": self.counts(),
        }
        if detail:
            payload["contents"] = {
                name: [entity.to_json() if hasattr(entity, "to_json") else _identity(entity)
                       for entity in sort_canonical(items, key=lambda e: (_box_of(e) or (), _identity(e)))]
                for name, items in sorted(self.groups.items())
            }
        return payload


def _collect(entities: Sequence[Any], page: int, bbox: Sequence[float],
             radius: float) -> list[Any]:
    entries = []
    boxes: dict[str, tuple[float, float, float, float]] = {}
    for entity in entities:
        box = _box_of(entity)
        if box is None:
            continue
        entity_page = getattr(entity, "page", page)
        key = _identity(entity)
        boxes[key] = box
        entries.append((key, entity_page, box))
    if not entries:
        return []
    index = SpatialIndex(entries)
    by_key = {_identity(e): e for e in entities}
    keys = index.within_distance(page, bbox, radius)
    return [by_key[k] for k in keys if k in by_key]


def inspect_neighbourhood(workspace: Any, object_id: Optional[str] = None,
                          page: Optional[int] = None,
                          point: Optional[Sequence[float]] = None,
                          bbox: Optional[Sequence[float]] = None,
                          radius: Optional[float] = None) -> Neighbourhood:
    """Everything the drawing has around one object, point or region.

    ``radius`` defaults to a multiple of the target's own size, so the question
    scales with what is being asked about: a neighbourhood around a 5 pt label
    is not a neighbourhood around a 500 pt run.
    """
    target = None
    if object_id is not None:
        target = workspace.find(object_id)
        if target is None:
            raise KeyError(f"unknown object {object_id!r}")
        page = getattr(target, "page", page or 0)
        bbox = _box_of(target)
    if bbox is None:
        if point is None:
            raise ValueError("give an object_id, a point or a bbox")
        bbox = (point[0], point[1], point[0], point[1])
    if page is None:
        page = 0
    if radius is None:
        size = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
        radius = max(12.0, 2.5 * size)
    result = Neighbourhood(page, bbox, radius)
    for name, entities in workspace.collections().items():
        found = _collect(entities, page, bbox, radius)
        if object_id is not None:
            found = [e for e in found if _identity(e) != object_id]
        result.add(name, found)
    return result
