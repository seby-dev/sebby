from __future__ import annotations

import pytest

from sebby.storage import AtomicJSONStore


def test_write_then_read_roundtrips_data(tmp_path):
    store = AtomicJSONStore(tmp_path / "data.json")
    store.write({"a": 1, "b": [1, 2, 3]})
    assert store.read() == {"a": 1, "b": [1, 2, 3]}


def test_read_returns_default_when_file_missing(tmp_path):
    store = AtomicJSONStore(tmp_path / "missing.json")
    assert store.read(default={"empty": True}) == {"empty": True}


def test_read_returns_none_default_when_file_missing_and_no_default_given(tmp_path):
    store = AtomicJSONStore(tmp_path / "missing.json")
    assert store.read() is None


def test_write_creates_parent_directories(tmp_path):
    store = AtomicJSONStore(tmp_path / "nested" / "dir" / "data.json")
    store.write({"x": 1})
    assert store.read() == {"x": 1}


def test_write_does_not_corrupt_existing_file_on_serialization_error(tmp_path):
    path = tmp_path / "data.json"
    store = AtomicJSONStore(path)
    store.write({"good": "data"})

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        store.write({"bad": Unserializable()})

    assert store.read() == {"good": "data"}
