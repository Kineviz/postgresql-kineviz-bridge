# Mapping Cypher → PostgreSQL 19 SQL/PGQ — a systematic approach

Kineviz speaks two graph query languages: **Cypher** (to Neo4j and KoreDB) and **GQL**,
the ISO-standard Graph Query Language (to Spanner Graph and BigQuery). PostgreSQL 19
speaks **SQL/PGQ** (`GRAPH_TABLE`). Because this bridge presents itself as KoreDB, the
queries it receives are **Cypher**, and it translates them into SQL/PGQ. This document
describes that translation systematically, rather than shape-by-shape. It is the
architecture reference for `pg_kineviz_proxy/`.

> Cypher and GQL share the same `MATCH … RETURN` shape, so much of what follows carries
> over to GQL. Where this doc says "Cypher", it means the specific dialect Kineviz's
> KoreDB connector sends — that's what the bridge actually parses.

## 1. The core idea: Cypher clauses map to SQL *layers*

A Cypher read query has the shape

```
MATCH <pattern> [WHERE <expr>] RETURN [DISTINCT] <items> [ORDER BY] [SKIP] [LIMIT]
```

SQL/PGQ's `GRAPH_TABLE(graph MATCH … [WHERE …] COLUMNS(…))` yields a **relational
table**. So every Cypher query becomes a layered SQL statement:

```
SELECT   <- RETURN         (projection / aggregation / graph reconstruction)
FROM GRAPH_TABLE (
    graph
    MATCH   <- MATCH       (pattern: nodes, edges, direction, labels)
    WHERE   <- WHERE       (element/path predicates)
    COLUMNS ( … )          (every property referenced by WHERE or RETURN)
) AS gx
[GROUP BY]  <- aggregation in RETURN
[ORDER BY / LIMIT / OFFSET]  <- ORDER BY / LIMIT / SKIP
```

The inner `COLUMNS` is the seam: `GRAPH_TABLE` can only expose element *properties*
as columns, so the compiler must collect every property the outer query needs and
project it, then refer to those columns from the outer `SELECT`/`WHERE`/`GROUP BY`.

## 2. The pipeline

```
Cypher text
  │  1. parse        cypher_translator.translate → MatchPlan
  │                  predicate.parse            → WHERE expression tree
  │  2. bind         resolve each pattern variable to an element label
  │                  (explicit (v:Label) > id() decode > edge-schema inference)
  │  3. classify     RETURN → graph | scalar-table | aggregation
  │  4. compile      pattern → MATCH path;  WHERE tree → SQL;  RETURN → outer SELECT
  │  5. assemble     layer the SQL; parameterize all values
  ▼
SQL/PGQ  →  execute  →  rows  →  (reconstruct graph | table)  →  Kineviz JSON
```

Each stage is a separate, testable component:

| Stage | Module |
|---|---|
| Cypher → `MatchPlan` (pattern, return items) | `cypher_translator.py` |
| WHERE → predicate AST + SQL/eval | `predicate.py` |
| variable → label resolution | `pg_backend._resolve_labels` |
| RETURN classification + dispatch | `query_processor._parse_return` |
| `MatchPlan` → `GRAPH_TABLE` SQL | `pg_backend` (`compile`/`project`/`aggregate`) |
| rows → graph | `result_converter.py` |
| node identity ⇄ keys | `identity.py` |

## 3. MATCH → the graph pattern

- A node `(v:Label)` → `(v0 IS "Label")`. Labels are **double-quoted** to preserve
  case (unquoted folds to lowercase and PG raises *label does not exist*).
- An edge `[e:TYPE]` with direction → `-[e0 IS "TYPE"]->` / `<-…-` / `-…-`.
- A path chains as one expression: `(v0)-[e0]->(v1)-[e1]->(v2)` — rendered once,
  not per-edge (see `_render_match`).
- Untyped edges `[e]` / alternations `[e:A|B]` are **branched**: one concrete-typed
  query per compatible rel type, results merged (`_expand_branches`,
  `_compatible_typed_plans`). Endpoint labels prune incompatible types.

**Variable → label resolution** (`_resolve_labels`), in precedence order:
1. explicit `(v:Label)`;
2. the element type a selected node's `id()` decodes to (authoritative);
3. edge-schema inference — once one endpoint of a typed edge is known, the other
   is its complement.

## 4. WHERE → a predicate tree → SQL

`WHERE` is a boolean expression, so it is parsed by a real **recursive-descent
parser with precedence** (`predicate.py`) into an AST, then either compiled to SQL
or evaluated in memory. This is what makes `AND`/`OR`/`NOT`/parentheses and every
operator uniform — no per-shape regexes.

Grammar (low → high precedence): `OR` → `AND` → `NOT` → predicate → primary.

