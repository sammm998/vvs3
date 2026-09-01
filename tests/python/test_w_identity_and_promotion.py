"""W. Entity identity, designation promotion, scale hypotheses, drawing roles.

These cover the architectural correction: geometry the file drew twice must be
one entity, text must not become a designation without a pipe behind it, scale
must survive a misread character, and every object must get a drawing role
without any rule reading a layer's name.
"""

from __future__ import annotations

import pytest

from vvs_pipe.canonical import entity_id
from vvs_pipe.designations.promote import promote_designations
from vvs_pipe.geometry.primitives import BBox
from vvs_pipe.measurement.hypotheses import (
    POINT_IN_METRES,
    _iso_a_series,
    ratio_note_hypotheses,
    tolerant_ratio_hypotheses,
)
from vvs_pipe.model import (
    Confidence,
    Designation,
    GlyphCandidate,
    PhysicalPipe,
    PipeCandidate,
    Provenance,
    TextItem,
    TokenStructure,
    VectorObject,
)
from vvs_pipe.pipes.dedupe import dedupe_candidates
from vvs_pipe.topology.physical import RunAssignment
from vvs_pipe.roles import classify_roles
from vvs_pipe.states import DesignationTier, DrawingRole, IdentityState, Reason, TextRole


# --------------------------------------------------------------------- helpers


def _candidate(points, style="double_line", width=6.0, suffix="") -> PipeCandidate:
    return PipeCandidate(
        candidate_id=entity_id("pc", (1, points, style, suffix)),
        page=1,
        centerline=tuple(points),
        style=style,
        width_pt=width,
        stroke_width=0.5,
        color=None,
        dashes=None,
        source_object_ids=(f"obj{suffix or '0'}",),
        accepted=True,
        rejection_reason=None,
        confidence=Confidence(geometry=0.9),
        evidence=(),
        provenance=Provenance(stage="test", rule="fixture"),
    )


def _designation(text, did="des1", role=TextRole.PIPE_DESIGNATION) -> Designation:
    return Designation(
        designation_id=did,
        page=1,
        text=text,
        bbox=BBox(0.0, 0.0, 10.0, 5.0),
        rotation=0.0,
        role=role,
        role_scores=((role.value, 0.8),),
        is_legend=False,
        structure=TokenStructure(parts=(), pattern=""),
        diameter_mm=None,
        diameter_reason=None,
        system_token=None,
        text_item_id="txt1",
        glyph_ids=(),
        source_object_ids=(),
        confidence=Confidence(text=0.9),
        state=IdentityState.HIGH_CONFIDENCE,
        reasons=(),
        associated_physical_pipe_ids=(),
        provenance=Provenance(stage="test", rule="fixture"),
        tier=DesignationTier.DESIGNATION_CANDIDATE,
    )


def _pipe(designation, run_ids, pid="pp1", length=120.0) -> PhysicalPipe:
    return PhysicalPipe(
        physical_pipe_id=pid,
        page=1,
        pipe_run_ids=tuple(run_ids),
        centerline=(((0.0, 0.0), (length, 0.0)),),
        source_object_ids=(),
        horizontal_length_m=None,
        vertical_length_m=None,
        total_length_m=None,
        length_pt=length,
        diameter_mm=None,
        designation=designation,
        designation_ids=(),
        vertical_ids=(),
        identity_state=IdentityState.HIGH_CONFIDENCE,
        reasons=(),
        confidence=Confidence(geometry=0.9),
        evidence=(),
        provenance=Provenance(stage="test", rule="fixture"),
    )


def _assignment(did, leader=0.9, orientation=0.5) -> RunAssignment:
    return RunAssignment(
        designation=None,
        designation_ids=(did,),
        diameter_mm=None,
        state=IdentityState.HIGH_CONFIDENCE,
        reasons=(),
        association_confidence=0.8,
        evidence=(("leader", leader), ("orientation", orientation), ("proximity", 0.9)),
    )


def _text(text, x0=0.0, y0=0.0, x1=40.0, y1=9.0, glyph_ids=(), conf=0.9) -> TextItem:
    return TextItem(
        text_id=entity_id("txt", (text, x0, y0)),
        page=1,
        text=text,
        bbox=BBox(x0, y0, x1, y1),
        rotation=0.0,
        height=y1 - y0,
        origin="glyph",
        glyph_ids=tuple(glyph_ids),
        source_object_ids=(),
        confidence=conf,
        state=IdentityState.HIGH_CONFIDENCE,
        reasons=(),
        provenance=Provenance(stage="test", rule="fixture"),
    )


