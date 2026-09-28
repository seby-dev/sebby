"""The gate phase: run a project's `[gates.task]` commands through `gate-if-changed`.

Each template expands its placeholders from the task's files (pipeline spec Appendix I,
"Gate phase"). A command whose placeholder expands to nothing is skipped, never run
unscoped, and a web command runs only when the task touches `web/`. A failing command is
one `gate.failed` finding, unless every test it names already failed at the wave's base
gate and its output reports no failure those names don't cover; a timeout or a command
that couldn't start is recorded as couldn't-run, which the caller turns into exit 2.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from revgate.config import Config, ConfigError, is_test_path
from revgate.gate_cache import run_gate_capture
from revgate.model import Finding, Grade, Source, canonical, new_finding
from revgate.store import atomic_write_text

GateCommandStatus = Literal["passed", "failed", "cached", "skipped", "timeout", "couldnt_run"]

PLACEHOLDER_NAMES = frozenset(
    {"py_files", "py_src_files", "py_test_files", "ts_files", "ts_test_files", "run_tests"}
)
WEB_PREFIX = "web/"
_TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_WEB_FLAGS = (("--prefix", "web"), ("-p", "web"))
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PYTEST_FAILED_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S.*?)(?:\s+-\s.*)?$")
_VITEST_FAIL_RE = re.compile(r"^\s*FAIL\s+(\S+\s+>\s+.+?)\s*$")
# A Vitest suite that failed as a whole (an import error): `FAIL  src/x.test.ts [ ... ]`.
_VITEST_SUITE_RE = re.compile(r"^\s*FAIL\s+(\S+\.(?:test|spec)\.[cm]?[jt]sx?)(?:\s+\[.*\])?\s*$")
# Any line that reports a failure, parsed or not.
_FAILURE_LINE_RE = re.compile(r"^\s*(?:FAIL|FAILED|ERROR)\b")
# pytest's closing summary: `=== 2 failed, 1 error in 0.3s ===`.
_PYTEST_COUNT_RE = re.compile(r"(\d+) (failed|errors?)\b")
_PYTEST_SUMMARY_RE = re.compile(r"^=+ .*\bin [\d.]+s\b.*=+$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
TAIL_LINES = 60
TAIL_CHARS = 8000
RULE = "gate.failed"


@dataclass(frozen=True)
class GateCommandResult:
    template: str
    argv: tuple[str, ...] | None  # None when skipped before expansion finished
    status: GateCommandStatus
    exit: int | None
    tail: str  # the output's tail, for the run's meta sidecar, never the run file
    failed_tests: tuple[str, ...]
    new_failures: tuple[str, ...]  # failed_tests less those failing at the wave base
    duration_s: float


@dataclass(frozen=True)
class GatePhaseResult:
    results: tuple[GateCommandResult, ...]
    findings: tuple[Finding, ...]
    couldnt_run: tuple[str, ...]  # templates that timed out or couldn't start

    def failed(self) -> bool:
        """A gate failed with something new; pre-existing failures and couldn't-run don't."""
        return bool(self.findings)


# --- placeholders ------------------------------------------------------------------------


def _dedupe(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def placeholders(
    repo: Path,
    *,
    changed: Sequence[str],
    owned: Sequence[str],
    owns_test: Sequence[str],
    runs: Sequence[str],
    cfg: Config,
) -> dict[str, list[str]]:
    """Each placeholder's files: changed plus owned files that exist in the worktree."""
    present = sorted(p for p in set(changed) | set(owned) if (repo / p).is_file())
    py = [p for p in present if p.endswith(".py")]
    ts = [p for p in present if p.startswith(WEB_PREFIX) and p.endswith(_TS_SUFFIXES)]
    return {
        "py_files": py,
        "py_src_files": [p for p in py if not is_test_path(p, cfg)],
        "py_test_files": [p for p in py if is_test_path(p, cfg)],
        "ts_files": ts,
        "ts_test_files": [p for p in ts if is_test_path(p, cfg)],
        "run_tests": _dedupe([*owns_test, *runs]),
    }


def expand(template: str, ph: Mapping[str, list[str]]) -> list[str] | None:
    """The command's argv, or None when a placeholder it uses expands to nothing.

    A token that is exactly `{name}` becomes that placeholder's list. A known placeholder
    inside other text, or an unknown `{name}` token, is a configuration error; other
    braces (a dict literal in `python -c` code) are left alone.
    """
    known = PLACEHOLDER_NAMES | set(ph)
    try:
        tokens = shlex.split(template)
    except ValueError as exc:
        raise ConfigError(f"[gates.task] {template!r}: {exc}") from exc
    argv: list[str] = []
    for token in tokens:
        whole = _PLACEHOLDER_RE.fullmatch(token)
        if whole is not None:
            name = whole.group(1)
            if name not in known:
                raise ConfigError(f"[gates.task] {template!r}: unknown placeholder {token}")
            values = ph.get(name, [])
            if not values:
                return None
            argv.extend(values)
            continue
        embedded = [m.group(1) for m in _PLACEHOLDER_RE.finditer(token) if m.group(1) in known]
        if embedded:
            raise ConfigError(
                f"[gates.task] {template!r}: placeholder {{{embedded[0]}}} must be a whole "
                "argument, not part of one"
            )
        argv.append(token)
    return argv


