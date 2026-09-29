"""barrier.sh: the QA swarm's file barrier for collision scenarios."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BARRIER = ROOT / "claude" / "skills" / "qa-swarm" / "barrier.sh"
HOOK = ROOT / "claude" / "hooks" / "qa_tester_guard.py"


def start(
    folder: Path,
    name: str,
    count: int,
    timeout: int | None = None,
    env_extra: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    args = ["bash", str(BARRIER), "wait", str(folder), name, str(count)]
    if timeout is not None:
        args.append(str(timeout))
    return subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, **(env_extra or {})},
    )


def finish(procs: list[subprocess.Popen[str]], timeout: float = 30) -> list[int]:
    codes = []
    for proc in procs:
        proc.communicate(timeout=timeout)
        codes.append(proc.returncode)
    return codes


def run(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(BARRIER), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, **(env_extra or {})},
    )


def fields(path: Path) -> dict[str, str]:
    """A `key=value key=value` file, such as `go`, as a dict."""
    return dict(pair.split("=", 1) for pair in path.read_text().split())


def listing(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def lock_owner(folder: Path) -> str:
    """Who holds go.lock: a symlink to the owner's name, or an older directory lock."""
    lock = folder / "go.lock"
    if lock.is_symlink():
        return os.readlink(lock)
    return (lock / "owner").read_text().strip()


@pytest.mark.parametrize("trial", range(10))
def test_five_concurrent_waiters_are_all_released_by_one_decider(
    tmp_path: Path, trial: int
) -> None:
    names = [f"w{i}" for i in range(5)]
    codes = finish([start(tmp_path, name, 5) for name in names])
    assert codes == [0] * 5
    go = fields(tmp_path / "go")
    assert (tmp_path / "go.lock").is_symlink()  # the lock and its owner, one atomic step
    owner = lock_owner(tmp_path)
    assert owner in names and go["by"] == owner and go["count"] == "5"
    for name in names:
        assert (tmp_path / f"ready-{name}").is_file() and (tmp_path / f"acted-{name}").is_file()
    latest_ready = max(int((tmp_path / f"ready-{n}").read_text()) for n in names)
    assert int(go["released_ms"]) >= latest_ready
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]  # no temp file left
    assert not (tmp_path / "abandoned").exists()


def test_a_duplicate_name_counts_once(tmp_path: Path) -> None:
    codes = finish([start(tmp_path, "a", 2, 1), start(tmp_path, "a", 2, 1)])
    assert codes == [75, 75]
    assert (tmp_path / "abandoned").is_file() and not (tmp_path / "go").exists()
    assert [n for n in listing(tmp_path) if n.startswith("ready-")] == ["ready-a"]


def test_a_lone_waiter_times_out_and_abandons_the_barrier(tmp_path: Path) -> None:
    began = time.monotonic()
    proc = start(tmp_path, "solo", 3, 1)
    _, stderr = proc.communicate(timeout=30)
    assert proc.returncode == 75 and "abandoned" in stderr
    assert time.monotonic() - began < 7
    abandoned = fields(tmp_path / "abandoned")
    assert abandoned["by"] == "solo" and abandoned["reason"] == "timeout"


def test_a_later_waiter_on_an_abandoned_barrier_exits_at_once_and_writes_nothing(
    tmp_path: Path,
) -> None:
    assert finish([start(tmp_path, "solo", 3, 1)]) == [75]
    before = listing(tmp_path)
    began = time.monotonic()
    result = run("wait", str(tmp_path), "late", "3", "600")
    assert result.returncode == 75 and time.monotonic() - began < 2
    assert listing(tmp_path) == before


def test_a_late_arriver_after_go_returns_at_once_with_an_acted_file(tmp_path: Path) -> None:
    assert finish([start(tmp_path, "a", 2), start(tmp_path, "b", 2)]) == [0, 0]
    began = time.monotonic()
    result = run("wait", str(tmp_path), "late", "2", "600")
    assert result.returncode == 0 and time.monotonic() - began < 2
    assert (tmp_path / "acted-late").is_file()


def test_a_rerun_after_acting_exits_75_at_once_and_does_not_act_again(tmp_path: Path) -> None:
    assert finish([start(tmp_path, "a", 2), start(tmp_path, "b", 2)]) == [0, 0]

    def contents() -> dict[str, str]:
        return {p.name: p.read_text() for p in tmp_path.iterdir() if not p.is_symlink()}

    before = contents()
    began = time.monotonic()
    result = run("wait", str(tmp_path), "a", "2", "600")
    assert result.returncode == 75 and time.monotonic() - began < 2
    assert "already run" in result.stderr
    assert contents() == before and lock_owner(tmp_path) in ("a", "b")


