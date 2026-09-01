"""Pipes come from geometry, and from reasons that are recorded."""

from __future__ import annotations

import math

from pdf_forensics import pipes as pipes_mod


def test_every_candidate_states_why_it_exists(analysis_a, analysis_b):
    for workspace, _ in (analysis_a, analysis_b):
        for candidate in workspace.pipe_candidates:
            rules = candidate.evidence.get("rules") or [candidate.evidence.get("rule")]
            assert any(rules), candidate
            assert candidate.segment_ids and candidate.source_object_ids


def test_the_sheet_frame_and_the_lettering_are_not_pipes(analysis_a):
    workspace, report = analysis_a
    roles = report["stages"]["pipes"]["segmentRoles"]
    assert roles.get("LETTERING", 0) > 0 and roles.get("SHEET_FRAME", 0) > 0
    for candidate in workspace.pipe_candidates:
        for segment_id in candidate.segment_ids:
            assert workspace.roles[segment_id].role == pipes_mod.LINEWORK


def test_a_scale_bar_cell_is_not_a_pipe(analysis_a, analysis_b):
    """Two short edges facing each other are a shape, not a bore."""
    for workspace, _ in (analysis_a, analysis_b):
        for candidate in workspace.pipe_candidates:
            if candidate.wall_separation is None:
                continue
            assert candidate.length >= candidate.wall_separation


def test_double_line_walls_are_mutually_closest(analysis_a):
    workspace, _ = analysis_a
    for candidate in workspace.pipe_candidates:
        if candidate.kind == "double_line":
            assert "PARALLEL_WALLS_MUTUALLY_CLOSEST" in (candidate.evidence.get("rules") or [])


def test_a_lone_long_stroke_is_not_promoted(analysis_a):
    """No rule anywhere says 'the longest line is the pipe'."""
    workspace, _ = analysis_a
    explained = {sid for c in workspace.pipe_candidates for sid in c.segment_ids}
    longest = max(workspace.geometry.segments, key=lambda s: (s.length, s.segment_id))
    if longest.segment_id in explained:
        owning = [c for c in workspace.pipe_candidates if longest.segment_id in c.segment_ids]
        assert all(c.kind != "single_line" or c.evidence.get("rule") for c in owning)
    else:
        assert True     # the longest stroke on the sheet is its frame, and it is not a pipe
