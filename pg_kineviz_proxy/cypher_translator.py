"""Cypher (the dialect Kineviz emits) -> MatchPlan IR.

This is intentionally a pragmatic, regex-based translator that covers the finite
surface Kineviz generates (doc §3B.2, §36), not the whole Cypher language. It
handles the MATCH/WHERE/RETURN/LIMIT shape; the special CALL/probe/count/sample
shapes are intercepted upstream in query_processor.

The IR (MatchPlan) is backend-agnostic: MockBackend runs it in memory and
Pg19Backend compiles it to GRAPH_TABLE SQL.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from . import predicate
from .backend import EdgePat, MatchPlan, VertexPat


class TranslateError(Exception):
    pass


_NODE_SPLIT = re.compile(r"(\([^()]*\))")
_LIMIT = re.compile(r"\bLIMIT\s+(\d+)\b", re.IGNORECASE)
_SKIP = re.compile(r"\bSKIP\s+(\d+)\b", re.IGNORECASE)


def translate(query: str) -> MatchPlan:
    q = query.strip().rstrip(";")
    q = q.replace("`", "")                         # Kineviz backtick-quotes identifiers

    _reject_unsupported_clauses(q)

    if not re.search(r"\bMATCH\b", q, re.IGNORECASE):
        raise TranslateError("no MATCH clause")

    limit = None
    m = _LIMIT.search(q)
    if m:
        limit = int(m.group(1))
    skip = 0
    sm = _SKIP.search(q)
    if sm:
        skip = int(sm.group(1))

    # Carve MATCH ... [WHERE ...] [RETURN ...] .
    mm = re.search(r"\bMATCH\b(?P<pat>.*?)(?=\bWHERE\b|\bRETURN\b|$)", q, re.IGNORECASE | re.DOTALL)
    pattern = mm.group("pat").strip()
    if len(_split_top(pattern, ",")) > 1:
        raise TranslateError("multiple comma-separated MATCH patterns are not supported")

    where = ""
    wm = re.search(r"\bWHERE\b(?P<w>.*?)(?=\bRETURN\b|\bLIMIT\b|$)", q, re.IGNORECASE | re.DOTALL)
    if wm:
        where = wm.group("w").strip()

    return_items: Optional[List[str]] = None
    rm = re.search(r"\bRETURN\b(?P<r>.*?)(?=\bORDER\b|\bSKIP\b|\bLIMIT\b|$)", q, re.IGNORECASE | re.DOTALL)
    if rm:
        return_items = [it.strip() for it in _split_top(rm.group("r"), ",") if it.strip()]

    order: List[Tuple[str, str]] = []
    om = re.search(r"\bORDER\s+BY\b(?P<o>.*?)(?=\bSKIP\b|\bLIMIT\b|$)", q, re.IGNORECASE | re.DOTALL)
    if om:
        for term in _split_top(om.group("o"), ","):
            term = term.strip().replace("`", "")
            if not term:
                continue
            direction = "DESC" if re.search(r"\bDESC\b", term, re.IGNORECASE) else "ASC"
            expr = re.sub(r"\s+(ASC|DESC)\s*$", "", term, flags=re.IGNORECASE).strip()
            order.append((expr, direction))

    vertices, edges = _parse_pattern(pattern)
    if not vertices:
        raise TranslateError("could not parse any node from pattern: %r" % pattern)

    plan = MatchPlan(vertices=vertices, edges=edges, order=order, limit=limit, skip=skip,
                     return_items=return_items)
    if where:
        plan.where = predicate.parse(where)
        if plan.where is None:                     # couldn't parse — don't silently drop it
            raise TranslateError("unsupported WHERE expression: {!r}".format(where))
        # positive top-level `id(n) IN [internal_id(...)]` gives an authoritative
        # label hint for the pattern variable (used by pg label resolution).
        for var, refs in predicate.id_hints(plan.where):
            vp = plan.vertex(var)
            if vp is not None and refs:
                vp.id_refs = refs
    return plan


# Clauses we don't translate — reject loudly instead of returning wrong results.
_UNSUPPORTED_CLAUSES = [
    (r"\bOPTIONAL\s+MATCH\b", "OPTIONAL MATCH is not supported"),
    (r"\bUNWIND\b", "UNWIND is not supported"),
    (r"\bHAVING\b", "HAVING (filter on aggregates) is not supported"),
    (r"\bCALL\s*\{", "subqueries (CALL { }) are not supported"),
    (r"\bWITH\b", "WITH pipelines are not supported"),
]


def _reject_unsupported_clauses(q: str) -> None:
    # Strip STARTS/ENDS WITH so the WITH check doesn't false-positive on them.
    probe = re.sub(r"\b(STARTS|ENDS)\s+WITH\b", "", q, flags=re.IGNORECASE)
    for pat, msg in _UNSUPPORTED_CLAUSES:
        if re.search(pat, probe, re.IGNORECASE):
            raise TranslateError(msg)


def _parse_pattern(pattern: str) -> Tuple[List[VertexPat], List[EdgePat]]:
    parts = _NODE_SPLIT.split(pattern)
    vertices: List[VertexPat] = []
    edges: List[EdgePat] = []
    node_vars: List[str] = []

    node_idx = 0
    # parts alternate: [connector, node, connector, node, ...]
    node_positions = [i for i in range(len(parts)) if i % 2 == 1]
    for i in node_positions:
        var, label = _parse_node(parts[i], node_idx)
        vertices.append(VertexPat(var=var, label=label))
        node_vars.append(var)
        node_idx += 1

    # connectors sit between consecutive nodes: parts index 2, 4, ...
    for e_idx, ci in enumerate(range(2, len(parts), 2)):
        conn = parts[ci]
        if ci - 1 < 0 or ci + 1 >= len(parts):
            continue
        left = node_vars[e_idx]
        right = node_vars[e_idx + 1]
        edge = _parse_edge(conn, e_idx, left, right)
        if edge is not None:
            edges.append(edge)
    return vertices, edges


def _parse_node(token: str, idx: int) -> Tuple[str, Optional[str]]:
    inner = token.strip()[1:-1].strip()          # strip ( )
    if not inner:
        return "__n{}".format(idx), None
    m = re.match(r"^([A-Za-z_]\w*)?\s*(?::\s*([A-Za-z_]\w*))?", inner)
    var = m.group(1) or "__n{}".format(idx)
    label = m.group(2)
    return var, label


def _parse_edge(conn: str, idx: int, left: str, right: str) -> Optional[EdgePat]:
    c = conn.strip()
    if not c or c in (",",):
        return None
    inbound = c.startswith("<")
    outbound = c.endswith(">")
    if inbound and not outbound:
        direction = "in"
    elif outbound and not inbound:
        direction = "out"
    else:
        direction = "both"

    var = "__e{}".format(idx)
    types: Optional[List[str]] = None
    bm = re.search(r"\[([^\]]*)\]", c)
    if bm:
        body = bm.group(1).strip()
        # Variable-length paths ([:T*], [:T*1..3]) can't be pushed to GRAPH_TABLE —
        # PostgreSQL 19 has no path quantifier ("element pattern quantifier is not
        # supported"). Reject clearly instead of emitting a broken label.
        if re.search(r"\*|\+|\.\.|\{", body):
            raise TranslateError(
                "variable-length paths are not supported (PostgreSQL 19 GRAPH_TABLE "
                "has no path quantifier)")
        vm = re.match(r"^([A-Za-z_]\w*)?\s*(?::\s*(.+))?$", body)
        if vm:
            if vm.group(1):
                var = vm.group(1)
            if vm.group(2):
                # dedup, preserve order — Kineviz sometimes repeats types (A|A|A|B)
                types = list(dict.fromkeys(t.strip() for t in vm.group(2).split("|") if t.strip()))
    return EdgePat(var=var, types=types, direction=direction, src_var=left, dst_var=right)


def _split_top(s: str, sep: str) -> List[str]:
    """Split on sep at bracket depth 0. sep is either ',' or the word 'AND'."""
    out: List[str] = []
    buf: List[str] = []
    depth = 0
    i = 0
    word = sep.upper() == "AND"
    while i < len(s):
        ch = s[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if depth == 0:
            if not word and ch == sep:
                out.append("".join(buf)); buf = []; i += 1; continue
            if word and s[i:i + 3].upper() == "AND" and _boundary(s, i, i + 3):
                out.append("".join(buf)); buf = []; i += 3; continue
        buf.append(ch)
        i += 1
    if buf:
        out.append("".join(buf))
    return out


def _boundary(s: str, start: int, end: int) -> bool:
    before = s[start - 1] if start > 0 else " "
    after = s[end] if end < len(s) else " "
    return not before.isalnum() and not after.isalnum()
