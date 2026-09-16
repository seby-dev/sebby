from __future__ import annotations

import io

import pytest

from sebby.cli import run_main


def test_run_main_returns_zero_on_success_with_no_return_value():
    assert run_main(lambda: None) == 0


def test_run_main_returns_bodys_int_result():
    assert run_main(lambda: 42) == 42


def test_run_main_catches_listed_exception_prints_and_returns_one():
    stream = io.StringIO()

    def body() -> int | None:
        raise ValueError("bad input")

    result = run_main(body, catch=(ValueError,), stream=stream)

    assert result == 1
    assert "bad input" in stream.getvalue()


def test_run_main_lets_unlisted_exception_propagate():
    def body() -> int | None:
        raise KeyError("oops")

    with pytest.raises(KeyError):
        run_main(body, catch=(ValueError,), stream=io.StringIO())


def test_run_main_default_catch_is_broad_exception():
    stream = io.StringIO()

    def body() -> int | None:
        raise RuntimeError("boom")

    assert run_main(body, stream=stream) == 1
    assert "boom" in stream.getvalue()
