"""`revgate task`: preflight, inputs, the run cache, gates, the static phase, and the verdict.

Follows the algorithm spec's top-level pseudocode without the behavioral phase. Exit `2`
is kept for tree safety, a gate that couldn't run, an ambiguous report path, a malformed
plan or configuration, and a crash of the index or the change model (amendment A6); an
exception inside one rule only marks the run `incomplete` and focuses it for the
controller.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal, TextIO, cast

import revgate
from revgate import __version__, gitio
from revgate.config import Config, ConfigError, glob_match, is_test_path, load_config
from revgate.gates import GateCommandResult, GatePhaseResult, run_gate_phase
from revgate.gitio import GitError, SafetyError
from revgate.labels import log_findings
from revgate.model import (
    Finding,
    Obligation,
    RuleOutput,
    RunFile,
    TaskScope,
    Unverified,
    Verdict,
    canonical,
    finding_from_dict,
    finding_to_dict,
    obligation_from_dict,
    obligation_to_dict,
)
from revgate.plan import PlanTask, load_plan_task, plan_slug, plan_task_hash
from revgate.plan_yaml import PlanYamlError
from revgate.report import AmbiguousReport, ReportModel, load_report, parse_report
from revgate.rules.registry import load_tier_table
from revgate.spi.cache import FACTS_VERSION, BlobCache
from revgate.static import all_rules
from revgate.static.ctx import StaticCtx, build_static_ctx
from revgate.static.ratchets import RatchetResult
from revgate.store import (
    ledger_append,
    ledger_assigned,
    ledger_rulings,
    run_cache_get,
    run_cache_put,
    run_file_path,
    state_dir,
    write_meta,
    write_run_file,
)
from revgate.verdict.render import exit_code_for, render_summary
from revgate.verdict.route import FocusInputs
from revgate.verdict.triage import decide

Role = Literal["implementer", "controller", "bench"]
ADHOC_SLUG = "adhoc"
_DIRTY_SHOWN = 5


@dataclass(frozen=True)
class TaskArgs:
    repo: Path
    base: str
    head: str
    role: Role
    plan: str | None = None
    task: str | None = None
    report: Path | None = None
    plan_rev: str | None = None
    round_: int = 1


class TaskAbort(Exception):
    """A condition that ends the run with exit 2 and one `revgate: <reason>` line."""


@cache
def source_hash() -> str:
    """SHA-256 over the installed package's files, sorted by relative path."""
    root = Path(revgate.__file__).resolve().parent
    files = [
        p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.relative_to(root).parts
    ]
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def blob_cache(state: Path) -> BlobCache:
    return BlobCache(state / "spi", f"{__version__}:{FACTS_VERSION}")


# --- the static phase ----------------------------------------------------------------------


@dataclass(frozen=True)
class StaticPhase:
    outputs: tuple[RuleOutput, ...]
    ctx: StaticCtx
    ratchet: RatchetResult
    incomplete: tuple[str, ...]  # internal:<rule>
    budget_notes: tuple[str, ...]  # budget:<rule> and skipped: budget:<rule>
    rule_seconds: Mapping[str, float]
    rule_errors: Mapping[str, str]
    checks: int


_NO_RATCHET = RatchetResult(
    findings=(), suppression_added=False, review_toml_edited=False, raw_counts={}
)


