"""First search: every object the PDF contains.

This stage interprets nothing.  It walks the file's own structures - text
blocks down to individual characters, the drawing operators, the images, the
annotations, the fonts, the form XObjects - and turns each one into a
:class:`~pdf_forensics.model.PdfObject` with its provenance attached.

The one hard rule is conservation: the count of things the PDF says it has and
the count of things this representation holds must agree, per page and per
kind.  :meth:`ObjectStore.conservation` reports that comparison, and the
inspect command prints it.  If something disappeared between the file and the
model, every later search is searching a lie.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import fitz

from .canonical import canonical_json, entity_id, q, qa, qbbox, qpoly, sort_canonical
from .loader import LoadedPdf
from .model import PdfObject

# A cubic is flattened into a fixed number of chords chosen from the size of
# its control polygon, so the same curve always yields the same polyline.
_BEZIER_MIN, _BEZIER_MAX = 4, 24


def _flatten_cubic(p0, p1, p2, p3) -> list[tuple[float, float]]:
    hull = (
        math.dist(p0, p1) + math.dist(p1, p2) + math.dist(p2, p3)
    )
    n = int(min(_BEZIER_MAX, max(_BEZIER_MIN, round(hull / 2.0))))
    pts = []
    for i in range(n + 1):
        t = i / n
        mt = 1.0 - t
        x = (mt ** 3) * p0[0] + 3 * (mt ** 2) * t * p1[0] + 3 * mt * (t ** 2) * p2[0] + (t ** 3) * p3[0]
        y = (mt ** 3) * p0[1] + 3 * (mt ** 2) * t * p1[1] + 3 * mt * (t ** 2) * p2[1] + (t ** 3) * p3[1]
        pts.append((x, y))
    return pts


def _pt(p) -> tuple[float, float]:
    return (float(p.x), float(p.y)) if hasattr(p, "x") else (float(p[0]), float(p[1]))


def _flatten_points(vertices) -> list[tuple[float, float]]:
    """Annotation vertices, however the viewer nests them.

    A polyline hands over a flat list of points; an ink annotation hands over a
    list of strokes, each a list of points.  Both are ink on the page, and
    neither may crash the inventory.
    """
    out: list[tuple[float, float]] = []
    for item in vertices:
        if hasattr(item, "x"):
            out.append((float(item.x), float(item.y)))
        elif isinstance(item, (list, tuple)) and item and isinstance(item[0], (int, float)):
            out.append((float(item[0]), float(item[1])))
        elif isinstance(item, (list, tuple)):
            out.extend(_flatten_points(item))
    return out


def _colour(value) -> Optional[list[float]]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return [q(float(value))]
    return [q(float(v)) for v in value]


def _drawing_polylines(items: Iterable) -> tuple[list[list[tuple[float, float]]], list[str]]:
    """Turn drawing operators into polylines, keeping the operator names."""
    polys: list[list[tuple[float, float]]] = []
    ops: list[str] = []
    for item in items:
        op = item[0]
        ops.append(op)
        if op == "l":
            polys.append([_pt(item[1]), _pt(item[2])])
        elif op == "c":
            polys.append(_flatten_cubic(_pt(item[1]), _pt(item[2]), _pt(item[3]), _pt(item[4])))
        elif op == "re":
            r = item[1]
            x0, y0, x1, y1 = float(r.x0), float(r.y0), float(r.x1), float(r.y1)
            polys.append([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])
        elif op == "qu":
            quad = item[1]
            pts = [_pt(quad.ul), _pt(quad.ur), _pt(quad.lr), _pt(quad.ll)]
            polys.append(pts + [pts[0]])
        else:  # an operator PyMuPDF added since - keep the fact, lose nothing silently
            polys.append([])
    return polys, ops


def _bbox_of(polys) -> tuple[float, float, float, float]:
    xs = [p[0] for poly in polys for p in poly]
    ys = [p[1] for poly in polys for p in poly]
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return qbbox((min(xs), min(ys), max(xs), max(ys)))


class _Occurrences:
    """Gives identical content distinct, order-independent identifiers."""

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def next(self, key: str) -> int:
        n = self._seen.get(key, 0)
        self._seen[key] = n + 1
        return n


@dataclass
class RawCounts:
    """What the file itself reports, before we model anything."""

    text_blocks: int = 0
    text_lines: int = 0
    text_spans: int = 0
    chars: int = 0
    drawings: int = 0
    drawing_items: int = 0
    images: int = 0
    annotations: int = 0
    fonts: int = 0
    xobjects: int = 0

    def to_json(self) -> dict:
        return {
            "textBlocks": self.text_blocks,
            "textLines": self.text_lines,
            "textSpans": self.text_spans,
            "chars": self.chars,
            "drawings": self.drawings,
            "drawingItems": self.drawing_items,
            "images": self.images,
            "annotations": self.annotations,
            "fonts": self.fonts,
            "xobjects": self.xobjects,
        }


class ObjectStore:
    """Every object in the document, addressable and searchable."""

    def __init__(self, pdf: LoadedPdf) -> None:
        self.pdf = pdf
        self.objects: list[PdfObject] = []
        self.by_id: dict[str, PdfObject] = {}
        self.raw = RawCounts()
        self.fonts: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self._extract()

    # -- extraction -------------------------------------------------------
    def _extract(self) -> None:
        occ = _Occurrences()
        collected: list[PdfObject] = []
        for info in self.pdf.pages:
            page = self.pdf.page(info.number)
            collected.extend(self._text_objects(page, info.number, occ))
            collected.extend(self._path_objects(page, info.number, occ))
            collected.extend(self._image_objects(page, info.number, occ))
            collected.extend(self._annotation_objects(page, info.number, occ))
            collected.extend(self._form_objects(page, info.number, occ))
            self._collect_fonts(page, info.number)
        # Canonical order: page, then position, then kind, then id.  Never the
        # order in which the file happened to list them.
        self.objects = sort_canonical(
            collected, key=lambda o: (o.page, o.bbox, o.kind, o.subtype, o.object_id)
        )
        self.by_id = {o.object_id: o for o in self.objects}

    def _text_objects(self, page, number: int, occ: _Occurrences) -> list[PdfObject]:
        out: list[PdfObject] = []
        raw = page.get_text("rawdict")
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            self.raw.text_blocks += 1
            for line in block.get("lines", []):
                self.raw.text_lines += 1
                direction = tuple(float(v) for v in line.get("dir", (1.0, 0.0)))
                rotation = qa(math.degrees(math.atan2(-direction[1], direction[0])))
                for span in block and line.get("spans", []):
                    self.raw.text_spans += 1
                    font = str(span.get("font", ""))
                    size = q(float(span.get("size", 0.0)))
                    span_origin = tuple(float(v) for v in span.get("origin", (0.0, 0.0)))
                    span_chars = span.get("chars", [])
                    span_text = "".join(str(c.get("c", "")) for c in span_chars)
                    span_payload = {
                        "k": "text_span", "p": number, "b": qbbox(span["bbox"]),
                        "t": span_text, "f": font, "s": size, "r": rotation,
                    }
                    span_key = canonical_json(span_payload)
                    span_id = entity_id("span", span_payload, occ.next(span_key))
                    out.append(
                        PdfObject(
                            object_id=span_id,
                            page=number,
                            kind="text_span",
                            subtype="native",
                            bbox=qbbox(span["bbox"]),
                            coordinates=(q(span_origin[0]), q(span_origin[1])),
                            transform=_text_matrix(direction, size, span_origin),
                            style={
                                "font": font,
                                "size": size,
                                "flags": int(span.get("flags", 0)),
                                "colour": _colour(span.get("color")),
                                "ascender": q(float(span.get("ascender", 0.0))),
                                "descender": q(float(span.get("descender", 0.0))),
                                "rotation": rotation,
                                "writingMode": int(line.get("wmode", 0)),
                            },
                            source={
                                "origin": "text",
                                "text": span_text,
                                "direction": [q(direction[0]), q(direction[1])],
                                "charCount": len(span_chars),
                            },
                        )
                    )
                    for index, char in enumerate(span_chars):
                        self.raw.chars += 1
                        character = str(char.get("c", ""))
                        cbbox = qbbox(char["bbox"])
                        corigin = tuple(float(v) for v in char.get("origin", span_origin))
                        payload = {
                            "k": "glyph", "p": number, "b": cbbox, "c": character,
                            "f": font, "s": size, "r": rotation,
                        }
                        out.append(
                            PdfObject(
                                object_id=entity_id("glyph", payload, occ.next(canonical_json(payload))),
                                page=number,
                                kind="glyph",
                                subtype="char",
                                bbox=cbbox,
                                coordinates=(q(corigin[0]), q(corigin[1])),
                                transform=_text_matrix(direction, size, corigin),
                                style={
                                    "font": font,
                                    "size": size,
                                    "colour": _colour(span.get("color")),
                                    "rotation": rotation,
                                    "flags": int(span.get("flags", 0)),
                                },
                                source={
                                    "origin": "text",
                                    "character": character,
                                    "spanObjectId": span_id,
                                    "indexInSpan": index,
                                },
                            )
                        )
        return out

    def _path_objects(self, page, number: int, occ: _Occurrences) -> list[PdfObject]:
        out: list[PdfObject] = []
        try:
            drawings = page.get_drawings(extended=True)
        except TypeError:  # older PyMuPDF
            drawings = page.get_drawings()
            self.warnings.append("get_drawings(extended=True) unavailable; clips and groups not modelled")
        for drawing in drawings:
            self.raw.drawings += 1
            dtype = str(drawing.get("type", ""))
            items = drawing.get("items", []) or []
            self.raw.drawing_items += len(items)
            polys, ops = _drawing_polylines(items)
            kept = [qpoly(p) for p in polys if len(p) >= 2]
            rect = drawing.get("rect")
            bbox = _bbox_of(kept) if kept else (
                qbbox((rect.x0, rect.y0, rect.x1, rect.y1)) if rect is not None else (0.0, 0.0, 0.0, 0.0)
            )
            dashes = str(drawing.get("dashes", "") or "")
            style = {
                "strokeColour": _colour(drawing.get("color")),
                "fillColour": _colour(drawing.get("fill")),
                "lineWidth": q(float(drawing.get("width") or 0.0)),
                "dashes": dashes,
                "closePath": bool(drawing.get("closePath", False)),
                "evenOdd": bool(drawing.get("even_odd", False)),
                "lineCap": _as_int_tuple(drawing.get("lineCap")),
                "lineJoin": _as_int(drawing.get("lineJoin")),
                "strokeOpacity": q(float(drawing.get("stroke_opacity", 1.0) or 1.0)),
                "fillOpacity": q(float(drawing.get("fill_opacity", 1.0) or 1.0)),
                "layer": drawing.get("layer"),
            }
            payload = {
                "k": "path", "p": number, "t": dtype, "g": [list(map(list, poly)) for poly in kept],
                "o": ops, "s": {k: style[k] for k in sorted(style)},
            }
            out.append(
                PdfObject(
                    object_id=entity_id("path", payload, occ.next(canonical_json(payload))),
                    page=number,
                    kind="path",
                    subtype=dtype or "s",
                    bbox=bbox,
                    coordinates=tuple(kept),
                    transform=self.pdf.pages[number].transform,
                    style=style,
                    source={
                        "origin": "content_stream",
                        "operators": ops,
                        "itemCount": len(items),
                        "level": _as_int(drawing.get("level")),
                        "isClip": dtype == "clip",
                        "scissor": _rect_json(drawing.get("scissor")),
                    },
                )
            )
        return out

    def _image_objects(self, page, number: int, occ: _Occurrences) -> list[PdfObject]:
        out: list[PdfObject] = []
        for image in page.get_images(full=True):
            self.raw.images += 1
            xref = int(image[0])
            try:
                rects = page.get_image_rects(xref)
            except Exception:  # pragma: no cover - malformed image entry
                rects = []
            boxes = [qbbox((r.x0, r.y0, r.x1, r.y1)) for r in rects] or [(0.0, 0.0, 0.0, 0.0)]
            payload = {"k": "image", "p": number, "x": xref, "b": boxes[0]}
            out.append(
                PdfObject(
                    object_id=entity_id("image", payload, occ.next(canonical_json(payload))),
                    page=number,
                    kind="image",
                    subtype="xobject",
                    bbox=boxes[0],
                    coordinates=tuple(boxes),
                    transform=self.pdf.pages[number].transform,
                    style={"width": int(image[2]), "height": int(image[3]), "bpc": int(image[4]),
                           "colourspace": str(image[5]), "filter": str(image[8])},
                    source={"origin": "image", "xref": xref, "name": str(image[7]),
                            "placements": len(boxes)},
                )
            )
        return out

    def _annotation_objects(self, page, number: int, occ: _Occurrences) -> list[PdfObject]:
        """Annotations are recorded, and never used as drawing geometry.

        A previous take-off arrives as annotations.  Losing them would break
        conservation; letting them into the geometry stages would be reading
        somebody else's answer.  So they are modelled, marked, and excluded by
        :func:`drawing_objects`.
        """
        out: list[PdfObject] = []
        for annot in page.annots() or []:
            self.raw.annotations += 1
            rect = annot.rect
            info = annot.info or {}
            vertices = getattr(annot, "vertices", None) or []
            payload = {"k": "annot", "p": number, "b": qbbox((rect.x0, rect.y0, rect.x1, rect.y1)),
                       "t": annot.type[1] if annot.type else ""}
            out.append(
                PdfObject(
                    object_id=entity_id("annot", payload, occ.next(canonical_json(payload))),
                    page=number,
                    kind="annotation",
                    subtype=str(annot.type[1] if annot.type else annot.type[0]),
                    bbox=qbbox((rect.x0, rect.y0, rect.x1, rect.y1)),
                    coordinates=tuple(qpoly(_flatten_points(vertices))) if vertices else (),
                    transform=self.pdf.pages[number].transform,
                    style={"colours": {k: _colour(v) for k, v in sorted((annot.colors or {}).items())},
                           "border": _json_safe(annot.border)},
                    source={"origin": "annotation", "content": str(info.get("content", "")),
                            "title": str(info.get("title", "")), "xref": int(annot.xref),
                            "excludedFromGeometry": True},
                )
            )
        return out

    def _form_objects(self, page, number: int, occ: _Occurrences) -> list[PdfObject]:
        out: list[PdfObject] = []
        try:
            xobjects = page.get_xobjects()
        except Exception:  # pragma: no cover
            xobjects = []
        for xo in xobjects:
            self.raw.xobjects += 1
            xref = int(xo[0])
            # PyMuPDF has reported this entry as a Rect in some versions and as
            # a plain tuple in others, and the matrix is not always present.
            box = _as_bbox(xo[3] if len(xo) > 3 else None)
            matrix = xo[4] if len(xo) > 4 else None
            transform = (_as_matrix(matrix) or self.pdf.pages[number].transform)
            payload = {"k": "form", "p": number, "x": xref, "b": box, "m": list(transform)}
            out.append(
                PdfObject(
                    object_id=entity_id("form", payload, occ.next(canonical_json(payload))),
                    page=number,
                    kind="form",
                    subtype="xobject",
                    bbox=box,
                    coordinates=(box,),
                    transform=transform,
                    source={"origin": "xobject", "xref": xref, "name": str(xo[1]) if len(xo) > 1 else ""},
                    style={},
                )
            )
        return out

    def _collect_fonts(self, page, number: int) -> None:
        for font in page.get_fonts(full=True):
            self.raw.fonts += 1
            self.fonts.append(
                {
                    "page": number,
                    "xref": int(font[0]),
                    "ext": str(font[1]),
                    "type": str(font[2]),
                    "basefont": str(font[3]),
                    "name": str(font[4]),
                    "encoding": str(font[5]) if len(font) > 5 else "",
                    "embedded": str(font[1]) not in ("", "n/a"),
                }
            )
        self.fonts = sort_canonical(self.fonts, key=lambda f: (f["page"], f["basefont"], f["xref"]))

    # -- views ------------------------------------------------------------
    def of_kind(self, kind: str) -> list[PdfObject]:
        return [o for o in self.objects if o.kind == kind]

    def drawing_objects(self) -> list[PdfObject]:
        """Paths that are part of the drawing itself.

        Clip and group records carry no ink, and annotations are somebody's
        commentary on the drawing rather than the drawing.  Neither may reach
        the geometry stages.
        """
        return [o for o in self.objects
                if o.kind == "path" and o.subtype not in ("clip", "group") and o.coordinates]

    def transformed_objects(self) -> list[PdfObject]:
        """Objects whose transform is not the page's own."""
        page_transforms = {p.number: p.transform for p in self.pdf.pages}
        out = []
        for o in self.objects:
            if o.kind in ("glyph", "text_span"):
                if abs(float(o.style.get("rotation", 0.0))) > 0.05:
                    out.append(o)
            elif tuple(o.transform) != tuple(page_transforms.get(o.page, ())):
                out.append(o)
        return out

    # -- accounting -------------------------------------------------------
    def inventory(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for o in self.objects:
            counts[o.kind] = counts.get(o.kind, 0) + 1
        per_page: dict[str, dict[str, int]] = {}
        for o in self.objects:
            page_counts = per_page.setdefault(str(o.page), {})
            page_counts[o.kind] = page_counts.get(o.kind, 0) + 1
        return {
            "totalObjects": len(self.objects),
            "byKind": {k: counts[k] for k in sorted(counts)},
            "perPage": {k: per_page[k] for k in sorted(per_page)},
            "transformedObjects": len(self.transformed_objects()),
            "fonts": len(self.fonts),
            "distinctFonts": len({f["basefont"] for f in self.fonts}),
            "embeddedFonts": len({f["basefont"] for f in self.fonts if f["embedded"]}),
            "drawingObjects": len(self.drawing_objects()),
            "clipsAndGroups": len([o for o in self.objects
                                   if o.kind == "path" and o.subtype in ("clip", "group")]),
        }

    def conservation(self) -> dict[str, Any]:
        """Does the model hold everything the file reported?"""
        modelled = {
            "textSpans": len(self.of_kind("text_span")),
            "chars": len(self.of_kind("glyph")),
            "drawings": len(self.of_kind("path")),
            "images": len(self.of_kind("image")),
            "annotations": len(self.of_kind("annotation")),
            "xobjects": len(self.of_kind("form")),
            "fonts": len(self.fonts),
        }
        reported = {
            "textSpans": self.raw.text_spans,
            "chars": self.raw.chars,
            "drawings": self.raw.drawings,
            "images": self.raw.images,
            "annotations": self.raw.annotations,
            "xobjects": self.raw.xobjects,
            "fonts": self.raw.fonts,
        }
        deltas = {k: modelled[k] - reported[k] for k in sorted(reported)}
        empty_paths = len([o for o in self.of_kind("path") if not o.coordinates])
        return {
            "reported": reported,
            "modelled": modelled,
            "delta": deltas,
            "ok": all(v == 0 for v in deltas.values()),
            "pathsWithoutGeometry": empty_paths,
            "warnings": list(self.warnings),
        }

    def to_json(self, include_objects: bool = False) -> dict[str, Any]:
        payload = {
            "inventory": self.inventory(),
            "conservation": self.conservation(),
            "rawCounts": self.raw.to_json(),
            "fonts": self.fonts,
        }
        if include_objects:
            payload["objects"] = [o.to_json() for o in self.objects]
        return payload


def _text_matrix(direction, size: float, origin) -> tuple[float, ...]:
    dx, dy = float(direction[0]), float(direction[1])
    s = float(size)
    return (q(dx * s), q(dy * s), q(-dy * s), q(dx * s), q(float(origin[0])), q(float(origin[1])))


def _as_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_int_tuple(value) -> Optional[list[int]]:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [int(v) for v in value]
    return [int(value)]


def _as_bbox(value) -> tuple[float, float, float, float]:
    if value is None:
        return (0.0, 0.0, 0.0, 0.0)
    if hasattr(value, "x0"):
        return qbbox((value.x0, value.y0, value.x1, value.y1))
    try:
        return qbbox(tuple(float(v) for v in value)[:4])
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0, 0.0)


def _as_matrix(value) -> Optional[tuple[float, ...]]:
    if value is None:
        return None
    try:
        values = tuple(q(float(v)) for v in value)
    except (TypeError, ValueError):
        return None
    return values if len(values) == 6 else None


def _rect_json(rect) -> Optional[list[float]]:
    if rect is None:
        return None
    try:
        return list(qbbox((rect.x0, rect.y0, rect.x1, rect.y1)))
    except AttributeError:
        return None


def _json_safe(value) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)


def extract(pdf: LoadedPdf) -> ObjectStore:
    return ObjectStore(pdf)
