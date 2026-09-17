"""Tests for plugin/hooks/scripts/semantic_destructive_check.py."""

from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers to import the hook script as a module
# ---------------------------------------------------------------------------

HOOK = Path(__file__).parents[2] / "plugin" / "hooks" / "scripts" / "semantic_destructive_check.py"


def _load_hook() -> types.ModuleType:
    """Import the hook script under a stable name, reloading each time."""
    spec = importlib.util.spec_from_file_location("semantic_destructive_check", HOOK)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Pre-filter isolation tests
# ---------------------------------------------------------------------------


class TestPreFilter:
    """The pre-filter must prevent Jev calls for obviously benign commands."""

    def _hook(self) -> types.ModuleType:
        return _load_hook()

    def test_benign_command_is_not_risk_suggestive(self) -> None:
        hook = self._hook()
        assert not hook._is_risk_suggestive("git status")
        assert not hook._is_risk_suggestive("ls -la")
        assert not hook._is_risk_suggestive("echo hello")
        assert not hook._is_risk_suggestive("cat README.md")
        assert not hook._is_risk_suggestive("git log --oneline -5")

    def test_destructive_command_is_risk_suggestive(self) -> None:
        hook = self._hook()
        assert hook._is_risk_suggestive("rm -rf /tmp/foo")
        assert hook._is_risk_suggestive("sudo apt-get install vim")
        assert hook._is_risk_suggestive("dd if=/dev/zero of=/dev/sda")
        assert hook._is_risk_suggestive("git branch -D old-feature")
        assert hook._is_risk_suggestive("find . -name '*.pyc' -delete")
        assert hook._is_risk_suggestive("echo secret | sh")
        assert hook._is_risk_suggestive("truncate -s 0 file.txt")

    def test_redirect_operator_triggers_pre_filter(self) -> None:
        hook = self._hook()
        assert hook._is_risk_suggestive("cat foo > bar.txt")

    def test_benign_command_never_reaches_jev(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If the mock client is called, the test fails — proving benign commands bypass Jev."""

        class BoomClient:
            def __init__(self, **kwargs: Any) -> None:
                raise AssertionError("Jev should not be called for benign commands")

        fake_sdk = types.ModuleType("typesafe_sdk")
        fake_sdk.TypeSafeClient = BoomClient  # type: ignore[attr-defined]
        fake_sdk.Noul = MagicMock()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "typesafe_sdk", fake_sdk)
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        hook = _load_hook()
        # Should not raise — benign command exits via pre-filter before Jev
        hook._jev_check("git status", "/project")


# ---------------------------------------------------------------------------
# SDK-not-installed / no-key paths
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_sdk_not_installed_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delitem(sys.modules, "typesafe_sdk", raising=False)
        # Make import fail
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "typesafe_sdk":
                raise ImportError("No module named 'typesafe_sdk'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        hook = _load_hook()
        hook._jev_check("rm -rf /tmp/foo", "/project")

        captured = capsys.readouterr()
        assert captured.out == ""  # no output — fail open

    def test_no_api_key_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

        hook = _load_hook()
        hook._jev_check("rm -rf /tmp/foo", "/project")

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
        hook._jev_check("rm -rf /tmp/foo", "/project")

        captured = capsys.readouterr()
        assert captured.out == ""  # no output — fail open


# ---------------------------------------------------------------------------
# Confidence-tier tests
# ---------------------------------------------------------------------------

from dataclasses import dataclass  # noqa: E402


@dataclass
class FakeNoul:
    noul: float


@dataclass
class FakeNoulsResponse:
    irreversible: float
    privilege: float

    @property
    def nouls(self) -> dict[str, FakeNoul]:
        return {
            "irreversible_data_loss": FakeNoul(self.irreversible),
            "privilege_or_scope_escalation": FakeNoul(self.privilege),
        }


def _make_fake_sdk(irr: float, priv: float) -> tuple[types.ModuleType, type]:
    response = FakeNoulsResponse(irr, priv)

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def system_one(self, *args: Any, **kwargs: Any) -> FakeNoulsResponse:
            return response

    fake_sdk = types.ModuleType("typesafe_sdk")
    fake_sdk.TypeSafeClient = FakeClient  # type: ignore[attr-defined]
    fake_sdk.Noul = MagicMock()  # type: ignore[attr-defined]
    return fake_sdk, FakeClient


class TestConfidenceTiers:
    def test_high_irreversible_confidence_denies(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_sdk, _ = _make_fake_sdk(irr=0.95, priv=0.1)
        monkeypatch.setitem(sys.modules, "typesafe_sdk", fake_sdk)
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        hook = _load_hook()
        with pytest.raises(SystemExit):
            hook._jev_check("rm -rf /important", "/project")

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = output["hookSpecificOutput"]["permissionDecisionReason"].lower()
        assert "permanently delete" in reason or "irreversible" in reason or "overwrite" in reason

    def test_high_privilege_confidence_denies(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_sdk, _ = _make_fake_sdk(irr=0.1, priv=0.92)
        monkeypatch.setitem(sys.modules, "typesafe_sdk", fake_sdk)
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        hook = _load_hook()
        with pytest.raises(SystemExit):
            hook._jev_check("sudo something", "/project")

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "privilege" in output["hookSpecificOutput"]["permissionDecisionReason"].lower()

    def test_medium_confidence_warns(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_sdk, _ = _make_fake_sdk(irr=0.75, priv=0.2)
        monkeypatch.setitem(sys.modules, "typesafe_sdk", fake_sdk)
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        hook = _load_hook()
        hook._jev_check("rm ./build", "/project")

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "systemMessage" in output
        assert "warning" in output["systemMessage"].lower()

    def test_low_confidence_allows_silently(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_sdk, _ = _make_fake_sdk(irr=0.3, priv=0.2)
        monkeypatch.setitem(sys.modules, "typesafe_sdk", fake_sdk)
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        hook = _load_hook()
        hook._jev_check("rm ./build", "/project")

        captured = capsys.readouterr()
        assert captured.out == ""  # no output — allowed silently
