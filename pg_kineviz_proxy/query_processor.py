"""Validate, intercept, dispatch — the one component that knows Cypher behaviour.

Mirrors the reference proxies' query_processor: block writes, auto-LIMIT, serve
the special CALL/probe/count/sample shapes from the backend, and hand real
MATCH patterns to the translator + backend.
"""

from __future__ import annotations

import itertools
import logging
import re
from typing import Any, List, Optional

from .backend import Backend, EdgePat, MatchPlan, QueryOutcome
from .cypher_translator import TranslateError, translate

logger = logging.getLogger(__name__)

MAX_QUERY_RESULTS = 20000
# Kineviz inlines every selected node id into expand queries (`id(a) IN
# [internal_id(t,o), …]`), so a large selection produces a very long but
# legitimate query. Keep a generous cap (~large selections) rather than the
# reference proxies' 10k, which rejected real expands.
MAX_QUERY_LENGTH = 2_000_000
KUZU_VERSION = "0.11.2"

WRITE_KEYWORDS = ("create ", "merge ", "set ", "delete ", "remove ", "drop ")
DANGEROUS = ("drop database", "delete database", "shutdown", "kill ", "terminate ")


class QueryProcessor:
    def __init__(self, backend: Backend, db_name: str) -> None:
        self.backend = backend
        self.db_name = db_name
        self.last_sql: Optional[str] = None      # populated when a pg backend compiled SQL

    def summary(self) -> dict:
        return {"version": KUZU_VERSION, "storageVersion": KUZU_VERSION}

    # ----- validation -----

    def validate(self, query: str):
        if not query or not query.strip():
            return False, "Query is empty"
        if len(query) > MAX_QUERY_LENGTH:
            return False, "Query is too long (max {} characters)".format(MAX_QUERY_LENGTH)
        q = query.lower()
        for p in DANGEROUS:
            if p in q:
                return False, "Query contains forbidden pattern: {}".format(p.strip())
        if _is_special(q):
            return True, "ok"
        for kw in WRITE_KEYWORDS:
            if re.search(r"\b" + kw.strip() + r"\b", q):
                return False, "writes via Cypher are not supported (matched '{}')".format(kw.strip())
        return True, "ok"

    # ----- dispatch -----

    def execute(self, query: str) -> QueryOutcome:
        self.last_sql = None
        ql = query.lower().strip()

        if "call schema" in ql:
            return QueryOutcome("SCHEMA", self.backend.schema_response(self.db_name), self.summary())
        if "show tables" in ql or "show_tables" in ql or "show databases" in ql:
            # Kineviz/Kuzu sends `CALL show_tables() RETURN *`.
            return QueryOutcome("TABLE", self._show_tables(), self.summary())
        if "call test" in ql:
            # Never mutate a real graph from a probe (doc §3A.7).
            return QueryOutcome("TABLE", [["status"], ["test() is a no-op in the PostgreSQL bridge"]], self.summary())

        probe = self._probe(query)
        if probe is not None:
            return probe
        count = self._count(query)
        if count is not None:
            return count
        sample = self._sample(query)
        if sample is not None:
            return sample

        prepared = self._auto_limit(query.strip().rstrip(";"))
        try:
            plan = translate(prepared)
        except TranslateError as e:
            return QueryOutcome("TABLE", [["error"], ["unsupported query: {}".format(e)]], self.summary())

        # Untyped `[r]` / alternation `[r:A|B]` fan out into one typed branch per
        # relationship type (GRAPH_TABLE needs a concrete label). Run each and
        # merge — this is how `MATCH (n)-[r]->(m)` works end to end.
        branches = _expand_branches(plan, self.backend.rel_types())

        kind, payload = _parse_return(plan.return_items)

        if kind == "unsupported":
            return QueryOutcome(
                "TABLE", [["error"], ["unsupported RETURN item: {} (expressions/functions "
                                     "beyond aggregates and var.prop are not supported)".format(payload)]],
                self.summary())

        # Grouped aggregation (e.g. `RETURN n.name, count(t), sum(t.amount)`) →
        # SQL GROUP BY over GRAPH_TABLE. The backend resolves edge types itself.
        if kind == "agg":
            header, rows = self.backend.aggregate(plan, payload["group_keys"], payload["aggs"])
            self.last_sql = getattr(self.backend, "last_sql", None)
            return QueryOutcome("TABLE", [header] + rows, self.summary())

        # Scalar RETURN (e.g. `RETURN c1.name`) → TABLE, not a reconstructed graph.
        if kind == "scalar":
            header, rows, errors = None, [], []
            for bp in branches:
                try:
                    header, rrows = self.backend.project(bp, payload["cols"], distinct=payload["distinct"])
                    rows.extend(rrows)
                except Exception as e:
                    logger.warning("projection failed: %s", e)
                    errors.append(str(e))
            # A genuine error (e.g. an unknown property) with no rows should be
            # surfaced, not returned as a misleading empty table.
            if not rows and errors:
                raise RuntimeError(errors[0])
            if header is None:
                header = [a for (_v, _p, a) in payload["cols"]]
            if payload["distinct"]:
                seen, ded = set(), []
                for r in rows:
                    k = tuple(r)
                    if k not in seen:
                        seen.add(k); ded.append(r)
                rows = ded
            self.last_sql = getattr(self.backend, "last_sql", None)
            return QueryOutcome("TABLE", [header] + rows, self.summary())

        compile_fn = getattr(self.backend, "compile", None)

        merged_nodes: List[Any] = []
        merged_rels: List[Any] = []
        sqls: List[str] = []
        for bp in branches:
            if callable(compile_fn):
                try:
                    sqls.append(compile_fn(bp).sql)
                except Exception as e:
                    logger.debug("compile for logging failed: %s", e)
            try:
                result = self.backend.execute(bp)
            except Exception as e:
                # Some branches are naturally unsatisfiable (e.g. a Company on a
                # KNOWS endpoint) or unsupported — skip them, keep the session healthy.
                logger.warning("branch execute failed: %s", e)
                continue
            merged_nodes.extend(result.nodes)
            merged_rels.extend(result.relationships)

        self.last_sql = "\n---\n".join(sqls) if sqls else None
        payload = {
            "nodes": [n.to_dict() for n in merged_nodes],
            "relationships": [r.to_dict() for r in merged_rels],
        }
        return QueryOutcome("GRAPH", payload, self.summary())

    # ----- intercepts -----

    def _show_tables(self):
        rows = [["name", "type"]]
        for label in self.backend.labels():
            rows.append([label, "NODE"])
        for rtype in self.backend.rel_types():
            rows.append([rtype, "REL"])
        return rows

    def _probe(self, query: str) -> Optional[QueryOutcome]:
        s = query.strip().rstrip(";")
        if re.search(r"\b(MATCH|UNWIND|CALL|SHOW)\b", s, re.IGNORECASE):
            return None
        m = re.match(r"^\s*RETURN\s+(.+)$", s, re.IGNORECASE | re.DOTALL)
        if not m:
            return None
        m2 = re.match(r"""^["']([^"']*)["']\s+AS\s+(\w+)\s*$""", m.group(1).strip(), re.IGNORECASE)
        if m2:
            return QueryOutcome("TABLE", [[m2.group(2)], [m2.group(1)]], self.summary())
        return QueryOutcome("TABLE", [["value"], [m.group(1).strip()]], self.summary())

    def _count(self, query: str) -> Optional[QueryOutcome]:
        # Kineviz sends `RETURN count(n) AS count` — allow an optional AS alias,
        # `count(*)`, and backtick-quoted labels.
        s = query.strip().rstrip(";")
        tail = r"(?:\s+AS\s+\w+)?\s*(?:LIMIT\s+\d+)?\s*$"
        # typed single vertex: MATCH (c:Label) RETURN count(c|*)
        m = re.match(
            r"^\s*MATCH\s*\(\s*([A-Za-z_]\w*)\s*:\s*`?([A-Za-z_]\w*)`?\s*\)\s*"
            r"RETURN\s+count\(\s*(?:\1|\*)\s*\)" + tail, s, re.IGNORECASE)
        if m:
            alias = _return_alias(s) or "count({})".format(m.group(1))
            return QueryOutcome("TABLE", [[alias], [self.backend.node_count(m.group(2))]], self.summary())
        # untyped: MATCH (n) RETURN count(n|*)
        m = re.match(r"^\s*MATCH\s*\(\s*([A-Za-z_]\w*)\s*\)\s*RETURN\s+count\(\s*(?:\1|\*)\s*\)" + tail,
                     s, re.IGNORECASE)
        if m:
            alias = _return_alias(s) or "count(n)"
            return QueryOutcome("TABLE", [[alias], [self.backend.node_count()]], self.summary())
        m = re.match(
            r"^\s*MATCH\s*\([^)]*\)\s*-\s*\[\s*([A-Za-z_]\w*)\s*(?::\s*([A-Za-z_]\w*))?\s*\]\s*(->|-)\s*\([^)]*\)\s*"
            r"RETURN\s+count\(\s*\1\s*\)" + tail,
            s, re.IGNORECASE)
        if m:
            rel_type = m.group(2)
            total = self.backend.rel_count(rel_type)
            if m.group(3) == "-":
                total *= 2
            alias = _return_alias(s) or "count({})".format(m.group(1))
            return QueryOutcome("TABLE", [[alias], [total]], self.summary())
        return None

    def _sample(self, query: str) -> Optional[QueryOutcome]:
        s = query.strip().rstrip(";")
        m = re.match(r"^\s*MATCH\s*\(\s*([A-Za-z_]\w*)\s*\)\s*RETURN\s+(\*|\1)\s*(?:LIMIT\s+(\d+))?\s*$",
                     s, re.IGNORECASE)
        if not m:
            return None
        limit = int(m.group(3)) if m.group(3) else MAX_QUERY_RESULTS
        return QueryOutcome("GRAPH", self.backend.sample(limit).to_payload(), self.summary())

    def _auto_limit(self, query: str) -> str:
        ql = query.lower()
        if "return" in ql and "limit" not in ql:
            return "{} LIMIT {}".format(query, MAX_QUERY_RESULTS)
        return query


