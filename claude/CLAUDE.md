# Development workflow

The procedure for any change to a repository lives in the `feature-development`
skill (`~/.claude/skills/feature-development/SKILL.md`), with `wave-execution.md`
beside it for multi-task plans. This file keeps only the rules that every
session needs.

## Safety rules

1. Pushing to origin is always manual: stop after committing and wait for an
   explicit go-ahead, even under a blanket "proceed without checking in,"
   because a push can start a metered GitHub Actions run; a repository's
   `pre-push` hook runs its local gate (`make pre-push`) first.
2. `~/.git` is live infrastructure (an accidental home-directory repository from
   2025-08-28 that backs other projects' worktrees): never delete,
   reinitialize, or run destructive commands against it, never run `git` in
   `~/.claude`, and before the first `git init` or commit, confirm that
   `git rev-parse --show-toplevel` prints the project directory, not
   `/Users/sebby`.
3. Never use the harness's `EnterWorktree` or `ExitWorktree` tools, because
   they hang here: use `git worktree add`, `git worktree remove`, and
   `git -C <path>`, and because the Bash tool's working directory resets
   between calls, chain dependent commands in one call.
4. This Mac is an 8-core, 16 GB M3, so run at most five implementers at once
   when none starts a browser, Playwright run, or dev server and each caps its
   test threads, and three otherwise; implementers run targeted tests only.

## Project location

Every project lives under `~/Developer/`, never `~/Documents/Dev/` or any other
location.

## Agents and models

Pass a typed agent (from `~/.claude/agents/`) or an explicit `model` on every
dispatch, because a subagent with neither inherits the session's model.
These defaults apply to every project:

- The session runs on Opus 5.5 at medium effort.
- Sonnet 5.5 at high effort drafts specs and plans (`drafter`), implements
  `risk: low` tasks (`implementer`), and does research (`researcher`).
- Sonnet 5.5 at medium effort runs every other read-only agent (`reader`).
- Opus 5.5 at high effort reviews specs and plans (`plan-advisor`, a fresh
  subagent with no conversation history), reviews waves (`wave-reviewer` and
  `wave-reviewer-domain`) and branches (`branch-reviewer`), implements
  `risk: high` tasks (`implementer-risky`), and adjudicates (`adjudicator`).

Dispatch an Opus role by its typed agent without a `model` override. A project's
own `CLAUDE.md` can override a row only by naming it; a blanket rule about
read-only agents doesn't move the Opus reviewers. For every role's agent, model,
and effort, see the role table in the skill's "Model selection" section.

## Review and security

- Per-task review is `revgate task` only. In a repository without
  `.review.toml`, also run the repository's own targeted gates for the task.
- The wave and branch gates are in the `feature-development` skill.
- The branch gate runs `pr-review-toolkit:silent-failure-hunter` and the
  semgrep CLI: `semgrep --config=auto <source dirs> --error`.
- `security-guidance` stays on passively and needs no invocation.

## Documentation style

All project documentation follows Google's developer documentation style guide
(full reference: the `writing-style` skill). The rules below apply universally —
even for commit messages, PR descriptions, and CLAUDE.md edits.

- **Active voice**: state who performs the action.
- **Sentence case**: headings and titles capitalize only the first word and proper nouns.
- **Serial comma**: "A, B, and C" — always.
- **Contractions** in prose: "don't", "can't", "isn't" — preferred over "do not", "cannot".
- **Word choices**: "run" not "execute"; "stop" not "kill" or "abort" (in prose);
  "set up" (verb) / "setup" (noun); "log in" (verb) / "login" (noun); "filename" not "file name."
- **Conditions before instructions**: "If X, do Y" — not "Do Y if X."
- **No "click here"** or bare URL link text.
- **No "please"** in instructions: "Run the command," not "Please run the command."
- **No unbacked superlatives**: "best", "fastest", "always", "never" need a cited
  reason, not an assertion.
- **Numbers**: spell out zero through nine in prose; digits for 10 and above.
- **No directional language**: "earlier"/"following", not "above"/"below" — layout
  shifts on reflow and translation.
- **Em dash, no spaces, no en dash**: use "—" for a sentence break; never "–"; use a
  hyphen or "to" for ranges instead.
- **Demonstrative pronouns need a noun**: "this file", not a standalone "this".

Load `writing-style` before writing or substantially revising any documentation.

## Git and PR workflow

Only after the user's explicit go-ahead for that repository's push:

1. Push the branch: `git push -u origin HEAD`.
2. Open a PR: `gh pr create --title "<title>" --body "<summary>" --draft=false`.
3. Merge it once checks pass: `gh pr merge --squash --auto --delete-branch`.
4. Report the PR URL to the user.

Never force-push, and never merge while CI is failing.
