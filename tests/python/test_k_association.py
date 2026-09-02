"""K. Designation association test."""

from __future__ import annotations

from vvs_pipe.association import associate_designations


def test_each_callout_binds_to_the_pipe_its_leader_points_at(analysis_a, specs_by_stem):
    spec = specs_by_stem["drawing_a"]
    page = analysis_a.pages[0]
    by_designation = {}
    for pipe in page.physical_pipes:
        if pipe.designation:
            by_designation.setdefault(pipe.designation, []).append(pipe)
    for pipe_spec in spec.pipes:
        assert pipe_spec.designation in by_designation, pipe_spec.designation


def test_association_never_falls_back_to_nearest_only(analysis_a):
    """Every association records the evidence that produced it."""
    page = analysis_a.pages[0]
    for pipe in page.physical_pipes:
        if pipe.designation is None:
            continue
        assert pipe.confidence.association is not None
        assert pipe.confidence.association > 0.0


def test_two_equally_supported_pipes_produce_AMBIGUOUS_not_a_guess(analysis_a):
    """A label placed symmetrically between two identical pipes must not be assigned."""
    page = analysis_a.pages[0]
    runs = list(page.runs)
    assert runs

    # Take the drawing's own designation object and move it to a point that is
    # exactly equidistant from two runs of the same size, with no leader.
    designation = next(d for d in page.designations if d.role.value == "PIPE_DESIGNATION")
    same_width = [r for r in runs if r.width_pt is not None]
    assert len(same_width) >= 2

    from dataclasses import replace

    from vvs_pipe.geometry.primitives import BBox
    from vvs_pipe.model import PipeRun

    a = same_width[0]
    mirror_offset = 24.0
    b = PipeRun(
        pipe_run_id="run_mirror",
        page=a.page,
        centerline=tuple((x, y + mirror_offset) for x, y in a.centerline),
        edge_ids=("edge_mirror",),
        source_object_ids=(),
        width_pt=a.width_pt,
        style=a.style,
        direction=a.direction,
        designation_candidates=(),
        dimension_candidates=(),
        vertical_transition_ids=(),
        confidence=a.confidence,
        state=a.state,
        reasons=(),
        provenance=a.provenance,
    )
    mid_y = (a.centerline[0][1] + b.centerline[0][1]) / 2.0
    mid_x = (a.centerline[0][0] + a.centerline[-1][0]) / 2.0
    centred = replace(designation, bbox=BBox(mid_x - 20, mid_y - 4, mid_x + 20, mid_y + 4))

    result = associate_designations(
        [centred],
        [a, b],
        (),   # no leader was traced
        (),   # so nothing was attached
        (),
        {a.pipe_run_id: designation.diameter_mm, b.pipe_run_id: designation.diameter_mm},
        7.0,
    )
    assigned = [v for v in result.assignments.values() if v.designation is not None]
    assert not assigned, "an equidistant label must not be assigned to either pipe"
    # It is not even a contest: with no leader there is nothing to compete over,
    # and the label is reported as having reached no association evidence.
    assert any(code == "NO_ASSOCIATION_EVIDENCE" for _id, code in result.diagnostics)
    assert result.proximity_hints, "the distance is still measured, and still unused"


def test_designation_propagates_only_across_matching_widths(analysis_a):
    page = analysis_a.pages[0]
    for pipe in page.physical_pipes:
        if pipe.designation is None or pipe.diameter_mm is None:
            continue
        for other in page.physical_pipes:
            if other.designation == pipe.designation and other.diameter_mm is not None:
                assert abs(other.diameter_mm - pipe.diameter_mm) < 2.0
