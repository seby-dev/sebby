from __future__ import annotations

import pytest

from sebby.retry import RetryExhaustedError, retry_once_on_server_error, retry_with_backoff


def test_retry_with_backoff_returns_on_first_success() -> None:
    calls = []

    def fn() -> str:
        calls.append(1)
        return "ok"

    result = retry_with_backoff(fn, max_attempts=3, base_delay=0.0, sleep=lambda _: None)

    assert result == "ok"
    assert len(calls) == 1


def test_retry_with_backoff_retries_then_succeeds() -> None:
    attempts = {"n": 0}

    def fn() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ValueError("transient")
        return "ok"

    result = retry_with_backoff(
        fn,
        max_attempts=3,
        base_delay=0.0,
        retryable_exceptions=(ValueError,),
        sleep=lambda _: None,
    )

    assert result == "ok"
    assert attempts["n"] == 3


def test_retry_with_backoff_raises_retry_exhausted_after_max_attempts() -> None:
    def fn() -> str:
        raise ValueError("always fails")

    with pytest.raises(RetryExhaustedError) as exc_info:
        retry_with_backoff(
            fn,
            max_attempts=3,
            base_delay=0.0,
            retryable_exceptions=(ValueError,),
            sleep=lambda _: None,
        )

    assert isinstance(exc_info.value.__cause__, ValueError)


def test_retry_with_backoff_does_not_retry_non_retryable_exception() -> None:
    calls = []

    def fn() -> str:
        calls.append(1)
        raise KeyError("not retryable")

    with pytest.raises(KeyError):
        retry_with_backoff(
            fn,
            max_attempts=3,
            base_delay=0.0,
            retryable_exceptions=(ValueError,),
            sleep=lambda _: None,
        )

    assert len(calls) == 1


def test_retry_with_backoff_calls_on_attempt_callback() -> None:
    seen: list[int] = []

    def fn() -> str:
        if len(seen) < 2:
            raise ValueError("transient")
        return "ok"

    retry_with_backoff(
        fn,
        max_attempts=3,
        base_delay=0.0,
        retryable_exceptions=(ValueError,),
        sleep=lambda _: None,
        on_attempt=lambda n: seen.append(n),
    )

    assert seen == [1, 2, 3]


def test_retry_once_on_server_error_retries_once_then_succeeds() -> None:
    attempts = {"n": 0}

    class ServerError(Exception):
        pass

    def fn() -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ServerError("500")
        return "ok"

    result = retry_once_on_server_error(
        fn,
        is_server_error=lambda exc: isinstance(exc, ServerError),
        sleep=lambda _: None,
    )

    assert result == "ok"
    assert attempts["n"] == 2


def test_retry_once_on_server_error_does_not_retry_client_error() -> None:
    calls = []

    class ClientError(Exception):
        pass

    def fn() -> str:
        calls.append(1)
        raise ClientError("400")

    with pytest.raises(ClientError):
        retry_once_on_server_error(
            fn,
            is_server_error=lambda _exc: False,
            sleep=lambda _: None,
        )

    assert len(calls) == 1
