# revgate

`revgate` is a deterministic reviewer for tasks and waves of an implementation plan, and
it provides `gate-if-changed`, a cache for gate commands such as `make pre-push`. It runs
static checks only, uses nothing outside the Python standard library at runtime, and never
calls a language model. Version 0.1.0 ships `gate-if-changed` and `gate-stats`; the review
commands arrive in later versions.

## Install

To install a tagged release as a non-editable `uv` tool, export the tag and install from
the export:

```bash
git -C ~/Developer/sebby archive revgate-v0.1.0 revgate | tar -x -C BUILD_DIR
uv tool install --force --python 3.12 BUILD_DIR/revgate
revgate --version
```

Replace `BUILD_DIR` with an empty scratch directory.

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
  exits `0`.
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
