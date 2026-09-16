from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from sebby.llm.client import AllProvidersFailedError, LLMClient, UsageRecord
from sebby.llm.providers import ProviderConfig
from sebby.retry import RetryExhaustedError


@dataclass
class FakeUsage:
    prompt_tokens: int
    completion_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class FakeResponse:
    usage: FakeUsage
    text: str = "ok"


def test_complete_calls_first_provider_and_returns_response() -> None:
    calls: list[dict[str, Any]] = []

    def fake_completion(**kwargs: Any) -> FakeResponse:
        calls.append(kwargs)
        return FakeResponse(usage=FakeUsage(prompt_tokens=10, completion_tokens=5))

    client = LLMClient(
        providers=[ProviderConfig(provider="anthropic", model="claude-sonnet-5")],
        completion_fn=fake_completion,
    )

    response = client.complete(system="be helpful", messages=[{"role": "user", "content": "hi"}])

    assert response.text == "ok"
    assert len(calls) == 1
    assert calls[0]["model"] == "anthropic/claude-sonnet-5"


def test_complete_marks_cache_breakpoint_for_anthropic_only() -> None:
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> FakeResponse:
        captured.update(kwargs)
        return FakeResponse(usage=FakeUsage(prompt_tokens=1, completion_tokens=1))

    client = LLMClient(
        providers=[ProviderConfig(provider="anthropic", model="claude-sonnet-5")],
        completion_fn=fake_completion,
    )

    client.complete(system="be helpful", messages=[{"role": "user", "content": "hi"}])

    last_message = captured["messages"][-1]
    assert last_message["content"] == [
        {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
    ]


def test_complete_does_not_mark_cache_breakpoint_for_non_anthropic() -> None:
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> FakeResponse:
        captured.update(kwargs)
        return FakeResponse(usage=FakeUsage(prompt_tokens=1, completion_tokens=1))

    client = LLMClient(
        providers=[ProviderConfig(provider="openai", model="gpt-5.1")],
        completion_fn=fake_completion,
    )

    client.complete(system="be helpful", messages=[{"role": "user", "content": "hi"}])

    last_message = captured["messages"][-1]
    assert last_message["content"] == "hi"


def test_complete_fails_over_to_next_provider_on_exhausted_retries() -> None:
    attempts: list[str] = []

    def fake_completion(**kwargs: Any) -> FakeResponse:
        attempts.append(kwargs["model"])
        if kwargs["model"].startswith("anthropic/"):
            raise RuntimeError("anthropic is down")
        return FakeResponse(usage=FakeUsage(prompt_tokens=1, completion_tokens=1))

    client = LLMClient(
        providers=[
            ProviderConfig(provider="anthropic", model="claude-sonnet-5"),
            ProviderConfig(provider="openai", model="gpt-5.1"),
        ],
        completion_fn=fake_completion,
        max_attempts_per_provider=1,
    )

    response = client.complete(system="be helpful", messages=[{"role": "user", "content": "hi"}])

    assert response.text == "ok"
    assert attempts == ["anthropic/claude-sonnet-5", "openai/gpt-5.1"]


def test_complete_raises_when_all_providers_fail() -> None:
    def fake_completion(**kwargs: Any) -> FakeResponse:
        raise RuntimeError("down")

    client = LLMClient(
        providers=[ProviderConfig(provider="anthropic", model="claude-sonnet-5")],
        completion_fn=fake_completion,
        max_attempts_per_provider=1,
    )

    with pytest.raises(AllProvidersFailedError):
        client.complete(system="be helpful", messages=[{"role": "user", "content": "hi"}])


def test_complete_invokes_on_usage_callback_with_token_counts() -> None:
    records: list[UsageRecord] = []

    def fake_completion(**kwargs: Any) -> FakeResponse:
        return FakeResponse(
            usage=FakeUsage(
                prompt_tokens=100,
                completion_tokens=20,
                cache_creation_input_tokens=80,
                cache_read_input_tokens=0,
            )
        )

    client = LLMClient(
        providers=[ProviderConfig(provider="anthropic", model="claude-sonnet-5")],
        completion_fn=fake_completion,
        on_usage=records.append,
    )

    client.complete(system="be helpful", messages=[{"role": "user", "content": "hi"}])

    assert records == [
        UsageRecord(
            provider="anthropic",
            model="claude-sonnet-5",
            input_tokens=100,
            output_tokens=20,
            cache_creation_input_tokens=80,
            cache_read_input_tokens=0,
        )
    ]


def test_complete_raises_value_error_when_no_providers_configured() -> None:
    with pytest.raises(ValueError):
        LLMClient(providers=[], completion_fn=lambda **_: None)


def test_complete_handles_tool_call_message_with_none_content() -> None:
    # Simulates a prior assistant turn that only made tool calls, the
    # standard shape being {"role": "assistant", "content": None, "tool_calls": [...]}.
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> FakeResponse:
        captured.update(kwargs)
        return FakeResponse(usage=FakeUsage(prompt_tokens=1, completion_tokens=1))

    client = LLMClient(
        providers=[ProviderConfig(provider="anthropic", model="claude-sonnet-5")],
        completion_fn=fake_completion,
    )

    messages = [
        {"role": "user", "content": "do something"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_1", "function": {"name": "search"}}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
    ]

    # Must not raise (previously crashed with TypeError/KeyError) and
    # failover must remain reachable for exceptions raised during cache prep.
    response = client.complete(system="be helpful", messages=messages)

    assert response.text == "ok"
    # Cache marking still applies to the actual last message.
    last_message = captured["messages"][-1]
    assert last_message["content"] == [
        {"type": "text", "text": "result", "cache_control": {"type": "ephemeral"}}
    ]
    # The None-content tool-call message passed through unmarked, untouched.
    tool_call_message = captured["messages"][2]
    assert tool_call_message["content"] is None
    assert tool_call_message["tool_calls"] == [{"id": "call_1", "function": {"name": "search"}}]


def test_complete_fails_over_when_cache_prep_raises() -> None:
    # A cache-marking exception (e.g. exceeding Anthropic's breakpoint cap)
    # must be caught by the per-provider retry/failover machinery, not
    # escape complete() entirely.
    from sebby.llm.cache import MAX_CACHE_BREAKPOINTS

    block = {"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}
    over_cap_messages = [
        {"role": "user", "content": [block]} for _ in range(MAX_CACHE_BREAKPOINTS + 1)
    ]

    attempts: list[str] = []

    def fake_completion(**kwargs: Any) -> FakeResponse:
        attempts.append(kwargs["model"])
        return FakeResponse(usage=FakeUsage(prompt_tokens=1, completion_tokens=1))

    client = LLMClient(
        providers=[
            ProviderConfig(provider="anthropic", model="claude-sonnet-5"),
            ProviderConfig(provider="openai", model="gpt-5.1"),
        ],
        completion_fn=fake_completion,
        max_attempts_per_provider=1,
        sleep=lambda _: None,
    )

    response = client.complete(system="be helpful", messages=over_cap_messages)

    assert response.text == "ok"
    # The anthropic provider never actually called the completion_fn (its
    # cache-prep raised before reaching it) and failover moved straight to
    # openai, which does not mark cache breakpoints so is unaffected.
    assert attempts == ["openai/gpt-5.1"]


def test_complete_chains_underlying_error_on_all_providers_failed() -> None:
    def fake_completion(**kwargs: Any) -> FakeResponse:
        raise RuntimeError("down")

    client = LLMClient(
        providers=[ProviderConfig(provider="anthropic", model="claude-sonnet-5")],
        completion_fn=fake_completion,
        max_attempts_per_provider=1,
        sleep=lambda _: None,
    )

    with pytest.raises(AllProvidersFailedError) as exc_info:
        client.complete(system="be helpful", messages=[{"role": "user", "content": "hi"}])

    assert exc_info.value.__cause__ is not None
    assert isinstance(exc_info.value.__cause__, RetryExhaustedError)


def test_complete_passes_reasoning_effort_when_configured() -> None:
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> FakeResponse:
        captured.update(kwargs)
        return FakeResponse(usage=FakeUsage(prompt_tokens=1, completion_tokens=1))

    client = LLMClient(
        providers=[ProviderConfig(provider="anthropic", model="claude-sonnet-5", effort="high")],
        completion_fn=fake_completion,
    )

    client.complete(system="be helpful", messages=[{"role": "user", "content": "hi"}])

    assert captured["reasoning_effort"] == "high"


def test_complete_omits_reasoning_effort_when_not_configured() -> None:
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> FakeResponse:
        captured.update(kwargs)
        return FakeResponse(usage=FakeUsage(prompt_tokens=1, completion_tokens=1))

    client = LLMClient(
        providers=[ProviderConfig(provider="anthropic", model="claude-sonnet-5")],
        completion_fn=fake_completion,
    )

    client.complete(system="be helpful", messages=[{"role": "user", "content": "hi"}])

    assert "reasoning_effort" not in captured


def test_complete_uses_custom_sleep_for_retry_backoff() -> None:
    recorded: list[float] = []
    attempts = {"n": 0}

    def fake_completion(**kwargs: Any) -> FakeResponse:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("transient")
        return FakeResponse(usage=FakeUsage(prompt_tokens=1, completion_tokens=1))

    client = LLMClient(
        providers=[ProviderConfig(provider="anthropic", model="claude-sonnet-5")],
        completion_fn=fake_completion,
        max_attempts_per_provider=2,
        base_delay=3.0,
        sleep=recorded.append,
    )

    response = client.complete(system="be helpful", messages=[{"role": "user", "content": "hi"}])

    assert response.text == "ok"
    assert recorded == [3.0]


def test_complete_custom_retryable_exceptions_propagates_non_matching_error() -> None:
    calls: list[str] = []

    def fake_completion(**kwargs: Any) -> FakeResponse:
        calls.append(kwargs["model"])
        raise KeyError("not retryable")

    client = LLMClient(
        providers=[
            ProviderConfig(provider="anthropic", model="claude-sonnet-5"),
            ProviderConfig(provider="openai", model="gpt-5.1"),
        ],
        completion_fn=fake_completion,
        max_attempts_per_provider=3,
        retryable_exceptions=(ValueError,),
        sleep=lambda _: None,
    )

    with pytest.raises(KeyError):
        client.complete(system="be helpful", messages=[{"role": "user", "content": "hi"}])

    # Propagated immediately on the first provider, no retry, no failover.
    assert calls == ["anthropic/claude-sonnet-5"]


def test_llm_package_exports_public_api() -> None:
    from sebby.llm import AllProvidersFailedError as ExportedError
    from sebby.llm import LLMClient as ExportedClient
    from sebby.llm import ProviderConfig as ExportedConfig

    assert ExportedError is AllProvidersFailedError
    assert ExportedClient is LLMClient
    assert ExportedConfig is ProviderConfig
