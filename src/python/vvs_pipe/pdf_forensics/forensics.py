"""PDF forensic report - produced *before* any pipe detection.

The report is a pure description of what the file physically contains.  It
makes no claim about pipes, designations or quantities, and it is written to
disk first so that any later result can be audited against the raw file.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz

from ..canonical import canonical_json, digest, qc
from ..vector_extraction.extractor import (
    ExtractionConfig,
    _delete_all_annotations,
    _norm_color,
    sha256_of,
)


@dataclass(slots=True)
class ForensicReport:
    data: dict[str, Any]

    def to_canonical(self) -> dict[str, Any]:
        return self.data

    def write(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(canonical_json(self.data, indent=2), encoding="utf-8")
        return p

    @property
    def report_digest(self) -> str:
        return digest(self.data)


def _hist(counter: Counter, limit: int = 40) -> list[list[Any]]:
    items = sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))[:limit]
    return [[list(k) if isinstance(k, tuple) else k, v] for k, v in items]


def forensic_report(pdf_path: str | Path, cfg: ExtractionConfig | None = None) -> ForensicReport:
    cfg = cfg or ExtractionConfig()
    pdf_path = Path(pdf_path)
    doc = fitz.open(pdf_path)
    try:
        pages_out: list[dict[str, Any]] = []
        totals = Counter()
        colors: Counter = Counter()
        fills: Counter = Counter()
        widths: Counter = Counter()
        dashes: Counter = Counter()
        layers: Counter = Counter()
        fonts: Counter = Counter()
        annot_types: Counter = Counter()
        clip_count = 0

        annotation_appearance_objects = 0
        for pno in range(doc.page_count):
            page = doc[pno]
            annots = []
            try:
                for a in page.annots() or ():
                    subtype = a.type[1] if isinstance(a.type, (list, tuple)) else str(a.type)
                    annot_types[subtype] += 1
                    r = a.rect
                    annots.append(
                        {
                            "subtype": subtype,
                            "rect": [qc(r.x0), qc(r.y0), qc(r.x1), qc(r.y1)],
                            "hasContent": bool(a.info.get("content")),
                        }
                    )
            except Exception:  # pragma: no cover
                pass
            n_with_annots = len(page.get_drawings())
            _delete_all_annotations(page)
            drawings = page.get_drawings()
            annotation_appearance_objects += max(0, n_with_annots - len(drawings))
            per = Counter()
            for dr in drawings:
                dtype = dr.get("type", "s")
                if dtype in ("s", "fs"):
                    per["strokes"] += 1
                    c = _norm_color(dr.get("color"))
                    if c is not None:
                        colors[c] += 1
                    w = dr.get("width")
                    if w is not None:
                        widths[qc(float(w))] += 1
                if dtype in ("f", "fs"):
                    per["fills"] += 1
                    f = _norm_color(dr.get("fill"))
                    if f is not None:
                        fills[f] += 1
                d = dr.get("dashes")
                dashes[str(d) if d else "[] 0"] += 1
                layers[dr.get("layer") or ""] += 1
                if dr.get("scissor") is not None:
                    per["clipped"] += 1
                per["drawings"] += 1
                for it in dr.get("items", ()):
                    op = it[0]
                    per["lines" if op == "l" else "curves" if op == "c" else "rects" if op == "re" else "quads" if op == "qu" else "otherItems"] += 1
                    per["pathItems"] += 1

            try:
                extended = page.get_drawings(extended=True)
                clip_count += sum(1 for e in extended if e.get("type") == "clip")
            except Exception:  # pragma: no cover - older PyMuPDF
                pass

            text = page.get_text("dict")
            n_spans = 0
            n_chars = 0
            for block in text.get("blocks", ()):
                for line in block.get("lines", ()):
                    for sp in line.get("spans", ()):
                        n_spans += 1
                        n_chars += len(sp.get("text", ""))
                        fonts[str(sp.get("font", ""))] += 1

            images = page.get_images(full=True)
            mb, cb = page.mediabox, page.cropbox
            pages_out.append(
                {
                    "page": pno,
                    "width": qc(page.rect.width),
                    "height": qc(page.rect.height),
                    "rotation": int(page.rotation),
                    "mediaBox": [qc(mb.x0), qc(mb.y0), qc(mb.x1), qc(mb.y1)],
                    "cropBox": [qc(cb.x0), qc(cb.y0), qc(cb.x1), qc(cb.y1)],
                    "vectorDrawings": per["drawings"],
                    "pathItems": per["pathItems"],
                    "lines": per["lines"],
                    "curves": per["curves"],
                    "rects": per["rects"],
                    "quads": per["quads"],
                    "strokes": per["strokes"],
                    "fills": per["fills"],
                    "clippedDrawings": per["clipped"],
                    "textSpans": n_spans,
                    "textChars": n_chars,
                    "annotations": annots,
                    "embeddedImages": len(images),
                }
            )
            for k, v in per.items():
                totals[k] += v
            totals["textSpans"] += n_spans
            totals["textChars"] += n_chars
            totals["annotations"] += len(annots)
            totals["embeddedImages"] += len(images)

        has_ocg = False
        try:
            has_ocg = bool(doc.get_ocgs())
        except Exception:  # pragma: no cover
            pass

        data: dict[str, Any] = {
            "schema": "vvs-pipe/forensics/1",
            "file": pdf_path.name,
            "pdfSha256": sha256_of(pdf_path),
            "fileSizeBytes": pdf_path.stat().st_size,
            "pdfVersion": doc.metadata.get("format") if doc.metadata else None,
            "producer": (doc.metadata or {}).get("producer"),
            "creator": (doc.metadata or {}).get("creator"),
            "encrypted": bool(doc.is_encrypted),
            "pages": doc.page_count,
            "vectorObjectCount": totals["drawings"],
            "pathItemCount": totals["pathItems"],
            "lineCount": totals["lines"],
            "curveCount": totals["curves"],
            "rectCount": totals["rects"],
            "quadCount": totals["quads"],
            "strokeCount": totals["strokes"],
            "fillCount": totals["fills"],
            "clipPathCount": clip_count,
            "clippedDrawingCount": totals["clipped"],
            "textObjectCount": totals["textSpans"],
            "textCharCount": totals["textChars"],
            "annotationCount": totals["annotations"],
            "annotationAppearanceObjectCount": annotation_appearance_objects,
            "embeddedImageCount": totals["embeddedImages"],
            "hasOptionalContentGroups": has_ocg,
            "strokeColors": _hist(colors),
            "fillColors": _hist(fills),
            "strokeWidths": _hist(widths),
            "dashPatterns": _hist(dashes),
            "layers": _hist(layers),
            "fonts": _hist(fonts),
            "annotationSubtypes": _hist(annot_types),
            "pageDetail": pages_out,
            "extractionConfig": {
                "curveFlattenTolerancePt": cfg.curve_flatten_tolerance_pt,
                "maxCurveSubdivisions": cfg.max_curve_subdivisions,
                "dropFlattenedAnnotationGeometry": cfg.drop_flattened_annotation_geometry,
            },
        }
        return ForensicReport(data)
    finally:
        doc.close()
