"""Serve the Kineviz **Database Proxy** REST contract over our translation engine.

Adds the `/api/postgresql/{project}/...` endpoints that Kineviz's "Database Proxy"
connector calls (info / query / schema / graphSchema / sampleData / test). The
query path reuses the existing QueryProcessor + Pg19Backend, but:
  * outbound ids are stable `element_id` strings (see result_converter), and
  * inbound expand (`ELEMENT_ID … UNNEST`) is rewritten to the engine's
    `internal_id` form by `element_expand`.

This lets the same simple bridge present as a Database Proxy — giving clean,
stable node ids — as a stepping stone to a full driver in graphxr-database-proxy.
"""

from __future__ import annotations

import re
import time
import traceback

from fastapi import Request

from . import element_expand
from .graph_schema import build_graph_schema
from .proxy_models import (APIInfo, DatabaseType, GraphData, GraphSchemaResponse,
                           Node, QueryData, QueryResponse, RelationshipData,
                           SampleDataResponse, SchemaResponse)
from .query_processor import QueryProcessor

DB = "postgresql"


def add_database_proxy_routes(app, backend, logger=None) -> None:
    prefix = "/api/" + DB + "/{project}"

    @app.get(prefix)
    async def info(project: str):
        p = "/api/%s/%s" % (DB, project)
        return APIInfo(type=DatabaseType.POSTGRESQL, version="0.1.0", api_urls={
            "info": p, "query": p + "/query", "schema": p + "/schema",
            "graphSchema": p + "/graphSchema", "sampleData": p + "/sampleData",
        })

    @app.get(prefix + "/graphSchema", response_model=GraphSchemaResponse)
    async def graph_schema(project: str):
        try:
            gs = build_graph_schema(getattr(backend, "node_schemas", {}),
                                    getattr(backend, "rel_schemas", {}))
            return GraphSchemaResponse(success=True, data=gs)
        except Exception as e:
            return GraphSchemaResponse(success=False, error=str(e))

    @app.get(prefix + "/schema", response_model=SchemaResponse)
    async def schema(project: str):
        try:
            data = {label: dict(ns.properties) for label, ns in getattr(backend, "node_schemas", {}).items()}
            data.update({typ: dict(rs.properties) for typ, rs in getattr(backend, "rel_schemas", {}).items()})
            return SchemaResponse(success=True, data=data)
        except Exception as e:
            return SchemaResponse(success=False, error=str(e))

    @app.get(prefix + "/sampleData", response_model=SampleDataResponse)
    async def sample_data(project: str):
        return SampleDataResponse(success=True, data={})

    @app.post(prefix + "/test")
    async def test(project: str):
        return {"success": True}

    async def _handle_query(project: str, request: Request):
        start = time.time()
        try:
            body = await request.json()
            q = (body.get("query") or body.get("cypher") or body.get("command") or "").strip()
            if not q:
                return QueryResponse(success=False, error="query is required")
            rewritten = element_expand.rewrite(q, backend.registry,
                                               frozenset(getattr(backend, "rel_types", lambda: [])()))
            print("\n[proxy] project=%s\n  in : %s\n  rew: %s\n" % (project, q, rewritten))
            proc = QueryProcessor(backend, db_name=project)
            ok, msg = proc.validate(rewritten)
            if not ok:
                return QueryResponse(success=False, error=msg)
            outcome = proc.execute(rewritten)
            # `paged` = the client asked for a bounded page (explicit LIMIT). If it
            # didn't, we returned the complete set, so report numRows=0 on graphs to
            # tell Kineviz's expand there's nothing more to page (older frontends omit
            # LIMIT/SKIP; without this their hasMore probe loops forever).
            paged = bool(re.search(r"\bLIMIT\b", q, re.IGNORECASE))
            return QueryResponse(success=True, data=_to_query_data(outcome, paged),
                                 execution_time=time.time() - start)
        except Exception as e:
            traceback.print_exc()
            return QueryResponse(success=False, error=str(e))

    # Kineviz posts queries to `.../query`, but its connection check (checkConnection)
    # POSTs a liveness probe (`return "api" as a`) to the BASE url — so accept both.
    app.post(prefix + "/query", response_model=QueryResponse)(_handle_query)
    app.post(prefix, response_model=QueryResponse)(_handle_query)


def _to_query_data(outcome, paged: bool = True) -> QueryData:
    if outcome.type == "GRAPH" and isinstance(outcome.data, dict):
        p = outcome.data
        nodes = [Node(id=n["id"], labels=n["labels"], properties=n["properties"])
                 for n in p.get("nodes", [])]
        rels = [RelationshipData(id=r["id"], type=r["type"], startNodeId=r["startNodeId"],
                                 endNodeId=r["endNodeId"], properties=r.get("properties", {}))
                for r in p.get("relationships", [])]
        return QueryData(type="GRAPH", data=GraphData(nodes=nodes, relationships=rels),
                         numRows=len(rels) if paged else 0)
    if outcome.type == "TABLE" and isinstance(outcome.data, list):
        return QueryData(type="TABLE", data=outcome.data, numRows=max(0, len(outcome.data) - 1))
    # SCHEMA or anything unexpected shouldn't arrive via /query (it has its own
    # endpoint); surface it as a one-cell table rather than crashing.
    return QueryData(type="TABLE", data=[["info"], [str(outcome.data)[:200]]])
