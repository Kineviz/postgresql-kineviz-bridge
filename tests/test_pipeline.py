"""Skeleton pipeline tests — run with `python3 -m pytest` or `python3 tests/test_pipeline.py`.

No database required: everything runs against the MockBackend. Also asserts the
PostgreSQL backend compiles the same plans to well-formed GRAPH_TABLE SQL.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pg_kineviz_proxy import envelope
from pg_kineviz_proxy.cypher_translator import translate
from pg_kineviz_proxy.identity import IdentityRegistry
from pg_kineviz_proxy.mock_backend import MockBackend
from pg_kineviz_proxy.pg_backend import Pg19Backend
from pg_kineviz_proxy.query_processor import QueryProcessor


def _proc():
    reg = IdentityRegistry()
    be = MockBackend(reg)
    return QueryProcessor(be, "business_network"), be


def test_schema_is_single_wrapped():
    proc, _ = _proc()
    env = envelope.success(proc.execute("CALL schema()"))
    assert "categories" in env["data"]["business_network"]      # not double-wrapped
    assert env["status"] == 0


def test_graph_is_double_wrapped():
    proc, _ = _proc()
    env = envelope.success(proc.execute("MATCH (a:Person)-[r:WORKS_AT]->(b:Company) RETURN *"))
    assert env["data"]["type"] == "GRAPH"
    assert env["data"]["data"]["nodes"] and env["data"]["data"]["relationships"]
    assert env["data"]["summary"]["version"] == "0.11.2"


def test_counts():
    proc, _ = _proc()
    assert proc.execute("MATCH (n) RETURN count(n)").data == [["count(n)"], [6]]
    assert proc.execute("MATCH ()-[r]->() RETURN count(r)").data == [["count(r)"], [7]]


def test_probe_and_writes():
    proc, _ = _proc()
    assert proc.execute('RETURN "api" AS a').data == [["a"], ["api"]]
    ok, _ = proc.validate("CREATE (n:Person)")
    assert not ok


def test_expand_roundtrip():
    proc, be = _proc()
    # Load, capture a Person id, expand from it.
    proc.execute("MATCH (a:Person)-[r:WORKS_AT]->(b:Company) RETURN *")
    pid = be.registry.node_id("Person", (1,))          # "0:0"
    t, o = pid.split(":")
    env = envelope.success(proc.execute(
        "MATCH (n)-[r]-(m) WHERE id(n) IN [internal_id(%s, %s)] RETURN *" % (t, o)))
    nodes = env["data"]["data"]["nodes"]
    rels = env["data"]["data"]["relationships"]
    assert rels, "expand should return neighbors"
    assert any(n["id"] == pid for n in nodes)


def test_expand_scoped_to_selection_paren_wrapped_list():
    """Regression: Kineviz sends `id(n) IN ([internal_id(...)])` (paren-wrapped).

    Expanding ONE company via WORKS_AT must return only that company + its
    employees — not every WORKS_AT edge / the other company.
    """
    proc, be = _proc()
    # Load WORKS_AT so company offsets are minted (Acme=1:0, Globex=1:1).
    proc.execute("MATCH (a:Person)-[r:WORKS_AT]->(b:Company) RETURN *")
    acme = be.registry.node_id("Company", (100,))
    t, o = acme.split(":")
    out = proc.execute(
        "MATCH (n)-[r:WORKS_AT]-(m) WHERE id(n) IN ([internal_id(%s, %s)]) RETURN n,r,m LIMIT 1000" % (t, o))
    rels = out.data["relationships"]
    assert len(rels) == 2, "Acme has exactly 2 WORKS_AT edges, got %d" % len(rels)
    company_ids = {n["id"] for n in out.data["nodes"] if n["labels"] == ["Company"]}
    assert company_ids == {acme}, "expand must not pull in other companies: %s" % company_ids


def test_skip_pagination_terminates():
    """Regression: Kineviz probes SKIP <count> LIMIT 1 to ask 'is there more?'.

    SKIP past the last row must return empty so Kineviz stops paginating.
    """
    proc, _ = _proc()
    q = "MATCH (a:Person)-[r:WORKS_AT]->(b:Company) RETURN *"          # exactly 4 edges
    assert len(proc.execute(q + " SKIP 0 LIMIT 1000").data["relationships"]) == 4
    assert len(proc.execute(q + " SKIP 2 LIMIT 1").data["relationships"]) == 1
    assert len(proc.execute(q + " SKIP 4 LIMIT 1").data["relationships"]) == 0
    assert len(proc.execute(q + " SKIP 99 LIMIT 1").data["relationships"]) == 0


def test_pg_compiles_sql_and_decodes_ids():
    reg = IdentityRegistry()
    mock = MockBackend(reg)
    pg = Pg19Backend("business_network", reg, mock.node_schemas, mock.rel_schemas)
    # Mint an id so the pg WHERE-decode has something to resolve.
    pid = reg.node_id("Person", (1,))
    t, o = pid.split(":")
    plan = translate(
        "MATCH (n:Person)-[r:WORKS_AT]->(m:Company) WHERE id(n) IN [internal_id(%s, %s)] RETURN *" % (t, o))
    c = pg.compile(plan)
    assert "GRAPH_TABLE" in c.sql
    assert '(v0 IS "Person")-[e0 IS "WORKS_AT"]->(v1 IS "Company")' in c.sql
    assert "v0.id IN (%s)" in c.sql       # psycopg pyformat
    assert c.params == [1]
    assert c.manifest["vertices"][0]["alias"] == "Person"


def test_count_with_alias_intercepted():
    """Regression: Kineviz sends `RETURN count(n) AS count` — must be intercepted
    (not translated into invalid `(v0 IS UNKNOWN) COLUMNS ()` SQL)."""
    proc, _ = _proc()
    o = proc.execute("MATCH (n) RETURN count(n) AS count")
    assert o.type == "TABLE" and o.data == [["count"], [6]]
    o = proc.execute("MATCH ()-[r]->() RETURN count(r) AS count")
    assert o.type == "TABLE" and o.data == [["count"], [7]]


def test_pg_reverse_edge_endpoint_mapping():
    """Regression: reverse edge (Company)<-[WORKS_AT]-(Person) must map the rel's
    schema-source endpoint to the Person var, so endpoint ids match node ids."""
    reg = IdentityRegistry()
    mock = MockBackend(reg)
    pg = Pg19Backend("business_network", reg, mock.node_schemas, mock.rel_schemas)
    plan = translate("MATCH (c:Company)<-[e:WORKS_AT]-(p:Person) RETURN *")
    edge = pg.compile(plan).manifest["edges"][0]
    assert edge["start_var"] == "p" and edge["end_var"] == "c"
    assert edge["src_alias"] == "Person" and edge["dst_alias"] == "Company"


def test_pg_undirected_expand_labels_var_from_id():
    """Regression: undirected expand whose selected node is on the DEST side must
    resolve that var's label from the internal_id decode (Company), not assume src."""
    reg = IdentityRegistry()
    mock = MockBackend(reg)
    pg = Pg19Backend("business_network", reg, mock.node_schemas, mock.rel_schemas)
    acme = reg.node_id("Company", (100,))
    t, o = acme.split(":")
    plan = translate(
        "MATCH (n)-[r:WORKS_AT]-(m) WHERE id(n) IN ([internal_id(%s, %s)]) RETURN *" % (t, o))
    c = pg.compile(plan)
    assert '(v0 IS "Company")' in c.sql       # n resolved to Company (from the id), not Person
    assert '(v1 IS "Person")' in c.sql        # m resolved to the complement
    assert c.params == [100]                    # filter on the company key, not a person key


