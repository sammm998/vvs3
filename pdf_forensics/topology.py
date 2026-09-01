"""Topology: nodes, edges, runs.

Pipe candidates are pieces of centerline.  A building's piping is a graph, and
the questions that matter - does this continue into that, where does it branch,
what is one physical pipe - are graph questions.

The chaining rule is *mutual best continuation*: at a node, two edges are joined
only when each is the other's straightest continuation.  That is symmetric, so
it cannot depend on which edge was considered first, and it refuses to guess at
a tee where three ways continue equally.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from .canonical import canonical_json, entity_id, q, sort_canonical, undirected
from .fragment_search import polyline_length
from .geometry_search import angle_difference
from .model import PipeCandidate, PipeRun
from .spatial_index import SpatialIndex

Point = tuple[float, float]

# Endpoints closer than this are the same place.  It is a drawing tolerance,
# expressed in points, not a fudge factor on results.
NODE_TOLERANCE = 1.6


def _direction_at(candidates: Sequence[PipeCandidate], endpoint_index: int) -> Optional[Point]:
    """The direction a centerline leaves the endpoint numbered ``endpoint_index``."""
    usable = [c for c in candidates if len(c.centerline) >= 2]
    candidate = usable[endpoint_index // 2]
    line = candidate.centerline
    a, b = (line[0], line[1]) if endpoint_index % 2 == 0 else (line[-1], line[-2])
    dx, dy = b[0] - a[0], b[1] - a[1]
    norm = math.hypot(dx, dy)
    if norm < 1e-9:
        return None
    return (dx / norm, dy / norm)


def _meeting_point(points: Sequence[Point], directions: Sequence[Optional[Point]]) -> Point:
    """Where the lines that end here would actually meet.

    At an elbow the true corner is the intersection of the two centerlines, not
    the midpoint between where they stop.  Using the intersection restores the
    length the wall overlap left out instead of quietly losing it.
    """
    if len(points) == 2 and all(d is not None for d in directions):
        (x1, y1), (x2, y2) = points
        (dx1, dy1), (dx2, dy2) = directions
        denominator = dx1 * dy2 - dy1 * dx2
        if abs(denominator) > 1e-6:
            t = ((x2 - x1) * dy2 - (y2 - y1) * dx2) / denominator
            meeting = (x1 + t * dx1, y1 + t * dy1)
            if all(math.dist(meeting, p) <= 4.0 + math.dist(points[0], points[1])
                   for p in points):
                return (q(meeting[0]), q(meeting[1]))
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (q(sum(xs) / len(xs)), q(sum(ys) / len(ys)))


@dataclass(frozen=True)
class Node:
    node_id: str
    page: int
    point: Point
    edge_ids: tuple[str, ...]

    @property
    def degree(self) -> int:
        return len(self.edge_ids)

    def to_json(self) -> dict:
        return {"nodeId": self.node_id, "page": self.page, "point": list(self.point),
                "edgeIds": list(self.edge_ids), "degree": self.degree}


@dataclass(frozen=True)
class Edge:
    edge_id: str
    page: int
    candidate_id: str
    a_node: str
    b_node: str
    centerline: tuple[Point, ...]
    length: float
    separation: Optional[float]

    def to_json(self) -> dict:
        return {"edgeId": self.edge_id, "page": self.page, "candidateId": self.candidate_id,
                "nodes": [self.a_node, self.b_node],
                "centerline": [list(p) for p in self.centerline],
                "length": self.length, "separation": self.separation}


class Graph:
    def __init__(self, candidates: Sequence[PipeCandidate]) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: dict[str, Edge] = {}
        self._build(candidates)

    def _build(self, candidates: Sequence[PipeCandidate]) -> None:
        points: list[tuple[int, Point]] = []
        tolerances: list[float] = []
        for candidate in candidates:
            if len(candidate.centerline) < 2:
                continue
            # Two centerlines that meet at an elbow stop short of the corner by
            # about half the pipe's own bore, because each was measured where
            # its two walls overlap.  The tolerance therefore scales with the
            # bore rather than being a fixed slop.
            # ... but never more than would swallow the candidate itself, or a
            # short piece would have both of its ends snapped to one node and
            # vanish.
            tolerance = min(max(NODE_TOLERANCE, 0.9 * (candidate.wall_separation or 0.0)),
                            0.45 * max(candidate.length, 1e-6))
            points.append((candidate.page, candidate.centerline[0]))
            points.append((candidate.page, candidate.centerline[-1]))
            tolerances.append(tolerance)
            tolerances.append(tolerance)
        # snap coincident endpoints to shared nodes, deterministically
        index = SpatialIndex([(f"{i}", page, (p[0], p[1], p[0], p[1]))
                              for i, (page, p) in enumerate(points)])
        assignment: dict[int, Point] = {}
        canonical_points: dict[tuple[int, Point], list[int]] = {}
        for i, (page, point) in enumerate(points):
            if i in assignment:
                continue
            members = [int(k) for k in index.near_point(page, point, tolerances[i])
                       if abs(tolerances[int(k)] - tolerances[i]) <= 1.0
                       or math.dist(points[int(k)][1], point) <= NODE_TOLERANCE]
            cluster = sorted(m for m in members if m not in assignment)
            if not cluster:
                cluster = [i]
            centre = _meeting_point([points[m][1] for m in cluster],
                                    [_direction_at(candidates, m) for m in cluster])
            for m in cluster:
                assignment[m] = centre
            canonical_points.setdefault((page, centre), []).extend(cluster)
        node_ids: dict[tuple[int, Point], str] = {}
        self._node_points: dict[str, Point] = {}
        for (page, centre) in sorted(canonical_points):
            node_id = entity_id("node", {"p": page, "x": centre[0], "y": centre[1]})
            node_ids[(page, centre)] = node_id
            self._node_points[node_id] = centre
        edges: dict[str, Edge] = {}
        node_edges: dict[str, list[str]] = {}
        cursor = 0
        for candidate in candidates:
            if len(candidate.centerline) < 2:
                continue
            a_point = assignment[cursor]
            b_point = assignment[cursor + 1]
            cursor += 2
            a_node = node_ids[(candidate.page, a_point)]
            b_node = node_ids[(candidate.page, b_point)]
            if a_node == b_node:
                continue                     # a candidate may not collapse to a point
            edge_id = entity_id("edge", {"c": candidate.candidate_id, "a": a_node, "b": b_node})
            centerline = _with_endpoints(candidate.centerline,
                                         self._node_points[a_node], self._node_points[b_node])
            edges[edge_id] = Edge(
                edge_id=edge_id,
                page=candidate.page,
                candidate_id=candidate.candidate_id,
                a_node=a_node,
                b_node=b_node,
                centerline=centerline,
                length=polyline_length(centerline),
                separation=candidate.wall_separation,
            )
            node_edges.setdefault(a_node, []).append(edge_id)
            if b_node != a_node:
                node_edges.setdefault(b_node, []).append(edge_id)
        for (page, centre), node_id in sorted(node_ids.items(), key=lambda kv: kv[1]):
            self.nodes[node_id] = Node(node_id=node_id, page=page, point=centre,
                                       edge_ids=tuple(sorted(set(node_edges.get(node_id, [])))))
        self.edges = {k: edges[k] for k in sorted(edges)}

    # -- queries ----------------------------------------------------------

    def other_node(self, edge: Edge, node_id: str) -> str:
        return edge.b_node if edge.a_node == node_id else edge.a_node

    def heading(self, edge: Edge, node_id: str) -> float:
        """The angle at which an edge leaves a node."""
        line = edge.centerline
        if self.nodes[node_id].point == line[0] or edge.a_node == node_id:
            a, b = line[0], line[1]
        else:
            a, b = line[-1], line[-2]
        return q(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0)

    def junctions(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.degree >= 3]

    def to_json(self) -> dict:
        degrees: dict[str, int] = {}
        for node in self.nodes.values():
            key = str(node.degree)
            degrees[key] = degrees.get(key, 0) + 1
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "junctions": len(self.junctions()),
            "degreeHistogram": {k: degrees[k] for k in sorted(degrees, key=int)},
        }


def _with_endpoints(centerline: Sequence[Point], start: Point, end: Point) -> tuple[Point, ...]:
    points = list(centerline)
    points[0] = start
    points[-1] = end
    return tuple(points)


def build_runs(graph: Graph, candidates: Sequence[PipeCandidate],
               angle_tolerance: float = 20.0,
               separation_tolerance: float = 0.6) -> list[PipeRun]:
    """Chain edges into runs by mutual best continuation."""
    by_candidate = {c.candidate_id: c for c in candidates}
    partner: dict[tuple[str, str], Optional[str]] = {}
    for node in sorted(graph.nodes.values(), key=lambda n: n.node_id):
        incident = sorted(node.edge_ids)
        for edge_id in incident:
            edge = graph.edges[edge_id]
            best: Optional[tuple[float, str]] = None
            second: Optional[float] = None
            for other_id in incident:
                if other_id == edge_id:
                    continue
                other = graph.edges[other_id]
                if edge.separation is not None and other.separation is not None:
                    if abs(edge.separation - other.separation) > separation_tolerance:
                        continue
                elif (edge.separation is None) != (other.separation is None):
                    continue
                deviation = angle_difference(graph.heading(edge, node.node_id),
                                             graph.heading(other, node.node_id))
                straightness = abs(90.0 - deviation)   # 90 deg apart == straight through
                if straightness > angle_tolerance:
                    continue
                score = -straightness
                if best is None or (score, other_id) < (best[0], best[1]):
                    second = best[0] if best else None
                    best = (score, other_id)
                elif second is None or score < second:
                    second = score
            if best is not None and second is not None and abs(best[0] - second) < 1e-6:
                partner[(node.node_id, edge_id)] = None      # a tie is not a continuation
            else:
                partner[(node.node_id, edge_id)] = best[1] if best else None
    # union edges whose continuation is mutual
    parent = {edge_id: edge_id for edge_id in graph.edges}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rb < ra:
            ra, rb = rb, ra
        parent[rb] = ra

    for (node_id, edge_id), other_id in sorted(partner.items()):
        if other_id is None:
            continue
        if partner.get((node_id, other_id)) == edge_id:
            union(edge_id, other_id)
    groups: dict[str, list[str]] = {}
    for edge_id in sorted(parent):
        groups.setdefault(find(edge_id), []).append(edge_id)
    runs: list[PipeRun] = []
    for root in sorted(groups):
        members = sorted(groups[root])
        edges = [graph.edges[e] for e in members]
        centerline = _walk(edges, graph)
        separations = [e.separation for e in edges if e.separation is not None]
        payload = {"p": edges[0].page, "g": [list(p) for p in centerline],
                   "s": q(sum(separations) / len(separations)) if separations else None,
                   "e": members}
        node_ids = sorted({e.a_node for e in edges} | {e.b_node for e in edges})
        runs.append(
            PipeRun(
                run_id=entity_id("run", payload),
                page=edges[0].page,
                centerline=centerline,
                member_ids=tuple(sorted(by_candidate[e.candidate_id].candidate_id for e in edges)),
                length=q(sum(e.length for e in edges)),
                wall_separation=q(sum(separations) / len(separations)) if separations else None,
                node_ids=tuple(node_ids),
            )
        )
    return sort_canonical(runs, key=lambda r: (r.page, r.centerline, r.run_id))


def _walk(edges: Sequence[Edge], graph: Graph) -> tuple[Point, ...]:
    """Order a run's points by following it, starting at an end."""
    if len(edges) == 1:
        return edges[0].centerline
    adjacency: dict[str, list[Edge]] = {}
    for edge in edges:
        adjacency.setdefault(edge.a_node, []).append(edge)
        adjacency.setdefault(edge.b_node, []).append(edge)
    ends = sorted(n for n, incident in adjacency.items() if len(incident) == 1)
    start = ends[0] if ends else sorted(adjacency)[0]
    used: set[str] = set()
    points: list[Point] = []
    current = start
    while True:
        nxt = None
        for edge in sorted(adjacency.get(current, []), key=lambda e: e.edge_id):
            if edge.edge_id in used:
                continue
            used.add(edge.edge_id)
            line = list(edge.centerline)
            if graph.nodes[current].point != line[0] and edge.b_node == current:
                line.reverse()
            if points and points[-1] == line[0]:
                points.extend(line[1:])
            else:
                points.extend(line)
            nxt = graph.other_node(edge, current)
            break
        if nxt is None:
            break
        current = nxt
    for edge in edges:                      # nothing may be dropped silently
        if edge.edge_id not in used:
            points.extend(edge.centerline)
    return tuple(points)


