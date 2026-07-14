#!/usr/bin/env python3
"""Stress-probe the Cypher → SQL/PGQ mapping against a running bridge.

Fires a broad battery of queries (working shapes, edge cases, and known/suspected
limitations) at the server and classifies each response, so we can see exactly
what works, what fails cleanly, and what fails wrong.

    python3 scripts/probe_queries.py [http://localhost:7072/postgres-graph/paysim]
"""

import json
import sys
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:7072/postgres-graph/paysim"

# (category, query, expectation)  — expectation is a human note, not asserted.
QUERIES = [
    ("basic",   "MATCH (n:Client) RETURN n LIMIT 3", "graph"),
    ("basic",   "MATCH (n:Client) RETURN count(n)", "count"),
    ("basic",   "MATCH (n:Client) RETURN n.name LIMIT 3", "table names"),
    ("where-cmp",  "MATCH (t:Transaction) WHERE t.amount > 1000000 RETURN t.id, t.amount ORDER BY t.amount DESC LIMIT 3", "table"),
    ("where-bool", "MATCH (n:Client) WHERE n.isfraud RETURN count(*)", "fraud count"),
    ("where-not-bool", "MATCH (n:Client) WHERE NOT n.isfraud RETURN count(*)", "non-fraud count"),
    ("where-or",   "MATCH (n:Client) WHERE n.name CONTAINS 'john' OR n.name CONTAINS 'jane' RETURN count(*)", "or"),
    ("where-and-or", "MATCH (n:Client) WHERE (n.name STARTS WITH 'A' OR n.name STARTS WITH 'B') AND n.isfraud RETURN count(*)", "nested"),
    ("where-in-str", "MATCH (n:Client) WHERE n.name IN ['Aaron Grimes','Kayla Ramos'] RETURN n.name", "in list"),
    ("where-isnull", "MATCH (t:Transaction) WHERE t.action IS NOT NULL RETURN count(*)", "is not null"),
    ("where-regex",  "MATCH (n:Client) WHERE n.name =~ '.*Grimes.*' RETURN n.name", "regex"),
    ("where-eq-strid", "MATCH (n:Client) WHERE n.id = '4054294970398398' RETURN n.name", "exact id"),
    ("agg-nogroup",  "MATCH (a:Client)-[:PERFORMS]->(t:Transaction) WHERE t.amount > 5000000 RETURN count(*)", "agg no group-key"),
    ("agg-where",    "MATCH (n:Client)-[:PERFORMS]->(t:Transaction) WHERE t.isfraud RETURN n.name AS n, count(t) AS c ORDER BY c DESC LIMIT 3", "agg+where+order"),
    ("agg-distinct", "MATCH (t:Transaction)-[:TO_MERCHANT]->(m:Merchant) RETURN m.name AS m, count(DISTINCT t.id) AS c ORDER BY c DESC LIMIT 3", "count distinct"),
    ("agg-alt",      "MATCH (t:Transaction)-[:TO_MERCHANT|TO_BANK]->(x) RETURN count(*)", "alternation agg"),
    ("multi-hop",    "MATCH (a:Client)-[:PERFORMS]->(t:Transaction)-[:TO_MERCHANT]->(m:Merchant) WHERE m.highrisk RETURN a.name, m.name LIMIT 3", "3-hop + bool"),
    ("graph-order",  "MATCH (n:Client) RETURN n ORDER BY n.name LIMIT 3", "graph ORDER BY"),
    ("distinct-scalar", "MATCH (n:Transaction) RETURN DISTINCT n.action", "distinct values"),
    ("shared-pii",   "MATCH (c1:Client)-[:HAS_PHONE]-(p:PhoneNumber)-[:HAS_PHONE]-(c2:Client) WHERE c1.name < c2.name RETURN c1.name, c2.name LIMIT 3", "shared phone"),
    # --- suspected limitations / edge cases ---
    ("mixed-return", "MATCH (n:Client) RETURN n, n.name LIMIT 3", "mixed node+scalar?"),
    ("comma-pattern","MATCH (a:Client), (m:Merchant) RETURN a.name, m.name LIMIT 3", "disjoint/cartesian?"),
    ("comma-shared", "MATCH (n:Client)-[:HAS_EMAIL]->(e:Email), (n)-[:HAS_PHONE]->(p:PhoneNumber) RETURN n.name, e.id, p.id LIMIT 3", "multi-pattern shared var?"),
    ("lowercase-label", "MATCH (n:client) RETURN n LIMIT 3", "wrong label case?"),
    ("having",       "MATCH (n:Client)-[:PERFORMS]->(t:Transaction) RETURN n.name AS n, count(t) AS c HAVING c > 100", "HAVING?"),
    ("order-agg-expr","MATCH (n:Client)-[:PERFORMS]->(t:Transaction) RETURN n.name, count(t) ORDER BY count(t) DESC LIMIT 3", "ORDER BY agg expr (no alias)?"),
    ("var-length",   "MATCH (a:Client)-[:PERFORMS*1..3]->(b) RETURN * LIMIT 3", "var-length (blocked)"),
    ("optional",     "MATCH (n:Client) OPTIONAL MATCH (n)-[:HAS_EMAIL]->(e:Email) RETURN n.name, e.id LIMIT 3", "OPTIONAL MATCH?"),
    ("with-pipe",    "MATCH (n:Client)-[:PERFORMS]->(t:Transaction) WITH n, count(t) AS c WHERE c > 100 RETURN n.name, c LIMIT 3", "WITH pipeline?"),
    ("unwind",       "UNWIND [1,2,3] AS x RETURN x", "UNWIND?"),
    ("func-substring","MATCH (n:Client) RETURN substring(n.name, 0, 3) LIMIT 3", "function?"),
    ("arithmetic",   "MATCH (t:Transaction) WHERE t.amount * 2 > 1000000 RETURN t.id LIMIT 3", "arithmetic in WHERE?"),
]


def classify(env):
    if not isinstance(env, dict):
        return "BADRESP", str(env)[:80]
    if env.get("status") != 0:
        return "STATUS1", (env.get("message") or "")[:90]
    data = env.get("data")
    if isinstance(data, dict) and "data" in data:
        inner, typ = data["data"], data.get("type")
        if typ == "GRAPH":
            n, r = len(inner.get("nodes", [])), len(inner.get("relationships", []))
            return "GRAPH", "nodes=%d rels=%d" % (n, r)
        if typ == "TABLE":
            if inner and inner[0] == ["error"]:
                return "ERRTBL", (inner[1][0] if len(inner) > 1 else "")[:90]
            return "TABLE", "cols=%s rows=%d" % (inner[0] if inner else [], max(0, len(inner) - 1))
    if isinstance(data, dict):
        return "SCHEMA", list(data.keys())
    return "OTHER", str(data)[:80]


def run(query):
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return classify(json.load(resp))
    except Exception as e:
        return "HTTPERR", str(e)[:80]


def main():
    print("Probing %s\n" % URL)
    width = max(len(c) for c, _, _ in QUERIES)
    for cat, q, note in QUERIES:
        kind, detail = run(q)
        print("[%-9s] %-6s %-28s | %s" % (cat, kind, detail, note))


if __name__ == "__main__":
    main()