def test_untyped_edge_branches_over_rel_types():
    """Regression: `MATCH (n)-[r]->(m) RETURN *` (untyped edge) must fan out into
    one typed query per rel type and merge — not return empty."""
    proc, _ = _proc()
    o = proc.execute("MATCH (n)-[r]->(m) RETURN * LIMIT 100")
    assert o.type == "GRAPH"
    types = {r["type"] for r in o.data["relationships"]}
    assert types == {"KNOWS", "WORKS_AT"}
    assert len(o.data["relationships"]) == 7      # 3 KNOWS + 4 WORKS_AT


def test_pg_cross_variable_filter():
    """Regression: `WHERE a.name <> b.name` (comparison between two bindings)
    must appear in the generated SQL — previously it was silently dropped."""
    reg = IdentityRegistry()
    mock = MockBackend(reg)
    pg = Pg19Backend("business_network", reg, mock.node_schemas, mock.rel_schemas)
    plan = translate(
        "MATCH (a:Person)-[r1:KNOWS]-(p:Person)-[r2:KNOWS]-(b:Person) WHERE a.name<>b.name RETURN *")
    c = pg.compile(plan)
    assert "v0.name <> v2.name" in c.sql       # a=v0, b=v2; cross predicate emitted


def test_typed_node_count():
    """Regression: `MATCH (c1:Client) RETURN count(c1)` must return the COUNT,
    not the Client nodes (the typed count wasn't being intercepted)."""
    proc, _ = _proc()
    o = proc.execute("MATCH (c1:Person) RETURN count(c1)")     # mock: 4 Person
    assert o.type == "TABLE" and o.data == [["count(c1)"], [4]]
    assert proc.execute("MATCH (m:Company) RETURN count(m)").data == [["count(m)"], [2]]
    # backtick label + count(*) + AS alias, exactly as Kineviz emits
    o2 = proc.execute("MATCH (c1:`Person`) RETURN count(*) AS count")
    assert o2.type == "TABLE" and o2.data == [["count"], [4]]
    # untyped still works
    assert proc.execute("MATCH (n) RETURN count(n)").data == [["count(n)"], [6]]


