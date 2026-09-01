"""A. PDF forensic test."""

from __future__ import annotations

import hashlib
from pathlib import Path

from vvs_pipe.pdf_forensics import forensic_report


def test_forensic_report_describes_the_file(drawing_a):
    path = Path(drawing_a["files"]["clean"])
    report = forensic_report(path).data

    assert report["schema"] == "vvs-pipe/forensics/1"
    assert report["pdfSha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert report["fileSizeBytes"] == path.stat().st_size
    assert report["pages"] == 1

    # Everything the specification asks the report to carry.
    for key in (
        "vectorObjectCount",
        "lineCount",
        "curveCount",
        "rectCount",
        "strokeCount",
        "fillCount",
        "clipPathCount",
        "textObjectCount",
        "annotationCount",
        "embeddedImageCount",
        "strokeColors",
        "fillColors",
        "strokeWidths",
        "dashPatterns",
        "layers",
        "fonts",
        "pageDetail",
    ):
        assert key in report, key

    assert report["vectorObjectCount"] > 100
    # The CAD-style sheet carries no text layer at all: every character is
    # vector outlines, which is exactly the case the engine must handle.
    assert report["textObjectCount"] == 0

    page = report["pageDetail"][0]
    assert page["width"] > 0 and page["height"] > 0
    assert len(page["mediaBox"]) == 4 and len(page["cropBox"]) == 4


def test_forensic_report_is_deterministic(drawing_a):
    a = forensic_report(drawing_a["files"]["clean"])
    b = forensic_report(drawing_a["files"]["clean"])
    assert a.report_digest == b.report_digest
