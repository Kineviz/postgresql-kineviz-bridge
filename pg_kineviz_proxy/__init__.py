"""PostgreSQL 19 SQL/PGQ -> Kineviz bridge.

Lets Kineviz query a PostgreSQL 19 property graph by looking like a KoreDB
connection (KoreDB is a Kineviz-managed fork of the open-source KuzuDB, so it
shares the same HTTP + JSON interface): it receives Kineviz's Cypher queries over HTTP, translates them into
PostgreSQL 19 GRAPH_TABLE queries, and returns the rows as the {nodes,
relationships} JSON Kineviz expects. See the README for a worked example.
"""

__version__ = "0.1.0"