def test_scalar_return_is_table():
    """Regression: `MATCH (c1:Client) RETURN c1.name` must return a TABLE of the
    property values, not a graph of nodes."""
    proc, _ = _proc()
    o = proc.execute("MATCH (c1:Person) RETURN c1.name")
    assert o.type == "TABLE"
    assert o.data[0] == ["c1.name"]                       # header
    assert {row[0] for row in o.data[1:]} == {"Alice", "Bob", "Carol", "Dave"}
    # multiple props + AS alias
    o2 = proc.execute("MATCH (c1:Person) RETURN c1.name AS n, c1.email AS e")
    assert o2.type == "TABLE" and o2.data[0] == ["n", "e"]
    # DISTINCT
    o3 = proc.execute("MATCH (c1:Company) RETURN DISTINCT c1.name")
    assert o3.type == "TABLE" and {r[0] for r in o3.data[1:]} == {"Acme", "Globex"}
    # a bare var still returns a GRAPH, not a table
    assert proc.execute("MATCH (c1:Person) RETURN c1").type == "GRAPH"


def test_projection_error_is_surfaced():
    """A scalar projection that errors (e.g. unknown property) with no rows must
    surface the error, not return a misleading empty table."""
    reg = IdentityRegistry()

    class Boom(MockBackend):
        def project(self, plan, projections, distinct=False):
            raise RuntimeError('property "is_fraud" does not exist')

    proc = QueryProcessor(Boom(reg), "business_network")
    try:
        proc.execute("MATCH (c1:Person) RETURN c1.is_fraud")
        assert False, "expected the projection error to propagate"
    except RuntimeError as e:
        assert "does not exist" in str(e)


def test_grouped_aggregation():
    """`MATCH (p:Person)-[w:WORKS_AT]->(c:Company) RETURN c.name, count(p), sum(...)`
    groups by the non-aggregate key and returns a TABLE."""
    proc, _ = _proc()
    o = proc.execute(
        "MATCH (p:Person)-[w:WORKS_AT]->(c:Company) "
        "RETURN c.name AS company, count(p) AS employees")
    assert o.type == "TABLE"
    assert o.data[0] == ["company", "employees"]
    got = {row[0]: row[1] for row in o.data[1:]}
    # fixture: Acme has Alice+Bob (2), Globex has Carol+Dave (2)
    assert got == {"Acme": 2, "Globex": 2}


def test_aggregation_untyped_edge_resolves_type():
    """Untyped edge `[w]` in an aggregation resolves to the compatible rel type
    from the endpoint labels (Person->Company => WORKS_AT)."""
    proc, _ = _proc()
    o = proc.execute("MATCH (p:Person)-[w]->(c:Company) RETURN c.name AS co, count(*) AS n")
    got = {row[0]: row[1] for row in o.data[1:]}
    assert got == {"Acme": 2, "Globex": 2}


