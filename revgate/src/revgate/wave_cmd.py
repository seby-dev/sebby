"""`revgate wave`: the W0 integrity checks (the whole of the MVP's wave review).

Every task of the wave needs a controller run file whose head the merged head contains;
every such run used one `revgate` source hash; no task changed a file it didn't declare
(lockfiles and ledger assignments aside); and `revgate`'s own source checkout is clean.
Each violation is one blocking `wave.integrity` finding. W0 checks the pipeline itself,
not the code, so it blocks even in a project without `.review.toml`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TextIO

from revgate import __version__, gitio
from revgate.config import Config, ConfigError, glob_match, load_config
from revgate.gates import parse_failed_tests, record_wave_gate
from revgate.gitio import GitError, SafetyError
from revgate.labels import log_findings
from revgate.model import Finding, Grade, RunFile, Source, TaskScope, Tier, new_finding
from revgate.plan import BlockTask, PlanWave, parse_plan_block, plan_slug
from revgate.plan_yaml import PlanYamlError
from revgate.rules.registry import load_tier_table
from revgate.store import (
    ledger_assigned,
    read_run_file,
    state_dir,
    write_run_file,
)
from revgate.task_cmd import source_hash
from revgate.verdict.route import route, sort_key

RULE = "wave.integrity"
LOCKFILE_NAMES = frozenset({"uv.lock", "package-lock.json"})


class WaveAbort(Exception):
    """Exit 2 with one `revgate: <reason>` line."""


@dataclass(frozen=True)
class _TaskRun:
    task: str
    run: RunFile | None


def _finding(file: str, message: str, evidence: str, key: str) -> Finding:
    return new_finding(
        RULE,
        file=file,
        line=1,
        message=message,
        evidence=evidence,
        grade=Grade.E1_EXACT,
        source=Source.POLICY,
        kind="integration",
        evidence_key=key,
    )


def _controller_run(top: Path, state: Path, task: str, head: str) -> RunFile | None:
    """The task's latest controller run file whose head the merged head contains."""
    candidates: list[RunFile] = []
    for path in sorted((state / "runs" / task).glob("*.controller.json")):
        try:
            rf = read_run_file(path)
        except (OSError, ValueError):
            continue
        try:
            if gitio.is_ancestor(top, rf.head, head):
                candidates.append(rf)
        except GitError:
            continue
    latest = [
        rf
        for rf in candidates
        if not any(
            other.head != rf.head and gitio.is_ancestor(top, rf.head, other.head)
            for other in candidates
        )
    ]
    return sorted(latest, key=lambda rf: rf.head)[0] if latest else None


def _allowed(path: str, task: BlockTask, assigned: frozenset[str]) -> bool:
    if path in task.owns.all() or path in task.runs or path in assigned:
        return True
    return PurePosixPath(path).name in LOCKFILE_NAMES


def _report_path(path: str, cfg: Config, slug: str, task: str) -> bool:
    if cfg.report_glob is None:
        return False
    filled = cfg.report_glob.replace("{plan}", slug).replace("{id}", task)
    return glob_match(path, (filled,))


def _task_findings(
    top: Path, state: Path, cfg: Config, plan_path: str, wave: PlanWave, head: str
) -> tuple[list[Finding], list[_TaskRun]]:
    slug = plan_slug(plan_path)
    findings: list[Finding] = []
    runs: list[_TaskRun] = []
    for sub in wave.subwaves:
        for task in sub.tasks:
            rf = _controller_run(top, state, task.id, head)
            runs.append(_TaskRun(task.id, rf))
            if rf is None:
                findings.append(
                    _finding(
                        plan_path,
                        f"task {task.id} has no controller run file merged into this head",
                        f"runs/{task.id}/*.controller.json: none whose head is in {head[:7]}",
                        f"missing-run:{task.id}",
                    )
                )
                continue
            assigned = ledger_assigned(state, slug, task.id)
            for _letter, path, old in gitio.diff_name_status(top, rf.base, rf.head):
                for p in (path, old):
                    if p is None or _allowed(p, task, assigned):
                        continue
                    if _report_path(p, cfg, slug, task.id):
                        continue
                    findings.append(
                        _finding(
                            p,
                            f"task {task.id} changed {p}, which it doesn't own or run",
                            f"{rf.base[:7]}..{rf.head[:7]}: {p}; plan owns and runs omit it",
                            f"unowned:{task.id}:{p}",
                        )
                    )
    return findings, runs


def _hash_findings(plan_path: str, runs: Sequence[_TaskRun]) -> list[Finding]:
    hashes = sorted({tr.run.source_hash for tr in runs if tr.run is not None})
    if len(hashes) <= 1:
        return []
    per = ", ".join(f"{tr.task}={tr.run.source_hash[:8]}" for tr in runs if tr.run is not None)
    return [
        _finding(
            plan_path,
            f"the revgate source hash changed during the wave ({len(hashes)} hashes)",
            f"controller runs: {per}",
            "source-hash",
        )
    ]


