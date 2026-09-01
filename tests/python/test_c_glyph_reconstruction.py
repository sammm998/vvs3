"""C. Glyph reconstruction test.

The recogniser's parameters were fitted on characters, so the honest check is
whether it generalises to typefaces that are **not** in its prototype bank.
``test_recognises_held_out_fonts`` renders the alphabet with fonts the bank
never sees and measures accuracy on those.
"""

from __future__ import annotations

import fitz
import numpy as np
import pytest

from vvs_pipe.geometry.primitives import BBox
from vvs_pipe.glyph import segment_glyphs
from vvs_pipe.glyph.alphabet import GlyphObservation, resolve_alphabet
from vvs_pipe.glyph.classify import glyph_distance_vector
from vvs_pipe.glyph.features import rasterise_mask, rasterise_polylines, thin
from vvs_pipe.glyph.prototypes import FONTS
from vvs_pipe.text_reconstruction import reconstruct_text
from vvs_pipe.vector_extraction import extract_document

HELD_OUT_FONTS = ("tibo", "cobo", "hebi")
TARGET_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _render(ch: str, font: str, px: int = 160) -> np.ndarray | None:
    doc = fitz.open()
    try:
        page = doc.new_page(width=px, height=px)
        page.insert_text(fitz.Point(px * 0.12, px * 0.78), ch, fontname=font, fontsize=px * 0.62)
        pm = page.get_pixmap(colorspace=fitz.csGRAY, alpha=False)
        arr = np.frombuffer(pm.samples, dtype=np.uint8).reshape(pm.height, pm.width)
        mask = (arr < 128).astype(np.uint8)
        return mask if mask.any() else None
    finally:
        doc.close()


def test_held_out_fonts_are_not_in_the_prototype_bank():
    assert not set(HELD_OUT_FONTS) & set(FONTS)


def test_recognises_held_out_fonts():
    """Cross-typeface character accuracy, measured on fonts outside the bank."""
    correct = 0
    total = 0
    for font in HELD_OUT_FONTS:
        for ch in TARGET_CHARS:
            mask = _render(ch, font)
            if mask is None:
                continue
            # A pre-rendered mask goes through the same distance vector the
            # classifier uses; only the rasterisation entry point differs.
            raster = rasterise_mask(mask)
            ranked = sorted(
                glyph_distance_vector(raster, 1.0, 0.0).items(), key=lambda kv: (kv[1], kv[0])
            )
            total += 1
            if ranked and ranked[0][0] == ch:
                correct += 1
    assert total >= 60
    accuracy = correct / total
    assert accuracy >= 0.80, f"cross-font character accuracy {accuracy:.3f}"


def test_thinning_is_idempotent_and_deterministic():
    mask = np.zeros((48, 48), dtype=np.uint8)
    mask[10:38, 20:28] = 1
    once = thin(mask)
    assert (thin(once) == once).all()
    assert (thin(mask) == once).all()


def test_glyph_segmentation_and_reconstruction_recovers_the_sheet_text(drawing_a):
    doc = extract_document(drawing_a["files"]["clean"])
    page = doc.pages[0]
    box = BBox(0, 0, page.width, page.height)
    seg = segment_glyphs(doc.objects_on(0), box)
    text = reconstruct_text(seg, doc.spans_on(0), 0)
    strings = {t.text for t in text.items}

    # Every designation the drawing carries has to come back out of the vector
    # geometry, with no text layer to help.
    for expected in drawing_a["designations"]:
        assert expected in strings, f"{expected!r} not in {sorted(strings)}"
    assert "SKALA 1:50" in strings
    assert any(s.startswith("VG+") for s in strings)


def test_unrecognisable_marks_stay_unresolved(drawing_a):
    """A riser circle is not a character; it must not be forced onto one."""
    doc = extract_document(drawing_a["files"]["clean"])
    page = doc.pages[0]
    seg = segment_glyphs(doc.objects_on(0), BBox(0, 0, page.width, page.height))
    text = reconstruct_text(seg, doc.spans_on(0), 0)
    unresolved = [g for g in text.glyphs if g.character is None]
    assert unresolved, "expected the symbol marks to be reported unresolved"
    for g in unresolved:
        assert g.state.value == "UNRESOLVED"
        assert any(r.value == "UNRESOLVED_GLYPH" for r in g.reasons)


def test_alphabet_assignment_is_order_independent(drawing_a):
    from vvs_pipe.validation.determinism import permutations_of

    doc = extract_document(drawing_a["files"]["clean"])
    page = doc.pages[0]
    seg = segment_glyphs(doc.objects_on(0), BBox(0, 0, page.width, page.height))
    observations = [
        GlyphObservation(
            key=(g.bbox.key(), g.object_ids),
            raster=rasterise_polylines(g.polylines, g.filled),
            rel_height=g.bbox.height / max(line.cap_height, 1e-6),
            rel_base=(line.bbox.y1 - g.bbox.y1) / max(line.cap_height, 1e-6),
        )
        for line in seg.lines
        if len(line.glyphs) >= 2
        for g in line.glyphs
    ]
    baseline = None
    for _name, permuted in permutations_of(observations):
        assignment = resolve_alphabet(permuted)
        mapping = {k: assignment.character(k) for k in sorted(assignment.cluster_of)}
        if baseline is None:
            baseline = mapping
        else:
            assert mapping == baseline
