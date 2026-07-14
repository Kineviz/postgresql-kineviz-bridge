# Limitations & unsupported features

What the bridge does **not** handle, split by cause. See
[`cypher-to-sqlpgq-mapping.md`](cypher-to-sqlpgq-mapping.md) for what *is*
supported and how the translation works.

Status legend:
- 🟥 **Blocked by PostgreSQL 19** — the database itself doesn't support it (Beta 1,
  verified). Awaiting a future PostgreSQL release.
- 🟨 **Not yet in the bridge** — PostgreSQL supports it (or it's pure translation);
  we simply haven't implemented it. Feasible to add.

---

## 1. Awaiting PostgreSQL 🟥

### Variable-length / quantified paths — `[:T*]`, `[:T*1..3]`, `+`
PostgreSQL 19 Beta 1's `GRAPH_TABLE` has **no path quantifier**. Verified:

```
MATCH (a IS N)-[IS E]->{1,3}(b IS N) …
ERROR:  element pattern quantifier is not supported
```

The bridge rejects such queries with a clear message. They cannot be pushed down;
emulating them requires **unrolling** into a `UNION ALL` of fixed-length paths
(1-hop ∪ 2-hop ∪ 3-hop), capped at a max depth — a possible future addition, but
not equivalent to true transitive closure.

### Other `GRAPH_TABLE` / SQL/PGQ boundaries in Beta 1
These shape how graphs are built and queried (recorded in
[`spanner-vs-postgresql19-sqlpgq.md`](spanner-vs-postgresql19-sqlpgq.md)):

- **No standalone GQL** — only `GRAPH_TABLE(… COLUMNS …)` embedded in SQL; there
  is no `GRAPH g MATCH … RETURN …` returning graph values. (The bridge is built
  around this — it reconstructs the graph from rows.)
- **No `CREATE OR REPLACE PROPERTY GRAPH`** — use `DROP … IF EXISTS` + `CREATE`.
- **One type per property name across labels** — a property with the same name
  must have the same type on every label (so e.g. all `id` columns are `text`).
- Beta software: syntax/behavior may change before GA; re-validate on upgrade.

---

## 2. Not yet implemented in the bridge 🟨

Read-only Cypher features Kineviz (or users) may send that we don't translate yet.
Each is a self-contained addition (see the [mapping guide](cypher-to-sqlpgq-mapping.md)).

| Feature | Notes |
|---|---|
| `OPTIONAL MATCH` | Left-join semantics; would map to `LEFT JOIN`/optional pattern. |
| `WITH` pipelines / multi-part queries | Chaining, intermediate aggregation, `CALL { … }` subqueries. |
| `UNWIND` | Expand a list into rows. |
| `HAVING` (filter on aggregates) | `WHERE` after a `GROUP BY`, e.g. `… count(t) > 5`. |
| Expressions & functions | Arithmetic (`a + b`), `substring`/`size`/`coalesce`/`toString`, `CASE`, etc. Only `toLower`/`toUpper` are handled today. |
| Path variables & path functions | `p = (a)-[*]-(b)`, `nodes(p)`, `length(p)`. |
| `labels(n)` / `type(r)` in projections/filters | Label/type introspection. |
| Composite keys | Registry/predicates assume single-column keys today. |
| Multiple labels per element | One label per element assumed (schema exposes one). |
| Comma-separated / disjoint MATCH patterns | `MATCH (a), (b)` and `MATCH (a)-[..]->(b), (c)-[..]->(d)` — cross-joins / multi-part patterns. |
| Mixed graph + scalar RETURN | `RETURN n, n.name` returns the **graph** (the scalar column is dropped), not a mixed table. |
| `ORDER BY <expr>` | `var.prop`, RETURN aliases, and aggregate expressions (`count(t)`) sort; arbitrary expressions don't. |

## 3. Out of scope by design

- **Writes** — `CREATE` / `MERGE` / `SET` / `DELETE` / `REMOVE` / `DROP` over Cypher
  are rejected. The bridge is a **read-only** query/visualization proxy; data is
  loaded through SQL (see `fixtures/`, `scripts/`).
- **Arbitrary SQL passthrough** — only the translated `GRAPH_TABLE` surface runs.

## 4. Node ids (tags) are session-scoped 🟨

The bridge invents a tag (`"<tableId>:<offset>"`) for each node so Kineviz's *expand*
works (see the README's "How node ids work"). These tags are held **in the bridge's
memory**, not stored in PostgreSQL, and are minted in the order nodes are first seen.
Consequences:

- **A bridge restart clears the tags.** Node ids Kineviz still holds (on the canvas or in
  a saved project) won't resolve against a freshly restarted bridge — expanding those
  nodes returns nothing until they're re-loaded by a query, which mints new tags.
- **A tag anchors on the row's key.** If the underlying row is **deleted** or its **key
  value changes**, the tag points at a row that no longer exists and expand comes back
  empty. Changing non-key columns is safe.
- **Tags aren't stable across sessions.** The same row can get a different tag in a
  different run, so ids saved in a Kineviz project are only reliable while that same
  bridge process keeps running against unchanged data.

Practical guidance: after a large data change or a restart, reload from a fresh query
rather than expanding pre-existing nodes.

---

## Handling

Unsupported shapes fail **loudly, not silently**: an untranslatable query
(unsupported clause, comma pattern, unparseable WHERE expression, function/
expression RETURN item, variable-length path) returns a one-row error table with
a clear message, rather than an empty or wrong result — so it's obvious in
Kineviz what didn't work. Labels typed in the wrong case resolve
case-insensitively (`:client` → `Client`).

`scripts/probe_queries.py` fires a broad battery of queries (working shapes and
every limitation above) at a running bridge and classifies each response — run it
after changes to confirm nothing regressed to silent-wrong behavior.
