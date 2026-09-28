"""gate-if-changed: skip a command that already passed on identical inputs.

The key hashes the working tree's content (tracked, staged, unstaged, and untracked
non-ignored files), the command's arguments, its directory relative to the repository
root, the allowlisted environment variables, and the revgate version. Failures are
never cached, entries expire, and a per-key lock makes a second identical run wait and
reuse the first one's result. Git-ignored files and external tools aren't in the key.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from revgate import __version__
from revgate.config import ConfigError, GateCacheConfig, load_gate_cache_config
from revgate.gitio import SafetyError, safe_toplevel, toplevel, worktree_tree_hash
from revgate.store import append_jsonl_locked, atomic_write_text, read_jsonl, state_dir


@dataclass(frozen=True)
class GateKey:
    digest: str
    tree: str
    cwd_rel: str


def gate_key(
    argv: Sequence[str],
    top: Path,
    cwd: Path,
    env_allowlist: Sequence[str],
    scratch: Path,
) -> GateKey:
    tree = worktree_tree_hash(top, scratch)
    cwd_rel = os.path.relpath(cwd.resolve(), top)
    payload = {
        "argv": list(argv),
        "cwd": cwd_rel,
        "env": {name: os.environ.get(name) for name in sorted(env_allowlist)},
        "tree": tree,
        "version": __version__,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return GateKey(hashlib.sha256(blob).hexdigest(), tree, cwd_rel)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _execute(argv: Sequence[str], cwd: Path, err: TextIO) -> int:
    try:
        return subprocess.run(list(argv), cwd=cwd).returncode
    except (FileNotFoundError, PermissionError) as exc:
        print(f"gate-if-changed: couldn't run {argv[0]!r}: {exc}", file=err)
        return 2


def run_gate_if_changed(
    argv: Sequence[str],
    *,
    cwd: Path,
    max_age_s: int | None = None,
    no_cache: bool = False,
    clock: Callable[[], float] = time.time,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    if not argv:
        print("gate-if-changed: no command given after --", file=err)
        return 2
    if toplevel(cwd) is None:
        print("gate-if-changed: not inside a git repository; running uncached", file=err)
        return _execute(argv, cwd, err)
    try:
        top = safe_toplevel(cwd)
    except SafetyError as exc:
        print(f"gate-if-changed: {exc}", file=err)
        return 2
    try:
        cfg = load_gate_cache_config(top)
    except ConfigError as exc:
        print(f"gate-if-changed: ignoring [gates.cache]: {exc}", file=err)
        cfg = GateCacheConfig()
    max_age = cfg.max_age_s if max_age_s is None else max_age_s
    state = state_dir(top)
    key = gate_key(argv, top, cwd, cfg.env_allowlist, state / "scratch")
    entry_path = state / "gate" / f"{key.digest}.json"
    log_path = state / "gate" / "runs.jsonl"
    with _locked(state / "gate" / f"{key.digest}.lock"):
        entry = None if no_cache else _read_entry(entry_path)
        if entry is not None:
            passed_at, dur_s = entry
            if clock() - passed_at <= max_age:
                stamp = time.strftime("%H:%M", time.localtime(passed_at))
                saved = round(dur_s)
                out.flush()
                print(
                    f"gate-if-changed: PASS (cached {stamp}, tree {key.tree[:7]}, saved {saved} s)",
                    file=out,
                )
                append_jsonl_locked(log_path, _log_row(clock(), key, argv, True, 0, 0.0, saved))
                return 0
        start = time.monotonic()
        code = _execute(argv, cwd, err)
        duration = time.monotonic() - start
        if code == 0:
            record = {
                "argv": list(argv),
                "cwd": key.cwd_rel,
                "duration_s": duration,
                "passed_at": clock(),
                "tree": key.tree,
            }
            atomic_write_text(entry_path, json.dumps(record, sort_keys=True))
        append_jsonl_locked(log_path, _log_row(clock(), key, argv, False, code, duration, 0))
        return code


def _read_entry(path: Path) -> tuple[float, float] | None:
    """(passed_at, duration_s) of a cache entry; an unreadable or partial entry is a miss."""
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        return float(entry["passed_at"]), float(entry["duration_s"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def prune_entries(gate_dir: Path, max_age_s: int, now: float) -> int:
    """Delete expired or unreadable `<digest>.json` entries; returns how many.

    The empty `<digest>.lock` files stay: deleting one another process holds would let
    two runs of one key lock different inodes.
    """
    removed = 0
    for path in gate_dir.glob("*.json"):
        entry = _read_entry(path)
        if entry is None or now - entry[0] > max_age_s:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def _log_row(
    now: float, key: GateKey, argv: Sequence[str], hit: bool, code: int, dur: float, saved: int
) -> dict[str, object]:
    return {
        "argv": list(argv),
        "cwd": key.cwd_rel,
        "duration_s": round(dur, 3),
        "exit": code,
        "hit": hit,
        "key": key.digest[:12],
        "saved_s": saved,
        "tree": key.tree,
        "ts": now,
    }


def gate_stats(
    cwd: Path,
    since_s: int | None = None,
    *,
    clock: Callable[[], float] = time.time,
    out: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    try:
        top = safe_toplevel(cwd)
    except SafetyError as exc:
        print(f"gate-stats: {exc}", file=out)
        return 2
    try:
        max_age = load_gate_cache_config(top).max_age_s
    except ConfigError:
        max_age = GateCacheConfig().max_age_s
    pruned = prune_entries(state_dir(top) / "gate", max_age, clock())
    rows = read_jsonl(state_dir(top) / "gate" / "runs.jsonl")
    if since_s is not None:
        rows = [r for r in rows if float(str(r["ts"])) >= clock() - since_s]
    hits = sum(1 for r in rows if r["hit"])
    saved_h = sum(float(str(r["saved_s"])) for r in rows) / 3600
    share = f"{hits / len(rows):.0%}" if rows else "n/a"
    print(
        f"gate-if-changed: {hits}/{len(rows)} runs hit the cache ({share}); "
        f"saved {saved_h:.1f} h; pruned {pruned} expired entries",
        file=out,
    )
    return 0