def to_json(graph: Graph, runs: Sequence[PipeRun]) -> dict:
    payload = graph.to_json()
    payload.update({
        "pipeRuns": len(runs),
        "totalRunLength": q(sum(r.length for r in runs)),
        "runsWithSeparation": len([r for r in runs if r.wall_separation is not None]),
    })
    return payload


# ---------------------------------------------------------------------------
# physical pipes - identity from geometry, never from a label
# ---------------------------------------------------------------------------

def build_physical_pipes(graph: Graph, runs: Sequence["PipeRun"],
                         separation_tolerance: float = 0.6) -> list["PhysicalPipe"]:
    """Group runs into the pipes a fitter would install.

    Two runs are the same pipe when they meet at a node *and* have the same
    bore.  Neither condition mentions text: a pipe exists whether or not the
    sheet names it, and the designation is attached afterwards.  This is the
    correction of the inversion that made a pipe's identity depend on the text
    stage succeeding.
    """
    from .model import PhysicalPipe, Reason, State

    node_runs: dict[str, list[str]] = {}
    by_run = {r.run_id: r for r in runs}
    for run in runs:
        for node_id in run.node_ids:
            node_runs.setdefault(node_id, []).append(run.run_id)
    parent = {r.run_id: r.run_id for r in runs}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rb < ra:
            ra, rb = rb, ra
        parent[rb] = ra

    for node_id in sorted(node_runs):
        members = sorted(set(node_runs[node_id]))
        for i, a_id in enumerate(members):
            for b_id in members[i + 1:]:
                a, b = by_run[a_id], by_run[b_id]
                if a.wall_separation is None or b.wall_separation is None:
                    continue
                if abs(a.wall_separation - b.wall_separation) <= separation_tolerance:
                    union(a_id, b_id)
    groups: dict[str, list[str]] = {}
    for run_id in sorted(parent):
        groups.setdefault(find(run_id), []).append(run_id)
    pipes: list[PhysicalPipe] = []
    for root in sorted(groups):
        members = sorted(groups[root])
        member_runs = [by_run[m] for m in members]
        centerline = tuple(p for run in member_runs for p in run.centerline)
        separations = [r.wall_separation for r in member_runs if r.wall_separation is not None]
        payload = {"p": member_runs[0].page, "r": members,
                   "g": [list(p) for p in centerline]}
        pipes.append(
            PhysicalPipe(
                pipe_id=entity_id("pipe", payload),
                page=member_runs[0].page,
                run_ids=tuple(members),
                centerline=centerline,
                parts=tuple(run.centerline for run in member_runs),
                designation=None,
                designation_state=State.UNRESOLVED,
                designation_reasons=(Reason.NO_DESIGNATION,),
                diameter_mm=None,
                diameter_state=State.UNRESOLVED,
                diameter_reasons=(Reason.NO_DIMENSION_EVIDENCE,),
                horizontal_points=q(sum(r.length for r in member_runs)),
                vertical_metres=None,
                vertical_state=State.UNRESOLVED,
                measurement={
                    "wallSeparationPoints": q(sum(separations) / len(separations)) if separations else None,
                    "runCount": len(members),
                },
                confidence={"geometry": q(min(1.0, 0.5 + 0.1 * len(members)))},
            )
        )
    return sort_canonical(pipes, key=lambda p: (p.page, p.centerline, p.pipe_id))


