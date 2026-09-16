from __future__ import annotations

import pytest

from sebby.http.files import write_temp_file


async def test_write_temp_file_writes_data_and_returns_readable_path():
    path = await write_temp_file(b"hello world", suffix=".bin")
    try:
        assert path.read_bytes() == b"hello world"
        assert path.suffix == ".bin"
    finally:
        path.unlink()


async def test_write_temp_file_propagates_mkstemp_failure(monkeypatch):
    def _boom(*args: object, **kwargs: object) -> tuple[int, str]:
        raise OSError("no space left on device")

    monkeypatch.setattr("tempfile.mkstemp", _boom)

    with pytest.raises(OSError):
        await write_temp_file(b"data")
