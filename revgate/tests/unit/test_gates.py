from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path

import pytest
from conftest import MakeRepo, RepoFixture

from revgate.config import Config, ConfigError, load_config
from revgate.gate_cache import run_gate_capture
from revgate.gates import (
    GatePhaseResult,
    expand,
    parse_failed_tests,
    placeholders,
    record_wave_gate,
    run_gate_phase,
    wave_gate_failures,
)
from revgate.model import Grade, Source
from revgate.store import state_dir

PY = shlex.quote(sys.executable)

BASE_FILES = {
    "src/pkg/a.py": "A = 1\n",
    "tests/test_a.py": "def test_a() -> None:\n    pass\n",
    "web/src/x.ts": "export const x = 1;\n",
    "web/src/x.test.ts": "test('x', () => {});\n",
    "other/y.ts": "export const y = 1;\n",
    "README.md": "readme\n",
}


def one_liner(code: str, *extra: str) -> str:
    """A `[gates.task]` command line running Python code."""
    return " ".join([PY, "-c", shlex.quote(code), *extra])


def review_toml(commands: list[str], timeout: int = 600) -> str:
    lines = ",\n".join(f"  {json.dumps(cmd)}" for cmd in commands)
    return f"[budget]\ngate_timeout_seconds = {timeout}\n\n[gates.task]\ncommands = [\n{lines}\n]\n"


def repo_with(make_repo: MakeRepo, commands: list[str], timeout: int = 600) -> RepoFixture:
    return make_repo(BASE_FILES, {}, review_toml=review_toml(commands, timeout))


def phase(
    fx: RepoFixture,
    *,
    changed: tuple[str, ...] = ("src/pkg/a.py",),
    owned: tuple[str, ...] = ("src/pkg/a.py", "tests/test_a.py"),
    owns_test: tuple[str, ...] = ("tests/test_a.py",),
    runs: tuple[str, ...] = (),
    cfg: Config | None = None,
) -> tuple[Path, GatePhaseResult]:
    state = state_dir(fx.path)
    result = run_gate_phase(
        fx.path,
        cfg=cfg or load_config(fx.path),
        state=state,
        base=fx.base,
        changed=changed,
        owned=owned,
        owns_test=owns_test,
        runs=runs,
    )
    return state, result


# --- expand ------------------------------------------------------------------------------


def test_empty_placeholder_expands_to_none() -> None:
    assert expand("uv run ruff check {py_files}", {"py_files": []}) is None


def test_placeholder_token_splices_the_list() -> None:
    got = expand("uv run ruff check {py_files}", {"py_files": ["a.py", "b c.py"]})
    assert got == ["uv", "run", "ruff", "check", "a.py", "b c.py"]


def test_template_without_placeholders_expands_to_its_tokens() -> None:
    assert expand("uv run mypy src 'a b'", {}) == ["uv", "run", "mypy", "src", "a b"]


def test_placeholder_inside_other_text_is_a_config_error() -> None:
    with pytest.raises(ConfigError):
        expand("uv run pytest --files={py_files}", {"py_files": ["a.py"]})


def test_unknown_placeholder_token_is_a_config_error() -> None:
    with pytest.raises(ConfigError):
        expand("uv run ruff check {py_file}", {"py_files": ["a.py"]})


def test_braces_inside_code_are_left_alone() -> None:
    got = expand(one_liner("print({1: 2})"), {"py_files": []})
    assert got is not None and got[-1] == "print({1: 2})"


# --- placeholders ------------------------------------------------------------------------


def test_placeholders_split_files_by_language_and_role(make_repo: MakeRepo) -> None:
    fx = make_repo(BASE_FILES, {})
    cfg = load_config(fx.path)
    ph = placeholders(
        fx.path,
        changed=["src/pkg/a.py", "web/src/x.ts", "other/y.ts", "gone.py", "README.md"],
        owned=["tests/test_a.py", "src/pkg/a.py", "web/src/x.test.ts"],
        owns_test=["tests/test_a.py"],
        runs=["tests/test_b.py", "tests/test_a.py"],
        cfg=cfg,
    )
    assert ph["py_files"] == ["src/pkg/a.py", "tests/test_a.py"]  # gone.py doesn't exist
    assert ph["py_src_files"] == ["src/pkg/a.py"]
    assert ph["py_test_files"] == ["tests/test_a.py"]
    assert ph["ts_files"] == ["web/src/x.test.ts", "web/src/x.ts"]  # not other/y.ts
    assert ph["ts_test_files"] == ["web/src/x.test.ts"]
    assert ph["run_tests"] == ["tests/test_a.py", "tests/test_b.py"]


# --- the gate phase ----------------------------------------------------------------------


def test_passing_command_passes_and_a_second_run_is_cached(make_repo: MakeRepo) -> None:
    fx = repo_with(make_repo, [one_liner("pass")])
    _, first = phase(fx)
    _, second = phase(fx)
    assert [r.status for r in first.results] == ["passed"]
    assert [r.status for r in second.results] == ["cached"]
    assert not second.failed() and not second.findings


