# Spanner Graph vs PostgreSQL 19 SQL/PGQ

A concrete, tested comparison of how the same **PaySim** property graph is defined
and queried in **Spanner Graph** versus **PostgreSQL 19** (SQL/PGQ)
(`fixtures/paysim/02_build_graph.sql`). Both are property graphs *over existing
relational tables* in the SQL:2023 / SQL/PGQ lineage — no separate graph store, no
data duplication.

Findings marked **[verified]** were checked against `postgres:19beta1`;
Spanner-side statements are from Spanner Graph's documentation.

---

## Same graph, two DDLs

**Spanner Graph** (GQL DDL):

```sql
CREATE OR REPLACE PROPERTY GRAPH graph_view
NODE TABLES (
    Client KEY (id) LABEL Client PROPERTIES (id, name, isfraud),
    ...
)
EDGE TABLES (
    Client_Perform_Transaction
        KEY (client_id, transaction_id, timestamp)
        SOURCE KEY (client_id) REFERENCES Client (id)
        DESTINATION KEY (transaction_id) REFERENCES Transaction (id)
        LABEL PERFORMS PROPERTIES (timestamp, client_id, transaction_id),
    ...
)
```

**PostgreSQL 19** (`fixtures/paysim/02_build_graph.sql`):

```sql
CREATE PROPERTY GRAPH paysim
    VERTEX TABLES (
        client KEY (id) LABEL Client PROPERTIES (id, name, isfraud),
        ...
    )
    EDGE TABLES (
        client_perform_transaction KEY (rid)
            SOURCE KEY (client_id) REFERENCES client (id)
            DESTINATION KEY (transaction_id) REFERENCES txn (id)
            LABEL PERFORMS PROPERTIES (ts, client_id, transaction_id),
        ...
    );
```

---

## Similarities

- **Same model.** `CREATE PROPERTY GRAPH <name>` over base tables; a graph is a
  logical view, not a copy. Both discover nodes/edges from ordinary tables.
- **Same DDL skeleton.** `(NODE|VERTEX) TABLES (...)` + `EDGE TABLES (...)`; each
  element has `KEY (...)`, one or more `LABEL`s, and `PROPERTIES (col [AS alias])`.
- **`NODE TABLES` keyword is accepted by both.** PG19 takes `NODE TABLES` as a
  synonym for `VERTEX TABLES` **[verified]**, so the Spanner node-table clause
  ports unchanged.
- **Explicit endpoints.** `SOURCE KEY (...) REFERENCES <vtable> (...)` /
  `DESTINATION KEY (...) REFERENCES ...` is identical.
- **No FK / referential integrity required.** Only the *referenced* key must be
  unique; dangling edges are silently ignored, not errors **[verified on PG19]**.
- **Composite & multi-label** elements are supported by both.
- **Multiple labels per element** (`LABEL a LABEL b`) supported by both.

---

## Differences

| Aspect | Spanner Graph | PostgreSQL 19 SQL/PGQ |
|---|---|---|
| Node-table keyword | `NODE TABLES` | `VERTEX TABLES` (also accepts `NODE TABLES`) |
| Replace in place | `CREATE OR REPLACE PROPERTY GRAPH` | **not supported** — `DROP PROPERTY GRAPH IF EXISTS` + `CREATE` **[verified]** |
| Query surface | GQL: `GRAPH g MATCH … RETURN …` returning graph values (plus `GRAPH_TABLE`) | **only** `GRAPH_TABLE (g MATCH … COLUMNS …)` embedded in SQL; standalone `GRAPH … RETURN` is a **syntax error** **[verified]** |
| Result type | graph/tabular values via GQL | **relational rows only** — the client must reconstruct the graph (this bridge's job) |
| Property type across labels | per-label — `id` may be INT64 on Client and STRING on Merchant | **one type per property name across all labels** — forced every `id` (and edge FK columns) to `text` **[verified: mixed types → error]** |
| Identifier case | declared `LABEL Client` is preserved without quoting | unquoted folds to lowercase; to keep the TitleCase/UPPER_SNAKE convention the DDL **double-quotes** labels (`LABEL "Client"`) and the bridge quotes them in generated `GRAPH_TABLE` SQL (`IS "Client"`) — an unquoted label raises *"label … does not exist"* **[verified]** |
| `timestamp` as a property | allowed | keyword collision — exposed as `ts` here |
| Maturity / ops | GA, distributed, managed cloud | Beta 1 (docs say "unsupported version"), single-node, syntax may change before GA |

---

## Query language, side by side

The same fraud path — a fraudulent client transferring to another client.

**Spanner (GQL):**

```sql
GRAPH graph_view
MATCH (c:Client {isfraud: true})-[:PERFORMS]->(t:Transaction)-[:TO_CLIENT]->(d:Client)
RETURN c.name, t.amount, d.name
```

**PostgreSQL 19 (SQL/PGQ):**

```sql
SELECT * FROM GRAPH_TABLE ( paysim
    MATCH (c IS Client WHERE c.isfraud = true)
          -[IS performs]->(t IS Transaction)-[IS to_client]->(d IS Client)
    COLUMNS (c.name AS client, t.amount AS amount, d.name AS recipient)
);
```

Pattern-syntax notes:
- Label test: GQL `(c:Client)` vs SQL/PGQ `(c IS Client)`.
- Inline property match: GQL `{isfraud: true}` vs SQL/PGQ `WHERE c.isfraud = true`.
- Projection: GQL `RETURN …` (can yield graph values) vs SQL/PGQ `COLUMNS (…)`
  (always a relational projection).
- Multi-label alternation in a pattern: both use `(x IS A|B)` / `[y IS T1|T2]`.

---

## Why this matters for the Kineviz bridge

Kineviz speaks **Cypher** and expects **graph values** back. Spanner Graph
(and its Kineviz Explorer) can return graph-shaped results directly via GQL.
PostgreSQL 19 returns only a **relational table** from `GRAPH_TABLE`, so the
bridge must (a) translate Cypher → `GRAPH_TABLE`, and (b) reconstruct nodes/edges
from rows — plus quote labels consistently (to keep case) and honor the
uniform-`id`-type rule. That reconstruction layer is the whole reason this
project exists; against Spanner it would be largely unnecessary.
