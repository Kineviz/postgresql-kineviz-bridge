"""Build the Database Proxy `graphSchema` from our NodeSchema/RelSchema.

Kineviz calls `GET /api/postgresql/{project}/graphSchema` on connect to populate
its category/relationship panels. This maps our discovered schema (metadata.py)
into the proxy's `Category` / `Relationship` shapes, carrying key columns + types
so Kineviz knows each node's identity.
"""

from __future__ import annotations

from typing import Dict

from .backend import NodeSchema, RelSchema
from .proxy_models import Category, GraphSchema, Relationship


def build_graph_schema(node_schemas: Dict[str, NodeSchema],
                       rel_schemas: Dict[str, RelSchema]) -> GraphSchema:
    categories = []
    for label, ns in node_schemas.items():
        keys = ns.keys or [ns.primary_key]
        categories.append(Category(
            name=label,
            props=list(ns.properties.keys()),
            keys=keys,
            keysTypes={k: ns.properties.get(k, "STRING") for k in keys},
            propsTypes=dict(ns.properties),
        ))

    relationships = []
    for typ, rs in rel_schemas.items():
        categories_props = dict(rs.properties)
        relationships.append(Relationship(
            name=typ,
            props=list(rs.properties.keys()),
            keys=list(rs.keys),
            # endpoint join columns aren't kept as edge properties, so default their type
            keysTypes={k: categories_props.get(k, "STRING") for k in rs.keys},
            propsTypes=categories_props,
            startCategory=rs.src_label,
            endCategory=rs.dst_label,
        ))

    return GraphSchema(categories=categories, relationships=relationships)
