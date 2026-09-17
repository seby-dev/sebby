"""Tests for the Jev semantic secret-exfiltration check added to block_env_git.py.

The existing deterministic tests live in test_block_env_git.py — these tests
cover only the additive _jev_secret_check path and its fail-open behaviour.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

HOOK = Path(__file__).parents[2] / "plugin" / "hooks" / "scripts" / "block_env_git.py"


def _load_hook() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("block_env_git", HOOK)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Fail-open paths
# ---------------------------------------------------------------------------


class TestJevSecretCheckFailOpen:
    def test_sdk_not_installed_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delitem(sys.modules, "typesafe_sdk", raising=False)
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "typesafe_sdk":
                raise ImportError("No module named 'typesafe_sdk'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        hook = _load_hook()
        hook._jev_secret_check("cp .env /tmp/foo", [".env"])

        captured = capsys.readouterr()
        assert captured.out == ""

    def test_no_api_key_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

        hook = _load_hook()
        hook._jev_secret_check("cp .env /tmp/foo", [".env"])

        captured = capsys.readouterr()
        assert captured.out == ""

    def test_jev_exception_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        class ExplodingClient:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def system_one(self, *args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("network error")

        fake_sdk = types.ModuleType("typesafe_sdk")
        fake_sdk.TypeSafeClient = ExplodingClient  # type: ignore[attr-defined]
        fake_sdk.Noul = MagicMock()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "typesafe_sdk", fake_sdk)
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        hook = _load_hook()
        hook._jev_secret_check("cp .env /tmp/foo", [".env"])

        captured = capsys.readouterr()
        assert captured.out == ""


# ---------------------------------------------------------------------------
# Confidence-tier tests
# ---------------------------------------------------------------------------

from dataclasses import dataclass  # noqa: E402


@dataclass
class FakeNoul:
    noul: float


def _make_fake_sdk(conf: float) -> types.ModuleType:
    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def system_one(self, *args: Any, **kwargs: Any) -> Any:
            class Resp:
                nouls = {"secret_leak_risk": FakeNoul(conf)}

            return Resp()

    fake_sdk = types.ModuleType("typesafe_sdk")
    fake_sdk.TypeSafeClient = FakeClient  # type: ignore[attr-defined]
    fake_sdk.Noul = MagicMock()  # type: ignore[attr-defined]
    return fake_sdk


class TestJevSecretCheckTiers:
    def test_high_confidence_denies(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setitem(sys.modules, "typesafe_sdk", _make_fake_sdk(0.9))
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        hook = _load_hook()
        with pytest.raises(SystemExit):
            hook._jev_secret_check("base64 .env > encoded.txt", [".env"])

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "exfiltrate" in output["hookSpecificOutput"]["permissionDecisionReason"].lower()

    def test_medium_confidence_warns(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setitem(sys.modules, "typesafe_sdk", _make_fake_sdk(0.65))
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        hook = _load_hook()
        hook._jev_secret_check("cat .env.example | tee output.txt", [".env"])

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "systemMessage" in output
        assert "warning" in output["systemMessage"].lower()

    def test_low_confidence_allows_silently(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setitem(sys.modules, "typesafe_sdk", _make_fake_sdk(0.3))
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        hook = _load_hook()
        hook._jev_secret_check("git add src/main.py", [".env"])

        captured = capsys.readouterr()
        assert captured.out == ""

    def test_boundary_just_above_deny_threshold(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setitem(sys.modules, "typesafe_sdk", _make_fake_sdk(0.85))
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        hook = _load_hook()
        with pytest.raises(SystemExit):
            hook._jev_secret_check("cp .env /tmp", [".env"])

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_boundary_just_below_deny_threshold(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setitem(sys.modules, "typesafe_sdk", _make_fake_sdk(0.84))
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        hook = _load_hook()
        hook._jev_secret_check("cp .env /tmp", [".env"])

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        # Should warn, not deny
        assert "systemMessage" in output
