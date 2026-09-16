from __future__ import annotations

import time
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    """A trivial in-memory cache where each entry expires after a fixed TTL.

    `clock` defaults to `time.monotonic` but is injectable so tests can
    advance time deterministically instead of sleeping for real.

    This cache is unbounded (no max-size eviction — entries stay until they
    expire or are cleared) and not thread-safe.
    """

    def __init__(self, ttl_seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[str, tuple[float, T]] = {}

    def get(self, key: str) -> T | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            del self._entries[key]
            return None
        return value

    def set(self, key: str, value: T) -> None:
        self._entries[key] = (self._clock() + self._ttl, value)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
