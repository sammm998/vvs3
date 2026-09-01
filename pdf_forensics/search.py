"""The search engine, and ``python -m pdf_forensics.search``.

    python -m pdf_forensics.search sheet.pdf --text S3-98
    python -m pdf_forensics.search sheet.pdf --near 412 380 --radius 40
    python -m pdf_forensics.search sheet.pdf --kind path --line-width 0.35 --vertical

Everything the pipeline does, it does through these searches, and every one of
them is available from the command line so that a person can ask the same
questions the engine asks.  Each hit reports its page, its object id, its type,
its bounding box, its coordinates, the transform in effect and where in the
file it came from.

Text and glyph searches build the glyph model, which is the expensive part;
they are therefore built lazily, so a search over paths costs nothing extra.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from .canonical import q, sort_canonical
from .geometry_search import GeometryModel, seg_bbox
from .glyphs import (GlyphModel, build_bank, embedded_font_buffers, extract_path_glyphs,
                     extract_text_glyphs, group_ink_lines, ink_components)
from .loader import LoadedPdf, load
from .model import Glyph, Hit, PdfObject, Segment, TextItem
from .objects import ObjectStore, extract
from .paths import PathModel
from .spatial_index import SpatialIndex
from .text_reconstruction import merge_duplicate_readings, reconstruct


class Microscope:
    """A searchable representation of one PDF."""

    def __init__(self, path: str | Path, *, allow_answer_file: bool = False) -> None:
        self.pdf = load(path, allow_answer_file=allow_answer_file)
        self.store = extract(self.pdf)
        self.paths = PathModel(self.store.drawing_objects())
        self.geometry = GeometryModel(self.paths.segments)
        self.object_index = SpatialIndex([(o.object_id, o.page, o.bbox) for o in self.store.objects])
        self._glyphs: Optional[GlyphModel] = None
        self._text: Optional[list[TextItem]] = None

    # -- lazily built stages ----------------------------------------------
    @property
    def glyphs(self) -> GlyphModel:
        if self._glyphs is None:
            components = ink_components(self.paths.segments)
            page_height = max((p.height for p in self.pdf.pages), default=1.0)
            lines = group_ink_lines(components, page_height)
            bank = build_bank(embedded_font_buffers(self.pdf))
            self._glyphs = GlyphModel(extract_text_glyphs(self.store.objects)
                                      + extract_path_glyphs(lines, components, bank))
        return self._glyphs

    @property
    def text(self) -> list[TextItem]:
        if self._text is None:
            items, _ = merge_duplicate_readings(reconstruct(self.glyphs.glyphs))
            self._text = items
        return self._text

    # -- the searches ------------------------------------------------------
    def search_objects(self, kind: Optional[str] = None, subtype: Optional[str] = None,
                       predicate: Optional[Callable[[PdfObject], bool]] = None) -> list[Hit]:
        out = []
        for obj in self.store.objects:
            if kind and obj.kind != kind:
                continue
            if subtype and obj.subtype != subtype:
                continue
            if predicate and not predicate(obj):
                continue
            out.append(_object_hit(obj))
        return _ordered(out)

    def search_paths(self, line_width: Optional[float] = None, colour: Optional[str] = None,
                     dashed: Optional[bool] = None, closed: Optional[bool] = None,
                     filled: Optional[bool] = None) -> list[Hit]:
        def matches(obj: PdfObject) -> bool:
            facts = self.paths.facts_of(obj.object_id)
            if facts is None:
                return False
            if line_width is not None and abs(facts.width - line_width) > 0.01:
                return False
            if colour is not None and json.dumps(obj.style.get("strokeColour")) != colour:
                return False
            if dashed is not None and facts.dashed != dashed:
                return False
            if closed is not None and facts.closed != closed:
                return False
            if filled is not None and facts.filled != filled:
                return False
            return True

        return self.search_objects(kind="path", predicate=matches)

    def search_text(self, pattern: str, regex: bool = False,
                    case_sensitive: bool = False) -> list[Hit]:
        flags = 0 if case_sensitive else re.IGNORECASE
        matcher = (re.compile(pattern, flags) if regex
                   else re.compile(re.escape(pattern), flags))
        out = [_text_hit(item) for item in self.text if matcher.search(item.text)]
        for item in self.text:                       # alternative readings count as hits
            if any(matcher.search(alt) for alt, _ in item.alternatives) and \
                    not matcher.search(item.text):
                hit = _text_hit(item)
                hit.detail["matchedAlternative"] = True
                out.append(hit)
        return _ordered(out)

    def search_glyphs(self, character: Optional[str] = None, page: Optional[int] = None,
                      bbox: Optional[Sequence[float]] = None,
                      unresolved: bool = False) -> list[Hit]:
        glyphs: Iterable[Glyph] = self.glyphs.glyphs
        if character:
            glyphs = [g for g in glyphs if g.character == character]
        if unresolved:
            glyphs = [g for g in glyphs if g.confidence < 0.25 or not g.character]
        if bbox is not None:
            page_number = page if page is not None else 0
            keys = set(self.glyphs.index.intersecting_bbox(page_number, bbox))
            glyphs = [g for g in glyphs if g.glyph_id in keys]
        elif page is not None:
            glyphs = [g for g in glyphs if g.page == page]
        return _ordered([_glyph_hit(g) for g in glyphs])

    def search_region(self, page: int, bbox: Sequence[float]) -> list[Hit]:
        keys = self.object_index.intersecting_bbox(page, bbox)
        return _ordered([_object_hit(self.store.by_id[k]) for k in keys])

    def search_near_point(self, page: int, point: Sequence[float], radius: float) -> list[Hit]:
        keys = self.object_index.near_point(page, point, radius)
        return _ordered([_object_hit(self.store.by_id[k]) for k in keys])

    def search_near_text(self, pattern: str, radius: float = 40.0, **kwargs) -> list[Hit]:
        out: list[Hit] = []
        for hit in self.search_text(pattern, **kwargs):
            out.extend(self.search_region(hit.page, _expand(hit.bbox, radius)))
        return _ordered(out)

    def search_geometry(self, page: Optional[int] = None, min_length: float = 0.0,
                        angle: Optional[float] = None, tolerance: float = 2.0,
                        line_width: Optional[float] = None) -> list[Hit]:
        from .geometry_search import angle_difference
        out = []
        for segment in self.geometry.segments:
            if page is not None and segment.page != page:
                continue
            if segment.length < min_length:
                continue
            if angle is not None and angle_difference(segment.angle, angle) > tolerance:
                continue
            if line_width is not None and abs(segment.width - line_width) > 0.01:
                continue
            out.append(_segment_hit(segment, self.paths))
        return _ordered(out)

    def search_vertical(self, page: Optional[int] = None, tolerance: float = 2.0) -> list[Hit]:
        return _ordered([_segment_hit(s, self.paths)
                         for s in self.geometry.vertical(page, tolerance)])

    def search_parallel(self, segment_id: str, max_distance: float = 40.0) -> list[Hit]:
        segment = self.geometry.by_id[segment_id]
        return _ordered([_segment_hit(s, self.paths)
                         for s in self.geometry.parallel_to(segment, max_distance=max_distance)])

    def search_collinear(self, segment_id: str) -> list[Hit]:
        segment = self.geometry.by_id[segment_id]
        return _ordered([_segment_hit(s, self.paths)
                         for s in self.geometry.collinear_with(segment)])

    def search_connected(self, segment_id: str, tolerance: float = 0.6) -> list[Hit]:
        segment = self.geometry.by_id[segment_id]
        return _ordered([_segment_hit(s, self.paths)
                         for s in self.geometry.connected_to(segment, tolerance)])

    def search_leaders(self) -> list[Hit]:
        from .leader_search import find_leaders
        leaders = find_leaders(self.text, self.geometry)
        return _ordered([
            Hit(page=leader.page, object_id=leader.leader_id, type="leader",
                bbox=_bbox_of_points(leader.polyline), coordinates=[list(p) for p in leader.polyline],
                transform=self.pdf.pages[leader.page].transform,
                source={"segmentIds": list(leader.segment_ids)},
                detail={"length": leader.length, "targetEnd": list(leader.target_end),
                        "confidence": leader.confidence})
            for leader in leaders])

    def search_dimensions(self) -> list[Hit]:
        from .dimensions import find_dimension_tokens
        tokens = find_dimension_tokens(self.text)
        return _ordered([
            Hit(page=token.page, object_id=token.token_id, type="dimension", bbox=token.bbox,
                coordinates=list(token.values_mm),
                transform=self.pdf.pages[token.page].transform,
                source={"textId": token.text_id, "rule": token.rule},
                detail={"text": token.text, "valuesMm": list(token.values_mm)})
            for token in tokens])

    def close(self) -> None:
        self.pdf.close()


def _expand(bbox: Sequence[float], pad: float) -> tuple[float, float, float, float]:
    return (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)


def _bbox_of_points(points: Sequence[Sequence[float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _ordered(hits: Sequence[Hit]) -> list[Hit]:
    seen: dict[tuple, Hit] = {}
    for hit in hits:
        seen.setdefault(hit.sort_key(), hit)
    return [seen[k] for k in sorted(seen)]


def _object_hit(obj: PdfObject) -> Hit:
    return Hit(page=obj.page, object_id=obj.object_id, type=obj.kind, bbox=obj.bbox,
               coordinates=obj.coordinates, transform=obj.transform,
               source=obj.source, detail={"subtype": obj.subtype, "style": obj.style})


def _glyph_hit(glyph: Glyph) -> Hit:
    return Hit(page=glyph.page, object_id=glyph.glyph_id, type="glyph", bbox=glyph.bbox,
               coordinates=[list(glyph.origin)], transform=glyph.transform,
               source={"origin": glyph.source, "objectIds": list(glyph.source_object_ids)},
               detail={"character": glyph.character, "confidence": glyph.confidence,
                       "alternatives": [[c, s] for c, s in glyph.alternatives],
                       "rotation": glyph.rotation, "font": glyph.font, "size": glyph.size})


def _text_hit(item: TextItem) -> Hit:
    return Hit(page=item.page, object_id=item.text_id, type="text", bbox=item.bbox,
               coordinates=[list(item.origin)], transform=(1.0, 0.0, 0.0, 1.0, *item.origin),
               source={"origin": item.source, "glyphIds": list(item.glyph_ids)},
               detail={"text": item.text, "confidence": item.confidence,
                       "rotation": item.rotation, "capHeight": item.cap_height,
                       "alternatives": [[t, s] for t, s in item.alternatives]})


def _segment_hit(segment: Segment, paths: PathModel) -> Hit:
    return Hit(page=segment.page, object_id=segment.segment_id, type="segment",
               bbox=seg_bbox(segment), coordinates=[list(segment.a), list(segment.b)],
               transform=(1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
               source={"pathIds": paths.paths_by_segment.get(segment.segment_id, []),
                       "pathId": segment.path_id},
               detail={"length": segment.length, "angle": segment.angle,
                       "lineWidth": segment.width, "dashed": segment.dashed,
                       "styleKey": segment.style_key})


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pdf_forensics.search",
        description="Search inside a vector PDF: text, glyphs, paths, geometry, regions.")
    parser.add_argument("pdf")
    parser.add_argument("--text", help="find a string (also matches alternative readings)")
    parser.add_argument("--regex", action="store_true", help="treat --text as a regular expression")
    parser.add_argument("--case-sensitive", action="store_true")
    parser.add_argument("--glyph", help="find one character")
    parser.add_argument("--unresolved-glyphs", action="store_true")
    parser.add_argument("--kind", help="object kind: path, glyph, text_span, image, annotation, form")
    parser.add_argument("--subtype")
    parser.add_argument("--page", type=int)
    parser.add_argument("--region", nargs=4, type=float, metavar=("X0", "Y0", "X1", "Y1"))
    parser.add_argument("--near", nargs=2, type=float, metavar=("X", "Y"))
    parser.add_argument("--near-text", metavar="STRING")
    parser.add_argument("--radius", type=float, default=40.0)
    parser.add_argument("--line-width", type=float)
    parser.add_argument("--colour", help="stroke colour as JSON, e.g. '[0.0]' or '[0,0,0]'")
    parser.add_argument("--dashed", action="store_true")
    parser.add_argument("--filled", action="store_true")
    parser.add_argument("--closed", action="store_true")
    parser.add_argument("--min-length", type=float, default=0.0)
    parser.add_argument("--angle", type=float, help="segment angle in degrees, 0..180")
    parser.add_argument("--tolerance", type=float, default=2.0)
    parser.add_argument("--vertical", action="store_true")
    parser.add_argument("--parallel-to", metavar="SEGMENT_ID")
    parser.add_argument("--collinear-with", metavar="SEGMENT_ID")
    parser.add_argument("--connected-to", metavar="SEGMENT_ID")
    parser.add_argument("--leaders", action="store_true")
    parser.add_argument("--dimensions", action="store_true")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    scope = Microscope(args.pdf)
    hits: list[Hit] = []
    if args.text:
        hits += scope.search_text(args.text, regex=args.regex, case_sensitive=args.case_sensitive)
    if args.near_text:
        hits += scope.search_near_text(args.near_text, radius=args.radius, regex=args.regex)
    if args.glyph or args.unresolved_glyphs:
        hits += scope.search_glyphs(character=args.glyph, page=args.page,
                                    bbox=args.region, unresolved=args.unresolved_glyphs)
    if args.kind and args.kind != "path":
        hits += scope.search_objects(kind=args.kind, subtype=args.subtype)
    if args.kind == "path" or args.colour or args.dashed or args.filled or args.closed \
            or (args.line_width is not None and not args.vertical and args.angle is None):
        hits += scope.search_paths(line_width=args.line_width, colour=args.colour,
                                   dashed=args.dashed or None, closed=args.closed or None,
                                   filled=args.filled or None)
    if args.region and not args.glyph:
        hits += scope.search_region(args.page or 0, args.region)
    if args.near:
        hits += scope.search_near_point(args.page or 0, args.near, args.radius)
    if args.vertical or args.angle is not None or args.min_length > 0.0:
        # geometric filters combine into one search rather than three unions,
        # so "--vertical --min-length 100" means both, not either
        angle = 90.0 if args.vertical and args.angle is None else args.angle
        hits += scope.search_geometry(page=args.page, min_length=args.min_length,
                                      angle=angle, tolerance=args.tolerance,
                                      line_width=args.line_width)
    if args.parallel_to:
        hits += scope.search_parallel(args.parallel_to, max_distance=args.radius)
    if args.collinear_with:
        hits += scope.search_collinear(args.collinear_with)
    if args.connected_to:
        hits += scope.search_connected(args.connected_to)
    if args.leaders:
        hits += scope.search_leaders()
    if args.dimensions:
        hits += scope.search_dimensions()

    hits = _ordered(hits)
    if args.json:
        print(json.dumps([h.to_json() for h in hits[:args.limit]], indent=2, sort_keys=True))
    else:
        print(f"{len(hits)} hit(s)" + (f", showing {min(len(hits), args.limit)}" if hits else ""))
        for hit in hits[:args.limit]:
            box = " ".join(f"{v:8.2f}" for v in hit.bbox)
            label = hit.detail.get("text") or hit.detail.get("character") or hit.type
            print(f"  p{hit.page} {hit.type:<9} [{box}]  {str(label)[:32]:<32} {hit.object_id}")
    scope.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