| Cypher predicate | SQL |
|---|---|
| `a.p = v` · `<>` `!=` `<` `>` `<=` `>=` | `v0.p <op> %s` (parameterized) |
| `a.p = b.q` (cross-variable) | `v0.p = v1.q` |
| `a.p CONTAINS 'x'` (and `CONTAINS('x')`) | `v0.p::text ILIKE '%x%'` |
| `a.p STARTS WITH / ENDS WITH 'x'` | `ILIKE 'x%'` / `ILIKE '%x'` |
| `a.p =~ 'regex'` | `v0.p::text ~ %s` |
| `a.p IN [ … ]` · `IN UNNEST([ … ])` · `NOT a.p IN [ … ]` | `v0.p IN (%s,…)` / `NOT (…)` |
| `a.p IS [NOT] NULL` | `v0.p IS [NOT] NULL` |
| `a.isfraud` (bare boolean) | `v0.isfraud = true` |
| `id(n) IN [internal_id(t,o), …]` | `v0.<key> IN (%s,…)` (via identity registry) |
| `X AND Y`, `X OR Y`, `NOT X`, `( … )` | `(X AND Y)`, `(X OR Y)`, `NOT (X)`, grouping |
| `toLower(x)` / `toUpper(x)` | folded (literal) or unwrapped (property; ILIKE is case-insensitive) |

Values are always **parameterized** (`%s` → server-side bind); identifiers are
resolved against the schema. The same AST is evaluated in memory by the mock
(`predicate.evaluate`) against a `{var: {label, key, props}}` binding, so id-in and
cross-variable predicates behave identically off-database.

## 5. RETURN → three outer shapes

`RETURN` is classified (`_parse_return`) and dispatched:

| RETURN | Outer SQL | Result to Kineviz |
|---|---|---|
| bare vars / `*` (`RETURN a, r, b`) | `SELECT * …` projecting each binding's key+props | **GRAPH** `{nodes, relationships}` (reconstructed via the manifest) |
| scalar props (`RETURN a.name, b.id`) | `SELECT <cols> …` (or `SELECT DISTINCT`) | **TABLE** |
| aggregates (`RETURN a.name, count(t), sum(t.x)`) | `SELECT <keys>, <aggs> FROM GRAPH_TABLE(…) GROUP BY <keys>` | **TABLE** |

- **Graph** mode projects opaque `__gx_*` key/property columns plus a *projection
  manifest* that maps each column back to a node/edge binding, so the converter can
  rebuild elements and mint stable ids (`identity.py`, `result_converter.py`).
- **Aggregation** collects the group-key and aggregate-argument properties into the
  inner `COLUMNS`, wraps in `GROUP BY`, and `UNION ALL`s across branched rel types.

`SKIP`/`LIMIT` map to `OFFSET`/`LIMIT`; Kineviz's `SKIP <count> LIMIT 1` "is there
more?" probe therefore terminates correctly.

## 6. Node identity (the reason a bridge is needed)

PostgreSQL has no built-in node id that Kineviz can see. The bridge invents a tag for
each node — `"<tableId>:<offset>"` (e.g. `"0:0"` = row 0 of the Client table) — and
keeps a lookup table (per connection) of what each tag stands for. Kineviz sends the tag
back on expand as `internal_id(0, 0)`; the bridge looks it up and rewrites it into a
condition on the real key column (`c.id = '...'`). See the "How node ids work" section
of the [README](../README.md) for a full worked example.

## 7. Coverage & extension points

**Supported:** one- and multi-hop fixed-length paths, directed/undirected/reverse
edges, untyped and multi-type edges, all the common WHERE operators with full
`AND`/`OR`/`NOT` nesting, the three RETURN modes above, `DISTINCT`, aggregates
(including `count(DISTINCT …)`), `ORDER BY … [ASC|DESC]`, `SKIP`/`LIMIT`, and the
built-in answers for schema, counts, liveness checks, and samples.

**Blocked by PostgreSQL 19 Beta 1 (not us):** variable-length / quantified paths
`[:T*1..3]` — `GRAPH_TABLE` raises *"element pattern quantifier is not supported"*
(verified). We reject these with a clear message; they'd need unrolling to a
`UNION` of fixed lengths to emulate.

**Not yet (feasible, deferred):** `WITH` pipelines / subqueries; `UNWIND`;
`OPTIONAL MATCH` (left-join semantics); `HAVING` (WHERE on aggregates); list/map
expressions and functions beyond `toLower/toUpper`; writes.

**To extend**, add at the layer that owns the concern:
- new WHERE operator → an AST node + `to_sql`/`evaluate` case in `predicate.py`;
- new RETURN shape → a branch in `_parse_return` + a backend method;
- new pattern feature → `_parse_pattern` + `_render_match`;
- new scalar function → fold/handle it in `predicate._operand`.

The layering is the point: each Cypher concept has exactly one home, so additions
are local and the whole surface stays testable.