def test_command_with_an_empty_placeholder_is_skipped(make_repo: MakeRepo) -> None:
    fx = repo_with(make_repo, [one_liner("raise SystemExit(1)", "{ts_files}")])
    _, result = phase(fx)
    (res,) = result.results
    assert res.status == "skipped" and res.argv is None
    assert not result.failed()


@pytest.mark.parametrize("flag", ["--prefix web", "-p web"])
def test_web_command_is_skipped_unless_the_task_touches_web(make_repo: MakeRepo, flag: str) -> None:
    fx = repo_with(make_repo, [one_liner("raise SystemExit(1)", flag)])
    _, skipped = phase(fx)
    assert [r.status for r in skipped.results] == ["skipped"]
    _, ran = phase(fx, changed=("web/src/x.ts",))
    assert [r.status for r in ran.results] == ["failed"]


def test_failing_command_gives_one_gate_failed_finding(make_repo: MakeRepo) -> None:
    code = (
        "print('FAILED tests/test_a.py::test_a - assert 1 == 2'); "
        "print('FAILED tests/test_a.py::test_b - boom'); raise SystemExit(1)"
    )
    template = one_liner(code, "{run_tests}")
    fx = repo_with(make_repo, [template])
    _, result = phase(fx)
    (res,) = result.results
    assert res.status == "failed" and res.exit == 1
    assert res.failed_tests == ("tests/test_a.py::test_a", "tests/test_a.py::test_b")
    assert res.new_failures == res.failed_tests
    assert "FAILED tests/test_a.py::test_a" in res.tail
    (f,) = result.findings
    assert f.rule == "gate.failed"
    assert f.grade is Grade.E0_EXECUTED and f.source is Source.POLICY
    assert f.file == "src/pkg/a.py"  # the first owned path
    assert "exit 1" in f.evidence
    assert "tests/test_a.py::test_a" in f.evidence
    assert PY in f.evidence or sys.executable in f.evidence
    assert result.failed()
    _, again = phase(fx)
    assert again.findings[0].id == f.id


def test_failure_already_failing_on_the_wave_base_gives_no_finding(make_repo: MakeRepo) -> None:
    code = "print('FAILED tests/test_a.py::test_a - assert 1 == 2'); raise SystemExit(1)"
    fx = repo_with(make_repo, [one_liner(code)])
    record_wave_gate(state_dir(fx.path), fx.base, ["tests/test_a.py::test_a"])
    _, result = phase(fx)
    (res,) = result.results
    assert res.status == "failed" and res.new_failures == ()
    assert result.findings == () and not result.failed()


def test_failure_with_a_new_test_among_old_ones_gives_a_finding(make_repo: MakeRepo) -> None:
    code = (
        "print('FAILED tests/test_a.py::test_a - old'); "
        "print('FAILED tests/test_a.py::test_new - new'); raise SystemExit(1)"
    )
    fx = repo_with(make_repo, [one_liner(code)])
    record_wave_gate(state_dir(fx.path), fx.base, ["tests/test_a.py::test_a"])
    _, result = phase(fx)
    (res,) = result.results
    assert res.new_failures == ("tests/test_a.py::test_new",)
    assert len(result.findings) == 1


def test_failure_without_test_ids_always_gives_a_finding(make_repo: MakeRepo) -> None:
    fx = repo_with(make_repo, [one_liner("raise SystemExit(4)")])
    record_wave_gate(state_dir(fx.path), fx.base, ["tests/test_a.py::test_a"])
    _, result = phase(fx)
    assert len(result.findings) == 1
    assert "exit 4" in result.findings[0].evidence


def test_parse_failed_tests_reads_errors_and_suite_failures() -> None:
    output = (
        "ERROR tests/test_b.py::test_setup - fixture 'db' not found\n"
        "ERROR tests/test_c.py - ImportError: no module named x\n"
        " FAIL  src/x.test.ts [ src/x.test.ts ]\n"
        "FAILED tests/test_a.py::test_old - flaky\n"
    )
    assert parse_failed_tests(output) == (
        "tests/test_b.py::test_setup",
        "tests/test_c.py",
        "src/x.test.ts",
        "tests/test_a.py::test_old",
    )


@pytest.mark.parametrize(
    "extra",
    [
        "print('ERROR tests/test_b.py::test_setup - fixture not found'); ",
        "print(' FAIL  src/x.test.ts [ src/x.test.ts ]'); ",
        "print('FAIL something the parser has never seen'); ",
        "print('=== 1 failed, 1 error in 0.30s ==='); ",
    ],
)
def test_a_known_failure_does_not_mask_an_unparsed_one(make_repo: MakeRepo, extra: str) -> None:
    code = extra + "print('FAILED tests/test_a.py::test_a - old'); raise SystemExit(1)"
    fx = repo_with(make_repo, [one_liner(code)])
    record_wave_gate(state_dir(fx.path), fx.base, ["tests/test_a.py::test_a"])
    _, result = phase(fx)
    assert len(result.findings) == 1, extra
    assert result.failed()