def test_the_timeout_defaults_to_300_seconds(tmp_path: Path) -> None:
    assert "${5:-300}" in BARRIER.read_text()  # the default, since a real run would take 300 s
    proc = start(tmp_path, "a", 2)  # no timeout argument
    try:
        time.sleep(1.5)
        assert proc.poll() is None  # still waiting
    finally:
        proc.terminate()
        proc.communicate(timeout=10)


def test_a_stalled_decider_still_lets_every_waiter_time_out(tmp_path: Path) -> None:
    (tmp_path / "go.lock").mkdir()  # a decider that took the lock and never wrote an outcome
    (tmp_path / "ready-other").write_text("1\n")
    began = time.monotonic()
    result = run(
        "wait", str(tmp_path), "me", "2", "1"
    )  # the count is met, so only the timeout ends it
    assert result.returncode == 75 and time.monotonic() - began < 1 + 5 + 2 + 2
    assert not (tmp_path / "go").exists() and not (tmp_path / "abandoned").exists()
    assert json.loads(run("spread", str(tmp_path)).stdout)["outcome"] == "pending"


def wait_for(path: Path, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), path


SIGNALS = [signal.SIGTERM, signal.SIGINT, signal.SIGHUP]


@pytest.mark.parametrize("sig", SIGNALS, ids=lambda s: s.name)
def test_a_waiter_stopped_by_a_signal_abandons_the_barrier(
    tmp_path: Path, sig: signal.Signals
) -> None:
    proc = start(tmp_path, "gone", 2)
    wait_for(tmp_path / "ready-gone")
    proc.send_signal(sig)
    proc.communicate(timeout=10)
    assert proc.returncode == 75
    assert (tmp_path / "ready-gone").exists()  # no withdrawal: the barrier is abandoned instead
    abandoned = fields(tmp_path / "abandoned")
    assert abandoned["by"] == "gone" and abandoned["reason"] == "signal"
    assert lock_owner(tmp_path) == "gone"
    # A second participant sees the abandoned barrier and exits at once, not at its timeout.
    began = time.monotonic()
    assert finish([start(tmp_path, "other", 2, 60)]) == [75]
    assert time.monotonic() - began < 10
    assert not (tmp_path / "go").exists() and not (tmp_path / "acted-other").exists()


@pytest.mark.parametrize("sig", SIGNALS, ids=lambda s: s.name)
def test_a_signaled_waiter_leaves_another_deciders_lock_alone(
    tmp_path: Path, sig: signal.Signals
) -> None:
    (tmp_path / "go.lock").mkdir()  # another participant is deciding right now
    (tmp_path / "go.lock" / "owner").write_text("decider\n")
    proc = start(tmp_path, "me", 3)
    wait_for(tmp_path / "ready-me")
    proc.send_signal(sig)
    proc.communicate(timeout=10)
    assert proc.returncode == 75
    assert not (tmp_path / "abandoned").exists() and not (tmp_path / "go").exists()
    assert (tmp_path / "go.lock" / "owner").read_text().strip() == "decider"


@pytest.mark.parametrize("sig", SIGNALS, ids=lambda s: s.name)
def test_a_signaled_decider_that_holds_the_lock_writes_abandoned(
    tmp_path: Path, sig: signal.Signals
) -> None:
    (tmp_path / "go.lock").mkdir()  # this waiter's own lock, with no outcome written yet
    (tmp_path / "go.lock" / "owner").write_text("me\n")
    proc = start(tmp_path, "me", 3)
    wait_for(tmp_path / "ready-me")
    proc.send_signal(sig)
    proc.communicate(timeout=10)
    assert proc.returncode == 75
    assert fields(tmp_path / "abandoned")["by"] == "me" and not (tmp_path / "go").exists()


def shim_dir(tmp_path: Path, tool: str, body: str) -> Path:
    """A folder holding a `tool` shim, put first on PATH; `$REAL` runs the real tool."""
    real = shutil.which(tool)
    assert real is not None, tool
    bin_dir = tmp_path / "shims"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / tool
    shim.write_text(f"#!/bin/sh\nREAL={real}\n{body}\n")
    shim.chmod(0o755)
    return bin_dir


