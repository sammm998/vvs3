"""M. Vertical pipe test."""

from __future__ import annotations

import pytest

from vvs_pipe.states import IdentityState, Reason


def test_two_elevations_give_a_vertical_length(analysis_a, specs_by_stem):
    spec = specs_by_stem["drawing_a"]
    riser = next(r for r in spec.risers if r["resolved_length_m"] is not None)
    verticals = analysis_a.pages[0].verticals
    resolved = [v for v in verticals if v.length_m is not None]
    assert resolved, "the riser annotated with two levels should resolve"
    assert any(v.length_m == pytest.approx(riser["resolved_length_m"], abs=0.005) for v in resolved)
    for v in resolved:
        assert v.state is IdentityState.CONFIRMED
        assert v.from_elevation_m is not None and v.to_elevation_m is not None


def test_one_elevation_is_reported_unknown_and_never_guessed(analysis_a, specs_by_stem):
    spec = specs_by_stem["drawing_a"]
    assert any(r["resolved_length_m"] is None for r in spec.risers)
    unresolved = [v for v in analysis_a.pages[0].verticals if v.length_m is None]
    assert unresolved, "a riser with a single level must not produce a length"
    for v in unresolved:
        assert Reason.VERTICAL_HEIGHT_UNKNOWN in v.reasons
        assert v.state is IdentityState.INSUFFICIENT


def test_an_unresolved_riser_downgrades_the_pipe_that_carries_it(analysis_a):
    page = analysis_a.pages[0]
    unresolved_ids = {v.vertical_id for v in page.verticals if v.length_m is None}
    carriers = [p for p in page.physical_pipes if set(p.vertical_ids) & unresolved_ids]
    assert carriers
    for p in carriers:
        assert Reason.VERTICAL_HEIGHT_UNKNOWN in p.reasons
        assert p.identity_state is not IdentityState.CONFIRMED


def test_vertical_length_is_added_to_the_total_not_to_the_horizontal(analysis_a):
    for page in analysis_a.pages:
        for p in page.physical_pipes:
            if p.total_length_m is None:
                continue
            expected = (p.horizontal_length_m or 0.0) + (p.vertical_length_m or 0.0)
            assert p.total_length_m == pytest.approx(expected, abs=1e-4)


def test_elevation_parsing_accepts_any_prefix():
    from vvs_pipe.designations.discovery import parse_elevation

    assert parse_elevation("VG+2.800") == pytest.approx(2.800)
    assert parse_elevation("FG+0,150") == pytest.approx(0.150)
    assert parse_elevation("OK-1.250") == pytest.approx(-1.250)
    assert parse_elevation("+3.000") == pytest.approx(3.000)
    assert parse_elevation("S1-P2-110") is None
