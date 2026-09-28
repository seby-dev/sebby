"""G1 plan fidelity: did the task do what its brief said.

`PlanFidelityRule` checks the brief's content against the head index: every symbol the
task's code blocks define exists (`plan.symbol_missing`), every signature the
**Interfaces** block states matches in parameter names, order, and stated defaults
(`plan.signature_mismatch`), every test the brief names exists or was renamed with a body
of token Jaccard similarity at least 0.8 (`plan.test_missing`), and every call edge the
code blocks draw between two production functions exists in the head call graph
(`plan.call_edge_missing`, an E2 question: a callee with other production callers is
usually an edge the prose attributed to the wrong owner).

`PlanFilesRule` compares the diff's paths with the task's `owns` and `runs`
(`plan.file_missing`, `plan.file_extra`), treating `.ts` and `.tsx` as one file,
exempting lockfiles and the task's report, and exempting a test file that changed only
because a signature it calls changed.

With no plan, neither rule says anything.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from revgate import gitio
from revgate.config import glob_match, is_prod_path, is_test_path
from revgate.model import Grade, RuleOutput, Source, Unverified, new_finding
from revgate.plan import CodeBlock, PlanSignature, PlanTask, parse_task_sections, plan_slug
from revgate.spi.facts import FuncFact, Param
from revgate.static.ctx import StaticCtx

SYMBOL_MISSING = "plan.symbol_missing"
SIGNATURE_MISMATCH = "plan.signature_mismatch"
TEST_MISSING = "plan.test_missing"
CALL_EDGE_MISSING = "plan.call_edge_missing"
FILE_MISSING = "plan.file_missing"
FILE_EXTRA = "plan.file_extra"

RENAME_JACCARD = 0.8
LOCKFILES = frozenset({"uv.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"})

_PY_LANGS = frozenset({"python", "py"})
_TS_LANGS = frozenset({"typescript", "ts", "tsx", "javascript", "js", "jsx"})
_TS_DEF_RE = re.compile(
    r"\bfunction\s+([A-Za-z_$][\w$]*)|\b(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\("
)
_PY_TEST_NAME_RE = re.compile(r"test_\w+")
_TOKEN_RE = re.compile(r"\w+|[^\w\s]")
_WRAPPER = "__snippet__"


# --- the brief's code blocks ----------------------------------------------------------------


def _code_blocks(plan: PlanTask) -> tuple[CodeBlock, ...]:
    prose = parse_task_sections(plan.section_text).get(plan.task_id)
    return prose.code_blocks if prose is not None else ()


def _parse_snippet(text: str) -> tuple[ast.Module, str] | None:
    """The block's tree and the source it was parsed from; a block of loose statements
    (a function body) is wrapped in a function so `return` parses."""
    try:
        return ast.parse(text), text
    except SyntaxError:
        pass
    wrapped = f"def {_WRAPPER}():\n" + "\n".join("    " + ln for ln in text.splitlines())
    wrapped += "\n    pass\n"
    try:
        return ast.parse(wrapped), wrapped
    except SyntaxError:
        return None


_DefNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _py_defs(block: CodeBlock) -> Iterator[tuple[_DefNode, str]]:
    parsed = _parse_snippet(block.text)
    if parsed is None:
        return
    tree, source = parsed
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name != _WRAPPER:
                yield node, source


def _body_text(node: ast.FunctionDef | ast.AsyncFunctionDef, source: str) -> str:
    lines = source.splitlines()
    start = node.body[0].lineno - 1 if node.body else node.lineno
    return "\n".join(lines[start : node.end_lineno or start])


def _tokens(text: str) -> frozenset[str]:
    return frozenset(_TOKEN_RE.findall(text))


def jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta and not tb:
        return 1.0
    return len(ta & tb) / len(ta | tb)


# --- head lookups ---------------------------------------------------------------------------


def _short(qualname: str) -> str:
    return qualname.rsplit(".", 1)[-1]


def _head_functions(ctx: StaticCtx, name: str) -> list[FuncFact]:
    """Head functions whose qualname ends with `name` (a short or a dotted name)."""
    suffix = f".{name}"
    return [ctx.head.functions[q] for q in sorted(ctx.head.functions) if q.endswith(suffix)]


def _head_defines(ctx: StaticCtx, name: str) -> bool:
    short = _short(name)
    if any(q in ctx.head.functions or q in ctx.head.classes for q in ctx.head.resolve(short)):
        return True
    return any(fn.name == short for facts in ctx.head.ts.values() for fn in facts.functions)


def _prod_definitions(ctx: StaticCtx, name: str) -> list[str]:
    """Production functions and classes whose last component is `name`."""
    found = [
        q
        for q in ctx.head.resolve(name)
        if (q in ctx.head.functions and is_prod_path(ctx.head.functions[q].path, ctx.cfg))
        or (q in ctx.head.classes and is_prod_path(ctx.head.classes[q].path, ctx.cfg))
    ]
    return sorted(q for q in found if _short(q) == name)


def _anchor_file(plan: PlanTask, suffix: str, *, test: bool = False) -> str:
    """Where a finding about something missing is anchored: an owned file of the right
    kind, else any owned file, else the plan itself."""
    pools: list[Sequence[str]] = [plan.owns.test] if test else []
    pools.append([*plan.owns.create, *plan.owns.modify])
    for pool in pools:
        for path in pool:
            if path.endswith(suffix):
                return path
    for pool in pools:
        if pool:
            return pool[0]
    return plan.plan_path


# --- signatures -----------------------------------------------------------------------------


def _norm(expr: str | None) -> str | None:
    if expr is None:
        return None
    try:
        return ast.unparse(ast.parse(expr, mode="eval"))
    except SyntaxError:
        return expr.strip()


def _param_name(p: Param) -> str:
    return {"vararg": "*", "kwarg": "**"}.get(p.kind, "") + p.name


def _head_params(fn: FuncFact) -> list[Param]:
    params = list(fn.params)
    if fn.class_qualname is not None and params and params[0].name in ("self", "cls"):
        params = params[1:]
    return params


def _signature_matches(sig: PlanSignature, fn: FuncFact) -> bool:
    params = _head_params(fn)
    if tuple(_param_name(p) for p in params) != sig.params:
        return False
    for stated, param in zip(sig.defaults, params, strict=True):
        if stated is not None and _norm(stated) != _norm(param.default):
            return False
    return True


def _render_plan(sig: PlanSignature) -> str:
    parts = [n if d is None else f"{n}={d}" for n, d in zip(sig.params, sig.defaults, strict=True)]
    return f"({', '.join(parts)})"


def _render_head(fn: FuncFact) -> str:
    parts = [
        _param_name(p) if p.default is None else f"{_param_name(p)}={p.default}"
        for p in _head_params(fn)
    ]
    return f"({', '.join(parts)})"


# --- the rule -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _BriefTest:
    name: str
    body: str | None  # the brief's own body, when a code block defines it


class PlanFidelityRule:
    """Symbols, signatures, tests, and call edges the brief promises, against head."""

    ids = frozenset({SYMBOL_MISSING, SIGNATURE_MISMATCH, TEST_MISSING, CALL_EDGE_MISSING})
    languages = frozenset({"py", "ts"})

    def check(self, ctx: StaticCtx) -> Iterable[RuleOutput]:
        plan = ctx.plan
        if plan is None:
            return []
        blocks = _code_blocks(plan)
        out: list[RuleOutput] = []
        reported: set[str] = set()
        out.extend(self._symbols(ctx, plan, blocks, reported))
        out.extend(self._signatures(ctx, plan, reported))
        out.extend(self._tests(ctx, plan, blocks))
        out.extend(self._edges(ctx, plan))
        return out

    # plan.symbol_missing ---------------------------------------------------------------

    def _symbols(
        self, ctx: StaticCtx, plan: PlanTask, blocks: Sequence[CodeBlock], reported: set[str]
    ) -> Iterator[RuleOutput]:
        for block in blocks:
            names: list[tuple[str, str]] = []  # (name, anchor suffix)
            if block.lang in _PY_LANGS:
                names = [(node.name, ".py") for node, _src in _py_defs(block)]
            elif block.lang in _TS_LANGS:
                names = [(a or b, ".ts") for a, b in _TS_DEF_RE.findall(block.text)]
            for name, suffix in names:
                if name in reported or name.startswith("test_") or _head_defines(ctx, name):
                    continue
                reported.add(name)
                yield self._missing(plan, name, suffix, block.ref)

    def _missing(self, plan: PlanTask, name: str, suffix: str, where: str) -> RuleOutput:
        return new_finding(
            SYMBOL_MISSING,
            file=_anchor_file(plan, suffix),
            line=1,
            kind="spec_gap",
            grade=Grade.E1_EXACT,
            source=Source.DECLARED,
            message=f"the brief defines {name}, and head doesn't define it",
            evidence=f"plan Task {plan.task_id} {where}: {name}; head index: no definition",
            evidence_key=name,
            fix=f"define {name} as the brief does",
        )

    # plan.signature_mismatch -----------------------------------------------------------

    def _signatures(
        self, ctx: StaticCtx, plan: PlanTask, reported: set[str]
    ) -> Iterator[RuleOutput]:
        for sig in plan.signatures:
            short = _short(sig.name)
            defs = _head_functions(ctx, sig.name)
            if not defs:
                if short not in reported and not _head_defines(ctx, sig.name):
                    reported.add(short)
                    yield self._missing(plan, short, ".py", "Interfaces")
                continue
            if any(_signature_matches(sig, fn) for fn in defs):
                continue
            if len(defs) > 1:
                where = ", ".join(fn.qualname for fn in defs)
                yield Unverified(SIGNATURE_MISMATCH, f"ambiguous: {sig.name} at {where}")
                continue
            fn = defs[0]
            yield new_finding(
                SIGNATURE_MISMATCH,
                file=fn.path,
                line=fn.lineno,
                end_line=fn.end_lineno,
                symbol=short,
                kind="spec_gap",
                grade=Grade.E1_EXACT,
                source=Source.DECLARED,
                message=f"{short} takes {_render_head(fn)}; the brief says {_render_plan(sig)}",
                evidence=f"plan Task {plan.task_id} Interfaces: `{sig.source}`",
                fix=f"give {short} the brief's parameters {_render_plan(sig)}",
            )

    # plan.test_missing -----------------------------------------------------------------

    def _brief_tests(self, plan: PlanTask, blocks: Sequence[CodeBlock]) -> list[_BriefTest]:
        bodies: dict[str, str] = {}
        for block in blocks:
            if block.lang not in _PY_LANGS:
                continue
            for node, source in _py_defs(block):
                if isinstance(node, ast.ClassDef) or not node.name.startswith("test_"):
                    continue
                bodies.setdefault(node.name, _body_text(node, source))
        return [_BriefTest(name, bodies.get(name)) for name in plan.tests]

    def _added_test_bodies(self, ctx: StaticCtx) -> list[str]:
        out: list[str] = []
        texts: dict[str, list[str]] = {}
        for fc in ctx.change.functions.values():
            fn = fc.head
            if fc.status != "added" or fn is None or not _short(fn.qualname).startswith("test_"):
                continue
            if fn.path not in texts:
                data = gitio.show(ctx.repo, ctx.tip, fn.path) or b""
                texts[fn.path] = data.decode("utf-8", errors="replace").splitlines()
            lines = texts[fn.path]
            out.append("\n".join(lines[fn.lineno : fn.end_lineno]))
        return out

    def _ts_test_sources(self, ctx: StaticCtx) -> str:
        blobs = gitio.ls_tree(ctx.repo, ctx.tip)
        paths = sorted(p for p in blobs if p.endswith((".ts", ".tsx")) and is_test_path(p, ctx.cfg))
        read = gitio.cat_blobs(ctx.repo, [blobs[p] for p in paths])
        return "\n".join(read[blobs[p]].decode("utf-8", errors="replace") for p in paths)

    def _tests(
        self, ctx: StaticCtx, plan: PlanTask, blocks: Sequence[CodeBlock]
    ) -> Iterator[RuleOutput]:
        head_names = {_short(q) for q in ctx.head.functions}
        added: list[str] | None = None
        ts_text: str | None = None
        for test in self._brief_tests(plan, blocks):
            if _PY_TEST_NAME_RE.fullmatch(test.name):
                if test.name in head_names:
                    continue
                if test.body is not None:
                    added = self._added_test_bodies(ctx) if added is None else added
                    if any(jaccard(test.body, body) >= RENAME_JACCARD for body in added):
                        continue  # a rename with the brief's body
                suffix = ".py"
            else:
                ts_text = self._ts_test_sources(ctx) if ts_text is None else ts_text
                if any(f"{q}{test.name}{q}" in ts_text for q in ("'", '"', "`")):
                    continue
                suffix = ".ts"
            yield new_finding(
                TEST_MISSING,
                file=_anchor_file(plan, suffix, test=True),
                line=1,
                kind="test_gap",
                grade=Grade.E1_EXACT,
                source=Source.DECLARED,
                message=f"the brief names the test {test.name}, and head has no such test",
                evidence=f"plan Task {plan.task_id}: {test.name}; head index: no such test",
                evidence_key=test.name,
                fix=f"add {test.name} as the brief writes it",
            )

    # plan.call_edge_missing ------------------------------------------------------------

    def _calls(self, ctx: StaticCtx, caller: FuncFact, callee_q: str, callee: str) -> bool:
        prefix = f"{caller.qualname}."
        for q, fn in ctx.head.functions.items():
            if q != caller.qualname and not q.startswith(prefix):
                continue
            for site in fn.calls:
                if callee_q in site.resolved or _short(site.callee) == callee:
                    return True
        return False

    def _edges(self, ctx: StaticCtx, plan: PlanTask) -> Iterator[RuleOutput]:
        for edge in plan.call_edges:
            if edge.caller == edge.callee:
                continue
            callers = [
                ctx.head.functions[q]
                for q in _prod_definitions(ctx, edge.caller)
                if q in ctx.head.functions
            ]
            callees = _prod_definitions(ctx, edge.callee)
            if not callers or not callees:
                continue  # plan.symbol_missing speaks for a name head doesn't define
            if len(callees) > 1:
                yield Unverified(
                    CALL_EDGE_MISSING,
                    f"ambiguous: {edge.callee} has {len(callees)} definitions at head "
                    f"({', '.join(callees)}); edge {edge.caller} -> {edge.callee} not checked",
                )
                continue
            callee_q = callees[0]
            if any(self._calls(ctx, fn, callee_q, edge.callee) for fn in callers):
                continue
            fn = callers[0]
            others = {
                s.caller
                for s in ctx.head.callers(callee_q)
                if is_prod_path(s.path, ctx.cfg) and s.caller != fn.qualname
            }
            yield new_finding(
                CALL_EDGE_MISSING,
                file=fn.path,
                line=fn.lineno,
                end_line=fn.end_lineno,
                symbol=edge.caller,
                kind="spec_gap",
                grade=Grade.E2_STRUCTURAL,
                source=Source.DECLARED,
                message=f"the brief has {edge.caller} call {edge.callee}; at head it doesn't",
                evidence=(
                    f"plan Task {plan.task_id} {edge.block_ref}: {edge.caller} -> {edge.callee};"
                    f" {edge.callee} has {len(others)} other production callers at head"
                ),
                evidence_key=f"{edge.caller} -> {edge.callee}",
            )


# --- files ----------------------------------------------------------------------------------


def _twin(path: str) -> str | None:
    if path.endswith(".tsx"):
        return path[:-1]
    if path.endswith(".ts"):
        return path + "x"
    return None


def _covered(path: str, entries: frozenset[str]) -> bool:
    if path in entries or _twin(path) in entries:
        return True
    return any(e.endswith("/") and path.startswith(e) for e in entries)


class PlanFilesRule:
    """Owned `create` and `modify` paths the diff didn't change, and changed paths the
    task neither owns nor runs."""

    ids = frozenset({FILE_MISSING, FILE_EXTRA})
    languages = frozenset({"any"})

    def check(self, ctx: StaticCtx) -> Iterable[RuleOutput]:
        plan = ctx.plan
        if plan is None:
            return []
        changed = frozenset(ctx.change.changed_paths)
        out: list[RuleOutput] = []
        owned_tests = frozenset(plan.owns.test)
        seen: set[str] = set()
        for kind, paths in (("create", plan.owns.create), ("modify", plan.owns.modify)):
            for path in paths:
                if path in seen or path in owned_tests or self._exempt(ctx, plan, path):
                    continue
                seen.add(path)
                if any(_covered(c, frozenset({path})) for c in changed):
                    continue
                out.append(
                    new_finding(
                        FILE_MISSING,
                        file=path,
                        line=1,
                        kind="spec_gap",
                        grade=Grade.E1_EXACT,
                        source=Source.DECLARED,
                        message=f"owns.{kind} names {path}, and the diff doesn't change it",
                        evidence=f"plan Task {plan.task_id} owns.{kind}: {path}; diff: unchanged",
                        evidence_key=path,
                    )
                )
        allowed = plan.owns.all() | frozenset(plan.runs)
        for path in sorted(changed):
            if _covered(path, allowed) or self._exempt(ctx, plan, path):
                continue
            if self._signature_follow_up(ctx, path):
                continue
            out.append(
                new_finding(
                    FILE_EXTRA,
                    file=path,
                    line=1,
                    kind="spec_gap",
                    grade=Grade.E1_EXACT,
                    source=Source.DECLARED,
                    message=f"{path} changed, and the task neither owns nor runs it",
                    evidence=f"plan Task {plan.task_id}: {path} not in owns or runs; diff: changed",
                    evidence_key=path,
                )
            )
        return out

    def _exempt(self, ctx: StaticCtx, plan: PlanTask, path: str) -> bool:
        if PurePosixPath(path).name in LOCKFILES:
            return True
        if ctx.report is not None and path == ctx.report.path:
            return True
        pattern = ctx.cfg.report_glob
        if pattern is None:
            return False
        filled = pattern.replace("{plan}", plan_slug(plan.plan_path)).replace("{id}", plan.task_id)
        return glob_match(path, (filled,))

    def _signature_follow_up(self, ctx: StaticCtx, path: str) -> bool:
        """A test that changed because a function it names changed its signature."""
        if not is_test_path(path, ctx.cfg):
            return False
        changed = {_short(q) for q, fc in ctx.change.functions.items() if fc.signature_changed}
        if not changed:
            return False
        facts = ctx.head.py.get(path)
        refs = facts.refs if facts is not None else ()
        ts = ctx.head.ts.get(path)
        if ts is not None:
            refs = (*refs, *ts.refs)
        return any(r.name in changed for r in refs)
