#!/usr/bin/env python3
"""SessionStart hook: redirect a session opened at the project root into a
configured dev worktree.

Configure via SEBBY_WORKTREE_DEV_PATH (absolute path to the worktree to
redirect into). No-ops if unset, or if the session's cwd isn't exactly
CLAUDE_PROJECT_DIR (i.e. already inside a worktree or subdirectory).
"""

import json
import os
import sys


def main() -> None:
    dev_path = os.environ.get("SEBBY_WORKTREE_DEV_PATH")
    if not dev_path:
        sys.exit(0)

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    cwd = payload.get("cwd", "")
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not project_dir or cwd != project_dir:
        sys.exit(0)

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": (
                        "Session started in the project root. Per project convention, run "
                        f"'cd {dev_path}' via Bash now, before any other action, to switch "
                        "into the dev worktree."
                    ),
                }
            }
        )
    )


if __name__ == "__main__":
    main()
