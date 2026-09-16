from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TextIO


def run_main(
    body: Callable[[], int | None],
    *,
    catch: tuple[type[Exception], ...] = (Exception,),
    stream: TextIO = sys.stderr,
) -> int:
    """Run `body`, catching exceptions in `catch`, printing them to `stream`.

    Returns `body`'s result (0 if it returns None) on success, or 1 if a
    caught exception was raised. Use as `sys.exit(run_main(main))` at a
    script's entry point — each stage should raise a domain-specific
    exception rather than calling `sys.exit` itself, so `run_main` is the
    single place that turns an error into a process exit code.
    """
    try:
        result = body()
    except catch as exc:
        print(f"error: {exc}", file=stream)
        return 1
    return result if result is not None else 0