def run_static_phase(
    repo: Path,
    base: str,
    head: str,
    *,
    cfg: Config,
    plan: PlanTask | None,
    report: ReportModel | None,
    scope: TaskScope,
    cache: BlobCache,
    overrides: Mapping[str, bytes | None] | None = None,
) -> StaticPhase:
    """Build the static context, then run every rule in `ALL_RULES` under the budget.

    An exception from `build_static_ctx` propagates (the caller exits 2); an exception
    inside a rule adds `internal:<rule>` to `incomplete` and the other rules still run.
    """
    ctx = build_static_ctx(
        repo,
        base,
        head,
        cfg=cfg,
        plan=plan,
        report=report,
        scope=scope,
        cache=cache,
        overrides=overrides,
    )
    rules = all_rules.ALL_RULES
    share = cfg.rules_seconds / len(rules) if rules else 0.0
    outputs: list[RuleOutput] = []
    ratchet = _NO_RATCHET
    incomplete: list[str] = []
    notes: list[str] = []
    seconds: dict[str, float] = {}
    errors: dict[str, str] = {}
    total = 0.0
    checks = 0
    for rule in rules:
        name = all_rules.rule_name(rule)
        if total > cfg.rules_seconds:
            notes.append(f"skipped: budget:{name}")
            continue
        start = time.monotonic()
        produced: list[RuleOutput] = []
        try:
            if isinstance(rule, all_rules.RatchetRule):
                ratchet = rule.ratchet(ctx)
                produced = list(ratchet.findings)
            else:
                produced = list(rule.check(ctx))
        except Exception as exc:  # one rule's crash never ends the run (A6)
            incomplete.append(f"internal:{name}")
            errors[name] = f"{type(exc).__name__}: {exc}"
            produced = []
        elapsed = time.monotonic() - start
        total += elapsed
        seconds[name] = round(elapsed, 4)
        checks += len(rule.ids)
        if elapsed > share:
            notes.append(f"budget:{name}")
        outputs.extend(produced)
    return StaticPhase(
        outputs=tuple(outputs),
        ctx=ctx,
        ratchet=ratchet,
        incomplete=tuple(incomplete),
        budget_notes=tuple(notes),
        rule_seconds=seconds,
        rule_errors=errors,
        checks=checks,
    )


def review_static(
    repo: Path,
    base: str,
    head: str,
    *,
    cfg: Config,
    plan: PlanTask | None,
    report: ReportModel | None,
    scope: TaskScope,
    cache: BlobCache,
    overrides: Mapping[str, bytes | None] | None = None,
) -> tuple[list[RuleOutput], StaticCtx, RatchetResult]:
    """The static phase alone: no preflight, no gates, no run cache (for bench and recheck)."""
    phase = run_static_phase(
        repo,
        base,
        head,
        cfg=cfg,
        plan=plan,
        report=report,
        scope=scope,
        cache=cache,
        overrides=overrides,
    )
    return list(phase.outputs), phase.ctx, phase.ratchet


# --- serialization of the cached part of a run ------------------------------------------------


def output_to_dict(o: RuleOutput) -> dict[str, object]:
    if isinstance(o, Finding):
        return {"type": "finding", **finding_to_dict(o)}
    if isinstance(o, Obligation):
        return {"type": "obligation", **obligation_to_dict(o)}
    return {"type": "unverified", "rule": o.rule, "note": o.note}


def output_from_dict(d: Mapping[str, object]) -> RuleOutput:
    kind = d.get("type")
    if kind == "finding":
        return finding_from_dict(d)
    if kind == "obligation":
        return obligation_from_dict(d)
    rule, note = d.get("rule"), d.get("note")
    if kind == "unverified" and isinstance(rule, str) and isinstance(note, str):
        return Unverified(rule, note)
    raise ValueError(f"unknown output {d!r}")


def unverified_notes(outputs: Sequence[RuleOutput]) -> list[str]:
    return [f"{o.rule}: {o.note}" for o in outputs if isinstance(o, Unverified)]


@dataclass(frozen=True)
class _Cached:
    outputs: tuple[RuleOutput, ...]
    ctx_unverified: tuple[str, ...]
    suppression_added: bool
    review_toml_edited: bool
    checks: int


def _cache_payload(phase: StaticPhase) -> str:
    return canonical(
        {
            "outputs": [output_to_dict(o) for o in phase.outputs],
            "ctx_unverified": list(phase.ctx.unverified),
            "suppression_added": phase.ratchet.suppression_added,
            "review_toml_edited": phase.ratchet.review_toml_edited,
            "checks": phase.checks,
        }
    )


def _cache_load(text: str) -> _Cached | None:
    """The cached outputs, or None for an entry this version can't read (a miss)."""
    try:
        data = json.loads(text)
        outputs = tuple(output_from_dict(o) for o in data["outputs"])
        notes = tuple(str(n) for n in data["ctx_unverified"])
        return _Cached(
            outputs,
            notes,
            bool(data["suppression_added"]),
            bool(data["review_toml_edited"]),
            int(data["checks"]),
        )
    except (ValueError, KeyError, TypeError):
        return None


