from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parents[2] / "plugin" / "hooks" / "scripts" / "stop_quality_check.py"
FIXTURES = Path(__file__).parent / "fixtures"


def run_hook(env_overrides: dict[str, str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path), **env_overrides}
    return subprocess.run(
        [sys.executable, str(HOOK)], input="{}", capture_output=True, text=True, env=env
    )


def test_blocks_when_lint_fails(tmp_path: Path) -> None:
    result = run_hook(
        {"SEBBY_LINT_COMMAND": f"{sys.executable} {FIXTURES / 'fake_lint_fail.py'}"}, tmp_path
    )

    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    assert "fake lint issue" in payload["reason"]


def test_blocks_when_typecheck_fails(tmp_path: Path) -> None:
    result = run_hook(
        {
            "SEBBY_LINT_COMMAND": f"{sys.executable} {FIXTURES / 'fake_lint_pass.py'}",
            "SEBBY_TYPECHECK_COMMAND": f"{sys.executable} {FIXTURES / 'fake_lint_fail.py'}",
        },
        tmp_path,
    )

    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    assert "fake lint issue" in payload["reason"]


def test_silent_when_both_configured_and_passing(tmp_path: Path) -> None:
    result = run_hook(
        {
            "SEBBY_LINT_COMMAND": f"{sys.executable} {FIXTURES / 'fake_lint_pass.py'}",
            "SEBBY_TYPECHECK_COMMAND": f"{sys.executable} {FIXTURES / 'fake_lint_pass.py'}",
        },
        tmp_path,
    )

    assert result.stdout.strip() == ""


def test_skips_typecheck_when_not_configured(tmp_path: Path) -> None:
    result = run_hook(
        {"SEBBY_LINT_COMMAND": f"{sys.executable} {FIXTURES / 'fake_lint_pass.py'}"}, tmp_path
    )

    assert result.stdout.strip() == ""


def test_blocks_with_message_when_lint_command_not_found(tmp_path: Path) -> None:
    result = run_hook({"SEBBY_LINT_COMMAND": "definitely-not-a-real-binary-xyz"}, tmp_path)

    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    assert "could not run" in payload["reason"]
