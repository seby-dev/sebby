# revgate

`revgate` is a deterministic reviewer for tasks and waves of an implementation plan, and
it provides `gate-if-changed`, a cache for gate commands such as `make pre-push`. It runs
static checks only, uses nothing outside the Python standard library at runtime, and never
calls a language model. Version 0.2.0 adds the review commands: `task`, `wave`, `map`,
`recheck`, `plan-lint`, `plan brief`, `mark`, and `label`.

## Install

To install a tagged release as a non-editable `uv` tool, export the tag and install from
the export:

```bash
git -C ~/Developer/sebby archive revgate-v0.2.0 revgate | tar -x -C BUILD_DIR
uv tool install --force --python 3.12 BUILD_DIR/revgate
revgate --version
```

Replace `BUILD_DIR` with an empty scratch directory.

## Commands

Every command that takes `--repo` resolves the repository's toplevel first. It exits `2`
without reading anything when the toplevel is `$HOME` or isn't the `--repo` path.
`--repo` defaults to the current directory's toplevel.

- `revgate task --repo R --base B --head H --role implementer|controller [--plan P --task T]
  [--report PATH] [--plan-rev REV] [--round N]`: reviews one task. It runs the
  `[gates.task]` commands, then every static rule, then the verdict, and prints the
  summary. It reads `.review.toml` and the plan at `--base` (or the plan at
  `--plan-rev`), never from the worktree.
- `revgate wave --repo R --base B --head H --plan P --wave W [--gate-log PATH]`: checks a
  merged wave's integrity. Every task needs a controller run file that the merged head
  contains, every run used one `revgate` source hash, no task changed a file it doesn't
  own or run (lockfiles aside), and `revgate`'s own source checkout is clean.
  `--gate-log` records the wave gate's failing tests for the next wave's tasks.
- `revgate map --repo R --base B --head H [--plan P] [--json]`: lists each changed area
  (a function, or a 25-line bucket outside one) with its status and reasons. In this
  version every area is `deep`, because pinning and fail-first aren't built yet.
- `revgate recheck --repo R --head H FINDING_ID...`: re-runs the static review of the
  run that produced each finding, at `--head`, and prints `fixed` or `still present`.
- `revgate plan-lint PLAN [--repo R --base B] [--cap 5]`: checks a plan's `plan-waves`
  block and prints the computed duration table.
- `revgate plan brief PLAN TASK [--repo R --head H]`: prints one task's block entry and
  prose section, for a controller to append to a brief.
- `revgate gate-if-changed [--max-age AGE] [--no-cache] -- CMD...`: runs a gate command
  unless it already passed on identical inputs; see the following section.
- `revgate gate-stats [--since AGE]`: prints the gate cache's hit rate.
- `revgate mark FINDING_ID tp|fp [--note TEXT] [--ruling] [--plan PLAN] [--labeler NAME]
  [--repo R]`: records an explicit label. With `--ruling`, it also records an
  adjudication in the plan's ledger.
- `revgate label [--list N | --stats] [--repo R]`: lists findings with no explicit label,
  or prints each rule's true and false positives and the Wilson lower bound of its
  precision.

## Exit codes

The review commands exit with one of the following codes:

- `0`: clean, or advisory findings only.
- `1`: a blocking finding, or a failed gate.
- `2`: tree safety (the `$HOME` repository, a dirty worktree, or `HEAD` that isn't
  `--head`), a gate that timed out or couldn't start, an ambiguous report path, a
  malformed plan or `.review.toml`, or a crash of the index or the change model.

An exception inside one rule doesn't exit `2`. The run records `internal:<rule>` in its
`incomplete` list and sets `focus`, so the controller resolves it before the wave passes.
Exit `2` is never a pass.

## The summary

`revgate task` prints at most 20 lines. The first line is the status, for example
`revgate: 1 blocking, 2 advisory printed (+1 counted)  task T1  ef45ab6  round 1`, with
the focus reasons when the task is focused. Each blocking finding follows as
`file:line rule message`, then at most five advisory findings graded E0 or E1, each
marked `advisory`. Evidence lines follow while the cap allows, then the run file's path.
Shadow findings never print. A clean run is one line.

Without `.review.toml`, every rule except `gate.failed` runs in shadow. The run file and
the findings log still record every finding, for calibration.

## State

All state lives in `$(git rev-parse --git-common-dir)/revgate/`, so every worktree of a
repository shares it:

- `runs/<task>/<head>.<role>.json`: the run file, canonical JSON that's byte-identical
  for identical inputs. The `.meta.json` sidecar beside it holds timings, gate output
  tails, and untracked files.
- `runs/wave-<id>/<head>.wave.json`: a wave's run file.
- `results/runs/`: the run cache, keyed by base, head, plan task, configuration,
  version, and source hash. Only a run whose gates passed and whose rules all finished
  within budget is cached.
- `findings.jsonl`: the append-only findings log and labels.
- `ledger/<plan>.jsonl`: controller runs, deferred obligations, and rulings.
- `maps/<head>.json`: the output of `revgate map`.
- `spi/`: index facts, keyed by blob.
- `gate/`: the `gate-if-changed` cache.

## Run a gate through the cache

Put the gate command after `--`:

```bash
revgate gate-if-changed -- make pre-push
```

If the same command already passed in the same directory on identical working-tree
content, `revgate` skips it and prints one line in the following format:

```
gate-if-changed: PASS (cached 14:02, tree 3f9a1c2, saved 212 s)
```

Otherwise it runs the command, passes its output through, and exits with its exit code.
Only a pass is cached. The command runs uncached when the directory isn't inside a git
repository, and `revgate` exits `2` without running it when the repository root is
`$HOME`.

The command accepts the following flags:

- `--no-cache`: run the command even on a cache hit, and record a fresh pass if it
  exits `0`. If it exits non-zero, `revgate` removes the cached pass for that key, so
  the next plain run runs the command again.
- `--max-age AGE`: treat an older pass as a miss. `AGE` is a number followed by `s`,
  `m`, `h`, or `d`, for example `24h`. The default is `[gates.cache] max_age` in
  `.review.toml`, or 24 hours.

To see the cache's hit rate and remove expired entries, run `revgate gate-stats`, or
`revgate gate-stats --since 7d` for the last seven days.

## What the key covers

The cache key is a SHA-256 hash of the following inputs:

- The working tree's content as `git add -A` would stage it: tracked, staged, unstaged,
  and untracked non-ignored files, with their modes and symlinks. `revgate` builds the
  tree in a temporary index and never touches the real one.
- The command and its arguments.
- The command's directory relative to the repository root.
- The environment variables that `[gates.cache] env_allowlist` in `.review.toml` names.
- The `revgate` version.

Git-ignored files and external tools, such as the installed Python or Node version,
aren't in the key. To make one of them count, add an environment variable that carries
it to `env_allowlist`.

The cache lives in `$(git rev-parse --git-common-dir)/revgate/gate/`, so every worktree
of a repository shares it. A per-key lock makes a second identical run wait for the
first one and reuse its result.

## Development

To run the lint, format, type, and test checks, run `make check` in this directory.
