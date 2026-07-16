"""Discover a PostgreSQL 19 property graph's schema from the Information Schema.

Reads the SQL/PGQ catalog views (verified against postgres:19beta1) and builds
the bridge's NodeSchema/RelSchema plus a label/type -> underlying-table map. This
replaces the seeded fixture schema so the bridge serves *any* property graph.

Key facts about the PG19 views (as observed):
  * pg_element_tables      : element_table_alias, element_table_kind (VERTEX|EDGE), table_name
  * pg_element_table_labels: element_table_alias -> label_name  (labels are stored
                             lowercased when declared unquoted, e.g. LABEL Client -> "client")
  * pg_element_table_key_columns : element_table_alias -> column_name (ordinal_position)
  * pg_element_table_properties  : element_table_alias -> property_name (+ expression)
  * pg_edge_table_components: edge_table_alias, edge_end (SOURCE|DESTINATION), vertex_table_alias
  * pg_property_data_types : property_name -> data_type  (graph-global; a property
                             name has one type across all labels)
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from .backend import NodeSchema, RelSchema

# PostgreSQL type -> the Kuzu-ish type string Kineviz's schema browser expects.
_TYPE_MAP = {
    "text": "STRING", "character varying": "STRING", "varchar": "STRING", "char": "STRING",
    "uuid": "STRING", "name": "STRING",
    "bigint": "INT64", "integer": "INT64", "smallint": "INT64", "int": "INT64",
    "numeric": "DOUBLE", "double precision": "DOUBLE", "real": "DOUBLE", "decimal": "DOUBLE",
    "boolean": "BOOL",
    "date": "DATE",
    "timestamp without time zone": "TIMESTAMP", "timestamp with time zone": "TIMESTAMP",
    "timestamp": "TIMESTAMP",
    "json": "STRING", "jsonb": "STRING",
}


def _gx_type(pg_type: str) -> str:
    return _TYPE_MAP.get((pg_type or "").lower(), "STRING")


def collect_schema(conn, graph_name: str) -> Tuple[Dict[str, NodeSchema], Dict[str, RelSchema], Dict[str, str]]:
    """Return (node_schemas, rel_schemas, tables).

    node_schemas keyed by label, rel_schemas keyed by type, and `tables` maps each
    label/type to its underlying table name (labels can differ from table names,
    e.g. label `transaction` over table `txn`).
    """
    q = lambda sql: _rows(conn, sql, (graph_name,))

    kinds = {a: k for a, k, _ in q(
        "SELECT element_table_alias, element_table_kind, table_name "
        "FROM information_schema.pg_element_tables WHERE property_graph_name = %s")}
    alias_table = {a: t for a, _, t in q(
        "SELECT element_table_alias, element_table_kind, table_name "
        "FROM information_schema.pg_element_tables WHERE property_graph_name = %s")}

    alias_label: Dict[str, str] = {}
    for alias, label in q("SELECT element_table_alias, label_name "
                          "FROM information_schema.pg_element_table_labels WHERE property_graph_name = %s"):
        alias_label.setdefault(alias, label)          # first label wins (single-label graphs)

    alias_keys: Dict[str, list] = {}
    for alias, col, _pos in q("SELECT element_table_alias, column_name, ordinal_position "
                              "FROM information_schema.pg_element_table_key_columns "
                              "WHERE property_graph_name = %s ORDER BY element_table_alias, ordinal_position"):
        alias_keys.setdefault(alias, []).append(col)

    alias_props: Dict[str, list] = {}
    for alias, prop in q("SELECT element_table_alias, property_name "
                         "FROM information_schema.pg_element_table_properties WHERE property_graph_name = %s"):
        alias_props.setdefault(alias, []).append(prop)

    prop_type = {p: _gx_type(dt) for p, dt in q(
        "SELECT property_name, data_type FROM information_schema.pg_property_data_types "
        "WHERE property_graph_name = %s")}

    endpoints: Dict[str, Dict[str, str]] = {}
    edge_fk_cols: Dict[str, set] = {}
    edge_end_col: Dict[str, Dict[str, str]] = {}   # alias -> {SOURCE: col, DESTINATION: col}
    for edge_alias, end, vtx_alias, edge_col in q(
            "SELECT edge_table_alias, edge_end, vertex_table_alias, edge_table_column_name "
            "FROM information_schema.pg_edge_table_components WHERE property_graph_name = %s"):
        endpoints.setdefault(edge_alias, {})[end.upper()] = vtx_alias
        # The edge-side join columns are endpoint references, not edge properties;
        # Kineviz/Kuzu never surface them as properties, so exclude them below.
        if edge_col:
            edge_fk_cols.setdefault(edge_alias, set()).add(edge_col)
            edge_end_col.setdefault(edge_alias, {})[end.upper()] = edge_col

    node_schemas: Dict[str, NodeSchema] = {}
    rel_schemas: Dict[str, RelSchema] = {}
    tables: Dict[str, str] = {}

    for alias, kind in kinds.items():
        label = alias_label.get(alias, alias)
        props = {p: prop_type.get(p, "STRING") for p in alias_props.get(alias, [])}
        if kind == "VERTEX":
            keys = alias_keys.get(alias) or list(props.keys())[:1]
            pk = keys[0] if keys else "id"
            node_schemas[label] = NodeSchema(label=label, primary_key=pk, properties=props, keys=list(keys))
            tables[label] = alias_table[alias]
        elif kind == "EDGE":
            ep = endpoints.get(alias, {})
            src = alias_label.get(ep.get("SOURCE"))
            dst = alias_label.get(ep.get("DESTINATION"))
            if src is None or dst is None:
                continue
            fk = edge_fk_cols.get(alias, set())
            edge_props = {p: t for p, t in props.items() if p not in fk}
            ends = edge_end_col.get(alias, {})
            rel_keys = [c for c in (ends.get("SOURCE"), ends.get("DESTINATION")) if c]
            rel_schemas[label] = RelSchema(type=label, src_label=src, dst_label=dst,
                                           properties=edge_props, keys=rel_keys)
            tables[label] = alias_table[alias]

    return node_schemas, rel_schemas, tables


def _rows(conn, sql: str, params) -> list:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())
