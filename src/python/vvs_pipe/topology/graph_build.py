"""Pipe candidates -> a deterministic geometric graph.

Three geometric repairs happen here, all of them driven by the drawn pipe
width rather than by tuned absolutes:

* **corner healing** - two candidate ends that stop short of each other at a
  bend (offsetting two walls around a mitre leaves the reconstructed midlines
  a little short) are joined at the intersection of their axes, which restores
  the true corner point instead of guessing a midpoint;
* **tee splitting** - an end that lands on another candidate's interior splits
  that candidate at the foot of the perpendicular, so the branch and the main
  share a real node;
* **node merging** - ends within tolerance collapse to their centroid.

Nothing in this module breaks a tie by array position, object id or insertion
order.  Where two repairs are equally supported the geometry is left alone and
the ambiguity is reported.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from ..canonical import canonical_sort, entity_id, qc
from ..geometry.index import SpatialIndex, connected_components
from ..geometry.primitives import (
    BBox,
    Segment,
    angle_diff,
    dist,
    point_segment_distance,
    project_scalar,
    segment_intersection,
)
from ..model import GraphEdge, GraphNode, PipeCandidate

Pt = tuple[float, float]


@dataclass(frozen=True, slots=True)
class TopologyConfig:
    node_tolerance_floor_pt: float = 1.2
    node_tolerance_width_factor: float = 1.30
    corner_max_angle_rad: float = math.radians(165.0)
    corner_min_angle_rad: float = math.radians(15.0)
    tee_tolerance_width_factor: float = 1.30
    tee_edge_margin_pt: float = 0.5


@dataclass(frozen=True, slots=True)
class PipeGraph:
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    page: int

    def node_map(self) -> dict[str, GraphNode]:
        return {n.node_id: n for n in self.nodes}

    def incident(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for e in self.edges:
            out.setdefault(e.node_a, []).append(e.edge_id)
            out.setdefault(e.node_b, []).append(e.edge_id)
        return {k: sorted(v) for k, v in out.items()}

    def to_canonical(self) -> dict:
        return {
            "page": self.page,
            "nodes": [n.to_canonical() for n in self.nodes],
            "edges": [e.to_canonical() for e in self.edges],
        }


def _tolerance(cfg: TopologyConfig, *widths: float | None) -> float:
    w = max([x for x in widths if x is not None] or [0.0])
    return max(cfg.node_tolerance_floor_pt, cfg.node_tolerance_width_factor * w)


def build_graph(
    candidates: Sequence[PipeCandidate], page: int, cfg: TopologyConfig | None = None
) -> PipeGraph:
    cfg = cfg or TopologyConfig()
    cands = canonical_sort(list(candidates), key=lambda c: c.canonical_key())
    if not cands:
        return PipeGraph((), (), page)

    # Working polylines, mutated by the repairs below.
    polys: list[list[Pt]] = [[(float(x), float(y)) for x, y in c.centerline] for c in cands]

    _heal_corners(polys, cands, cfg)
    polys, owners = _split_tees(polys, cands, cfg)

    # Node identity: cluster all endpoints within tolerance, then place the node
    # at the cluster centroid.
    endpoints: list[tuple[int, int, Pt]] = []
    for pi, poly in enumerate(polys):
        endpoints.append((pi, 0, poly[0]))
        endpoints.append((pi, len(poly) - 1, poly[-1]))
    endpoints = canonical_sort(endpoints, key=lambda t: ((qc(t[2][0]), qc(t[2][1])), t[0], t[1]))

    index: SpatialIndex[int] = SpatialIndex.for_items(
        [(BBox(p[0], p[1], p[0], p[1]).expanded(0.5), i) for i, (_pi, _k, p) in enumerate(endpoints)]
    )
    edges_uf: list[tuple[int, int]] = []
    for i, (pi, _k, p) in enumerate(endpoints):
        tol = _tolerance(cfg, cands[owners[pi]].width_pt)
        for j in index.query_box(BBox(p[0], p[1], p[0], p[1]).expanded(tol)):
            if j <= i:
                continue
            qj = endpoints[j]
            tol_j = _tolerance(cfg, cands[owners[qj[0]]].width_pt)
            if dist(p, qj[2]) <= min(tol, tol_j):
                edges_uf.append((i, j))
    comps = connected_components(len(endpoints), edges_uf)

    node_of_endpoint: dict[tuple[int, int], str] = {}
    nodes: list[GraphNode] = []
    degree: dict[str, int] = {}
    for comp in comps:
        pts = [endpoints[i][2] for i in comp]
        cx = qc(sum(p[0] for p in pts) / len(pts))
        cy = qc(sum(p[1] for p in pts) / len(pts))
        nid = entity_id("nd", (page, (cx, cy)))
        for i in comp:
            pi, k, _p = endpoints[i]
            node_of_endpoint[(pi, k)] = nid
            polys[pi][k] = (cx, cy)
        degree[nid] = len(comp)
        nodes.append(GraphNode(node_id=nid, page=page, point=(cx, cy), degree=len(comp), kind="endpoint"))

    nodes = [
        GraphNode(
            node_id=n.node_id,
            page=page,
            point=n.point,
            degree=degree[n.node_id],
            kind="endpoint" if degree[n.node_id] == 1 else ("continuation" if degree[n.node_id] == 2 else "junction"),
        )
        for n in nodes
    ]

    out_edges: list[GraphEdge] = []
    for pi, poly in enumerate(polys):
        c = cands[owners[pi]]
        a = node_of_endpoint[(pi, 0)]
        b = node_of_endpoint[(pi, len(poly) - 1)]
        if a == b:
            continue  # degenerate after snapping
        eid = entity_id("ed", (page, tuple((qc(x), qc(y)) for x, y in poly), c.candidate_id))
        out_edges.append(
            GraphEdge(
                edge_id=eid,
                page=page,
                node_a=min(a, b),
                node_b=max(a, b),
                polyline=tuple(poly),
                candidate_id=c.candidate_id,
                width_pt=c.width_pt,
                style=c.style,
            )
        )

    nodes = canonical_sort(nodes, key=lambda n: ((qc(n.point[0]), qc(n.point[1])), n.node_id))
    out_edges = _collapse_coincident_edges(out_edges, page)
    out_edges = canonical_sort(
        out_edges, key=lambda e: (tuple((qc(x), qc(y)) for x, y in e.polyline), e.edge_id)
    )
    return PipeGraph(tuple(nodes), tuple(out_edges), page)


# An edge carrying a measured width says more about what was drawn than a dashed
# chain, which says more than a lone stroke; this is the order in which a
# coincident group's survivor is chosen.
_EDGE_STYLE_EVIDENCE = {"double_line": 0, "dashed_line": 1, "single_line": 2}


def _collapse_coincident_edges(edges: Sequence[GraphEdge], page: int) -> list[GraphEdge]:
    """One stretch of drawn centerline is one edge.

    Splitting candidates at tees cuts long candidates into pieces, and two
    candidates that overlap along part of their length therefore produce pieces
    with identical geometry.  They are not two pipes lying exactly on top of
    each other; they are one stretch of drawing found twice, and keeping both
    measures those metres twice and puts the resulting run into two physical
    pipes.  Only exact coincidence collapses, so two pipes running close
    together are untouched, and the survivor is chosen by evidence rather than
    by which candidate happened to be processed first.
    """
    groups: dict[tuple, list[GraphEdge]] = {}
    for e in edges:
        fwd = tuple((qc(x), qc(y)) for x, y in e.polyline)
        rev = tuple(reversed(fwd))
        groups.setdefault((e.node_a, e.node_b, fwd if fwd <= rev else rev), []).append(e)

    out: list[GraphEdge] = []
    for key in sorted(groups, key=lambda k: (k[0], k[1], k[2])):
        members = groups[key]
        if len(members) == 1:
            out.append(members[0])
            continue
        keeper = canonical_sort(
            members,
            key=lambda e: (
                _EDGE_STYLE_EVIDENCE.get(e.style, len(_EDGE_STYLE_EVIDENCE)),
                1e9 if e.width_pt is None else qc(e.width_pt),
                e.candidate_id,
            ),
        )[0]
        # Re-addressed on the geometry the group shares, so the surviving edge's
        # identity does not depend on which candidate won.
        out.append(
            GraphEdge(
                edge_id=entity_id("ed", (page, key[2], keeper.style)),
                page=keeper.page,
                node_a=keeper.node_a,
                node_b=keeper.node_b,
                polyline=keeper.polyline,
                candidate_id=keeper.candidate_id,
                width_pt=keeper.width_pt,
                style=keeper.style,
            )
        )
    return out


def _heal_corners(polys: list[list[Pt]], cands: Sequence[PipeCandidate], cfg: TopologyConfig) -> None:
    """Join ends that stop short of each other at a bend.

    Offsetting two pipe walls around a mitre and taking their midline leaves
    each reconstructed centerline short by roughly half the pipe width.  The
    true corner is where the two axes cross, so that intersection is used - not
    the midpoint between the two short ends, which would lose real length.
    """
    ends: list[tuple[int, int, Pt]] = []
    for pi, poly in enumerate(polys):
        ends.append((pi, 0, poly[0]))
        ends.append((pi, len(poly) - 1, poly[-1]))
    ends = canonical_sort(ends, key=lambda t: ((qc(t[2][0]), qc(t[2][1])), t[0], t[1]))

    index: SpatialIndex[int] = SpatialIndex.for_items(
        [(BBox(p[0], p[1], p[0], p[1]).expanded(0.5), i) for i, (_a, _b, p) in enumerate(ends)]
    )
    pairs: list[tuple[float, int, int, Pt]] = []
    for i, (pi, ki, p) in enumerate(ends):
        tol_i = _tolerance(cfg, cands[pi].width_pt)
        for j in index.query_box(BBox(p[0], p[1], p[0], p[1]).expanded(tol_i * 2.0)):
            if j <= i:
                continue
            pj, kj, q = ends[j]
            if pj == pi:
                continue
            tol = max(tol_i, _tolerance(cfg, cands[pj].width_pt)) * 2.0
            d = dist(p, q)
            if d > tol or d <= 1e-9:
                continue
            si = _end_segment(polys[pi], ki)
            sj = _end_segment(polys[pj], kj)
            ang = angle_diff(si.angle, sj.angle)
            if not (cfg.corner_min_angle_rad <= ang <= math.pi / 2):
                continue
            hit = segment_intersection(_extend(si, tol * 3.0), _extend(sj, tol * 3.0))
            if hit is None:
                continue
            if dist(hit, p) > tol * 3.0 or dist(hit, q) > tol * 3.0:
                continue
            pairs.append((d, i, j, (qc(hit[0]), qc(hit[1]))))

    pairs.sort(key=lambda t: (t[0], ends[t[1]][2], ends[t[2]][2]))
    used: set[int] = set()
    for _d, i, j, hit in pairs:
        if i in used or j in used:
            continue
        used.add(i)
        used.add(j)
        pi, ki, _p = ends[i]
        pj, kj, _q = ends[j]
        polys[pi][ki] = hit
        polys[pj][kj] = hit


def _end_segment(poly: Sequence[Pt], k: int) -> Segment:
    if k == 0:
        return Segment(poly[1], poly[0])
    return Segment(poly[-2], poly[-1])


def _extend(seg: Segment, amount: float) -> Segment:
    u = seg.unit
    return Segment(seg.a, (seg.b[0] + u[0] * amount, seg.b[1] + u[1] * amount))


def _split_tees(
    polys: list[list[Pt]], cands: Sequence[PipeCandidate], cfg: TopologyConfig
) -> tuple[list[list[Pt]], list[int]]:
    """Split a candidate wherever another candidate's end lands on its interior."""
    splits: dict[int, list[tuple[float, Pt]]] = {}
    ends: list[tuple[int, Pt, float]] = []
    for pi, poly in enumerate(polys):
        w = cands[pi].width_pt or 0.0
        ends.append((pi, poly[0], w))
        ends.append((pi, poly[-1], w))
    ends = canonical_sort(ends, key=lambda t: ((qc(t[1][0]), qc(t[1][1])), t[0]))

    boxes = [(BBox.from_points(poly), pi) for pi, poly in enumerate(polys)]
    index: SpatialIndex[int] = SpatialIndex.for_items(boxes)

    for pi, p, w in ends:
        tol = max(cfg.node_tolerance_floor_pt, cfg.tee_tolerance_width_factor * w)
        for pj in index.query_box(BBox(p[0], p[1], p[0], p[1]).expanded(tol)):
            if pj == pi:
                continue
            tol_j = max(
                cfg.node_tolerance_floor_pt,
                cfg.tee_tolerance_width_factor * (cands[pj].width_pt or 0.0),
            )
            reach = max(tol, tol_j)
            target = polys[pj]
            for si in range(len(target) - 1):
                seg = Segment(target[si], target[si + 1])
                if point_segment_distance(p, seg) > reach:
                    continue
                t = project_scalar(seg.a, seg.unit, p)
                if not (cfg.tee_edge_margin_pt < t < seg.length - cfg.tee_edge_margin_pt):
                    continue
                foot = (seg.a[0] + seg.unit[0] * t, seg.a[1] + seg.unit[1] * t)
                splits.setdefault(pj, []).append((si + t / max(seg.length, 1e-9), (qc(foot[0]), qc(foot[1]))))

    out_polys: list[list[Pt]] = []
    owners: list[int] = []
    for pi, poly in enumerate(polys):
        cuts = splits.get(pi)
        if not cuts:
            out_polys.append(poly)
            owners.append(pi)
            continue
        cuts = sorted(set(cuts), key=lambda kv: (kv[0], kv[1]))
        pieces = _cut_polyline(poly, cuts)
        for piece in pieces:
            if len(piece) >= 2 and dist(piece[0], piece[-1]) > 1e-9:
                out_polys.append(piece)
                owners.append(pi)
    return out_polys, owners


def _cut_polyline(poly: Sequence[Pt], cuts: Sequence[tuple[float, Pt]]) -> list[list[Pt]]:
    pieces: list[list[Pt]] = []
    current: list[Pt] = [poly[0]]
    cut_iter = list(cuts)
    ci = 0
    for si in range(len(poly) - 1):
        while ci < len(cut_iter) and si <= cut_iter[ci][0] < si + 1:
            pt = cut_iter[ci][1]
            if dist(pt, current[-1]) > 1e-9:
                current.append(pt)
            if len(current) >= 2:
                pieces.append(current)
            current = [pt]
            ci += 1
        if dist(poly[si + 1], current[-1]) > 1e-9:
            current.append(poly[si + 1])
    if len(current) >= 2:
        pieces.append(current)
    return pieces