def _glyph(gid, char, alternatives, conf=0.8, x0=0.0) -> GlyphCandidate:
    return GlyphCandidate(
        glyph_id=gid,
        page=1,
        bbox=BBox(x0, 0.0, x0 + 4.0, 9.0),
        source_object_ids=(),
        stroke_count=1,
        holes=0,
        aspect=0.5,
        complexity=1.0,
        contour_signature=(),
        character=char,
        alternatives=tuple(alternatives),
        confidence=conf,
        state=IdentityState.HIGH_CONFIDENCE,
        reasons=(),
        provenance=Provenance(stage="test", rule="fixture"),
    )


def _object(points, oid, layer="L", closed=False, dashes=None, width=0.7) -> VectorObject:
    return VectorObject(
        object_id=oid,
        page=1,
        kind="line",
        points=tuple(points),
        closed=closed,
        stroke_color=(0.0, 0.0, 0.0),
        fill_color=None,
        stroke_width=width,
        dashes=dashes,
        layer=layer,
        even_odd=False,
        from_annotation=False,
    )


# ------------------------------------------------------------------- identity


def test_a_line_drawn_twice_is_one_candidate():
    line = ((0.0, 0.0), (100.0, 0.0))
    kept, exact, concentric = dedupe_candidates([_candidate(line), _candidate(line)])
    assert len(kept) == 1
    assert exact == 1
    assert concentric == 0
    assert ("duplicateInstances", 2.0) in kept[0].evidence


def test_the_reverse_of_a_line_is_the_same_line():
    kept, exact, _ = dedupe_candidates(
        [
            _candidate(((0.0, 0.0), (100.0, 0.0))),
            _candidate(((100.0, 0.0), (0.0, 0.0))),
        ]
    )
    assert len(kept) == 1, "direction is not part of what a drawn line is"
    assert exact == 1


def test_two_pipes_running_close_together_are_not_merged():
    kept, exact, concentric = dedupe_candidates(
        [
            _candidate(((0.0, 0.0), (100.0, 0.0))),
            _candidate(((0.0, 8.0), (100.0, 8.0)), suffix="b"),
        ]
    )
    assert len(kept) == 2
    assert exact == 0 and concentric == 0


def test_a_pipe_inside_its_jacket_is_one_pipe_carrying_both_widths():
    line = ((0.0, 0.0), (100.0, 0.0))
    kept, exact, concentric = dedupe_candidates(
        [
            _candidate(line, width=8.0, suffix="bore"),
            _candidate(line, width=26.0, suffix="jacket"),
        ]
    )
    assert len(kept) == 1
    assert concentric == 1
    evidence = dict(kept[0].evidence)
    assert evidence["concentricMinWidthPt"] == 8.0
    assert evidence["concentricMaxWidthPt"] == 26.0
    assert kept[0].width_pt == 8.0, "the narrower pairing is the bore"


def test_deduplication_does_not_depend_on_order():
    line = ((0.0, 0.0), (100.0, 0.0))
    a = _candidate(line, width=8.0, suffix="bore")
    b = _candidate(line, width=26.0, suffix="jacket")
    forward, *_ = dedupe_candidates([a, b])
    reverse, *_ = dedupe_candidates([b, a])
    assert [c.to_canonical() for c in forward] == [c.to_canonical() for c in reverse]


# ------------------------------------------------------------------ promotion


def test_text_a_pipe_accepted_becomes_a_confirmed_designation():
    d = _designation("S1-P2-75")
    promoted = promote_designations(
        [d],
        {"des1": ("run1",)},
        [_pipe("S1-P2-75", ["run1"])],
        {"run1": _assignment("des1")},
    )
    assert promoted[0].tier is DesignationTier.CONFIRMED_DESIGNATION
    assert promoted[0].associated_physical_pipe_ids == ("pp1",)


def test_a_label_that_only_sits_near_a_pipe_is_not_confirmed():
    """The failure that put "ENL. PM-2" in the quantity list.

    A note beside a pipe is close to it, and closeness is all the old rule
    asked for.  Confirmation now needs the label to point at the pipe or run
    along it.
    """
    d = _designation("ENL. PM-2")
    promoted = promote_designations(
        [d],
        {"des1": ("run1",)},
        [_pipe("ENL. PM-2", ["run1"])],
        {"run1": _assignment("des1", leader=0.0, orientation=0.4)},
    )
    assert promoted[0].tier is DesignationTier.DESIGNATION_CANDIDATE
    assert dict(promoted[0].pipe_evidence)["pointsAtItsPipe"] == 0.0


