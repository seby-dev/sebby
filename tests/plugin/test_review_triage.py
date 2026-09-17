"""Tests for the deterministic parts of review_triage.py: candidate aspect
detection, the silent-failure-hunter trigger, and the CLI end-to-end.
Jev-backed risk scoring and rollout modes are covered separately in
test_review_triage_jev.py.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path

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
    """Create a real git repo: commit `base_files`, then `branch_files` on
    top. Returns (repo_path, base_sha) so callers can pass `base_sha` as
    `--base` and get a diff scoped to exactly the branch_files changes.
    """
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


class TestCandidateAspects:
    def test_test_file_changed_triggers_tests_aspect(self, tmp_path: Path) -> None:
        repo, base = make_repo(
            tmp_path,
            base_files={"README.md": "hello\n"},
            branch_files={"tests/test_widget.py": "def test_x():\n    assert True\n"},
        )
        mod = _load_script()
        files = mod.get_changed_files(base, repo)
        diff = mod.get_diff_text(base, repo)
        assert mod.candidate_aspects(files, diff) == {"tests"}

    def test_new_class_triggers_types_aspect(self, tmp_path: Path) -> None:
        repo, base = make_repo(
            tmp_path,
            base_files={"main.py": "x = 1\n"},
            branch_files={"main.py": "x = 1\n\nclass Widget:\n    pass\n"},
        )
        mod = _load_script()
        files = mod.get_changed_files(base, repo)
        diff = mod.get_diff_text(base, repo)
        assert "types" in mod.candidate_aspects(files, diff)

    def test_new_comment_triggers_comments_aspect(self, tmp_path: Path) -> None:
        repo, base = make_repo(
            tmp_path,
            base_files={"main.py": "x = 1\n"},
            branch_files={"main.py": "# Explains a subtle invariant\nx = 1\n"},
        )
        mod = _load_script()
        files = mod.get_changed_files(base, repo)
        diff = mod.get_diff_text(base, repo)
        assert "comments" in mod.candidate_aspects(files, diff)

    def test_trivial_docs_change_triggers_nothing(self, tmp_path: Path) -> None:
        repo, base = make_repo(
            tmp_path,
            base_files={"README.md": "hello\n"},
            branch_files={"README.md": "hello world\n"},
        )
        mod = _load_script()
        files = mod.get_changed_files(base, repo)
        diff = mod.get_diff_text(base, repo)
        assert mod.candidate_aspects(files, diff) == set()


class TestSilentFailureHunterTrigger:
    def test_new_try_except_triggers(self, tmp_path: Path) -> None:
        repo, base = make_repo(
            tmp_path,
            base_files={"main.py": "x = 1\n"},
            branch_files={"main.py": "try:\n    x = 1\nexcept Exception:\n    x = 0\n"},
        )
        mod = _load_script()
        diff = mod.get_diff_text(base, repo)
        assert mod.silent_failure_hunter_needed(diff) is True

    def test_no_error_handling_does_not_trigger(self, tmp_path: Path) -> None:
        repo, base = make_repo(
            tmp_path,
            base_files={"main.py": "x = 1\n"},
            branch_files={"main.py": "x = 2\n"},
        )
        mod = _load_script()
        diff = mod.get_diff_text(base, repo)
        assert mod.silent_failure_hunter_needed(diff) is False


class TestGitErrorHandling:
    def test_get_changed_files_raises_on_bad_ref(self, tmp_path: Path) -> None:
        repo, _ = make_repo(
            tmp_path,
            base_files={"README.md": "hello\n"},
            branch_files={"README.md": "hello world\n"},
        )
        mod = _load_script()
        with pytest.raises(RuntimeError):
            mod.get_changed_files("not-a-real-ref", repo)

    def test_get_diff_text_raises_on_bad_ref(self, tmp_path: Path) -> None:
        repo, _ = make_repo(
            tmp_path,
            base_files={"README.md": "hello\n"},
            branch_files={"README.md": "hello world\n"},
        )
        mod = _load_script()
        with pytest.raises(RuntimeError):
            mod.get_diff_text("not-a-real-ref", repo)


class TestCLIEndToEnd:
    def test_cli_prints_recommendation_json(self, tmp_path: Path) -> None:
        repo, base = make_repo(
            tmp_path,
            base_files={"README.md": "hello\n"},
            branch_files={"README.md": "hello world\n"},
        )
        # Explicitly drop TYPESAFE_API_KEY so this test never makes a real
        # network call regardless of the ambient environment it runs in --
        # once Task 2 lands, recommend() checks this var for real.
        env = {k: v for k, v in os.environ.items() if k != "TYPESAFE_API_KEY"}
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--base", base, "--project-dir", str(repo)],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["candidate_aspects"] == []
        assert payload["silent_failure_hunter"] is False
