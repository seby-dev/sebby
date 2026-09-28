"""Shared fixtures: hermetic git repositories in tmp_path."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point git at a throwaway global config, so the user's hooks and settings don't leak in."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text(
        "[user]\n\tname = revgate-test\n\temail = test@example.invalid\n"
        "[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = main\n"
        "[core]\n\thooksPath = /dev/null\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path, git_env: None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.txt").write_text("alpha\n")
    (repo / ".gitignore").write_text("ignored/\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo
