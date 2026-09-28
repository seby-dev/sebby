---
name: feature-development
description: Use when starting a new feature, enhancement, or any change to a repository's files — including small, obvious, or docs-only edits, and before writing implementation code, drafting a plan, dispatching implementation subagents, or committing anything in a repo whose environments are separate worktrees.
---

# Feature Development

## Overview

End-to-end path from idea to shipped code: isolate → brainstorm/spec → spec review →
**user approval (spec only)** → plan → plan review → implement → quality gates → ship
(merge back for e2e). It sequences existing skills rather than reinventing them; the
original content here is the single approval gate, the advisor-review requirement, and
the temporary-worktree-per-session convention — build in an isolated worktree, merge
back into the shared dev worktree for onward e2e testing, never implement directly in
the shared one.

**Core principle — approval:** the user approves the *spec*, not the plan. Once the
spec is approved, plan writing, plan review, implementation, quality gates, and
shipping all run autonomously with no further human checkpoint — the spec approval is
the one place intent gets confirmed before everything downstream executes on its own.

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
| 1. Brainstorm + spec | Explore intent, requirements, approach with the user; produce a spec. Before scanning the codebase from scratch, check for an existing technical reference doc set (commonly `docs/reference/` with an index file) — if present, its index points straight at the relevant subsystem file(s) instead of re-deriving architecture from source | superpowers:brainstorming | any |
| 2. Spec review | Fresh advisor subagent reviews the spec — not as an implementer. Point it explicitly at relevant existing files/docs (a prompt with no codebase pointers gives plausible-but-locally-blind advice) — **and, if the repo has a `docs/reference/`-style doc set, name its index file explicitly** so the advisor loads the right subsystem file(s) instead of grepping cold | Agent, `model: "opus"` | **Opus 5.5 (required)** |
| 3. Fold + approve | Merge advisor findings into the spec, applying judgment where the advisor is wrong. Put a wall-clock estimate in front of the user with the spec: task count × about 50 minutes per implementation task ÷ the concurrency the machine allows (see Throughput and Verification), plus one gate per wave. If it comes to more than a day, propose shippable slices the user can approve and merge one at a time instead of one approval for everything. **User approves the spec here — this is the only human gate in the pipeline.** | user | — |
| 4. Plan | Convert the approved spec into a step-by-step plan. Runs immediately, no separate confirmation. | superpowers:writing-plans | any |
| 5. Plan review | A **fresh** advisor subagent (not the one that wrote the plan) reviews it against the spec: task-decomposition completeness, missing edge cases, and whether each task is unambiguous enough for a subagent with zero conversation history to execute correctly. Findings are folded in automatically — no user checkpoint. | Agent, `model: "opus"` | **Opus 5.5 (required)** |
| 6. Implement | Decompose into tasks. Small, well-scoped change (single file/function, no cross-cutting decision, one obvious edit) → inline execution. Otherwise → one fresh subagent per task, dispatched in parallel where tasks don't share files, each written test-first, with no more running at once than Throughput and Verification allows. An implementer runs only the tests for what it touched, never the full suite or the end-to-end suite. Each subagent starts cold — if the repo has a `docs/reference/`-style doc set, name the specific subsystem file(s) relevant to that task in its brief, same as any other codebase pointer | superpowers:subagent-driven-development, superpowers:test-driven-development | any |
| 7. Quality gates | Run the repo's own lint/type-check/test/security commands (read its CLAUDE.md/README/build config — don't assume a stack); run review agents. For a multi-wave plan, run the full gate once per wave, after the wave's last task has merged, in one reusable gate worktree (see Throughput and Verification) | pr-review-toolkit:code-reviewer, pr-review-toolkit:silent-failure-hunter (+ semgrep if configured) | any; review agents Sonnet 5 |
| 8. Ship | From inside the temporary feature worktree: commit, then merge that branch back into the shared dev/integration worktree (if the repo has one) and push `origin/dev` (or the repo's equivalent branch) **from there** — never push straight from the temp worktree. Then check whether the repo serves a built frontend/static-asset bundle whose output directory is gitignored (a `package.json` with a `build` script feeding a directory the app serves as static files — e.g. a dashboard's `dist/`): a `git push` alone never regenerates a gitignored build artifact, so the served app keeps showing pre-change code until it's rebuilt. If so, run that build now in the dev worktree and restart whatever process serves the output. Then, if this feature added a schema migration, apply it to the dev environment's own database **before anything restarts into the new code** — a `git push` never runs migrations, and dev is the one environment nothing migrates automatically (environment-promotion's A5 migrates staging, not dev). Compare the migration tool's current revision against its head (e.g. `alembic current` vs `alembic heads`) and upgrade if they differ. Skip this and the fleet runs code whose schema its own database lacks, and the mismatch stays invisible until the new code first touches the new tables — a recurring job surfaces it within minutes, but a feature reached only through a user command fails silently until someone uses it. No PR here — promoting past dev (to staging/main) is a separate, later decision outside this pipeline's scope; see environment-promotion, which also removes the temporary worktree once this feature reaches staging. Before this stage's merge-back, answer one question using this implementation's own full context (the same session, not a fresh subagent reconstructing it after the fact): "Was there a genuine wrong assumption, a surprising cross-subsystem interaction, or a false lead during this implementation that cost real investigation time?" — the exact bar `docs/reference/14-experience-log.md`'s own header already states. If yes, write one entry there in the file's existing Area/Symptom/Root cause/Fix/Lesson format. If no — the common case — write nothing; silence is the expected, correct outcome, not a gap to fill. This never blocks or delays the merge-back itself. | git (+ the repo's frontend build command, if any) | any |

