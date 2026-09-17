#!/usr/bin/env python3
"""PreToolUse hook: block git operations that would commit .env files.

A .gitignore already excludes .env and .env.*, so this guards the remaining
paths around it: explicit `git add .env` (or any `.env*`-prefixed token,
including glob patterns the shell hasn't expanded), force/broad adds
(`git add -f`, `-A`, `--all`) when a .env file exists anywhere in the
project, and commits where a .env file somehow ended up staged.

Additionally, if Jev (TypeSafe) is available (TYPESAFE_API_KEY set and
typesafe_sdk installed), a semantic check detects indirect secret exfiltration
— commands that read, copy, rename, or encode .env content into a different
file or output channel without staging the secret file directly. This additive
check never weakens the unconditional deterministic checks above.
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


def _jev_secret_check(command: str, env_files: list[str]) -> None:
    """Semantic check: does this command exfiltrate .env content indirectly?

    Runs only when Jev is available and env files exist. Fails open — any
    ImportError, missing API key, or SDK exception is silently swallowed.
    """
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        return

    try:
        from typesafe_sdk import Noul, TypeSafeClient  # type: ignore[import]
    except ImportError:
        return

    try:
        client = TypeSafeClient(api_key=api_key, model="jev-latest")
        env_list = ", ".join(env_files)
        response = client.system_one(
            {"command": command, "env_files": env_list},
            {
                "secret_leak_risk": Noul(
                    instructions=(
                        f"Given these secret files exist in the project ({env_list}), "
                        "does this shell command read, copy, rename, encode, or otherwise "
                        "move their contents into a different file or output channel "
                        "before/without staging the original secret file directly?"
                    )
                )
            },
        )
        conf = response.nouls["secret_leak_risk"].noul

        if conf >= 0.85:
            deny(
                f"Blocked (Jev): this command appears likely to exfiltrate secret file "
                f"contents indirectly (confidence {conf:.0%}). "
                "Do not copy, encode, or redirect .env content into other files."
            )
        elif conf >= 0.5:
            warn(
                f"Warning (Jev): this command may move secret file contents into "
                f"another location (confidence {conf:.0%}). "
                "Confirm this does not leak .env data."
            )
    except Exception:  # noqa: BLE001
        # Fail open: any network, auth, or SDK error is swallowed silently.
        pass


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

    # Semantic exfiltration check: only when env files exist and Jev is available.
    # This is additive — the deterministic checks above fire unconditionally first.
    is_git_add_or_commit = "add" in tokens or re_search_commit(command)
    if is_git_add_or_commit:
        env_files = find_env_files(project_dir)
        if env_files:
            _jev_secret_check(command, env_files)

    sys.exit(0)


if __name__ == "__main__":
    main()
