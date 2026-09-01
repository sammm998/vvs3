"""PDF content-stream -> canonical vector IR.

Design decisions that matter for correctness of the whole engine:

* **Annotations are removed before the content stream is parsed.**  PyMuPDF's
  ``Page.get_drawings()`` walks annotation appearance streams as well as page
  content, so previous manual take-off stored as ``/Annots`` would otherwise
  enter the geometry.  Every annotation is therefore deleted from the in-memory
  document (the file on disk is untouched) *before* any path is read, and the
  number of drawings that disappear is reported.
* **Flattened markup is removed by exact geometric match.**  If a markup
  annotation declares vertices (``Line``/``PolyLine``/``Polygon``/``Ink``) or a
  rectangle (``Square``), any content-stream object whose canonical geometry
  matches it is dropped and counted.  This is an exact match, not a heuristic:
  nothing is discarded on a guess.
* **Curves are flattened deterministically** with a fixed chord tolerance, so
  the IR contains polylines only and every later stage is exact.
* **Object ids are content addresses.**  They are derived from geometry and
  paint attributes, never from the order in which the content stream happened
  to emit them, so a permuted extraction produces identical ids.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import fitz

from ..canonical import canonical_sort, entity_id, qc
from ..geometry.primitives import BBox
from ..model import PageInfo, TextSpan, VectorDocument, VectorObject

Pt = tuple[float, float]

MARKUP_SUBTYPES = frozenset(
    {"Square", "Circle", "Line", "PolyLine", "Polygon", "Ink", "Highlight", "FreeText", "Text", "StrikeOut", "Underline", "Squiggly", "Stamp", "Caret", "FileAttachment", "Popup"}
)


@dataclass(frozen=True, slots=True)
class ExtractionConfig:
    """All extraction tolerances live here, so they are reviewable in one place."""

    curve_flatten_tolerance_pt: float = 0.05
    max_curve_subdivisions: int = 6
    min_object_extent_pt: float = 1e-4
    drop_flattened_annotation_geometry: bool = True
    annotation_match_tolerance_pt: float = 0.05


def sha256_of(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _norm_color(c: Any) -> tuple[float, float, float] | None:
    if c is None:
        return None
    try:
        seq = list(c)
    except TypeError:
        return None
    if len(seq) == 1:
        g = float(seq[0])
        seq = [g, g, g]
    if len(seq) < 3:
        return None
    return (qc(float(seq[0])), qc(float(seq[1])), qc(float(seq[2])))


def _flatten_cubic(p0: Pt, p1: Pt, p2: Pt, p3: Pt, cfg: ExtractionConfig) -> list[Pt]:
    """Adaptive-depth bezier flattening with a *fixed* recursion budget.

    The number of subdivisions depends only on the control points, so the
    result is bit-identical across runs and machines.
    """

    def flat_enough(a: Pt, b: Pt, c: Pt, d: Pt) -> bool:
        # distance of control points from the chord
        ax, ay = a
        dx, dy = d[0] - ax, d[1] - ay
        den = math.hypot(dx, dy)
        if den < 1e-12:
            return max(math.hypot(b[0] - ax, b[1] - ay), math.hypot(c[0] - ax, c[1] - ay)) <= cfg.curve_flatten_tolerance_pt
        d1 = abs((b[0] - ax) * dy - (b[1] - ay) * dx) / den
        d2 = abs((c[0] - ax) * dy - (c[1] - ay) * dx) / den
        return max(d1, d2) <= cfg.curve_flatten_tolerance_pt

    def rec(a: Pt, b: Pt, c: Pt, d: Pt, depth: int) -> list[Pt]:
        if depth >= cfg.max_curve_subdivisions or flat_enough(a, b, c, d):
            return [d]
        ab = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        bc = ((b[0] + c[0]) / 2, (b[1] + c[1]) / 2)
        cd = ((c[0] + d[0]) / 2, (c[1] + d[1]) / 2)
        abc = ((ab[0] + bc[0]) / 2, (ab[1] + bc[1]) / 2)
        bcd = ((bc[0] + cd[0]) / 2, (bc[1] + cd[1]) / 2)
        mid = ((abc[0] + bcd[0]) / 2, (abc[1] + bcd[1]) / 2)
        return rec(a, ab, abc, mid, depth + 1) + rec(mid, bcd, cd, d, depth + 1)

    return rec(p0, p1, p2, p3, 0)


def _pt(p: Any) -> Pt:
    return (float(p.x), float(p.y)) if hasattr(p, "x") else (float(p[0]), float(p[1]))


def _subpaths_from_items(items: Sequence[Any], cfg: ExtractionConfig) -> list[tuple[str, list[Pt], bool]]:
    """Split a PyMuPDF drawing's items into (kind, polyline, closed) subpaths."""
    out: list[tuple[str, list[Pt], bool]] = []
    cur: list[Pt] = []
    cur_kind = "line"

    def flush() -> None:
        nonlocal cur, cur_kind
        if len(cur) >= 2:
            out.append((cur_kind, cur, False))
        cur = []
        cur_kind = "line"

    for it in items:
        op = it[0]
        if op == "l":
            a, b = _pt(it[1]), _pt(it[2])
            if cur and cur[-1] == a:
                cur.append(b)
            else:
                flush()
                cur = [a, b]
                cur_kind = "line"
        elif op == "c":
            a, c1, c2, b = _pt(it[1]), _pt(it[2]), _pt(it[3]), _pt(it[4])
            pts = _flatten_cubic(a, c1, c2, b, cfg)
            if cur and cur[-1] == a:
                cur.extend(pts)
                cur_kind = "curve"
            else:
                flush()
                cur = [a] + pts
                cur_kind = "curve"
        elif op == "re":
            flush()
            r = it[1]
            x0, y0, x1, y1 = float(r.x0), float(r.y0), float(r.x1), float(r.y1)
            out.append(("rect", [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], True))
        elif op == "qu":
            flush()
            quad = it[1]
            pts = [_pt(quad.ul), _pt(quad.ur), _pt(quad.lr), _pt(quad.ll), _pt(quad.ul)]
            out.append(("quad", pts, True))
        else:  # pragma: no cover - unknown operator, keep it visible in forensics
            flush()
    flush()
    return out


