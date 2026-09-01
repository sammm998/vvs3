"""Nothing may disappear between the file and the model."""

from __future__ import annotations

from pdf_forensics.inspect import inspect_file
from pdf_forensics.loader import load
from pdf_forensics.objects import extract


def test_conservation_holds_for_every_kind(clean_a, clean_b):
    for path in (clean_a, clean_b):
        payload = inspect_file(path)
        scope = payload.pop("_scope")
        conservation = payload["objects"]["conservation"]
        assert conservation["ok"], conservation
        assert conservation["delta"] == {k: 0 for k in conservation["delta"]}
        scope.close()


def test_every_drawing_operator_is_modelled(clean_a):
    with load(clean_a) as pdf:
        store = extract(pdf)
        assert store.raw.drawings == len(store.of_kind("path"))
        assert store.raw.drawing_items > 0
        assert store.conservation()["pathsWithoutGeometry"] == 0


def test_object_ids_are_content_addressed_and_stable(clean_a):
    with load(clean_a) as pdf:
        first = [o.object_id for o in extract(pdf).objects]
    with load(clean_a) as pdf:
        second = [o.object_id for o in extract(pdf).objects]
    assert first == second
    assert len(set(first)) == len(first)


def test_glyph_stage_reports_its_sources(clean_b):
    payload = inspect_file(clean_b, with_glyphs=True)
    scope = payload.pop("_scope")
    assert payload["glyphs"]["glyphs"] > 0
    assert set(payload["glyphs"]["bySource"]) <= {"text", "path"}
    scope.close()
