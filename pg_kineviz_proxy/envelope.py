"""Build the exact JSON envelope Kineviz expects (doc §3A.3, §3A.7).

Shared by the HTTP server and the simulator so both emit byte-identical shapes.
Schema is single-wrapped; GRAPH/TABLE are double-wrapped.
"""

from __future__ import annotations

from typing import Any, Dict

from .backend import QueryOutcome


def success(outcome: QueryOutcome) -> Dict[str, Any]:
    if outcome.type == "SCHEMA":
        # Schema sits directly under top-level data (no inner wrap).
        return {"data": outcome.data, "status": 0, "message": "Successful"}
    inner: Dict[str, Any] = {"data": outcome.data, "type": outcome.type, "summary": outcome.summary}
    # Kineviz's table renderer expects `numRows` in the inner payload (data[0] is
    # the header, data[1..] are value rows) — see the use-kineviz skill's documented
    # `_content` shape. The Kuzu reference server omits it; GRAPH does not carry it.
    if outcome.type == "TABLE" and isinstance(outcome.data, list):
        inner["numRows"] = max(0, len(outcome.data) - 1)
    return {"data": inner, "status": 0, "message": "Successful"}


def error(message: str) -> Dict[str, Any]:
    return {"data": None, "status": 1, "message": message}
