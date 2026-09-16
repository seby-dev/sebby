from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parents[2] / "plugin" / "hooks" / "scripts" / "scrub_env_transcript.py"


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


def test_redacts_secret_value_from_transcript(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("API_KEY=sk-supersecret123\n")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"text": "using key sk-supersecret123"}\n')

    result = run_hook(
        {
            "tool_name": "Read",
            "tool_input": {"file_path": str(tmp_path / ".env")},
            "transcript_path": str(transcript),
        },
        tmp_path,
    )

    assert result.returncode == 0
    content = transcript.read_text()
    assert "sk-supersecret123" not in content
    assert "[REDACTED:API_KEY]" in content


def test_skips_short_values(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("PORT=8080\nFLAG=true\n")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"text": "listening on 8080 with flag true"}\n')

    run_hook(
        {
            "tool_name": "Read",
            "tool_input": {"file_path": str(tmp_path / ".env")},
            "transcript_path": str(transcript),
        },
        tmp_path,
    )

    content = transcript.read_text()
    assert "8080" in content
    assert "true" in content


def test_stop_hook_variant_scrubs_without_tool_name(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=abcdef123456\n")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"text": "token is abcdef123456"}\n')

    result = run_hook({"transcript_path": str(transcript)}, tmp_path)

    assert result.returncode == 0
    assert "abcdef123456" not in transcript.read_text()


def test_noop_when_no_env_files_exist(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"text": "nothing to scrub here"}\n')
    original = transcript.read_text()

    run_hook({"transcript_path": str(transcript)}, tmp_path)

    assert transcript.read_text() == original
