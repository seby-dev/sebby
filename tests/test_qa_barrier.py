"""barrier.sh: the QA swarm's file barrier for collision scenarios."""

from __future__ import annotations

import json
import os
import shutil
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


@pytest.mark.parametrize("trial", range(10))
def test_five_concurrent_waiters_are_all_released_by_one_decider(
    tmp_path: Path, trial: int
) -> None:
    names = [f"w{i}" for i in range(5)]
    codes = finish([start(tmp_path, name, 5) for name in names])
    assert codes == [0] * 5
    go = fields(tmp_path / "go")
    owner = (tmp_path / "go.lock" / "owner").read_text().strip()
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
    assert fields(tmp_path / "abandoned")["by"] == "solo"


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


def test_a_waiter_stopped_by_a_signal_withdraws_its_arrival(tmp_path: Path) -> None:
    proc = start(tmp_path, "gone", 2)
    ready = tmp_path / "ready-gone"
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.exists()
    proc.terminate()
    proc.communicate(timeout=10)
    assert proc.returncode == 75 and not ready.exists()
    # The second participant doesn't see a phantom arrival: it times out alone.
    assert finish([start(tmp_path, "other", 2, 1)]) == [75]
    assert (tmp_path / "abandoned").is_file()


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


@pytest.mark.parametrize("clock", ["perl", "python3", "bash"])
def test_each_clock_gives_integer_milliseconds_near_now(tmp_path: Path, clock: str) -> None:
    _clock_or_skip(clock)
    before = time.time() * 1000
    result = run("wait", str(tmp_path), "a", "1", "5", env_extra={"QA_BARRIER_CLOCK": clock})
    assert result.returncode == 0, result.stderr
    for path in (tmp_path / "ready-a", tmp_path / "acted-a"):
        text = path.read_text().strip()
        assert text.isdigit() and abs(int(text) - before) < 5000, (path.name, text)
    assert abs(int(fields(tmp_path / "go")["released_ms"]) - before) < 5000


def test_spread_reports_a_released_barrier(tmp_path: Path) -> None:
    names = ["a", "b", "c"]
    assert finish([start(tmp_path, n, 3) for n in names]) == [0, 0, 0]
    out = json.loads(run("spread", str(tmp_path)).stdout)
    acted = {n: int((tmp_path / f"acted-{n}").read_text()) for n in names}
    assert out["outcome"] == "go" and out["count"] == 3 and out["acted"] == acted
    assert out["spread_ms"] == max(acted.values()) - min(acted.values())


def test_spread_on_an_empty_folder_is_pending(tmp_path: Path) -> None:
    result = run("spread", str(tmp_path))
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "outcome": "pending",
        "count": 0,
        "acted": {},
        "spread_ms": None,
    }


def test_spread_on_an_abandoned_folder_says_so(tmp_path: Path) -> None:
    assert finish([start(tmp_path, "solo", 3, 1)]) == [75]
    out = json.loads(run("spread", str(tmp_path)).stdout)
    assert out["outcome"] == "abandoned" and out["count"] == 1 and out["spread_ms"] is None


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
