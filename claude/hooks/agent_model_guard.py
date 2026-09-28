#!/usr/bin/env python3
"""PreToolUse hook: flag an Agent dispatch that names no model-pinning agent and no model.

An untyped `general-purpose` dispatch inherits the session's model; 974 of about 1,753
root dispatches in 14 days did (pipeline spec, P2). Warn mode (the default) adds a note
for Claude and the user; SEBBY_AGENT_GUARD=refuse denies the call. The hook never fails a
dispatch on its own error.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

AGENT_TOOLS = {"Agent", "Task"}
# The agents T08 defines, each of which pins a model. Built-in agents (general-purpose,
# Explore, Plan) inherit the session's model unless the call passes one.
PINNED = frozenset(
    {
        "implementer",
        "implementer-risky",
        "adjudicator",
        "plan-advisor",
        "wave-reviewer",
        "wave-reviewer-domain",
        "branch-reviewer",
        "reader",
    }
)
MODEL_LINE = re.compile(r"(?m)^model:\s*\S")
MESSAGE = (
    "This Agent dispatch names neither an agent that pins a model nor an explicit model, "
    "so it inherits this session's model. Pass subagent_type (implementer, "
    "implementer-risky, reader, wave-reviewer, wave-reviewer-domain, branch-reviewer, "
    "adjudicator, plan-advisor) or an explicit model."
)


def agents_dir() -> Path:
    return Path(os.environ.get("SEBBY_AGENTS_DIR") or Path.home() / ".claude" / "agents")


def pins_model(name: str) -> bool:
    """True for T08's agents and for any agent file whose frontmatter sets `model:`."""
    if name in PINNED:
        return True
    if not name or "/" in name or name.startswith("."):
        return False
    try:
        text = (agents_dir() / f"{name}.md").read_text(encoding="utf-8")
    except OSError:
        return False
    return MODEL_LINE.search(text.split("\n---", 1)[0]) is not None


def decide(payload: dict[str, object], mode: str) -> dict[str, object] | None:
    if payload.get("tool_name") not in AGENT_TOOLS:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    if tool_input.get("model") or pins_model(str(tool_input.get("subagent_type") or "")):
        return None
    if mode == "refuse":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": MESSAGE,
            }
        }
    return {
        "systemMessage": MESSAGE,
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": MESSAGE},
    }


def log_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "sebby" / "agent-guard.jsonl"


def append_log(payload: dict[str, object], mode: str) -> None:
    tool_input = payload.get("tool_input")
    description = tool_input.get("description") if isinstance(tool_input, dict) else None
    row = {
        "ts": datetime.now(UTC).isoformat(),
        "mode": mode,
        "session": payload.get("session_id"),
        "description": description,
    }
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    mode = os.environ.get("SEBBY_AGENT_GUARD", "warn")
    result = decide(payload, mode)
    if result is None:
        return 0
    try:
        append_log(payload, mode)
    except (OSError, RuntimeError):
        # RuntimeError: Path.home() fails when HOME is unset; the log is best effort.
        pass
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
