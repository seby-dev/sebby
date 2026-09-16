---
name: feature
description: End-to-end feature workflow — autopilot (plan+implement+PR) → simplify → code-review → security-review → verify. Use when implementing a new feature from scratch with full quality gates.
---

# Feature Implementation Workflow

## Overview

Full pipeline for shipping a new feature with quality gates baked in. Steps
run sequentially; any issues found in review steps are fixed before
proceeding to the next step.

## Steps

### 1. Implement — `/autopilot`

Invoke the `autopilot` skill (if available in this project) with the feature
description from args. Wait for it to complete — it scopes, plans,
implements, and opens a PR. If `autopilot` isn't available, use
`superpowers:brainstorming` → `superpowers:writing-plans` →
`superpowers:subagent-driven-development` instead.

### 2. Simplify — `/simplify`

Invoke the `simplify` skill on the changed files. Apply all suggested
cleanups (reuse, dead code, altitude). Re-run only this step if fixes are
needed.

### 3. Code review — `/code-review`

Invoke the `code-review` skill at **medium** effort with `--fix` to apply
findings automatically. If significant rework is needed, loop back to
step 2 after fixing.

### 4. Security review — `/security-review`

Invoke the `security-review` skill on the pending branch diff. Fix any
findings, then re-run steps 3-4 until both pass clean.

### 5. Verify — `/verify`

Invoke the `verify` skill to run the app and confirm the feature works
end-to-end on the golden path and key edge cases. Document any regressions
found and fix them before marking the workflow complete.

### 6. Report

Summarise in a single message:
- What was built (feature name, files changed)
- What each review step caught and fixed
- PR URL

## Usage

```
/feature <task description>
```

**Examples:**
```
/feature Add a keyword filter that rejects records whose name matches a configurable blocklist
/feature Add a get_pending_items tool so the user can ask what's awaiting a reply
```

## Notes

- If this plugin's hooks are installed, the `post_pr_created` hook fires
  automatically when a PR is opened and reminds Claude to check CI and
  merge — steps 3-4 of this skill are for the pre-PR review pass; the
  post-PR merge nudge comes from the hook, not this skill.
- After steps 2-4 pass clean, check CI status (`gh pr checks`) and merge
  via `gh pr merge --squash --auto --delete-branch` once CI is green.
- Push any fix commits to the same branch before CI completes — Claude will
  wait for the updated run.
- Skip `/verify` only if the change is purely internal (no user-visible
  behaviour, no runtime path changed) and say so explicitly in the report.
- If this plugin's `post_edit_lint` hook is installed and configured (via
  `SEBBY_LINT_COMMAND`), lint issues surface automatically after every file
  write — you don't need a separate manual lint pass.
