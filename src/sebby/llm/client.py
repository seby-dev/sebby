from __future__ import annotations

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
    import litellm

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
    ) -> None:
        if not providers:
            raise ValueError("at least one provider must be configured")
        self._providers = providers
        self._completion_fn = completion_fn or _default_completion_fn()
        self._on_usage = on_usage
        self._max_attempts_per_provider = max_attempts_per_provider

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
            call_messages = messages
            call_tools = tools
            if provider.provider == "anthropic":
                call_messages = mark_cache_breakpoint(messages)
                call_tools = mark_cache_breakpoint_on_tools(tools)
                assert_within_breakpoint_cap(call_messages, call_tools)

            def _call(
                _provider: ProviderConfig = provider,
                _messages: list[dict[str, Any]] = call_messages,
                _tools: list[dict[str, Any]] = call_tools,
            ) -> Any:
                return self._completion_fn(
                    model=_provider.litellm_model_string(),
                    messages=[{"role": "system", "content": system}, *_messages],
                    tools=_tools or None,
                )

            try:
                response = retry_with_backoff(_call, max_attempts=self._max_attempts_per_provider)
            except RetryExhaustedError as exc:
                errors.append(exc)
                continue

            self._record_usage(provider, response)
            return response

        raise AllProvidersFailedError(f"all {len(self._providers)} provider(s) failed: {errors}")

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
