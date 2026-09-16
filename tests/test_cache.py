from __future__ import annotations

from sebby.cache import TTLCache


def test_get_returns_none_for_missing_key():
    cache: TTLCache[str] = TTLCache(ttl_seconds=60)
    assert cache.get("missing") is None


def test_set_then_get_returns_value_before_expiry():
    clock = {"t": 0.0}
    cache: TTLCache[str] = TTLCache(ttl_seconds=10, clock=lambda: clock["t"])
    cache.set("key", "value")
    clock["t"] = 5.0
    assert cache.get("key") == "value"


def test_get_returns_none_after_expiry():
    clock = {"t": 0.0}
    cache: TTLCache[str] = TTLCache(ttl_seconds=10, clock=lambda: clock["t"])
    cache.set("key", "value")
    clock["t"] = 10.0
    assert cache.get("key") is None


def test_expired_entry_is_evicted_from_storage():
    clock = {"t": 0.0}
    cache: TTLCache[str] = TTLCache(ttl_seconds=10, clock=lambda: clock["t"])
    cache.set("key", "value")
    clock["t"] = 10.0
    cache.get("key")
    assert len(cache) == 0


def test_clear_removes_all_entries():
    cache: TTLCache[str] = TTLCache(ttl_seconds=60)
    cache.set("a", "1")
    cache.set("b", "2")
    cache.clear()
    assert cache.get("a") is None
    assert cache.get("b") is None
