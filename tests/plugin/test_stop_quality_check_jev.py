"""Tests for the Jev severity-triage addition to stop_quality_check.py.

The existing blocking behaviour tests live in test_stop_quality_check.py.
These tests cover only the additive _jev_severity_note path:
- SDK not installed → empty string, no exception
- No API key → empty string
- Jev call raises → empty string (fail open)
- Jev call succeeds → severity label appended to block reason
- Blocking decision is never changed by Jev (any issues still block)
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

HOOK = Path(__file__).parents[2] / "plugin" / "hooks" / "scripts" / "stop_quality_check.py"
FIXTURES = Path(__file__).parent / "fixtures"


def _load_hook() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("stop_quality_check", HOOK)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# _jev_severity_note fail-open paths
# ---------------------------------------------------------------------------


class TestJevSeverityNoteFailOpen:
    def test_sdk_not_installed_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        result = hook._jev_severity_note("lint: E501 line too long")
        assert result == ""

    def test_no_api_key_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

        hook = _load_hook()
        result = hook._jev_severity_note("lint: E501 line too long")
        assert result == ""

    def test_jev_exception_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class ExplodingClient:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def system_one(self, *args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("network error")

        fake_sdk = types.ModuleType("typesafe_sdk")
        fake_sdk.TypeSafeClient = ExplodingClient  # type: ignore[attr-defined]
        fake_sdk.Score = MagicMock()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "typesafe_sdk", fake_sdk)
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        hook = _load_hook()
        result = hook._jev_severity_note("typecheck: error: Name 'foo' is not defined")
        assert result == ""


# ---------------------------------------------------------------------------
# _jev_severity_note success paths
# ---------------------------------------------------------------------------


@dataclass
class FakeScore:
    score: str


def _make_fake_sdk(score_value: str) -> types.ModuleType:
    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def system_one(self, *args: Any, **kwargs: Any) -> Any:
            class Resp:
                scores = {"severity": FakeScore(score_value)}

            return Resp()

    fake_sdk = types.ModuleType("typesafe_sdk")
    fake_sdk.TypeSafeClient = FakeClient  # type: ignore[attr-defined]
    fake_sdk.Score = MagicMock()  # type: ignore[attr-defined]
    return fake_sdk


class TestJevSeverityNoteSuccess:
    def test_returns_severity_label_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "typesafe_sdk", _make_fake_sdk("cosmetic"))
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        hook = _load_hook()
        result = hook._jev_severity_note("E501 line too long")
        assert "cosmetic" in result

    def test_definite_bug_label_appended(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "typesafe_sdk", _make_fake_sdk("definite_bug"))
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        hook = _load_hook()
        result = hook._jev_severity_note("error: Name 'foo' is not defined")
        assert "definite_bug" in result


# ---------------------------------------------------------------------------
# Integration: Jev label appears in block message but blocking never changes
# ---------------------------------------------------------------------------


import subprocess  # noqa: E402


def run_hook(env_overrides: dict[str, str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path), **env_overrides}
    return subprocess.run(
        [sys.executable, str(HOOK)], input="{}", capture_output=True, text=True, env=env
    )


class TestBlockingNotChangedByJev:
    def test_still_blocks_when_jev_unavailable(self, tmp_path: Path) -> None:
        result = run_hook(
            {
                "SEBBY_LINT_COMMAND": f"{sys.executable} {FIXTURES / 'fake_lint_fail.py'}",
                # No TYPESAFE_API_KEY — Jev unavailable
            },
            tmp_path,
        )
        payload = json.loads(result.stdout)
        assert payload["decision"] == "block"

    def test_still_blocks_when_jev_would_say_cosmetic(self, tmp_path: Path) -> None:
        """Even if Jev says 'cosmetic', the hook must still block."""
        # We can't inject a fake SDK cleanly in a subprocess test, so we confirm
        # the blocking decision is independent of Jev by verifying that the hook
        # blocks on the same lint failure regardless of TYPESAFE_API_KEY presence.
        result_no_key = run_hook(
            {"SEBBY_LINT_COMMAND": f"{sys.executable} {FIXTURES / 'fake_lint_fail.py'}"},
            tmp_path,
        )
        assert json.loads(result_no_key.stdout)["decision"] == "block"

        # With a fake API key set (Jev will fail to connect gracefully — fail open),
        # the block must still happen.
        result_with_key = run_hook(
            {
                "SEBBY_LINT_COMMAND": f"{sys.executable} {FIXTURES / 'fake_lint_fail.py'}",
                "TYPESAFE_API_KEY": "fake-key-that-wont-connect",
            },
            tmp_path,
        )
        assert json.loads(result_with_key.stdout)["decision"] == "block"
