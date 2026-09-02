"""Eighth search: designations, without a list of designations.

Nothing in this module knows any drawing's codes.  A candidate is proposed from
the *structure* of a string - how many alphanumeric runs it has, how they are
joined, whether digits appear, how the sheet repeats it, whether it is set as a
label rather than as prose - and from the geometry around it.

A candidate is never a designation on its own evidence.  Only association with
real pipe geometry, supported from both directions, can confirm one; text that
never reaches a pipe stays a candidate or plain text, and says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from .canonical import canonical_json, entity_id, q, sort_canonical
from .model import (Association, DesignationCandidate, Leader, PhysicalPipe, Reason,
                    State, TextItem)
from .spatial_index import SpatialIndex, bbox_distance
from .text_reconstruction import token_structure

# A candidate must clear this to be worth associating at all.  It is a floor on
# *structure*, not a threshold that can confirm anything.
CANDIDATE_FLOOR = 0.30


def _structural_signals(structure: dict[str, Any], text: str) -> dict[str, float]:
    """How label-shaped is this string?"""
    alnum_runs = structure["alnumRuns"]
    separators = structure["separators"]
    signals: dict[str, float] = {}
    signals["compound"] = 1.0 if alnum_runs >= 2 and structure["separatorCount"] >= 1 else 0.0
    signals["mixedAlphaDigit"] = 1.0 if structure["hasAlpha"] and structure["hasDigit"] else 0.0
    signals["leadingAlpha"] = 1.0 if structure["leadingAlpha"] else 0.0
    signals["compact"] = 1.0 if 3 <= structure["length"] <= 24 and structure["words"] <= 1 else 0.0
    signals["upperCase"] = q(structure["upperFraction"])
    consistent = separators and len(set(separators)) == 1
    signals["consistentSeparator"] = 1.0 if consistent else 0.0
    # A date is three numeric runs of 4-2-2 joined by one separator: structural,
    # not a vocabulary.
    runs = [r for r in structure["runs"] if r["kind"] in ("alpha", "digit")]
    date_like = (
        len(runs) == 3 and all(r["kind"] == "digit" for r in runs)
        and [len(r["text"]) for r in runs] in ([4, 2, 2], [2, 2, 4])
    )
    signals["dateLike"] = 1.0 if date_like else 0.0
    signals["prose"] = 1.0 if structure["words"] > 1 else 0.0
    signals["allDigits"] = 1.0 if structure["hasDigit"] and not structure["hasAlpha"] else 0.0
    return signals


def _score(signals: dict[str, float]) -> float:
    positive = (
        0.30 * signals["compound"]
        + 0.22 * signals["mixedAlphaDigit"]
        + 0.12 * signals["leadingAlpha"]
        + 0.12 * signals["compact"]
        + 0.10 * signals["upperCase"]
        + 0.08 * signals["consistentSeparator"]
        + 0.10 * min(1.0, signals.get("repetition", 0.0))
        + 0.10 * signals.get("inLegend", 0.0)
        + 0.08 * signals.get("hasLeader", 0.0)
    )
    penalty = 0.45 * signals["dateLike"] + 0.30 * signals["prose"] + 0.20 * signals["allDigits"]
    penalty += 0.25 * (1.0 - signals.get("textConfidence", 1.0))
    return q(max(0.0, min(1.0, positive - penalty)))


def find_candidates(text_items: Sequence[TextItem], panel_text_ids: Iterable[str],
                    leaders_by_text: dict[str, Leader]) -> list[DesignationCandidate]:
    """Propose every string that is shaped like a label, and say why."""
    panel_ids = set(panel_text_ids)
    repetition: dict[str, int] = {}
    for item in text_items:
        repetition[item.text] = repetition.get(item.text, 0) + 1
    out: list[DesignationCandidate] = []
    for item in sort_canonical(text_items, key=lambda t: (t.page, t.bbox, t.text_id)):
        text = item.text.strip()
        if not text:
            continue
        structure = token_structure(text)
        signals = _structural_signals(structure, text)
        signals["repetition"] = q(min(1.0, (repetition.get(item.text, 1) - 1) / 3.0))
        signals["inLegend"] = 1.0 if item.text_id in panel_ids else 0.0
        signals["hasLeader"] = 1.0 if item.text_id in leaders_by_text else 0.0
        signals["textConfidence"] = q(item.confidence)
        score = _score(signals)
        if score < CANDIDATE_FLOOR:
            continue
        payload = {"p": item.page, "b": list(item.bbox), "t": text}
        out.append(
            DesignationCandidate(
                candidate_id=entity_id("desig", payload),
                page=item.page,
                text=text,
                bbox=item.bbox,
                rotation=item.rotation,
                text_id=item.text_id,
                glyph_ids=item.glyph_ids,
                source_object_ids=(),
                structure=structure,
                signals=signals,
                score=score,
                state=State.CANDIDATE,
                reasons=(Reason.TEXT_ONLY,),
            )
        )
    return sort_canonical(out, key=lambda c: (c.page, c.bbox, c.text, c.candidate_id))


# ---------------------------------------------------------------------------
# association - the only thing that can confirm a designation
# ---------------------------------------------------------------------------

@dataclass
class PipeGeometryIndex:
    """Pipe centerlines, indexed for the local searches association needs."""

    pipes: Sequence[PhysicalPipe]

    def __post_init__(self) -> None:
        self.by_id = {p.pipe_id: p for p in self.pipes}
        entries = []
        for pipe in self.pipes:
            xs = [p[0] for p in pipe.centerline]
            ys = [p[1] for p in pipe.centerline]
            if not xs:
                continue
            entries.append((pipe.pipe_id, pipe.page, (min(xs), min(ys), max(xs), max(ys))))
        self.index = SpatialIndex(entries)

    def near_point(self, page: int, point: Sequence[float], radius: float) -> list[tuple[float, str]]:
        out: list[tuple[float, str]] = []
        for key in self.index.near_point(page, point, radius):
            pipe = self.by_id[key]
            distance = distance_to_pipe(point, pipe)
            if distance <= radius:
                out.append((q(distance), key))
        return sorted(out, key=lambda pair: (pair[0], pair[1]))

    def near_bbox(self, page: int, bbox: Sequence[float], distance: float) -> list[str]:
        return sorted(self.index.within_distance(page, bbox, distance))


def distance_to_pipe(point: Sequence[float], pipe: PhysicalPipe) -> float:
    """Distance to the pipe's own geometry, part by part.

    A pipe made of several runs is not one polyline; measuring to a straight
    line drawn between two of its parts would invent geometry that is not on
    the sheet.
    """
    parts = pipe.parts or (pipe.centerline,)
    return q(min(_distance_to_polyline(point, part) for part in parts))


def _distance_to_polyline(point: Sequence[float], polyline: Sequence[Sequence[float]]) -> float:
    best = float("inf")
    for i in range(len(polyline) - 1):
        best = min(best, _point_segment(point, polyline[i], polyline[i + 1]))
    return q(best if best < float("inf") else 1e9)


def _point_segment(p: Sequence[float], a: Sequence[float], b: Sequence[float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-12:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length_sq))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def associate(candidates: Sequence[DesignationCandidate], pipes: Sequence[PhysicalPipe],
              leaders_by_text: dict[str, Leader],
              text_items: Sequence[TextItem]) -> tuple[list[Association], list[dict]]:
    """Match designations to pipes along the chain the drawing drew.

    Forward - designation -> its leader -> the leader's far end -> the pipe
    there - is the drawing *stating* the pairing, and it is the only thing that
    can create an association.  Backward - is this label in that pipe's own
    neighbourhood - corroborates a forward claim and raises its confidence; it
    can never make one.  A label that is merely near a pipe produces a
    proximity hint, which is reported and not used: on a real sheet every note,
    date and drawing number is near something.

    Two leaders landing on one pipe is a contradiction, and leaves both
    AMBIGUOUS rather than picking the nearer.
    """
    geometry = PipeGeometryIndex(pipes)
    by_text = {t.text_id: t for t in text_items}
    by_candidate = {c.candidate_id: c for c in candidates}
    forward: dict[tuple[str, str], dict] = {}
    hints: list[dict] = []

    for candidate in sort_canonical(list(candidates), key=lambda c: (c.page, c.bbox, c.candidate_id)):
        leader = leaders_by_text.get(candidate.text_id)
        if leader is None:
            continue
        cap = max(by_text[candidate.text_id].cap_height, 1.0)
        radius = max(2.0, 1.5 * cap)
        hits = geometry.near_point(candidate.page, leader.target_end, radius)
        if not hits:
            continue
        if len(hits) > 1 and abs(hits[0][0] - hits[1][0]) < 0.05:
            forward[(candidate.candidate_id, hits[0][1])] = {
                "leaderId": leader.leader_id,
                "distance": hits[0][0],
                "state": State.AMBIGUOUS,
                "reason": Reason.COMPETING_PIPES_EQUALLY_SUPPORTED,
            }
            continue
        distance, pipe_id = hits[0]
        forward[(candidate.candidate_id, pipe_id)] = {
            "leaderId": leader.leader_id,
            "leaderEnd": list(leader.target_end),
            "distance": distance,
            "rule": "LEADER_ENDS_ON_PIPE",
        }

    # backward: corroboration only - is this label in that pipe's neighbourhood
    text_index = SpatialIndex([(c.candidate_id, c.page, c.bbox) for c in candidates])
    caps = sorted(by_text[c.text_id].cap_height for c in candidates) or [6.0]
    reach = 4.0 * caps[len(caps) // 2]
    backward: dict[tuple[str, str], dict] = {}
    for pipe in pipes:
        xs = [p[0] for p in pipe.centerline]
        ys = [p[1] for p in pipe.centerline]
        if not xs:
            continue
        box = (min(xs), min(ys), max(xs), max(ys))
        for key in text_index.within_distance(pipe.page, box, reach):
            candidate = by_candidate[key]
            if candidate.signals.get("inLegend"):
                continue          # a legend is a key to the sheet, not a label on this pipe
            centre = ((candidate.bbox[0] + candidate.bbox[2]) / 2.0,
                      (candidate.bbox[1] + candidate.bbox[3]) / 2.0)
            distance = distance_to_pipe(centre, pipe)
            leader = leaders_by_text.get(candidate.text_id)
            if leader is not None:
                distance = min(distance, distance_to_pipe(leader.target_end, pipe))
            record = {"distance": q(distance), "rule": "LABEL_IN_THE_PIPE_NEIGHBOURHOOD"}
            pair = (candidate.candidate_id, pipe.pipe_id)
            if pair in forward:
                backward[pair] = record
            else:
                hints.append({"candidateId": candidate.candidate_id, "pipeId": pipe.pipe_id,
                              "distance": q(distance), "text": candidate.text,
                              "usedForAssociation": False})

    # a pipe that two leaders point at is a contradiction, not a majority
    claims: dict[str, list[str]] = {}
    for candidate_id, pipe_id in forward:
        claims.setdefault(pipe_id, []).append(candidate_id)

    associations: list[Association] = []
    for key in sorted(forward):
        candidate_id, pipe_id = key
        f = forward[key]
        b = backward.get(key)
        texts = {by_candidate[c].text for c in claims.get(pipe_id, ())}
        if f.get("state") == State.AMBIGUOUS:
            state, reasons, score = State.AMBIGUOUS, [f["reason"]], 0.4
        elif len(texts) > 1:
            state = State.AMBIGUOUS
            reasons = [Reason.COMPETING_DESIGNATIONS_EQUALLY_SUPPORTED]
            score = 0.4
        else:
            state = State.CONFIRMED
            reasons = [] if b else [Reason.ONE_DIRECTION_ONLY]
            score = q(min(1.0, 0.70 + 0.20 * (1.0 if b else 0.0)
                          + 0.10 * by_candidate[candidate_id].score))
        associations.append(
            Association(
                association_id=entity_id("assoc", {"c": candidate_id, "p": pipe_id}),
                candidate_id=candidate_id,
                pipe_id=pipe_id,
                forward=f,
                backward=b or {},
                score=score,
                state=state,
                reasons=tuple(sorted(set(reasons))),
            )
        )
    return (sort_canonical(associations, key=lambda a: (a.candidate_id, a.pipe_id)),
            sort_canonical(hints, key=lambda h: (h["candidateId"], h["pipeId"])))


def resolve(associations: Sequence[Association]) -> tuple[dict[str, Association], list[Association]]:
    """Pick at most one designation per pipe, and refuse to pick on a tie."""
    by_pipe: dict[str, list[Association]] = {}
    for association in associations:
        by_pipe.setdefault(association.pipe_id, []).append(association)
    resolved: dict[str, Association] = {}
    rejected: list[Association] = []
    for pipe_id in sorted(by_pipe):
        ordered = sorted(by_pipe[pipe_id], key=lambda a: (-a.score, a.candidate_id))
        best = ordered[0]
        if len(ordered) > 1 and abs(ordered[1].score - best.score) < 1e-9:
            for association in ordered:
                rejected.append(
                    Association(
                        association_id=association.association_id,
                        candidate_id=association.candidate_id,
                        pipe_id=association.pipe_id,
                        forward=association.forward,
                        backward=association.backward,
                        score=association.score,
                        state=State.AMBIGUOUS,
                        reasons=tuple(sorted(set(association.reasons)
                                             | {Reason.COMPETING_DESIGNATIONS_EQUALLY_SUPPORTED})),
                    )
                )
            continue
        resolved[pipe_id] = best
        rejected.extend(ordered[1:])
    return resolved, sort_canonical(rejected, key=lambda a: (a.pipe_id, a.candidate_id))


def to_json(candidates: Sequence[DesignationCandidate], associations: Sequence[Association],
            resolved: dict[str, Association]) -> dict:
    states: dict[str, int] = {}
    for association in associations:
        states[association.state] = states.get(association.state, 0) + 1
    return {
        "designationCandidates": len(candidates),
        "associations": len(associations),
        "associationStates": {k: states[k] for k in sorted(states)},
        "pipesWithDesignation": len(resolved),
        "confirmedAssociations": len([a for a in resolved.values() if a.state == State.CONFIRMED]),
    }
