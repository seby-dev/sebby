#!/usr/bin/env python3
"""PreToolUse hook: semantic check for destructive or privilege-escalating Bash commands.

Uses Jev (TypeSafe) to detect commands that may permanently delete data or
escalate privileges — a second opinion beyond the static glob denylist in
settings.json.

Requirements for Jev enhancement to activate:
  - `typesafe_sdk` must be importable (optional; hook fails open if absent)
  - TYPESAFE_API_KEY must be set in the environment

If either is missing, or if the API call raises any exception, the hook exits
silently without producing any output (fail-open), and the static denylist
in settings.json remains the hard backstop.
"""

import json
import os
import re
import sys

# Tokens that suggest a command might be destructive. This cheap pre-filter
# prevents a network round-trip on every trivial `ls` or `git status`.
_RISK_TOKENS: frozenset[str] = frozenset(
    [
        "rm",
        "DROP",
        "sudo",
        "dd",
        "kill",
        "pkill",
        "truncate",
        ">",
        "git branch -D",
        "find",
        "-delete",
        "shred",
        "wipe",
        "mkfs",
        "format",
        "fdisk",
        "parted",
        "hdparm",
        "chmod",
        "chown",
    ]
)

_RISK_PATTERN = re.compile(
    r"\brm\b"
    r"|\bDROP\b"
    r"|\bsudo\b"
    r"|\bdd\b"
    r"|\bkill\b"
    r"|\bpkill\b"
    r"|\btruncate\b"
    r"|(?<![0-9a-zA-Z_])>"  # redirect operator (not inside alphanumerics)
    r"|\|[^|]*sh\b"  # pipe to a shell
    r"|\bgit\s+branch\s+-D\b"
    r"|\bfind\b.*-delete\b"
    r"|\bshred\b"
    r"|\bwipe\b"
    r"|\bmkfs\b"
    r"|\bfdisk\b"
    r"|\bparted\b"
)


def _is_risk_suggestive(command: str) -> bool:
    return bool(_RISK_PATTERN.search(command))


def deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def warn(message: str) -> None:
    print(json.dumps({"systemMessage": message}))


def _jev_check(command: str, project_dir: str) -> None:
    """Run Jev semantic check. Fails open — any exception is swallowed."""
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        return

    try:
        from typesafe_sdk import Noul, TypeSafeClient  # type: ignore[import]
    except ImportError:
        return

    try:
        client = TypeSafeClient(api_key=api_key, model="jev-latest")
        response = client.system_one(
            {"command": command, "project_dir": project_dir},
            {
                "irreversible_data_loss": Noul(
                    instructions=(
                        "Does this command permanently delete, overwrite, or truncate "
                        "files/data such that it can't be trivially undone?"
                    )
                ),
                "privilege_or_scope_escalation": Noul(
                    instructions=(
                        "Does this command escalate privileges or act outside "
                        "the current project directory?"
                    )
                ),
            },
        )
        irr_conf = response.nouls["irreversible_data_loss"].noul
        priv_conf = response.nouls["privilege_or_scope_escalation"].noul

        if irr_conf >= 0.9:
            deny(
                f"Blocked (Jev): this command appears likely to permanently delete or "
                f"overwrite data (confidence {irr_conf:.0%}). "
                "Use a safer approach or confirm this is intentional."
            )
        if priv_conf >= 0.9:
            deny(
                f"Blocked (Jev): this command appears likely to escalate privileges or "
                f"act outside the project directory (confidence {priv_conf:.0%}). "
                "Use a safer approach or confirm this is intentional."
            )

        max_conf = max(irr_conf, priv_conf)
        if max_conf >= 0.6:
            flag = (
                "irreversible data loss" if irr_conf >= priv_conf else "privilege/scope escalation"
            )
            warn(
                f"Warning (Jev): this command may involve {flag} "
                f"(confidence {max_conf:.0%}). Confirm this is intentional."
            )
    except Exception:  # noqa: BLE001
        # Fail open: any network, auth, or SDK error is swallowed silently.
        pass


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    command: str = (payload.get("tool_input") or {}).get("command", "")
    if not command:
        sys.exit(0)

    # Cheap pre-filter: skip Jev round-trip for obviously benign commands.
    if not _is_risk_suggestive(command):
        sys.exit(0)

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", ".")
    _jev_check(command, project_dir)

    sys.exit(0)


if __name__ == "__main__":
    main()
