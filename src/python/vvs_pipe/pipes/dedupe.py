"""Collapse pipe candidates that are the same entity.

A CAD file routinely emits the same line twice - two identical strokes on top of
each other, or a double-line pipe whose two edges were each drawn twice, giving
two pairings whose midlines coincide exactly.  The engine addresses entities by
content, so two candidates with the same page, the same direction-independent
centerline and the same style *are the same candidate*; keeping both counts the
same drawn metre twice and makes the run appear in two physical pipes.

Merging is not a tolerance and not a heuristic.  Only exact identity under the
existing canonical key collapses, so two pipes that genuinely run close together
are never joined, and the result does not depend on the order the duplicates
arrived in: the surviving member is chosen by canonical sort, the source object
ids are unioned, and the number of instances is recorded as evidence so the
merge is visible in the report rather than silent.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from ..canonical import canonical_json, canonical_sort
from ..model import PipeCandidate


def dedupe_candidates(
    candidates: Sequence[PipeCandidate],
) -> tuple[tuple[PipeCandidate, ...], int, int]:
    """Return the distinct candidates, exact duplicates removed, concentric merged."""
    exact, removed = _collapse_exact(candidates)
    distinct, concentric = _collapse_concentric(exact)
    return tuple(canonical_sort(distinct, key=lambda c: c.canonical_key())), removed, concentric


def _collapse_exact(
    candidates: Sequence[PipeCandidate],
) -> tuple[list[PipeCandidate], int]:
    # Keyed on the width as well as the canonical key.  Two pairings that share
    # a centerline and a style but not a separation are *not* the same
    # candidate - one is a pipe and the other its jacket - and collapsing them
    # here would throw one of the two widths away before the concentric pass
    # could record both.
    groups: dict[tuple, list[PipeCandidate]] = {}
    for c in candidates:
        groups.setdefault(
            (c.canonical_key(), -1.0 if c.width_pt is None else round(c.width_pt, 4)), []
        ).append(c)

    out: list[PipeCandidate] = []
    removed = 0
    for key in sorted(groups, key=lambda k: canonical_json([_key_repr(k[0]), k[1]])):
        members = canonical_sort(groups[key], key=lambda c: canonical_json(c.to_canonical()))
        keeper = members[0]
        if len(members) == 1:
            out.append(keeper)
            continue
        removed += len(members) - 1
        sources = tuple(sorted({o for m in members for o in m.source_object_ids}))
        evidence = tuple(
            sorted(
                dict(keeper.evidence + (("duplicateInstances", float(len(members))),)).items()
            )
        )
        out.append(replace(keeper, source_object_ids=sources, evidence=evidence))
    return out, removed


# A wider pairing sharing a narrower one's midline is that pipe's jacket, not a
# second pipe.  Style preference records which detection carries more
# information about what was drawn: a double-line pairing measured a width, a
# dashed chain reassembled a linetype, a lone stroke asserted the least.
_STYLE_EVIDENCE = {"double_line": 0, "dashed_line": 1, "single_line": 2}


def _collapse_concentric(
    candidates: Sequence[PipeCandidate],
) -> tuple[list[PipeCandidate], int]:
    """Collapse candidates that share a centerline but not a width.

    Two concentric double-line pairings - a pipe inside its insulation, a duct
    inside its lining - have the same midline and different separations.  They
    are one pipe drawn twice over, and measuring both counts the same metre
    twice.  Which separation is the pipe's own bore is a *dimension* question,
    not an identity question, so it is not decided here: the survivor keeps the
    strongest-evidence style and both widths are recorded so the dimension stage
    can reconcile them against the label.
    """
    groups: dict[tuple, list[PipeCandidate]] = {}
    for c in candidates:
        page, points, _style = c.canonical_key()
        groups.setdefault((page, points), []).append(c)

    out: list[PipeCandidate] = []
    merged = 0
    for key in sorted(groups, key=lambda k: canonical_json([k[0], [[x, y] for x, y in k[1]]])):
        members = groups[key]
        if len(members) == 1:
            out.append(members[0])
            continue
        merged += len(members) - 1
        ordered = canonical_sort(
            members,
            key=lambda c: (
                _STYLE_EVIDENCE.get(c.style, len(_STYLE_EVIDENCE)),
                1e9 if c.width_pt is None else c.width_pt,
                canonical_json(c.to_canonical()),
            ),
        )
        keeper = ordered[0]
        widths = sorted(m.width_pt for m in members if m.width_pt is not None)
        sources = tuple(sorted({o for m in members for o in m.source_object_ids}))
        extra: dict[str, float] = {"concentricCandidates": float(len(members))}
        if widths:
            extra["concentricMinWidthPt"] = widths[0]
            extra["concentricMaxWidthPt"] = widths[-1]
        out.append(
            replace(
                keeper,
                source_object_ids=sources,
                evidence=tuple(sorted(dict(keeper.evidence + tuple(extra.items())).items())),
            )
        )
    return out, merged


def _key_repr(key: tuple) -> list:
    """A JSON-representable form of a canonical key, for stable ordering."""
    page, points, style = key
    return [page, [[x, y] for x, y in points], style]