Stages 4 through 8 run without pausing for user input once the spec is approved in
stage 3 — do not re-introduce a plan-approval checkpoint.

## Worktree Isolation

**Default for every session, not every feature.** A new session working in a repo with
shared environment worktrees sets up its own temporary worktree off the dev/integration
one *before its first write*, and does all work there — whether that work turns out to
be a full feature, a one-line fix, or a docs edit. Don't wait until a change looks big
enough to deserve isolation; by then you're already editing the shared worktree.

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
  that path directly (Read/Edit/Bash with the worktree's absolute path); no `cd`
  or session-switch needed for file edits. **Never use a native worktree-switch
  tool (e.g. `EnterWorktree`/`ExitWorktree`) — it hangs in this environment.**
  **Deliberately skip that skill's Step 0 "already isolated, don't nest" shortcut
  here** — a shared dev/staging worktree is persistent and possibly in concurrent
  use by other sessions, so merely being inside one is not sufficient per-feature
  isolation; always branch a fresh temporary worktree off it.

Stages 6-7 (implement, quality gates) run inside that temporary worktree. Stage 8
(ship) merges the finished branch back into the shared dev/integration worktree and
pushes from there — never straight from the temp worktree — so e2e testing
(superpowers:e2e-dev-testing) and any other concurrent session see one coherent
dev history.

**Merge back only when the work is finished — implementation complete *and* quality
gates green.** The merge-back is a single event at the end of stage 8, not a running
sync: don't merge partial work into dev to "keep it current", don't merge before the
gates pass intending to fix forward, and don't merge task-by-task while other tasks
are still in flight. Until that moment, dev stays exactly as you found it, so any
concurrent session or e2e run sees a dev that is coherent by construction rather than
by timing luck. If the work is abandoned, the temporary worktree is discarded and dev
never knew about it — which is the other half of what isolation buys you.

**The temporary worktree survives shipping.** Its job isn't done until the change
is validated end-to-end in dev and promoted to staging — deleting it right after
the merge-back would strand any need to re-run implementation against the same
branch if e2e testing turns up a bug. Cleanup is deferred to environment-promotion:
once this feature's commits land on staging (that pipeline's Stage A), it removes
the now-spent temporary worktree — see [[environment-promotion]].

**Stage 8 ends at the push. It does not include cleanup.** `git worktree remove`
and `git branch -d` are not yours to run at the end of this pipeline — they belong
to environment-promotion's A8, which deletes only after confirming the branch is an
ancestor of the merged `origin/staging` tip. That confirmation is impossible at ship
time: the commits are on `dev` and nowhere else yet, so "is this worktree spent?"
has no answer you can check. Deferring isn't caution — it's the earliest point the
question becomes decidable.

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
straight into `main`, as in staff2solfa), A8 never runs, so deferred worktrees are
never removed: staff2solfa had 118 of them holding 70 GB, with 9.5 GB of disk free
(measured 2026-09-25). In such a repo the question *is* decidable at merge time,
because `main` is the branch's final destination. After the merge into `main` and
green gates, confirm `git merge-base --is-ancestor <branch> main`, then run
`git worktree remove <path>` (never `--force`) and `git branch -d <branch>`. Both
refuse anything unmerged or uncommitted, so nothing can be lost. A branch that is
waiting for the user's order to merge (for example "don't merge to main until I
say") keeps its worktree until it merges.

