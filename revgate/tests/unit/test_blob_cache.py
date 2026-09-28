from __future__ import annotations

from pathlib import Path

import pytest

from revgate.spi.cache import FACTS_VERSION, BlobCache

SHA = "0123456789abcdef0123456789abcdef01234567"


def test_put_then_get_round_trips(tmp_path: Path) -> None:
    cache = BlobCache(tmp_path, "0.1.0:1")
    value = {"a": (1, 2), "b": "text"}
    cache.put(SHA, "py", value)
    assert cache.get(SHA, "py") == value
    assert (tmp_path / SHA[:2]).is_dir()


def test_missing_entry_is_a_miss(tmp_path: Path) -> None:
    assert BlobCache(tmp_path, "s").get(SHA, "py") is None


def test_another_salt_or_kind_misses(tmp_path: Path) -> None:
    BlobCache(tmp_path, "one").put(SHA, "py", [1])
    assert BlobCache(tmp_path, "two").get(SHA, "py") is None
    assert BlobCache(tmp_path, "one").get(SHA, "ts") is None


def test_truncated_entry_is_a_miss(tmp_path: Path) -> None:
    cache = BlobCache(tmp_path, "s")
    cache.put(SHA, "py", list(range(1000)))
    [entry] = list(tmp_path.rglob("*.pkl"))
    entry.write_bytes(entry.read_bytes()[:10])
    assert cache.get(SHA, "py") is None
    entry.write_bytes(b"")
    assert cache.get(SHA, "py") is None


def test_put_overwrites_and_leaves_no_temporary_files(tmp_path: Path) -> None:
    cache = BlobCache(tmp_path, "s")
    cache.put(SHA, "py", 1)
    cache.put(SHA, "py", 2)
    assert cache.get(SHA, "py") == 2
    assert [p.suffix for p in tmp_path.rglob("*") if p.is_file()] == [".pkl"]


def test_unsafe_key_is_refused(tmp_path: Path) -> None:
    cache = BlobCache(tmp_path, "s")
    with pytest.raises(ValueError):
        cache.put("../../etc", "py", 1)
    with pytest.raises(ValueError):
        cache.get(SHA, "p/y")


def test_facts_version_is_a_string() -> None:
    assert isinstance(FACTS_VERSION, str) and FACTS_VERSION
