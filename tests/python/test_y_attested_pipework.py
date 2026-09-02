"""Only geometry the drawing points at is counted as pipework.

A plan sheet carries far more line than it carries pipe: hatching, walls, a
structural grid, furniture, a title block.  Much of it is parallel at a
pipe-like separation, so geometry alone cannot separate it from piping - on the
production sheet that produced 303 "physical pipes" and 1 395 m of take-off,
most of it building fabric.

What separates them is the drawing's own statement: a verified leader landing
on a layer says that layer carries pipework.  Geometry elsewhere is still
extracted, measured and reported - as linework, not as pipe.
"""

from __future__ import annotations

import pytest

from vvs_pipe.association.attachment import (FeAttachment, PipeLayers,
                                             attested_pipe_layers)


def _attachment(layer: str, style: str = "dashed_line", n: int = 1) -> list[FeAttachment]:
    return [
        FeAttachment(
            attachment_id=f"att{layer}{i}", leader_id=f"l{i}", text_id=f"t{i}",
            run_id=f"r{layer}{i}", fe_object_id="obj", fe_layer=layer,
            tip=(0.0, 0.0), distance_pt=0.5, run_style=style,
        )
        for i in range(n)
    ]


def test_a_layer_the_drawing_points_at_repeatedly_is_pipework():
    attachments = _attachment("V-53BB-FE--S3-", n=20) + _attachment("V-52BB-FE--V2-", n=5)
    layers = attested_pipe_layers(attachments)
    assert layers.active
    assert layers.names == {"V-53BB-FE--S3-", "V-52BB-FE--V2-"}


def test_one_stray_hit_does_not_make_a_layer_pipework():
    """A mis-traced leader landing once on the architectural background."""
    attachments = _attachment("V-53BB-FE--S3-", n=30) + _attachment("K-------Y3-", n=1)
    layers = attested_pipe_layers(attachments)
    assert layers.names == {"V-53BB-FE--S3-"}


def test_a_bare_stroke_does_not_attest_a_layer():
    """Single-line acceptance is the weakest verdict the detector makes.

    A pipe whose bore the drawing actually draws - two walls, or a dash chain -
    is evidence about its layer.  One leader landing near an unpaired stroke is
    not, or every annotation layer would become pipework.
    """
    attachments = _attachment("V-53BB-FE--S3-", n=20) + _attachment("V-5--T1_-", "single_line", 8)
    layers = attested_pipe_layers(attachments)
    assert layers.names == {"V-53BB-FE--S3-"}


def test_a_file_with_no_layers_leaves_the_gate_open():
    """Nothing is excluded on a drawing that declares no layers at all."""
    layers = attested_pipe_layers(_attachment("", n=10))
    assert not layers.active
    fallback = PipeLayers(frozenset({"X"}), (("X", 1.0),), active=True)
    assert attested_pipe_layers([], fallback=fallback) is fallback


def test_the_take_off_counts_attested_pipework_and_reports_the_rest(analysis_a):
    """Both sides are published: nothing is deleted to make a number look good."""
    result = analysis_a
    page = result.pages[0]
    assert "unattributedGeometry" in page.diagnostics
    report = page.diagnostics["unattributedGeometry"]
    assert set(report) == {"pipes", "lengthPt", "note"}
    counted = {p.physical_pipe_id for p in page.physical_pipes if p.on_attested_layer}
    quantified = {pid for q in result.quantities for pid in q.physical_pipe_ids}
    assert quantified <= counted


def test_a_drawing_without_layers_counts_everything(analysis_a):
    """Drawing A declares no layers, so nothing may be withheld from its take-off."""
    page = analysis_a.pages[0]
    assert all(p.on_attested_layer for p in page.physical_pipes)
    assert page.diagnostics["unattributedGeometry"]["pipes"] == 0
