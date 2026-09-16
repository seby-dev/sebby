from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from sebby.judgement import make_client, record_usage
from sebby.llm.client import UsageRecord


@dataclass
class FakeClient:
    """Records the kwargs it was constructed with — stands in for
    typesafe_sdk.TypeSafeClient so tests don't need the real SDK installed."""

    api_key: str
    model: str
    extra: dict[str, Any]

    def __init__(self, *, api_key: str, model: str, **kwargs: Any) -> None:
        self.api_key = api_key
        self.model = model
        self.extra = kwargs


@dataclass
class FakeUsage:
    input_tokens: int | None
    output_tokens: int | None


@dataclass
class FakeResponse:
    usage: FakeUsage


class TestMakeClient:
    def test_passes_api_key_and_model_to_client_cls(self) -> None:
        client = make_client(api_key="secret", model="jev-latest", client_cls=FakeClient)

        assert client.api_key == "secret"
        assert client.model == "jev-latest"

    def test_defaults_model_to_jev_latest(self) -> None:
        client = make_client(api_key="secret", client_cls=FakeClient)

        assert client.model == "jev-latest"

    def test_extra_kwargs_pass_through(self) -> None:
        sentinel = object()
        client = make_client(api_key="secret", client_cls=FakeClient, retry=sentinel)

        assert client.extra == {"retry": sentinel}

    def test_missing_extra_raises_friendly_import_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "typesafe_sdk":
                raise ImportError("No module named 'typesafe_sdk'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        with pytest.raises(ImportError, match="judgement.*extra"):
            make_client(api_key="secret")


class TestRecordUsage:
    def test_calls_on_usage_with_typesafe_provider(self) -> None:
        records: list[UsageRecord] = []
        response = FakeResponse(usage=FakeUsage(input_tokens=312, output_tokens=48))

        record_usage(response, model="jev-latest", on_usage=records.append)

        assert records == [
            UsageRecord(
                provider="typesafe",
                model="jev-latest",
                input_tokens=312,
                output_tokens=48,
            )
        ]

    def test_none_token_counts_default_to_zero(self) -> None:
        records: list[UsageRecord] = []
        response = FakeResponse(usage=FakeUsage(input_tokens=None, output_tokens=None))

        record_usage(response, model="jev-latest", on_usage=records.append)

        assert records[0].input_tokens == 0
        assert records[0].output_tokens == 0

    def test_no_op_when_on_usage_is_none(self) -> None:
        response = FakeResponse(usage=FakeUsage(input_tokens=10, output_tokens=5))

        record_usage(response, model="jev-latest", on_usage=None)  # must not raise
