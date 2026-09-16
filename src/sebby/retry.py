from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class RetryExhaustedError(Exception):
    """Raised when all retry attempts of `retry_with_backoff` are exhausted."""


def retry_with_backoff(
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
    sleep: Callable[[float], None] = time.sleep,
    on_attempt: Callable[[int], None] | None = None,
) -> T:
    """Call `fn` up to `max_attempts` times with exponential backoff.

    Only exceptions matching `retryable_exceptions` are retried; anything
    else propagates immediately. Raises `RetryExhaustedError` (chained to
    the last exception) if every attempt fails.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        if on_attempt is not None:
            on_attempt(attempt)
        try:
            return fn()
        except retryable_exceptions as exc:
            last_exc = exc
            if attempt == max_attempts:
                break
            sleep(base_delay * (2 ** (attempt - 1)))
    raise RetryExhaustedError(f"failed after {max_attempts} attempts") from last_exc


def retry_once_on_server_error(
    fn: Callable[[], T],
    *,
    is_server_error: Callable[[Exception], bool],
    sleep: Callable[[float], None] = time.sleep,
    delay: float = 1.0,
) -> T:
    """Call `fn`; on an exception where `is_server_error(exc)` is true, wait
    `delay` seconds and try exactly once more. Any other exception, or a
    second failure, propagates immediately.
    """
    try:
        return fn()
    except Exception as exc:
        if not is_server_error(exc):
            raise
        sleep(delay)
        return fn()
