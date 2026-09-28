"""Safe git access: every call is `git -C <path>`, and $HOME is never a repository root."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Iterable, Mapping
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


# --- read-only repository access (Stage 1a) ---------------------------------------------

_HUNK_RE = re.compile(rb"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_DIFF_FLAGS = ("--no-color", "--no-ext-diff", "--no-textconv")


def _check_rev(rev: str) -> None:
    if not rev or rev.startswith("-"):
        raise GitError(f"refusing revision {rev!r}")


def rev_parse(repo: Path, rev: str) -> str:
    """Full SHA of the commit `rev` names."""
    _check_rev(rev)
    return run_git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").decode().strip()


def show(repo: Path, rev: str, path: str) -> bytes | None:
    """A file's bytes at `rev`, or None when the path isn't a file there."""
    sha = blob_sha(repo, rev, path)
    if sha is None:
        return None
    return run_git(repo, "cat-file", "blob", sha)


def ls_tree(repo: Path, rev: str) -> dict[str, str]:
    """Path to blob SHA for every file at `rev` (submodules excluded)."""
    _check_rev(rev)
    out = run_git(repo, "ls-tree", "-r", "-z", "--full-tree", rev)
    tree: dict[str, str] = {}
    for record in out.split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        _mode, kind, sha = meta.split(b" ")
        if kind == b"blob":
            tree[os.fsdecode(path)] = sha.decode()
    return tree


def blob_sha(repo: Path, rev: str, path: str) -> str | None:
    _check_rev(rev)
    out = run_git(repo, "ls-tree", "-z", "--full-tree", rev, "--", path)
    for record in out.split(b"\0"):
        meta, _, name = record.partition(b"\t")
        if meta and os.fsdecode(name) == path:
            _mode, kind, sha = meta.split(b" ")
            return sha.decode() if kind == b"blob" else None
    return None


def cat_blobs(repo: Path, shas: Iterable[str]) -> dict[str, bytes]:
    """Every blob's bytes, read through one `git cat-file --batch` process."""
    wanted = list(dict.fromkeys(shas))
    if not wanted:
        return {}
    if any(not s or not all(c in "0123456789abcdef" for c in s) for s in wanted):
        raise GitError("cat_blobs takes hexadecimal object names only")
    out = run_git(repo, "cat-file", "--batch", input_=("\n".join(wanted) + "\n").encode())
    blobs: dict[str, bytes] = {}
    pos = 0
    for sha in wanted:
        eol = out.index(b"\n", pos)
        header = out[pos:eol].split(b" ")
        pos = eol + 1
        if len(header) != 3 or header[1] != b"blob":
            raise GitError(f"{sha} is not a blob: {b' '.join(header).decode(errors='replace')}")
        size = int(header[2])
        blobs[sha] = out[pos : pos + size]
        pos += size + 1
    return blobs


def diff_name_status(repo: Path, base: str, head: str) -> list[tuple[str, str, str | None]]:
    """`(status letter, path, old path)`, with renames and copies as `("R", new, old)`."""
    _check_rev(base)
    _check_rev(head)
    out = run_git(repo, "diff", "--name-status", "-z", "-M", *_DIFF_FLAGS, base, head)
    tokens = [os.fsdecode(t) for t in out.split(b"\0")]
    result: list[tuple[str, str, str | None]] = []
    i = 0
    while i < len(tokens) and tokens[i]:
        letter = tokens[i][0]
        if letter in ("R", "C"):
            result.append((letter, tokens[i + 2], tokens[i + 1]))
            i += 3
        else:
            result.append((letter, tokens[i + 1], None))
            i += 2
    return result


def diff_numstat(repo: Path, base: str, head: str) -> list[tuple[int | None, int | None, str]]:
    """`(added, deleted, path)` at the head-side path; binary files count as None."""
    _check_rev(base)
    _check_rev(head)
    out = run_git(repo, "diff", "--numstat", "-z", "-M", *_DIFF_FLAGS, base, head)
    tokens = out.split(b"\0")
    result: list[tuple[int | None, int | None, str]] = []
    i = 0
    while i < len(tokens) and tokens[i]:
        added, deleted, path = tokens[i].split(b"\t", 2)
        if path:
            i += 1
        else:  # a rename: the old and new paths follow as their own tokens
            path = tokens[i + 2]
            i += 3
        result.append(
            (
                None if added == b"-" else int(added),
                None if deleted == b"-" else int(deleted),
                os.fsdecode(path),
            )
        )
    return result


def _unquote_path(raw: bytes) -> str:
    """A path from a diff header, undoing git's C-style quoting when present."""
    raw = raw.rstrip(b"\t")
    if raw.startswith(b'"') and raw.endswith(b'"'):
        body = raw[1:-1].decode("ascii", errors="surrogateescape")
        raw = (
            body.encode("latin-1", errors="surrogateescape")
            .decode("unicode_escape")
            .encode("latin-1")
        )
    return os.fsdecode(raw)


def diff_hunks(repo: Path, base: str, head: str) -> dict[str, tuple[tuple[int, int], ...]]:
    """Head-side `(start, count)` of every `-U0` hunk, keyed by the head path."""
    _check_rev(base)
    _check_rev(head)
    out = run_git(
        repo,
        "diff",
        "-U0",
        "-M",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        *_DIFF_FLAGS,
        base,
        head,
    )
    hunks: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    in_header = False
    for line in out.split(b"\n"):
        if line.startswith(b"diff --git "):
            in_header, current = True, None
        elif in_header and line.startswith(b"+++ "):
            target = line[4:]
            if target.startswith(b'"b/'):
                current = _unquote_path(b'"' + target[3:])
            elif target.startswith(b"b/"):
                current = _unquote_path(target[2:])
            else:  # /dev/null: the file is deleted and has no head side
                current = None
        elif line.startswith(b"@@ "):
            in_header = False
            match = _HUNK_RE.match(line)
            if match is not None and current is not None:
                count = int(match.group(2)) if match.group(2) is not None else 1
                hunks.setdefault(current, []).append((int(match.group(1)), count))
    return {path: tuple(ranges) for path, ranges in hunks.items()}


def is_ancestor(repo: Path, a: str, b: str) -> bool:
    _check_rev(a)
    _check_rev(b)
    proc = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", a, b], capture_output=True
    )
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    raise GitError(f"git merge-base --is-ancestor: {proc.stderr.decode(errors='replace').strip()}")


def merge_base(repo: Path, a: str, b: str) -> str:
    _check_rev(a)
    _check_rev(b)
    return run_git(repo, "merge-base", a, b).decode().strip()


def status_porcelain(repo: Path) -> list[tuple[str, str]]:
    """`(XY, path)` for every changed or untracked file; a rename reports its new path."""
    out = run_git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    tokens = out.split(b"\0")
    result: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens) and tokens[i]:
        entry = tokens[i]
        xy, path = entry[:2].decode(), os.fsdecode(entry[3:])
        result.append((xy, path))
        i += 2 if ("R" in xy or "C" in xy) else 1
    return result