def _is_web_command(template: str) -> bool:
    try:
        tokens = shlex.split(template)
    except ValueError:
        return False
    if "--prefix=web" in tokens:
        return True
    return any(pair in _WEB_FLAGS for pair in zip(tokens, tokens[1:], strict=False))


# --- failed tests and the wave gate record -----------------------------------------------


def _match_failure(line: str) -> re.Match[str] | None:
    return (
        _PYTEST_FAILED_RE.match(line) or _VITEST_FAIL_RE.match(line) or _VITEST_SUITE_RE.match(line)
    )


def parse_failed_tests(output: str) -> tuple[str, ...]:
    """Test ids from pytest `FAILED <nodeid>` and `ERROR <nodeid>` lines, Vitest `FAIL
    <file> > <name>` lines, and Vitest `FAIL <file>` suite failures."""
    found: list[str] = []
    for raw in output.splitlines():
        m = _match_failure(_ANSI_RE.sub("", raw).rstrip())
        if m is not None:
            found.append(m.group(1).strip())
    return tuple(_dedupe(found))


def has_unparsed_failure(output: str) -> bool:
    """A failure the test ids don't account for: a FAIL, FAILED, or ERROR line no pattern
    reads, or a pytest summary counting more failures and errors than the ids name."""
    ids = 0
    counted = 0
    for raw in output.splitlines():
        line = _ANSI_RE.sub("", raw).rstrip()
        if _match_failure(line) is not None:
            ids += 1
        elif _FAILURE_LINE_RE.match(line):
            return True
        if _PYTEST_SUMMARY_RE.match(line.strip()):
            counted = sum(int(n) for n, _kind in _PYTEST_COUNT_RE.findall(line))
    return counted > ids


def _wave_gate_path(state: Path, sha: str) -> Path:
    if not _SHA_RE.match(sha):
        raise ValueError(f"{sha!r} isn't a commit SHA")
    return state / "wave-gates" / f"{sha}.json"


def record_wave_gate(state: Path, head: str, failed_tests: Iterable[str]) -> Path:
    """Record which tests failed at a wave gate on `head`, for the next wave's tasks."""
    path = _wave_gate_path(state, head)
    atomic_write_text(path, canonical({"failed_tests": sorted(set(failed_tests)), "head": head}))
    return path


def wave_gate_failures(state: Path, base: str) -> frozenset[str]:
    """Tests that failed at the wave gate on `base`; empty when there's no readable record."""
    try:
        data = json.loads(_wave_gate_path(state, base).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    tests = data.get("failed_tests") if isinstance(data, dict) else None
    if not isinstance(tests, list):
        return frozenset()
    return frozenset(t for t in tests if isinstance(t, str))


# --- the phase ---------------------------------------------------------------------------


def _tail(output: str) -> str:
    lines = output.splitlines()[-TAIL_LINES:]
    return "\n".join(lines)[-TAIL_CHARS:]


def _finding(
    template: str, argv: Sequence[str], code: int | None, new: Sequence[str], anchor: str
) -> Finding:
    # Evidence is capped (model.EVIDENCE_MAX), so the command, the longest part, goes last.
    tests = f"; failing tests: {', '.join(new)}" if new else ""
    return new_finding(
        RULE,
        file=anchor,
        line=1,
        message=f"gate failed: {template}",
        evidence=f"exit {code}{tests}; command: {shlex.join(argv)}",
        grade=Grade.E0_EXECUTED,
        source=Source.POLICY,
        fix="make the command pass, then rerun revgate task",
        evidence_key=template,
    )


def run_gate_phase(
    repo: Path,
    *,
    cfg: Config,
    state: Path,
    base: str,
    changed: Sequence[str],
    owned: Sequence[str],
    owns_test: Sequence[str],
    runs: Sequence[str],
) -> GatePhaseResult:
    """Run every `[gates.task]` command in order; a ConfigError in a template propagates."""
    ph = placeholders(repo, changed=changed, owned=owned, owns_test=owns_test, runs=runs, cfg=cfg)
    touches_web = any(p.startswith(WEB_PREFIX) for p in (*changed, *owned))
    anchor = next(iter((*owned, *changed)), ".review.toml")
    pre_existing = wave_gate_failures(state, base)
    results: list[GateCommandResult] = []
    findings: list[Finding] = []
    couldnt_run: list[str] = []
    for template in cfg.gates_task:
        argv = expand(template, ph)
        if argv is None or (not touches_web and _is_web_command(template)):
            results.append(
                GateCommandResult(
                    template, tuple(argv) if argv else None, "skipped", None, "", (), (), 0.0
                )
            )
            continue
        run = run_gate_capture(argv, cwd=repo, timeout_s=cfg.gate_timeout_s)
        failed_tests: tuple[str, ...] = ()
        new: tuple[str, ...] = ()
        if run.status == "failed":
            failed_tests = parse_failed_tests(run.output)
            new = tuple(t for t in failed_tests if t not in pre_existing)
            if new or not failed_tests or has_unparsed_failure(run.output):
                findings.append(_finding(template, argv, run.exit, new, anchor))
        elif run.status in ("timeout", "couldnt_run"):
            couldnt_run.append(template)
        results.append(
            GateCommandResult(
                template,
                tuple(argv),
                run.status,
                run.exit,
                _tail(run.output),
                failed_tests,
                new,
                run.duration_s,
            )
        )
    return GatePhaseResult(tuple(results), tuple(findings), tuple(couldnt_run))