def shim_path(bin_dir: Path) -> dict[str, str]:
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def test_a_signaled_waiter_that_cant_write_abandoned_says_so(tmp_path: Path) -> None:
    # The shim fails the rename that would put `abandoned` in place.
    body = (
        'for last; do :; done\ncase "${last##*/}" in abandoned) exit 1 ;; esac\nexec "$REAL" "$@"'
    )
    env = shim_path(shim_dir(tmp_path, "mv", body))
    folder = tmp_path / "barrier"
    folder.mkdir()
    proc = start(folder, "gone", 2, env_extra=env)
    wait_for(folder / "ready-gone")
    proc.send_signal(signal.SIGTERM)
    _, stderr = proc.communicate(timeout=10)
    assert proc.returncode == 75
    assert f"barrier.sh: couldn't record the abandon in {folder}" in stderr
    assert not (folder / "abandoned").exists()
    assert not [p for p in folder.iterdir() if p.name.startswith(".")]  # no temp file left


# How a waiter reaches `claim`: as the decider once the count is met, or at its timeout.
CLAIM_PATHS = {"count": (1, 60), "timeout": (2, 1)}


@pytest.mark.parametrize("path", ["decider", "follower"])
def test_a_waiter_stopped_while_writing_acted_drops_it_and_exits_75(
    tmp_path: Path, path: str
) -> None:
    # The shim renames, then sleeps once the acted- file is in place: a TERM in that window
    # arrives after the rename and before the waiter returns, so the action never ran.
    body = (
        'for last; do :; done\n"$REAL" "$@" || exit\n'
        'case "${last##*/}" in acted-*) sleep 10 ;; esac'
    )
    env = shim_path(shim_dir(tmp_path, "mv", body))
    folder = tmp_path / "barrier"
    folder.mkdir()
    count = 1 if path == "decider" else 2
    proc = start(folder, "me", count, 60, env_extra=env)
    others = [] if path == "decider" else [start(folder, "other", 2, 60, env_extra=env)]
    wait_for(folder / "acted-me", 20)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=20)
    assert proc.returncode == 75
    assert not (folder / "acted-me").exists()
    assert finish(others) == [0] * len(others)
    out = json.loads(run("spread", str(folder)).stdout)
    assert out["outcome"] == "go" and "me" not in out["acted"]
    # It leaves a marker, so a forbidden re-run can't act late and alone.
    assert (folder / "stopped-me").is_file() and out["stopped"] == ["me"]
    again = run("wait", str(folder), "me", str(count), "5")
    assert again.returncode == 75 and "stopped after the release" in again.stderr
    assert not (folder / "acted-me").exists()
    assert json.loads(run("spread", str(folder)).stdout)["acted"] == out["acted"]


def test_a_waiter_stopped_before_the_release_leaves_no_stopped_marker(tmp_path: Path) -> None:
    proc = start(tmp_path, "me", 2, 60)
    wait_for(tmp_path / "ready-me", 20)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=20)
    assert proc.returncode == 75 and (tmp_path / "abandoned").is_file()
    assert not (tmp_path / "stopped-me").exists()
    assert json.loads(run("spread", str(tmp_path)).stdout)["stopped"] == []


def test_spread_lists_only_valid_stopped_names(tmp_path: Path) -> None:
    (tmp_path / "go").write_text("released_ms=1 by=a count=2 clock=bash\n")
    (tmp_path / "stopped-b").write_text("1700000000100\n")
    (tmp_path / "stopped-a").write_text("1700000000050\n")
    (tmp_path / 'stopped-x"y').write_text("1\n")  # a name that breaks JSON
    out = json.loads(run("spread", str(tmp_path)).stdout)
    assert out["stopped"] == ["a", "b"]


