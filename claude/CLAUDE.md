# Development Workflow

## Project Location & Git Safety

- **All current and future projects live under `~/Developer/`** — never `~/Documents/Dev/`
  or any other location. This is the canonical root for every repo, full stop.
- **Known landmine — do not touch:** `~/.git` exists (an accidental home-directory repo,
  created 2025-08-28 by a `git init` run from `$HOME` instead of a project directory). It
  is currently live infrastructure backing other projects' linked worktrees — **never
  delete, reinitialize, or run destructive commands against it.** Its mere existence means
  any git command run with `cwd` anywhere under `$HOME` that isn't inside a project's own
  `.git`-bearing directory will silently succeed against `~/.git` instead of erroring "not
  a git repository." This is exactly how pmp-project's own repo got polluted early on
  (docs commits landed in `~/.git`'s `main` before the mistake was caught and a clean repo
  was created at the correct path).
- **Before the first `git init`/`git commit` in any new project**, and any time you're
  unsure which repo a git command will hit: run `git rev-parse --show-toplevel` and
  confirm it matches the actual intended project directory under `~/Developer/`. If it
  prints `/Users/sebby` (home) instead, STOP — you are about to operate on the wrong repo.
- **Never use the harness's native worktree-switch tools (`EnterWorktree`/`ExitWorktree`)
  — they hang in this environment.** Create and remove worktrees with plain git instead:
  `git worktree add <path> -b <branch> <base-branch>` and `git worktree remove <path>` +
  `git branch -d <branch>`. There's no need to "switch into" a worktree to work in it:
  Read/Edit/Write take the worktree's absolute path directly, and git operations go
  through `git -C <path> <command>` rather than `cd`. Note the Bash tool's cwd resets
  to the session's pinned primary directory *between* calls (confirmed 2026-08-24:
  `cd <path> && pwd` prints the new path correctly within that one call, but the next
  call starts back at the pinned directory) — so for non-git tools that need a working
  directory (`uv`, `pytest`, `make`), either call the binary by its full path
  (`<worktree>/.venv/bin/<tool> ...`) or chain the whole sequence into one Bash call
  (`cd <path> && cmd1 && cmd2 && ...`); never split `cd` and a dependent command across
  separate calls.

## Agent Strategy
- For any task that can be decomposed into independent subtasks, always use subagents.
- **Parallel by default**: if N items can be checked/implemented independently, spawn N subagents simultaneously — never check them one by one in a single agent.
- **Wave-based dispatch for a multi-task plan with dependencies.** When a plan's tasks
  aren't all mutually independent — the common case, since some share a file or need
  another task's output first — group them into dependency waves instead of falling back
  to one-at-a-time execution. A wave is every task whose prerequisites are already
  complete and that shares no file with any other task in the same wave. Dispatch a
  whole wave's tasks together, in one message, as parallel subagents, up to the
  machine's concurrency limit (see the next bullet). Verify and commit each task's work
  individually as it lands — never batch commits to the end of a wave — so one task's
  problem never blocks landing the others. Per-task verification is the type-check plus
  the tests for the files the task touched; the full suite, `make pre-push`, and the
  end-to-end suite run once per wave, after its last task merges, in one reusable gate
  worktree. Start the next wave only once every task in the current one is verified and
  committed. Write the
  file-ownership table and the wave assignment into the plan itself (see Full Feature
  Workflow below) so a fresh dispatcher, not just the session that wrote the plan, can
  execute it correctly.
- Delegate exploratory/read-only work (finding files, analysing schemas) to an Explore subagent.
- **Read-only subagents run on Sonnet 5.** Any subagent that only reads, searches,
  runs checks, or reviews and reports, and never edits a file, commits, or pushes,
  gets `model: "sonnet"`: Explore agents, code and security review agents, and
  workflow steps that only verify or report. Pass it explicitly, because a subagent
  with no `model` inherits the session's own. The one exception is the spec and plan
  advisor review, which runs on Opus 5.5 (`model: "opus"`) as a fresh subagent with
  no conversation history; no review uses a Fable model. A project's own CLAUDE.md
  can override either rule.
