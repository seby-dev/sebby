#!/usr/bin/env python3
"""PostToolUse hook: after `gh pr merge`, drive a docs-synchronisation pass.

Configure the doc files to check via SEBBY_DOCS_FILES (comma-separated,
default "README.md").
"""

import json
import os
import re
import sys

DEFAULT_DOCS_FILES = "README.md"


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}

    command = (payload.get("tool_input") or {}).get("command", "")
    response = json.dumps(payload.get("tool_response", ""))
    m = re.search(r"gh pr merge\s+(\d+)", command) or re.search(r"/pull/(\d+)", response)
    pr_num = m.group(1) if m else None

    docs_files = [
        f.strip()
        for f in os.environ.get("SEBBY_DOCS_FILES", DEFAULT_DOCS_FILES).split(",")
        if f.strip()
    ]
    docs_list = ", ".join(f"`{f}`" for f in docs_files)

    if pr_num:
        pr_ref = f"PR #{pr_num}"
        resolve_step = f"`gh pr view {pr_num} --json state,mergedAt`"
        diff_step = f"`gh pr diff {pr_num}` and `gh pr view {pr_num} --json files,title,body`"
        branch_name = f"docs/post-merge-sync-{pr_num}"
    else:
        pr_ref = "the most recently merged PR"
        resolve_step = (
            "first resolve the PR number with "
            "`gh pr list --state merged --limit 1 --json number,mergedAt,title` "
            "(call this $PR), then check state with `gh pr view $PR --json state,mergedAt`"
        )
        diff_step = "`gh pr diff $PR` and `gh pr view $PR --json files,title,body`"
        branch_name = "docs/post-merge-sync-$PR"

    print(
        json.dumps(
            {
                "systemMessage": (
                    f"`gh pr merge` just ran for {pr_ref}. Drive the docs-sync follow-up:\n"
                    f"1. Confirm the merge actually landed (auto-merge may be waiting on CI): "
                    f"{resolve_step}.\n"
                    "2. If state != MERGED, stop here — the docs sync only runs after the "
                    "merge completes.\n"
                    f"3. Read the merged diff: {diff_step} for context.\n"
                    f"4. Update any of {docs_list} whose documented surface changed (setup "
                    "steps, env vars, commands, feature list, architecture, integrations).\n"
                    "5. If none need an update, say so and stop.\n"
                    "6. If you edited any doc, spawn an Agent (subagent_type=general-purpose) "
                    'with a prompt like: "Read <the edited docs> against the latest code on '
                    'main. Report any factual inconsistency in under 250 words." Address any '
                    "findings before declaring done.\n"
                    f"7. Commit on a `{branch_name}` branch with a `docs:` conventional commit "
                    "and open a PR (same workflow as any other change)."
                )
            }
        )
    )


if __name__ == "__main__":
    main()
