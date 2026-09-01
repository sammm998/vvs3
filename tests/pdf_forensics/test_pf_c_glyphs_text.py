"""Lettering is read from shapes and assembled by position."""

from __future__ import annotations

from pdf_forensics.search import Microscope


def _texts(path):
    scope = Microscope(path)
    try:
        return {item.text for item in scope.text}, scope
    finally:
        pass


def test_stroke_font_lettering_is_read(clean_a):
    scope = Microscope(clean_a)
    texts = {item.text for item in scope.text}
    # the sheet's own codes, read from geometry with no text layer at all
    assert "S1-P2-160" in texts
    assert "S1-P2-110" in texts
    assert "S1-P2-75" in texts
    assert any(t.startswith("VG+") for t in texts)
    scope.close()


def test_text_layer_supersedes_the_same_lettering_drawn_as_outlines(clean_b):
    scope = Microscope(clean_b)
    natives = [t for t in scope.text if t.source == "native"]
    assert natives, "drawing B has a text layer"
    for item in natives:
        overlapping = [o for o in scope.text
                       if o is not item and o.page == item.page
                       and not (o.bbox[2] < item.bbox[0] or item.bbox[2] < o.bbox[0]
                                or o.bbox[3] < item.bbox[1] or item.bbox[3] < o.bbox[1])]
        assert not overlapping, f"{item.text} is reported twice"
    scope.close()


def test_uncertain_characters_keep_their_alternatives(clean_a):
    scope = Microscope(clean_a)
    uncertain = [g for g in scope.glyphs.glyphs if g.confidence < 0.9]
    assert uncertain, "a stroke font matched against base-14 is never all-certain"
    assert all(len(g.alternatives) > 1 for g in uncertain)
    scope.close()


def test_reading_order_comes_from_geometry_not_from_the_pdf(clean_a):
    from pdf_forensics.text_reconstruction import reconstruct
    scope = Microscope(clean_a)
    forward = reconstruct(scope.glyphs.glyphs)
    backward = reconstruct(list(reversed(scope.glyphs.glyphs)))
    assert [t.text for t in forward] == [t.text for t in backward]
    scope.close()