def _tamper_findings(cfg: Config) -> tuple[list[Finding], list[str]]:
    """`git status --porcelain revgate/` in revgate's own source checkout must be empty."""
    source = Path(cfg.revgate_source_repo).expanduser()
    if not source.is_dir():
        return [], []
    try:
        top = gitio.safe_toplevel(source)
    except SafetyError:
        return [], [f"{RULE}: revgate source {source} isn't a safe repository"]
    try:
        out = gitio.run_git(top, "status", "--porcelain", "--", "revgate/")
    except GitError as exc:
        return [], [f"{RULE}: git status in {top} failed: {exc}"]
    dirty = out.decode(errors="replace").strip()
    if not dirty:
        return [], []
    first = dirty.splitlines()[0]
    return [
        _finding(
            "revgate/",
            f"revgate's source checkout {top} has uncommitted changes under revgate/",
            f"git status --porcelain revgate/: {first}",
            "tamper",
        )
    ], []


def _load_wave(top: Path, base: str, plan_path: str, wave_id: str) -> PlanWave:
    try:
        data = gitio.show(top, base, plan_path)
    except GitError as exc:
        raise WaveAbort(f"can't read {plan_path} at {base[:7]}: {exc}") from exc
    if data is None:
        raise WaveAbort(f"{plan_path} doesn't exist at {base[:7]}")
    try:
        block = parse_plan_block(data.decode("utf-8", errors="replace"))
    except PlanYamlError as exc:
        raise WaveAbort(f"{plan_path}: {exc}") from exc
    if block is None:
        raise WaveAbort("revgate wave needs a plan-waves block")
    for wave in block.waves:
        if wave.id == wave_id:
            return wave
    raise WaveAbort(f"{plan_path} has no wave {wave_id}")


def run_wave(
    repo: Path,
    base: str,
    head: str,
    plan_path: str,
    wave_id: str,
    *,
    gate_log: Path | None,
    out: TextIO,
) -> int:
    try:
        return _run_wave(repo, base, head, plan_path, wave_id, gate_log, out)
    except WaveAbort as exc:
        print(f"revgate: {exc}", file=out)
        return 2


def _run_wave(
    repo: Path,
    base_rev: str,
    head_rev: str,
    plan_path: str,
    wave_id: str,
    gate_log: Path | None,
    out: TextIO,
) -> int:
    try:
        top = gitio.safe_toplevel(repo, expected=repo)
    except SafetyError as exc:
        raise WaveAbort(str(exc)) from exc
    try:
        base = gitio.rev_parse(top, base_rev)
        head = gitio.rev_parse(top, head_rev)
    except GitError as exc:
        raise WaveAbort(f"--base or --head doesn't name a commit: {exc}") from exc
    try:
        cfg = load_config(top, rev=base)
    except ConfigError as exc:
        raise WaveAbort(str(exc)) from exc
    wave = _load_wave(top, base, plan_path, wave_id)
    state = state_dir(top)
    if gate_log is not None:
        try:
            text = gate_log.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise WaveAbort(f"can't read the gate log {gate_log}: {exc}") from exc
        record_wave_gate(state, head, parse_failed_tests(text))

    findings, runs = _task_findings(top, state, cfg, plan_path, wave, head)
    findings += _hash_findings(plan_path, runs)
    tamper, unverified = _tamper_findings(cfg)
    findings += tamper

    # W0 is the pipeline's own check, so calibration mode doesn't demote it.
    policy_cfg = dataclasses.replace(cfg, is_default=False)
    tiers = load_tier_table()
    scope = TaskScope(None, frozenset(), frozenset())
    routed = sorted((route(f, tiers, policy_cfg, scope) for f in findings), key=sort_key)
    blocking = [f for f in routed if f.tier is Tier.BLOCKING]
    code = 1 if blocking else 0
    rf = RunFile(
        task=f"wave-{wave_id}",
        role="wave",
        base=base,
        head=head,
        plan=plan_path,
        plan_task_hash=None,
        revgate_version=__version__,
        source_hash=source_hash(),
        config_hash=cfg.config_hash,
        focus=False,
        focus_reasons=(),
        findings=tuple(routed),
        obligations=(),
        unverified=tuple(sorted(set(unverified))),
        incomplete=(),
        exit_code=code,
    )
    path = write_run_file(state, rf)
    log_findings(state, rf)
    tasks = ", ".join(tr.task for tr in runs)
    print(
        f"revgate wave {wave_id} ({tasks}) merged={head[:7]}: {len(blocking)} blocking",
        file=out,
    )
    for f in routed:
        if f.tier is not Tier.SHADOW:
            print(f"{f.file}:{f.line} {f.rule} {f.message}", file=out)
    print(f"details: {path}", file=out)
    return code
