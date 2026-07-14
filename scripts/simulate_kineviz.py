#!/usr/bin/env python3
"""Simulate the queries Kineviz sends and show exactly what comes back.

Runs the whole bridge pipeline in-process against the MockBackend (no database,
no server, stdlib only) and prints the Kineviz-shaped envelope for each query.
For pattern queries it also prints the SQL/PGQ the PostgreSQL backend WOULD
generate (requirement 1), and it demonstrates the expand round-trip
(requirement 2) by feeding node ids from one response back in as internal_id().

    python3 scripts/simulate_kineviz.py            # summaries
    python3 scripts/simulate_kineviz.py --full     # full JSON envelopes
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pg_kineviz_proxy import envelope
from pg_kineviz_proxy.cypher_translator import translate
from pg_kineviz_proxy.identity import IdentityRegistry
from pg_kineviz_proxy.mock_backend import MockBackend
from pg_kineviz_proxy.pg_backend import Pg19Backend
from pg_kineviz_proxy.query_processor import QueryProcessor

BAR = "=" * 78


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="print full JSON envelopes")
    args = ap.parse_args()

    registry = IdentityRegistry()
    backend = MockBackend(registry)
    # Share the SAME registry so the SQL demo decodes ids the mock minted.
    pg = Pg19Backend("business_network", registry, backend.node_schemas, backend.rel_schemas)
    proc = QueryProcessor(backend, db_name="business_network")

    def show(query, note=""):
        print("\n" + BAR)
        print("Kineviz ->  " + query.strip())
        if note:
            print("            (" + note + ")")
        ok, msg = proc.validate(query)
        if not ok:
            print("<- REJECTED: " + msg)
            return None
        outcome = proc.execute(query)
        env = envelope.success(outcome)

        # Show the SQL/PGQ the PostgreSQL backend would generate for graph patterns.
        # Skip intercepted shapes (schema/count/sample/probe) — those never hit
        # SQL — and untyped-edge expands, which the pg backend branches per rel
        # type (not yet implemented in this skeleton).
        try:
            plan = translate(proc._auto_limit(query.strip().rstrip(";")))
            sql = pg.compile(plan).sql
            if "IS UNKNOWN" not in sql and "COLUMNS (\n\n" not in sql:
                print("\n--- generated SQL/PGQ (PostgreSQL 19) ---\n" + sql)
            elif outcome.type == "GRAPH" and plan.edges and any(e.types is None for e in plan.edges):
                print("\n--- (PostgreSQL backend branches this untyped [r] expand per rel type — Phase 4) ---")
        except Exception:
            pass

        print("\n<- return  type=%s" % outcome.type)
        if args.full:
            print(json.dumps(env, indent=2, default=str))
        else:
            _summarize(env)
        return env

    print(BAR + "\n Kineviz CONNECT / SCHEMA\n" + BAR)
    show('RETURN "api" AS a', "liveness probe fired on connect")
    show("CALL schema()", "schema browser")
    show("SHOW TABLES")
    show("MATCH (n) RETURN count(n)", "node count panel")
    show("MATCH ()-[r]->() RETURN count(r)", "edge count panel")

    print("\n" + BAR + "\n INITIAL LOAD / SEARCH\n" + BAR)
    show("MATCH (n) RETURN n LIMIT 100", "untyped sample")
    show("MATCH (n:Person) WHERE toLower(n.name) CONTAINS toLower('AL') RETURN n", "substring search")

    print("\n" + BAR + "\n ONE-HOP PATTERNS\n" + BAR)
    load_env = show("MATCH (a:Person)-[r:WORKS_AT]->(b:Company) RETURN * LIMIT 1000")
    show("MATCH (a:Person)-[r:KNOWS]->(b:Person) RETURN a, r, b LIMIT 1000")
    show("MATCH (a:Company)<-[r:WORKS_AT]-(b:Person) RETURN *", "reverse direction")
    show("MATCH (a:Person)-[r:KNOWS]-(b:Person) RETURN *", "undirected")

    print("\n" + BAR + "\n EXPAND FROM SELECTED NODES (the id() round-trip)\n" + BAR)
    # Grab two Person node ids from the load response, exactly as Kineviz would
    # have stored them, and feed them back as internal_id(table, offset).
    person_ids = _pick_person_ids(load_env, backend, n=2)
    refs = ", ".join("internal_id(%s, %s)" % tuple(i.split(":")) for i in person_ids)
    print("(selected Person node ids from previous response: %s)" % person_ids)
    show("MATCH (n)-[r]->(m) WHERE id(n) IN [%s] RETURN n, r, m LIMIT 500" % refs,
         "expand outbound from selection")
    show("MATCH (n)-[r]-(m) WHERE id(n) IN [%s] RETURN *" % refs,
         "expand either-direction from selection")

    print("\n" + BAR + "\nDone. Full request/response log would be written to logs/queries.jsonl by the server.\n" + BAR)


def _summarize(env):
    data = env.get("data")
    if env.get("status") != 0:
        print("   status=1 message=%s" % env.get("message"))
        return
    if isinstance(data, dict) and "categories" not in str(list(data.keys())):
        inner = data.get("data")
        if isinstance(inner, dict) and "nodes" in inner:
            nodes, rels = inner["nodes"], inner["relationships"]
            print("   nodes=%d rels=%d" % (len(nodes), len(rels)))
            if nodes:
                print("   e.g. node " + json.dumps(nodes[0], default=str))
            if rels:
                print("   e.g. rel  " + json.dumps(rels[0], default=str))
            return
        if isinstance(inner, list):
            print("   table rows=%d  header=%s" % (max(0, len(inner) - 1), inner[0] if inner else []))
            if len(inner) > 1:
                print("   e.g. row " + json.dumps(inner[1], default=str))
            return
    # schema (single-wrapped)
    print("   schema keys=" + json.dumps(list(data.keys())))
    for db, s in data.items():
        print("   %s: categories=%s relationships=%s"
              % (db, list(s["categories"].keys()), list(s["relationships"].keys())))


def _pick_person_ids(load_env, backend, n=2):
    ids = []
    if load_env and load_env.get("status") == 0:
        inner = load_env["data"]["data"]
        for node in inner.get("nodes", []):
            if node["labels"] == ["Person"] and node["id"] not in ids:
                ids.append(node["id"])
            if len(ids) >= n:
                break
    if not ids:  # fallback: mint directly
        ids = [backend.registry.node_id("Person", (1,)), backend.registry.node_id("Person", (2,))]
    return ids[:n]


if __name__ == "__main__":
    main()
