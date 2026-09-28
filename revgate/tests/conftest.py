"""Shared fixtures: hermetic git repositories in tmp_path."""

from __future__ import annotations

import itertools
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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


@dataclass(frozen=True)
class RepoFixture:
    """A repository with a base commit and a head commit on `main`."""

    path: Path
    base: str
    head: str

    def commit(self, files: Mapping[str, str | None], message: str = "next") -> str:
        """Apply `files` (None deletes a path), commit even when nothing changed, return the SHA."""
        _apply(self.path, files)
        git(self.path, "add", "-A")
        git(self.path, "commit", "-q", "--allow-empty", "-m", message)
        return git(self.path, "rev-parse", "HEAD")


class MakeRepo(Protocol):
    def __call__(
        self,
        base_files: Mapping[str, str],
        head_files: Mapping[str, str | None],
        plan: str | None = None,
        plan_path: str = "docs/plans/plan.md",
        review_toml: str | None = None,
    ) -> RepoFixture: ...


def _apply(repo: Path, files: Mapping[str, str | None]) -> None:
    for rel, text in files.items():
        target = repo / rel
        if text is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")


@pytest.fixture
def make_repo(tmp_path: Path, git_env: None) -> MakeRepo:
    """Factory for base and head commits; each call makes a fresh repository."""
    counter = itertools.count()

    def factory(
        base_files: Mapping[str, str],
        head_files: Mapping[str, str | None],
        plan: str | None = None,
        plan_path: str = "docs/plans/plan.md",
        review_toml: str | None = None,
    ) -> RepoFixture:
        repo = tmp_path / f"mr{next(counter)}"
        repo.mkdir()
        git(repo, "init", "-q")
        base: dict[str, str | None] = dict(base_files)
        if plan is not None:
            base[plan_path] = plan
        if review_toml is not None:
            base[".review.toml"] = review_toml
        _apply(repo, base)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "--allow-empty", "-m", "base")
        base_sha = git(repo, "rev-parse", "HEAD")
        fixture = RepoFixture(repo, base_sha, base_sha)
        head_sha = fixture.commit(head_files, "head")
        return RepoFixture(repo, base_sha, head_sha)

    return factory
