"""G5 wiring: a new production symbol that nothing in production refers to.

Candidates are the functions and classes the task added in production paths (not nested,
not dunders, not `test_*`, not entry points by decorator, not overrides, not Protocol or
ABC members, not test hooks) and the TypeScript functions a production file newly
exports. A candidate with a production reference outside its own definition is wired.
An unwired one is classified against the plan, as the algorithm spec's G5 says:

- planned here: this task's code blocks call it, so it's `wiring.unwired_planned_here`,
  an E1 index fact paired with an E2 plan fact, starting advisory;
- deferred: a later task's code calls it, so it's a deferred obligation for that owner;
- unplanned: no task wires it, so it's `wiring.unwired_new_symbol` for the wave review.

With no plan the rule says nothing: the plan is what makes the check precise (42 of 43
unwired symbols were wired by a later task, r3 §5.1).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from revgate.config import is_prod_path, is_test_path
from revgate.model import (
    Anchor,
    Grade,
    Obligation,
    RuleOutput,
    Source,
    new_finding,
)
from revgate.plan import PlanEdge, PlanTask
from revgate.spi.facts import FuncFact, Ref
from revgate.static.ctx import StaticCtx

PLANNED_HERE = "wiring.unwired_planned_here"
NEW_SYMBOL = "wiring.unwired_new_symbol"

_ENTRY_MARKERS = ("route", ".get", ".post", ".put", ".delete", "fixture", "command", "validator")
_TEST_HOOK_RE = re.compile(r"^_?(reset|clear)_\w*(cache|state)\w*$|_for_tests?$")
_ABSTRACT_BASES = frozenset({"Protocol", "ABC", "ABCMeta"})


@dataclass(frozen=True)
class _Candidate:
    name: str
    path: str
    lineno: int
    end_lineno: int


def production_references(
    ctx: StaticCtx, name: str, def_path: str, span: tuple[int, int]
) -> list[Ref]:
    """Head production refs to `name` (identifier, attribute, import, or string literal, in
    either language) outside the symbol's own definition."""
    start, end = span
    return [
        r
        for r in ctx.head.references(name, prod_only=True)
        if not (r.path == def_path and start <= r.line <= end)
    ]


def _test_references(ctx: StaticCtx, name: str) -> list[Ref]:
    return [r for r in ctx.head.references(name, prod_only=False) if is_test_path(r.path, ctx.cfg)]


def _short(qualname: str) -> str:
    return qualname.rsplit(".", 1)[-1]


def _abstract(ctx: StaticCtx, class_qualname: str) -> bool:
    """A Protocol or ABC, directly or through a project base."""
    for cls_q in ctx.head.mro(class_qualname):
        cls = ctx.head.classes.get(cls_q)
        bases = cls.resolved_bases + cls.bases if cls is not None else (cls_q,)
        if any(_short(b) in _ABSTRACT_BASES for b in bases):
            return True
    return False


def _overrides(ctx: StaticCtx, fn: FuncFact) -> bool:
    if fn.class_qualname is None:
        return False
    name = _short(fn.qualname)
    return any(f"{c}.{name}" in ctx.head.functions for c in ctx.head.mro(fn.class_qualname)[1:])


def _excluded_function(ctx: StaticCtx, fn: FuncFact) -> bool:
    name = _short(fn.qualname)
    if fn.is_nested or name.startswith("test_") or _TEST_HOOK_RE.search(name):
        return True
    if name.startswith("__") and name.endswith("__"):
        return True
    if any(marker in deco for deco in fn.decorators for marker in _ENTRY_MARKERS):
        return True
    if fn.class_qualname is not None:
        return _overrides(ctx, fn) or _abstract(ctx, fn.class_qualname)
    return False


def _python_candidates(ctx: StaticCtx) -> Iterator[_Candidate]:
    for fc in ctx.change.functions.values():
        fn = fc.head
        if fc.status != "added" or fn is None or not is_prod_path(fn.path, ctx.cfg):
            continue
        if not _excluded_function(ctx, fn):
            yield _Candidate(_short(fn.qualname), fn.path, fn.lineno, fn.end_lineno)
    for qualname in ctx.change.added_classes:
        cls = ctx.head.classes.get(qualname)
        if cls is None or not is_prod_path(cls.path, ctx.cfg):
            continue
        name = _short(qualname)
        nested = qualname.rpartition(".")[0] in ctx.head.functions
        if nested or name.startswith("Test") or _abstract(ctx, qualname):
            continue
        yield _Candidate(name, cls.path, cls.lineno, cls.end_lineno)


