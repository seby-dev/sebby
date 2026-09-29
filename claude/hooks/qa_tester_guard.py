#!/usr/bin/env python3
"""PreToolUse hook for the QA swarm's tester agents.

Best effort, and fail open: any exception, bad input, or missing pointer file exits 0 with no
output (exit 2 from a PreToolUse hook blocks the tool call, so this hook never exits 2). It
signals a denial only through its JSON output. It denies: a `.env` read, the dev key, the run's
private folder, the eval and extraction scripts, `env`, `printenv`, and the shell builtins that
list the environment, `qa_env` outside `scenario-run`, `git push` and `git commit`, a loopback
port that isn't the run's own, and a write outside the run's `testers/` folder or into the
repository. It appends every command to `testers/<name>/commands.log`. A tester can build a
command these patterns miss; the orchestrator's `git status` check and the backend log scan
are the backstops.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ACTIVE_ENV = "QA_ACTIVE_FILE"
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
READ_TOOLS = frozenset({"Read"})

# Text a tester may not name in a command, or in a Read, Write, or Edit path.
PATH_DENIALS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<![\w.])\.env(?![A-Za-z0-9_])"), "a .env file"),
    (re.compile(r"env/private"), "the run's private folder"),
    (re.compile(r"organist_bot"), "the organist_bot folder"),
    (re.compile(r"/proc/[^\s/]+/environ"), "a process environment"),
)
# The end of a shell word: whitespace, the end, or a shell operator.
_END = r"(?=\s|$|[;&|()<>`])"
# The start of a simple command: the start, or after an operator, `$(`, or a backtick.
_CMD = r"(?:^|[;&|(`]|\$\()\s*"
_LISTS_ENV = "a shell builtin that lists the environment"
COMMAND_DENIALS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"dev-key"), "the shared dev key"),
    *PATH_DENIALS,
    (re.compile(r"scripts/extract_hymn\.py"), "scripts/extract_hymn.py"),
    (re.compile(r"scripts/eval_\w+"), "an eval_* script"),
    (re.compile(r"(?:^|[\s;&|(`/])(?:env|printenv)" + _END), "env or printenv"),
    # Bare `set`, `export`, `declare`, or `typeset`, or `export -p` and `declare -x`.
    (
        re.compile(
            _CMD + r"(?:set|(?:export|declare|typeset)(?:\s+-[a-zA-Z]*[px][a-zA-Z]*)?)"
            r"\s*(?:$|[;&|)`])"
        ),
        _LISTS_ENV,
    ),
    (re.compile(r"\bcompgen\s+-\w*[ev]"), _LISTS_ENV),
    (re.compile(r"(?:TYPESAFE_API_KEY|ANTHROPIC_API_KEY)"), "a provider key variable"),
    (re.compile(r"qa_env\.sh(?!\s+scenario-run\b)"), "qa_env.sh outside scenario-run"),
    (re.compile(r"-m\s+\S*qa_env\b"), "the qa_env package"),
    (
        re.compile(r"\bgit(?:\s+(?:-[Cc]\s+\S+|--\S+))*\s+(?:push|commit)\b"),
        "git push or git commit",
    ),
)
PORT_RE = re.compile(
    r"(?:127(?:\.\d{1,3}){3}|localhost|0\.0\.0\.0|\[::1?\]|\[::ffff:127(?:\.\d{1,3}){3}\])"
    r":(\d{1,5})",
    re.IGNORECASE,
)
REDIRECT_RE = re.compile(r">>?\|?\s*(\"[^\"]+\"|'[^']+'|[^\s;&|<>]+)")
TEE_RE = re.compile(r"\btee\s+((?:-\S+\s+)*)(\"[^\"]+\"|'[^']+'|[^\s;&|<>]+)")
# Commands that write their last argument, and commands that write every path argument.
LAST_ARG_VERBS = frozenset({"cp", "mv", "install", "ln", "rsync"})
ALL_ARG_VERBS = frozenset({"touch", "mkdir", "rm", "rmdir", "truncate", "unlink"})
WRAPPERS = frozenset({"sudo", "command", "nohup", "exec", "time"})
SPLIT_RE = re.compile(r"\s*(?:;|&&|\|\||\||\n)\s*")
CD_RE = re.compile(r"cd\s+(\"[^\"]+\"|'[^']+'|\S+)\s*$")
TESTER_RE = re.compile(r"testers/([A-Za-z0-9_-]+)")


def active_file() -> Path:
    override = os.environ.get(ACTIVE_ENV)
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "sebby" / "qa-active.json"


def load_active() -> dict[str, object] | None:
    try:
        data = json.loads(active_file().read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError):
        return None
    return data if isinstance(data, dict) else None


def _ports(active: dict[str, object]) -> set[int]:
    raw = active.get("ports")
    if not isinstance(raw, list):
        return set()
    return {p for p in raw if isinstance(p, int) and not isinstance(p, bool)}


def _folder(active: dict[str, object] | None, key: str) -> Path | None:
    value = active.get(key) if active else None
    return Path(value) if isinstance(value, str) and value else None


def _under(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
    except (ValueError, OSError, RuntimeError):
        return False
    return True


def _resolve(target: str, cwd: str) -> Path:
    path = Path(target).expanduser()
    return path if path.is_absolute() else Path(cwd) / path


def _unquote(word: str) -> str:
    return word.strip("\"'")


def _part_targets(part: str) -> list[str]:
    """The paths one simple command writes: redirects, `tee`, and the write verbs."""
    targets = [_unquote(m) for m in REDIRECT_RE.findall(part)]
    targets += [_unquote(m[1]) for m in TEE_RE.findall(part)]
    tokens = [_unquote(t) for t in part.split()]
    while tokens and ("=" in tokens[0] or tokens[0] in WRAPPERS):
        tokens = tokens[1:]  # skip VAR=value prefixes and wrappers
    if not tokens:
        return targets
    verb = os.path.basename(tokens[0])
    flags = [t for t in tokens[1:] if t.startswith("-")]
    args = [t for t in tokens[1:] if not t.startswith(("-", ">", "<")) and ">" not in t]
    if verb in LAST_ARG_VERBS and args:
        targets.append(args[-1])
    elif verb in ALL_ARG_VERBS:
        targets += args
    elif verb == "sed" and any(f.startswith("-i") or f == "--in-place" for f in flags):
        targets += args[1:]  # the first argument is the script
    elif verb == "perl" and any("i" in f and not f.startswith("--") for f in flags):
        targets += args[1:]
    return targets


def _writes(command: str, cwd: str) -> list[Path]:
    """Every path a Bash command appears to write, following `cd` between commands."""
    paths: list[Path] = []
    for raw in SPLIT_RE.split(command):
        part = raw.strip().lstrip("(").strip()
        cd = CD_RE.match(part)
        if cd:
            cwd = str(_resolve(_unquote(cd.group(1)), cwd))
            continue
        for target in _part_targets(part):
            if target and not target.startswith("&") and target != "/dev/null":
                paths.append(_resolve(target, cwd))
    return paths


def _path_of(tool_input: dict[str, object]) -> str:
    return str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")


def _decide_file(tool: str, raw: str, cwd: str, testers: Path | None) -> str | None:
    for pattern, what in PATH_DENIALS:
        if pattern.search(raw):
            return f"This path is {what}, which QA testers may not use."
    if tool in WRITE_TOOLS and testers is not None and not _under(_resolve(raw, cwd), testers):
        return f"{tool} is limited to {testers}."
    return None


def decide(payload: dict[str, object], active: dict[str, object] | None) -> str | None:
    """A denial reason, or None to allow."""
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or not isinstance(tool, str):
        return None
    cwd = str(payload.get("cwd") or "/")
    run_dir = _folder(active, "run_dir")
    repo_root = _folder(active, "repo_root")
    testers = run_dir / "testers" if run_dir else None

    if tool in READ_TOOLS or tool in WRITE_TOOLS:
        return _decide_file(tool, _path_of(tool_input), cwd, testers)
    if tool != "Bash":
        return None

    command = str(tool_input.get("command") or "")
    for pattern, what in COMMAND_DENIALS:
        if pattern.search(command):
            return f"This command uses {what}, which QA testers may not use."
    if active is not None:
        ports = _ports(active)
        for match in PORT_RE.findall(command):
            if int(match) not in ports:
                return f"Port {match} isn't this run's instance; its ports are {sorted(ports)}."
    if repo_root is not None:
        for path in _writes(command, cwd):
            if _under(path, repo_root) and not (testers and _under(path, testers)):
                return f"This command writes {path}, inside the repository under test."
    return None


def _log(payload: dict[str, object], reason: str | None, active: dict[str, object] | None) -> None:
    run_dir = _folder(active, "run_dir")
    tool_input = payload.get("tool_input")
    if run_dir is None or not isinstance(tool_input, dict):
        return
    # Attribute by the command or path only: a Write's content must not pick the folder.
    command = str(tool_input.get("command") or _path_of(tool_input))
    match = TESTER_RE.search(command)
    folder = run_dir / "testers" / (match.group(1) if match else "_unattributed")
    folder.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(UTC).isoformat(),
        "tool": payload.get("tool_name"),
        "agent_id": payload.get("agent_id"),
        "agent_type": payload.get("agent_type"),
        "decision": "deny" if reason else "allow",
        "command": command,
    }
    with (folder / "commands.log").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        active = load_active()
        reason = decide(payload, active)
        if reason:
            # Print the denial before anything that can fail, and flush it: a broken log
            # must never lose a denial.
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": reason,
                        }
                    }
                ),
                flush=True,
            )
        try:
            _log(payload, reason, active)
        except (OSError, RuntimeError, ValueError):
            pass  # the log is best effort
    except Exception:  # noqa: BLE001  (fail open: a broken hook must never block a tool call)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
