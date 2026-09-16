from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parents[2] / "plugin" / "hooks" / "scripts" / "block_env_git.py"


def run_hook(payload: dict[str, object], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(cwd)}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )


def test_denies_git_add_of_env_file(tmp_path: Path) -> None:
    result = run_hook({"tool_input": {"command": "git add .env"}}, tmp_path)

    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_denies_forced_add_of_env_file(tmp_path: Path) -> None:
    result = run_hook({"tool_input": {"command": "git add -f .env.production"}}, tmp_path)

    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_allows_normal_git_add(tmp_path: Path) -> None:
    result = run_hook({"tool_input": {"command": "git add src/main.py"}}, tmp_path)

    assert result.stdout.strip() == ""


def test_ignores_non_git_commands(tmp_path: Path) -> None:
    result = run_hook({"tool_input": {"command": "cat .env"}}, tmp_path)

    assert result.stdout.strip() == ""


def test_denies_glob_pattern_env_add(tmp_path: Path) -> None:
    result = run_hook({"tool_input": {"command": "git add -f .env*"}}, tmp_path)

    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_denies_broad_add_when_env_file_exists(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=1\n")

    result = run_hook({"tool_input": {"command": "git add -A"}}, tmp_path)

    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_allows_broad_add_when_no_env_file_exists(tmp_path: Path) -> None:
    result = run_hook({"tool_input": {"command": "git add -A"}}, tmp_path)

    assert result.stdout.strip() == ""


def test_warns_instead_of_silently_allowing_when_git_diff_fails(tmp_path: Path) -> None:
    # tmp_path has no .git directory, so `git diff --cached` fails naturally.
    result = run_hook({"tool_input": {"command": "git commit -m test"}}, tmp_path)

    output = json.loads(result.stdout)
    assert "could not verify" in output["systemMessage"].lower()
