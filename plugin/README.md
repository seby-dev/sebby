# sebby-toolkit (Claude Code plugin)

Shared Claude Code hooks, a feature-development skill, and a settings.json
template, generalized from patterns duplicated across several projects.

## Install

    /plugin marketplace add seby-dev/sebby
    /plugin install sebby-toolkit

## What's included

- **Hooks** (see `hooks/hooks.json`): env-file git-add blocking, transcript
  secret scrubbing, post-edit lint, stop-time quality gate, a post-PR-created
  nudge, a post-merge docs-sync nudge, and an optional worktree-redirect on
  session start.
- **Skills**:
  - `feature` — an autopilot → simplify → code-review → security-review →
    verify pipeline for shipping a feature end-to-end.
  - `review` — a pre-PR review pass with Jev-backed triage: runs
    code-reviewer and semgrep unconditionally, silent-failure-hunter when
    the diff touches error handling, and narrows `review-pr`'s optional
    aspects only on diffs Jev confidently scores as low-risk.
- **Template**: `templates/settings.json` — a safe-defaults permission
  deny-list. Copy its contents into your project's own
  `.claude/settings.json` (plugins can't write to a consuming project's
  settings file automatically). Its `"defaultMode": "dontAsk"` with no
  `allow` list isn't a complete permission setup on its own — add your own
  `allow` rules (project- or user-level) for anything to actually run
  without individual prompts.

## Configuring the hooks

Several hooks read environment variables to avoid hardcoding project-specific
values — set these in your project's own `.claude/settings.json` under an
`"env"` block, or in your shell environment:

| Variable | Used by | Default | Purpose |
|---|---|---|---|
| `SEBBY_LINT_COMMAND` | `post_edit_lint.py`, `stop_quality_check.py` | `ruff check --output-format=concise` | Lint command run after edits and at Stop; the project root is appended as the last argument. |
| `SEBBY_TYPECHECK_COMMAND` | `stop_quality_check.py` | unset (skipped) | Type-check command run at Stop, e.g. `mypy src`. Pass the target path explicitly — it is NOT auto-appended. |
| `SEBBY_DOCS_FILES` | `post_merge_docs_update.py` | `README.md` | Comma-separated list of doc files to check for staleness after a PR merge. |
| `SEBBY_WORKTREE_DEV_PATH` | `worktree_redirect.py` | unset (hook no-ops) | Absolute path to a dev worktree to redirect a session into, if the session started at the project root. |
| `SEBBY_REVIEW_TRIAGE_MODE` | `review_triage.py` (via the `review` skill) | `shadow` | `shadow` logs would-be triage decisions without narrowing review; `active` applies real narrowing; `off` disables triage entirely. |
| `TYPESAFE_API_KEY` | `stop_quality_check.py`, `semantic_destructive_check.py`, `block_env_git.py`, `review_triage.py` | unset (Jev-backed enhancements inert) | Enables all Jev (TypeSafe) semantic checks across these hooks/scripts. Also requires `typesafe_sdk` (the `sebby[judgement]` extra) to be importable by whatever `python3` runs on `PATH` in the consuming project — plugin scripts don't carry a bundled venv. Without both, every Jev-backed check silently fails open (never blocks, never narrows). |
