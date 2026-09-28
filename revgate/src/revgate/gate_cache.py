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
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TextIO

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


_FORWARDED = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


@contextmanager
def _kill_on_termination(proc: subprocess.Popen[bytes]) -> Iterator[None]:
    """While the gate runs, a SIGTERM, SIGINT, or SIGHUP to revgate kills the gate's
    process group before revgate exits, so a harness that times revgate out never leaves
    an orphaned mypy or pytest behind. Outside the main thread signals can't be caught,
    and nothing changes."""
    try:
        previous = {sig: signal.getsignal(sig) for sig in _FORWARDED}

        def handler(signum: int, _frame: object) -> None:
            _kill_group(proc.pid)
            raise SystemExit(128 + signum)

        for sig in _FORWARDED:
            signal.signal(sig, handler)
    except ValueError:  # not the main thread
        yield
        return
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


def _execute(argv: Sequence[str], cwd: Path, err: TextIO) -> int:
    # Its own process group (not a new session, so it keeps the terminal) lets revgate
    # kill the command and everything it started when revgate itself is stopped.
    try:
        proc = subprocess.Popen(list(argv), cwd=cwd, process_group=0)
    except (FileNotFoundError, PermissionError) as exc:
        print(f"gate-if-changed: couldn't run {argv[0]!r}: {exc}", file=err)
        return 2
    with _kill_on_termination(proc):
        return proc.wait()


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
        passed = code == 0 and _tree_unchanged(top, key, state)
        _store(entry_path, key, argv, passed, duration, clock())
        append_jsonl_locked(log_path, _log_row(clock(), key, argv, False, code, duration, 0))
        return code


def _tree_unchanged(top: Path, key: GateKey, state: Path) -> bool:
    """Whether the working tree still hashes to the key's tree after the command ran. An
    edit during the run (the command's own, an agent's, or another gate's) means the pass
    covers no single tree, so it isn't stored."""
    return worktree_tree_hash(top, state / "scratch") == key.tree


def _store(
    entry_path: Path, key: GateKey, argv: Sequence[str], passed: bool, duration: float, now: float
) -> None:
    if passed:
        record = {
            "argv": list(argv),
            "cwd": key.cwd_rel,
            "duration_s": duration,
            "passed_at": now,
            "tree": key.tree,
        }
        atomic_write_text(entry_path, json.dumps(record, sort_keys=True))
    else:
        # A failure, such as a --no-cache rerun after a toolchain change, clears the
        # earlier pass so the next plain run doesn't report it as cached.
        entry_path.unlink(missing_ok=True)


GateStatus = Literal["passed", "failed", "cached", "timeout", "couldnt_run"]

# How long to wait for a killed process group's pipe to drain before giving up on it.
_DRAIN_S = 5


@dataclass(frozen=True)
class GateRun:
    """One captured gate command: `exit` is None when it timed out or couldn't start."""

    status: GateStatus
    exit: int | None
    output: str
    duration_s: float


def _capture(argv: Sequence[str], cwd: Path, timeout_s: int) -> GateRun:
    """Run `argv` in its own process group with stdout and stderr combined.

    A timeout kills the whole group, so a grandchild holding the pipe can't outlive it.
    """
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
        msg = f"gate-if-changed: couldn't run {argv[0]!r}: {exc}\n"
        return GateRun("couldnt_run", None, msg, time.monotonic() - start)
    try:
        with _kill_on_termination(proc):
            raw, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_group(proc.pid)
        try:
            raw, _ = proc.communicate(timeout=_DRAIN_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            raw = b""
        text = raw.decode("utf-8", errors="replace")
        text += f"\ngate-if-changed: timed out after {timeout_s} s; killed\n"
        return GateRun("timeout", None, text, time.monotonic() - start)
    code = proc.returncode
    text = raw.decode("utf-8", errors="replace")
    return GateRun("passed" if code == 0 else "failed", code, text, time.monotonic() - start)


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_gate_capture(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_s: int,
    max_age_s: int | None = None,
    no_cache: bool = False,
    clock: Callable[[], float] = time.time,
) -> GateRun:
    """`run_gate_if_changed` for `revgate task`: same key, entries, lock, and log.

    Returns the combined output instead of streaming it. Only a pass is cached; a failure,
    a timeout, or a command that couldn't start clears any earlier pass for the key.
    """
    if not argv:
        return GateRun("couldnt_run", None, "gate-if-changed: empty command\n", 0.0)
    if toplevel(cwd) is None:
        return _capture(argv, cwd, timeout_s)
    try:
        top = safe_toplevel(cwd)
    except SafetyError as exc:
        return GateRun("couldnt_run", None, f"gate-if-changed: {exc}\n", 0.0)
    try:
        cfg = load_gate_cache_config(top)
    except ConfigError:
        cfg = GateCacheConfig()
    max_age = cfg.max_age_s if max_age_s is None else max_age_s
    state = state_dir(top)
    key = gate_key(argv, top, cwd, cfg.env_allowlist, state / "scratch")
    entry_path = state / "gate" / f"{key.digest}.json"
    log_path = state / "gate" / "runs.jsonl"
    with _locked(state / "gate" / f"{key.digest}.lock"):
        entry = None if no_cache else _read_entry(entry_path)
        if entry is not None and clock() - entry[0] <= max_age:
            saved = round(entry[1])
            stamp = time.strftime("%H:%M", time.localtime(entry[0]))
            line = f"gate-if-changed: PASS (cached {stamp}, tree {key.tree[:7]}, saved {saved} s)\n"
            append_jsonl_locked(log_path, _log_row(clock(), key, argv, True, 0, 0.0, saved))
            return GateRun("cached", 0, line, 0.0)
        run = _capture(argv, cwd, timeout_s)
        passed = run.status == "passed" and _tree_unchanged(top, key, state)
        _store(entry_path, key, argv, passed, run.duration_s, clock())
        code = run.exit if run.exit is not None else -1
        append_jsonl_locked(log_path, _log_row(clock(), key, argv, False, code, run.duration_s, 0))
        return run


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