# --- preflight and inputs -------------------------------------------------------------------


def _resolve(top: Path, rev: str, what: str) -> str:
    try:
        return gitio.rev_parse(top, rev)
    except GitError as exc:
        raise TaskAbort(f"{what} {rev!r} doesn't name a commit") from exc


def _preflight(args: TaskArgs) -> Path:
    """The repository's toplevel, after the $HOME and --repo checks."""
    if args.role == "bench":
        return args.repo.resolve()
    try:
        return gitio.safe_toplevel(args.repo, expected=args.repo)
    except SafetyError as exc:
        raise TaskAbort(str(exc)) from exc


def _worktree_state(top: Path, head: str, cfg: Config) -> list[str]:
    """Untracked files that don't make the tree dirty; raises TaskAbort when it's dirty."""
    actual = _resolve(top, "HEAD", "HEAD")
    if actual != head:
        raise TaskAbort(f"HEAD is {actual[:7]}, not --head {head[:7]}")
    dirty: list[str] = []
    untracked: list[str] = []
    for xy, path in gitio.status_porcelain(top):
        if xy != "??":
            dirty.append(path)
        elif glob_match(path, cfg.prod_globs) or is_test_path(path, cfg):
            dirty.append(path)
        else:
            untracked.append(path)
    if dirty:
        shown = ", ".join(sorted(dirty)[:_DIRTY_SHOWN])
        more = f" and {len(dirty) - _DIRTY_SHOWN} more" if len(dirty) > _DIRTY_SHOWN else ""
        raise TaskAbort(f"the worktree is dirty: {shown}{more}")
    return sorted(untracked)


def _load_plan(top: Path, args: TaskArgs, base: str) -> PlanTask | None:
    if args.plan is None and args.task is None:
        return None
    if args.plan is None or args.task is None:
        raise TaskAbort("--plan and --task go together")
    rev = args.plan_rev or base
    try:
        plan = load_plan_task(top, rev, args.plan, args.task)
    except PlanYamlError as exc:
        raise TaskAbort(f"{args.plan}: {exc}") from exc
    except GitError as exc:
        raise TaskAbort(f"can't read {args.plan} at {rev}: {exc}") from exc
    if plan is None:
        raise TaskAbort(f"{args.plan} at {rev[:7]} has no task {args.task}")
    return plan


def _load_report(
    top: Path, args: TaskArgs, cfg: Config, plan: PlanTask | None
) -> ReportModel | None:
    if args.report is not None:
        path = args.report if args.report.is_absolute() else top / args.report
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise TaskAbort(f"can't read the report {path}: {exc}") from exc
        resolved = path.resolve()
        rel = resolved.relative_to(top).as_posix() if resolved.is_relative_to(top) else str(path)
        return parse_report(text, rel)
    if plan is None or cfg.report_glob is None or args.task is None:
        return None
    try:
        return load_report(top, cfg.report_glob, plan_slug(plan.plan_path), args.task)
    except AmbiguousReport as exc:
        raise TaskAbort(str(exc)) from exc


def _changed(top: Path, base: str, head: str, cfg: Config) -> tuple[tuple[str, ...], int]:
    paths: set[str] = set()
    for _letter, path, old in gitio.diff_name_status(top, base, head):
        paths.add(path)
        if old is not None:
            paths.add(old)
    numstat = gitio.diff_numstat(top, base, head)
    lines = sum((a or 0) + (d or 0) for a, d, p in numstat if not is_test_path(p, cfg))
    return tuple(sorted(paths)), lines


def _gate_dict(r: GateCommandResult) -> dict[str, object]:
    return {
        "template": r.template,
        "argv": list(r.argv) if r.argv is not None else None,
        "status": r.status,
        "exit": r.exit,
        "tail": r.tail,
        "failed_tests": list(r.failed_tests),
        "new_failures": list(r.new_failures),
        "seconds": round(r.duration_s, 3),
    }


# --- the run ---------------------------------------------------------------------------------


def run_task(args: TaskArgs, *, out: TextIO) -> int:
    try:
        return _run_task(args, out)
    except TaskAbort as exc:
        print(f"revgate: {exc}", file=out)
        return 2


