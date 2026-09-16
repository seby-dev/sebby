from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parents[2] / "plugin" / "hooks" / "scripts" / "worktree_redirect.py"


def run_hook(
    payload: dict[str, object], env_overrides: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **env_overrides}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def test_redirects_when_session_starts_at_project_root(tmp_path: Path) -> None:
    result = run_hook(
        {"cwd": str(tmp_path)},
        {"CLAUDE_PROJECT_DIR": str(tmp_path), "SEBBY_WORKTREE_DEV_PATH": str(tmp_path / "dev")},
    )

    payload = json.loads(result.stdout)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert str(tmp_path / "dev") in context
    assert "SessionStart" == payload["hookSpecificOutput"]["hookEventName"]


def test_noop_when_cwd_is_not_project_root(tmp_path: Path) -> None:
    result = run_hook(
        {"cwd": str(tmp_path / "subdir")},
        {"CLAUDE_PROJECT_DIR": str(tmp_path), "SEBBY_WORKTREE_DEV_PATH": str(tmp_path / "dev")},
    )

    assert result.stdout.strip() == ""


def test_noop_when_env_var_unset(tmp_path: Path) -> None:
    result = run_hook({"cwd": str(tmp_path)}, {"CLAUDE_PROJECT_DIR": str(tmp_path)})

    assert result.stdout.strip() == ""