def test_where_operators():
    """WHERE parsing: outer parens, CONTAINS('x') call form, ENDS WITH, IN list,
    IS NULL, OR/AND/NOT — parsed into a predicate tree and compiled to SQL."""
    from pg_kineviz_proxy import predicate as P
    from pg_kineviz_proxy.cypher_translator import translate

    def where_sql(q):
        plan = translate(q)
        params = []
        ctx = P.SqlCtx({v.var: "v0" for v in plan.vertices}, {v.var: "Person" for v in plan.vertices},
                       {"Person": "id"}, None, params)
        return P.to_sql(plan.where, ctx), params

    # Kineviz's wrapped substring form must not be dropped
    s, p = where_sql("MATCH (n:Person) WHERE (toLower(n.name) CONTAINS toLower('AL')) RETURN n")
    assert s == "v0.name::text ILIKE %s" and p == ["%al%"]
    # paren-call form CONTAINS('john')
    s, p = where_sql("MATCH (n:Person) WHERE n.name CONTAINS('Ali') RETURN n")
    assert p == ["%Ali%"]
    # OR + AND + NOT + parens
    s, _ = where_sql("MATCH (n:Person) WHERE n.a = 1 AND (n.b = 2 OR NOT n.c = 3) RETURN n")
    assert s == "(v0.a = %s AND (v0.b = %s OR NOT (v0.c = %s)))"
    # ENDS WITH, IN list, IS NULL, regex
    assert where_sql("MATCH (n:Person) WHERE n.name ENDS WITH 'ice' RETURN n")[0] == "v0.name::text ILIKE %s"
    assert where_sql("MATCH (n:Person) WHERE n.id IN [1, 2, 3] RETURN n")[1] == [1, 2, 3]
    assert where_sql("MATCH (n:Person) WHERE n.email IS NULL RETURN n")[0] == "v0.email IS NULL"
    assert where_sql("MATCH (n:Person) WHERE n.name =~ '.*x.*' RETURN n")[0] == "v0.name::text ~ %s"


def test_in_unnest():
    """Kineviz emits `WHERE x IN UNNEST([...])` — equivalent to `IN [...]`."""
    proc, _ = _proc()
    o = proc.execute("MATCH (n:Person) WHERE n.name IN UNNEST(['Alice', 'Carol']) RETURN n.name")
    assert o.type == "TABLE" and {r[0] for r in o.data[1:]} == {"Alice", "Carol"}


def test_where_or_and_not_on_mock():
    """End-to-end boolean logic on the mock: OR, AND, NOT actually filter."""
    proc, _ = _proc()
    # OR
    o = proc.execute("MATCH (n:Person) WHERE n.name = 'Alice' OR n.name = 'Bob' RETURN n.name")
    assert {r[0] for r in o.data[1:]} == {"Alice", "Bob"}
    # AND + NOT + CONTAINS
    o = proc.execute("MATCH (n:Person) WHERE n.name CONTAINS 'a' AND NOT n.name = 'Dave' RETURN n.name")
    assert {r[0] for r in o.data[1:]} == {"Alice", "Carol"}
    # parenthesized OR combined with AND
    o = proc.execute("MATCH (n:Person) WHERE (n.name = 'Alice' OR n.name = 'Carol') AND n.email IS NOT NULL RETURN n.name")
    assert {r[0] for r in o.data[1:]} == {"Alice", "Carol"}


def test_where_contains_filters_on_mock():
    """End-to-end on the mock: CONTAINS actually filters (Alice, Carol match 'a')."""
    proc, _ = _proc()
    o = proc.execute("MATCH (n:Person) WHERE n.name CONTAINS('a') RETURN n.name")
    names = {r[0] for r in o.data[1:]}
    assert names == {"Alice", "Carol", "Dave"}      # case-insensitive substring 'a'
    # IS NULL: only Dave has a null email
    o2 = proc.execute("MATCH (n:Person) WHERE n.email IS NULL RETURN n.name")
    assert {r[0] for r in o2.data[1:]} == {"Dave"}


def test_order_by():
    """ORDER BY on scalar and aggregation tables (ASC/DESC), before LIMIT."""
    proc, _ = _proc()
    # scalar DESC
    o = proc.execute("MATCH (n:Person) RETURN n.name ORDER BY n.name DESC")
    assert [r[0] for r in o.data[1:]] == ["Dave", "Carol", "Bob", "Alice"]
    # scalar ASC + LIMIT (top-2 alphabetically)
    o = proc.execute("MATCH (n:Person) RETURN n.name ORDER BY n.name ASC LIMIT 2")
    assert [r[0] for r in o.data[1:]] == ["Alice", "Bob"]
    # aggregation ORDER BY the aggregate alias
    o = proc.execute("MATCH (p:Person)-[:WORKS_AT]->(c:Company) "
                     "RETURN c.name AS company, count(p) AS employees ORDER BY employees DESC")
    assert o.data[0] == ["company", "employees"]
    assert [row[1] for row in o.data[1:]] == sorted([row[1] for row in o.data[1:]], reverse=True)