## Throughput and Verification

The limit on a multi-task plan is usually the machine, not the model. Measured on
the staff2solfa evensong redesign (2026-09-24 to 25, an 8-core, 16 GB Apple M3):
five parallel implementers, each running its own tests and dev servers, pushed the
load average to 186 and made a cross-browser suite time out. Implementation tasks
averaged about 49 minutes, and the same full gates ran 9 (pre-push) and 18 (end to
end) times.

- **Size concurrency to the machine.** On the 8-core, 16 GB Mac, run up to
  **five** implementers at once when all of these hold, and three otherwise:
  - Implementers start no browser, Playwright run, or dev server. End-to-end
    suites and dev servers run only at the wave gate, when no implementer runs.
    (Five implementers each running browser suites caused the load average of 186.)
  - Each implementer caps its test threads: `npx vitest run <paths> --maxWorkers=2`,
    and targeted pytest runs serially (no `-n`). Measured on 2026-09-25: a capped
    web-test batch keeps about 2 cores busy instead of 4.3; the type-check peaks
    near 600 MB and a web-test batch near 530 MB.
  - Memory has room: before dispatching, `memory_pressure | tail -1` reports at
    least 40% free. (Don't gate on `vm.swapusage`: macOS keeps swap allocated
    until each page is touched again, so it stays high after the pressure is gone.)
    Heavy idle apps (a second browser, other projects' local services) are closed
    first.
  Read-only agents (Explore, reviews) are cheap and don't count against the cap.
  Where the harness offers remote subagents (`isolation: "remote"`), prefer them
  for implementers: they don't compete for local cores or memory.
- **Targeted tests per task.** An implementer runs the tests for the files it
  touched (for example `pytest tests/test_<module>.py`, `npx vitest related
  <files>`), plus the type-check. The full suite, `make pre-push`, and the
  end-to-end suite belong to the wave gate.
- **One gate per wave, frozen first.** Decide the wave's membership, merge every
  task, and only then run the gate. A task that lands after the gate started means
  a second full gate (wave 3 of the evensong redesign ran two back to back).
- **One reusable gate worktree.** Keep a single gate worktree per branch, check out
  the commit to test, and reinstall dependencies only when a lockfile changed
  (`npm ci` when `package-lock.json` changed, `uv sync --all-extras` when `uv.lock`
  changed). Don't build a fresh worktree for each gate.
- **Cross-browser runs at the wave gate only.** During a wave, run end-to-end
  checks in one browser with fewer workers; run the other browsers once, at the
  gate, when no implementer is running.
- **Keep the machine awake.** A long run on a laptop needs the app's keep-awake
  setting on: a 2h13m outage mid-wave cut off three tasks in the evensong run.

## Model Selection

Pick whatever model and thinking effort fits each stage's actual task — vary freely
stage to stage.

**Exception: spec review (stage 2) and plan review (stage 5) always run on Opus 5.5**
(`model: "opus"`), dispatched as a fresh advisor subagent that didn't write the
artifact under review and gets no conversation history, only the artifact and
pointers to the relevant files. No review uses a Fable model (decided 2026-09-23).

**Read-only subagents run on Sonnet 5** (`model: "sonnet"`): any subagent that only
reads, searches, runs checks, or reviews and reports, and never edits a file,
commits, or pushes. That includes Explore agents and stage 7's review agents. Pass
the model explicitly, because a subagent with no `model` inherits the session's
own. The stage 2 and stage 5 advisor is the one exception. A repo's own CLAUDE.md
can override either rule.

**Why:** a reviewer sharing context with the author inherits the author's blind
spots. Now that the advisor runs on the same model as the author, the fresh
context is what makes the review independent. This was tested directly in a prior
project: the same review prompt without explicit pointers to relevant existing
files missed a bug that had been fixed in that exact codebase hours earlier. Fresh
context alone isn't enough either — the advisor prompt must name the specific
files/docs to read, not just paste the spec/plan text.

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
