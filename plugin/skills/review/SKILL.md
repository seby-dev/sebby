---
name: review
description: Pre-PR review pass with Jev-backed triage -- runs code-reviewer and semgrep unconditionally, silent-failure-hunter when the diff touches error handling, and review-pr's tests/types/comments aspects narrowed only on diffs Jev confidently scores as low-risk. Use before opening a PR.
---

# Pre-PR Review Triage

## Overview

Runs the full pre-PR review pass, but skips `review-pr`'s optional aspects
(tests/types/comments) on diffs a Jev risk score confidently calls trivial
or low risk. `code-reviewer` and `semgrep` always run. `silent-failure-hunter`
runs whenever the diff touches error-handling or fallback code, determined
deterministically -- never by the Jev score. Nothing is ever silently
skipped: this skill's report always states what ran and what triage
skipped or would have skipped.

## Steps

### 1. Determine the base ref

Resolve the ref to diff against:
- If this branch tracks a remote, use its merge-base:
  `git merge-base HEAD origin/<default-branch>`.
- Otherwise try `git merge-base HEAD main`, then `git merge-base HEAD master`.
- If neither exists, ask the user which ref to diff against rather than
  guessing.

### 2. Run triage

```
python3 "$CLAUDE_PLUGIN_ROOT/hooks/scripts/review_triage.py" --base <resolved-ref>
```

Parse the JSON on stdout: `risk_tier`, `mode`, `candidate_aspects`,
`recommended_aspects`, `silent_failure_hunter` (bool), `reasons`.

**If the command exits non-zero** (bad `--base` ref, git failure, or any
other error), do not trust a partial or malformed result. Treat this
exactly like `pr-review-toolkit` being unavailable in step 5: fall back to
the full review set — dispatch `silent-failure-hunter` unconditionally,
and pass every candidate aspect to `review-pr` unfiltered — and note the
triage failure in the final report rather than silently proceeding as if
it recommended a narrow set.

### 3. Dispatch code-reviewer (always)

Dispatch `pr-review-toolkit:code-reviewer`. If the plugin isn't installed
in this project, review the diff manually for bugs, convention violations,
and missed edge cases instead of skipping this step. This step never
depends on triage output.

### 4. Dispatch silent-failure-hunter (conditional, deterministic)

If `silent_failure_hunter` is `true`, dispatch
`pr-review-toolkit:silent-failure-hunter`. If the plugin isn't installed,
manually check the diff's catch blocks and fallback branches for swallowed
errors and inadequate logging instead of skipping this step. If `false`,
skip it and say so in the report -- this decision never depends on the Jev
risk score.

### 5. Run review-pr for the narrowed aspects

Build the aspect list: `"code"` plus `recommended_aspects` (which may be
empty on a low-risk diff in `active` mode). Invoke:

```
/pr-review-toolkit:review-pr <aspect list, space-separated>
```

If `pr-review-toolkit` isn't installed, manually review the diff for each
aspect in `candidate_aspects` (not just `recommended_aspects`) instead of
skipping this step -- the same "unavailable plugin" fallback
`plugin/skills/feature/SKILL.md` already uses for its own review steps.

### 6. Run semgrep (always)

Run the semgrep MCP tool's SAST/secrets/supply-chain findings tools
unconditionally. If semgrep isn't set up in this session, note that in the
report rather than skipping silently -- the user can run
`/setup-semgrep-plugin`.

### 7. Report

Summarize in one message:
- What ran: code-reviewer, semgrep, and (if applicable)
  silent-failure-hunter and each review-pr aspect actually dispatched.
- **If triage's JSON couldn't be parsed** (step 2's non-zero-exit case),
  report that explicitly — e.g. "Triage failed to run (git error); ran the
  full review set as a fallback" — rather than silently omitting this
  bullet because there was no `reasons` field to quote.
- **Otherwise, what triage skipped**, quoting `reasons` from the JSON -- e.g.
  "Skipped: tests, types, comments (risk_tier: low, mode: active)". If
  `mode` is `shadow`, report it as "would have skipped" instead, since
  shadow mode runs everything regardless of tier.
- Findings from the dispatched agents/tools, using the same
  critical/important/suggestion structure `review-pr` itself uses.

## Usage

```
/review
```

When installed as a plugin, invoke namespaced: `/sebby-toolkit:review`.

## Notes

- Triage is inert (recommends everything, `risk_tier` always `"high"`)
  unless `TYPESAFE_API_KEY` is set and `typesafe_sdk` (the `judgement`
  extra) is importable by this project's `python3` -- see
  `plugin/README.md`'s configuration table.
- `SEBBY_REVIEW_TRIAGE_MODE` defaults to `shadow`: triage logs what it
  would have skipped to `~/.claude/sebby-triage.jsonl` without actually
  skipping anything, so real PRs can be sampled before trusting it. Set it
  to `active` to apply real narrowing, or `off` to disable triage entirely
  (equivalent to Jev being unavailable).
- Only committed commits on this branch are scored -- commit local changes
  before running this skill, the same expectation any PR review already has.