def test_command_over_its_timeout_couldnt_run(make_repo: MakeRepo) -> None:
    template = one_liner("import time; time.sleep(30)")
    fx = repo_with(make_repo, [template], timeout=1)
    start = time.monotonic()
    _, result = phase(fx)
    assert time.monotonic() - start < 15
    (res,) = result.results
    assert res.status == "timeout" and res.exit is None
    assert result.couldnt_run == (template,)
    assert result.findings == ()


def test_missing_executable_couldnt_run(make_repo: MakeRepo) -> None:
    template = "definitely-not-a-command-xyz {py_files}"
    fx = repo_with(make_repo, [template])
    _, result = phase(fx)
    (res,) = result.results
    assert res.status == "couldnt_run" and res.exit is None
    assert result.couldnt_run == (template,)


# --- run_gate_capture --------------------------------------------------------------------


def test_capture_timeout_kills_the_process_group_and_is_never_cached(
    git_repo: Path,
) -> None:
    # The child starts a grandchild that holds the output pipe; only a group kill ends it.
    code = (
        "import subprocess, sys; "
        "subprocess.run([sys.executable, '-c', 'import time; time.sleep(30)'])"
    )
    argv = [sys.executable, "-c", code]
    start = time.monotonic()
    run = run_gate_capture(argv, cwd=git_repo, timeout_s=1)
    assert run.status == "timeout" and run.exit is None
    assert time.monotonic() - start < 15
    assert not list((git_repo / ".git" / "revgate" / "gate").glob("*.json"))


def test_capture_collects_combined_output(git_repo: Path) -> None:
    code = "import sys; print('out'); print('err', file=sys.stderr); raise SystemExit(2)"
    run = run_gate_capture([sys.executable, "-c", code], cwd=git_repo, timeout_s=60)
    assert run.status == "failed" and run.exit == 2
    assert "out" in run.output and "err" in run.output


def test_capture_pass_then_cached(git_repo: Path) -> None:
    argv = [sys.executable, "-c", "print('hi')"]
    first = run_gate_capture(argv, cwd=git_repo, timeout_s=60)
    second = run_gate_capture(argv, cwd=git_repo, timeout_s=60)
    assert (first.status, first.exit) == ("passed", 0) and "hi" in first.output
    assert (second.status, second.exit) == ("cached", 0)
    third = run_gate_capture(argv, cwd=git_repo, timeout_s=60, no_cache=True)
    assert third.status == "passed"


def test_capture_missing_executable(git_repo: Path) -> None:
    run = run_gate_capture(["definitely-not-a-command-xyz"], cwd=git_repo, timeout_s=60)
    assert run.status == "couldnt_run" and run.exit is None
    assert "definitely-not-a-command-xyz" in run.output


def test_capture_refuses_a_home_root(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(git_repo))
    run = run_gate_capture([sys.executable, "-c", "pass"], cwd=git_repo, timeout_s=60)
    assert run.status == "couldnt_run"


# --- failed test ids and the wave gate record -------------------------------------------

PYTEST_SUMMARY = """\
tests/test_a.py .F.                                                      [100%]
=========================== short test summary info ============================
FAILED tests/test_a.py::test_two - AssertionError: assert 1 == 2
FAILED tests/test_b.py::test_param[x-1] - ValueError
ERROR tests/test_c.py - ImportError
========================= 2 failed, 1 passed in 0.12s ==========================
"""

VITEST_OUTPUT = """\
 \x1b[31mFAIL\x1b[39m  src/review/beatSlots.test.ts > subBeatSeparators > marks a 3:1 split
AssertionError: expected [] to deeply equal [ '.' ]
 FAIL  src/a.test.ts > adds > one
 FAIL  src/a.test.ts > adds > one
 Test Files  2 failed (2)
"""


def test_parse_failed_tests_reads_pytest_and_vitest() -> None:
    assert parse_failed_tests(PYTEST_SUMMARY) == (
        "tests/test_a.py::test_two",
        "tests/test_b.py::test_param[x-1]",
        "tests/test_c.py",
    )
    assert parse_failed_tests(VITEST_OUTPUT) == (
        "src/review/beatSlots.test.ts > subBeatSeparators > marks a 3:1 split",
        "src/a.test.ts > adds > one",
    )
    assert parse_failed_tests("all good\n") == ()


def test_wave_gate_record_round_trips(tmp_path: Path) -> None:
    sha = "a" * 40
    path = record_wave_gate(tmp_path, sha, ["t::b", "t::a", "t::a"])
    assert path == tmp_path / "wave-gates" / f"{sha}.json"
    assert wave_gate_failures(tmp_path, sha) == frozenset({"t::a", "t::b"})
    assert wave_gate_failures(tmp_path, "b" * 40) == frozenset()
    path.write_text("{not json")
    assert wave_gate_failures(tmp_path, sha) == frozenset()


def test_wave_gate_record_refuses_a_bad_sha(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        record_wave_gate(tmp_path, "../evil", [])
