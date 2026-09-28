---
name: feature-development
description: Use when starting a new feature, enhancement, or any change to a repository's files — including small, obvious, or docs-only edits, and before writing implementation code, drafting a plan, dispatching implementation subagents, or committing anything in a repo whose environments are separate worktrees.
---

# Feature Development

## Overview

End-to-end path from idea to shipped code: isolate → brainstorm/spec → spec review →
**user approval (spec only)** → plan → plan lint → plan review → implement → wave
gates → branch gate → ship. It sequences existing skills and `revgate`; the original
content here is the single approval gate, the advisor-review requirement, the
wave-structured plan, and the temporary-worktree-per-session convention.

**Core principle — approval:** the user approves the *spec*, not the plan. Once the
spec is approved, everything through the local merge runs autonomously with no
further human checkpoint. The push is the one exception: it always waits for the
user's explicit go-ahead.

**Core principle — isolation:** a session works in its own temporary worktree by
default and merges back once, at the end, when implementation is done and the quality
gates are green. This is unconditional in repos with shared environment worktrees —
it does not scale down with the size of the change, and stage 0 applies even when
stages 1-5 are skipped.

## When to Use

- Any feature request, enhancement, or change that isn't small, well-scoped, and unambiguous
- Before opening a plan or spec file
- Before dispatching the first implementation subagent
- Before the *first write of the session* to any repo whose environments are separate
  worktrees — including a one-line fix, a docs edit, or a config tweak

**Two separate questions — decide them independently:**

| Question | Answer |
|---|---|
| Does this change need the **pipeline** (stages 1-5: brainstorm, spec, reviews, plan)? | No, if it's small, well-scoped, and unambiguous — skip straight to stage 6, regardless of how many lines or files it touches. Also no for pure bug investigation (use superpowers:systematic-debugging). |
| Does this change need **isolation** (stage 0)? | **Always yes**, in a repo with shared environment worktrees. Size is not an input to this question. |

The size exemption scopes to the pipeline only. It has never scoped to stage 0, and
"too small for a spec" does not imply "too small for a worktree" — a one-line change
committed in the shared dev worktree pollutes it for concurrent sessions exactly as
much as a large one.

## Pipeline