@pytest.mark.parametrize("path", sorted(CLAIM_PATHS))
def test_a_process_group_signal_inside_the_lock_step_never_leaves_pending(
    tmp_path: Path, path: str
) -> None:
    # The shim takes the lock, then sleeps: a TERM to the whole group kills it too, so `ln`
    # reports a failure although the lock is now this waiter's.
    lock_taken = tmp_path / "lock-taken"
    body = f'"$REAL" "$@" && touch {lock_taken}\nsleep 3'
    env = shim_path(shim_dir(tmp_path, "ln", body))
    folder = tmp_path / "barrier"
    folder.mkdir()
    count, timeout = CLAIM_PATHS[path]
    proc = subprocess.Popen(
        ["bash", str(BARRIER), "wait", str(folder), "me", str(count), str(timeout)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, **env},
        start_new_session=True,
    )
    wait_for(lock_taken, 20)
    began = time.monotonic()
    os.killpg(proc.pid, signal.SIGTERM)
    proc.communicate(timeout=30)
    assert time.monotonic() - began < 15  # not the timeout plus the grace period
    assert proc.returncode == 75
    assert lock_owner(folder) == "me"
    out = json.loads(run("spread", str(folder)).stdout)
    assert out["outcome"] in ("abandoned", "go"), out
    if out["outcome"] == "abandoned":
        assert fields(folder / "abandoned")["reason"] == "signal"
    assert not (folder / "acted-me").exists()


def test_a_parent_check_that_gets_eperm_keeps_waiting(tmp_path: Path) -> None:
    # `kill -0` fails with EPERM for a live parent owned by another user: not an orphan.
    eperm = (
        '() { if [ "$1" = -0 ]; then : >"$QA_EPERM_MARK";'
        ' echo "kill: ($2) - Operation not permitted" >&2; return 1; fi; builtin kill "$@"; }'
    )
    mark = tmp_path.parent / f"{tmp_path.name}-eperm-called"
    # An exported function, which bash prefers to a builtin; the marker proves bash imported it.
    env = {"BASH_FUNC_kill%%": eperm, "QA_EPERM_MARK": str(mark)}
    result = run("wait", str(tmp_path), "a", "1", "5", env_extra=env)
    assert mark.is_file(), "the kill shim never ran, so this test proves nothing"
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "acted-a").is_file() and not (tmp_path / "abandoned").exists()


def test_a_waiter_whose_parent_pid_was_reused_does_not_act(tmp_path: Path) -> None:
    # The parent pid answers `kill -0`, but this waiter's parent is now someone else.
    body = 'case "$*" in *ppid=*) echo "    1"; exit 0 ;; esac\nexec "$REAL" "$@"'
    env = shim_path(shim_dir(tmp_path, "ps", body))
    folder = tmp_path / "barrier"
    folder.mkdir()
    result = run("wait", str(folder), "a", "1", "5", env_extra=env)
    assert result.returncode == 75 and "orphaned" in result.stderr
    assert not (folder / "acted-a").exists()


def barrier_pids(folder: Path) -> list[int]:
    """The pids of the barrier.sh processes waiting on `folder`."""
    out = subprocess.run(
        ["ps", "-ww", "-eo", "pid=,args="], capture_output=True, text=True, check=True
    ).stdout
    pids = []
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        if "barrier.sh wait" in args and str(folder) in args and not args.startswith("bash -c"):
            pids.append(int(pid))
    return pids


