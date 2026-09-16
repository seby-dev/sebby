from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parents[2] / "plugin" / "hooks" / "scripts" / "post_edit_lint.py"
FIXTURES = Path(__file__).parent / "fixtures"


def run_hook(env_overrides: dict[str, str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path), **env_overrides}
    return subprocess.run(
        [sys.executable, str(HOOK)], input="{}", capture_output=True, text=True, env=env
    )


def test_reports_issues_when_lint_command_fails(tmp_path: Path) -> None:
    result = run_hook(
        {"SEBBY_LINT_COMMAND": f"{sys.executable} {FIXTURES / 'fake_lint_fail.py'}"}, tmp_path
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert "fake lint issue" in payload["systemMessage"]


def test_silent_when_lint_command_passes(tmp_path: Path) -> None:
    result = run_hook(
        {"SEBBY_LINT_COMMAND": f"{sys.executable} {FIXTURES / 'fake_lint_pass.py'}"}, tmp_path
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_defaults_to_ruff_when_no_command_configured(tmp_path: Path) -> None:
    result = run_hook({}, tmp_path)

    assert result.returncode == 0


def test_reports_message_when_lint_command_not_found(tmp_path: Path) -> None:
    result = run_hook({"SEBBY_LINT_COMMAND": "definitely-not-a-real-binary-xyz"}, tmp_path)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert "Could not run lint command" in payload["systemMessage"]
