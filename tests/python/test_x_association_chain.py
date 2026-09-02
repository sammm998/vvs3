"""The evidence chain, and the refusals it is there to make.

    vector glyphs -> designation + DN -> vector leader -> leader endpoint
                  -> FE-layer geometry -> physical pipe

``drawing_c`` is built to break a proximity rule: it carries an inline label
sitting half a point from a pipe, a code-shaped note beside another, a date, a
duct-schedule string and a title block - and three callouts whose leaders are
drawn the way CAD draws them, as a shoulder and a slant in separate objects.
Only the three may name a pipe.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fixtures.make_hard_drawing import build as build_hard_drawing
from vvs_pipe.pipeline import analyse


@pytest.fixture(scope="module")
def hard(tmp_path_factory):
    truth = build_hard_drawing(tmp_path_factory.mktemp("hard"))
    result = analyse(truth["files"]["clean"])
    return result, truth


def _chain(result):
    return result.pages[0].diagnostics["associationChain"]


def test_only_labels_the_drawing_pointed_at_name_pipes(hard):
    result, truth = hard
    named = sorted({p.designation for p in result.pages[0].physical_pipes if p.designation})
    assert named == truth["expectedConfirmed"]


def test_an_inline_label_touching_a_pipe_names_nothing(hard):
    """The regression, stated as a test.

    ``S3-R8-160`` is written on the DN110 pipe, half a point from its
    centerline, and is exactly the kind of string a proximity rule binds.  The
    hint is still measured and reported - so the decision can be argued with -
    but it names nothing.
    """
    result, _ = hard
    page = result.pages[0]
    named = {p.designation for p in page.physical_pipes if p.designation}
    assert "S3-R8-160" not in named
    hints = _chain(result)["proximityHintsNotUsed"]
    assert any(h["distancePt"] < 2.0 for h in hints), \
        "a label sitting on a pipe must still be measured, and still unused"


def test_notes_dates_and_title_block_stay_text(hard):
    result, truth = hard
    named = {p.designation for p in result.pages[0].physical_pipes if p.designation}
    for refused in ("2024-04-19", "ENL. PM-1", "ENL. PM-2", "W-50-1-A-0024"):
        assert refused not in named
    tiers = result.pages[0].diagnostics["designationTiers"]
    assert tiers["CONFIRMED_DESIGNATION"] == len(truth["expectedConfirmed"])


def test_a_leader_that_reaches_no_pipe_is_refused(hard):
    """S3-X9-50's leader ends on building fabric, not on pipework."""
    result, _ = hard
    failures = {f["text"]: f for f in _chain(result)["chainFailures"]}
    assert "S3-X9-50" in failures
    assert failures["S3-X9-50"]["stage"] == "attachment"
    assert failures["S3-X9-50"]["reason"].startswith("TIP_")
    named = {p.designation for p in result.pages[0].physical_pipes if p.designation}
    assert "S3-X9-50" not in named


def test_the_pipe_layer_is_discovered_not_named(hard):
    result, truth = hard
    layers = _chain(result)["pipeLayers"]
    assert layers["active"] is True
    assert layers["layers"] == [truth["pipeLayer"]]
    # ... and it was found from the geometry, so the engine may not contain it
    engine = Path(__file__).resolve().parents[2] / "src" / "python" / "vvs_pipe"
    for module in engine.rglob("*.py"):
        text = module.read_text(encoding="utf-8")
        assert truth["pipeLayer"] not in text
        assert '"-FE-"' not in text and "'-FE-'" not in text


def test_every_confirmed_designation_publishes_its_whole_chain(hard):
    result, truth = hard
    chains = result.pages[0].diagnostics["evidenceChains"]
    assert len(chains) == len(truth["expectedConfirmed"])
    for link in chains:
        assert link["glyphIds"], "the reading must come from glyphs"
        assert link["designation"] and link["diameterMm"]
        assert link["leaderId"] and link["leaderObjectIds"], "the leader's own objects"
        assert link["leaderTip"] and link["feObjectId"], "where it landed, and on what"
        assert link["feLayer"] == truth["pipeLayer"]
        assert link["pipeRunId"] and link["physicalPipeId"]


