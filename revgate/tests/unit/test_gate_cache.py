from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import git  # pytest's default import mode puts tests/ on sys.path

from revgate.gate_cache import gate_key, gate_stats, run_gate_if_changed


def counting_cmd(counter: Path) -> list[str]:
    """A command that appends one character to a file outside the repository."""
    code = (
        "import pathlib, sys; p = pathlib.Path(sys.argv[1]); "
        "p.write_text((p.read_text() if p.exists() else '') + 'x')"
    )
    return [sys.executable, "-c", code, str(counter)]


def runs(counter: Path) -> int:
    return len(counter.read_text()) if counter.exists() else 0


def gate(repo: Path, argv: list[str], **kw: object) -> tuple[int, str]:
    out = io.StringIO()
    code = run_gate_if_changed(argv, cwd=repo, out=out, err=out, **kw)  # type: ignore[arg-type]
    return code, out.getvalue()


def test_second_run_on_same_tree_is_a_hit(git_repo: Path, tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    assert gate(git_repo, counting_cmd(counter))[0] == 0
    code, text = gate(git_repo, counting_cmd(counter))
    assert code == 0
    assert runs(counter) == 1
    assert text.startswith("gate-if-changed: PASS (cached ")
    assert ", tree " in text and " s)" in text


def test_same_size_edit_changes_key(git_repo: Path, tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    gate(git_repo, counting_cmd(counter))
    (git_repo / "a.txt").write_text("alphA\n")  # same length: a --stat key would miss this
    gate(git_repo, counting_cmd(counter))
    assert runs(counter) == 2


def test_untracked_file_changes_key(git_repo: Path, tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    gate(git_repo, counting_cmd(counter))
    (git_repo / "new.txt").write_text("n\n")
    gate(git_repo, counting_cmd(counter))
    assert runs(counter) == 2


def test_ignored_file_does_not_change_key(git_repo: Path, tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    gate(git_repo, counting_cmd(counter))
    (git_repo / "ignored").mkdir()
    (git_repo / "ignored" / "x.txt").write_text("x\n")
    gate(git_repo, counting_cmd(counter))
    assert runs(counter) == 1


def test_failure_is_never_cached(git_repo: Path) -> None:
    fail = [sys.executable, "-c", "raise SystemExit(3)"]
    assert gate(git_repo, fail)[0] == 3
    assert gate(git_repo, fail)[0] == 3


def test_expired_entry_reruns(git_repo: Path, tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    gate(git_repo, counting_cmd(counter), clock=lambda: 1000.0)
    gate(git_repo, counting_cmd(counter), clock=lambda: 1000.0 + 86401)
    assert runs(counter) == 2


def test_no_cache_flag_runs_again(git_repo: Path, tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    gate(git_repo, counting_cmd(counter))
    gate(git_repo, counting_cmd(counter), no_cache=True)
    assert runs(counter) == 2


def test_failed_no_cache_run_invalidates_the_pass(git_repo: Path, tmp_path: Path) -> None:
    flag = tmp_path / "fail"
    cmd = [
        sys.executable,
        "-c",
        "import pathlib, sys; raise SystemExit(1 if pathlib.Path(sys.argv[1]).exists() else 0)",
        str(flag),
    ]
    assert gate(git_repo, cmd)[0] == 0
    flag.write_text("")  # something outside the key changed, such as a toolchain
    assert gate(git_repo, cmd, no_cache=True)[0] == 1
    code, text = gate(git_repo, cmd)
    assert code == 1
    assert "PASS (cached" not in text


def test_cwd_is_part_of_the_key(git_repo: Path, tmp_path: Path) -> None:
    (git_repo / "sub").mkdir()
    (git_repo / "sub" / "b.txt").write_text("b\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    top = gate_key(["true"], git_repo, git_repo, (), scratch)
    sub = gate_key(["true"], git_repo, git_repo / "sub", (), scratch)
    assert top.tree == sub.tree and top.digest != sub.digest


def test_env_allowlist_changes_key(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("UV_PYTHON", "3.12")
    one = gate_key(["true"], git_repo, git_repo, ("UV_PYTHON",), scratch)
    monkeypatch.setenv("UV_PYTHON", "3.13")
    two = gate_key(["true"], git_repo, git_repo, ("UV_PYTHON",), scratch)
    assert one.digest != two.digest


def test_tree_hash_on_clean_tree_equals_commit_tree(git_repo: Path, tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    key = gate_key(["true"], git_repo, git_repo, (), scratch)
    assert key.tree == git(git_repo, "rev-parse", "HEAD^{tree}")
    assert list(scratch.iterdir()) == []  # the temporary index is removed


def test_home_root_refused(git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(git_repo))
    counter = tmp_path / "counter"
    code, text = gate(git_repo, counting_cmd(counter))
    assert code == 2 and runs(counter) == 0
    assert "HOME" in text or "home" in text


def test_outside_repo_runs_uncached(tmp_path: Path, git_env: None) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    counter = tmp_path / "counter"
    assert gate(plain, counting_cmd(counter))[0] == 0
    assert gate(plain, counting_cmd(counter))[0] == 0
    assert runs(counter) == 2


def test_missing_executable_exits_2(git_repo: Path) -> None:
    assert gate(git_repo, ["definitely-not-a-command-xyz"])[0] == 2


def test_concurrent_identical_runs_share_one_execution(git_repo: Path, tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    slow = (
        "import pathlib, sys, time; time.sleep(1); p = pathlib.Path(sys.argv[1]); "
        "p.write_text((p.read_text() if p.exists() else '') + 'x')"
    )
    argv = [
        sys.executable,
        "-m",
        "revgate",
        "gate-if-changed",
        "--",
        sys.executable,
        "-c",
        slow,
        str(counter),
    ]
    procs = [subprocess.Popen(argv, cwd=git_repo) for _ in range(2)]
    assert [p.wait() for p in procs] == [0, 0]
    assert runs(counter) == 1


def test_corrupt_entry_is_a_miss(git_repo: Path, tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    assert gate(git_repo, counting_cmd(counter))[0] == 0
    entries = list((git_repo / ".git" / "revgate" / "gate").glob("*.json"))
    assert entries  # the first run stored a pass
    for entry in entries:
        entry.write_text("{not json")
    assert gate(git_repo, counting_cmd(counter))[0] == 0
    assert runs(counter) == 2


def test_stats_reports_hit_rate(git_repo: Path, tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    gate(git_repo, counting_cmd(counter))
    gate(git_repo, counting_cmd(counter))
    out = io.StringIO()
    assert gate_stats(git_repo, out=out) == 0
    assert "1/2" in out.getvalue()
