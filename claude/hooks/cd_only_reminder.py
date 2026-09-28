#!/usr/bin/env python3
"""PreToolUse hook: remind Claude that a Bash call holding only `cd` changes nothing later.

The Bash tool's working directory resets between calls, so a lone `cd` has no effect on
the next command. The hook adds a note and never blocks; it exits 0 on any input.
"""

from __future__ import annotations

import json
import re
import sys

CD_ONLY = re.compile(r"""^\s*cd\s+(?:"[^"]*"|'[^']*'|[^\s;&|]+)\s*;?\s*$""")
MESSAGE = (
    "This Bash call only changes directory, and the working directory resets before the "
    "next call. Chain the dependent command in the same call (cd <path> && <command>), "
    "use an absolute path, or use git -C <path>."
)


def decide(payload: dict[str, object]) -> dict[str, object] | None:
    if payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or not CD_ONLY.match(command):
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": MESSAGE}}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    result = decide(payload)
    if result is not None:
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
