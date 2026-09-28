"""revgate command line.

Exit codes: `0` clean or advisory only, `1` a blocking finding or a failed gate, `2` tree
safety, a gate or rule input that couldn't be used, or a crash. A crash anywhere prints
its traceback to stderr and exits `2`, so it's never read as a pass or as a finding.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path
from typing import TextIO

from revgate import __version__
from revgate.config import ConfigError, load_config, parse_age
from revgate.gate_cache import gate_stats, run_gate_if_changed
from revgate.gitio import GitError, SafetyError, safe_toplevel, toplevel
from revgate.labels import mark, rule_stats, unlabeled, wilson_lower_bound
from revgate.plan import plan_slug
from revgate.plan_lint import lint_plan, plan_brief, render_lint
from revgate.plan_yaml import PlanYamlError
from revgate.store import state_dir


def _add_repo(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", type=Path, default=None, help="default: this directory's toplevel")


def _add_range(p: argparse.ArgumentParser) -> None:
    _add_repo(p)
    p.add_argument("--base", required=True, help="the task's fork or the wave's base")
    p.add_argument("--head", required=True, help="the commit under review")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="revgate")
    parser.add_argument("--version", action="version", version=f"revgate {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    task = sub.add_parser("task", help="review one task: gates, static rules, verdict")
    _add_range(task)
    task.add_argument("--role", required=True, choices=("implementer", "controller"))
    task.add_argument("--plan", default=None, help="the plan's path in the repository")
    task.add_argument("--task", default=None, help="the task's id in the plan")
    task.add_argument("--report", type=Path, default=None, help="the task's report file")
    task.add_argument("--plan-rev", default=None, help="read the plan here (default: --base)")
    task.add_argument("--round", type=int, default=1, dest="round_", help="the review round")

    wave = sub.add_parser("wave", help="check a merged wave's integrity (W0)")
    _add_range(wave)
    wave.add_argument("--plan", required=True, help="the plan's path in the repository")
    wave.add_argument("--wave", required=True, help="the wave's id in the plan block")
    wave.add_argument("--gate-log", type=Path, default=None, help="the wave gate's output")

    mp = sub.add_parser("map", help="mark each changed area deep or cleared")
    _add_range(mp)
    mp.add_argument("--plan", default=None, help="the plan's path in the repository")
    mp.add_argument("--json", action="store_true", help="print the JSON, not Markdown")

    recheck = sub.add_parser("recheck", help="re-run the check behind each finding")
    _add_repo(recheck)
    recheck.add_argument("--head", required=True, help="the commit to check")
    recheck.add_argument("finding_ids", nargs="+", metavar="FINDING_ID")

    lint = sub.add_parser("plan-lint", help="check a plan's plan-waves block")
    lint.add_argument("plan", type=Path, metavar="PLAN")
    _add_repo(lint)
    lint.add_argument("--base", default=None, help="check paths and anchors at this commit")
    lint.add_argument("--cap", type=int, default=5, help="concurrent implementers")

    plan = sub.add_parser("plan", help="plan helpers")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True)
    brief = plan_sub.add_parser("brief", help="print one task's block entry and section")
    brief.add_argument("plan", type=Path, metavar="PLAN")
    brief.add_argument("task", metavar="TASK")
    _add_repo(brief)
    brief.add_argument("--head", default=None, help="re-resolve context anchors here")

    gic = sub.add_parser("gate-if-changed", help="skip a command that passed on this exact tree")
    gic.add_argument("--max-age", default=None, help="for example 24h (default: [gates.cache])")
    gic.add_argument("--no-cache", action="store_true", help="run even on a cache hit")
    gic.add_argument("cmd", nargs=argparse.REMAINDER, help="-- COMMAND [ARGS...]")

    stats = sub.add_parser("gate-stats", help="print the gate cache's hit rate")
    stats.add_argument("--since", default=None, help="for example 7d")

    mk = sub.add_parser("mark", help="label a finding a true or false positive")
    mk.add_argument("finding_id", metavar="FINDING_ID")
    mk.add_argument("label", choices=("tp", "fp"))
    mk.add_argument("--note", default="", help="why")
    mk.add_argument("--ruling", action="store_true", help="also record it in the plan's ledger")
    mk.add_argument("--plan", default=None, help="the plan whose ledger gets the ruling")
    mk.add_argument("--labeler", default=None, help="default: $USER")
    _add_repo(mk)

    label = sub.add_parser("label", help="list unlabeled findings, or print rule precision")
    group = label.add_mutually_exclusive_group()
    group.add_argument("--list", type=int, default=None, metavar="N", help="default 20")
    group.add_argument("--stats", action="store_true", help="true and false positives per rule")
    _add_repo(label)
    return parser


def _repo(arg: Path | None) -> Path:
    """`--repo`, or the current directory's toplevel (the current directory outside one)."""
    if arg is not None:
        return arg
    cwd = Path.cwd()
    return toplevel(cwd) or cwd


def _read(path: Path, out: TextIO) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"revgate: can't read {path}: {exc}", file=out)
        return None


