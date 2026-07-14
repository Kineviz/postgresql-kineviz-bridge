"""In-memory Backend so the whole bridge is runnable with no database.

Holds the doc's `business_network` fixture (Person, Company, KNOWS, WORKS_AT)
as plain dicts and executes a MatchPlan against them. It mints node/edge ids
through the shared IdentityRegistry, so expand round-trips exactly as it will
against PostgreSQL. Data is expressed at the *graph property* level (e.g. the
Person key property is `id`), matching what a SQL/PGQ COLUMNS projection exposes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import predicate
from .backend import (
    Backend,
    GraphNode,
    GraphRel,
    GraphResult,
    MatchPlan,
    NodeSchema,
    RelSchema,
    VertexPat,
    build_schema_response,
)
from .identity import IdentityRegistry


def _nb(label, key, props):
    """A node binding entry for the predicate evaluator."""
    return {"label": label, "key": key, "props": props}


def _sort_rows(rows, order, index_of):
    """Apply ORDER BY to a list of value-rows. NULLs last (ASC) / first (DESC), like PG."""
    for expr, direction in reversed(order):        # reversed → first term is primary key
        idx = index_of.get(expr)
        if idx is None:
            idx = index_of.get(expr.lower())
        if idx is None:
            continue
        rows.sort(key=lambda r: (r[idx] is None, r[idx]), reverse=(direction == "DESC"))
    return rows


def _agg_fn(fn: str, vals):
    if fn == "count":
        return len(vals)
    if not vals:
        return None
    if fn == "sum":
        return sum(vals)
    if fn == "avg":
        return sum(vals) / len(vals)
    if fn == "min":
        return min(vals)
    if fn == "max":
        return max(vals)
    return None


class MockBackend(Backend):
    def __init__(self, registry: IdentityRegistry) -> None:
        self.registry = registry
        self.node_schemas: Dict[str, NodeSchema] = {}
        self.rel_schemas: Dict[str, RelSchema] = {}
        self.nodes: Dict[str, Dict[Any, Dict[str, Any]]] = {}       # label -> {pk_value: props}
        self.edges: Dict[str, List[Dict[str, Any]]] = {}            # type -> [{__src,__dst,...props}]
        self._seed_business_network()
        self.registry.register_aliases(self.labels() + self.rel_types())

    # ----- Backend interface -----

    def schema_response(self, db_name: str) -> Dict[str, Any]:
        return build_schema_response(db_name, self.node_schemas, self.rel_schemas)

    def node_count(self, label: Optional[str] = None) -> int:
        if label is not None:
            return len(self.nodes.get(label, {}))
        return sum(len(rows) for rows in self.nodes.values())

    def rel_count(self, rel_type: Optional[str]) -> int:
        if rel_type:
            return len(self.edges.get(rel_type, []))
        return sum(len(rows) for rows in self.edges.values())

    def sample(self, limit: int) -> GraphResult:
        labels = self.labels()
        if not labels:
            return GraphResult()
        per = max(1, -(-limit // len(labels)))
        out = GraphResult()
        for label in labels:
            if len(out.nodes) >= limit:
                break
            for pk, props in list(self.nodes.get(label, {}).items())[:per]:
                if len(out.nodes) >= limit:
                    break
                out.nodes.append(self._node(label, pk, props))
        return out

    def execute(self, plan: MatchPlan) -> GraphResult:
        if not plan.edges:
            return self._exec_node_only(plan)
        return self._exec_one_hop(plan)

    def project(self, plan: MatchPlan, projections, distinct: bool = False):
        """Scalar RETURN over the in-memory fixture (node-only patterns)."""
        if plan.edges:
            raise NotImplementedError("mock scalar projection supports node-only patterns")
        header = [alias for (_v, _p, alias) in projections]
        rows = []
        for vp in plan.vertices:
            label = vp.label
            if label is None or label not in self.nodes:
                continue
            for pk, props in self.nodes[label].items():
                if self._where_ok(plan, {vp.var: _nb(label, pk, props)}):
                    rows.append([props.get(prop) if var == vp.var else None
                                 for (var, prop, _a) in projections])
        if distinct:
            seen, ded = set(), []
            for r in rows:
                k = tuple(r)
                if k not in seen:
                    seen.add(k); ded.append(r)
            rows = ded
        if plan.order:                              # sort before slicing
            index_of = {}
            for i, (var, prop, alias) in enumerate(projections):
                index_of["{}.{}".format(var, prop)] = i
                index_of[alias] = i
            _sort_rows(rows, plan.order, index_of)
        rows = rows[plan.skip:]
        if plan.limit:
            rows = rows[:plan.limit]
        return header, rows

    def aggregate(self, plan: MatchPlan, group_keys, aggs):
        """Grouped aggregation over the in-memory fixture (node-only + single-edge)."""
        header = [a for (_v, _p, a) in group_keys] + [a for (_f, _v, _p, a, _d) in aggs]
        groups = {}  # group-key-tuple -> {"key": [...], "rows": [binding_row]}
        for brow in self._binding_rows(plan):
            gk = tuple(self._val(brow, v, p) for (v, p, _a) in group_keys)
            groups.setdefault(gk, {"key": list(gk), "rows": []})["rows"].append(brow)
        out = []
        for g in groups.values():
            row = list(g["key"])
            for (fn, v, p, _a, distinct) in aggs:
                if fn == "count" and p is None and not distinct:
                    row.append(len(g["rows"]))
                elif fn == "count" and p is None:                 # count(DISTINCT node) -> distinct keys
                    row.append(len({r.get(v, {}).get("key") for r in g["rows"]}))
                else:
                    vals = [self._val(r, v, p) for r in g["rows"]]
                    vals = [x for x in vals if x is not None]
                    if distinct:
                        vals = list(dict.fromkeys(vals))
                    row.append(_agg_fn(fn, vals))
            out.append(row)
        if plan.order:
            index_of = {}
            for i, (v, p, a) in enumerate(group_keys):
                index_of["{}.{}".format(v, p)] = i
                index_of[a] = i
            for j, (fn, v, p, a, _d) in enumerate(aggs):
                idx = len(group_keys) + j
                index_of[a] = idx
                arg = "*" if v is None else (v if p is None else "{}.{}".format(v, p))
                index_of["{}({})".format(fn, arg)] = idx      # ORDER BY count(b) (by expression)
            _sort_rows(out, plan.order, index_of)
        if plan.limit:
            out = out[:plan.limit]
        return header, out

    def _binding_rows(self, plan: MatchPlan):
        """Yield {var: {label,key,props}} per matched row (node-only or single edge)."""
        if not plan.edges:
            for vp in plan.vertices:
                label = vp.label
                if label is None or label not in self.nodes:
                    continue
                for pk, props in self.nodes[label].items():
                    binding = {vp.var: _nb(label, pk, props)}
                    if self._where_ok(plan, binding):
                        yield binding
            return
        edge = plan.edges[0]
        left, right = plan.vertex(edge.src_var), plan.vertex(edge.dst_var)
        types = edge.types or self.rel_types()
        orientations = {"out": ["fwd"], "in": ["rev"], "both": ["fwd", "rev"]}[edge.direction]
        for t in types:
            rs = self.rel_schemas.get(t)
            if rs is None:
                continue
            for row in self.edges.get(t, []):
                s, d = row["__src"], row["__dst"]
                for orient in orientations:
                    (ll, lp, rl, rp) = (rs.src_label, s, rs.dst_label, d) if orient == "fwd" \
                        else (rs.dst_label, d, rs.src_label, s)
                    if not self._endpoint_ok(left, ll) or not self._endpoint_ok(right, rl):
                        continue
                    eprops = {k: v for k, v in row.items() if k not in ("__src", "__dst")}
                    binding = {
                        edge.src_var: _nb(ll, lp, self.nodes[ll][lp]),
                        edge.dst_var: _nb(rl, rp, self.nodes[rl][rp]),
                        edge.var: {"label": t, "key": None, "props": eprops},
                    }
                    if self._where_ok(plan, binding):
                        yield binding
                    break

    def _where_ok(self, plan: MatchPlan, binding) -> bool:
        return predicate.evaluate(plan.where, binding, self.registry)

    @staticmethod
    def _val(brow, var, prop):
        return brow.get(var, {}).get("props", {}).get(prop) if prop else None

    # ----- execution -----

    def _exec_node_only(self, plan: MatchPlan) -> GraphResult:
        out = GraphResult()
        matched = 0          # index of matching rows (for SKIP/LIMIT windowing)
        taken = 0
        for vp in plan.vertices:
            label = vp.label
            if label is None or label not in self.nodes:
                continue
            for pk, props in self.nodes[label].items():
                if not self._where_ok(plan, {vp.var: _nb(label, pk, props)}):
                    continue
                if matched < plan.skip:
                    matched += 1
                    continue
                matched += 1
                out.nodes.append(self._node(label, pk, props))
                taken += 1
                if plan.limit and taken >= plan.limit:
                    return out
        return out

    def _exec_one_hop(self, plan: MatchPlan) -> GraphResult:
        out = GraphResult()
        matched = 0          # index of matching rows (for SKIP)
        taken = 0            # emitted rows (for LIMIT)
        for edge in plan.edges:
            left = plan.vertex(edge.src_var)
            right = plan.vertex(edge.dst_var)
            types = edge.types or self.rel_types()
            orientations = {"out": ["fwd"], "in": ["rev"], "both": ["fwd", "rev"]}[edge.direction]

            for t in types:
                rs = self.rel_schemas.get(t)
                if rs is None:
                    continue
                seen_rels = set()
                for row in self.edges.get(t, []):
                    src_pk, dst_pk = row["__src"], row["__dst"]
                    for orient in orientations:
                        if orient == "fwd":
                            l_label, l_pk = rs.src_label, src_pk
                            r_label, r_pk = rs.dst_label, dst_pk
                        else:
                            l_label, l_pk = rs.dst_label, dst_pk
                            r_label, r_pk = rs.src_label, src_pk

                        if not self._endpoint_ok(left, l_label) or not self._endpoint_ok(right, r_label):
                            continue
                        edge_props = {k: v for k, v in row.items() if k not in ("__src", "__dst")}
                        binding = {
                            edge.src_var: _nb(l_label, l_pk, self.nodes[l_label][l_pk]),
                            edge.dst_var: _nb(r_label, r_pk, self.nodes[r_label][r_pk]),
                            edge.var: {"label": t, "key": None, "props": edge_props},
                        }
                        if not self._where_ok(plan, binding):
                            continue

                        # Row matched. Apply SKIP (Kineviz paginates), then LIMIT.
                        if matched < plan.skip:
                            matched += 1
                            break
                        matched += 1
                        rel = self._rel(t, rs, src_pk, dst_pk, edge_props)
                        out.nodes.append(self._node(l_label, l_pk, self.nodes[l_label][l_pk]))
                        out.nodes.append(self._node(r_label, r_pk, self.nodes[r_label][r_pk]))
                        if rel.id not in seen_rels:
                            out.relationships.append(rel)
                            seen_rels.add(rel.id)
                        taken += 1
                        if plan.limit and taken >= plan.limit:
                            return out
                        break  # one orientation match is enough per row
        return out

    def _endpoint_ok(self, vp: Optional[VertexPat], label: str) -> bool:
        """Label compatibility only — id/property predicates are checked via the
        WHERE evaluator on the full binding (predicate.evaluate)."""
        if vp is None:
            return True
        return vp.label is None or vp.label == label

    # ----- element builders -----

    def _node(self, label: str, pk: Any, props: Dict[str, Any]) -> GraphNode:
        return GraphNode(
            id=self.registry.node_id(label, (pk,)),
            labels=[label],
            properties={k: v for k, v in props.items() if v is not None},
        )

    def _rel(self, rtype: str, rs: RelSchema, src_pk: Any, dst_pk: Any, props: Dict[str, Any]) -> GraphRel:
        return GraphRel(
            id=self.registry.node_id(rtype, (src_pk, dst_pk)),
            startNodeId=self.registry.node_id(rs.src_label, (src_pk,)),
            endNodeId=self.registry.node_id(rs.dst_label, (dst_pk,)),
            type=rtype,
            properties={k: v for k, v in props.items() if v is not None},
        )

    # ----- fixture -----

    def _seed_business_network(self) -> None:
        self.node_schemas = {
            "Person": NodeSchema("Person", "id", {"id": "INT64", "name": "STRING", "email": "STRING"}),
            "Company": NodeSchema("Company", "id", {"id": "INT64", "name": "STRING"}),
        }
        self.rel_schemas = {
            "KNOWS": RelSchema("KNOWS", "Person", "Person", {"since": "STRING"}),
            "WORKS_AT": RelSchema("WORKS_AT", "Person", "Company", {"title": "STRING", "started_at": "STRING"}),
        }
        self.nodes = {
            "Person": {
                1: {"id": 1, "name": "Alice", "email": "alice@example.com"},
                2: {"id": 2, "name": "Bob", "email": "bob@example.com"},
                3: {"id": 3, "name": "Carol", "email": "carol@example.com"},
                4: {"id": 4, "name": "Dave", "email": None},
            },
            "Company": {
                100: {"id": 100, "name": "Acme"},
                200: {"id": 200, "name": "Globex"},
            },
        }
        self.edges = {
            "KNOWS": [
                {"__src": 1, "__dst": 2, "since": "2021-02-10"},
                {"__src": 2, "__dst": 3, "since": "2022-06-01"},
                {"__src": 1, "__dst": 3, "since": "2020-01-15"},
            ],
            "WORKS_AT": [
                {"__src": 1, "__dst": 100, "title": "CTO", "started_at": "2019-03-01"},
                {"__src": 2, "__dst": 100, "title": "Engineer", "started_at": "2020-07-01"},
                {"__src": 3, "__dst": 200, "title": "Analyst", "started_at": "2021-09-01"},
                {"__src": 4, "__dst": 200, "title": "Manager", "started_at": "2018-05-01"},
            ],
        }
