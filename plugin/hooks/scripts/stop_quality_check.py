#!/usr/bin/env python3
"""Stop hook: run a configurable lint + type-check when Claude stops; block
if issues remain.

Configure via SEBBY_LINT_COMMAND (see post_edit_lint.py) and
SEBBY_TYPECHECK_COMMAND (shell-split; the project root is NOT auto-appended
— pass the target path explicitly, e.g. "mypy src"). If
SEBBY_TYPECHECK_COMMAND is unset, the type-check step is skipped entirely.
"""

import json
import os
import shlex
import subprocess
from pathlib import Path

DEFAULT_LINT_COMMAND = "ruff check --output-format=concise"


def _run_and_collect(command: list[str], cwd: Path, label: str, issues: list[str]) -> None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, cwd=cwd)
    except (FileNotFoundError, OSError) as exc:
        issues.append(f"{label}: could not run `{' '.join(command)}` ({exc})")
        return
    if result.returncode != 0 and result.stdout.strip():
        lines = result.stdout.strip().splitlines()
        preview = "\n".join(lines[:20])
        if len(lines) > 20:
            preview += f"\n… ({len(lines) - 20} more)"
        issues.append(f"{label}:\n{preview}")


def main() -> None:
    proj_root = Path(os.environ.get("CLAUDE_PROJECT_DIR", "."))
    issues: list[str] = []

    lint_command = [
        *shlex.split(os.environ.get("SEBBY_LINT_COMMAND", DEFAULT_LINT_COMMAND)),
        str(proj_root),
    ]
    _run_and_collect(lint_command, proj_root, "lint", issues)

    typecheck_raw = os.environ.get("SEBBY_TYPECHECK_COMMAND")
    if typecheck_raw:
        _run_and_collect(shlex.split(typecheck_raw), proj_root, "typecheck", issues)

    if issues:
        reason = "Quality gate failed — fix these before finishing:\n\n" + "\n\n".join(issues)
        print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    main()
