"""Twenty-seventh requirement: the evidence graph.

Every derived thing records what it was derived from, as an edge in one graph.
That makes two questions answerable at any time:

    why(P0042)          - what made this pipe, and what named it
    why(S3-98 -> P0042) - which glyphs, which leader, which segments

and it makes the negative answers possible too: what competed, what was
rejected, and for what stated reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from .canonical import canonical_json, entity_id, q, sort_canonical


@dataclass(frozen=True)
class EvidenceEdge:
    edge_id: str
    source_id: str
    target_id: str
    relation: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"edgeId": self.edge_id, "from": self.source_id, "to": self.target_id,
                "relation": self.relation, "detail": self.detail}


class EvidenceGraph:
    """Provenance for everything the engine says."""

    def __init__(self) -> None:
        self.edges: dict[str, EvidenceEdge] = {}
        self.kinds: dict[str, str] = {}
        self.labels: dict[str, str] = {}
        self.rejections: list[dict] = []

    def declare(self, entity_id_value: str, kind: str, label: str = "") -> None:
        self.kinds[entity_id_value] = kind
        if label:
            self.labels[entity_id_value] = label

    def link(self, source_id: str, target_id: str, relation: str,
             detail: Optional[dict] = None) -> None:
        edge_id = entity_id("ev", {"s": source_id, "t": target_id, "r": relation})
        self.edges[edge_id] = EvidenceEdge(edge_id, source_id, target_id, relation, detail or {})

    def reject(self, entity_id_value: str, competitor_id: str, reason: str,
               detail: Optional[dict] = None) -> None:
        self.rejections.append({
            "entityId": entity_id_value,
            "competitorId": competitor_id,
            "reason": reason,
            "detail": detail or {},
        })

    # -- traversal --------------------------------------------------------
    def incoming(self, target_id: str) -> list[EvidenceEdge]:
        return sort_canonical([e for e in self.edges.values() if e.target_id == target_id],
                              key=lambda e: (e.relation, e.source_id))

    def outgoing(self, source_id: str) -> list[EvidenceEdge]:
        return sort_canonical([e for e in self.edges.values() if e.source_id == source_id],
                              key=lambda e: (e.relation, e.target_id))

    def why(self, entity_id_value: str, depth: int = 6) -> dict:
        """Walk back from an answer to the ink it came from."""
        seen: set[str] = set()

        def expand(node_id: str, level: int) -> dict:
            payload: dict[str, Any] = {
                "id": node_id,
                "kind": self.kinds.get(node_id, "unknown"),
            }
            if node_id in self.labels:
                payload["label"] = self.labels[node_id]
            if level <= 0 or node_id in seen:
                return payload
            seen.add(node_id)
            supports = []
            for edge in self.incoming(node_id):
                supports.append({
                    "relation": edge.relation,
                    "detail": edge.detail,
                    "from": expand(edge.source_id, level - 1),
                })
            if supports:
                payload["builtFrom"] = supports
            competing = [r for r in self.rejections if r["entityId"] == node_id]
            if competing:
                payload["rejected"] = sort_canonical(
                    competing, key=lambda r: (r["competitorId"], r["reason"]))
            return payload

        return expand(entity_id_value, depth)

    def chain(self, entity_id_value: str) -> list[str]:
        """The flat lineage, ink first - the shape the specification asks for."""
        order: list[str] = []
        stack = [entity_id_value]
        seen: set[str] = set()
        while stack:
            node_id = stack.pop()
            if node_id in seen:
                continue
            seen.add(node_id)
            order.append(node_id)
            for edge in self.incoming(node_id):
                stack.append(edge.source_id)
        return list(reversed(order))

    def to_json(self, include_edges: bool = False) -> dict:
        by_kind: dict[str, int] = {}
        for kind in self.kinds.values():
            by_kind[kind] = by_kind.get(kind, 0) + 1
        by_relation: dict[str, int] = {}
        for edge in self.edges.values():
            by_relation[edge.relation] = by_relation.get(edge.relation, 0) + 1
        payload = {
            "entities": len(self.kinds),
            "edges": len(self.edges),
            "byKind": {k: by_kind[k] for k in sorted(by_kind)},
            "byRelation": {k: by_relation[k] for k in sorted(by_relation)},
            "rejections": len(self.rejections),
        }
        if include_edges:
            payload["allEdges"] = [self.edges[k].to_json() for k in sorted(self.edges)]
            payload["allRejections"] = sort_canonical(
                self.rejections, key=lambda r: (r["entityId"], r["competitorId"], r["reason"]))
        return payload
