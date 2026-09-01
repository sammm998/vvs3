"""E. Legend separation test."""

from __future__ import annotations

from vvs_pipe.states import TextRole


def test_the_same_code_is_a_legend_entry_inside_the_panel_and_a_callout_outside(
    analysis_a, drawing_a
):
    page = analysis_a.pages[0]
    for code in drawing_a["designations"]:
        instances = [d for d in page.designations if d.text == code]
        assert len(instances) >= 2, f"{code} should appear both in the legend and on the drawing"
        assert any(d.is_legend for d in instances), f"{code} has no legend instance"
        assert any(
            d.role is TextRole.PIPE_DESIGNATION and not d.is_legend for d in instances
        ), f"{code} has no drawing callout"


def test_legend_instances_never_become_pipes(analysis_a):
    page = analysis_a.pages[0]
    legend_ids = {d.designation_id for d in page.designations if d.is_legend}
    for pipe in page.physical_pipes:
        assert not (set(pipe.designation_ids) & legend_ids)


def test_legend_geometry_is_excluded_from_pipe_detection(analysis_a):
    """The sample lines drawn beside each legend entry are not pipework."""
    page = analysis_a.pages[0]
    panels = [p.bbox for p in page.panels]
    assert panels
    for candidate in page.candidates:
        for x, y in candidate.centerline:
            assert not any(p.contains_point((x, y)) for p in panels)


def test_panels_are_found_without_assuming_where_they_sit(analysis_a, analysis_b):
    # Two panels on each sheet: the legend and the title block.  Neither is
    # located by a hardcoded corner.
    assert len(analysis_a.pages[0].panels) == 2
    assert len(analysis_b.pages[0].panels) == 2
