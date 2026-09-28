"""The plan model: a plan's `plan-waves` block for structure, its task sections for content.

This follows the algorithm spec's "Plan and report models". The plan is read as committed
at the wave base (`git show <wave base>:<plan path>`), never from the implementer's
worktree, and the plan file joins every task's `forbidden` list (amendment A8). A plan
with no block (every plan written before the block existed) falls back to the prose
`### Task <id>` sections and their **Files** lists, and records `plan-block-missing` in
`unverified`, so every check that needs the block reports `unverified`, never `satisfied`.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from revgate import gitio
from revgate.model import canonical
from revgate.plan_yaml import PlanYamlError, parse_yaml_subset

KINDS = frozenset({"feat", "fix", "refactor", "perf", "test", "docs"})

_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_TASK_HEADING_RE = re.compile(r"^###\s+Task\s+([A-Za-z]*\d+[A-Za-z0-9.-]*)\s*[:.—-]\s*(.*)$")
_FILES_RE = re.compile(r"^\s*[-*]\s+(?:\*\*)?(Create|Modify|Test)(?:\*\*)?:(?:\*\*)?\s*(.*)$")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_LINE_RANGE_RE = re.compile(r":\d+(?:\s*[-–]\s*\d+)?$")
_LABEL_RE = re.compile(r"^\s*\*\*[^*]+:\*\*")
_STEP_RE = re.compile(r"^\s*[-*]\s+\[[ xX]\]")
_GOAL_RE = re.compile(r"^\*\*Goal:\*\*\s*(.+?)\s*$")
_OWNER_RE = re.compile(r"`([A-Za-z_][\w.]*)(?:\(\))?`")
_SIGNATURE_RE = re.compile(r"^([A-Za-z_][\w.]*)\s*\((.*)\)\s*(?:->\s*(.+?))?\s*:?$")
_PY_TEST_RE = re.compile(r"\bdef\s+(test_\w+)")
_TS_TEST_RE = re.compile(r"(?<![\w.])(?:it|test)(?:\.\w+)?\(\s*([\"'`])(.*?)\1")
_TS_LANGS = frozenset({"typescript", "ts", "tsx", "javascript", "js", "jsx"})

# --- types ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Owns:
    create: tuple[str, ...]
    modify: tuple[str, ...]
    test: tuple[str, ...]

    def all(self) -> frozenset[str]:
        return frozenset(self.create) | frozenset(self.modify) | frozenset(self.test)


@dataclass(frozen=True)
class BlockTask:
    id: str
    risk: str  # "" when the block leaves it out; plan-lint reports that
    depends_on: tuple[str, ...]
    owns: Owns
    runs: tuple[str, ...]
    forbidden: tuple[str, ...]
    context: tuple[str, ...]
    tests_none: str | None  # the reason given with `tests: none`, else None
    estimate_min: int | None
    kind: str


@dataclass(frozen=True)
class PlanSubwave:
    id: str
    tasks: tuple[BlockTask, ...]


@dataclass(frozen=True)
class PlanWave:
    id: str
    subwaves: tuple[PlanSubwave, ...]


@dataclass(frozen=True)
class PlanBlock:
    forbidden: tuple[str, ...]
    waves: tuple[PlanWave, ...]

    def ordered_tasks(self) -> list[tuple[int, int, BlockTask]]:
        """(wave index, sub-wave index, task) in block order."""
        return [
            (wi, si, task)
            for wi, wave in enumerate(self.waves)
            for si, sub in enumerate(wave.subwaves)
            for task in sub.tasks
        ]


@dataclass(frozen=True)
class PlanSignature:
    name: str
    params: tuple[str, ...]
    defaults: tuple[str | None, ...]
    returns: str | None
    source: str


@dataclass(frozen=True)
class PlanEdge:
    caller: str
    callee: str
    block_ref: str


@dataclass(frozen=True)
class CodeBlock:
    lang: str
    text: str
    preceding_prose: str
    ref: str  # plan:<task id>:code-block:<n>, n counting the section's fenced blocks from 1


@dataclass(frozen=True)
class ProseTask:
    id: str
    title: str
    files_create: tuple[str, ...]
    files_modify: tuple[str, ...]
    files_test: tuple[str, ...]
    interfaces_text: str
    code_blocks: tuple[CodeBlock, ...]
    section_text: str


@dataclass(frozen=True)
class PlanTask:
    task_id: str
    owns: Owns
    runs: tuple[str, ...]
    forbidden: tuple[str, ...]
    kind: str
    signatures: tuple[PlanSignature, ...]
    tests: tuple[str, ...]
    call_edges: tuple[PlanEdge, ...]
    later_owners: Mapping[str, str]  # file -> the earliest later task that owns it
    later_edges: Mapping[str, str]  # callee -> the earliest later task whose code calls it
    owners: Mapping[str, tuple[str, ...]]  # file -> every task that owns it, in plan order
    risk: str | None
    depends_on: tuple[str, ...]
    context: tuple[str, ...]
    estimate_min: int | None
    section_text: str
    plan_path: str
    goal: str | None
    block_present: bool
    unverified: tuple[str, ...]


# --- Markdown scanning ----------------------------------------------------------------------


@dataclass(frozen=True)
class Fence:
    """A top-level fenced block; `open_line` and `close_line` are 0-based line indexes."""

    info: str
    text: str
    open_line: int
    close_line: int  # the closing fence, or len(lines) when the fence never closes


@dataclass(frozen=True)
class Markdown:
    lines: tuple[str, ...]
    fences: tuple[Fence, ...]
    in_fence: tuple[bool, ...]  # per line, including the fence lines themselves

    def headings(self) -> list[tuple[int, int, str]]:
        """(line index, level, text) of every heading outside a fence."""
        out: list[tuple[int, int, str]] = []
        for i, line in enumerate(self.lines):
            if self.in_fence[i]:
                continue
            m = _HEADING_RE.match(line)
            if m:
                out.append((i, len(m.group(1)), m.group(2)))
        return out


def scan_markdown(md: str) -> Markdown:
    lines = md.splitlines()
    in_fence = [False] * len(lines)
    fences: list[Fence] = []
    i = 0
    while i < len(lines):
        m = _FENCE_RE.match(lines[i])
        if not m or (m.group(2)[0] == "`" and "`" in m.group(3)):
            i += 1
            continue
        marker = m.group(2)
        close = len(lines)
        for j in range(i + 1, len(lines)):
            c = _FENCE_RE.match(lines[j])
            if (
                c
                and c.group(2)[0] == marker[0]
                and len(c.group(2)) >= len(marker)
                and not c.group(3).strip()
            ):
                close = j
                break
        for k in range(i, min(close + 1, len(lines))):
            in_fence[k] = True
        fences.append(Fence(m.group(3).strip(), "\n".join(lines[i + 1 : close]), i, close))
        i = close + 1
    return Markdown(tuple(lines), tuple(fences), tuple(in_fence))


# --- the plan-waves block -------------------------------------------------------------------


def _block_fence(doc: Markdown) -> Fence | None:
    for fence in doc.fences:
        if " ".join(fence.info.split()) == "yaml plan-waves":
            return fence
    return None


def parse_plan_block(md: str) -> PlanBlock | None:
    """The plan's `plan-waves` block, or None when it has none.

    Raises `PlanYamlError` with a line number in the plan file when the block is outside the
    YAML subset or doesn't have the block's shape.
    """
    fence = _block_fence(scan_markdown(md))
    if fence is None:
        return None
    try:
        raw = parse_yaml_subset(fence.text)
    except PlanYamlError as err:
        raise PlanYamlError(err.line + fence.open_line + 1, err.message) from err
    return _Shape(fence.open_line + 1).block(raw)


class _Shape:
    """Checks the parsed block's shape; errors point at the block's opening fence."""

    def __init__(self, line: int) -> None:
        self.line = line

    def fail(self, message: str) -> PlanYamlError:
        return PlanYamlError(self.line, message)

    def mapping(self, value: object, what: str) -> Mapping[str, object]:
        if not isinstance(value, dict):
            raise self.fail(f"{what} must be a mapping")
        return value

    def seq(self, value: object, what: str) -> list[object]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise self.fail(f"{what} must be a list")
        return value

    def ident(self, value: object, what: str) -> str:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise self.fail(f"{what} must be a string")
        return str(value)

    def strs(self, value: object, what: str) -> tuple[str, ...]:
        return tuple(self.ident(v, what) for v in self.seq(value, what))

    def block(self, raw: object) -> PlanBlock:
        if raw is None:
            return PlanBlock((), ())
        doc = self.mapping(raw, "the plan-waves block")
        waves: list[PlanWave] = []
        for w in self.seq(doc.get("waves"), "waves"):
            wave = self.mapping(w, "a wave")
            wid = self.ident(wave.get("id"), "a wave id")
            subs: list[PlanSubwave] = []
            for s in self.seq(wave.get("subwaves"), f"{wid}'s subwaves"):
                sub = self.mapping(s, "a sub-wave")
                sid = self.ident(sub.get("id"), "a sub-wave id")
                tasks = tuple(self.task(t) for t in self.seq(sub.get("tasks"), f"{sid}'s tasks"))
                subs.append(PlanSubwave(sid, tasks))
            waves.append(PlanWave(wid, tuple(subs)))
        return PlanBlock(self.strs(doc.get("forbidden"), "forbidden"), tuple(waves))

    def task(self, raw: object) -> BlockTask:
        t = self.mapping(raw, "a task")
        tid = self.ident(t.get("id"), "a task id")
        owns_raw = self.mapping(t.get("owns") or {}, f"{tid}'s owns")
        owns = Owns(
            self.strs(owns_raw.get("create"), f"{tid}'s owns.create"),
            self.strs(owns_raw.get("modify"), f"{tid}'s owns.modify"),
            self.strs(owns_raw.get("test"), f"{tid}'s owns.test"),
        )
        risk = t.get("risk")
        estimate = t.get("estimate_min")
        if estimate is not None and (isinstance(estimate, bool) or not isinstance(estimate, int)):
            raise self.fail(f"{tid}'s estimate_min must be an integer")
        kind = t.get("kind", "feat")
        if kind not in KINDS:
            raise self.fail(f"{tid}'s kind must be one of {', '.join(sorted(KINDS))}")
        return BlockTask(
            id=tid,
            risk="" if risk is None else self.ident(risk, f"{tid}'s risk"),
            depends_on=self.strs(t.get("depends_on"), f"{tid}'s depends_on"),
            owns=owns,
            runs=self.strs(t.get("runs"), f"{tid}'s runs"),
            forbidden=self.strs(t.get("forbidden"), f"{tid}'s forbidden"),
            context=self.strs(t.get("context"), f"{tid}'s context"),
            tests_none=self.tests_none(t.get("tests"), tid),
            estimate_min=estimate,
            kind=str(kind),
        )

    def tests_none(self, value: object, tid: str) -> str | None:
        if value is None:
            return None
        if isinstance(value, dict) and set(value) == {"none"}:
            reason = value["none"]
            return "" if reason is None else str(reason).strip()
        if isinstance(value, str):
            m = re.fullmatch(r"none\b\s*(?:[:—–-]+\s*)?(.*)", value.strip(), re.S)
            if m:
                return m.group(1).strip()
        raise self.fail(f"{tid}'s tests must be `none` with a reason")


# --- task sections --------------------------------------------------------------------------


def _first_path(text: str) -> str | None:
    m = _BACKTICK_RE.search(text)
    if not m:
        return None
    return _LINE_RANGE_RE.sub("", m.group(1).strip())


def _preceding_prose(doc: Markdown, open_line: int, start: int) -> str:
    i = open_line - 1
    while i >= start and not doc.lines[i].strip() and not doc.in_fence[i]:
        i -= 1
    end = i + 1
    while i >= start and doc.lines[i].strip() and not doc.in_fence[i]:
        i -= 1
    return "\n".join(doc.lines[i + 1 : end])


def _interfaces_text(doc: Markdown, start: int, end: int) -> str:
    for i in range(start, end):
        if doc.in_fence[i] or "**Interfaces:**" not in doc.lines[i]:
            continue
        out = [doc.lines[i].split("**Interfaces:**", 1)[1]]
        for j in range(i + 1, end):
            line = doc.lines[j]
            if not doc.in_fence[j] and (
                _LABEL_RE.match(line) or _STEP_RE.match(line) or _HEADING_RE.match(line)
            ):
                break
            out.append(line)
        return "\n".join(out).strip()
    return ""


def parse_task_sections(md: str) -> dict[str, ProseTask]:
    """Every `### Task <id>` section, keyed by id; a repeated id keeps its first section."""
    doc = scan_markdown(md)
    headings = doc.headings()
    out: dict[str, ProseTask] = {}
    for n, (start, level, _text) in enumerate(headings):
        m = _TASK_HEADING_RE.match(doc.lines[start])
        if level != 3 or not m or m.group(1) in out:
            continue
        end = next((i for i, lvl, _t in headings[n + 1 :] if lvl <= 3), len(doc.lines))
        tid = m.group(1)
        files: dict[str, list[str]] = {"Create": [], "Modify": [], "Test": []}
        for i in range(start + 1, end):
            fm = None if doc.in_fence[i] else _FILES_RE.match(doc.lines[i])
            path = _first_path(fm.group(2)) if fm else None
            if fm and path and path not in files[fm.group(1)]:
                files[fm.group(1)].append(path)
        blocks: list[CodeBlock] = []
        for fence in doc.fences:
            if start < fence.open_line < end:
                lang = (fence.info.split() or [""])[0].lower()
                ref = f"plan:{tid}:code-block:{len(blocks) + 1}"
                prose = _preceding_prose(doc, fence.open_line, start + 1)
                blocks.append(CodeBlock(lang, fence.text, prose, ref))
        out[tid] = ProseTask(
            id=tid,
            title=m.group(2).strip(),
            files_create=tuple(files["Create"]),
            files_modify=tuple(files["Modify"]),
            files_test=tuple(files["Test"]),
            interfaces_text=_interfaces_text(doc, start + 1, end),
            code_blocks=tuple(blocks),
            section_text="\n".join(doc.lines[start:end]),
        )
    return out


# --- code blocks and signatures -------------------------------------------------------------


@dataclass
class _CodeFacts:
    edges: list[PlanEdge] = dataclasses.field(default_factory=list)
    tests: list[str] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _callee(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _calls(nodes: Sequence[ast.AST]) -> list[str]:
    """Callee names in `nodes` in source order, not descending into nested defs or classes."""
    found: list[tuple[int, int, str]] = []
    stack = list(nodes)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Call):
            name = _callee(node.func)
            if name is not None:
                found.append((node.lineno, node.col_offset, name))
        stack.extend(c for c in ast.iter_child_nodes(node) if not isinstance(c, _SCOPES))
    return [name for _l, _c, name in sorted(found)]


def _parse_snippet(text: str) -> tuple[ast.Module, bool] | None:
    try:
        return ast.parse(text), False
    except SyntaxError:
        pass
    wrapped = "def __snippet__():\n" + "\n".join("    " + ln for ln in text.splitlines())
    try:
        return ast.parse(wrapped + "\n    pass\n"), True
    except SyntaxError:
        return None


def _python_facts(block: CodeBlock, facts: _CodeFacts) -> None:
    facts.tests.extend(_PY_TEST_RE.findall(block.text))
    parsed = _parse_snippet(block.text)
    if parsed is None:
        facts.notes.append(f"plan-snippet-unparsed:{block.ref}")
        return
    tree, wrapped = parsed
    top: list[ast.stmt] = list(tree.body)
    if wrapped:
        snippet = tree.body[0]
        assert isinstance(snippet, ast.FunctionDef)
        top = list(snippet.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not (
            wrapped and node is tree.body[0]
        ):
            for callee in _calls(node.body):
                facts.edges.append(PlanEdge(node.name, callee, block.ref))
    loose = [s for s in top if not isinstance(s, (*_SCOPES, ast.Import, ast.ImportFrom))]
    loose_calls = _calls(loose)
    if not loose_calls:
        return
    owners = _OWNER_RE.findall(block.preceding_prose)
    if not owners:
        facts.notes.append(f"plan-snippet-owner:{block.ref}")
        return
    owner = owners[-1].split(".")[-1]
    facts.edges.extend(PlanEdge(owner, callee, block.ref) for callee in loose_calls)


def _code_facts(prose: ProseTask | None) -> _CodeFacts:
    facts = _CodeFacts()
    for block in prose.code_blocks if prose else ():
        if block.lang in ("python", "py"):
            _python_facts(block, facts)
        elif block.lang in _TS_LANGS:
            facts.tests.extend(title for _q, title in _TS_TEST_RE.findall(block.text))
    facts.edges = _unique(facts.edges)
    facts.tests = _unique(facts.tests)
    return facts


def _signature(span: str) -> PlanSignature | None:
    text = re.sub(r"^(?:@\w+\s+)+", "", span.strip())
    has_def = bool(re.match(r"(?:async\s+)?def\s", text))
    text = re.sub(r"^(?:async\s+)?def\s+", "", text)
    m = _SIGNATURE_RE.match(text)
    if not m:
        return None
    try:
        tree = ast.parse(f"def _f({m.group(2)}): pass")
    except SyntaxError:
        return None
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    a = fn.args
    positional = [*a.posonlyargs, *a.args]
    pos_defaults: list[ast.expr | None] = [None] * (len(positional) - len(a.defaults))
    pos_defaults.extend(a.defaults)
    pairs: list[tuple[str, ast.expr | None, ast.expr | None]] = [
        (arg.arg, d, arg.annotation) for arg, d in zip(positional, pos_defaults, strict=True)
    ]
    if a.vararg:
        pairs.append(("*" + a.vararg.arg, None, a.vararg.annotation))
    pairs.extend(
        (arg.arg, d, arg.annotation) for arg, d in zip(a.kwonlyargs, a.kw_defaults, strict=True)
    )
    if a.kwarg:
        pairs.append(("**" + a.kwarg.arg, None, a.kwarg.annotation))
    if pairs and pairs[0][0] in ("self", "cls"):
        pairs = pairs[1:]
    returns = m.group(3).strip() if m.group(3) else None
    annotated = any(ann is not None for _n, _d, ann in pairs)
    if not (has_def or returns or annotated):
        return None  # a call written in prose, not a signature
    return PlanSignature(
        name=m.group(1),
        params=tuple(n for n, _d, _a in pairs),
        defaults=tuple(None if d is None else ast.unparse(d) for _n, d, _a in pairs),
        returns=returns,
        source=span.strip(),
    )


def _signatures(interfaces_text: str) -> tuple[PlanSignature, ...]:
    out: list[PlanSignature] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for span in _BACKTICK_RE.findall(interfaces_text):
        sig = _signature(span)
        if sig is not None and (sig.name, sig.params) not in seen:
            seen.add((sig.name, sig.params))
            out.append(sig)
    return tuple(out)


def _unique[T](items: Sequence[T]) -> list[T]:
    seen: set[T] = set()
    out: list[T] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# --- PlanTask -------------------------------------------------------------------------------


def _goal(doc: Markdown) -> str | None:
    for i, line in enumerate(doc.lines):
        m = None if doc.in_fence[i] else _GOAL_RE.match(line)
        if m:
            return m.group(1)
    return None


def _prose_owns(prose: ProseTask) -> Owns:
    return Owns(prose.files_create, prose.files_modify, prose.files_test)


def task_from_plan(md: str, plan_path: str, task_id: str) -> PlanTask | None:
    """The model of one task, or None when the plan has neither a block entry nor a section.

    Raises `PlanYamlError` when the plan's block is malformed.
    """
    block = parse_plan_block(md)
    sections = parse_task_sections(md)
    prose = sections.get(task_id)
    entry = (
        next((t for _w, _s, t in block.ordered_tasks() if t.id == task_id), None) if block else None
    )
    if entry is None and prose is None:
        return None
    notes: list[str] = []
    # Plan order and ownership: from the block when the task is in it, else heading order.
    order: dict[str, tuple[int, int]] = {}
    owns_by: dict[str, Owns] = {}
    if block is not None and entry is not None:
        for wi, si, t in block.ordered_tasks():
            order.setdefault(t.id, (wi, si))
            owns_by.setdefault(t.id, t.owns)
    else:
        notes.append("plan-block-missing" if block is None else "plan-task-not-in-block")
        for n, (tid, sec) in enumerate(sections.items()):
            order[tid] = (n, 0)
            owns_by[tid] = _prose_owns(sec)
    if prose is None:
        notes.append("plan-section-missing")
    ordered_ids = list(order)  # insertion order is plan order
    mine = order[task_id]
    owns = owns_by[task_id]
    owners: dict[str, list[str]] = {}
    for tid in ordered_ids:
        for path in sorted(owns_by[tid].all()):
            owners.setdefault(path, []).append(tid)
    later_ids = [tid for tid in ordered_ids if order[tid] > mine]
    later_owners: dict[str, str] = {}
    for tid in later_ids:
        for path in sorted(owns_by[tid].all() - owns.all()):
            later_owners.setdefault(path, tid)
    facts = _code_facts(prose)
    my_callees = {e.callee for e in facts.edges}
    later_edges: dict[str, str] = {}
    for tid in later_ids:
        for edge in _code_facts(sections.get(tid)).edges:
            if edge.callee not in my_callees:
                later_edges.setdefault(edge.callee, tid)
    plan_forbidden = block.forbidden if block is not None else ()
    task_forbidden = entry.forbidden if entry is not None else ()
    return PlanTask(
        task_id=task_id,
        owns=owns,
        runs=entry.runs if entry else (),
        forbidden=tuple(_unique([*plan_forbidden, *task_forbidden, plan_path])),
        kind=entry.kind if entry else "feat",
        signatures=_signatures(prose.interfaces_text) if prose else (),
        tests=tuple(facts.tests),
        call_edges=tuple(facts.edges),
        later_owners=later_owners,
        later_edges=later_edges,
        owners={path: tuple(tids) for path, tids in sorted(owners.items())},
        risk=entry.risk if entry else None,
        depends_on=entry.depends_on if entry else (),
        context=entry.context if entry else (),
        estimate_min=entry.estimate_min if entry else None,
        section_text=prose.section_text if prose else "",
        plan_path=plan_path,
        goal=_goal(scan_markdown(md)),
        block_present=entry is not None,
        unverified=tuple(_unique([*notes, *facts.notes])),
    )


def load_plan_task(repo: Path, wave_base: str, plan_path: str, task_id: str) -> PlanTask | None:
    """The task as the plan was committed at `wave_base` (amendment A8), or None."""
    data = gitio.show(repo, wave_base, plan_path)
    if data is None:
        return None
    return task_from_plan(data.decode("utf-8", errors="replace"), plan_path, task_id)


def plan_slug(plan_path: str) -> str:
    return PurePosixPath(plan_path).stem


def plan_task_hash(task: PlanTask) -> str:
    """A SHA-256 of the task's canonical JSON; equal tasks give equal hashes."""
    payload = dataclasses.asdict(task)
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()
