"""Opening the file, once.

The loader is also where the blindness rules live.  An analysis reads exactly
one clean PDF.  A spreadsheet, a previous take-off or a marked-up copy is not
an input to detection at any point, and the guard below refuses them rather
than trusting a later stage to ignore them.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import fitz

# Extensions that can only be a facit or a working file, never a drawing.
FORBIDDEN_SUFFIXES = {".xlsx", ".xls", ".xlsm", ".csv", ".ods", ".json"}
# Name fragments that mark a file as carrying somebody's answer already.
ANSWER_MARKERS = ("facit", "answer", "truth", "ground_truth", "groundtruth",
                  "marked", "markerad", "takeoff", "take_off", "with_takeoff",
                  "mangd", "quantities")


class BlindnessError(RuntimeError):
    """Raised when something that could contain the answer is handed to detection."""


def assert_blind_input(path: str | os.PathLike[str], allow_answer_file: bool = False) -> Path:
    """Refuse an input that cannot honestly be called a clean drawing."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in FORBIDDEN_SUFFIXES:
        raise BlindnessError(
            f"{p.name}: detection reads drawings, not results ({suffix} is a facit format)"
        )
    if suffix != ".pdf":
        raise BlindnessError(f"{p.name}: expected a PDF, got {suffix or 'no extension'}")
    stem = p.stem.lower()
    if not allow_answer_file:
        for marker in ANSWER_MARKERS:
            if marker in stem:
                raise BlindnessError(
                    f"{p.name}: the name says this file already carries an answer "
                    f"('{marker}').  Analyse the clean PDF; compare afterwards.  "
                    f"Pass allow_answer_file=True only to inspect it, never to detect."
                )
    return p


@dataclass(frozen=True)
class PageInfo:
    number: int                    # zero-based, as PyMuPDF counts
    width: float
    height: float
    rotation: int
    mediabox: tuple[float, float, float, float]
    cropbox: tuple[float, float, float, float]
    transform: tuple[float, ...]

    def to_json(self) -> dict:
        return {
            "page": self.number,
            "width": self.width,
            "height": self.height,
            "rotation": self.rotation,
            "mediabox": list(self.mediabox),
            "cropbox": list(self.cropbox),
            "transform": list(self.transform),
        }


class LoadedPdf:
    """A parsed document, parsed once.

    Stages ask this object for pages; nothing re-opens the file.  The handle
    stays open for the lifetime of an analysis because local rendering
    (:mod:`pdf_forensics.render`) needs it.
    """

    def __init__(self, path: str | os.PathLike[str], *, allow_answer_file: bool = False) -> None:
        self.path = assert_blind_input(path, allow_answer_file=allow_answer_file)
        self.blob_sha1 = hashlib.sha1(self.path.read_bytes()).hexdigest()
        self.doc = fitz.open(str(self.path))
        self.pages: list[PageInfo] = []
        for i in range(self.doc.page_count):
            page = self.doc.load_page(i)
            rect = page.rect
            self.pages.append(
                PageInfo(
                    number=i,
                    width=float(rect.width),
                    height=float(rect.height),
                    rotation=int(page.rotation),
                    mediabox=tuple(float(v) for v in page.mediabox),
                    cropbox=tuple(float(v) for v in page.cropbox),
                    transform=tuple(float(v) for v in page.transformation_matrix),
                )
            )

    # -- lifetime ---------------------------------------------------------
    def page(self, number: int) -> "fitz.Page":
        return self.doc.load_page(number)

    def close(self) -> None:
        if self.doc is not None and not self.doc.is_closed:
            self.doc.close()

    def __enter__(self) -> "LoadedPdf":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- description ------------------------------------------------------
    @property
    def page_count(self) -> int:
        return len(self.pages)

    def metadata(self) -> dict[str, Any]:
        md = dict(self.doc.metadata or {})
        return {k: md.get(k) for k in sorted(md)}

    def sheet_size_mm(self, page: int = 0) -> tuple[float, float]:
        info = self.pages[page]
        return (info.width * 25.4 / 72.0, info.height * 25.4 / 72.0)

    def to_json(self) -> dict:
        return {
            "file": self.path.name,
            "sha1": self.blob_sha1,
            "bytes": self.path.stat().st_size,
            "pageCount": self.page_count,
            "pages": [p.to_json() for p in self.pages],
            "metadata": self.metadata(),
            "isEncrypted": bool(self.doc.is_encrypted),
            "needsPassword": bool(self.doc.needs_pass),
            "hasEmbeddedFiles": self.doc.embfile_count() > 0,
        }


def load(path: str | os.PathLike[str], *, allow_answer_file: bool = False) -> LoadedPdf:
    return LoadedPdf(path, allow_answer_file=allow_answer_file)