def _run_task(args: TaskArgs, out: TextIO) -> int:
    started = time.monotonic()
    top = _preflight(args)
    base = _resolve(top, args.base, "--base")
    head = _resolve(top, args.head, "--head")
    try:
        cfg = load_config(top, rev=base)
    except ConfigError as exc:
        raise TaskAbort(str(exc)) from exc
    untracked = [] if args.role == "bench" else _worktree_state(top, head, cfg)
    plan = _load_plan(top, args, base)
    report = _load_report(top, args, cfg, plan)

    state = state_dir(top)
    slug = plan_slug(plan.plan_path) if plan is not None else ADHOC_SLUG
    if plan is not None:
        scope = TaskScope(
            args.task,
            plan.owns.all(),
            frozenset(plan.runs),
            ledger_assigned(state, slug, args.task),
        )
    else:
        scope = TaskScope(args.task, frozenset(), frozenset())
    pth = plan_task_hash(plan) if plan is not None else None
    src = source_hash()
    # The gates run on the working tree, so the key holds its tree (untracked, non-ignored
    # files included: an untracked root config can change what a gate checks), and the
    # report the static phase reads, by path and content.
    tree = None if args.role == "bench" else gitio.worktree_tree_hash(top, state / "tmp")
    key = canonical(
        {
            "base": base,
            "head": head,
            "plan_task_hash": pth,
            "config_hash": cfg.config_hash,
            "version": __version__,
            "source_hash": src,
            "tree": tree,
            "report": report.path if report is not None else None,
            "report_sha256": (
                hashlib.sha256(report.text.encode()).hexdigest() if report is not None else None
            ),
        }
    )
    meta: dict[str, object] = {
        "role": args.role,
        "untracked": untracked,
        "plan_rev": (args.plan_rev or base) if plan is not None else None,
        "report": report.path if report is not None else None,
        "rules": {},
        "rule_errors": {},
        "gates": [],
    }
    timings: dict[str, float] = {}
    changed, nontest_lines = _changed(top, base, head, cfg)

    outputs: tuple[RuleOutput, ...] = ()
    ctx_unverified: tuple[str, ...] = ()
    incomplete: tuple[str, ...] = ()
    budget_notes: tuple[str, ...] = ()
    suppression_added = review_toml_edited = False
    gate: GatePhaseResult | None = None
    checks = 0

    hit = run_cache_get(state, key)
    cached = _cache_load(hit) if hit is not None else None
    meta["run_cache"] = "hit" if cached is not None else "miss"
    if cached is not None:
        outputs, ctx_unverified = cached.outputs, cached.ctx_unverified
        suppression_added, review_toml_edited = cached.suppression_added, cached.review_toml_edited
        checks = cached.checks
    else:
        t0 = time.monotonic()
        try:
            gate = run_gate_phase(
                top,
                cfg=cfg,
                state=state,
                base=base,
                changed=changed,
                owned=sorted(scope.owns),
                owns_test=plan.owns.test if plan is not None else (),
                runs=plan.runs if plan is not None else (),
            )
        except ConfigError as exc:
            raise TaskAbort(str(exc)) from exc
        timings["gates"] = round(time.monotonic() - t0, 3)
        meta["gates"] = [_gate_dict(r) for r in gate.results]
        checks = sum(1 for r in gate.results if r.status != "skipped")
        if gate.couldnt_run:
            incomplete = tuple(f"gate:{t}" for t in gate.couldnt_run)
            outputs = tuple(gate.findings)
        elif gate.failed():
            outputs = tuple(gate.findings)
        else:
            t1 = time.monotonic()
            try:
                phase = run_static_phase(
                    top,
                    base,
                    head,
                    cfg=cfg,
                    plan=plan,
                    report=report,
                    scope=scope,
                    cache=blob_cache(state),
                )
            except Exception as exc:  # the index or the change model crashed: exit 2 (A6)
                raise TaskAbort(f"the index or change model failed: {exc!r}") from exc
            timings["static"] = round(time.monotonic() - t1, 3)
            outputs, ctx_unverified = phase.outputs, phase.ctx.unverified
            incomplete, budget_notes = phase.incomplete, phase.budget_notes
            suppression_added = phase.ratchet.suppression_added
            review_toml_edited = phase.ratchet.review_toml_edited
            checks += phase.checks
            meta["rules"] = dict(phase.rule_seconds)
            meta["rule_errors"] = dict(phase.rule_errors)
            if not incomplete and not budget_notes:
                run_cache_put(state, key, _cache_payload(phase))

    findings = [o for o in outputs if isinstance(o, Finding)]
    if args.round_ != 1:
        findings = [dataclasses.replace(f, round=args.round_) for f in findings]
    obligations = [o for o in outputs if isinstance(o, Obligation)]
    unverified = [*ctx_unverified, *unverified_notes(outputs), *budget_notes]
    verdict = decide(
        findings,
        scope=scope,
        tiers=load_tier_table(),
        cfg=cfg,
        disclosed=report.disclosed if report is not None else frozenset(),
        responses=report.responses if report is not None else {},
        rulings=ledger_rulings(state, slug, args.task),
        focus_inputs=FocusInputs(
            risk=plan.risk if plan is not None else None,
            changed_paths=changed,
            nontest_changed_lines=nontest_lines,
            suppression_added=suppression_added,
            review_toml_edited=review_toml_edited,
        ),
        obligations=obligations,
        unverified=unverified,
    )
    if incomplete:
        reasons = tuple(f"incomplete:{i.removeprefix('internal:')}" for i in incomplete)
        verdict = dataclasses.replace(
            verdict, focus=True, focus_reasons=(*verdict.focus_reasons, *reasons)
        )
    gate_failed = gate is not None and gate.failed()
    couldnt_run = gate is not None and bool(gate.couldnt_run)
    code = 2 if couldnt_run else (1 if gate_failed else exit_code_for(verdict))
    provisional = bool(incomplete or budget_notes)
    rf = RunFile(
        task=args.task,
        role=args.role,
        base=base,
        head=head,
        plan=args.plan,
        plan_task_hash=pth,
        revgate_version=__version__,
        source_hash=src,
        config_hash=cfg.config_hash,
        focus=verdict.focus,
        focus_reasons=verdict.focus_reasons,
        findings=verdict.findings,
        obligations=verdict.obligations,
        unverified=verdict.unverified,
        incomplete=incomplete,
        exit_code=code,
        provisional=provisional,
    )
    path = write_run_file(state, rf)
    timings["total"] = round(time.monotonic() - started, 3)
    meta["timings"] = timings
    meta["checks"] = checks
    write_meta(state, args.task, head, args.role, meta)
    log_findings(state, rf)
    if args.role == "controller":
        _record_controller_run(state, slug, rf, verdict, path)
    if couldnt_run and gate is not None:
        names = ", ".join(gate.couldnt_run)
        print(f"revgate: a gate couldn't run ({names})  details: {path}", file=out)
        return code
    print(
        render_summary(
            verdict,
            task=args.task,
            head=head,
            round_=args.round_,
            run_file_path=str(path),
            checks=checks,
            provisional=provisional,
        ),
        file=out,
    )
    return code


def _record_controller_run(
    state: Path, slug: str, rf: RunFile, verdict: Verdict, path: Path
) -> None:
    ledger_append(
        state,
        slug,
        {
            "kind": "run",
            "task": rf.task,
            "role": rf.role,
            "base": rf.base,
            "head": rf.head,
            "source_hash": rf.source_hash,
            "exit_code": rf.exit_code,
            "blocking": [f.id for f in verdict.blocking()],
            "run_file": str(path),
        },
    )
    for o in verdict.obligations:
        if o.status != "deferred":
            continue
        ledger_append(
            state,
            slug,
            {
                "kind": "obligation",
                "task": rf.task,
                "oid": o.oid,
                "rule": o.rule,
                "owner": o.owner,
                "anchor": o.anchor.key(),
                "status": o.status,
            },
        )


def read_meta(state: Path, rf: RunFile) -> dict[str, object]:
    """A run's meta sidecar, or an empty dict when it's missing or unreadable."""
    path = run_file_path(state, rf.task, rf.head, rf.role).with_suffix(".meta.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cast(dict[str, object], data) if isinstance(data, dict) else {}
