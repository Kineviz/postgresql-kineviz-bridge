"""Response models for the GraphXR/Kineviz **Database Proxy** wire contract.

These are adapted from Kineviz's open-source `graphxr-database-proxy`
(https://github.com/Kineviz/graphxr-database-proxy, MIT License) — specifically
`src/graphxr_database_proxy/models/project.py`. We borrow the subset the
Database Proxy *connector* actually needs (the query / schema / graphSchema /
sampleData response shapes), so the bridge can present itself to Kineviz as a
Database Proxy instead of a KoreDB proxy. Trimmed of the framework-only pieces
(projects store, auth/OAuth config) that a single-backend bridge doesn't use.

Presenting as a Database Proxy lets us use clean, stable **string** node ids
(`ELEMENT_ID`), instead of Kuzu's `"<table>:<offset>"` integers — see
`element_id.py`.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


class DatabaseType(str, Enum):
    """Database types the proxy contract recognizes (path segment `/api/<type>/...`)."""
    SPANNER = "spanner"
    ROCKETGRAPH = "rocketgraph"
    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    MONGODB = "mongodb"


class QueryRequest(BaseModel):
    """POST /api/{type}/{project}/query body."""
    query: str = Field(..., description="Query string (Cypher)")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="Query parameters")


class Node(BaseModel):
    """A graph node. `id` is a stable, opaque string (our base64 Element_ID)."""
    id: str = Field(..., description="Unique node identifier (opaque string)")
    labels: List[str] = Field(..., description="Node labels/categories")
    properties: Dict[str, Any] = Field(..., description="Node properties")


class RelationshipData(BaseModel):
    """A graph relationship. `id`/`startNodeId`/`endNodeId` are opaque strings."""
    id: str = Field(..., description="Unique relationship identifier (opaque string)")
    type: str = Field(..., description="Relationship type/name")
    startNodeId: str = Field(..., description="Source node id")
    endNodeId: str = Field(..., description="Target node id")
    properties: Dict[str, Any] = Field(default_factory=dict, description="Relationship properties")


class GraphData(BaseModel):
    """`QueryData.data` when `type == "GRAPH"`."""
    nodes: List[Node] = Field(default_factory=list)
    relationships: List[RelationshipData] = Field(default_factory=list)


class QueryData(BaseModel):
    """Container for a query result: GRAPH (nodes/relationships) or TABLE (rows)."""
    type: Literal["TABLE", "GRAPH"] = Field(..., description="Result type indicator")
    data: Union[GraphData, List[List[Any]], List[Dict[str, Any]], None] = Field(
        None, description="GraphData for graph queries; 2D array (row 0 = headers) for tables"
    )
    # Kineviz reads `numRows` for expand paging (nextSkip/hasMore); rel count for
    # GRAPH, data-row count for TABLE.
    numRows: int = 0
    summary: Dict[str, str] = Field(default_factory=lambda: {"version": "4.0.1"})


class QueryResponse(BaseModel):
    """POST /api/{type}/{project}/query response."""
    success: bool
    data: Optional[QueryData] = None
    error: Optional[str] = None
    execution_time: Optional[float] = None


class SchemaResponse(BaseModel):
    """GET /api/{type}/{project}/schema — table_name -> column_name -> type."""
    success: bool
    data: Optional[Dict[str, Dict[str, str]]] = None
    error: Optional[str] = None


class Category(BaseModel):
    """A node label definition in the graph schema."""
    name: str
    props: Optional[List[str]] = None
    keys: Optional[List[str]] = None
    keysTypes: Optional[Dict[str, str]] = None
    propsTypes: Optional[Dict[str, str]] = None


class Relationship(BaseModel):
    """A relationship type definition in the graph schema."""
    name: str
    props: Optional[List[str]] = None
    keys: Optional[List[str]] = None
    keysTypes: Optional[Dict[str, str]] = None
    propsTypes: Optional[Dict[str, str]] = None
    startCategory: str
    endCategory: str


class GraphSchema(BaseModel):
    categories: List[Category] = Field(default_factory=list)
    relationships: List[Relationship] = Field(default_factory=list)


class GraphSchemaResponse(BaseModel):
    """GET /api/{type}/{project}/graphSchema."""
    success: bool
    data: GraphSchema = Field(default_factory=lambda: GraphSchema(categories=[], relationships=[]))
    error: Optional[str] = None


class SampleDataResponse(BaseModel):
    """GET /api/{type}/{project}/sampleData."""
    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class APIInfo(BaseModel):
    """GET /api/{type}/{project} — advertises the endpoint URLs for this project."""
    type: DatabaseType
    api_urls: Dict[str, str]
    version: Optional[str] = None
