"""Stable, stateless node/edge ids — the Spanner-style ``ELEMENT_ID``.

Kineviz's Database Proxy connector treats a node id as an **opaque string** and
round-trips it verbatim on expand (``ELEMENT_ID(n) IN UNNEST(['<id>', ...])``).
So, unlike Kuzu's ``"<table>:<offset>"`` (which needed a per-connection registry
to reverse the offset), we can make the id a pure function of the element's
label and primary key:

    id = base64url( json([label, [key values]]) )

That is **stateless and stable**: the same node always gets the same id, with no
registry, so ids survive a restart, are identical across sessions, and decode
back to ``(label, key)`` on their own — the fix for the old id limitation.

    >>> eid = encode("Client", ("4000262298158823",))
    >>> decode(eid)
    ('Client', ('4000262298158823',))
"""

from __future__ import annotations

import base64
import json
from typing import Any, Optional, Tuple


def encode(label: str, key: Tuple[Any, ...]) -> str:
    """Encode ``(label, key tuple)`` into a stable opaque id string."""
    payload = json.dumps([label, list(key)], separators=(",", ":"), default=str)
    raw = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    return raw.rstrip("=")                      # padding is redundant; drop for tidiness


def decode(element_id: str) -> Optional[Tuple[str, Tuple[Any, ...]]]:
    """Reverse :func:`encode`. Returns ``(label, key tuple)`` or ``None`` if the
    string isn't one of our ids (so callers can tell ours apart from raw input)."""
    if not element_id or not isinstance(element_id, str):
        return None
    try:
        pad = "=" * (-len(element_id) % 4)
        payload = base64.urlsafe_b64decode(element_id + pad).decode("utf-8")
        label, key_list = json.loads(payload)
        if not isinstance(label, str) or not isinstance(key_list, list):
            return None
        return label, tuple(key_list)
    except Exception:
        return None