def _plan_lint(args: argparse.Namespace, out: TextIO) -> int:
    md = _read(args.plan, out)
    if md is None:
        return 2
    top: Path | None = None
    plan_path = args.plan.as_posix()
    if args.base is not None:
        repo = _repo(args.repo)
        top = safe_toplevel(repo, expected=repo)
        resolved = args.plan.resolve()
        if resolved.is_relative_to(top):
            plan_path = resolved.relative_to(top).as_posix()
    cfg = load_config(top, rev=args.base) if top is not None else None
    report = lint_plan(md, plan_path=plan_path, repo=top, base=args.base, cfg=cfg, cap=args.cap)
    print(render_lint(report), end="", file=out)
    return 1 if report.errors() else 0


def _plan_brief(args: argparse.Namespace, out: TextIO) -> int:
    md = _read(args.plan, out)
    if md is None:
        return 2
    top: Path | None = None
    if args.head is not None:
        repo = _repo(args.repo)
        top = safe_toplevel(repo, expected=repo)
    try:
        print(plan_brief(md, args.task, repo=top, head=args.head), end="", file=out)
    except KeyError:
        print(f"revgate: {args.plan} has no task {args.task}", file=out)
        return 2
    return 0


def _mark(args: argparse.Namespace, out: TextIO) -> int:
    repo = _repo(args.repo)
    state = state_dir(safe_toplevel(repo, expected=repo))
    slug = plan_slug(args.plan) if args.plan is not None else None
    labeler = args.labeler or os.environ.get("USER") or "controller"
    try:
        mark(
            state,
            args.finding_id,
            args.label,
            labeler=labeler,
            note=args.note,
            ruling=args.ruling,
            plan_slug=slug,
        )
    except KeyError:
        print(f"revgate: no finding {args.finding_id} in the findings log", file=out)
        return 2
    except ValueError as exc:
        print(f"revgate: {exc}", file=out)
        return 2
    where = f" (ruling in ledger {slug})" if args.ruling else ""
    print(f"marked {args.finding_id} {args.label}{where}", file=out)
    return 0


def _label(args: argparse.Namespace, out: TextIO) -> int:
    repo = _repo(args.repo)
    state = state_dir(safe_toplevel(repo, expected=repo))
    if args.stats:
        stats = rule_stats(state)
        if not stats:
            print("no explicit labels yet", file=out)
        for rule, (tp, fp) in stats.items():
            bound = wilson_lower_bound(tp, tp + fp)
            print(f"{rule}  tp {tp}  fp {fp}  precision lower bound {bound:.2f}", file=out)
        return 0
    rows = unlabeled(state, args.list if args.list is not None else 20)
    for row in rows:
        where = f"{row.get('file')}:{row.get('line')}"
        print(f"{row.get('id')}  {row.get('rule')}  {where}  {row.get('message')}", file=out)
    print(f"{len(rows)} unlabeled; label each with `revgate mark <id> tp|fp`", file=out)
    return 0


def _review(args: argparse.Namespace, out: TextIO) -> int:
    """The review commands, which import the whole static stack only when one runs."""
    if args.command == "task":
        from revgate.task_cmd import TaskArgs, run_task

        task_args = TaskArgs(
            repo=_repo(args.repo),
            base=args.base,
            head=args.head,
            role=args.role,
            plan=args.plan,
            task=args.task,
            report=args.report,
            plan_rev=args.plan_rev,
            round_=args.round_,
        )
        return run_task(task_args, out=out)
    if args.command == "wave":
        from revgate.wave_cmd import run_wave

        return run_wave(
            _repo(args.repo),
            args.base,
            args.head,
            args.plan,
            args.wave,
            gate_log=args.gate_log,
            out=out,
        )
    if args.command == "map":
        from revgate.map_cmd import run_map

        return run_map(
            _repo(args.repo), args.base, args.head, args.plan, as_json=args.json, out=out
        )
    from revgate.recheck_cmd import run_recheck

    return run_recheck(_repo(args.repo), args.head, args.finding_ids, out=out)


def _dispatch(args: argparse.Namespace, out: TextIO) -> int:
    command = args.command
    if command in ("task", "wave", "map", "recheck"):
        return _review(args, out)
    if command == "plan-lint":
        return _plan_lint(args, out)
    if command == "plan":
        return _plan_brief(args, out)
    if command == "gate-if-changed":
        cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
        max_age = parse_age(args.max_age) if args.max_age else None
        return run_gate_if_changed(cmd, cwd=Path.cwd(), max_age_s=max_age, no_cache=args.no_cache)
    if command == "gate-stats":
        since = parse_age(args.since) if args.since else None
        return gate_stats(Path.cwd(), since)
    if command == "mark":
        return _mark(args, out)
    if command == "label":
        return _label(args, out)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = sys.stdout
    try:
        return _dispatch(args, out)
    except (ConfigError, SafetyError, GitError, PlanYamlError) as exc:
        print(f"revgate: {exc}", file=out)
        return 2
    except Exception:  # a crash is exit 2, never a pass and never a finding
        traceback.print_exc(file=sys.stderr)
        print("revgate: internal error (traceback on stderr)", file=out)
        return 2
