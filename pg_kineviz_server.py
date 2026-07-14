#!/usr/bin/env python3
"""PostgreSQL 19 SQL/PGQ → Kineviz proxy server.

Speaks the same Cypher-over-HTTP contract as kuzu-graphxr-server /
lancegraph-graphxr-server so Kineviz connects unchanged.

    python pg_kineviz_server.py --backend mock --port 7001
    python pg_kineviz_server.py --backend pg --dsn "postgresql://user@host/db" \
        --graph business_network --port 7001

Kineviz connection URL:  http://localhost:7001/postgres-graph/<name>
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from pg_kineviz_proxy import envelope
from pg_kineviz_proxy.identity import IdentityRegistry
from pg_kineviz_proxy.logging_setup import setup_logging
from pg_kineviz_proxy.query_log import make_entry, write as write_query_log
from pg_kineviz_proxy.query_processor import QueryProcessor

logger = None  # set in main()


def build_backend(args):
    registry = IdentityRegistry()
    if args.backend == "mock":
        from pg_kineviz_proxy.mock_backend import MockBackend
        return MockBackend(registry)
    # pg backend: connect and auto-discover the graph schema from the PostgreSQL
    # Information Schema property-graph views (works for any graph).
    from pg_kineviz_proxy.pg_backend import Pg19Backend
    if not args.dsn:
        raise SystemExit("--dsn is required for --backend pg")
    return Pg19Backend(args.graph, registry, dsn=args.dsn).connect()


def create_app(backend, default_db_name: str = "postgres-graph") -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("startup")
        yield
        logger.info("shutdown")

    app = FastAPI(title="PostgreSQL 19 → Kineviz Bridge", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
    )

    @app.get("/health")
    async def health():
        return {
            "status": "healthy",
            "backend": type(backend).__name__,
            "node_labels": backend.labels(),
            "rel_types": backend.rel_types(),
            "timestamp": datetime.now().isoformat(),
        }

    @app.post("/postgres-graph/{name}")
    async def kineviz_query(name: str, request: Request):
        start = time.time()
        rid = str(int(start * 1000))
        query = None
        params = None
        proc = None
        try:
            body = await request.json()
            query = body.get("query") or body.get("cypher") or body.get("sql") or body.get("gql") or body.get("command")
            params = body.get("params") or {}
            if not query:
                return envelope.error("query parameter is required.")

            print("\n" + "=" * 80 + "\n[req %s] db=%s query: %s\n" % (rid, name, query) + "=" * 80 + "\n")
            proc = QueryProcessor(backend, db_name=name or default_db_name)

            ok, msg = proc.validate(query)
            if not ok:
                _log(rid, name, query, params, start, proc, error=msg)
                return envelope.error(msg)

            outcome = proc.execute(query)
            _log(rid, name, query, params, start, proc, outcome=outcome)
            logger.info("[req %s] type=%s elapsed=%.3fs", rid, outcome.type, time.time() - start)
            return envelope.success(outcome)
        except Exception as e:
            logger.error("[req %s] failed: %s\n%s", rid, e, traceback.format_exc())
            _log(rid, name, query or "", params, start, proc, error=str(e))
            return envelope.error(str(e))

    return app


def _log(rid, name, query, params, start, proc, outcome=None, error=None):
    entry = make_entry(
        request_id=rid, db_name=name, query=query, params=params,
        elapsed_ms=int((time.time() - start) * 1000),
        outcome_type=outcome.type if outcome else None,
        data=outcome.data if outcome else None,
        sql=getattr(proc, "last_sql", None) if proc else None,
        error=error,
    )
    write_query_log(entry)


def parse_args():
    p = argparse.ArgumentParser(description="PostgreSQL 19 → Kineviz proxy server")
    p.add_argument("--backend", choices=["mock", "pg"], default="mock")
    p.add_argument("--dsn", help="PostgreSQL DSN (pg backend)")
    p.add_argument("--graph", default="business_network", help="property graph name (pg backend)")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "2899")), help="use 7001 for Kineviz")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--ssl-cert")
    p.add_argument("--ssl-key")
    p.add_argument("--ssl-password")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


def main():
    global logger
    args = parse_args()
    logger = setup_logging(debug=args.debug)

    def handler(signum, frame):
        logger.info("signal %s, shutting down", signum)
        sys.exit(0)
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    backend = build_backend(args)
    app = create_app(backend)

    use_ssl = bool(args.ssl_cert and args.ssl_key)
    proto = "https" if use_ssl else "http"
    print("Starting PostgreSQL→Kineviz bridge on %s://%s:%d  (backend=%s)" % (proto, args.host, args.port, args.backend))
    print("Kineviz URL:  %s://localhost:%d/postgres-graph/%s" % (proto, args.port, args.graph))
    print("  labels=%s  rels=%s" % (backend.labels(), backend.rel_types()))

    cfg: dict = {"app": app, "host": args.host, "port": args.port, "reload": False,
                 "log_level": "debug" if args.debug else "info"}
    if use_ssl:
        cfg["ssl_certfile"] = args.ssl_cert
        cfg["ssl_keyfile"] = args.ssl_key
        if args.ssl_password:
            cfg["ssl_keyfile_password"] = args.ssl_password
    uvicorn.run(**cfg)


if __name__ == "__main__":
    main()