def test_an_orphaned_waiter_abandons_the_barrier_and_never_acts(tmp_path: Path) -> None:
    clicked = tmp_path / "clicked"
    folder = tmp_path / "barrier"
    folder.mkdir()
    outer = subprocess.Popen(
        ["bash", "-c", f"bash {BARRIER} wait {folder} A 2 30 && touch {clicked}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(folder / "ready-A")
        orphans = barrier_pids(folder)
        assert orphans, "the inner barrier.sh isn't running"
        outer.terminate()  # only the outer shell, as a tool timeout that kills the shell does
        outer.wait(timeout=10)
        wait_for(folder / "abandoned", 5)
        deadline = time.monotonic() + 5
        while barrier_pids(folder) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not barrier_pids(folder), "the orphan is still waiting"
        abandoned = fields(folder / "abandoned")
        assert abandoned["by"] == "A" and abandoned["reason"] == "orphaned"
        assert not (folder / "acted-A").exists() and not clicked.exists()
    finally:
        for pid in barrier_pids(folder):
            os.kill(pid, signal.SIGKILL)
        if outer.poll() is None:
            outer.kill()


@pytest.mark.parametrize(
    "args",
    [
        ["wait", "{dir}", "../x", "2"],
        ["wait", "{dir}", "", "2"],
        ["wait", "{dir}", "a b", "2"],
        ["wait", "{dir}", "-a", "2"],
        ["wait", "{dir}", "a" * 65, "2"],
        ["wait", "{dir}", "a", "0"],
        ["wait", "{dir}", "a", "17"],
        ["wait", "{dir}", "a", "x"],
        ["wait", "{dir}", "a", ""],
        ["wait", "{dir}", "a", "2", "0"],
        ["wait", "{dir}", "a", "2", "601"],
        ["wait", "{dir}", "a", "2", "x"],
        ["wait", "{dir}", "a"],
        ["wait", "{dir}", "a", "2", "5", "extra"],
        ["wait", "{dir}/missing", "a", "2"],
        ["spread"],
        ["spread", "{dir}/missing"],
        ["frobnicate", "{dir}"],
        [],
    ],
)
def test_usage_errors_exit_64_and_create_nothing(tmp_path: Path, args: list[str]) -> None:
    result = run(*[a.replace("{dir}", str(tmp_path)) for a in args])
    assert result.returncode == 64 and "usage:" in result.stderr
    assert listing(tmp_path) == []


def _clock_or_skip(clock: str) -> None:
    if clock == "perl" and shutil.which("perl") is None:
        pytest.skip("perl isn't installed")
    if clock == "python3" and shutil.which("python3") is None:
        pytest.skip("python3 isn't installed")
    if clock == "bash":
        probe = subprocess.run(
            ["bash", "-c", 'echo "${EPOCHREALTIME:-}"'], capture_output=True, text=True
        )
        if not probe.stdout.strip():
            pytest.skip("this bash has no EPOCHREALTIME (bash 5 only)")


def _clock_values(folder: Path, clock: str) -> list[int]:
    before = time.time() * 1000
    result = run("wait", str(folder), "a", "1", "5", env_extra={"QA_BARRIER_CLOCK": clock})
    assert result.returncode == 0, result.stderr
    values = []
    for path in (folder / "ready-a", folder / "acted-a"):
        text = path.read_text().strip()
        assert text.isdigit(), (path.name, text)
        values.append(int(text))
    go = fields(folder / "go")
    values.append(int(go["released_ms"]))
    for value in values:
        assert abs(value - before) < 10000, (clock, value)  # loose: a loaded machine
    assert go["clock"] == clock
    assert json.loads(run("spread", str(folder)).stdout)["clock"] == clock
    return values


@pytest.mark.parametrize("clock", ["perl", "python3", "bash"])
def test_each_millisecond_clock_gives_integer_milliseconds_near_now(
    tmp_path: Path, clock: str
) -> None:
    _clock_or_skip(clock)
    values = _clock_values(tmp_path, clock)
    # A forced clock falls back only to `date`, whose whole seconds end in 000. Three values
    # that all end in 000 by chance is a one-in-a-billion event, so this proves the clock ran.
    assert any(v % 1000 for v in values), (clock, values)


def test_the_date_clock_gives_whole_seconds_in_milliseconds(tmp_path: Path) -> None:
    values = _clock_values(tmp_path, "date")
    assert all(v % 1000 == 0 for v in values), values


def test_a_forced_clock_that_fails_falls_back_to_date_not_epochrealtime(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("bash", "date", "mv", "ln", "readlink", "ps", "sleep", "rm"):
        found = shutil.which(tool)
        assert found is not None, tool
        (bin_dir / tool).symlink_to(found)
    folder = tmp_path / "barrier"
    folder.mkdir()
    env = {"PATH": str(bin_dir), "QA_BARRIER_CLOCK": "perl"}  # no perl on this PATH
    result = subprocess.run(
        [str(bin_dir / "bash"), str(BARRIER), "wait", str(folder), "a", "1", "5"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert int((folder / "acted-a").read_text()) % 1000 == 0
    assert int(fields(folder / "go")["released_ms"]) % 1000 == 0
    assert fields(folder / "go")["clock"] == "date"  # the clock that ran, not the one forced


def test_spread_reports_a_released_barrier(tmp_path: Path) -> None:
    names = ["a", "b", "c"]
    assert finish([start(tmp_path, n, 3) for n in names]) == [0, 0, 0]
    out = json.loads(run("spread", str(tmp_path)).stdout)
    acted = {n: int((tmp_path / f"acted-{n}").read_text()) for n in names}
    assert out["outcome"] == "go" and out["count"] == 3 and out["acted"] == acted
    assert out["released_count"] == 3 == len(out["acted"])
    assert out["spread_ms"] == max(acted.values()) - min(acted.values())
    assert out["skipped"] == 0 and out["clock"] in ("bash", "perl", "python3", "date")


def test_spread_on_an_empty_folder_is_pending(tmp_path: Path) -> None:
    result = run("spread", str(tmp_path))
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "outcome": "pending",
        "count": 0,
        "released_count": None,
        "acted": {},
        "spread_ms": None,
        "clock": None,
        "skipped": 0,
        "stopped": [],
    }


def test_spread_skips_stray_files_and_always_prints_valid_json(tmp_path: Path) -> None:
    (tmp_path / "go").write_text("released_ms=1700000000000 by=a count=2 clock=perl\n")
    (tmp_path / "acted-good").write_text("1700000000100\n")
    (tmp_path / "acted-late").write_text("1700000000150\n")
    (tmp_path / 'acted-x"y').write_text("1700000000200\n")  # a name that breaks JSON
    (tmp_path / "acted-zero").write_text("0012\n")  # a leading zero: octal in bash
    (tmp_path / "acted-nine").write_text("09\n")  # an invalid octal
    (tmp_path / "acted-huge").write_text("9" * 20 + "\n")  # past 64-bit arithmetic
    (tmp_path / "acted-").write_text("1700000000300\n")  # an empty name
    (tmp_path / "junk").write_text("hello\n")
    result = run("spread", str(tmp_path))
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["acted"] == {"good": 1700000000100, "late": 1700000000150}
    assert out["spread_ms"] == 50 and out["released_count"] == 2 and out["outcome"] == "go"
    assert out["skipped"] == 5 and out["clock"] == "perl"  # every bad name and value, not junk


@pytest.mark.parametrize(
    "go",
    [
        "released_ms=1 by=a\n",
        "released_ms=1 by=a count=0x1 clock=sundial\n",
        'released_ms=1 by=a count=x clock=b"a\n',
        "",
    ],
)
def test_spread_gives_a_null_released_count_and_clock_for_a_go_without_valid_ones(
    tmp_path: Path, go: str
) -> None:
    (tmp_path / "go").write_text(go)
    out = json.loads(run("spread", str(tmp_path)).stdout)
    assert out["outcome"] == "go" and out["released_count"] is None and out["clock"] is None


def test_spread_on_an_abandoned_folder_says_so(tmp_path: Path) -> None:
    assert finish([start(tmp_path, "solo", 3, 1)]) == [75]
    out = json.loads(run("spread", str(tmp_path)).stdout)
    assert out["outcome"] == "abandoned" and out["count"] == 1 and out["spread_ms"] is None
    assert out["released_count"] is None and out["clock"] is None


def test_the_script_passes_bash_n() -> None:
    result = subprocess.run(["bash", "-n", str(BARRIER)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_the_script_passes_shellcheck_when_it_is_installed() -> None:
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck isn't installed")
    result = subprocess.run(
        ["shellcheck", "-s", "bash", str(BARRIER)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout


def run_hook(payload: dict[str, object], active: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "QA_ACTIVE_FILE": str(active)},
    )
    return proc.returncode, proc.stdout.strip()


@pytest.fixture
def active(tmp_path: Path) -> tuple[Path, Path]:
    run_dir = tmp_path / "qa-20260929T101010-ab12"
    (run_dir / "testers" / "adv-1").mkdir(parents=True)
    (run_dir / "shared" / "double-publish").mkdir(parents=True)
    pointer = tmp_path / "qa-active.json"
    pointer.write_text(
        json.dumps({"run_dir": str(run_dir), "repo_root": "/repo/app", "ports": [5173]})
    )
    return run_dir, pointer


def test_the_guard_allows_the_documented_tester_command(active: tuple[Path, Path]) -> None:
    run_dir, pointer = active
    command = (
        f"cd {run_dir}/testers/adv-1 && bash {BARRIER} wait {run_dir}/shared/double-publish "
        "adv-1 2 600 && npm --prefix /repo/app/tools/qa-cli exec -- playwright-cli "
        "-s=adv-1-1 click e12"
    )
    payload: dict[str, object] = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": "/repo/app",
    }
    assert run_hook(payload, pointer) == (0, "")
    rows = (run_dir / "testers" / "adv-1" / "commands.log").read_text().splitlines()
    assert json.loads(rows[-1])["decision"] == "allow"


def test_the_guard_still_denies_a_write_into_the_shared_folder(active: tuple[Path, Path]) -> None:
    run_dir, pointer = active
    target = run_dir / "shared" / "double-publish" / "go"
    payload: dict[str, object] = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target), "content": "x"},
    }
    code, out = run_hook(payload, pointer)
    reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert code == 0 and "limited to" in reason
