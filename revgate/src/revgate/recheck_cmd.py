"""`revgate recheck`: re-run the static review for the task that produced each finding.

For each id, the newest run file that holds it gives the base, plan, task, and plan
revision; the static phase re-runs at `--head` with those inputs, and the finding is
`fixed` when no output shares its `(rule, cluster_key)`, else `still present`. It's
`unknown` when its rule crashed or the budget skipped it, and for a gate or wave finding,
which only `revgate task` or `revgate wave` can re-run. The exit code is `2` when any
finding is unknown or not found, else `1` when a still-present finding routes as blocking
for the implementer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from revgate import gitio
from revgate.config import ConfigError, load_config
from revgate.gitio import GitError, SafetyError
from revgate.model import Finding, RunFile, TaskScope, Tier
from revgate.plan import load_plan_task, plan_slug
from revgate.plan_yaml import PlanYamlError
from revgate.rules.registry import load_tier_table
from revgate.store import ledger_assigned, read_run_file, state_dir
from revgate.task_cmd import blob_cache, read_meta, review_static
from revgate.verdict.route import route


class RecheckAbort(Exception):
    """Exit 2 with one `revgate: <reason>` line."""


@dataclass(frozen=True)
class _Origin:
    run: RunFile
    finding: Finding


def _find(state: Path, finding_id: str) -> _Origin | None:
    """The newest task run file (by modification time) that holds the finding."""
    best: tuple[float, str, _Origin] | None = None
    for path in (state / "runs").glob("*/*.json"):
        if path.name.endswith(".meta.json") or path.name.endswith(".wave.json"):
            continue
        try:
            rf = read_run_file(path)
            stamp = path.stat().st_mtime
        except (OSError, ValueError):
            continue
        for f in rf.findings:
            if f.id == finding_id:
                key = (stamp, str(path))
                if best is None or key > best[:2]:
                    best = (stamp, str(path), _Origin(rf, f))
    return best[2] if best is not None else None


_NOT_STATIC = ("gate.", "wave.")


def _recheck_one(top: Path, state: Path, head: str, origin: _Origin) -> tuple[str, bool]:
    """`(status, blocking)` for one finding at `head`; status starts with `unknown` when the
    static phase can't tell."""
    rf = origin.run
    if origin.finding.rule.startswith(_NOT_STATIC):
        return f"unknown ({origin.finding.rule} isn't a static finding; rerun revgate task)", False
    try:
        cfg = load_config(top, rev=rf.base)
    except ConfigError as exc:
        raise RecheckAbort(str(exc)) from exc
    plan = None
    scope = TaskScope(rf.task, frozenset(), frozenset())
    if rf.plan is not None and rf.task is not None:
        meta_rev = read_meta(state, rf).get("plan_rev")
        plan_rev = meta_rev if isinstance(meta_rev, str) else rf.base
        try:
            plan = load_plan_task(top, plan_rev, rf.plan, rf.task)
        except (PlanYamlError, GitError) as exc:
            raise RecheckAbort(f"can't load {rf.plan} task {rf.task}: {exc}") from exc
        if plan is not None:
            scope = TaskScope(
                rf.task,
                plan.owns.all(),
                frozenset(plan.runs),
                ledger_assigned(state, plan_slug(rf.plan), rf.task),
            )
    try:
        phase = review_static(
            top,
            rf.base,
            head,
            cfg=cfg,
            plan=plan,
            report=None,
            scope=scope,
            cache=blob_cache(state),
        )
    except Exception as exc:  # the index or the change model crashed: exit 2 (A6)
        raise RecheckAbort(f"the index or change model failed: {exc!r}") from exc
    target = origin.finding
    if target.rule in phase.unrun:
        errors = "; ".join(f"{k}: {v}" for k, v in sorted(phase.rule_errors.items()))
        why = f"its rule crashed: {errors}" if errors else "the budget skipped its rule"
        return f"unknown ({why})", False
    matches = [
        o
        for o in phase.outputs
        if isinstance(o, Finding) and (o.rule, o.cluster_key) == (target.rule, target.cluster_key)
    ]
    if not matches:
        return "fixed", False
    tiers = load_tier_table()
    routed = [route(m, tiers, cfg, scope) for m in matches]
    blocking = any(r.tier is Tier.BLOCKING and "implementer" in r.audience for r in routed)
    return "still present", blocking


def run_recheck(repo: Path, head: str, finding_ids: Sequence[str], *, out: TextIO) -> int:
    try:
        return _run_recheck(repo, head, finding_ids, out)
    except RecheckAbort as exc:
        print(f"revgate: {exc}", file=out)
        return 2


def _run_recheck(repo: Path, head_rev: str, finding_ids: Sequence[str], out: TextIO) -> int:
    try:
        top = gitio.safe_toplevel(repo, expected=repo)
    except SafetyError as exc:
        raise RecheckAbort(str(exc)) from exc
    try:
        head = gitio.rev_parse(top, head_rev)
    except GitError as exc:
        raise RecheckAbort(f"--head {head_rev!r} doesn't name a commit") from exc
    state = state_dir(top)
    code = 0
    for fid in finding_ids:
        origin = _find(state, fid)
        if origin is None:
            print(f"{fid}: not found in any run file", file=out)
            code = max(code, 2)
            continue
        status, blocking = _recheck_one(top, state, head, origin)
        print(f"{fid}: {status}", file=out)
        if status.startswith("unknown"):
            code = max(code, 2)
        elif blocking:
            code = max(code, 1)
    return code
