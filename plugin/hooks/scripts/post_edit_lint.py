#!/usr/bin/env python3
"""PostToolUse hook: run a configurable lint command after each file write/edit.

Configure via the SEBBY_LINT_COMMAND environment variable (shell-split; the
project root is appended as the last argument) — defaults to `ruff check
--output-format=concise`.
"""

import json
import os
import shlex
import subprocess
from pathlib import Path

DEFAULT_LINT_COMMAND = "ruff check --output-format=concise"


def build_command(proj_root: Path) -> list[str]:
    raw = os.environ.get("SEBBY_LINT_COMMAND", DEFAULT_LINT_COMMAND)
    return [*shlex.split(raw), str(proj_root)]


def main() -> None:
    proj_root = Path(os.environ.get("CLAUDE_PROJECT_DIR", "."))
    command = build_command(proj_root)

    result = subprocess.run(command, capture_output=True, text=True, cwd=proj_root)

    if result.returncode != 0 and result.stdout.strip():
        lines = result.stdout.strip().splitlines()
        preview = "\n".join(lines[:20])
        if len(lines) > 20:
            preview += f"\n… ({len(lines) - 20} more issues)"
        message = f"Lint found issues — fix before committing:\n{preview}"
        print(json.dumps({"systemMessage": message}))


if __name__ == "__main__":
    main()
