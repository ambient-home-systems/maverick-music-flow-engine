"""Small in-memory LRU cache with expiry, an entry limit and an approximate byte limit."""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterator
from typing import Any

# Counted for a value that cannot be serialized, so it still takes part in the byte limit.
_UNSERIALIZABLE_SIZE = 64 * 1024


def estimate_json_size(value: Any) -> int:
    """Return an approximate size in bytes: the length of the value's JSON text."""
    try:
        return len(json.dumps(value, default=str, separators=(",", ":"), ensure_ascii=False))
    except (TypeError, ValueError, RecursionError):
        return _UNSERIALIZABLE_SIZE


class BoundedCache[K: Hashable, V]:
    """Mapping that evicts expired entries, then the least recently used ones.

    - ``max_entries`` and ``max_bytes`` (optional) limit the cache. Inserting past either
      evicts from the least recently used end until it fits again.
    - ``expires_at(value)`` returns the ``time.monotonic()`` deadline of a value, or
      ``None`` for a value that never expires. It is read from the live value, so a caller
      that edits an entry in place also moves its deadline.
    - ``sizeof(value)`` returns the approximate size of a value in bytes. It is measured
      once, when the value is stored. A single value larger than ``max_bytes`` is not
      stored at all, and replaces nothing.
    - Inserting or replacing a key marks it most recently used; ``get`` and ``touch`` do
      too. ``[]``, ``in``, ``items`` and ``values`` do not.
    - Expired entries are dropped when they are read and, on insert, by a scan that runs
      at most every ``purge_interval`` seconds (always when a limit is exceeded).
    - ``on_evict(key, value, reason)`` runs for every entry the cache drops by itself
      (reason ``"expired"`` or ``"capacity"``), not for ``pop``, ``clear`` or a replaced key.
    """

    def __init__(
        self,
        *,
        max_entries: int,
        max_bytes: int | None = None,
        expires_at: Callable[[V], float | None] | None = None,
        sizeof: Callable[[V], int] | None = None,
        on_evict: Callable[[K, V, str], None] | None = None,
        purge_interval: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._expires_at = expires_at
        self._sizeof = sizeof
        self._on_evict = on_evict
        self._purge_interval = purge_interval
        self._clock = clock
        self._entries: OrderedDict[K, tuple[V, int]] = OrderedDict()
        self._bytes = 0
        self._last_purge = clock()
        self.evicted_capacity = 0
        self.evicted_expired = 0
        self.rejected_oversize = 0

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def __iter__(self) -> Iterator[K]:
        return iter(list(self._entries))

    def __getitem__(self, key: K) -> V:
        return self._entries[key][0]

    def __setitem__(self, key: K, value: V) -> None:
        size = max(0, int(self._sizeof(value))) if self._sizeof else 0
        previous = self._entries.pop(key, None)
        if previous is not None:
            self._bytes -= previous[1]
        if self.max_bytes is not None and size > self.max_bytes:
            self.rejected_oversize += 1
            return
        self._entries[key] = (value, size)
        self._bytes += size
        self._trim(force_purge=self._over_limit())

    def __delitem__(self, key: K) -> None:
        self._bytes -= self._entries.pop(key)[1]

    def get(self, key: K, default: Any = None) -> Any:
        """Return a live value and mark it most recently used; drop it if it expired."""
        entry = self._entries.get(key)
        if entry is None:
            return default
        if self._is_expired(entry[0], self._clock()):
            self._drop(key, "expired")
            return default
        self._entries.move_to_end(key)
        return entry[0]

    def touch(self, key: K) -> None:
        """Mark an existing entry most recently used."""
        if key in self._entries:
            self._entries.move_to_end(key)

    def pop(self, key: K, default: Any = None) -> Any:
        entry = self._entries.pop(key, None)
        if entry is None:
            return default
        self._bytes -= entry[1]
        return entry[0]

    def clear(self) -> None:
        self._entries.clear()
        self._bytes = 0

    def keys(self) -> list[K]:
        return list(self._entries)

    def values(self) -> list[V]:
        return [value for value, _ in self._entries.values()]

    def items(self) -> list[tuple[K, V]]:
        return [(key, value) for key, (value, _) in self._entries.items()]

    @property
    def total_bytes(self) -> int:
        return self._bytes

    def purge_expired(self) -> int:
        """Drop every expired entry and return how many were dropped."""
        if self._expires_at is None:
            return 0
        now = self._clock()
        self._last_purge = now
        expired = [key for key, (value, _) in self._entries.items() if self._is_expired(value, now)]
        for key in expired:
            self._drop(key, "expired")
        return len(expired)

    def stats(self) -> dict[str, int | None]:
        """Return sizes and eviction counters for diagnostics."""
        return {
            "entries": len(self._entries),
            "max_entries": self.max_entries,
            "bytes": self._bytes,
            "max_bytes": self.max_bytes,
            "evicted_capacity": self.evicted_capacity,
            "evicted_expired": self.evicted_expired,
            "rejected_oversize": self.rejected_oversize,
        }

    def _over_limit(self) -> bool:
        return len(self._entries) > self.max_entries or (
            self.max_bytes is not None and self._bytes > self.max_bytes
        )

    def _is_expired(self, value: V, now: float) -> bool:
        if self._expires_at is None:
            return False
        deadline = self._expires_at(value)
        return deadline is not None and deadline <= now

    def _drop(self, key: K, reason: str) -> None:
        value, size = self._entries.pop(key)
        self._bytes -= size
        if reason == "expired":
            self.evicted_expired += 1
        else:
            self.evicted_capacity += 1
        if self._on_evict is not None:
            self._on_evict(key, value, reason)

    def _trim(self, *, force_purge: bool) -> None:
        """Purge expired entries, then evict least recently used ones until within limits."""
        if force_purge or self._clock() - self._last_purge >= self._purge_interval:
            self.purge_expired()
        # The entry just stored is last and fits on its own, so it is never the one evicted.
        while self._over_limit() and len(self._entries) > 1:
            self._drop(next(iter(self._entries)), "capacity")
