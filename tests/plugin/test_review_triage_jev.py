"""Tests for the Jev-backed risk scoring and rollout-mode handling in
review_triage.py. Deterministic aspect/trigger detection is covered in
test_review_triage.py -- these tests cover only _jev_risk_tier, resolve_mode,
log_shadow_decision, and how recommend() combines them.
"""

from __future__ import annotations

import builtins
import importlib
import json
import subprocess
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

SCRIPT = Path(__file__).parents[2] / "plugin" / "hooks" / "scripts" / "review_triage.py"


def _load_script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("review_triage", SCRIPT)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def make_repo(
    tmp_path: Path, base_files: dict[str, str], branch_files: dict[str, str]
) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    for name, content in base_files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "base"], repo)
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    for name, content in branch_files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "branch changes"], repo)

    return repo, base_sha


@dataclass
class FakeScore:
    score: str


def _make_fake_sdk(score_value: str) -> types.ModuleType:
    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def system_one(self, *args: Any, **kwargs: Any) -> Any:
            class Resp:
                scores = {"risk": FakeScore(score_value)}

            return Resp()

    fake_sdk = types.ModuleType("typesafe_sdk")
    fake_sdk.TypeSafeClient = FakeClient  # type: ignore[attr-defined]
    fake_sdk.Score = MagicMock()  # type: ignore[attr-defined]
    return fake_sdk


class TestJevRiskTierFailOpen:
    def test_no_api_key_returns_high(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        mod = _load_script()
        assert mod._jev_risk_tier("some diff") == "high"

    def test_sdk_not_installed_returns_high(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delitem(sys.modules, "typesafe_sdk", raising=False)
        real_import = builtins.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "typesafe_sdk":
                raise ImportError("No module named 'typesafe_sdk'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        mod = _load_script()
        assert mod._jev_risk_tier("some diff") == "high"

    def test_jev_exception_returns_high(self, monkeypatch: pytest.MonkeyPatch) -> None:
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

        mod = _load_script()
        assert mod._jev_risk_tier("some diff") == "high"

    def test_unrecognized_score_value_returns_high(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "typesafe_sdk", _make_fake_sdk("not_a_real_tier"))
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        mod = _load_script()
        assert mod._jev_risk_tier("some diff") == "high"


class TestJevRiskTierSuccess:
    @pytest.mark.parametrize("tier", ["trivial", "low", "moderate", "high"])
    def test_returns_tier_from_jev(self, monkeypatch: pytest.MonkeyPatch, tier: str) -> None:
        monkeypatch.setitem(sys.modules, "typesafe_sdk", _make_fake_sdk(tier))
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        mod = _load_script()
        assert mod._jev_risk_tier("some diff") == tier


class TestResolveMode:
    def test_default_is_shadow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SEBBY_REVIEW_TRIAGE_MODE", raising=False)
        mod = _load_script()
        assert mod.resolve_mode() == "shadow"

    def test_invalid_value_falls_back_to_shadow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SEBBY_REVIEW_TRIAGE_MODE", "bogus")
        mod = _load_script()
        assert mod.resolve_mode() == "shadow"

    @pytest.mark.parametrize("mode", ["off", "shadow", "active"])
    def test_valid_values_pass_through(self, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
        monkeypatch.setenv("SEBBY_REVIEW_TRIAGE_MODE", mode)
        mod = _load_script()
        assert mod.resolve_mode() == mode


class TestRecommendModes:
    def test_off_mode_never_calls_jev_and_never_narrows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        class ExplodingClient:
            def __init__(self, **kwargs: Any) -> None:
                raise AssertionError("Jev must not be called when mode is 'off'")

        fake_sdk = types.ModuleType("typesafe_sdk")
        fake_sdk.TypeSafeClient = ExplodingClient  # type: ignore[attr-defined]
        fake_sdk.Score = MagicMock()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "typesafe_sdk", fake_sdk)
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
        monkeypatch.setenv("SEBBY_REVIEW_TRIAGE_MODE", "off")

        repo, base = make_repo(
            tmp_path,
            base_files={"README.md": "hello\n"},
            branch_files={"README.md": "hello world\n\nclass Widget:\n    pass\n"},
        )
        mod = _load_script()
        result = mod.recommend(base, repo)
        assert result["risk_tier"] == "high"
        assert result["recommended_aspects"] == result["candidate_aspects"]

    def test_shadow_mode_logs_but_never_narrows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setitem(sys.modules, "typesafe_sdk", _make_fake_sdk("trivial"))
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
        monkeypatch.setenv("SEBBY_REVIEW_TRIAGE_MODE", "shadow")

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        repo, base = make_repo(
            tmp_path,
            base_files={"README.md": "hello\n"},
            branch_files={"README.md": "hello world\n\nclass Widget:\n    pass\n"},
        )
        mod = _load_script()
        result = mod.recommend(base, repo)

        assert result["risk_tier"] == "trivial"
        assert result["recommended_aspects"] == result["candidate_aspects"]
        assert result["candidate_aspects"] != []

        log_path = fake_home / ".claude" / "sebby-triage.jsonl"
        assert log_path.exists()
        entry = json.loads(log_path.read_text().splitlines()[-1])
        assert entry["would_recommend"] == []

    def test_active_mode_narrows_on_low_risk(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setitem(sys.modules, "typesafe_sdk", _make_fake_sdk("low"))
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
        monkeypatch.setenv("SEBBY_REVIEW_TRIAGE_MODE", "active")

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        repo, base = make_repo(
            tmp_path,
            base_files={"README.md": "hello\n"},
            branch_files={"README.md": "hello world\n\nclass Widget:\n    pass\n"},
        )
        mod = _load_script()
        result = mod.recommend(base, repo)

        assert result["risk_tier"] == "low"
        assert result["recommended_aspects"] == []
        assert result["candidate_aspects"] != []

    def test_active_mode_keeps_full_set_on_moderate_risk(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setitem(sys.modules, "typesafe_sdk", _make_fake_sdk("moderate"))
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
        monkeypatch.setenv("SEBBY_REVIEW_TRIAGE_MODE", "active")

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        repo, base = make_repo(
            tmp_path,
            base_files={"README.md": "hello\n"},
            branch_files={"README.md": "hello world\n\nclass Widget:\n    pass\n"},
        )
        mod = _load_script()
        result = mod.recommend(base, repo)

        assert result["risk_tier"] == "moderate"
        assert result["recommended_aspects"] == result["candidate_aspects"]
