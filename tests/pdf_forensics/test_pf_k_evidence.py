"""Every answer must be walkable back to the ink."""

from __future__ import annotations


def _relations(node: dict) -> set[str]:
    found = set()
    for support in node.get("builtFrom", []):
        found.add(support["relation"])
        found |= _relations(support["from"])
    return found


def test_a_named_pipe_explains_itself_down_to_the_glyphs(analysis_a):
    workspace, _ = analysis_a
    named = [p for p in workspace.physical_pipes if p.designation]
    assert named
    pipe = named[0]
    why = workspace.evidence.why(pipe.pipe_id, depth=10)
    relations = _relations(why)
    assert "RUN_IN_PHYSICAL_PIPE" in relations
    assert "CANDIDATE_IN_RUN" in relations
    assert "SEGMENT_IN_PIPE_CANDIDATE" in relations
    assert "PATH_YIELDS_SEGMENT" in relations


def test_the_association_chain_reaches_the_leader_and_the_glyphs(analysis_a):
    workspace, _ = analysis_a
    association = next(iter(workspace.resolved.values()))
    why = workspace.evidence.why(association.association_id, depth=10)
    relations = _relations(why)
    assert "CANDIDATE_IN_ASSOCIATION" in relations
    assert "TEXT_PROPOSES_DESIGNATION" in relations
    assert "GLYPH_IN_TEXT" in relations
    assert "OBJECT_YIELDS_GLYPH" in relations


def test_rejected_competitors_are_recorded(analysis_a, analysis_b):
    workspaces = [analysis_a[0], analysis_b[0]]
    assert any(w.evidence.rejections for w in workspaces), \
        "at least one sheet has a designation that lost"
    for workspace in workspaces:
        for rejection in workspace.evidence.rejections:
            assert rejection["reason"], rejection


def test_measurement_shows_its_calculation(analysis_b):
    workspace, _ = analysis_b
    for measurement in workspace.measurements:
        calculation = measurement.calculation
        assert calculation["formula"]
        assert calculation["runIds"]
        if measurement.horizontal_metres is not None:
            assert calculation["metresPerPoint"]
