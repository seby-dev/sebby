# Wave execution

This document is the controller's procedure for running a wave-structured plan, from
dispatching the first task to the branch gate before merge. It's written for a
controller with no other context: read it with the plan, the ledger, and `SKILL.md` in
this directory, which sets the stages this procedure implements (6, 7a, and 7b).

## Scheduling

A plan groups its tasks into waves, and each wave into sub-waves:

- **A wave is a barrier.** It has exactly one gate, and the next wave starts only after
  that gate is clean or each open finding has a ledger ruling.
- **A sub-wave orders tasks inside a wave without a gate.** A task in sub-wave *k*
  starts after the tasks it depends on have landed.
- **Dispatch any task whose dependencies have landed**, up to the concurrency cap in
  "Throughput and verification". When more tasks are ready than slots are free,
  dispatch the tasks on the critical path first, then the largest remaining
  `estimate_min`.
- **Pick the typed agent by the task's `risk`.** A `risk: high` task goes to
  `implementer-risky`; every other task goes to `implementer`. Every dispatch names a
  typed agent or a `model`.
- **Freeze the wave's membership when its last task lands.** Nothing joins a wave after
  that point: a late task belongs to the next wave. A task that lands after the gate
  starts forces a second full gate.

## Worktrees

Every task runs in its own worktree on its own branch:

- Fork the task's worktree from the plan's integration head after the task's
  dependencies merged, so it sees their code:
  `git -C <repo> worktree add <repo>/.worktrees/<task> -b <task-branch> <integration-branch>`.
- Before the first commit, run `git -C <wt> rev-parse --show-toplevel` and confirm it
  prints the worktree path. If it prints the home directory, stop: the command would hit
  the accidental home-directory repository.
- `revgate task` exits `2` when the tree has uncommitted changes or when `HEAD` isn't
  `--head`. As a result, a gate never reads a sibling task's half-edited files, and a
  cached pass always describes one commit.
- Keep one reusable gate worktree per branch. Check out the commit to test, and
  reinstall dependencies only when a lockfile changed.
- After a task lands, remove its worktree with `git worktree remove <path>` and delete
  its branch with `git branch -d <branch>`. Both commands refuse unmerged or uncommitted
  work.
- Create and remove worktrees with plain `git worktree add` and `git worktree remove`.
  Never use the harness's `EnterWorktree` or `ExitWorktree` tools, which hang in this
  environment.

## The per-task loop

Each task goes through the same loop. No LLM reviews an individual task; the
implementer's self-review stays.

1. The implementer works test-first and commits. In a repository without a
   `.review.toml`, it first runs the repository's own targeted gates (lint, type-check,
   and the tests for the files it touched), because there `revgate task` runs no gate
   and every rule is in shadow.
2. The implementer runs `revgate task --role implementer` with the Bash tool's
   `run_in_background: true` before it reports DONE, because a project's gates can
   together outlast the 10-minute foreground limit. If `revgate` is stopped anyway, it
   kills the running gate's process group before it exits. On a blocking finding, it fixes
   and re-runs at most twice. After two failed re-runs, see "Adjudication".
3. The controller re-runs `revgate task --role controller` on the reported head. This run
   is authoritative, and the result cache makes it take about a second when nothing
   changed.
4. The task lands: the controller merges the task branch into the integration branch
   and removes the task worktree.
5. The controller writes the task's ledger line: the task, its commit, its run file,
   and its actual minutes.
6. If the controller run's `focus` field is set, the controller adds the task, its focus
   reasons, and its advisory findings to the wave's focus list.

A task's `focus` flag comes from facts `revgate` can check. Any one of the following
conditions sets it:

- The plan marks the task `risk: high`.
- The diff touches a path in `.review.toml`'s `[risk.paths]`.
- A rule whose tier says "review" reports an advisory finding.
- The diff adds a suppression or edits `.review.toml`.
- The diff exceeds `[routing] max_lines`, 400 changed non-test lines by default.
- A blocking finding survived adjudication as a ruling for the wave reviewer to confirm.

The implementer reads the summary that `revgate task` prints on stdout, which has at
most 20 lines: a status line, every blocking finding, then at most five advisory
findings graded E0 or E1 and marked `advisory`. Each finding gets one line and, when the
cap allows, one indented evidence line. Shadow findings never print. The following
example shows the shape:

```
revgate: 1 blocking, 2 advisory printed (+1 counted), focus (risk path)  task T1  ef45ab6  round 1
src/app/marks.py:262 policy.unjustified_suppression `# type: ignore` added with no reason
    evidence: added in this task's diff (fork..tip); the house form is `# type: ignore[code] -- <reason>`   verify: revgate recheck 5d17e0aa
advisory src/app/marks.py:88 wiring.unwired_planned_here relocate_end_marks is new and nothing in production calls it; the plan's Task 1 code calls it from bar_cells
    evidence: head index: 0 production references; plan Task 1 step 8 code block; only test callers ran it   verify: revgate recheck 3f9a01c2