def _expand_branches(plan: MatchPlan, all_rel_types: List[str]) -> List[MatchPlan]:
    """Fan an untyped `[r]` / alternation `[r:A|B]` edge into one plan per concrete
    rel type. GRAPH_TABLE requires a concrete label; the results are merged upstream.
    Plans with every edge already single-typed are returned unchanged.
    """
    if not plan.edges:
        return [plan]
    opts: List[List[str]] = []
    needs = False
    for e in plan.edges:
        if e.types and len(e.types) == 1:
            opts.append(list(e.types))
        elif e.types:                      # alternation
            opts.append(list(e.types)); needs = True
        else:                              # untyped
            opts.append(list(all_rel_types)); needs = True
    if not needs:
        return [plan]
    branches: List[MatchPlan] = []
    for combo in itertools.product(*opts):
        edges = [EdgePat(var=e.var, types=[t], direction=e.direction,
                         src_var=e.src_var, dst_var=e.dst_var)
                 for e, t in zip(plan.edges, combo)]
        branches.append(MatchPlan(
            vertices=plan.vertices, edges=edges, where=plan.where, order=plan.order,
            limit=plan.limit, skip=plan.skip, return_items=plan.return_items))
    return branches


_PROJ_ITEM = re.compile(r"^([A-Za-z_]\w*)\.([A-Za-z_]\w*)(?:\s+AS\s+(\w+))?$", re.IGNORECASE)
_AGG_ITEM = re.compile(
    r"^(count|sum|avg|min|max)\(\s*(DISTINCT\s+)?(\*|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s*\)"
    r"(?:\s+AS\s+(\w+))?$",
    re.IGNORECASE)