def _delete_all_annotations(page: "fitz.Page") -> int:
    """Drop every annotation from the in-memory page.

    The on-disk file is never modified.  This is the primary guarantee that
    previous manual take-off markup cannot reach the geometry pipeline.
    """
    removed = 0
    try:
        annot = page.first_annot
        while annot is not None:
            annot = page.delete_annot(annot)
            removed += 1
    except Exception:  # pragma: no cover - defensive
        pass
    return removed


def _annotation_geometry(page: "fitz.Page") -> list[tuple[Pt, ...]]:
    """Canonical point tuples declared by markup annotations on the page."""
    shapes: list[tuple[Pt, ...]] = []
    try:
        annots = list(page.annots())
    except Exception:  # pragma: no cover - defensive
        return shapes
    for a in annots:
        info_type = a.type[1] if isinstance(a.type, (list, tuple)) else str(a.type)
        if info_type not in MARKUP_SUBTYPES:
            continue
        verts = getattr(a, "vertices", None)
        if verts:
            flat: list[Pt] = []
            for v in verts:
                if isinstance(v, (list, tuple)) and v and isinstance(v[0], (list, tuple, fitz.Point)):
                    flat.extend(_pt(x) for x in v)
                else:
                    flat.append(_pt(v))
            if len(flat) >= 2:
                shapes.append(tuple((qc(x), qc(y)) for x, y in flat))
        r = a.rect
        shapes.append(
            tuple(
                (qc(x), qc(y))
                for x, y in [(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1), (r.x0, r.y0)]
            )
        )
    return shapes


def _matches_annotation(points: Sequence[Pt], shapes: Iterable[tuple[Pt, ...]], tol: float) -> bool:
    cand = [(qc(x), qc(y)) for x, y in points]
    cs = set(cand)
    for shape in shapes:
        ss = set(shape)
        if len(cs) != len(ss):
            continue
        ok = True
        for p in cs:
            if not any(abs(p[0] - s[0]) <= tol and abs(p[1] - s[1]) <= tol for s in ss):
                ok = False
                break
        if ok:
            return True
    return False


