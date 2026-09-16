from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parents[2] / "plugin" / "hooks" / "scripts" / "post_pr_created.py"


def run_hook(payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True
    )


def test_extracts_pr_url_from_tool_response() -> None:
    result = run_hook({"tool_response": "Created https://github.com/seby-dev/sebby/pull/42"})

    payload = json.loads(result.stdout)
    assert "https://github.com/seby-dev/sebby/pull/42" in payload["systemMessage"]


def test_falls_back_to_generic_reference_when_no_url_found() -> None:
    result = run_hook({"tool_response": "some unrelated output"})

    payload = json.loads(result.stdout)
    assert "the PR" in payload["systemMessage"]


def test_always_mentions_auto_merge_step() -> None:
    result = run_hook({"tool_response": ""})

    payload = json.loads(result.stdout)
    assert "auto-merge" in payload["systemMessage"]
