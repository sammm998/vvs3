"""Graph edges -> PipeRuns.

A run is a maximal chain of edges that a reader would follow as one length of
pipe.  The chaining rule is *mutual best continuation*: at a node, edge A
continues into edge B only if B is A's best continuation **and** A is B's.
That makes the chaining a stable pairing which is independent of the order the
edges are visited in - the property the specification demands - and it stops a
run at a genuine ambiguity (two equally straight continuations) instead of
picking one.

Continuation requires compatible drawn width and style, and a turn below the
configured limit.  Anything else terminates the run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from ..canonical import canonical_sort, entity_id, ql, qs
from ..geometry.primitives import Segment, angle_diff, dist, polyline_length
from ..model import Confidence, GraphEdge, PipeRun, Provenance
from ..states import IdentityState, Reason
from .graph_build import PipeGraph

Pt = tuple[float, float]

MAX_TURN_RAD = math.radians(35.0)
WIDTH_RELATIVE_TOLERANCE = 0.12
TIE_EPSILON_RAD = math.radians(3.0)
DIRECTION_TOLERANCE_RAD = math.radians(8.0)


@dataclass(frozen=True, slots=True)
class _Half:
    edge_id: str
    node_id: str
    direction: float  # orientation of the edge leaving this node


def _edge_direction_at(edge: GraphEdge, node_point: Pt) -> float:
    if dist(edge.polyline[0], node_point) <= dist(edge.polyline[-1], node_point):
        a, b = edge.polyline[0], edge.polyline[1]
    else:
        a, b = edge.polyline[-1], edge.polyline[-2]
    return math.atan2(b[1] - a[1], b[0] - a[0])


def _width_compatible(a: GraphEdge, b: GraphEdge) -> bool:
    if a.style != b.style:
        return False
    if a.width_pt is None or b.width_pt is None:
        return a.width_pt is None and b.width_pt is None
    m = max(a.width_pt, b.width_pt, 1e-9)
    return abs(a.width_pt - b.width_pt) / m <= WIDTH_RELATIVE_TOLERANCE


def _turn(d1: float, d2: float) -> float:
    """Turn angle when leaving one edge and entering another at a shared node."""
    delta = abs(((d2 - (d1 + math.pi)) + math.pi) % (2 * math.pi) - math.pi)
    return delta


def build_runs(graph: PipeGraph, page: int) -> tuple[PipeRun, ...]:
    edges = {e.edge_id: e for e in graph.edges}
    nodes = graph.node_map()
    incident = graph.incident()

    best: dict[tuple[str, str], str | None] = {}
    for node_id, edge_ids in sorted(incident.items()):
        node = nodes[node_id]
        halves = [
            _Half(eid, node_id, _edge_direction_at(edges[eid], node.point))
            for eid in sorted(edge_ids)
        ]
        for h in halves:
            scored: list[tuple[float, str]] = []
            for other in halves:
                if other.edge_id == h.edge_id:
                    continue
                if not _width_compatible(edges[h.edge_id], edges[other.edge_id]):
                    continue
                turn = _turn(h.direction, other.direction)
                if turn > MAX_TURN_RAD:
                    continue
                scored.append((turn, other.edge_id))
            scored.sort(key=lambda t: (qs(t[0]), t[1]))
            if not scored:
                best[(node_id, h.edge_id)] = None
            elif len(scored) > 1 and scored[1][0] - scored[0][0] < TIE_EPSILON_RAD:
                best[(node_id, h.edge_id)] = None  # competing continuations
            else:
                best[(node_id, h.edge_id)] = scored[0][1]

    def links(node_id: str, edge_id: str) -> str | None:
        other = best.get((node_id, edge_id))
        if other is None:
            return None
        if best.get((node_id, other)) != edge_id:
            return None  # not mutual
        return other

    visited: set[str] = set()
    runs: list[PipeRun] = []
    for edge_id in sorted(edges):
        if edge_id in visited:
            continue
        chain = [edge_id]
        visited.add(edge_id)
        for direction in (0, 1):
            cur = edge_id
            cur_node = edges[edge_id].node_a if direction == 0 else edges[edge_id].node_b
            while True:
                nxt = links(cur_node, cur)
                if nxt is None or nxt in visited:
                    break
                visited.add(nxt)
                if direction == 0:
                    chain.insert(0, nxt)
                else:
                    chain.append(nxt)
                e = edges[nxt]
                cur_node = e.node_b if e.node_a == cur_node else e.node_a
                cur = nxt
        runs.append(_run_from_chain([edges[e] for e in chain], nodes, page))

    return tuple(canonical_sort(runs, key=lambda r: r.canonical_key()))


def _run_from_chain(chain: Sequence[GraphEdge], nodes, page: int) -> PipeRun:
    points: list[Pt] = []
    for i, e in enumerate(chain):
        poly = list(e.polyline)
        if i == 0:
            if len(chain) > 1:
                nxt = chain[1]
                shared = {nxt.node_a, nxt.node_b} & {e.node_a, e.node_b}
                shared_pt = nodes[sorted(shared)[0]].point if shared else None
                if shared_pt is not None and dist(poly[0], shared_pt) < dist(poly[-1], shared_pt):
                    poly.reverse()
            points.extend(poly)
        else:
            if dist(poly[-1], points[-1]) < dist(poly[0], points[-1]):
                poly.reverse()
            points.extend(poly[1:] if dist(poly[0], points[-1]) < 1e-9 else poly)

    # Canonical orientation: a run and its reverse are the same physical pipe.
    fwd = tuple(points)
    rev = tuple(reversed(points))
    centerline = fwd if fwd <= rev else rev

    widths = [e.width_pt for e in chain if e.width_pt is not None]
    width = sum(widths) / len(widths) if widths else None
    styles = sorted({e.style for e in chain})
    style = styles[0] if len(styles) == 1 else "mixed"

    direction = _classify_direction(centerline)
    reasons: list[Reason] = []
    state = IdentityState.HIGH_CONFIDENCE
    if width is None:
        reasons.append(Reason.INSUFFICIENT_GEOMETRY)
        state = IdentityState.INSUFFICIENT
    rid = entity_id("run", (page, tuple((round(x, 4), round(y, 4)) for x, y in centerline)))
    return PipeRun(
        pipe_run_id=rid,
        page=page,
        centerline=centerline,
        edge_ids=tuple(sorted(e.edge_id for e in chain)),
        source_object_ids=tuple(sorted({o for e in chain for o in ()})),
        width_pt=width,
        style=style,
        direction=direction,
        designation_candidates=(),
        dimension_candidates=(),
        vertical_transition_ids=(),
        confidence=Confidence(
            geometry=qs(0.9 if width is not None else 0.45),
            topology=qs(min(0.99, 0.7 + 0.05 * len(chain))),
        ),
        state=state,
        reasons=tuple(reasons),
        provenance=Provenance(
            stage="topology",
            rule="mutual-best-continuation chaining",
            inputs=tuple(sorted(e.edge_id for e in chain)),
            notes=(f"edges={len(chain)}", f"lengthPt={ql(polyline_length(centerline))}"),
        ),
    )


def _classify_direction(points: Sequence[Pt]) -> str:
    if len(points) < 2:
        return "diagonal"
    kinds: set[str] = set()
    for i in range(len(points) - 1):
        seg = Segment(points[i], points[i + 1])
        if seg.length <= 0:
            continue
        a = angle_diff(seg.angle, 0.0)
        if a <= DIRECTION_TOLERANCE_RAD:
            kinds.add("horizontal")
        elif abs(a - math.pi / 2) <= DIRECTION_TOLERANCE_RAD:
            kinds.add("vertical_on_sheet")
        else:
            kinds.add("diagonal")
    if len(kinds) == 1:
        return kinds.pop()
    return "mixed"