def _ts_candidates(ctx: StaticCtx) -> Iterator[_Candidate]:
    """Functions a production TypeScript file exports at head and didn't export at base."""
    for path in ctx.change.changed_paths:
        facts = ctx.head.ts.get(path)
        if facts is None or facts.parse_error is not None or not is_prod_path(path, ctx.cfg):
            continue
        before = ctx.base.ts.get(path)
        old = set(before.exports) if before is not None else set()
        for fn in facts.functions:
            if fn.exported and fn.name in facts.exports and fn.name not in old:
                yield _Candidate(fn.name, path, fn.lineno, fn.end_lineno)


def _planned_caller(plan: PlanTask, name: str) -> PlanEdge | None:
    return next(
        (e for e in plan.call_edges if e.callee == name and not e.caller.startswith("test_")),
        None,
    )


def _oid(kind: str, anchor: Anchor, source: str) -> str:
    return hashlib.sha1(f"{kind}|{anchor.key()}|{source}".encode()).hexdigest()[:10]


class WiringRule:
    """`wiring.unwired_planned_here` and `wiring.unwired_new_symbol`, plus deferred
    obligations for symbols a later task wires."""

    ids = frozenset({PLANNED_HERE, NEW_SYMBOL})
    languages = frozenset({"py", "ts"})

    def check(self, ctx: StaticCtx) -> Iterable[RuleOutput]:
        plan = ctx.plan
        if plan is None:
            return []
        out: list[RuleOutput] = []
        seen: set[tuple[str, str]] = set()
        for cand in [*_python_candidates(ctx), *_ts_candidates(ctx)]:
            if (cand.path, cand.name) in seen:
                continue
            seen.add((cand.path, cand.name))
            span = (cand.lineno, cand.end_lineno)
            if production_references(ctx, cand.name, cand.path, span):
                continue
            out.append(self._classify(ctx, plan, cand))
        return out

    def _classify(self, ctx: StaticCtx, plan: PlanTask, cand: _Candidate) -> RuleOutput:
        name = cand.name
        edge = _planned_caller(plan, name)
        if edge is not None:
            return new_finding(
                PLANNED_HERE,
                file=cand.path,
                line=cand.lineno,
                end_line=cand.end_lineno,
                symbol=name,
                kind="spec_gap",
                impact="critical",
                grade=Grade.E1_EXACT,
                source=Source.DECLARED,
                message=f"{name} is new and nothing in production calls it",
                evidence=(
                    f"head index: 0 production refs; plan Task {plan.task_id} "
                    f"{edge.block_ref}: {edge.caller} -> {name}"
                ),
                evidence_key=f"{edge.caller} -> {name}",
                fix=f"call {name} from {edge.caller}, as the brief's code block does",
            )
        owner = plan.later_edges.get(name)
        if owner is not None:
            anchor = Anchor.of(cand.path, name, cand.lineno)
            source = f"plan:{owner}"
            return Obligation(
                oid=_oid("wiring.deferred", anchor, source),
                kind="wiring.deferred",
                rule=NEW_SYMBOL,
                anchor=anchor,
                source=source,
                owner=owner,
                params={"symbol": name, "path": cand.path},
                engines=("static",),
                impact="important",
                question=None,
                status="deferred",
            )
        tests = _test_references(ctx, name)
        where = ", ".join(sorted({f"{r.path}:{r.line}" for r in tests})[:3])
        return new_finding(
            NEW_SYMBOL,
            file=cand.path,
            line=cand.lineno,
            end_line=cand.end_lineno,
            symbol=name,
            kind="integration",
            impact="important",
            grade=Grade.E1_EXACT,
            source=Source.GENERIC,
            message=(
                f"{name} is new, only tests refer to it, and no task's code calls it"
                if tests
                else f"{name} is new and nothing refers to it, and no task's code calls it"
            ),
            evidence=(
                f"head index: 0 production refs; test refs: {where}"
                if tests
                else "head index: no references anywhere"
            ),
            evidence_key="unwired",
        )