def test_a_label_written_along_its_pipe_is_confirmed_without_a_leader():
    d = _designation("S3-R8-110")
    promoted = promote_designations(
        [d],
        {"des1": ("run1",)},
        [_pipe("S3-R8-110", ["run1"])],
        {"run1": _assignment("des1", leader=0.0, orientation=0.95)},
    )
    assert promoted[0].tier is DesignationTier.CONFIRMED_DESIGNATION


def test_code_like_text_with_no_pipe_stays_a_candidate():
    # This is the shape of the failure the rebuild is for: a date and a note
    # are spelled like codes, so the text stage will always offer them.
    for text in ("2024-04-19", "ENL. PM-2", "SF 9ITNINS W-50-1-4-0111."):
        d = _designation(text)
        promoted = promote_designations([d], {}, [])
        assert promoted[0].tier is DesignationTier.DESIGNATION_CANDIDATE
        assert Reason.NO_PIPE_EVIDENCE in promoted[0].reasons
        assert promoted[0].state is IdentityState.AMBIGUOUS


def test_a_pipe_carrying_a_different_label_does_not_confirm_this_one():
    d = _designation("S1-P2-75")
    promoted = promote_designations(
        [d], {"des1": ("run1",)}, [_pipe("VV1-X31", ["run1"])], {"run1": _assignment("des1")}
    )
    assert promoted[0].tier is DesignationTier.DESIGNATION_CANDIDATE


def test_text_that_is_not_a_pipe_designation_is_text_only():
    d = _designation("TVATT", role=TextRole.ROOM_LABEL)
    promoted = promote_designations([d], {}, [])
    assert promoted[0].tier is DesignationTier.TEXT_ONLY
    assert Reason.NO_PIPE_EVIDENCE not in promoted[0].reasons


# ---------------------------------------------------------------------- scale


def test_a_ratio_note_gives_the_scale_directly():
    [h] = ratio_note_hypotheses([_text("SKALA 1:50")])
    assert h.ratio_denominator == 50.0
    assert h.metres_per_point == pytest.approx(POINT_IN_METRES * 50.0)


def test_two_ratios_are_resolved_by_the_sheet_size_not_by_preference():
    a1 = BBox(0.0, 0.0, 2384.0, 1684.0)
    items = [_text("1:50 (1:100)", 0.0, 0.0, 60.0, 9.0), _text("A1 (A3)", 0.0, 10.5, 40.0, 19.0)]
    hyps = ratio_note_hypotheses(items, a1)
    assert [h.ratio_denominator for h in hyps] == [50.0]
    assert dict(hyps[0].evidence)["paperQualified"] == 1.0


def test_a_sheet_that_is_not_a_standard_size_leaves_both_ratios_standing():
    odd = BBox(0.0, 0.0, 500.0, 500.0)
    items = [_text("1:50 (1:100)", 0.0, 0.0, 60.0, 9.0), _text("A1 (A3)", 0.0, 10.5, 40.0, 19.0)]
    assert sorted(h.ratio_denominator for h in ratio_note_hypotheses(items, odd)) == [50.0, 100.0]


def test_iso_sizes_are_derived_not_guessed():
    assert _iso_a_series(BBox(0.0, 0.0, 2384.0, 1684.0)) == "A1"
    assert _iso_a_series(BBox(0.0, 0.0, 1190.55, 841.89)) == "A3"
    assert _iso_a_series(BBox(0.0, 0.0, 500.0, 500.0)) is None


def test_a_misread_character_can_be_recovered_from_the_classifier_alternatives():
    # The mark was read as a full stop; the classifier had a colon ranked just
    # behind it.  Nothing here invents a character the classifier never saw.
    glyphs = {
        "g0": _glyph("g0", "1", (("1", 0.9),), x0=0.0),
        "g1": _glyph("g1", ".", ((".", 0.8), (":", 0.75)), conf=0.8, x0=4.0),
        "g2": _glyph("g2", "5", (("5", 0.9),), x0=8.0),
        "g3": _glyph("g3", "0", (("0", 0.9),), x0=12.0),
    }
    item = _text("1.50", glyph_ids=("g0", "g1", "g2", "g3"))
    [h] = tolerant_ratio_hypotheses([item], glyphs)
    assert h.ratio_denominator == 50.0
    assert h.weight < 0.9, "a recovered reading must not outweigh a clean one"


