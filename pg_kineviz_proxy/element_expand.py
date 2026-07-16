"""Rewrite the Database Proxy's expand predicate into the form our engine handles.

Kineviz's Database Proxy connector echoes selected node ids back on expand as:

    MATCH (n)-[r]-(m) FILTER WHERE ELEMENT_ID(n) IN UNNEST(['<id>', '<id>'])
      AND NOT(ELEMENT_ID(r) IN UNNEST(['<edge id>']))       -- exclude already-drawn edges

We handle two things:

1. **Node membership** ``ELEMENT_ID(n) IN UNNEST([...])`` — our `element_id` strings
   are self-describing, so we decode each to ``(label, key)`` and re-express the
   predicate as the Kuzu-style ``id(n) IN [internal_id(t,o), ...]`` that
   `cypher_translator`/`pg_backend` already resolve (label inference + key
   filter). The ``(t,o)`` come from an ephemeral per-request registry populated
   from the decoded ids — no persistent state.

2. **Edge exclusion** ``NOT(ELEMENT_ID(r) IN UNNEST([...]))`` — those ids decode to
   a *relationship type* (e.g. ``PERFORMS``), not a node label. Translating an
   edge-id filter to SQL/PGQ is complex and only an optimization (Kineviz dedupes
   edges client-side), so we **strip** the clause and return all matching edges.

We also normalize GQL's ``FILTER WHERE`` to Cypher's ``WHERE``.
"""

from __future__ import annotations

import re
from typing import FrozenSet

from . import element_id
from .identity import IdentityRegistry

# ELEMENT_ID(var) IN UNNEST([ ... ])  or  ELEMENT_ID(var) IN [ ... ]
_ELEMENT_ID_IN = re.compile(
    r"ELEMENT_ID\s*\(\s*(\w+)\s*\)\s+IN\s+(UNNEST\s*\(\s*)?\[(?P<ids>[^\]]*)\]\s*\)?",
    re.IGNORECASE | re.DOTALL,
)
# [AND] NOT( ELEMENT_ID(var) IN [UNNEST(] [ ... ] [)] )
_NOT_ELEMENT_ID = re.compile(
    r"(?:AND\s+)?NOT\s*\(\s*ELEMENT_ID\s*\(\s*\w+\s*\)\s+IN\s+"
    r"(?:UNNEST\s*\(\s*)?\[(?P<ids>[^\]]*)\]\s*\)?\s*\)",
    re.IGNORECASE | re.DOTALL,
)
_STR_LIT = re.compile(r"""['"]([^'"]*)['"]""")


def rewrite(query: str, registry: IdentityRegistry,
            rel_types: FrozenSet[str] = frozenset()) -> str:
    q = re.sub(r"\bFILTER\s+WHERE\b", "WHERE", query, flags=re.IGNORECASE)

    # 1. Drop edge-id exclusions (ids that decode to a relationship type).
    def _strip_edge_excl(m: "re.Match") -> str:
        for eid in _STR_LIT.findall(m.group("ids")):
            dec = element_id.decode(eid)
            if dec and dec[0] in rel_types:
                return ""                      # edge filter — remove the whole clause
        return m.group(0)                      # a node exclusion — leave it for step 2

    q = _NOT_ELEMENT_ID.sub(_strip_edge_excl, q)

    # 2. Rewrite node-id membership to id(n) IN [internal_id(t,o), ...].
    def _repl(m: "re.Match") -> str:
        var = m.group(1)
        internal = []
        for eid in _STR_LIT.findall(m.group("ids")):
            dec = element_id.decode(eid)
            if dec is None or dec[0] in rel_types:
                continue                       # skip non-ids / stray edge ids
            label, key = dec
            t, o = registry.node_id(label, key).split(":")
            internal.append("internal_id({}, {})".format(t, o))
        if not internal:
            return m.group(0)
        return "id({}) IN [{}]".format(var, ", ".join(internal))

    q = _ELEMENT_ID_IN.sub(_repl, q)
    # tidy a possible dangling connective left by a stripped clause
    q = re.sub(r"\bWHERE\s+AND\b", "WHERE", q, flags=re.IGNORECASE)
    q = re.sub(r"\bAND\s+AND\b", "AND", q, flags=re.IGNORECASE)
    return q
