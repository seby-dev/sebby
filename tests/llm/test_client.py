from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from sebby.llm.client import AllProvidersFailedError, LLMClient, UsageRecord
from sebby.llm.providers import ProviderConfig


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


def test_llm_package_exports_public_api() -> None:
    from sebby.llm import AllProvidersFailedError as ExportedError
    from sebby.llm import LLMClient as ExportedClient
    from sebby.llm import ProviderConfig as ExportedConfig

    assert ExportedError is AllProvidersFailedError
    assert ExportedClient is LLMClient
    assert ExportedConfig is ProviderConfig