def extract_document(
    pdf_path: str | Path,
    cfg: ExtractionConfig | None = None,
    pages: Sequence[int] | None = None,
) -> VectorDocument:
    cfg = cfg or ExtractionConfig()
    pdf_path = Path(pdf_path)
    doc = fitz.open(pdf_path)
    try:
        page_infos: list[PageInfo] = []
        raw_objects: list[VectorObject] = []
        spans: list[TextSpan] = []
        dropped_objects = 0
        dropped_spans = 0

        page_indices = list(range(doc.page_count)) if pages is None else list(pages)
        for pno in page_indices:
            page = doc[pno]
            mb, cb = page.mediabox, page.cropbox
            page_infos.append(
                PageInfo(
                    page=pno,
                    width=float(page.rect.width),
                    height=float(page.rect.height),
                    rotation=int(page.rotation),
                    media_box=(float(mb.x0), float(mb.y0), float(mb.x1), float(mb.y1)),
                    crop_box=(float(cb.x0), float(cb.y0), float(cb.x1), float(cb.y1)),
                )
            )
            annot_shapes = _annotation_geometry(page)
            # Suppress annotation appearance streams *before* reading paths.
            n_with_annots = len(page.get_drawings())
            _delete_all_annotations(page)
            drawings = page.get_drawings()
            dropped_objects += max(0, n_with_annots - len(drawings))
            if not cfg.drop_flattened_annotation_geometry:
                annot_shapes = []
            for dr in drawings:
                dtype = dr.get("type", "s")
                stroke = _norm_color(dr.get("color")) if dtype in ("s", "fs") else None
                fill = _norm_color(dr.get("fill")) if dtype in ("f", "fs") else None
                width = dr.get("width")
                width = float(width) if width is not None else None
                dashes = dr.get("dashes")
                dashes = str(dashes) if dashes else None
                layer = dr.get("layer") or None
                even_odd = bool(dr.get("even_odd") or False)
                close_path = bool(dr.get("closePath") or False)
                clip = dr.get("scissor")
                clip_box = None
                if clip is not None:
                    try:
                        clip_box = (qc(clip.x0), qc(clip.y0), qc(clip.x1), qc(clip.y1))
                    except Exception:  # pragma: no cover
                        clip_box = None

                for kind, pts, closed in _subpaths_from_items(dr.get("items", ()), cfg):
                    # A subpath whose last point coincides with its first is a
                    # closed contour whatever the content stream's closePath
                    # flag says - CAD exporters commonly emit the repeated
                    # point instead of the operator.
                    closed = closed or close_path or (len(pts) > 2 and pts[0] == pts[-1])
                    if closed:
                        # A contour that encloses no area is a stroke that was
                        # merely closed back onto itself, not a symbol; treating
                        # it as closed would hide real geometry from the pipe
                        # stages.
                        box = BBox.from_points(pts)
                        if min(box.width, box.height) <= cfg.min_object_extent_pt:
                            closed = False
                            pts = pts[:-1] if pts[0] == pts[-1] and len(pts) > 2 else pts
                    if closed and len(pts) > 2 and pts[0] != pts[-1]:
                        pts = pts + [pts[0]]
                    box = BBox.from_points(pts)
                    if max(box.width, box.height) < cfg.min_object_extent_pt:
                        continue
                    if annot_shapes and _matches_annotation(pts, annot_shapes, cfg.annotation_match_tolerance_pt):
                        dropped_objects += 1
                        continue
                    raw_objects.append(
                        VectorObject(
                            object_id="",
                            page=pno,
                            kind=kind,
                            points=tuple((float(x), float(y)) for x, y in pts),
                            closed=closed,
                            stroke_color=stroke,
                            fill_color=fill,
                            stroke_width=width,
                            dashes=dashes,
                            layer=layer,
                            even_odd=even_odd,
                            from_annotation=False,
                            clip_bbox=clip_box,
                        )
                    )

            # Native text layer, if the producer left one.
            td = page.get_text("dict")
            for block in td.get("blocks", ()):
                for line in block.get("lines", ()):
                    d = line.get("dir", (1.0, 0.0))
                    rot = math.degrees(math.atan2(float(d[1]), float(d[0])))
                    for sp in line.get("spans", ()):
                        text = str(sp.get("text", ""))
                        if not text.strip():
                            continue
                        b = sp.get("bbox")
                        spans.append(
                            TextSpan(
                                span_id="",
                                page=pno,
                                text=text,
                                bbox=BBox(float(b[0]), float(b[1]), float(b[2]), float(b[3])),
                                font=str(sp.get("font", "")),
                                size=float(sp.get("size", 0.0)),
                                rotation=rot,
                                from_annotation=False,
                            )
                        )

        # Content addressing: sort canonically, then assign ids.  Exact duplicates
        # get a stable occurrence suffix; because they are identical in every
        # respect, which one gets which suffix carries no semantics.
        raw_objects = canonical_sort(raw_objects, key=lambda o: o.canonical_key())
        objects: list[VectorObject] = []
        seen: dict[str, int] = {}
        for o in raw_objects:
            base = entity_id("obj", o.canonical_key())
            n = seen.get(base, 0)
            seen[base] = n + 1
            oid = base if n == 0 else f"{base}#{n}"
            objects.append(
                VectorObject(
                    object_id=oid,
                    page=o.page,
                    kind=o.kind,
                    points=o.points,
                    closed=o.closed,
                    stroke_color=o.stroke_color,
                    fill_color=o.fill_color,
                    stroke_width=o.stroke_width,
                    dashes=o.dashes,
                    layer=o.layer,
                    even_odd=o.even_odd,
                    from_annotation=o.from_annotation,
                    clip_bbox=o.clip_bbox,
                )
            )

        spans = canonical_sort(spans, key=lambda s: s.canonical_key())
        span_out: list[TextSpan] = []
        seen_s: dict[str, int] = {}
        for s in spans:
            base = entity_id("span", s.canonical_key())
            n = seen_s.get(base, 0)
            seen_s[base] = n + 1
            span_out.append(
                TextSpan(
                    span_id=base if n == 0 else f"{base}#{n}",
                    page=s.page,
                    text=s.text,
                    bbox=s.bbox,
                    font=s.font,
                    size=s.size,
                    rotation=s.rotation,
                    from_annotation=s.from_annotation,
                )
            )

        return VectorDocument(
            source_name=pdf_path.name,
            sha256=sha256_of(pdf_path),
            pages=page_infos,
            objects=objects,
            text_spans=span_out,
            excluded_annotation_objects=dropped_objects,
            excluded_annotation_spans=dropped_spans,
        )
    finally:
        doc.close()