def test_order_by_pg_sql():
    """Generated SQL carries ORDER BY on the right output column."""
    reg = IdentityRegistry()
    mock = MockBackend(reg)
    pg = Pg19Backend("business_network", reg, mock.node_schemas, mock.rel_schemas)
    plan = translate("MATCH (a:Person)-[r:WORKS_AT]->(b:Company) RETURN * ORDER BY b.name DESC")
    sql = pg.compile(plan).sql
    assert "ORDER BY __gx_v1_p0 DESC" in sql            # b=v1, name is its first prop col


def test_variable_length_rejected():
    """Variable-length paths are rejected clearly (PG19 has no path quantifier)."""
    proc, _ = _proc()
    o = proc.execute("MATCH (a:Person)-[:KNOWS*1..3]->(b:Person) RETURN *")
    assert o.type == "TABLE"
    assert "variable-length" in o.data[1][0].lower()


def test_count_distinct():
    """count(DISTINCT ...) aggregation."""
    proc, _ = _proc()
    o = proc.execute("MATCH (p:Person)-[:WORKS_AT]->(c:Company) "
                     "RETURN c.name AS co, count(DISTINCT p.id) AS people ORDER BY co")
    assert o.data[0] == ["co", "people"]
    assert {r[0]: r[1] for r in o.data[1:]} == {"Acme": 2, "Globex": 2}


def test_unsupported_fails_loudly():
    """Unsupported constructs must return a clear error, not wrong/empty results."""
    proc, _ = _proc()
    cases = [
        ("MATCH (n:Person) OPTIONAL MATCH (n)-[:KNOWS]->(m) RETURN n", "optional match"),
        ("MATCH (n:Person) WITH n RETURN n", "with"),
        ("MATCH (n:Person)-[:KNOWS]->(m) RETURN n.name, count(m) HAVING count(m) > 1", "having"),
        ("MATCH (a:Person), (b:Company) RETURN a, b", "comma-separated"),
        ("MATCH (n:Person) WHERE n.age * 2 > 5 RETURN n", "where expression"),
        ("MATCH (n:Person) RETURN substring(n.name, 0, 2)", "return item"),
        ("MATCH (a:Person)-[:KNOWS*1..2]->(b) RETURN *", "variable-length"),
        ("UNWIND [1,2,3] AS x RETURN x", "unwind"),
    ]
    for q, needle in cases:
        o = proc.execute(q)
        assert o.type == "TABLE" and o.data[0] == ["error"], "should reject: %s" % q
        assert needle in o.data[1][0].lower(), (q, o.data[1][0])
    # STARTS WITH must NOT be mistaken for the WITH clause
    o = proc.execute("MATCH (n:Person) WHERE n.name STARTS WITH 'A' RETURN n.name")
    assert o.type == "TABLE" and o.data[0] != ["error"]


def test_case_insensitive_label():
    """A label typed in the wrong case resolves to the stored (canonical) case."""
    reg = IdentityRegistry()
    mock = MockBackend(reg)
    pg = Pg19Backend("business_network", reg, mock.node_schemas, mock.rel_schemas)
    assert pg._canon_label("person") == "Person"
    assert pg._canon_label("KNOWS") == "KNOWS"
    assert pg._canon_label("works_at") == "WORKS_AT"
    sql = pg.compile(translate("MATCH (n:person) RETURN *")).sql
    assert '(v0 IS "Person")' in sql


def test_order_by_aggregate_expression():
    """ORDER BY count(b) (aggregate by expression, no alias) is honored."""
    proc, _ = _proc()
    o = proc.execute("MATCH (a:Person)-[:KNOWS]->(b:Person) "
                     "RETURN a.name AS name, count(b) AS c ORDER BY count(b) DESC")
    # Alice KNOWS Bob+Carol (2), Bob KNOWS Carol (1) -> Alice first
    assert [r[0] for r in o.data[1:]] == ["Alice", "Bob"]


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d tests passed." % len(fns))


if __name__ == "__main__":
    _run_all()
