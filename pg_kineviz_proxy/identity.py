"""Identity registry: the fix for "PostgreSQL has no node id" (doc §3C).

Kineviz round-trips whatever id string the backend hands it, re-expressing it as
`internal_id(<tableId>, <offset>)` on expand. Kuzu's id happens to be a physical
`{table, offset}`; we are free to put *any* two integers there as long as we can
reverse them. This registry mints stable `"<tableId>:<offset>"` ids per graph
element and decodes them back to (element alias, key tuple).

Keys may be composite, so a key is always stored as a tuple.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

KeyTuple = Tuple[Any, ...]


class IdentityRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.table_index: Dict[str, int] = {}          # alias -> tableId
        self._alias_by_table: Dict[int, str] = {}       # tableId -> alias
        self._next_offset: Dict[str, int] = {}          # alias -> next free offset
        self._key_to_offset: Dict[str, Dict[KeyTuple, int]] = {}
        self._offset_to_key: Dict[str, Dict[int, KeyTuple]] = {}

    def register_aliases(self, aliases: List[str]) -> None:
        """Assign a deterministic integer table id per element alias, in order."""
        with self._lock:
            for alias in aliases:
                if alias in self.table_index:
                    continue
                tid = len(self.table_index)
                self.table_index[alias] = tid
                self._alias_by_table[tid] = alias
                self._next_offset[alias] = 0
                self._key_to_offset[alias] = {}
                self._offset_to_key[alias] = {}

    def node_id(self, alias: str, key: KeyTuple) -> str:
        """Return the stable `"<tableId>:<offset>"` id for (alias, key), minting one if new."""
        key = tuple(key)
        with self._lock:
            if alias not in self.table_index:
                self.register_aliases([alias])
            offsets = self._key_to_offset[alias]
            off = offsets.get(key)
            if off is None:
                off = self._next_offset[alias]
                self._next_offset[alias] = off + 1
                offsets[key] = off
                self._offset_to_key[alias][off] = key
            return "{}:{}".format(self.table_index[alias], off)

    def decode(self, table_id: int, offset: int) -> Optional[Tuple[str, KeyTuple]]:
        """Reverse `internal_id(table_id, offset)` → (alias, key tuple). None if unknown."""
        with self._lock:
            alias = self._alias_by_table.get(table_id)
            if alias is None:
                return None
            key = self._offset_to_key.get(alias, {}).get(offset)
            if key is None:
                return None
            return alias, key

    def alias_for_table(self, table_id: int) -> Optional[str]:
        with self._lock:
            return self._alias_by_table.get(table_id)
