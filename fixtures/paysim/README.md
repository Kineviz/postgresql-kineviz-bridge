# PaySim — sample dataset

A financial-fraud property graph built from **PaySim**, a synthetic mobile-money
simulation. This is the larger of the bridge's two sample datasets — the other is
the tiny `business_network` fixture (`fixtures/business_network_pg19.sql`).

The schema models the classic PaySim fraud structure — clients, transactions,
merchants, banks, and the shared email/phone/SSN links that expose fraud rings. For
how PostgreSQL 19's graph syntax compares with Spanner Graph's, see
[`docs/spanner-vs-postgresql19-sqlpgq.md`](../../docs/spanner-vs-postgresql19-sqlpgq.md).

## Graph

**7 node labels** — Client, Merchant, Bank, Transaction, Email, PhoneNumber, SSN
**7 edge labels** — PERFORMS, TO_CLIENT, TO_MERCHANT, TO_BANK, HAS_EMAIL, HAS_PHONE, HAS_SSN

```
(Client)-[PERFORMS]->(Transaction)-[TO_CLIENT|TO_MERCHANT|TO_BANK]->(Client|Merchant|Bank)
(Client)-[HAS_EMAIL|HAS_PHONE|HAS_SSN]->(Email|PhoneNumber|SSN)
```

Scale: 1,334 clients (334 fraud), 347 merchants, 3 banks, 172,499 transactions,
~178k nodes / ~347k edges total.

> **Label convention.** Node labels are TitleCase (`Client`, `Merchant`,
> `Transaction`, …) and edge labels are UPPER_SNAKE (`PERFORMS`, `HAS_EMAIL`, …).
> The DDL **double-quotes** them so PG19 preserves the case (unquoted identifiers
> fold to lowercase), and the bridge quotes labels in generated `GRAPH_TABLE` SQL
> to match. `id` is **text** on every node (PG19 requires one type per property
> name); Transaction `id` = `globalstep`; the edge `timestamp` is exposed as `ts`.

## Source data

**Bundled with the repo — no download needed.** The full dataset ships as compressed CSVs
in [`paysim_data.zip`](paysim_data.zip) (~8 MB). `scripts/load_paysim.sh` unzips it to
`data/raw/` and `data/processed/` automatically on first run, so a fresh clone just works.

To load a different copy instead, set `PAYSIM_DIR` to a folder that contains
`data/raw/{clients,merchants}.csv` and `data/processed/*.csv`.

PaySim is a **fully synthetic** (made-up, no real people) mobile-money simulation. The
original simulator was created by **Dr. Edgar Lopez-Rojas**
([EdgarLopezPhD/PaySim](https://github.com/EdgarLopezPhD/PaySim), **GPL-3.0**); the graph
bundled here was generated with **PaySim 2** by **David Voutila**
([voutilad/paysim-demo](https://github.com/voutilad/paysim-demo), **GPL-3.0**) and
assembled into a property graph by Kineviz. The bundled CSVs are synthetic output of that
simulator.

If you use PaySim, please cite:

> E. A. Lopez-Rojas, A. Elmir, and S. Axelsson. "PaySim: A financial mobile money
> simulator for fraud detection." 28th European Modeling and Simulation Symposium (EMSS),
> Larnaca, Cyprus, 2016.

## Load it

```bash
# 1. PostgreSQL 19 beta with SQL/PGQ
docker run -d --name pg19beta -e POSTGRES_PASSWORD=pgpw -e POSTGRES_DB=appdb \
    -p 5433:5432 postgres:19beta1

# 2. build the graph (staging -> node/edge tables -> CREATE PROPERTY GRAPH paysim)
./scripts/load_paysim.sh          # or: PAYSIM_DIR=/path/to/paysim ./scripts/load_paysim.sh
```

Files:
- `01_staging.sql` — raw-CSV staging tables.
- `02_build_graph.sql` — typed node/edge tables + `CREATE PROPERTY GRAPH paysim`.
- `../../scripts/load_paysim.sh` — `\copy` loader + build + row-count verification.

## Serve it to Kineviz

```bash
python3 pg_kineviz_server.py --backend pg \
    --dsn "host=127.0.0.1 port=5433 dbname=appdb user=postgres password=pgpw" \
    --graph paysim --port 7072 --host ::
# In Kineviz, add a KoreDB (via Proxy API) connection to:
#   http://localhost:7072/postgres-graph/paysim
```

The bridge auto-discovers the schema from the Information Schema
(`pg_kineviz_proxy/metadata.py`), so no per-dataset configuration is needed.

## Example query

Fraud ring — a fraudulent client moving money to another client:

```
MATCH (c:Client)-[:PERFORMS]->(t:Transaction)-[:TO_CLIENT]->(d:Client)
WHERE c.isfraud = true
RETURN c, t, d
```

(PaySim fraud is concentrated in transfers / cash-outs, not merchant payments.)
