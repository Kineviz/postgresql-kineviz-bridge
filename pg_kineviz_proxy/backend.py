"""Shared types and the Backend contract.

The bridge is deliberately split so the same Cypher front-end can drive either
an in-memory mock (runnable with no database) or a real PostgreSQL 19 SQL/PGQ
graph. Everything a query touches flows through these dataclasses.

No third-party imports here on purpose: the whole core pipeline (translator,
registry, converter, processor) must import cleanly under plain CPython so it
can be exercised without FastAPI / psycopg installed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ----- schema descriptors -----

@dataclass
class NodeSchema:
    label: str
    primary_key: str                       # property name that identifies the vertex
    properties: Dict[str, str]             # property name -> Kineviz/Kuzu type string


@dataclass
class RelSchema:
    type: str
    src_label: str
    dst_label: str
    properties: Dict[str, str] = field(default_factory=dict)


# ----- query intermediate representation (produced by cypher_translator) -----

@dataclass
class VertexPat:
    var: str
    label: Optional[str] = None
    # id(var) IN [internal_id(t,o), ...] references, decoded by the backend:
    id_refs: Optional[List[Tuple[int, int]]] = None
    id_negated: bool = False


@dataclass
class EdgePat:
    var: str
    types: Optional[List[str]]             # None = any edge type
    direction: str                         # "out" | "in" | "both"
    src_var: str
    dst_var: str


@dataclass
class MatchPlan:
    vertices: List[VertexPat]
    edges: List[EdgePat]
    where: Any = None                          # predicate expression tree (see predicate.py)
    order: List[Tuple[str, str]] = field(default_factory=list)   # [(term, 'ASC'|'DESC')]
    limit: Optional[int] = None
    skip: int = 0                              # Kineviz paginates with SKIP n LIMIT m
    return_items: Optional[List[str]] = None   # bare vars / "*"; informational

    def vertex(self, var: str) -> Optional[VertexPat]:
        for v in self.vertices:
            if v.var == var:
                return v
        return None


# ----- results -----

@dataclass
class GraphNode:
    id: str
    labels: List[str]
    properties: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "labels": self.labels, "properties": self.properties}


@dataclass
class GraphRel:
    id: str
    startNodeId: str
    endNodeId: str
    type: str
    properties: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "startNodeId": self.startNodeId,
            "endNodeId": self.endNodeId,
            "type": self.type,
            "properties": self.properties,
        }


@dataclass
class GraphResult:
    nodes: List[GraphNode] = field(default_factory=list)
    relationships: List[GraphRel] = field(default_factory=list)

    def to_payload(self) -> Dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "relationships": [r.to_dict() for r in self.relationships],
        }


# The outcome a QueryProcessor hands back to the HTTP / simulator layer.
@dataclass
class QueryOutcome:
    type: str                              # "GRAPH" | "TABLE" | "SCHEMA"
    data: Any
    summary: Dict[str, Any] = field(default_factory=dict)


class Backend(ABC):
    """A source of graph data the bridge can query.

    Implementations: MockBackend (in-memory) and Pg19Backend (SQL/PGQ).
    """

    node_schemas: Dict[str, NodeSchema]
    rel_schemas: Dict[str, RelSchema]

    def labels(self) -> List[str]:
        return list(self.node_schemas.keys())

    def rel_types(self) -> List[str]:
        return list(self.rel_schemas.keys())

    @abstractmethod
    def schema_response(self, db_name: str) -> Dict[str, Any]:
        """Kineviz-shaped schema: {<db>: {categories, relationships}}."""

    @abstractmethod
    def node_count(self, label: Optional[str] = None) -> int:
        """Count all vertices, or vertices of `label` when given."""

    @abstractmethod
    def rel_count(self, rel_type: Optional[str]) -> int: ...

    @abstractmethod
    def sample(self, limit: int) -> GraphResult:
        """Round-robin sample across vertex labels (untyped MATCH (n) RETURN n)."""

    @abstractmethod
    def execute(self, plan: MatchPlan) -> GraphResult:
        """Run a translated MATCH plan and return graph elements."""

    @abstractmethod
    def project(self, plan: MatchPlan, projections: List[Tuple[str, str, str]], distinct: bool = False):
        """Scalar RETURN (e.g. `RETURN a.name, b.id`) → (header, rows).

        projections: list of (var, prop, alias). Returns a header list and a list
        of row value-lists — the caller wraps them into a TABLE.
        """

    @abstractmethod
    def aggregate(self, plan: MatchPlan, group_keys, aggs):
        """Grouped aggregation → (header, rows).

        group_keys: [(var, prop, alias)] — the implicit GROUP BY columns.
        aggs: [(fn, var, prop_or_None, alias)] where fn in count/sum/avg/min/max
        (prop is None for `count(*)` / `count(var)`).
        """


def build_schema_response(
    db_name: str,
    node_schemas: Dict[str, NodeSchema],
    rel_schemas: Dict[str, RelSchema],
) -> Dict[str, Any]:
    """The Kineviz schema shape, shared by every backend (see doc §3A.6)."""
    categories: Dict[str, Any] = {}
    for label, s in node_schemas.items():
        categories[label] = {
            "name": label,
            "props": list(s.properties.keys()),
            "propsTypes": dict(s.properties),
            "keys": [s.primary_key],
            "keysTypes": {s.primary_key: s.properties.get(s.primary_key, "STRING")},
        }
    relationships: Dict[str, Any] = {}
    for rtype, s in rel_schemas.items():
        relationships[rtype] = {
            "name": rtype,
            "props": list(s.properties.keys()),
            "propsTypes": dict(s.properties),
            "keys": [],
            "keysTypes": {},
            "startCategory": s.src_label,
            "endCategory": s.dst_label,
        }
    return {db_name: {"categories": categories, "relationships": relationships}}