def reconcile(candidates: Sequence[PipeCandidate], runs: Sequence[PipeRun],
              pipes: Sequence["PhysicalPipe"]) -> dict:
    """A metre must be counted once.  This is a gate, not a note.

    Three ways the same length can be counted twice: two candidates with one
    centerline, one run inside two pipes, one candidate inside two runs.  Each
    is checked here and the analysis is INVALID if any of them holds.
    """
    shared_centerlines: list[str] = []
    seen: dict[str, str] = {}
    for candidate in candidates:
        key = canonical_json({"p": candidate.page, "g": [list(x) for x in candidate.centerline]})
        if key in seen:
            shared_centerlines.append(candidate.candidate_id)
        else:
            seen[key] = candidate.candidate_id
    run_owner: dict[str, list[str]] = {}
    for pipe in pipes:
        for run_id in pipe.run_ids:
            run_owner.setdefault(run_id, []).append(pipe.pipe_id)
    runs_in_two_pipes = sorted(k for k, v in run_owner.items() if len(v) > 1)
    candidate_owner: dict[str, list[str]] = {}
    for run in runs:
        for member in run.member_ids:
            candidate_owner.setdefault(member, []).append(run.run_id)
    candidates_in_two_runs = sorted(k for k, v in candidate_owner.items() if len(v) > 1)
    ok = not (shared_centerlines or runs_in_two_pipes or candidates_in_two_runs)
    return {
        "ok": ok,
        "sharedCenterlines": shared_centerlines,
        "runsInTwoPipes": runs_in_two_pipes,
        "candidatesInTwoRuns": candidates_in_two_runs,
        "candidateLength": q(sum(c.length for c in candidates)),
        "runLength": q(sum(r.length for r in runs)),
        "pipeLength": q(sum(p.horizontal_points for p in pipes)),
    }