def _parse_return(return_items: Optional[List[str]]):
    """Classify the RETURN clause.

    Returns ("graph", None) | ("scalar", {distinct, cols}) | ("agg", {group_keys, aggs}).
      * all `var.prop` (+ optional DISTINCT) → scalar projection (TABLE)
      * any count/sum/avg/min/max(...) present → grouped aggregation (TABLE),
        with the non-aggregate `var.prop` items as GROUP BY keys
      * a bare var / `*` / anything unrecognized → graph
    """
    if not return_items:
        return "graph", None
    distinct = False
    group_keys, aggs = [], []
    for i, raw in enumerate(return_items):
        s = raw.strip().replace("`", "")
        if i == 0:
            m = re.match(r"^DISTINCT\s+(.*)$", s, re.IGNORECASE)
            if m:
                distinct = True
                s = m.group(1).strip()
        am = _AGG_ITEM.match(s)
        if am:
            fn, distinct, arg, alias = am.group(1).lower(), am.group(2) is not None, am.group(3), am.group(4)
            alias = alias or "{}_{}".format(fn, arg.replace(".", "_").replace("*", "all"))
            if arg == "*":
                aggs.append((fn, None, None, alias, distinct))
            elif "." in arg:
                v, p = arg.split(".", 1)
                aggs.append((fn, v, p, alias, distinct))
            else:
                aggs.append((fn, arg, None, alias, distinct))
            continue
        pm = _PROJ_ITEM.match(s)
        if pm:
            group_keys.append((pm.group(1), pm.group(2), pm.group(3) or "{}.{}".format(pm.group(1), pm.group(2))))
            continue
        # A bare variable or `*` means a graph return; anything else (a function
        # call, arithmetic, etc.) is a projection we can't translate — flag it so
        # it fails loudly rather than silently returning the graph.
        if re.match(r"^(\*|[A-Za-z_]\w*)$", s):
            return "graph", None
        return "unsupported", s
    if aggs:
        return "agg", {"group_keys": group_keys, "aggs": aggs}
    return "scalar", {"distinct": distinct, "cols": group_keys}


def _return_alias(query: str) -> Optional[str]:
    """Extract the alias from `... count(x) AS <alias> ...` so the TABLE header matches."""
    m = re.search(r"count\([^)]*\)\s+AS\s+(\w+)", query, re.IGNORECASE)
    return m.group(1) if m else None


def _is_special(q_lower: str) -> bool:
    return any(s in q_lower for s in ("call schema", "call test", "show tables", "show_tables", "show databases"))
