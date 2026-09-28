"""Tests for the ~/.claude hooks versioned in claude/hooks/."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "claude" / "hooks"


def run_hook(name: str, payload: object, env: dict[str, str] | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(HOOKS / name)],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.returncode, proc.stdout.strip()


def agent(tool_input: dict[str, object]) -> dict[str, object]:
    return {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_input": tool_input}


def test_typed_agent_passes_silently(tmp_path: Path) -> None:
    env = {"XDG_STATE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    assert run_hook(
        "agent_model_guard.py", agent({"subagent_type": "reader", "prompt": "x"}), env
    ) == (0, "")


def test_general_purpose_with_model_passes(tmp_path: Path) -> None:
    env = {"XDG_STATE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    payload = agent({"subagent_type": "general-purpose", "model": "sonnet", "prompt": "x"})
    assert run_hook("agent_model_guard.py", payload, env) == (0, "")


def test_untyped_without_model_warns_and_logs(tmp_path: Path) -> None:
    env = {"XDG_STATE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    code, out = run_hook("agent_model_guard.py", agent({"prompt": "x"}), env)
    assert code == 0
    body = json.loads(out)
    assert "permissionDecision" not in body.get("hookSpecificOutput", {})
    assert "inherits" in body["hookSpecificOutput"]["additionalContext"]
    assert (tmp_path / "sebby" / "agent-guard.jsonl").read_text().count("\n") == 1


def test_refuse_mode_denies(tmp_path: Path) -> None:
    env = {"XDG_STATE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "SEBBY_AGENT_GUARD": "refuse"}
    code, out = run_hook("agent_model_guard.py", agent({"subagent_type": "general-purpose"}), env)
    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_builtin_agent_without_model_warns(tmp_path: Path) -> None:
    # Explore and Plan inherit the session's model unless the call passes one.
    env = {
        "XDG_STATE_HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "SEBBY_AGENTS_DIR": str(tmp_path / "agents"),
    }
    for name in ("Explore", "Plan"):
        code, out = run_hook("agent_model_guard.py", agent({"subagent_type": name}), env)
        assert (
            code == 0 and "inherits" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
        )
    payload = agent({"subagent_type": "Explore", "model": "sonnet"})
    assert run_hook("agent_model_guard.py", payload, env) == (0, "")


def test_custom_agent_that_pins_a_model_passes(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "scout.md").write_text("---\nname: scout\nmodel: sonnet\n---\nBody.\n")
    (agents / "loose.md").write_text("---\nname: loose\n---\nBody.\n")
    env = {
        "XDG_STATE_HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "SEBBY_AGENTS_DIR": str(agents),
    }
    assert run_hook("agent_model_guard.py", agent({"subagent_type": "scout"}), env) == (0, "")
    assert run_hook("agent_model_guard.py", agent({"subagent_type": "loose"}), env)[1] != ""


def test_missing_hook_file_never_blocks(tmp_path: Path) -> None:
    # Exit 2 from a PreToolUse hook blocks the tool call, so a missing hook file (the
    # sebby checkout on another branch, mid-rebase, or moved) must exit 0.
    settings = json.loads((HOOKS.parent / "settings-hooks.json").read_text())
    commands = [h["command"] for m in settings["hooks"]["PreToolUse"] for h in m["hooks"]]
    assert len(commands) == 2
    for command in commands:
        proc = subprocess.run(
            ["sh", "-c", command],
            input=json.dumps(bash("ls")),
            capture_output=True,
            text=True,
            env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        )
        assert proc.returncode == 0, command


def test_other_tools_and_bad_input_pass(tmp_path: Path) -> None:
    env = {"XDG_STATE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    bash = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    assert run_hook("agent_model_guard.py", bash, env) == (0, "")
    assert run_hook("agent_model_guard.py", "not json", env) == (0, "")


def bash(command: str) -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


def test_cd_only_gets_a_reminder() -> None:
    for command in ("cd /tmp/x", "  cd 'a b'  ", 'cd "x y";'):
        code, out = run_hook("cd_only_reminder.py", bash(command))
        assert code == 0 and "resets" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_chained_cd_and_other_commands_pass() -> None:
    for command in ("cd /x && ls", "echo cd x", "git -C /x status"):
        assert run_hook("cd_only_reminder.py", bash(command)) == (0, "")