advisory src/app/marks.py:140 test.changed_line_unexecuted lines 151-158 (the end-of-bar branch) run under no selected test
details: <state>/runs/T1/ef45ab6.implementer.json
fix blocking findings, or add `<id>: dispute — <reason>` to your report's revgate-responses block. Advisory lines don't block; a test you add in response is shown to the wave reviewer. Don't suppress, skip, or weaken a test to clear a finding.
```

A clean run prints one line, such as
`revgate: clean  task T8  ef45ab6  (41 checks, 12 functions pinned)`. A run that hit a
limit or has an incomplete stage says `provisional` on its status line.

## The wave gate

When the wave's membership is frozen, the controller runs the wave gate once, in this
order:

1. **Integrity.** `revgate wave` checks that every task head has a controller run file
   with a matching SHA, that the `revgate` source hash didn't change during the wave, that
   no task edited a file it didn't declare, and that every task landed.
2. **The full gate.** In the reusable gate worktree, run the repository's
   `[gates.wave]` commands from `.review.toml`, each through
   `revgate gate-if-changed -- <command>`. On an unchanged tree, a cached pass costs
   nothing.
3. **End-to-end tiers.** At a wave gate, run the end-to-end suite in one browser, and
   only when the wave's combined diff touches a path in `[e2e].web_paths`. The full
   cross-browser run happens once, at the branch gate.
4. **The focused review.** It runs only when the wave's focus list isn't empty, on the
   last wave too. Dispatch `wave-reviewer-domain` when a `risk: high` task is in focus,
   and `wave-reviewer` otherwise. For each focused task, give it the task's diff, its
   owned files, its **Interfaces** block, its `risk`, its focus reasons, and its advisory
   findings as obligations to confirm or clear. The reviewer reads nothing outside that
   list except the context the task's section names, and answers each obligation
   CONFIRMED, REFUTED, or UNSURE with file-and-line evidence.

The next wave starts when all four steps are clean, or when each open finding has a
ledger ruling. A gate failure gets one fix dispatch and one scoped re-run; see "Fix
loops".

## The branch gate

One branch review runs before merge, over the whole branch diff:

1. Run `revgate map` to build the review's map from the branch's run files. It marks
   each area deep or cleared, with reasons.
2. Dispatch one `branch-reviewer`. It goes deep on flagged tasks, `risk: high` tasks,
   domain areas, and cross-wave interactions, and skims cleared areas as a safety net
   for defects `revgate` can't detect. Give it the ledger's deferred and parked lines
   and the wave-review results. When the branch diff exceeds 3,000 changed non-test
   lines, split the review by subsystem into parallel reviewers.
3. Run `silent-failure-hunter` beside it, unconditionally.
4. Run the `semgrep` CLI beside it: `semgrep --config=auto <source dirs> --error`.
5. Run the repository's pre-merge gate once with the cache turned off, for example
   `make pre-push GATE_FLAGS=--no-cache`, plus the full cross-browser end-to-end run.
6. On the first two pilot branches, also run the previous review trio at full depth and
   compare its findings with the branch review's, to measure what the skim misses.

An area counts as cleared only when `revgate` flagged nothing there and its file type is
covered by active rules. In a project without a `.review.toml`, every rule runs in
shadow, so nothing is cleared and the branch review reads everything at full depth.

A change run inline, with no plan, is its own branch: it gets `revgate task` and the
branch gate, whose `branch-reviewer` runs on Opus 5.5 at high effort.

## Fix loops

Every loop has a bound, and every exit leaves a ledger line. The following table lists
each loop's bound and what happens when the bound is reached:

| Loop | Bound | On exhaustion |
|---|---|---|
| `plan-lint` errors | Three author rounds | Stop and report to the user, since the approved spec might be wrong. |
| `revgate task` blocking finding | Two implementer re-runs | One `adjudicator` dispatch rules on the task's open blocking findings; see "Adjudication". |
| `revgate` exit code `2` | None | A failure, never a pass: run the gates by hand, flag the task for the wave review, and file a `revgate` bug. |
| Wave review | Five rounds | Park the finding or record a ruling. |
| Wave gate failure | One fix dispatch and one scoped re-run | Adjudicate as a final review does, and record the ruling in the ledger. |
| Branch review | One fix dispatch and one scoped re-review | Residual findings go to the user at finishing. |

Exit code `2` means only one of four things: tree safety (a dirty tree, a mismatched
`HEAD`, or a repository root that is the home directory), a required gate that couldn't
run, an ambiguous report path, or a crash of the index or the change model. Behavioral
incompleteness, an engine's exception, and environmental causes don't exit `2`. `revgate`
records them, sets `focus`, and sends them to the controller, which resolves them before
the wave passes; they never block the implementer.

## Adjudication

When a task still has a blocking finding after the implementer's two re-runs, dispatch
one `adjudicator`. It rules once on every open blocking finding of that task and records
each ruling:

```
revgate mark <id> tp|fp --ruling --plan <plan> --note "<reason>"
```

`--plan` names the plan whose ledger the ruling lands in; without it the command exits
2 and records nothing. An `fp` ruling explains the finding on the controller's next run,
so the adjudicator doesn't add a suppression comment (`revgate` reads no
`revgate-ignore` marker). If a ruling does change a file, the adjudicator commits it
before replying, because `revgate task` exits 2 on uncommitted changes.

A ruling is a false positive, a plan ruling, or a ruling for the wave reviewer to
confirm, which sets the task's `focus`. If a task has more than five blocking findings
after its first round, don't adjudicate: the controller takes the task back as likely
mis-scoped and fixes the plan or re-splits the task.

## Attribution and escapes

Attribution measures whether the deterministic layer is holding quality:

- For each Critical or Important finding from a wave or branch review, run `git blame`
  over the task commits to name the task that introduced the defect.
- Record in the ledger whether `revgate` had flagged that task.
- An **escape** is a Critical or Important finding in a task that `revgate` didn't
  flag. It escaped the deterministic layer.
- A **late catch** is a branch-review finding in a task that a later wave built on.

Because the branch review only skims cleared areas, attribution under-counts escapes
from unflagged tasks. Read the escape rate as a floor. The benchmark, the pilot trio
comparison, and a lagging check of `fix:` commits on `main` offset that bias.

## Resume notes

At each wave gate, the controller writes a resume note with these fields:

- The plan path.
- The ledger path.
- The next wave.
- The open rulings.
- The installed `revgate` version.

For a plan of three or more waves, the controller recommends a fresh session at a wave
gate, which starts from the resume note.

## Wave sizing

Size waves to keep gates few and slots full:

- Use as few waves as the dependencies allow. Each wave costs one gate, plus one more
  per gate fix.
- Balance task sizes inside a wave, so one long task doesn't hold the wave's gate while
  every other slot idles.
- Put a task that depends on a `risk: high` task, or on a task that owns a
  `[risk.paths]` file, in a later wave, after the focused review. `revgate plan-lint`
  refuses a plan that doesn't.

## Throughput and verification

The limit on a multi-task plan is usually the machine, not the model. Measured on one
project's multi-wave redesign (2026-09-24 to 25, an 8-core, 16 GB Apple M3):
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
  Remote implementers (`isolation: "remote"`) wait until `revgate` installs
  remotely: an implementer must run `revgate task` before it reports DONE, so a
  remote subagent can't implement a task yet.
- **Targeted tests per task.** An implementer runs the tests for the files it
  touched (for example `pytest tests/test_<module>.py`, `npx vitest related
  <files>`), plus the type-check. The full suite, `make pre-push`, and the
  end-to-end suite belong to the wave gate.
- **One gate per wave, frozen first.** Decide the wave's membership, merge every
  task, and only then run the gate. A task that lands after the gate started means
  a second full gate (wave 3 of that redesign ran two back to back).
- **One reusable gate worktree.** Keep a single gate worktree per branch, check out
  the commit to test, and reinstall dependencies only when a lockfile changed
  (`npm ci` when `package-lock.json` changed, `uv sync --all-extras` when `uv.lock`
  changed). Don't build a fresh worktree for each gate.
- **One browser at the wave gate, cross-browser once before merge.** The wave gate
  runs end-to-end checks in one browser, and only when the wave touched a path in
  `[e2e].web_paths`. The other browsers run once, at the branch gate before merge,
  when no implementer is running.
- **Keep the machine awake.** A long run on a laptop needs the app's keep-awake
  setting on: a 2h13m outage mid-wave cut off three tasks in that run.

## revgate reference

`revgate` never calls an LLM. The following table lists its commands:

| Command | What it does |
|---|---|
| `revgate task` | Reviews one task's diff (`--repo`, `--base`, `--head`, `--role`, optional `--plan` and `--task`) and writes a run file and a summary. |
| `revgate wave` | Checks a wave's integrity (`--repo`, `--base`, `--head`, `--plan`, `--wave`): run files per task head, one source hash, no undeclared edits, every task landed. |
| `revgate map` | Builds the branch review's map, marking each area deep or cleared with reasons. |
| `revgate plan-lint` | Lints a plan's `plan-waves` block and writes a report file. |
| `revgate plan brief` | Prints one task's block for the controller to append to the task brief. |
| `revgate recheck` | Re-runs the recorded task statically, so every printed `verify:` line works. |
| `revgate gate-if-changed` | Runs `-- <command>`, or skips it when it passed on this exact tree; `--no-cache` forces a run. |
| `revgate gate-stats` | Prints the gate cache's hit rate, optionally `--since` a period such as `7d`. |
| `revgate mark` | Records a label or ruling (`tp` or `fp`) for one finding in the findings log. |
| `revgate label` | Records labels for a batch of unlabeled findings. |
| `revgate bench` | Replays the benchmark cases against `revgate` and reports recall. |

Every command exits with one of three codes:

- `0`: clean, or advisory findings only.
- `1`: a blocking finding or a failed gate.
- `2`: tree safety, a gate that couldn't run, an ambiguous report path, or a crash of
  the index or the change model. Code `2` is never read as a pass.

`revgate` keeps its state in `$(git rev-parse --git-common-dir)/revgate/`, which every
worktree of a repository shares. Run files are canonical JSON, byte-identical for
identical inputs, with timings in a separate sidecar file. Without a `.review.toml`,
every rule except `gate.failed` runs in shadow.