- Delegate planning and design to a Plan subagent before implementation begins.
- Use background agents for long-running tasks (test runs, log analysis) so the main session stays responsive.
- Up to 10 subagents can run in parallel, but the machine usually runs out first. This
  Mac is an 8-core, 16 GB Apple M3: run up to five implementers at once when none of
  them starts a browser, Playwright run, or dev server, each caps its test threads
  (`vitest --maxWorkers=2`, serial targeted pytest), and memory has room; otherwise
  three (five running browser suites pushed the load average to 186 on 2026-09-25).
  Read-only agents don't count against that cap, and remote subagents
  (`isolation: "remote"`), where available, don't use local cores. The feature-development skill's "Throughput and
  Verification" section has the full rules.

## Full Feature Workflow
When given a new feature request:
1. **Brainstorm** (`superpowers:brainstorming`) — explore context, ask clarifying questions, propose approaches, write and commit a spec to `docs/superpowers/specs/`
2. **Plan** (`superpowers:writing-plans`) — convert the approved spec into a step-by-step implementation plan saved to `docs/superpowers/plans/`
3. **Implement** (`superpowers:subagent-driven-development`) — dispatch fresh subagents per task with spec + code quality review after each
4. **Ship** — commit locally, then follow the Git & PR Workflow below; pushing to origin is always manual, never automatic

## Decomposition Pattern
When given an implementation task:
1. Spawn an Explore subagent to map the relevant codebase
2. Spawn a Plan subagent to design the approach
3. Decompose implementation into independent modules and run them in parallel
4. Spawn a review subagent to validate output before committing

## Parallelism Examples
- "Check whether features A, B, C, D are implemented" → spawn 4 Explore subagents in one message, one per feature
- "Implement modules X, Y, Z" → spawn 3 implementer subagents simultaneously if they don't share files
- "Review these 5 files" → spawn 5 reviewer subagents in parallel
- The signal: any time you find yourself writing "check A, then check B, then check C" — stop and parallelize it

## Security & Review Workflow

Before shipping, run `/sebby-toolkit:review`. It runs
`pr-review-toolkit:code-reviewer` and `semgrep` unconditionally, and
`pr-review-toolkit:silent-failure-hunter` whenever the diff touches
error-handling or fallback code — narrowing only `review-pr`'s optional
tests/types/comments aspects, and only on diffs a Jev risk score
confidently calls low-risk. It never skips silently: it reports what it
skipped and why. If the `sebby-toolkit` plugin isn't installed in a given
project, fall back to running `pr-review-toolkit:silent-failure-hunter`,
`pr-review-toolkit:code-reviewer`, and `semgrep` directly, in parallel.

Passive (always on, no invocation needed):
- **`security-guidance`** — injects OWASP-style security reminders automatically each session

For new external integrations or auth flows, confirm `semgrep` actually
ran as part of `/sebby-toolkit:review`'s step six — check its output if
the session shows semgrep wasn't configured; it requires
`/setup-semgrep-plugin` on first use.

## Documentation Style

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

## Git & PR Workflow

**Pushing to origin is always manual, never automatic.** Every push triggers a
GitHub Actions run, and Actions minutes are a metered, limited resource — stop
after committing and wait for an explicit go-ahead before running `git push`,
regardless of how the implementation task was authorized. This holds even
under a blanket "proceed without checking in" instruction: that authorizes
skipping check-ins on implementation decisions, not skipping the push
confirmation.

When an implementation task is complete:

1. Stage and commit all changes with a descriptive commit message following conventional commits format.
2. Report what's committed and ready, then stop. Wait for the user to confirm before pushing.
3. Once the user confirms, push the branch to origin: `git push -u origin HEAD`.
4. Create a PR using the `gh` CLI:

```
gh pr create --title "<title>" --body "<summary of changes, what was done and why>" --draft=false
```

5. If all CI checks pass, merge the PR:

```
gh pr merge --squash --auto --delete-branch
```

6. Report the PR URL to the user.

Use `--auto` on the merge so it only merges once checks pass. Never force-push, and never merge while CI is failing.