def test_the_census_counts_every_stage(hard):
    result, truth = hard
    chain = _chain(result)
    assert chain["designationOccurrences"] >= len(truth["expectedConfirmed"])
    assert chain["designationsWithDn"] <= chain["designationOccurrences"]
    assert chain["vectorLeaders"] >= chain["verifiedAttachments"]
    assert chain["verifiedAttachments"] == len(truth["expectedConfirmed"])
    assert chain["physicalPipesDesignated"] <= chain["physicalPipes"]


def test_a_multi_object_leader_is_traced_through_its_bend(hard):
    """The old rule accepted only a single two-point object, so it saw none."""
    result, _ = hard
    leaders = result.pages[0].leaders
    assert leaders
    assert any(len(l.polyline) > 2 or l.hops > 0 for l in leaders), \
        "a shoulder-and-slant leader must be followed through its bend"


def test_debug_crops_are_written_for_every_failed_stage(hard, tmp_path):
    from vvs_pipe.rendering.crops import render_crops, requests_from_result

    result, _ = hard
    requests = requests_from_result(result)
    assert requests, "this sheet has failures worth looking at"
    written = render_crops(result.source_path, requests, tmp_path / "crops")
    assert len(written) == len(requests)
    for entry in written:
        assert (tmp_path / "crops" / entry["file"]).exists()
        assert entry["stage"] and entry["reason"]
    assert (tmp_path / "crops" / "index.json").exists()


def test_solid_strokes_are_not_counted_as_dashed():
    """``[] 0`` is a PDF saying *solid*; reading it as a dash deleted pipework."""
    from vvs_pipe.roles import _declares_dashes

    assert not _declares_dashes("[] 0")
    assert not _declares_dashes("")
    assert not _declares_dashes(None)
    assert not _declares_dashes("[0 0] 0")
    assert _declares_dashes("[3 2] 0")
    assert _declares_dashes("[1.5] 0")


def test_the_quantity_overlay_draws_only_owned_pipe_geometry(hard, tmp_path):
    """No association rays on the quantity drawing.

    The marked drawing answers "what was measured".  Every mark it adds has to
    lie on a pipe the engine owns; a line from a label to a pipe belongs to the
    debug overlay, on its own switchable layer.
    """
    import fitz

    from vvs_pipe.geometry.primitives import Segment, point_segment_distance
    from vvs_pipe.rendering import render_marked

    result, _ = hard
    target = render_marked(result, tmp_path / "marked.pdf")
    owned: list[Segment] = []
    for pipe in result.pages[0].physical_pipes:
        for poly in pipe.centerline:
            owned.extend(Segment(poly[i], poly[i + 1]) for i in range(len(poly) - 1))

    doc = fitz.open(str(target))
    try:
        page = doc[0]
        # only the pipe-geometry layer: the legend key is furniture, and the
        # association evidence is not on this drawing at all
        drawn = [
            item
            for drawing in page.get_drawings()
            for item in drawing["items"]
            if item[0] == "l" and drawing.get("layer") == "Pipe geometry"
        ]
        assert drawn, "the overlay drew something"
        for _kind, a, b in drawn:
            for point in ((a.x, a.y), (b.x, b.y)):
                assert min(point_segment_distance(point, s) for s in owned) <= 1.0, \
                    f"the overlay drew a line at {point} that is not on a pipe it owns"
    finally:
        doc.close()


def test_association_evidence_is_a_separate_switchable_layer(hard, tmp_path):
    import fitz

    from vvs_pipe.rendering import render_debug, render_marked

    result, _ = hard
    debug = fitz.open(str(render_debug(result, tmp_path / "debug.pdf")))
    marked = fitz.open(str(render_marked(result, tmp_path / "marked.pdf")))
    try:
        debug_layers = {ocg["name"] for ocg in debug.get_ocgs().values()}
        marked_layers = {ocg["name"] for ocg in marked.get_ocgs().values()}
        assert {"Leaders", "Association", "Pipe geometry"} <= debug_layers
        # the quantity drawing carries no association layer at all
        assert "Association" not in marked_layers and "Leaders" not in marked_layers
        assert "Pipe geometry" in marked_layers
    finally:
        debug.close()
        marked.close()
