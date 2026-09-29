#!/usr/bin/env python3
"""PreToolUse hook for the QA swarm's tester agents.

Best effort. It signals a denial only through its JSON output and always exits 0 (exit 2 from a
PreToolUse hook blocks the tool call). Bad input or a missing or malformed pointer file gives no
output, except that the static denials below still apply. An unexpected error while checking a
Bash, Read, or write-tool call fails closed (a denial) when the pointer file is valid, and fails
open when it isn't.

It denies: a `.env` read (also through simple quote, backslash, or `[...]` and `?` glob
obfuscation), the dev key, the run's private folder, the eval and extraction scripts, `env`,
`printenv`, the shell builtins and interpreter calls that dump the environment (`os.environ`,
`process.env`, `%ENV`, `getenv()`, `ps e`), `qa_env` outside `scenario-run`, `git push`,
`git commit`, `playwright-cli close-all` and `kill-all`, and the git verbs that edit or hide the
tree under test, a loopback port that isn't the run's own, and a write outside the run's
`testers/` folder or into the repository.
It appends every command to `testers/<name>/commands.log`.

Known limits: the Write check can't tell one tester's folder from another's, so a tester can
write into a sibling's `testers/<name>/` folder. A single-variable read by literal name
(`os.environ.get("HOME")`, `process.env.HOME`) is allowed. A tester can build a command these
patterns miss (a `.*` glob, `$'\\x2e'` escapes, a path built in a variable, a script that writes
or connects for it); the orchestrator's `git status` check and the backend log scan are the
backstops.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path

ACTIVE_ENV = "QA_ACTIVE_FILE"
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
READ_TOOLS = frozenset({"Read"})
GUARDED_TOOLS = WRITE_TOOLS | READ_TOOLS | {"Bash"}

# Text a tester may not name in a command, or in a Read, Write, or Edit path.
PATH_DENIALS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<![\w.])\.env(?![A-Za-z0-9_])"), "a .env file"),
    (re.compile(r"env/private"), "the run's private folder"),
    (re.compile(r"organist_bot"), "the organist_bot folder"),
    (re.compile(r"/proc/[^\s/]+/environ"), "a process environment"),
)
# The end of a shell word: whitespace, the end, or a shell operator.
_END = r"(?=\s|$|[;&|()<>`])"
# The start of a simple command: the start, a newline, an operator, `{`, `$(`, or a backtick,
# then any shell keywords, wrappers, and `VAR=value` prefixes.
_CMD = (
    r"(?:^|[\n;&|(`{!]|\$\()\s*"
    r"(?:(?:then|do|else|elif|if|while|until|sudo|command|nohup|exec|time|xargs|nice|\w+=\S*)\s+)*"
)
_WORD = r"[^\s;&|()<>`]"
_LISTS_ENV = "a shell builtin that lists the environment"
_DUMPS_ENV = "a call that dumps the environment"
_EDITS_TREE = "a git command that edits or hides the tree under test"
COMMAND_DENIALS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"dev-key"), "the shared dev key"),
    *PATH_DENIALS,
    (re.compile(r"scripts/extract_hymn\.py"), "scripts/extract_hymn.py"),
    (re.compile(r"scripts/eval_\w+"), "an eval_* script"),
    # `env` or `printenv` in command position, or as a `-c` script.
    (
        re.compile(r"(?:" + _CMD + r"|-c\s+['\"]?)(?:" + _WORD + r"*/)?(?:env|printenv)" + _END),
        "env or printenv",
    ),
    # Bare `set`, `export`, `declare`, or `typeset`, or `export -p` and `declare -x`.
    (
        re.compile(
            _CMD + r"(?:set|(?:export|declare|typeset)(?:\s+-[a-zA-Z]*[px][a-zA-Z]*)?)"
            r"\s*(?:$|[\n;&|)`}])"
        ),
        _LISTS_ENV,
    ),
    (re.compile(r"\bcompgen\s+-\w*[ev]"), _LISTS_ENV),
    # Interpreter dumps; a single read by name (`.get(`, `[`, `.NAME`) is allowed.
    (re.compile(r"(?<![\w/])environb?\b(?!\s*(?:\.get\s*\(|\[))"), _DUMPS_ENV),
    (re.compile(r"\bprocess\.env\b(?!\s*(?:\.\w|\[))"), _DUMPS_ENV),
    (re.compile(r"%ENV\b|\bENV\.(?:to_[ah]|inspect|each\w*|keys|values)\b"), _DUMPS_ENV),
    (re.compile(r"\bgetenv\s*\(\s*\)"), _DUMPS_ENV),
    (
        re.compile(
            _CMD + r"(?:" + _WORD + r"*/)?ps(?:\s+" + _WORD + r"+)*?\s+-?[a-zA-Z]*[eE]"
            r"[a-zA-Z]*" + _END
        ),
        _DUMPS_ENV,
    ),
    (re.compile(r"(?:TYPESAFE_API_KEY|ANTHROPIC_API_KEY)"), "a provider key variable"),
    (re.compile(r"qa_env\.sh(?![\"']?\s+scenario-run\b)"), "qa_env.sh outside scenario-run"),
    (re.compile(r"-m\s+\S*qa_env\b"), "the qa_env package"),
    (
        re.compile(r"\bplaywright-cli(?:\s+" + _WORD + r"+)*?\s+(?:close-all|kill-all)" + _END),
        "playwright-cli close-all or kill-all, which closes every tester's sessions",
    ),
    (
        re.compile(r"\bgit(?:\s+(?:-[Cc]\s+\S+|--\S+))*\s+(?:push|commit)\b"),
        "git push or git commit",
    ),
    (
        re.compile(
            r"\bgit(?:\s+(?:-[Cc]\s+\S+|--\S+))*\s+(?:checkout|restore|reset|stash|clean|apply"
            r"|switch|rebase|merge|cherry-pick|revert|am|pull|rm|mv)" + _END
        ),
        _EDITS_TREE,
    ),
)
# Checked only against the de-obfuscated command: a `.env` spelled with `?` or `*` globs.
GLOB_DENIALS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<![\w.])\.(?:[e?][n?](?:[v?]|\*)|e\*)(?![A-Za-z0-9_])"), "a .env file"),
)
# A loopback host in any spelling: 127/8 (short forms too), `0`, decimal, hex, and octal forms,
# `localhost` and its subdomains (with a trailing dot or not), and the IPv6 forms.
_HOST = (
    r"(?:(?<![\w.-])(?:127(?:\.\d{1,3}){1,3}|0(?:\.0){0,3}|21[34]\d{7}|0x7f[0-9a-f]{6}"
    r"|0177(?:\.[0-7]+){0,3}|(?:[\w-]+\.)*localhost\.?)(?![\w.-])"
    r"|\[(?:0{0,4}:){1,7}0{0,3}1?\]"
    r"|\[::ffff:(?:127(?:\.\d{1,3}){3}|7f[0-9a-f]{2}:[0-9a-f]{1,4})\])"
)
PORT_RES: tuple[re.Pattern[str], ...] = (
    # host:port, and a quoted host then a port, as in `("127.0.0.1", 8002)`.
    re.compile(_HOST + r"(?::|['\"]\s*,\s*)(\d{1,5})(?!\d)", re.IGNORECASE),
    # `nc`, `ncat`, `netcat`, or `telnet` with the port as its own word.
    re.compile(
        r"\b(?:nc|ncat|netcat|telnet)\s+(?:" + _WORD + r"+\s+)*?" + _HOST + r"\s+(\d{1,5})\b",
        re.IGNORECASE,
    ),
)
REDIRECT_RE = re.compile(r">>?\|?\s*(\"[^\"]+\"|'[^']+'|[^\s;&|<>]+)")
TEE_RE = re.compile(r"\btee\s+((?:-\S+\s+)*)(\"[^\"]+\"|'[^']+'|[^\s;&|<>]+)")
# Commands that write their last argument (or their `-t` folder), and every path argument.
LAST_ARG_VERBS = frozenset({"cp", "install", "ln", "rsync"})
TARGET_DIR_VERBS = frozenset({"cp", "install", "ln", "mv"})
ALL_ARG_VERBS = frozenset({"touch", "mkdir", "rm", "rmdir", "truncate", "unlink", "mv"})
WRAPPERS = frozenset({"sudo", "command", "nohup", "exec", "time"})
SPLIT_RE = re.compile(r"\s*(?:;|&&|\|\||\||\n)\s*")
CD_RE = re.compile(r"cd\s+(\"[^\"]+\"|'[^']+'|\S+)\s*$")
TESTER_RE = re.compile(r"testers/([A-Za-z0-9_-]+)")
BRACKET_RE = re.compile(r"\[([^\]]*)\]")


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
    # No exception is caught here: an unresolvable path reaches main, which fails closed.
    return path.resolve().is_relative_to(folder.resolve())


def _resolve(target: str, cwd: str) -> Path:
    try:
        path = Path(target).expanduser()
    except RuntimeError:
        path = Path(target)  # an unknown `~user`: bash leaves the word as it is
    return path if path.is_absolute() else Path(cwd) / path


def _unquote(word: str) -> str:
    return word.strip("\"'")


def _mask_quotes(command: str) -> str:
    """The command, same length, with every quoted or backslash-escaped character as `_`."""
    out: list[str] = []
    quote = ""
    escaped = False
    for ch in command:
        if escaped:
            escaped = False
            out.append("_")
        elif quote:
            if ch == quote:
                quote = ""
                out.append(ch)
            else:
                escaped = ch == "\\" and quote == '"'
                out.append("_")
        elif ch == "\\":
            escaped = True
            out.append("_")
        else:
            quote = ch if ch in "'\"" else ""
            out.append(ch)
    return "".join(out)


def _normalize(command: str) -> str:
    """The command with quotes and backslashes dropped and one-letter `[x]` globs as `x`."""
    plain = re.sub(r"[\"'\\]", "", command)
    return BRACKET_RE.sub(lambda m: m.group(1) if len(m.group(1)) == 1 else "?", plain)


def _words(part: str) -> list[str]:
    try:
        return shlex.split(part)
    except ValueError:
        return [_unquote(t) for t in part.split()]


def _target_dir(words: list[str]) -> str | None:
    """The folder named by `-t DIR`, `-tDIR`, or `--target-directory[=]DIR`, if any."""
    for index, word in enumerate(words):
        if word.startswith("--target-directory="):
            return word.split("=", 1)[1]
        if word == "--target-directory" or re.fullmatch(r"-[a-zA-Z]*t", word):
            return words[index + 1] if index + 1 < len(words) else None
        match = re.fullmatch(r"-t(.+)", word)
        if match:
            return match.group(1)
    return None


def _part_targets(part: str, masked: str) -> list[str]:
    """The paths one simple command writes: redirects, `tee`, and the write verbs.

    Redirects and `tee` are found in `masked` (quoted text hidden) and read from `part`.
    """
    targets = [_unquote(part[m.start(1) : m.end(1)]) for m in REDIRECT_RE.finditer(masked)]
    targets += [_unquote(part[m.start(2) : m.end(2)]) for m in TEE_RE.finditer(masked)]
    tokens = _words(part)
    while tokens and ("=" in tokens[0] or tokens[0] in WRAPPERS):
        tokens = tokens[1:]  # skip VAR=value prefixes and wrappers
    if not tokens:
        return targets
    verb = os.path.basename(tokens[0])
    flags = [t for t in tokens[1:] if t.startswith("-")]
    args = [t for t in tokens[1:] if not t.startswith(("-", ">", "<")) and ">" not in t]
    folder = _target_dir(tokens[1:]) if verb in TARGET_DIR_VERBS else None
    if folder is not None:
        targets.append(folder)
    if verb in ALL_ARG_VERBS or (verb == "install" and "-d" in flags):
        targets += args
    elif verb in LAST_ARG_VERBS and args and folder is None:
        targets.append(args[-1])
    elif verb == "sed" and any(f.startswith("-i") or f == "--in-place" for f in flags):
        targets += args[1:]  # the first argument is the script
    elif verb == "perl" and any("i" in f and not f.startswith("--") for f in flags):
        targets += args[1:]
    return targets


def _writes(command: str, cwd: str) -> list[Path]:
    """Every path a Bash command appears to write, following `cd` between commands."""
    paths: list[Path] = []
    masked = _mask_quotes(command)
    start = 0
    bounds = [(m.start(), m.end()) for m in SPLIT_RE.finditer(masked)]
    for end, next_start in [*bounds, (len(command), len(command))]:
        raw, raw_masked = command[start:end], masked[start:end]
        start = next_start
        lead = len(raw) - len(raw.lstrip(" \t("))
        part, part_masked = raw[lead:].rstrip(), raw_masked[lead:].rstrip()
        cd = CD_RE.match(part)
        if cd:
            cwd = str(_resolve(_unquote(cd.group(1)), cwd))
            continue
        for target in _part_targets(part, part_masked):
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


def _static_denial(command: str) -> str | None:
    normalized = _normalize(command)
    for pattern, what in COMMAND_DENIALS:
        if pattern.search(command) or pattern.search(normalized):
            return f"This command uses {what}, which QA testers may not use."
    for pattern, what in GLOB_DENIALS:
        if pattern.search(normalized):
            return f"This command uses {what}, which QA testers may not use."
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
    static = _static_denial(command)
    if static:
        return static
    if active is not None:
        ports = _ports(active)
        for pattern in PORT_RES:
            for match in pattern.findall(command):
                if int(match) not in ports:
                    return f"Port {match} isn't this run's instance; its ports are {sorted(ports)}."
    if repo_root is not None:
        for path in _writes(command, cwd):
            if _under(path, repo_root) and not (testers and _under(path, testers)):
                return f"This command writes {path}, inside the repository under test."
    return None


def _on_error(
    payload: dict[str, object], active: dict[str, object] | None, exc: Exception
) -> str | None:
    """Fail closed on a guarded tool when the run is active; fail open otherwise."""
    if active is None or payload.get("tool_name") not in GUARDED_TOOLS:
        return None
    return f"The QA guard hit an unexpected error ({type(exc).__name__}), so it denies this call."


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
        try:
            reason = decide(payload, active)
        except Exception as exc:  # noqa: BLE001  (an unexpected error must not allow the call)
            reason = _on_error(payload, active, exc)
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
    except Exception:  # noqa: BLE001  (bad input or a failed print: exit 0, never crash)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
