"""Structured per-query JSONL log (doc §37) — the capture mechanism.

One line per request appended to logs/queries.jsonl. Best-effort: logging IO
never blocks or fails a request. Adds a bridge-specific `sql` field carrying the
generated SQL/PGQ so captured Kineviz Cypher and its translation live together.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_PATH = os.path.join("logs", "queries.jsonl")
_lock = threading.Lock()


def write(entry: Dict[str, Any], path: Optional[str] = None) -> None:
    target = path or _DEFAULT_PATH
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        line = json.dumps(entry, default=str, separators=(",", ":"))
        with _lock, open(target, "a") as f:
            f.write(line + "\n")
    except Exception as e:  # pragma: no cover
        logger.warning("query_log.write failed: %s", e)


def make_entry(
    request_id: str,
    db_name: str,
    query: str,
    params: Optional[dict],
    elapsed_ms: int,
    outcome_type: Optional[str] = None,
    data: Any = None,
    sql: Optional[str] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "request_id": request_id,
        "db": db_name,
        "query": query,
        "params": params or {},
        "elapsed_ms": elapsed_ms,
    }
    if sql:
        entry["sql"] = sql
    if error is not None:
        entry["status"] = 1
        entry["type"] = "ERROR"
        entry["error"] = error
        return entry
    entry["status"] = 0
    entry["type"] = outcome_type or "UNKNOWN"
    if outcome_type == "GRAPH" and isinstance(data, dict):
        entry["node_count"] = len(data.get("nodes", []) or [])
        entry["rel_count"] = len(data.get("relationships", []) or [])
    elif outcome_type == "TABLE" and isinstance(data, list):
        entry["row_count"] = max(0, len(data) - 1)
    return entry
