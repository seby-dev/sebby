from __future__ import annotations

import pytest

from sebby.llm.providers import ProviderConfig, UnknownEffortError, UnknownProviderError


def test_provider_config_builds_litellm_model_string() -> None:
    config = ProviderConfig(provider="anthropic", model="claude-sonnet-5")

    assert config.litellm_model_string() == "anthropic/claude-sonnet-5"


def test_provider_config_exposes_api_key_env_var() -> None:
    config = ProviderConfig(provider="openai", model="gpt-5.1")

    assert config.api_key_env_var() == "OPENAI_API_KEY"


def test_provider_config_rejects_unknown_provider() -> None:
    with pytest.raises(UnknownProviderError):
        ProviderConfig(provider="not-a-real-provider", model="whatever")


def test_provider_config_accepts_valid_effort() -> None:
    config = ProviderConfig(provider="anthropic", model="claude-sonnet-5", effort="high")

    assert config.effort == "high"


def test_provider_config_rejects_invalid_effort() -> None:
    with pytest.raises(UnknownEffortError):
        ProviderConfig(provider="anthropic", model="claude-sonnet-5", effort="extreme")


def test_provider_config_effort_defaults_to_none() -> None:
    config = ProviderConfig(provider="gemini", model="gemini-2.5-pro")

    assert config.effort is None
