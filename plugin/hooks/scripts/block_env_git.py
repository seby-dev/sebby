#!/usr/bin/env python3
"""PreToolUse hook: block git operations that would commit .env files.

A .gitignore already excludes .env and .env.*, so this guards the remaining
paths around it: explicit `git add .env` (or any `.env*`-prefixed token,
including glob patterns the shell hasn't expanded), force/broad adds
(`git add -f`, `-A`, `--all`) when a .env file exists anywhere in the
project, and commits where a .env file somehow ended up staged.
"""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path


def deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def warn(message: str) -> None:
    print(json.dumps({"systemMessage": message}))


def is_env_path(path: str) -> bool:
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return name.startswith(".env")


def find_env_files(root: str) -> list[str]:
    try:
        return [str(p) for p in Path(root).glob(".env*") if p.is_file()]
    except OSError:
        return []


def re_search_commit(command: str) -> bool:
    return bool(re.search(r"\bgit\b[^|;&]*\bcommit\b", command))


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    command = (payload.get("tool_input") or {}).get("command", "")
    if "git" not in command:
        sys.exit(0)

    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", ".")

    if "add" in tokens:
        if any(is_env_path(t) for t in tokens):
            deny(
                "Blocked: .env files contain secrets and must never be staged. "
                "They are gitignored — do not add them, with or without -f, "
                "and not via a glob pattern either."
            )

        broad_flags = {"-f", "--force", "-A", "--all"}
        if broad_flags & set(tokens):
            env_files = find_env_files(project_dir)
            if env_files:
                deny(
                    "Blocked: `git add` with a broad or force flag (-f/-A/--all) "
                    f"while .env files exist in the project ({', '.join(env_files)}). "
                    "Stage specific non-.env files by name instead."
                )

    if re_search_commit(command):
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            cwd=project_dir,
        )
        if result.returncode != 0:
            warn(
                "Warning: could not verify staged files for .env safety "
                f"(git diff failed: {result.stderr.strip() or 'unknown error'}). "
                "Double-check `git status` before committing."
            )
            sys.exit(0)
        offenders = [p for p in result.stdout.splitlines() if is_env_path(p)]
        if offenders:
            deny(
                "Blocked: staged .env files detected "
                f"({', '.join(offenders)}). Run `git restore --staged <file>` "
                "before committing."
            )

    sys.exit(0)


if __name__ == "__main__":
    main()
