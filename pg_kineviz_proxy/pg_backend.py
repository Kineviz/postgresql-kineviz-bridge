"""PostgreSQL 19 SQL/PGQ backend.

Compiles a MatchPlan into a `GRAPH_TABLE ... MATCH ... COLUMNS` query plus a
projection manifest (doc §3B, §9), and — when connected — executes it and
converts rows back to a GraphResult via result_converter.

The SQL compilation is fully implemented and unit-demonstrable with no database.
Live execution is behind a lazy `psycopg` import so this module loads anywhere.
Schema metadata is supplied at construction (a metadata collector would fill it
from the PostgreSQL Information Schema views — see doc §6); for the skeleton it
can be seeded from the same fixture the mock uses.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import predicate
from .backend import (
    Backend,
    EdgePat,
    GraphResult,
    MatchPlan,
    NodeSchema,
    RelSchema,
    VertexPat,
    build_schema_response,
)
from .identity import IdentityRegistry
from .result_converter import convert_rows


class CompiledQuery:
    def __init__(self, sql: str, params: List[Any], manifest: Dict[str, Any]) -> None:
        self.sql = sql
        self.params = params
        self.manifest = manifest


class Pg19Backend(Backend):
    def __init__(
        self,
        graph_name: str,
        registry: IdentityRegistry,
        node_schemas: Optional[Dict[str, NodeSchema]] = None,
        rel_schemas: Optional[Dict[str, RelSchema]] = None,
        dsn: Optional[str] = None,
    ) -> None:
        self.graph_name = graph_name
        self.node_schemas = node_schemas or {}
        self.rel_schemas = rel_schemas or {}
        self.registry = registry
        self.dsn = dsn
        self._conn = None
        self.last_sql: Optional[str] = None      # most recent SQL run (for logging/debug)
        # label/type -> underlying table name (labels can differ, e.g. transaction/txn)
        self.tables: Dict[str, str] = {}
        if self.node_schemas or self.rel_schemas:
            self.registry.register_aliases(self.labels() + self.rel_types())

    # ----- Backend interface -----

    def schema_response(self, db_name: str) -> Dict[str, Any]:
        return build_schema_response(db_name, self.node_schemas, self.rel_schemas)

    def node_count(self, label: Optional[str] = None) -> int:
        if not self._connected():
            return 0
        labels = [label] if label else self.labels()
        return sum(self._scalar("SELECT count(*) FROM " + self._vtable(l)) for l in labels)

    def rel_count(self, rel_type: Optional[str]) -> int:
        if not self._connected():
            return 0
        types = [rel_type] if rel_type else self.rel_types()
        return sum(self._scalar("SELECT count(*) FROM " + self._etable(t)) for t in types)

    def sample(self, limit: int) -> GraphResult:
        # A production sampler would issue one vertex-only GRAPH_TABLE per label.
        raise NotImplementedError("sample() requires a live PostgreSQL connection")

    def execute(self, plan: MatchPlan) -> GraphResult:
        compiled = self.compile(plan)
        if not self._connected():
            raise NotImplementedError(
                "Pg19Backend.execute needs a live connection; use compile() to inspect the SQL"
            )
        rows = self._run(compiled.sql, compiled.params)
        return convert_rows(rows, compiled.manifest, self.registry)

    # ----- the compiler (requirement 1: Cypher plan -> SQL/PGQ) -----

    def compile(self, plan: MatchPlan) -> CompiledQuery:
        gen, va, match_sql, where_sql, params = self._prepare(plan)
        columns, manifest = self._render_columns(plan, gen, va)
        order_sql = self._order_sql(plan.order, self._graph_sort_map(manifest))
        sql = self._assemble(match_sql, where_sql, columns, plan.limit, plan.skip, order_sql=order_sql)
        return CompiledQuery(sql, params, manifest)

    def _order_sql(self, order, sort_map) -> str:
        """ORDER BY clause, mapping each `var.prop`/alias term to its output column."""
        terms = []
        for expr, direction in order:
            col = sort_map.get(expr) or sort_map.get(expr.lower())
            if col:
                terms.append("{} {}".format(col, direction))
        return "ORDER BY " + ", ".join(terms) if terms else ""

    def _graph_sort_map(self, manifest) -> Dict[str, str]:
        """`var.prop` -> the __gx column projected for it (graph-mode ORDER BY)."""
        sm: Dict[str, str] = {}
        for v in manifest["vertices"]:
            pk = self.node_schemas[v["alias"]].primary_key
            if v["key_cols"]:
                sm["{}.{}".format(v["var"], pk)] = v["key_cols"][0]
            for col, prop in v["prop_cols"].items():
                sm["{}.{}".format(v["var"], prop)] = col
        for e in manifest["edges"]:
            for col, prop in e["prop_cols"].items():
                sm["{}.{}".format(e["var"], prop)] = col
        return sm

    def _prepare(self, plan: MatchPlan):
        """Resolve labels, assign generated vars, render MATCH + WHERE."""
        va = self._resolve_labels(plan)
        # Never emit invalid SQL (e.g. `(v0 IS UNKNOWN) COLUMNS ()`). If a bound
        # vertex has no resolvable label/type, this shape isn't translatable here.
        unresolved = [v.var for v in plan.vertices if v.var not in va]
        if unresolved:
            raise ValueError(
                "cannot resolve label(s) for %s — untyped pattern not supported by the "
                "SQL/PGQ compiler (should be intercepted or branched upstream)" % ", ".join(unresolved)
            )
        gen: Dict[str, str] = {}          # plan var -> generated pattern var (v0/e0/...)
        vi = ei = 0
        for v in plan.vertices:
            gen[v.var] = "v{}".format(vi); vi += 1
        for e in plan.edges:
            gen[e.var] = "e{}".format(ei); ei += 1
        match_sql = self._render_match(plan, gen, va)
        params: List[Any] = []
        where_sql = self._render_where(plan, gen, va, params)
        return gen, va, match_sql, where_sql, params

    def _graph_table_expr(self, match_sql, where_sql, columns) -> str:
        """The `GRAPH_TABLE ( graph MATCH ... [WHERE ...] COLUMNS (...) )` expression."""
        lines = ["GRAPH_TABLE (", "    " + self.graph_name, "    MATCH", "        " + match_sql]
        if where_sql:
            lines.append("    WHERE " + where_sql)
        lines.append("    COLUMNS (")
        lines.append(",\n".join("        " + c for c in columns))
        lines.append("    )")
        lines.append(")")
        return "\n".join(lines)

    def _assemble(self, match_sql, where_sql, columns, limit, skip, distinct=False, order_sql=""):
        sql = "SELECT {}* FROM {} AS gx".format(
            "DISTINCT " if distinct else "", self._graph_table_expr(match_sql, where_sql, columns))
        if order_sql:
            sql += "\n" + order_sql
        if limit:
            sql += "\nLIMIT {}".format(limit)
        if skip:
            sql += "\nOFFSET {}".format(skip)
        return sql + ";"

    # ----- aggregation: GROUP BY over GRAPH_TABLE -----

    def aggregate(self, plan: MatchPlan, group_keys, aggs):
        variants = self._compatible_typed_plans(plan)
        header = [a for (_v, _p, a) in group_keys] + [a for (_f, _v, _p, a, _d) in aggs]
        if not variants:
            return header, []

        # The (var, prop) each agg reads. `count(*)`/`count(var)` need no column
        # unless DISTINCT, in which case count DISTINCT of the var's key column.
        def arg_key(fn, v, p, distinct):
            if p:
                return (v, p)
            if v is not None and distinct:                      # count(DISTINCT node) -> distinct pk
                label = self._explicit_label(plan, v)
                pk = self.node_schemas[label].primary_key if label in self.node_schemas else None
                return (v, pk) if pk else None
            return None

        # inner columns needed = distinct (var, prop) from group keys + agg args
        needed: List[Tuple[str, str]] = [(v, p) for (v, p, _a) in group_keys]
        for (fn, v, p, _a, d) in aggs:
            ak = arg_key(fn, v, p, d)
            if ak:
                needed.append(ak)
        ordered: List[Tuple[str, str]] = []
        for np in needed:
            if np not in ordered:
                ordered.append(np)
        colmap = {np: "c{}".format(i) for i, np in enumerate(ordered)}

        inner_sqls, params = [], []
        for vp in variants:
            gen, va, match_sql, where_sql, ps = self._prepare(vp)
            cols = ["{}.{} AS {}".format(gen[var], prop, colmap[(var, prop)]) for (var, prop) in ordered]
            if not cols:
                cols = ["1 AS c0"]                       # e.g. RETURN count(*) with no group key
            inner_sqls.append("SELECT * FROM {} AS gx".format(
                self._graph_table_expr(match_sql, where_sql, cols)))
            params += ps
        union = "\nUNION ALL\n".join(inner_sqls)

        sel, oi = [], 0
        sort_map: Dict[str, str] = {}
        for (v, p, a) in group_keys:
            sel.append("{} AS o{}".format(colmap[(v, p)], oi))
            sort_map["{}.{}".format(v, p)] = "o{}".format(oi)
            sort_map[a] = "o{}".format(oi)
            oi += 1
        for (fn, v, p, a, d) in aggs:
            ak = arg_key(fn, v, p, d)
            if ak is None:                                      # count(*) / count(var) non-distinct
                expr = "count(*)"
            else:
                expr = "{}({}{})".format(fn, "DISTINCT " if d else "", colmap[ak])
            sel.append("{} AS o{}".format(expr, oi))
            sort_map[a] = "o{}".format(oi)
            # also let ORDER BY reference the aggregate by expression, e.g. `count(t)`
            arg = "*" if v is None else (v if p is None else "{}.{}".format(v, p))
            sort_map["{}({})".format(fn, arg)] = "o{}".format(oi)
            oi += 1
        grp = [colmap[(v, p)] for (v, p, _a) in group_keys]
        outer = "SELECT {} FROM (\n{}\n) u".format(", ".join(sel), union)
        if grp:
            outer += " GROUP BY {}".format(", ".join(grp))
        order_sql = self._order_sql(plan.order, sort_map)
        if order_sql:
            outer += " " + order_sql
        if plan.limit:
            outer += " LIMIT {}".format(plan.limit)
        outer += ";"

        self.last_sql = outer
        if not self._connected():
            raise NotImplementedError("aggregate() needs a live connection")
        rows = self._run(outer, params)
        out = [[r.get("o{}".format(i)) for i in range(oi)] for r in rows]
        return header, out

    def _explicit_label(self, plan: MatchPlan, var: str):
        vp = plan.vertex(var)
        return vp.label if vp else None

    def _edge_compatible(self, rs, sl, dl, direction) -> bool:
        def ok(src, dst):
            return (sl is None or sl == src) and (dl is None or dl == dst)
        if direction == "in":
            return ok(rs.dst_label, rs.src_label)
        if direction == "both":
            return ok(rs.src_label, rs.dst_label) or ok(rs.dst_label, rs.src_label)
        return ok(rs.src_label, rs.dst_label)

    def _compatible_typed_plans(self, plan: MatchPlan) -> List[MatchPlan]:
        """Concrete single-edge-type plans compatible with the endpoint labels.

        Resolves an untyped edge (e.g. `[send]`) to the rel type(s) whose
        source/destination labels match the pattern's endpoints — so
        `(Client)-[send]->(Transaction)` becomes exactly PERFORMS.
        """
        if not plan.edges:
            return [plan]
        if len(plan.edges) != 1:
            return [plan]
        e = plan.edges[0]
        cands = [self._canon_label(t) for t in e.types] if e.types else list(self.rel_schemas.keys())
        sl = self._canon_label(self._explicit_label(plan, e.src_var))
        dl = self._canon_label(self._explicit_label(plan, e.dst_var))
        out = []
        for t in cands:
            rs = self.rel_schemas.get(t)
            if rs and self._edge_compatible(rs, sl, dl, e.direction):
                out.append(MatchPlan(
                    vertices=plan.vertices,
                    edges=[EdgePat(e.var, [t], e.direction, e.src_var, e.dst_var)],
                    where=plan.where, limit=None, skip=0, return_items=plan.return_items))
        return out

    def project(self, plan: MatchPlan, projections, distinct: bool = False):
        gen, va, match_sql, where_sql, params = self._prepare(plan)
        cols, sort_map = [], {}
        for i, (var, prop, alias) in enumerate(projections):
            if var not in gen:
                raise ValueError("RETURN references unknown variable '{}'".format(var))
            cols.append("{}.{} AS c{}".format(gen[var], prop, i))
            sort_map["{}.{}".format(var, prop)] = "c{}".format(i)
            sort_map[alias] = "c{}".format(i)
        order_sql = self._order_sql(plan.order, sort_map)
        sql = self._assemble(match_sql, where_sql, cols, plan.limit, plan.skip,
                             distinct=distinct, order_sql=order_sql)
        self.last_sql = sql
        header = [alias for (_v, _p, alias) in projections]
        if not self._connected():
            raise NotImplementedError("project() needs a live connection")
        rows = self._run(sql, params)
        out = [[r.get("c{}".format(i)) for i in range(len(projections))] for r in rows]
        return header, out

    def _canon_label(self, name):
        """Resolve a label/type to its canonical schema case (case-insensitive),
        so a user typing `:client` matches the stored `Client`."""
        if name is None or name in self.node_schemas or name in self.rel_schemas:
            return name
        low = name.lower()
        for k in list(self.node_schemas) + list(self.rel_schemas):
            if k.lower() == low:
                return k
        return name

    def _resolve_labels(self, plan: MatchPlan) -> Dict[str, str]:
        """Return {plan_var: label/type}.

        Precedence: (1) explicit `(v:Label)`; (2) the element type a selected
        node's `internal_id(...)` decodes to — authoritative, since the id knows
        what it is; (3) the edge schema. For a typed edge, once one endpoint's
        label is known the other is its complement — so an undirected expand
        whose selected node is on the DESTINATION side resolves correctly
        instead of assuming the SOURCE side.
        """
        va: Dict[str, str] = {}
        for v in plan.vertices:
            if v.label:
                va[v.var] = self._canon_label(v.label)
        # id-decode hints (authoritative element type of a selected node)
        for v in plan.vertices:
            if v.var not in va and v.id_refs:
                for tid, off in v.id_refs:
                    dec = self.registry.decode(tid, off)
                    if dec:
                        va[v.var] = dec[0]
                        break
        # edge schema: fill unknowns, respecting any known endpoint
        for e in plan.edges:
            rtype = self._canon_label((e.types or [None])[0])
            rs = self.rel_schemas.get(rtype) if rtype else None
            if not rs:
                continue
            va.setdefault(e.var, rtype)
            sv, dv = e.src_var, e.dst_var

            def complement(lbl):
                return rs.dst_label if lbl == rs.src_label else rs.src_label

            if sv in va and dv not in va:
                va[dv] = complement(va[sv])
            elif dv in va and sv not in va:
                va[sv] = complement(va[dv])
            elif sv not in va and dv not in va:
                if e.direction == "in":
                    va[sv], va[dv] = rs.dst_label, rs.src_label
                else:
                    va[sv], va[dv] = rs.src_label, rs.dst_label
        return va

    def _render_match(self, plan: MatchPlan, gen: Dict[str, str], va: Dict[str, str]) -> str:
        # Labels/types are double-quoted so case is preserved and matches the
        # stored (case-sensitive) label — an unquoted label folds to lowercase and
        # PG19 raises "label ... does not exist".
        def vtok(var):
            return '({} IS "{}")'.format(gen[var], va.get(var, "UNKNOWN"))

        def etok(e):
            rtype = va.get(e.var) or self._canon_label((e.types or [None])[0])
            inner = '{} IS "{}"'.format(gen[e.var], rtype) if rtype else gen[e.var]
            return "[{}]".format(inner)

        if not plan.edges:
            return vtok(plan.vertices[0].var)

        # Render a single linear path: the start vertex once, then edge+next-vertex
        # for each edge in order (edges are assumed to chain src->dst, which is how
        # Kineviz emits one- and multi-hop patterns). Avoids the malformed
        # `(v1)(v1)` that per-edge concatenation would produce for 2+ hops.
        parts = [vtok(plan.edges[0].src_var)]
        for e in plan.edges:
            if e.direction == "in":
                parts.append("<-{}-".format(etok(e)))
            elif e.direction == "both":
                parts.append("-{}-".format(etok(e)))
            else:
                parts.append("-{}->".format(etok(e)))
            parts.append(vtok(e.dst_var))
        return "".join(parts)

    def _render_where(self, plan: MatchPlan, gen: Dict[str, str], va: Dict[str, str], params: List[Any]) -> str:
        if plan.where is None:
            return ""
        node_pk = {label: s.primary_key for label, s in self.node_schemas.items()}
        ctx = predicate.SqlCtx(gen=gen, label_of=va, node_pk=node_pk, registry=self.registry, params=params)
        return predicate.to_sql(plan.where, ctx)

    def _render_columns(self, plan: MatchPlan, gen: Dict[str, str], va: Dict[str, str]):
        columns: List[str] = []
        manifest: Dict[str, Any] = {"vertices": [], "edges": []}
        for i, v in enumerate(plan.vertices):
            label = va.get(v.var)
            if not label:
                continue
            gv = gen[v.var]
            pk = self.node_schemas[label].primary_key
            kcol = "__gx_v{}_k0".format(i)
            columns.append("{}.{} AS {}".format(gv, pk, kcol))
            prop_cols: Dict[str, str] = {}
            for j, prop in enumerate(p for p in self.node_schemas[label].properties if p != pk):
                col = "__gx_v{}_p{}".format(i, j)
                columns.append("{}.{} AS {}".format(gv, prop, col))
                prop_cols[col] = prop
            manifest["vertices"].append(
                {"var": v.var, "alias": label, "key_cols": [kcol], "prop_cols": prop_cols,
                 "pk_prop": pk}
            )
        for i, e in enumerate(plan.edges):
            rtype = va.get(e.var)
            if not rtype:
                continue
            gv = gen[e.var]
            rs = self.rel_schemas[rtype]
            prop_cols = {}
            for j, prop in enumerate(rs.properties):
                col = "__gx_e{}_p{}".format(i, j)
                columns.append("{}.{} AS {}".format(gv, prop, col))
                prop_cols[col] = prop
            # Map the schema SOURCE endpoint to whichever pattern var carries the
            # source label (and dest likewise) — NOT by pattern position, which is
            # wrong for reverse/undirected edges. This keeps rel endpoint ids equal
            # to the corresponding vertex node ids.
            sv, dv = e.src_var, e.dst_var
            if va.get(dv) == rs.src_label and va.get(sv) != rs.src_label:
                start_var, end_var = dv, sv
            else:
                start_var, end_var = sv, dv
            manifest["edges"].append(
                {
                    "var": e.var,
                    "alias": rtype,
                    "start_var": start_var,     # var bound to the schema SOURCE (rs.src_label)
                    "end_var": end_var,         # var bound to the schema DEST  (rs.dst_label)
                    "src_alias": rs.src_label,
                    "dst_alias": rs.dst_label,
                    "prop_cols": prop_cols,
                }
            )
        return columns, manifest

    def _decode_keys(self, v: VertexPat) -> List[Tuple[Any, ...]]:
        keys = []
        for tid, off in (v.id_refs or []):
            dec = self.registry.decode(tid, off)
            if dec is not None:
                keys.append(dec[1])
        return keys

    def _param(self, params: List[Any], value: Any) -> str:
        # psycopg pyformat: `%s` positional placeholders (psycopg converts these to
        # server-side `$1` bindings, which PG19 accepts inside GRAPH_TABLE WHERE).
        params.append(value)
        return "%s"

    # ----- live connection (lazy) -----

    def _connected(self) -> bool:
        return self._conn is not None

    def connect(self):
        import psycopg  # lazy: only needed for live execution
        # autocommit: a read-only proxy must never let one bad query poison the
        # connection ("current transaction is aborted, commands ignored...").
        self._conn = psycopg.connect(self.dsn, autocommit=True)
        if not self.node_schemas and not self.rel_schemas:
            self.load_schema()
        return self

    def load_schema(self) -> None:
        """Discover node/edge schemas from the live database (Information Schema)."""
        from .metadata import collect_schema
        self.node_schemas, self.rel_schemas, self.tables = collect_schema(self._conn, self.graph_name)
        self.registry.register_aliases(self.labels() + self.rel_types())

    def _run(self, sql: str, params: List[Any]) -> List[Dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def _scalar(self, sql: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute(sql)
            return int(cur.fetchone()[0])

    def _vtable(self, label: str) -> str:
        label = self._canon_label(label)
        return self.tables.get(label, label)

    def _etable(self, rtype: str) -> str:
        rtype = self._canon_label(rtype)
        return self.tables.get(rtype, rtype)
