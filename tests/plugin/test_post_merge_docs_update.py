from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parents[2] / "plugin" / "hooks" / "scripts" / "post_merge_docs_update.py"


def run_hook(
    payload: dict[str, object], env_overrides: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **(env_overrides or {})}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def test_extracts_pr_number_from_command() -> None:
    result = run_hook({"tool_input": {"command": "gh pr merge 42 --squash"}})

    payload = json.loads(result.stdout)
    assert "PR #42" in payload["systemMessage"]
    assert "docs/post-merge-sync-42" in payload["systemMessage"]


def test_falls_back_when_no_pr_number_found() -> None:
    result = run_hook({"tool_input": {"command": "gh pr merge --squash"}})

    payload = json.loads(result.stdout)
    assert "most recently merged PR" in payload["systemMessage"]


def test_uses_configured_docs_files() -> None:
    result = run_hook(
        {"tool_input": {"command": "gh pr merge 1"}},
        env_overrides={"SEBBY_DOCS_FILES": "README.md,ARCHITECTURE.md"},
    )

    payload = json.loads(result.stdout)
    assert "`README.md`" in payload["systemMessage"]
    assert "`ARCHITECTURE.md`" in payload["systemMessage"]


def test_defaults_to_readme_when_docs_files_unconfigured() -> None:
    result = run_hook({"tool_input": {"command": "gh pr merge 1"}})

    payload = json.loads(result.stdout)
    assert "`README.md`" in payload["systemMessage"]
