from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from sebby.llm.cache import (
    assert_within_breakpoint_cap,
    mark_cache_breakpoint,
    mark_cache_breakpoint_on_tools,
)
from sebby.llm.providers import ProviderConfig
from sebby.retry import RetryExhaustedError, retry_with_backoff


@dataclass(frozen=True)
class UsageRecord:
    """Token usage for one completion call.

    Note: for the Anthropic provider via litellm, `input_tokens` already
    includes `cache_creation_input_tokens` and `cache_read_input_tokens`
    (litellm folds cache tokens into prompt_tokens for the Anthropic
    response shape) — don't sum them again when computing total input
    tokens or cost.
    """

    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class AllProvidersFailedError(Exception):
    pass


class CompletionFn(Protocol):
    def __call__(self, **kwargs: Any) -> Any: ...


def _default_completion_fn() -> CompletionFn:
    try:
        import litellm
    except ImportError as exc:
        raise ImportError(
            "sebby.llm's default completion backend requires the 'llm' extra: "
            "install with `uv add 'sebby[llm]'` or pass an explicit completion_fn."
        ) from exc
    return cast("CompletionFn", litellm.completion)


class LLMClient:
    """Multi-provider chat-completion client with Anthropic-first prompt
    caching and automatic failover across configured providers, tried in
    the order given.
    """

    def __init__(
        self,
        providers: list[ProviderConfig],
        *,
        completion_fn: CompletionFn | None = None,
        on_usage: Callable[[UsageRecord], None] | None = None,
        max_attempts_per_provider: int = 2,
        base_delay: float = 1.0,
        retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not providers:
            raise ValueError("at least one provider must be configured")
        self._providers = providers
        self._completion_fn = completion_fn or _default_completion_fn()
        self._on_usage = on_usage
        self._max_attempts_per_provider = max_attempts_per_provider
        self._base_delay = base_delay
        self._retryable_exceptions = retryable_exceptions
        self._sleep = sleep

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        tools = tools or []
        errors: list[Exception] = []

        for provider in self._providers:

            def _call(
                _provider: ProviderConfig = provider,
                _messages: list[dict[str, Any]] = messages,
                _tools: list[dict[str, Any]] = tools,
            ) -> Any:
                call_messages = _messages
                call_tools = _tools
                if _provider.provider == "anthropic":
                    call_messages = mark_cache_breakpoint(_messages)
                    call_tools = mark_cache_breakpoint_on_tools(_tools)
                    assert_within_breakpoint_cap(call_messages, call_tools)
                kwargs: dict[str, Any] = {}
                if _provider.effort is not None:
                    kwargs["reasoning_effort"] = _provider.effort
                return self._completion_fn(
                    model=_provider.litellm_model_string(),
                    messages=[{"role": "system", "content": system}, *call_messages],
                    tools=call_tools or None,
                    **kwargs,
                )

            try:
                response = retry_with_backoff(
                    _call,
                    max_attempts=self._max_attempts_per_provider,
                    base_delay=self._base_delay,
                    retryable_exceptions=self._retryable_exceptions,
                    sleep=self._sleep,
                )
            except RetryExhaustedError as exc:
                errors.append(exc)
                continue

            self._record_usage(provider, response)
            return response

        raise AllProvidersFailedError(
            f"all {len(self._providers)} provider(s) failed: {errors}"
        ) from errors[-1]

    def _record_usage(self, provider: ProviderConfig, response: Any) -> None:
        if self._on_usage is None:
            return
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self._on_usage(
            UsageRecord(
                provider=provider.provider,
                model=provider.model,
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            )
        )
