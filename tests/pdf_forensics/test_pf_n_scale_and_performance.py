"""A big sheet must stay affordable, and nothing may depend on sheet size."""

from __future__ import annotations

import time

import fitz
import pytest

from pdf_forensics.analyze import analyse
from pdf_forensics.search import Microscope


@pytest.fixture(scope="module")
def tiled(clean_a, tmp_path_factory) -> str:
    """The same drawing placed several times on one much larger sheet.

    Two things are being tested at once: content that arrives through form
    XObjects, and rules that would silently break if they were expressed as a
    fraction of the page rather than of the drawing's own lettering.
    """
    source = fitz.open(clean_a)
    width, height = source[0].rect.width, source[0].rect.height
    out = fitz.open()
    page = out.new_page(width=width * 3, height=height * 2)
    for i in range(3):
        for j in range(2):
            page.show_pdf_page(fitz.Rect(i * width, j * height, (i + 1) * width,
                                         (j + 1) * height), source, 0)
    target = tmp_path_factory.mktemp("stress") / "tiled.pdf"
    out.save(str(target), deflate=True)
    return str(target)


def test_content_inside_form_xobjects_is_found(tiled):
    scope = Microscope(tiled)
    assert scope.store.of_kind("form"), "the sheet is built from XObjects"
    assert len(scope.geometry.segments) > 3000
    assert scope.store.conservation()["ok"]
    scope.close()


def test_a_large_sheet_stays_affordable(tiled):
    started = time.time()
    workspace, report = analyse(tiled)
    elapsed = time.time() - started
    assert report["status"] == "VALID"
    # ~5 000 segments; a quadratic search would not come back in a minute
    assert elapsed < 90.0, f"{elapsed:.1f}s for {len(workspace.geometry.segments)} segments"


def test_panels_and_frames_survive_a_change_of_sheet_size(tiled, clean_a):
    """The legend is a legend on any sheet, and a border is never a pipe."""
    _, small = analyse(clean_a)
    _, large = analyse(tiled)
    assert large["stages"]["pipes"]["panels"] >= 6 * small["stages"]["pipes"]["panels"]
    tiles = 6
    small_total = sum(row["totalMetres"] for row in small["quantities"])
    large_total = sum(row["totalMetres"] for row in large["quantities"])
    assert large_total == pytest.approx(tiles * small_total, rel=0.02)
