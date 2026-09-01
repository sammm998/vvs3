"""F. Pipe detection test, and G. centerline test."""

from __future__ import annotations

import math

import pytest

from vvs_pipe.geometry.primitives import Segment
from vvs_pipe.pipes import PairingConfig, SegmentRef, pair_double_lines


def _ref(a, b, oid, w=0.35, color=(0.0, 0.0, 0.0), dashes=None) -> SegmentRef:
    return SegmentRef(Segment(a, b), oid, w, color, dashes)


def test_two_parallel_strokes_pair_into_a_centerline():
    pairs, consumed = pair_double_lines(
        [_ref((0, 95), (200, 95), "a"), _ref((0, 105), (200, 105), "b")]
    )
    assert len(pairs) == 1
    p = pairs[0]
    assert p.width_pt == pytest.approx(10.0)
    assert p.centerline[0] == pytest.approx((0.0, 100.0))
    assert p.centerline[1] == pytest.approx((200.0, 100.0))
    assert consumed == {"a", "b"}


def test_pipe_width_is_never_confused_with_pipe_length():
    """A short, wide pair still measures 20 pt long and 60 pt wide."""
    pairs, _ = pair_double_lines(
        [_ref((0, 70), (20, 70), "a"), _ref((0, 130), (20, 130), "b")]
    )
    assert len(pairs) == 1
    p = pairs[0]
    assert p.width_pt == pytest.approx(60.0)
    length = math.dist(p.centerline[0], p.centerline[1])
    assert length == pytest.approx(20.0)


def test_strokes_that_are_not_parallel_do_not_pair():
    pairs, _ = pair_double_lines(
        [_ref((0, 95), (200, 95), "a"), _ref((0, 105), (200, 160), "b")]
    )
    assert pairs == []


def test_strokes_of_different_weight_or_colour_do_not_pair():
    assert (
        pair_double_lines(
            [_ref((0, 95), (200, 95), "a", w=0.35), _ref((0, 105), (200, 105), "b", w=1.4)]
        )[0]
        == []
    )
    assert (
        pair_double_lines(
            [
                _ref((0, 95), (200, 95), "a", color=(0, 0, 0)),
                _ref((0, 105), (200, 105), "b", color=(1, 0, 0)),
            ]
        )[0]
        == []
    )


def test_one_stroke_cannot_pair_with_itself():
    """Both limbs of a letter M belong to one object and must not form a pipe."""
    pairs, _ = pair_double_lines([_ref((0, 0), (0, 40), "same"), _ref((10, 0), (10, 40), "same")])
    assert pairs == []


def test_pairing_is_order_independent():
    from vvs_pipe.validation.determinism import permutations_of

    refs = [
        _ref((0, 95), (200, 95), "a"),
        _ref((0, 105), (200, 105), "b"),
        _ref((0, 195), (200, 195), "c"),
        _ref((0, 205), (200, 205), "d"),
    ]
    baseline = None
    for _name, permuted in permutations_of(refs):
        pairs, consumed = pair_double_lines(permuted)
        signature = ([p.key() for p in pairs], sorted(consumed))
        if baseline is None:
            baseline = signature
        else:
            assert signature == baseline


def test_detection_finds_every_drawn_pipe_and_nothing_else(analysis_a, specs_by_stem):
    spec = specs_by_stem["drawing_a"]
    page = analysis_a.pages[0]
    doubles = [c for c in page.candidates if c.style == "double_line"]
    assert doubles, "no double-line pipes detected"

    mpp = spec.metres_per_point
    expected_widths = sorted({p.dn_mm for p in spec.pipes})
    got_widths = sorted({round(c.width_pt * mpp * 1000.0) for c in doubles})
    assert got_widths == [round(w) for w in expected_widths]

    # No candidate may sit inside a panel or run along the sheet frame.
    for c in page.candidates:
        assert c.length_pt > 0


def test_single_line_candidates_carry_no_measured_width(analysis_b):
    for page in analysis_b.pages:
        for c in page.candidates:
            if c.style == "single_line":
                assert c.width_pt is None
                assert c.rejection_reason is not None