| Stage | What happens | Skill / mechanism | Model |
|---|---|---|---|
| 0. Isolate | Confirm you're in the right repo before anything else: `git rev-parse --show-toplevel` must match the intended project directory under `~/Developer/` (never `~/Documents/Dev/` or another path) — if it prints `/Users/sebby` (home) instead, stop, a stray `~/.git` is catching this command. Then set up a temporary, disposable **per-feature** worktree — never implement directly in a shared environment worktree (e.g. `.worktrees/dev`). See Worktree Isolation below. Use `git init` only inside an already-confirmed project directory, never from `$HOME`. | git, superpowers:using-git-worktrees | any |
| 1. Brainstorm + spec | Explore intent, requirements, approach with the user; produce a spec. The session holds the conversation with the user; a `drafter` agent writes the spec document from the agreed design. Before scanning the codebase from scratch, check for a reference doc set (commonly `docs/reference/` with an index file); its index points at the relevant subsystem file(s) | superpowers:brainstorming, `drafter` agent | Sonnet 5.5 drafter |
| 2. Spec review | Fresh advisor subagent reviews the spec — not as an implementer. Point it explicitly at relevant existing files/docs (a prompt with no codebase pointers gives plausible-but-locally-blind advice), **including a `docs/reference/`-style index file if the repo has one** | `plan-advisor` agent | **Opus 5.5 (required)** |
| 3. Fold + approve | Merge advisor findings into the spec, applying judgment where the advisor is wrong. Put a wall-clock estimate in front of the user with the spec, computed by the duration rule in Plan format. If it comes to more than a day, propose shippable slices the user can approve and merge one at a time instead of one approval for everything. **User approves the spec here — this is the only human gate in the pipeline.** | user | — |
| 4. Plan | Convert the approved spec into waves, sub-waves, and tasks, with the `plan-waves` block, every task's `risk` and `estimate_min`, and the duration table; see Plan format. Runs immediately, no separate confirmation. | superpowers:writing-plans, `drafter` agent | Sonnet 5.5 drafter |
| 4a. Plan lint | `revgate plan-lint <plan>`. The author fixes every error, at most three rounds; if errors remain, stop and report to the user, since the approved spec may be wrong. | `revgate` | none |
| 5. Plan review | A **fresh** advisor (not the one that wrote the plan), given the lint report, reviews the plan against the spec: task-decomposition completeness, missing edge cases, each task's `risk`, and whether each task is unambiguous enough for a subagent with zero conversation history. Findings are folded in automatically — no user checkpoint. | `plan-advisor` agent | **Opus 5.5 (required)** |
| 6. Implement | Per-task loop: the implementer works test-first and commits; in a repo without `.review.toml`, it runs the repo's own targeted gates (lint, type-check, and tests for the touched files); then it runs `revgate task`. Dispatch `implementer` or `implementer-risky` by the task's `risk`, in parallel up to the caps in Throughput and verification. An implementer never runs the full suite or the end-to-end suite. No LLM reviews a single task; see Review. A small, well-scoped change with no plan runs inline as its own branch. If the repo has a `docs/reference/`-style doc set, name the subsystem file(s) relevant to each task in its brief | superpowers:subagent-driven-development, superpowers:test-driven-development, `revgate task` | see Model selection |
| 7a. Wave gate | When the wave's last task lands: `revgate wave`, then the full gate through `gate-if-changed` in one reusable gate worktree, end-to-end tests in one browser only when the wave touched `[e2e].web_paths`, then a focused LLM review of flagged and `risk: high` tasks only. See `wave-execution.md`. | `revgate wave`, `gate-if-changed`, `wave-reviewer` or `wave-reviewer-domain` | see Model selection |
| 7b. Branch gate | Before merge: one whole-branch review from `revgate map`, `silent-failure-hunter`, and the `semgrep` CLI, plus the full cross-browser end-to-end run once | `revgate map`, `branch-reviewer`, pr-review-toolkit:silent-failure-hunter, `semgrep` | see Model selection |
| 8. Ship | From inside the temporary feature worktree: commit, then merge that branch back into the shared dev/integration worktree (if the repo has one), or into `main` where the repo's convention merges locally. **Pushing waits for the user's explicit go-ahead**; after it, push from the shared worktree (never straight from the temp worktree) and follow the global `CLAUDE.md`'s Git and PR steps. A repo whose CI runs on dispatch only gets no CI run from the push. After a dev push, check whether the repo serves a built frontend/static-asset bundle whose output directory is gitignored (a `package.json` with a `build` script feeding a directory the app serves — e.g. a dashboard's `dist/`): a `git push` never regenerates it, so run that build in the dev worktree and restart whatever serves the output. If this feature added a schema migration, apply it to the dev environment's own database **before anything restarts into the new code** — a `git push` never runs migrations, and dev is the one environment nothing migrates automatically (environment-promotion's A5 migrates staging, not dev). Compare the migration tool's current revision against its head (e.g. `alembic current` vs `alembic heads`) and upgrade if they differ. No PR here — promoting past dev is a separate, later decision; see environment-promotion, which also removes the temporary worktree once this feature reaches staging. Before the merge-back, ask with this implementation's own full context: "Was there a genuine wrong assumption, a surprising cross-subsystem interaction, or a false lead that cost real investigation time?" If yes, write one entry in `docs/reference/14-experience-log.md` in its existing format; if no — the common case — write nothing. This never blocks the merge-back. | git (+ the repo's frontend build command, if any) | any |

Stages 4 through 8 run without pausing for user input once the spec is approved in
stage 3, except the push, which always waits — do not re-introduce a plan-approval
checkpoint.

## Plan format

- **Wave:** a barrier. Every task in the wave lands, then one gate runs. Use as few
  waves as the dependencies allow.
- **Sub-wave:** an ordering inside a wave with no gate.
- **Task:** one implementer dispatch in its own worktree.

Each plan carries one fenced `plan-waves` YAML block near its header. Every task
requires `id`, `risk` (`low` or `high`), `depends_on`, `owns` (with `create`,
`modify`, and `test`), and `estimate_min`. Optional fields are `runs`, `forbidden`,
`context`, `tests: none` with a reason, and `kind`; a plan-level `forbidden` list
covers files no task may touch. A task that depends on a `risk: high` task, or on
one that owns a `[risk.paths]` file, sits in a later wave, after the focused review.

Every plan opens with a duration table. A wave's wall-clock is the larger of its
critical path and its summed task time divided by the concurrency cap, plus its
gate and any review. For the full schema, an example, and every lint check, see
Appendix H of the pipeline design spec,
`2026-09-27-review-and-wave-pipeline-overhaul-design.md`.

## Review

- **Per task:** only `revgate task`, which is deterministic. In a repo without
  `.review.toml`, the repo's own targeted gates run first, because there
  `revgate task` runs no gate and every rule is in shadow. Exit `0` means clean or
  advisory only, `1` a blocking finding or a failed gate, and `2` a checker or tree
  failure that's never a pass.
- **Controller:** re-runs `revgate task --role controller` on the reported head;
  that run is authoritative.
- **Focused wave review:** runs only when the wave's focus list isn't empty.
  Dispatch `wave-reviewer-domain` when a `risk: high` task is in focus, else
  `wave-reviewer`.
- **Mapped branch review:** `branch-reviewer` goes deep on flagged, `risk: high`,
  music-theory, and cross-wave areas and skims cleared ones. Above 3,000 changed
  non-test lines, split it by subsystem.
- **Inline changes** are their own branch: `revgate task`, then a
  `branch-reviewer` review.

## Calibration and tightening

Every rule not proven runs in shadow at first. A project leaves calibration after
at least two plans, when `revgate` meets its benchmark bar, no unflagged task has a
Critical finding, and the one-sided 90% upper bound on the unflagged-task escape
rate is at most 5%. After that, a trigger moves the project one step up this
ladder:

1. Widen the flags (`[risk.paths]`, the plan's `risk` rule, or `max_lines`).
2. Deepen the branch review: escape-producing areas stop counting as cleared.

Every focused wave review already runs on Opus 5.5 at high effort (2026-09-28), so
the ladder has no reviewer-strength step.

No step restores per-task LLM review.

## Worktree Isolation

**Default for every session, not every feature.** A new session working in a repo with
shared environment worktrees sets up its own temporary worktree off the dev/integration
one *before its first write*, and does all work there — whether that work turns out to
be a full feature, a one-line fix, or a docs edit. Don't wait until a change looks big
enough to deserve isolation.

**The shared dev worktree is a branch point and a merge target — never a workspace.**
Its two legitimate uses in this pipeline are: branching a temporary worktree off its
tip (stage 0), and receiving the finished merge (stage 8). Editing, committing, or
pushing feature work *from inside* it is the thing this section exists to prevent —
other sessions may be using it concurrently, and downstream e2e testing needs to find
it in one coherent state, not mid-feature.

- **Repo has no persistent environment worktrees** (a single main checkout):
  superpowers:using-git-worktrees handles this correctly as-is — its Step 0
  detects a normal checkout and creates an isolated worktree off it.
- **Repo separates environments via persistent worktrees** (dev/staging/prod, each
  its own linked worktree): move into the dev/integration one first and pull it up
  to date — that's the branch point, not the workspace. Then create a *nested*
  temporary worktree off its tip for the feature itself. Use plain git —
  `git worktree add <path> -b <feature-branch> <dev-branch>` per
  superpowers:using-git-worktrees's directory-selection rules — then operate on
  that path directly by its absolute path. **Never use a native worktree-switch
  tool (e.g. `EnterWorktree`/`ExitWorktree`) — it hangs in this environment.**
  **Deliberately skip that skill's Step 0 "already isolated, don't nest" shortcut
  here** — a shared dev/staging worktree is persistent and possibly in concurrent
  use by other sessions, so merely being inside one is not sufficient per-feature
  isolation; always branch a fresh temporary worktree off it.

Stages 6-7 (implement, gates) run inside that temporary worktree, with one
worktree per task when a plan dispatches subagents. Stage 8 merges the finished
branch back into the shared dev/integration worktree and, after the user's
go-ahead, pushes from there — never straight from the temp worktree — so e2e
testing and any concurrent session see one coherent dev history.

**Merge back only when the work is finished — implementation complete *and* quality
gates green.** The merge-back is a single event at the end of stage 8, not a running
sync: don't merge partial work into dev, don't merge before the gates pass intending
to fix forward, and don't merge task by task. Until then, dev stays exactly as you
found it. If the work is abandoned, the temporary worktree is discarded and dev never
knew about it.

**The temporary worktree survives shipping.** Its job isn't done until the change
is validated end-to-end in dev and promoted to staging; if e2e testing turns up a
bug, the same branch needs it. **Stage 8 ends at the push. It does not include
cleanup.** `git worktree remove` and `git branch -d` belong to
environment-promotion's A8, which deletes only after confirming the branch is an
ancestor of the merged `origin/staging` tip — a check that can't be evaluated at
ship time, when the commits exist only on `dev`. See [[environment-promotion]].

**No exceptions:**
- Not for a docs-only or config-only change ("nothing to e2e-test" is a guess about
  the future, and it's the exact reasoning that strands a branch when the change
  turns out to need a follow-up)
- Not because the change is small, or obviously correct, or already verified locally
- Not to "keep the worktree list clean" — a live worktree costs disk, a deleted one
  costs a re-branch and a lost working state
- Not even if `git worktree list` is visibly cluttered with old worktrees; those are
  A8's backlog, not evidence the rule is wrong

Leaving it on disk is the finished state of this pipeline, not an omission.

**Exception: a repo with no staging promotion.** When the repo has no
environment-promotion pipeline (no staging branch or worktree — the branch merges
straight into `main`), A8 never runs, so deferred worktrees pile up: one such repo
had 118 of them holding 70 GB (measured 2026-09-25). In such a repo the question
*is* decidable at merge time, because `main` is the branch's final destination. After the merge into `main` and
green gates, confirm `git merge-base --is-ancestor <branch> main`, then run
`git worktree remove <path>` (never `--force`) and `git branch -d <branch>`. Both
refuse anything unmerged or uncommitted, so nothing can be lost. A branch that is
waiting for the user's order to merge (for example "don't merge to main until I
say") keeps its worktree until it merges.

## Throughput and verification

- On the 8-core, 16 GB Mac, run up to five implementers at once when none starts a
  browser or dev server, and three otherwise.
- Implementers run targeted tests only: the tests for the files they touched.
- Freeze a wave's membership, then run one gate in one reusable gate worktree.
- Keep the machine awake for a long run.

`wave-execution.md` in this directory has the full rules.

## Model selection

Every dispatch names a typed agent or a `model`, because a subagent with neither
inherits the session's model; the `agent_model_guard` hook warns otherwise. The
following table sets each role:

| Role | Typed agent | Model | Effort |
|---|---|---|---|
| Session (talks to the user, dispatches, folds findings) | — | Opus 5.5 | medium |
| Spec and plan drafting | `drafter` | Sonnet 5.5 | high |
| Spec and plan advisor | `plan-advisor` | Opus 5.5 | high |
| Implementer, `risk: low` | `implementer` | Sonnet 5.5 | high |
| Implementer, `risk: high` | `implementer-risky` | Opus 5.5 | high |
| Adjudication (blocking finding or suppression) | `adjudicator` | Opus 5.5 | high |
| Wave reviewer, with a `risk: high` task in focus | `wave-reviewer-domain` | Opus 5.5 | high |
| Wave reviewer, otherwise | `wave-reviewer` | Opus 5.5 | high |
| Branch reviewer | `branch-reviewer` | Opus 5.5 | high |
| Research (docs, APIs, the web) | `researcher` | Sonnet 5.5 | high |
| Explore and other read-only agents | `reader` | Sonnet 5.5 | medium |

These defaults apply to every project (set 2026-09-28, when Sonnet 5.5 shipped).
The session's own model and effort come from `~/.claude/settings.json`.

No review uses a Fable model (decided 2026-09-23). A repo's own CLAUDE.md can
override a row only by naming that row. A blanket rule such as "read-only agents run
on Sonnet" doesn't reach the Opus rows: dispatch those by their typed agent with no
`model`, since an explicit `model` overrides the agent's own pin.

**Why:** a reviewer sharing context with the author inherits the author's blind
spots. Now that the advisor runs on the same model as the author, the fresh
context is what makes the review independent: dispatch one that didn't write the
artifact and give it no conversation history, only the artifact and pointers to the
relevant files. This was tested directly in a prior project: the same review prompt
without explicit pointers to relevant existing files missed a bug that had been
fixed in that exact codebase hours earlier. Fresh context alone isn't enough either
— the advisor prompt must name the specific files/docs to read, not just paste the
spec/plan text.

## Common Mistakes

| Mistake | Why it hurts |
|---|---|
| Skipping brainstorming, writing a plan straight from the request | Loses the exploration step where requirements and approach get pressure-tested |
| Pausing for user approval again after the plan is written | The spec is the approval gate; a second pause contradicts the autonomous-after-approval design |
| Reusing the spec/plan-writing session's context for review | No fresh eyes — same blind spots reviewing themselves |
| Sending the reviewer only the spec/plan text | Produces plausible-sounding but locally-blind advice; always point it at relevant existing code/docs |
| Defaulting to subagents for a two-line fix | Wastes dispatch overhead; use inline execution for small well-scoped changes |
| Not confirming which worktree/branch is active before starting | In worktree-per-environment repos, work can silently land in the wrong environment |
| Running `git init`/`git commit` without confirming `cwd` first | A stray ancestor `.git` (e.g. an accidental one at `$HOME`) silently absorbs the commit instead of erroring — always verify `git rev-parse --show-toplevel` matches the intended project directory |
| Implementing directly in the shared dev/staging worktree instead of a temporary per-feature one | Other sessions may be using it concurrently, and a half-finished feature can block or pollute e2e testing that expects dev to be coherent |
| "This change is too small / it's just docs — isolation is overkill" | The size exemption scopes to the pipeline (stages 1-5), never to stage 0. A one-line commit dirties the shared worktree for concurrent sessions exactly as much as a large one, and "small" is the disguise the failure always wears — observed in practice: a docs-only CLAUDE.md edit was committed and pushed straight from `.worktrees/dev` precisely because it felt too trivial to isolate |
| "The session already started in `.worktrees/dev`, so I'm effectively isolated" | A shared environment worktree is persistent and possibly in concurrent use — being inside one is the problem, not the isolation. Branch a temporary worktree off it before the first write |
| Merging partial work back into dev to "keep it in sync" during implementation | The merge-back is one event after quality gates pass; incremental merges put half-finished work in front of every concurrent session and e2e run, which is exactly what the temporary worktree exists to prevent |
| Applying using-git-worktrees's Step 0 unmodified while already inside a shared environment worktree | Its "already isolated, don't nest" check treats the shared worktree as sufficient isolation and silently skips creating the per-feature one, defeating the point |
| Pushing `origin/dev` straight from the temporary feature worktree | The merge-back happens in the shared dev worktree specifically so e2e testing and other sessions see one coherent history, not a stray branch |
| Deleting the temporary worktree right after shipping to dev | Cleanup is deferred to environment-promotion once the feature reaches staging — e2e testing might still turn up something that needs a fix on the same branch |
| Ending stage 8 with `git worktree remove` / `git branch -d` in a repo that promotes through staging (a repo with no staging promotion cleans up at merge; see the exception under Worktree Isolation) | Stage 8 ends at the push. At ship time the commits exist only on `dev`, so the ancestry check that makes deletion safe (`git merge-base --is-ancestor <branch> origin/staging`) cannot even be evaluated yet — you'd be deleting on a guess. Measured: 3 of 4 agents appended these commands anyway, one reasoning "no e2e testing loop is needed for a pure docs fix" |
| Re-deriving codebase architecture from scratch when a `docs/reference/`-style doc set already exists | Wastes exploration budget rediscovering what's already documented — check its index first, then read source for the specific detail the doc set doesn't cover |
| Pushing a migration to dev without applying it to the dev database | The fleet restarts into code whose schema its own database lacks. `git push` doesn't run migrations, and environment-promotion's A5 migrates staging — so dev is the one environment nothing migrates automatically. Confirmed in practice: a feature's 10-minute recurring job raised `UndefinedTableError` in the dev bot every 10 minutes until `alembic upgrade head` was run against the dev database |
| Pushing to dev without rebuilding a gitignored frontend bundle the app serves | The served app keeps showing stale pre-change code — `git push` doesn't regenerate `.gitignore`'d build output like a dashboard's `dist/`; confirmed in practice: a dashboard feature merged and pushed to dev, but the deployed bundle was hours stale because nothing rebuilt it |
| Writing an experience-log entry for every feature, not just surprising ones | Drowns the genuinely useful entries in routine noise — honor the file's own stated bar, don't lower it |
| Asking an LLM to review a single task | Per-task review is deterministic only (`revgate task`); risky work gets the focused wave review |
| Starting a task that depends on a `risk: high` task in the same wave | It builds on code no one has reviewed; the dependent goes in a later wave |
| Pushing because the implementation was authorized | The push always waits for an explicit go-ahead |
| Dispatching an untyped `general-purpose` agent with no `model` | It inherits the session's model; name a typed agent or a `model` |
| Running a gate on an unchanged tree without `gate-if-changed` | The cache makes a re-run free |
