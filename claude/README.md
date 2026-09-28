# Claude Code assets

This directory versions the assets that live under `~/.claude`, so a change can be
tested, reviewed, and reverted like any other code:

- `CLAUDE.md`: the global instructions.
- `skills/feature-development/`: the feature-development skill and its
  `wave-execution.md`.
- `agents/`: the typed subagents (`implementer`, `implementer-risky`,
  `adjudicator`, `plan-advisor`, `wave-reviewer`, `wave-reviewer-domain`,
  `branch-reviewer`, and `reader`). Each one pins its own `model`, so a dispatch
  that names one never inherits the session's model.
- `hooks/`: the `PreToolUse` hooks, `agent_model_guard.py` and
  `cd_only_reminder.py`.
- `settings-hooks.json`: the hook fragment that registers those hooks.

## Install

1. Back up `~/.claude/settings.json` with a timestamped name, for example into
   `~/.claude/backups/`.
2. Symlink each asset from `~/.claude` into this directory, for example
   `ln -s ~/Developer/sebby/claude/agents ~/.claude/agents`. If a real file or
   directory is already there, move it into the backup directory first.
3. Merge the `hooks.PreToolUse` entries from `settings-hooks.json` into
   `~/.claude/settings.json`, then validate it with
   `python3 -m json.tool ~/.claude/settings.json`.

## The hooks

- **`agent_model_guard.py`** flags an `Agent` dispatch that names neither an agent
  that pins a model nor an explicit `model`, because such a dispatch inherits the
  session's model. It warns by default: Claude and the user see a note, and the
  dispatch runs. If you set `SEBBY_AGENT_GUARD=refuse`, it denies the dispatch
  instead. Every flagged dispatch is logged to
  `~/.local/state/sebby/agent-guard.jsonl` (`$XDG_STATE_HOME/sebby/` when that's
  set).
- **`cd_only_reminder.py`** adds a note when a Bash call only runs `cd`, because the
  Bash tool's working directory resets before the next call. It never blocks.

Each hook command fails open: a missing hook file or a crashed hook exits `0`. That
matters because exit `2` from a `PreToolUse` hook blocks the tool call in every
session. Each hook signals a warning or a denial only through its JSON output, so
the commands' `|| exit 0` loses nothing.

## What the symlinks follow

The symlinks and the hook commands point at `~/Developer/sebby`'s main checkout, so
they follow whatever branch that checkout has checked out. Switching or rebasing
that checkout changes every session's global instructions, agents, and hooks at
once. Keep the main checkout on `main`, and do other work in a worktree.

## Roll back

1. Remove the symlink from `~/.claude` and move the backed-up file or directory
   back.
2. Restore `~/.claude/settings.json` from its timestamped backup, then validate it
   with `python3 -m json.tool`.
