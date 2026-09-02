"""A designation is proposed by structure and confirmed only by a pipe."""

from __future__ import annotations

from pdf_forensics.model import Reason, State


def test_callouts_get_leaders(analysis_a):
    workspace, _ = analysis_a
    leaders = workspace.leaders_by_text
    assert len(leaders) >= 4, "each callout on drawing A points at its pipe"
    for leader in leaders.values():
        assert leader.length > 0.0
        assert leader.text_end != leader.target_end
        assert leader.segment_ids


def test_no_designation_is_confirmed_without_a_leader_chain(analysis_a, analysis_b):
    """Proximity may corroborate a claim; it may never make one.

    This test asserted that both directions were required.  The rule is now the
    chain the drawing states - the label's leader, and the geometry at its end -
    with the neighbourhood as corroboration, because requiring the backward
    direction let whatever note happened to sit nearest a pipe outrank the
    callout that pointed at it.
    """
    for workspace, _ in (analysis_a, analysis_b):
        for association in workspace.associations:
            if association.state == State.CONFIRMED:
                assert association.forward.get("leaderId"), association
        for hint in workspace.proximity_hints:
            assert hint["usedForAssociation"] is False


def test_a_label_that_only_sits_near_a_pipe_creates_no_association(analysis_a):
    workspace, _ = analysis_a
    associated = {(a.candidate_id, a.pipe_id) for a in workspace.associations}
    for hint in workspace.proximity_hints:
        assert (hint["candidateId"], hint["pipeId"]) not in associated


def test_a_candidate_alone_is_never_a_designation(analysis_a):
    workspace, _ = analysis_a
    associated = {a.candidate_id for a in workspace.associations}
    for candidate in workspace.candidates:
        if candidate.candidate_id not in associated:
            assert candidate.state == State.CANDIDATE
            assert Reason.TEXT_ONLY in candidate.reasons


def test_prose_and_dates_score_below_labels(analysis_a):
    from pdf_forensics.designation_search import _score, _structural_signals
    from pdf_forensics.text_reconstruction import token_structure

    def score(text: str) -> float:
        structure = token_structure(text)
        signals = _structural_signals(structure, text)
        signals.setdefault("repetition", 0.0)
        signals.setdefault("inLegend", 0.0)
        signals.setdefault("hasLeader", 0.0)
        signals.setdefault("textConfidence", 1.0)
        return _score(signals)

    # structure only: no vocabulary, no catalogue
    assert score("XY9-Q4-25") > score("2024-04-19")
    assert score("XY9-Q4-25") > score("ENL PM 2")
    assert score("XY9-Q4-25") > score("100")


def test_every_pipe_names_at_most_one_designation(analysis_a, analysis_b):
    for workspace, _ in (analysis_a, analysis_b):
        for pipe in workspace.physical_pipes:
            owners = [a for a in workspace.resolved.values() if a.pipe_id == pipe.pipe_id]
            assert len(owners) <= 1
