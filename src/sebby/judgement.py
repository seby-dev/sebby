"""Construction and usage-tracking helpers for TypeSafe's Python SDK
(https://docs.typesafe.ai) — typed, calibrated judgments (Choice/Score/Noul
questions evaluated against a state) in place of generating free text and
parsing it back into a structural decision.

The SDK (`typesafe_sdk`, the `judgement` extra) already provides typed
questions/answers, sensible default retries, and typed errors — this module
only wires up construction the way `sebby.config` expects (an explicit
`api_key`, since `Settings` never reads ambient env vars — see its
docstring) and reports usage through the same `UsageRecord` callback
`sebby.llm.LLMClient` uses, so callers track LLM and TypeSafe spend through
one mechanism. Import `Choice`, `Noul`, `Score`, `TypeSafeAPIError`, etc.
directly from `typesafe_sdk`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sebby.llm.client import UsageRecord


def _default_client_cls() -> type:
    try:
        from typesafe_sdk import TypeSafeClient
    except ImportError as exc:
        raise ImportError(
            "sebby.judgement's default client requires the 'judgement' extra: "
            "install with `uv add 'sebby[judgement]'`, or pass an explicit client_cls."
        ) from exc
    return TypeSafeClient


def make_client(
    *,
    api_key: str,
    model: str = "jev-latest",
    client_cls: type | None = None,
    **kwargs: Any,
) -> Any:
    """Build a typesafe_sdk.TypeSafeClient with an explicit api_key.

    `client_cls` defaults to `typesafe_sdk.TypeSafeClient`, imported lazily
    so `sebby.judgement` doesn't require the `judgement` extra just to be
    imported — pass an explicit `client_cls` in tests to avoid needing the
    real SDK installed. Extra keyword arguments (e.g. `retry=RetryPolicy(...)`)
    pass straight through to the client's constructor.
    """
    cls = client_cls or _default_client_cls()
    return cls(api_key=api_key, model=model, **kwargs)


def record_usage(
    response: Any,
    *,
    model: str,
    on_usage: Callable[[UsageRecord], None] | None,
) -> None:
    """Report a `typesafe_sdk.SystemOneResponse`'s usage through `on_usage`,
    using the same `UsageRecord` shape `sebby.llm.LLMClient` reports through
    (`provider="typesafe"`, cache fields left at 0 — TypeSafe has no prompt
    caching). A no-op when `on_usage` is None.
    """
    if on_usage is None:
        return
    on_usage(
        UsageRecord(
            provider="typesafe",
            model=model,
            input_tokens=response.usage.input_tokens or 0,
            output_tokens=response.usage.output_tokens or 0,
        )
    )
