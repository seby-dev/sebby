#!/usr/bin/env python3
"""PostToolUse hook: after `gh pr create`, nudge the auto-merge step.

Assumes review/quality gates are enforced elsewhere (before or during PR
creation); this hook only drives the post-create merge flow via the `gh`
CLI.
"""

import json
import re
import sys

try:
    payload = json.load(sys.stdin)
    blob = json.dumps(payload.get("tool_response", ""))
    m = re.search(r"https://github\.com/[^\s\"']+/pull/\d+", blob)
    pr_ref = m.group(0) if m else "the PR"
except Exception:
    pr_ref = "the PR"

print(
    json.dumps(
        {
            "systemMessage": (
                f"{pr_ref} created. Finish the ship step (gh CLI):\n"
                "1. Confirm CI: `gh pr checks` (or `gh pr checks --watch`).\n"
                "2. If every check passed, enable squash auto-merge: "
                "`gh pr merge --squash --auto --delete-branch`.\n"
                "3. If any check failed, report it to the user and do NOT merge."
            )
        }
    )
)
