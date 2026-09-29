"""Tests for the QA swarm's tester guard hook (claude/hooks/qa_tester_guard.py)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "claude" / "hooks" / "qa_tester_guard.py"


def run_hook(payload: object, active: Path | None) -> tuple[int, str]:
    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}
    if active is not None:
        env["QA_ACTIVE_FILE"] = str(active)
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.returncode, proc.stdout.strip()


def bash(command: str, cwd: str = "/tmp") -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd, "agent_id": "a1"}


def write(path: str) -> dict[str, object]:
    return {"tool_name": "Write", "tool_input": {"file_path": path, "content": "x"}, "cwd": "/tmp"}


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    root = tmp_path / "qa-20260929T101010-ab12"
    (root / "testers" / "qa-1").mkdir(parents=True)
    return root


@pytest.fixture
def active(tmp_path: Path, run_dir: Path) -> Path:
    file = tmp_path / "qa-active.json"
    file.write_text(
        json.dumps({"run_dir": str(run_dir), "repo_root": "/repo/app", "ports": [5173]})
    )
    return file


def denied(out: str) -> str:
    reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert isinstance(reason, str)
    return reason


def test_allows_a_harmless_command(active: Path) -> None:
    assert run_hook(bash("ls -la"), active) == (0, "")


@pytest.mark.parametrize(
    "command",
    [
        "curl -H 'X-API-Key: dev-key' http://127.0.0.1:5173/v1/health",
        "cat .env",
        "cat ~/Developer/organist_bot/.env",
        "cat env/private/api-key",
        "uv run python scripts/extract_hymn.py x",
        "uv run python scripts/eval_extraction.py",
        "env",
        "printenv ANTHROPIC_API_KEY",
        "echo $TYPESAFE_API_KEY",
        "scripts/qa_env.sh stop --run-dir x",
        "scripts/qa_env.sh seed --run-dir x",
        "git push origin main",
        "git -C /repo commit -m x",
        "npm --prefix /r/tools/qa-cli exec -- playwright-cli close-all",
        "cd /r/testers/qa-1 && npm --prefix /c exec -- playwright-cli -s=qa-1-1 kill-all",
        "playwright-cli 'close-all'",
    ],
)
def test_denies_static_patterns_even_with_no_active_file(command: str) -> None:
    code, out = run_hook(bash(command), None)
    assert code == 0 and out, command
    assert denied(out)


@pytest.mark.parametrize(
    "command",
    [
        "python -c 'import os; print(os.environ.get(\"HOME\"))'",
        "echo .envrc",
        "scripts/qa_env.sh scenario-run --run-dir x --tester qa-1 draft.py",
        "ls env/vite-cache",
        "npm --prefix /c exec -- playwright-cli -s=qa-1-1 close",
        "npm --prefix /c exec -- playwright-cli list",
        "echo close-all",
    ],
)
def test_does_not_flag_lookalikes(command: str, active: Path) -> None:
    assert run_hook(bash(command), active) == (0, "")


def test_denies_a_loopback_port_that_is_not_the_runs(active: Path) -> None:
    code, out = run_hook(bash("curl http://127.0.0.1:8002/v1/health"), active)
    assert code == 0 and "8002" in denied(out)
    assert run_hook(bash("curl http://127.0.0.1:5173/"), active) == (0, "")


def test_write_outside_the_testers_folder_is_denied(active: Path, run_dir: Path) -> None:
    assert denied(run_hook(write("/repo/app/src/x.py"), active)[1])
    assert run_hook(write(str(run_dir / "testers" / "qa-1" / "note.md")), active) == (0, "")


def test_bash_redirect_into_the_repository_is_denied(active: Path, run_dir: Path) -> None:
    assert denied(run_hook(bash("echo x > /repo/app/src/x.py"), active)[1])
    assert denied(run_hook(bash("sed -i s/a/b/ /repo/app/README.md"), active)[1])
    inside = run_dir / "testers" / "qa-1" / "note.md"
    assert run_hook(bash(f"echo x > {inside}"), active) == (0, "")
    assert run_hook(bash("ls 2>&1 > /dev/null"), active) == (0, "")


def test_every_command_is_logged_under_the_attributed_tester(active: Path, run_dir: Path) -> None:
    folder = run_dir / "testers" / "qa-1"
    run_hook(bash(f"ls {folder}"), active)
    run_hook(bash("ls /tmp"), active)
    run_hook(bash(f"cat {folder}/../../env/private/x"), active)
    rows = [json.loads(line) for line in (folder / "commands.log").read_text().splitlines()]
    assert rows[0]["decision"] == "allow" and rows[0]["agent_id"] == "a1"
    unattributed = run_dir / "testers" / "_unattributed" / "commands.log"
    assert "ls /tmp" in unattributed.read_text()


def test_bad_input_and_missing_or_malformed_active_file_fail_open(
    tmp_path: Path, active: Path
) -> None:
    assert run_hook("not json", active) == (0, "")
    assert run_hook(bash("ls"), tmp_path / "missing.json") == (0, "")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert run_hook(bash("ls"), bad) == (0, "")
    # The static patterns still apply, and the run-dependent checks are skipped.
    assert denied(run_hook(bash("git push"), bad)[1])
    assert run_hook(bash("curl http://127.0.0.1:8002/"), bad) == (0, "")


def test_a_folder_name_with_no_trailing_slash_is_still_attributed(
    active: Path, run_dir: Path
) -> None:
    folder = run_dir / "testers" / "qa-1"
    run_hook(bash(f"ls {folder}"), active)  # the path ends at the folder name
    assert (folder / "commands.log").is_file()


def test_a_denial_prints_even_when_the_log_cant_be_written(tmp_path: Path) -> None:
    file = tmp_path / "qa-active.json"
    file.write_text(
        json.dumps({"run_dir": "/proc/nope/qa", "repo_root": "/repo/app", "ports": [5173]})
    )
    code, out = run_hook(bash("git push"), file)
    assert code == 0 and denied(out)


def test_an_unwritable_log_folder_never_breaks_the_hook(tmp_path: Path) -> None:
    file = tmp_path / "qa-active.json"
    file.write_text(
        json.dumps({"run_dir": "/proc/nope/qa", "repo_root": "/repo/app", "ports": [5173]})
    )
    assert run_hook(bash("ls"), file) == (0, "")


def test_other_tools_pass(active: Path) -> None:
    assert run_hook({"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}}, active) == (
        0,
        "",
    )


# Hardening beyond the plan's list: bypasses of the same rules.


@pytest.mark.parametrize(
    "command",
    [
        "env|grep KEY",
        "/usr/bin/env",
        "echo $(printenv)",
        "set",
        "export -p",
        "declare -x",
        "ls; export",
        "compgen -e",
        "cat /proc/self/environ",
        "git -c user.name=x commit -m y",
        "git --no-pager push",
        "scripts/qa_env.sh --run-dir x stop",
        "uv run python -m scripts.qa_env stop",
    ],
)
def test_denies_bypasses_of_the_static_patterns(command: str) -> None:
    assert denied(run_hook(bash(command), None)[1]), command


@pytest.mark.parametrize(
    "command",
    [
        "set -euo pipefail",
        "export FOO=1",
        "declare -x FOO=1",
        "git status",
        "git log --oneline -3",
        "ls env/vite-cache",
    ],
)
def test_does_not_flag_harmless_builtins(command: str, active: Path) -> None:
    assert run_hook(bash(command), active) == (0, ""), command


@pytest.mark.parametrize(
    "command",
    ["curl http://LOCALHOST:8002/", "curl http://127.0.1.1:8002/", "curl http://[::1]:8002/"],
)
def test_denies_other_loopback_spellings(command: str, active: Path) -> None:
    assert "8002" in denied(run_hook(bash(command), active)[1])


@pytest.mark.parametrize(
    "command",
    [
        "cd /repo/app && echo x > src/x.py",
        "cd /repo/app; touch notes.md",
        "rm /repo/app/a.py /tmp/b",
        "echo x | tee -a /repo/app/log.txt",
        "cp /tmp/a /repo/app/src/",
        "sudo mv /tmp/a /repo/app/b",
    ],
)
def test_denies_writes_into_the_repository(command: str, active: Path) -> None:
    assert "inside the repository" in denied(run_hook(bash(command), active)[1]), command


def test_writes_from_inside_the_repository_cwd_are_checked(active: Path, run_dir: Path) -> None:
    assert denied(run_hook(bash("touch notes.md", cwd="/repo/app"), active)[1])
    inside = run_dir / "testers" / "qa-1"
    assert run_hook(bash(f"cd {inside} && touch notes.md", cwd="/repo/app"), active) == (0, "")


def test_edit_tools_follow_the_write_rule(active: Path, run_dir: Path) -> None:
    edit = {"tool_name": "Edit", "tool_input": {"file_path": "/repo/app/a.py"}, "cwd": "/tmp"}
    assert denied(run_hook(edit, active)[1])
    note = {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": "/repo/app/n.ipynb"}}
    assert denied(run_hook(note, active)[1])


@pytest.mark.parametrize("path", ["/repo/app/.env", "/home/u/Developer/organist_bot/.env"])
def test_a_read_or_write_of_a_denied_path_is_denied_with_no_active_file(path: str) -> None:
    for tool in ("Read", "Write"):
        payload = {"tool_name": tool, "tool_input": {"file_path": path}, "cwd": "/tmp"}
        assert denied(run_hook(payload, None)[1]), (tool, path)


def test_write_content_does_not_pick_the_log_folder(active: Path, run_dir: Path) -> None:
    target = run_dir / "testers" / "qa-1" / "a.md"
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target), "content": "see testers/qa-9/"},
        "cwd": "/tmp",
    }
    assert run_hook(payload, active) == (0, "")
    assert (run_dir / "testers" / "qa-1" / "commands.log").is_file()
    assert not (run_dir / "testers" / "qa-9").exists()


def test_malformed_ports_and_folders_fail_safe(tmp_path: Path) -> None:
    file = tmp_path / "qa-active.json"
    file.write_text(json.dumps({"run_dir": 5, "repo_root": None, "ports": 5173}))
    assert denied(run_hook(bash("git push"), file)[1])
    assert "8002" in denied(run_hook(bash("curl http://127.0.0.1:8002/"), file)[1])
    assert run_hook(bash("ls"), file) == (0, "")


def test_the_frontmatter_form_exits_zero_when_the_hook_file_is_missing(tmp_path: Path) -> None:
    command = f'f="{tmp_path}/missing.py"; [ -f "$f" ] || exit 0; python3 "$f" || exit 0'
    proc = subprocess.run(["sh", "-c", command], input="{}", capture_output=True, text=True)
    assert (proc.returncode, proc.stdout) == (0, "")


# Wave-review fixes.


def _load_hook() -> object:
    import importlib.util

    spec = importlib.util.spec_from_file_location("qa_tester_guard_under_test", HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_an_unknown_user_home_never_turns_off_the_checks(active: Path) -> None:
    for command in ("touch ~nosuchuser/x; rm -r src", "touch ~nosuchuser/x"):
        assert denied(run_hook(bash(command, cwd="/repo/app"), active)[1]), command
    assert "inside the repository" in denied(
        run_hook(bash("touch ~nosuchuser/x; rm -r src", cwd="/repo/app"), active)[1]
    )


def test_an_unexpected_error_fails_closed_with_a_valid_pointer(
    active: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io

    hook = _load_hook()

    def boom(payload: object, active: object) -> str | None:
        raise KeyError("boom")

    monkeypatch.setattr(hook, "decide", boom)
    monkeypatch.setenv("QA_ACTIVE_FILE", str(active))
    for payload in (bash("ls"), write("/tmp/x"), {"tool_name": "Read", "tool_input": {}}):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        assert hook.main() == 0  # type: ignore[attr-defined]
        assert "unexpected error" in denied(capsys.readouterr().out)


def test_an_unexpected_error_with_no_pointer_still_fails_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io

    hook = _load_hook()

    def boom(payload: object, active: object) -> str | None:
        raise KeyError("boom")

    monkeypatch.setattr(hook, "decide", boom)
    monkeypatch.setenv("QA_ACTIVE_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(bash("ls"))))
    assert hook.main() == 0  # type: ignore[attr-defined]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "command",
    [
        'npm --prefix /repo/app/web exec -- playwright-cli -s=qa-1-1 eval "() => document.title"',
        "echo 'a > b'",
        "python -c 'print(1>0)'",
        "node -e 'const f = (a) => a'",
        "grep -E 'a->b' /tmp/x.log",
        'echo "x; rm src"',
        '"/repo/app/scripts/qa_env.sh" scenario-run --run-dir x --tester qa-1 d.py',
        "'/repo/app/scripts/qa_env.sh' scenario-run --run-dir x --tester qa-1 d.py",
        "ls /repo/app/deploy/env",
        "cat /repo/app/deploy/env/README.md",
        "git diff HEAD~1",
        "git show HEAD",
        "curl http://127.0.0.1:5173/",
        "curl http://127.1:5173/",
        "nc -z localhost 5173",
        "cp /repo/app/README.md /tmp/x",
        "cp -t /tmp/out /repo/app/a.py /repo/app/b.py",
        "ps -o pid,comm",
    ],
)
def test_does_not_flag_quoted_text_or_read_only_forms(command: str, active: Path) -> None:
    assert run_hook(bash(command, cwd="/repo/app"), active) == (0, ""), command


def test_a_quoted_redirect_target_is_still_checked(active: Path) -> None:
    assert denied(run_hook(bash('echo x > "/repo/app/a b.py"'), active)[1])
    assert denied(run_hook(bash("echo 'y' > /repo/app/a.py"), active)[1])


@pytest.mark.parametrize(
    "command",
    [
        "git checkout -- src/x.py",
        "git restore src/x.py",
        "git reset --hard",
        "git stash",
        "git clean -fdx",
        "git apply /tmp/p.diff",
        "git switch main",
        "git rebase main",
        "git -C /repo/app checkout .",
    ],
)
def test_denies_git_verbs_that_edit_or_hide_the_tree(command: str) -> None:
    assert denied(run_hook(bash(command), None)[1]), command


@pytest.mark.parametrize(
    "command",
    [
        "python -c 'import os; print(dict(os.environ))'",
        "python3 -c 'import os; print(os.environ)'",
        "node -e 'console.log(process.env)'",
        "node -e 'console.log(JSON.stringify(process.env))'",
        "perl -e 'print %ENV'",
        "php -r 'print_r(getenv());'",
        "ps eww",
        "ps auxe",
        "ps -E",
        "sh -c env",
        "bash -c 'printenv'",
        "ls\nenv",
        "if true; then env; fi",
        "cat .en[v]",
        "cat .e''nv",
        'cat .e"n"v',
        "cat .en?",
        "cat .e\\nv",
    ],
)
def test_denies_environment_dumps_and_obfuscated_env_files(command: str) -> None:
    assert denied(run_hook(bash(command), None)[1]), command


@pytest.mark.parametrize(
    "command",
    [
        "python -c 'import os; print(os.environ.get(\"HOME\"))'",
        "node -e 'console.log(process.env.HOME)'",
        "python -c 'import os; print(os.getenv(\"HOME\"))'",
        "ps -p 1",
    ],
)
def test_allows_single_variable_reads(command: str, active: Path) -> None:
    assert run_hook(bash(command), active) == (0, ""), command


@pytest.mark.parametrize(
    "command",
    [
        "curl http://127.1:8002/",
        "curl http://127.0.1:8002/",
        "curl http://0:8002/",
        "curl http://0.0.0.0:8002/",
        "curl http://2130706433:8002/",
        "curl http://0x7f000001:8002/",
        "curl http://localhost.:8002/",
        "curl http://api.localhost:8002/",
        "curl http://[0:0:0:0:0:0:0:1]:8002/",
        "curl http://[::ffff:7f00:1]:8002/",
        "nc localhost 8002",
        "nc -w 1 127.0.0.1 8002",
        "ncat 127.1 8002",
        "telnet localhost 8002",
        "socat - TCP:localhost:8002",
        "python -c 'import socket; socket.create_connection((\"127.0.0.1\", 8002))'",
    ],
)
def test_denies_more_loopback_spellings(command: str, active: Path) -> None:
    assert "8002" in denied(run_hook(bash(command), active)[1]), command


@pytest.mark.parametrize(
    "command",
    [
        "mv /repo/app/a.py /tmp/a.py",
        "cp -t /repo/app/src /tmp/a.py",
        "cp --target-directory=/repo/app/src /tmp/a.py",
        "install -t /repo/app/bin /tmp/tool",
        "install -d /repo/app/newdir",
    ],
)
def test_denies_more_write_forms(command: str, active: Path) -> None:
    assert "inside the repository" in denied(run_hook(bash(command), active)[1]), command


# Branch-gate fixes: `protected_roots` in the pointer.


@pytest.fixture
def protected(tmp_path: Path, run_dir: Path) -> Path:
    file = tmp_path / "qa-active-protected.json"
    pointer = {
        "run_dir": str(run_dir),
        "repo_root": "/repo/app",
        "ports": [5173],
        "protected_roots": ["/tmp/qa-planted", 7, None, "relative/wt", ""],
    }
    file.write_text(json.dumps(pointer))
    return file


@pytest.mark.parametrize(
    "command",
    [
        "echo x > /tmp/qa-planted/web/src/a.ts",
        "sed -i s/a/b/ /tmp/qa-planted/web/package.json",
        "rm -r /tmp/qa-planted/web",
        "cp /tmp/a /tmp/qa-planted/",
        "cd /tmp/qa-planted && touch x",
    ],
)
def test_bash_writes_into_a_protected_root_are_denied(command: str, protected: Path) -> None:
    assert "inside the repository" in denied(run_hook(bash(command), protected)[1]), command


def test_the_repo_root_stays_protected_alongside_protected_roots(protected: Path) -> None:
    assert denied(run_hook(bash("echo x > /repo/app/a.py"), protected)[1])
    assert denied(run_hook(write("/repo/app/a.py"), protected)[1])


def test_write_tools_into_a_protected_root_are_denied(protected: Path) -> None:
    assert denied(run_hook(write("/tmp/qa-planted/web/src/a.ts"), protected)[1])
    edit = {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/qa-planted/a.ts"}, "cwd": "/"}
    assert denied(run_hook(edit, protected)[1])


def test_write_tools_into_a_protected_root_are_denied_with_no_run_dir(tmp_path: Path) -> None:
    file = tmp_path / "qa-active.json"
    file.write_text(json.dumps({"ports": [5173], "protected_roots": ["/tmp/qa-planted"]}))
    assert denied(run_hook(write("/tmp/qa-planted/a.ts"), file)[1])
    assert run_hook(write("/tmp/elsewhere/a.ts"), file) == (0, "")


def test_protected_roots_ignore_bad_entries_and_allow_other_writes(
    protected: Path, run_dir: Path
) -> None:
    assert run_hook(bash("echo x > /tmp/other/a.txt"), protected) == (0, "")
    assert run_hook(bash("touch relative/wt/x", cwd="/tmp"), protected) == (0, "")
    assert run_hook(bash("cat /tmp/qa-planted/web/src/a.ts"), protected) == (0, "")
    inside = run_dir / "testers" / "qa-1" / "note.md"
    assert run_hook(bash(f"echo x > {inside}"), protected) == (0, "")
    assert run_hook(write(str(inside)), protected) == (0, "")


def test_a_non_list_protected_roots_changes_nothing(tmp_path: Path) -> None:
    file = tmp_path / "qa-active.json"
    pointer = {"repo_root": "/repo/app", "ports": [5173], "protected_roots": "/tmp/qa-planted"}
    file.write_text(json.dumps(pointer))
    assert run_hook(bash("touch /tmp/qa-planted/x"), file) == (0, "")
    assert denied(run_hook(bash("touch /repo/app/x"), file)[1])


# The backend's direct port: only a security-tester may probe it (slice 3).

BACKEND = 6543


def bash_as(command: str, agent_type: str | None) -> dict[str, object]:
    payload = bash(command)
    if agent_type is not None:
        payload["agent_type"] = agent_type
    return payload


@pytest.fixture
def active_with_direct(tmp_path: Path, run_dir: Path) -> Path:
    file = tmp_path / "qa-active-direct.json"
    file.write_text(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "repo_root": "/repo/app",
                "ports": [5173],
                "direct_ports": [BACKEND],
            }
        )
    )
    return file


PROBE = f"curl -s http://127.0.0.1:{BACKEND}/v1/health"


def test_a_security_tester_may_probe_the_direct_backend_port(active_with_direct: Path) -> None:
    assert run_hook(bash_as(PROBE, "security-tester"), active_with_direct) == (0, "")
    nc = f"nc 127.0.0.1 {BACKEND}"
    assert run_hook(bash_as(nc, "security-tester"), active_with_direct) == (0, "")


@pytest.mark.parametrize("agent_type", ["qa-tester", "adversarial-tester", "Explore", "", None])
def test_every_other_agent_and_a_missing_agent_type_is_denied_the_direct_port(
    active_with_direct: Path, agent_type: str | None
) -> None:
    code, out = run_hook(bash_as(PROBE, agent_type), active_with_direct)
    assert code == 0
    reason = denied(out)
    assert str(BACKEND) in reason and "security-tester" in reason


def test_a_non_string_agent_type_is_denied_the_direct_port(active_with_direct: Path) -> None:
    payload = bash(PROBE)
    payload["agent_type"] = ["security-tester"]
    assert denied(run_hook(payload, active_with_direct)[1])


def test_a_security_tester_is_still_denied_every_other_port(active_with_direct: Path) -> None:
    reason = denied(
        run_hook(bash_as("curl http://127.0.0.1:8002/", "security-tester"), active_with_direct)[1]
    )
    assert "8002" in reason and "isn't this run's instance" in reason
    assert run_hook(
        bash_as("curl http://127.0.0.1:5173/", "security-tester"), active_with_direct
    ) == (
        0,
        "",
    )
    both = f"curl http://127.0.0.1:{BACKEND}/ http://127.0.0.1:8002/"
    assert "8002" in denied(run_hook(bash_as(both, "security-tester"), active_with_direct)[1])


def test_a_security_tester_gets_no_other_allowance_from_the_direct_port(
    active_with_direct: Path,
) -> None:
    for command in (
        f"curl -H 'X-API-Key: dev-key' http://127.0.0.1:{BACKEND}/v1/shares",
        "cat env/private/api-key",
        "git push origin main",
        "scripts/qa_env.sh restart --run-dir x",
    ):
        assert denied(run_hook(bash_as(command, "security-tester"), active_with_direct)[1]), command


@pytest.mark.parametrize("value", ["6543", 6543, [True], ["6543"], [6543.5], None, {}])
def test_a_malformed_direct_ports_entry_allows_nothing(
    tmp_path: Path, run_dir: Path, value: object
) -> None:
    file = tmp_path / "qa-active-bad.json"
    file.write_text(
        json.dumps(
            {"run_dir": str(run_dir), "repo_root": "/r", "ports": [5173], "direct_ports": value}
        )
    )
    assert denied(run_hook(bash_as(PROBE, "security-tester"), file)[1])


def test_an_active_file_with_no_direct_ports_denies_the_port_to_everyone(active: Path) -> None:
    assert denied(run_hook(bash_as(PROBE, "security-tester"), active)[1])


def test_a_direct_probe_is_logged_with_the_agent_type(
    active_with_direct: Path, run_dir: Path
) -> None:
    command = f"cd {run_dir}/testers/sec-1 && {PROBE}"
    assert run_hook(bash_as(command, "security-tester"), active_with_direct) == (0, "")
    rows = [
        json.loads(line)
        for line in (run_dir / "testers" / "sec-1" / "commands.log").read_text().splitlines()
    ]
    assert rows[-1]["agent_type"] == "security-tester" and rows[-1]["decision"] == "allow"
    assert PROBE in rows[-1]["command"]
    assert run_hook(bash_as(command, "qa-tester"), active_with_direct)[1]  # denied
    last = json.loads((run_dir / "testers" / "sec-1" / "commands.log").read_text().splitlines()[-1])
    assert last["agent_type"] == "qa-tester" and last["decision"] == "deny"
