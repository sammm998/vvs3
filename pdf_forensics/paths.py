"""Paths, reduced to straight segments without deciding what they mean.

A CAD export says almost nothing about intent: a pipe wall, a leader, a door
swing and a letter stroke are all the same operator.  So this stage answers
only mechanical questions - where does this piece of ink start and end, how
wide is it, is it dashed, is it filled - and leaves every interpretation to the
searches that come later.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .canonical import canonical_json, entity_id, q, qa, qbbox, sort_canonical, undirected
from .model import PdfObject, Segment

# Below this a segment is a rendering artefact, not a line anybody drew.
MIN_SEGMENT_LENGTH = 0.05


def style_key(obj: PdfObject) -> str:
    """Ink identity: two paths with the same key were drawn with the same pen."""
    style = obj.style
    return canonical_json(
        {
            "s": style.get("strokeColour"),
            "f": style.get("fillColour"),
            "w": q(float(style.get("lineWidth") or 0.0)),
            "d": style.get("dashes") or "",
        }
    )


def is_dashed(obj: PdfObject) -> bool:
    dashes = str(obj.style.get("dashes") or "").strip()
    if not dashes or dashes in ("[] 0", "[]0", "[ ] 0"):
        return False
    inside = dashes[dashes.find("[") + 1: dashes.find("]")] if "[" in dashes else dashes
    return any(tok.strip() not in ("", "0") for tok in inside.replace(",", " ").split())


def is_stroked(obj: PdfObject) -> bool:
    return obj.subtype in ("s", "fs") or obj.style.get("strokeColour") is not None


def is_filled(obj: PdfObject) -> bool:
    return obj.subtype in ("f", "fs") or obj.style.get("fillColour") is not None


def angle_of(a, b) -> float:
    """Direction-independent angle in degrees, in ``[0, 180)``."""
    ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
    return qa(ang % 180.0) % 180.0


def segments_of(obj: PdfObject) -> list[Segment]:
    """Every straight piece of one path object."""
    out: list[Segment] = []
    key = style_key(obj)
    dashed = is_dashed(obj)
    width = q(float(obj.style.get("lineWidth") or 0.0))
    for poly_index, poly in enumerate(obj.coordinates):
        for i in range(len(poly) - 1):
            a, b = poly[i], poly[i + 1]
            length = math.dist(a, b)
            if length < MIN_SEGMENT_LENGTH:
                continue
            geom = undirected([a, b])
            payload = {"p": obj.page, "g": [list(geom[0]), list(geom[1])], "w": width, "k": key}
            out.append(
                Segment(
                    segment_id=entity_id("seg", payload),
                    page=obj.page,
                    a=geom[0],
                    b=geom[1],
                    length=q(length),
                    angle=angle_of(geom[0], geom[1]),
                    width=width,
                    style_key=key,
                    path_id=obj.object_id,
                    dashed=dashed,
                )
            )
    return out


@dataclass(frozen=True)
class PathFacts:
    """What a path object is, mechanically."""

    object_id: str
    page: int
    bbox: tuple[float, float, float, float]
    segment_ids: tuple[str, ...]
    ink_length: float
    span: float                    # diagonal of the bbox
    aspect: float
    width: float
    style_key: str
    stroked: bool
    filled: bool
    dashed: bool
    closed: bool
    vertex_count: int
    distinct_angles: int

    def to_json(self) -> dict:
        return {
            "objectId": self.object_id,
            "page": self.page,
            "bbox": list(self.bbox),
            "segmentIds": list(self.segment_ids),
            "inkLength": self.ink_length,
            "span": self.span,
            "aspect": self.aspect,
            "lineWidth": self.width,
            "styleKey": self.style_key,
            "stroked": self.stroked,
            "filled": self.filled,
            "dashed": self.dashed,
            "closed": self.closed,
            "vertexCount": self.vertex_count,
            "distinctAngles": self.distinct_angles,
        }


class PathModel:
    """Segments and per-path facts for the whole document."""

    def __init__(self, objects: Iterable[PdfObject]) -> None:
        self.segments: list[Segment] = []
        self.facts: dict[str, PathFacts] = {}
        self.segments_by_path: dict[str, list[Segment]] = {}
        collected: list[Segment] = []
        for obj in objects:
            segs = segments_of(obj)
            self.segments_by_path[obj.object_id] = segs
            collected.extend(segs)
            self.facts[obj.object_id] = _facts(obj, segs)
        self.segments = sort_canonical(
            collected, key=lambda s: (s.page, s.a, s.b, s.width, s.style_key, s.segment_id)
        )
        self.by_id: dict[str, Segment] = {}
        for seg in self.segments:
            # Identical geometry drawn twice is one segment: the drawing has one
            # line there.  The paths that produced it are kept in the fact table.
            self.by_id.setdefault(seg.segment_id, seg)
        self.segments = [self.by_id[k] for k in sorted(self.by_id)]
        self.segments = sort_canonical(
            self.segments, key=lambda s: (s.page, s.a, s.b, s.width, s.style_key, s.segment_id)
        )
        self.paths_by_segment: dict[str, list[str]] = {}
        for path_id, segs in sorted(self.segments_by_path.items()):
            for seg in segs:
                self.paths_by_segment.setdefault(seg.segment_id, []).append(path_id)
        for key in self.paths_by_segment:
            self.paths_by_segment[key] = sorted(set(self.paths_by_segment[key]))

    def facts_of(self, object_id: str) -> Optional[PathFacts]:
        return self.facts.get(object_id)

    def stroke_width_histogram(self) -> dict[str, int]:
        hist: dict[str, int] = {}
        for seg in self.segments:
            k = f"{seg.width:.3f}"
            hist[k] = hist.get(k, 0) + 1
        return {k: hist[k] for k in sorted(hist, key=float)}

    def to_json(self) -> dict:
        return {
            "segments": len(self.segments),
            "paths": len(self.facts),
            "dashedPaths": len([f for f in self.facts.values() if f.dashed]),
            "filledPaths": len([f for f in self.facts.values() if f.filled]),
            "closedPaths": len([f for f in self.facts.values() if f.closed]),
            "strokeWidths": self.stroke_width_histogram(),
            "inkLength": q(sum(s.length for s in self.segments)),
        }


def _facts(obj: PdfObject, segs: list[Segment]) -> PathFacts:
    xs = [p[0] for poly in obj.coordinates for p in poly]
    ys = [p[1] for poly in obj.coordinates for p in poly]
    bbox = qbbox((min(xs), min(ys), max(xs), max(ys))) if xs else obj.bbox
    w = max(bbox[2] - bbox[0], 1e-6)
    h = max(bbox[3] - bbox[1], 1e-6)
    closed = any(len(poly) > 2 and poly[0] == poly[-1] for poly in obj.coordinates) or bool(
        obj.style.get("closePath")
    )
    return PathFacts(
        object_id=obj.object_id,
        page=obj.page,
        bbox=bbox,
        segment_ids=tuple(s.segment_id for s in segs),
        ink_length=q(sum(s.length for s in segs)),
        span=q(math.hypot(w, h)),
        aspect=q(max(w, h) / min(w, h)),
        width=q(float(obj.style.get("lineWidth") or 0.0)),
        style_key=style_key(obj),
        stroked=is_stroked(obj),
        filled=is_filled(obj),
        dashed=is_dashed(obj),
        closed=closed,
        vertex_count=sum(len(poly) for poly in obj.coordinates),
        distinct_angles=len({s.angle for s in segs}),
    )