def test_a_confident_misread_is_not_recovered_cheaply():
    glyphs = {
        "g0": _glyph("g0", "1", (("1", 0.9),), x0=0.0),
        "g1": _glyph("g1", ".", ((".", 0.99), (":", 0.01)), conf=0.99, x0=4.0),
        "g2": _glyph("g2", "5", (("5", 0.9),), x0=8.0),
        "g3": _glyph("g3", "0", (("0", 0.9),), x0=12.0),
    }
    [h] = tolerant_ratio_hypotheses([_text("1.50", glyph_ids=("g0", "g1", "g2", "g3"))], glyphs)
    assert h.weight < 0.15, "the classifier was sure; the alternative is weak evidence"


# ---------------------------------------------------------------------- roles


def test_every_object_receives_a_role():
    objects = [
        _object(((0.0, 0.0), (200.0, 0.0)), "o1"),
        _object(((0.0, 10.0), (200.0, 10.0)), "o2"),
        _object(((1.0, 1.0), (2.0, 2.0)), "o3"),
    ]
    result = classify_roles(objects, BBox(0.0, 0.0, 400.0, 400.0), cap_height=7.0)
    assert len(result.assignments) == len(objects)
    assert set(result.by_object) == {"o1", "o2", "o3"}
    assert sum(result.counts().values()) == len(objects)


def test_lettering_identified_by_grouping_is_labelled_text():
    objects = [_object(((0.0, 0.0), (2.0, 5.0)), "g1"), _object(((0.0, 0.0), (200.0, 0.0)), "p1")]
    result = classify_roles(
        objects, BBox(0.0, 0.0, 400.0, 400.0), cap_height=7.0, text_object_ids=frozenset({"g1"})
    )
    assert result.by_object["g1"] is DrawingRole.TEXT
    assert result.by_object["p1"] is not DrawingRole.TEXT


def test_a_family_of_lines_spanning_the_drawing_is_a_grid():
    objects = [
        _object(((0.0, 0.0), (400.0, 0.0)), "grid1"),
        _object(((0.0, 100.0), (400.0, 100.0)), "grid2"),
        _object(((0.0, 200.0), (400.0, 200.0)), "grid3"),
        _object(((10.0, 5.0), (30.0, 5.0)), "short"),
    ]
    result = classify_roles(objects, BBox(0.0, 0.0, 400.0, 400.0), cap_height=7.0)
    assert all(result.by_object[f"grid{i}"] is DrawingRole.GRID for i in (1, 2, 3))
    assert result.by_object["short"] is not DrawingRole.GRID


def test_one_long_stroke_alone_is_not_a_grid_line():
    """A schematic's longest pipe spans the sheet too; calling it a grid line
    would delete it from the take-off."""
    objects = [
        _object(((0.0, 0.0), (400.0, 0.0)), "long"),
        _object(((10.0, 5.0), (30.0, 5.0)), "short"),
    ]
    result = classify_roles(objects, BBox(0.0, 0.0, 400.0, 400.0), cap_height=7.0)
    assert result.by_object["long"] is not DrawingRole.GRID


def test_no_rule_reads_a_layer_name():
    """The same geometry must classify identically whatever the layers are called."""
    def build(layer_a: str, layer_b: str):
        return [
            _object(((0.0, 0.0), (200.0, 0.0)), "a1", layer=layer_a),
            _object(((200.0, 0.0), (200.0, 150.0)), "a2", layer=layer_a),
            _object(((5.0, 5.0), (7.0, 9.0)), "b1", layer=layer_b),
            _object(((9.0, 5.0), (11.0, 9.0)), "b2", layer=layer_b),
        ]

    box = BBox(0.0, 0.0, 400.0, 400.0)
    first = classify_roles(build("V-53BB--T--S3--", "slipstext"), box, 7.0)
    second = classify_roles(build("PIPES", "TEXT"), box, 7.0)
    assert {k: v.value for k, v in first.by_object.items()} == {
        k: v.value for k, v in second.by_object.items()
    }


def test_role_classification_does_not_depend_on_object_order():
    objects = [
        _object(((0.0, 0.0), (200.0, 0.0)), "o1"),
        _object(((0.0, 10.0), (400.0, 10.0)), "o2"),
        _object(((1.0, 1.0), (3.0, 6.0)), "o3"),
    ]
    box = BBox(0.0, 0.0, 400.0, 400.0)
    forward = classify_roles(objects, box, 7.0)
    reverse = classify_roles(list(reversed(objects)), box, 7.0)
    assert forward.to_canonical() == reverse.to_canonical()
