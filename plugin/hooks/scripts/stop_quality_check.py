#!/usr/bin/env python3
"""Stop hook: run a configurable lint + type-check when Claude stops; block
if issues remain.

Configure via SEBBY_LINT_COMMAND (see post_edit_lint.py) and
SEBBY_TYPECHECK_COMMAND (shell-split; the project root is NOT auto-appended
— pass the target path explicitly, e.g. "mypy src"). If
SEBBY_TYPECHECK_COMMAND is unset, the type-check step is skipped entirely.

If Jev (TypeSafe) is available (TYPESAFE_API_KEY set and typesafe_sdk
installed), a Score judgment is appended to the block message to help Claude
triage the severity of the issues. This classification is ADDITIVE ONLY —
it never changes whether the hook blocks; any issues still block unconditionally.
This conservative approach is intentional: reliably distinguishing a definite
type error from a cosmetic lint nit requires more context than the raw tool
output alone provides, and loosening a working safety gate is a higher-risk
change that warrants its own audit.
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


def _jev_severity_note(issues_text: str) -> str:
    """Return a Jev severity label string, or empty string if unavailable.

    Fails open — any ImportError, missing key, or SDK exception is swallowed.
    This is used solely to enrich the block message; it never changes the
    blocking decision.
    """
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        return ""

    try:
        from typesafe_sdk import Score, TypeSafeClient  # type: ignore[import]
    except ImportError:
        return ""

    try:
        client = TypeSafeClient(api_key=api_key, model="jev-latest")
        response = client.system_one(
            {"output": issues_text},
            {
                "severity": Score(
                    instructions=(
                        "Rate how likely this collected lint/typecheck output represents "
                        "a genuine bug or regression (vs. minor style/formatting) that "
                        "must be fixed before considering the task done."
                    ),
                    criteria=["cosmetic", "minor", "likely_bug", "definite_bug"],
                )
            },
        )
        score = response.scores["severity"].score
        return f"\n\nJev severity estimate: {score}"
    except Exception:  # noqa: BLE001
        # Fail open: any network, auth, or SDK error is swallowed silently.
        return ""


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
        issues_text = "\n\n".join(issues)
        severity_note = _jev_severity_note(issues_text)
        reason = (
            "Quality gate failed — fix these before finishing:\n\n" + issues_text + severity_note
        )
        print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    main()
