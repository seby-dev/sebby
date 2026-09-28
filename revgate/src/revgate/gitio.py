"""Safe git access: every call is `git -C <path>`, and $HOME is never a repository root."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Mapping
from pathlib import Path


class GitError(Exception):
    """A git command failed."""


class SafetyError(Exception):
    """The path resolves to a repository revgate must not touch."""


def run_git(
    repo: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    input_: bytes | None = None,
    check: bool = True,
) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_,
        capture_output=True,
        env=dict(env) if env is not None else None,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def home_dir() -> Path:
    return Path(os.environ.get("HOME") or Path.home()).resolve()


def toplevel(path: Path) -> Path | None:
    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True
    )
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.decode().strip()).resolve()


def safe_toplevel(path: Path, expected: Path | None = None) -> Path:
    top = toplevel(path)
    if top is None:
        raise SafetyError(f"{path} is not inside a git repository")
    if top == home_dir():
        raise SafetyError(
            f"{path} resolves to the repository at $HOME ({top}); refusing to run there"
        )
    if expected is not None and top != expected.resolve():
        raise SafetyError(f"{path} resolves to {top}, not the expected {expected}")
    return top


def git_path(top: Path, name: str) -> Path:
    raw = run_git(top, "rev-parse", "--git-path", name).decode().strip()
    p = Path(raw)
    return p if p.is_absolute() else (top / p).resolve()


def common_dir(top: Path) -> Path:
    raw = run_git(top, "rev-parse", "--git-common-dir").decode().strip()
    p = Path(raw)
    return p if p.is_absolute() else (top / p).resolve()


def worktree_tree_hash(top: Path, scratch: Path) -> str:
    """Tree id of the working tree as `git add -A` would stage it, without touching the index.

    Covers tracked, staged, unstaged, and untracked non-ignored files; respects .gitignore.
    On a clean tree it equals HEAD^{tree}.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    tmp_index = scratch / f"index-{uuid.uuid4().hex}"
    try:
        real_index = git_path(top, "index")
        if real_index.exists():
            shutil.copyfile(real_index, tmp_index)
        env = {**os.environ, "GIT_INDEX_FILE": str(tmp_index)}
        run_git(top, "add", "-A", env=env)
        return run_git(top, "write-tree", env=env).decode().strip()
    finally:
        tmp_index.unlink(missing_ok=True)
        Path(f"{tmp_index}.lock").unlink(missing_ok=True)
