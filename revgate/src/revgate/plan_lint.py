"""`revgate plan-lint` and `revgate plan brief`: check a plan's `plan-waves` block.

This follows the pipeline spec's Appendix H "Plan validator", its "Estimated duration"
and "Risk and domain flags" sections (a dependency on a risky task crosses a wave
boundary), and amendment A8 (the plan file is forbidden to every task). It reads the plan
text it's given; callers read that text from the wave base.

Every check that needs the base tree (`modify-missing`, `create-exists`, and `context`)
runs only when both `repo` and `base` are given.
"""

from __future__ import annotations

import ast
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from revgate import gitio
from revgate.config import Config, glob_match
from revgate.plan import (
    BlockTask,
    PlanBlock,
    ProseTask,
    parse_plan_block,
    parse_task_sections,
    scan_markdown,
)
from revgate.plan_yaml import PlanYamlError, parse_yaml_subset

RISKS = frozenset({"low", "high"})
REQUIRED_FIELDS = ("id", "risk", "depends_on", "owns", "estimate_min")
TOLERANCE = 0.10

_LINE_ANCHOR_RE = re.compile(r":\d+(?:\s*[-–]\s*\d+)?$")
_HASH_LINE_RE = re.compile(r"^L\d+(?:-L?\d+)?$")
_TEST_NAME_RE = re.compile(r"\btest_\w+|(?<![\w.])(?:it|test)(?:\.\w+)?\(\s*[\"'`]")
_HM_RE = re.compile(r"(\d+)\s*h\s*(\d+)\s*min")
_MIN_RE = re.compile(r"(\d[\d,]*)\s*min")
_H_RE = re.compile(r"(\d+)\s*h\b")
_BARE_RE = re.compile(r"^\s*(\d[\d,]*)\s*$")

Level = Literal["error", "warning"]


@dataclass(frozen=True)
class LintMessage:
    level: Level
    code: str
    message: str
    task: str | None


@dataclass(frozen=True)
class WaveEstimate:
    wave: str
    tasks_sum: int
    critical_path: int
    throughput: int
    gate: int
    review: int
    wall: int
    tasks_cell: str = ""  # the tasks and their estimates, per sub-wave, for the table
    task_count: int = 0


@dataclass(frozen=True)
class LintReport:
    messages: tuple[LintMessage, ...]
    estimates: tuple[WaveEstimate, ...]
    total_min: int
    cap: int = 5

    def errors(self) -> tuple[LintMessage, ...]:
        return tuple(m for m in self.messages if m.level == "error")

    def warnings(self) -> tuple[LintMessage, ...]:
        return tuple(m for m in self.messages if m.level == "warning")


# --- anchors ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Anchor:
    path: str
    symbol: str | None
    heading: str | None
    lines: bool  # a line-range anchor, which earlier waves move


def _anchor(entry: str) -> _Anchor:
    entry = entry.strip()
    if "::" in entry:
        path, symbol = entry.split("::", 1)
        return _Anchor(path.strip(), symbol.strip(), None, False)
    if "#" in entry:
        path, heading = entry.split("#", 1)
        if _HASH_LINE_RE.match(heading.strip()):
            return _Anchor(path.strip(), None, None, True)
        return _Anchor(path.strip(), None, heading.strip(), False)
    stripped = _LINE_ANCHOR_RE.sub("", entry)
    return _Anchor(stripped, None, None, stripped != entry)


def _slug(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.strip().lower())
    return re.sub(r"\s+", "-", text)


def _py_names(source: str) -> set[str] | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    names: set[str] = set()

    def visit(body: Iterable[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(prefix + node.name)
                visit(node.body, f"{prefix}{node.name}.")
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(prefix + target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(prefix + node.target.id)

    visit(tree.body, "")
    return names


def _has_symbol(path: str, text: str, symbol: str) -> bool:
    if path.endswith(".py"):
        names = _py_names(text)
        if names is not None:
            return symbol in names
    last = symbol.split(".")[-1]
    return re.search(rf"(?<![\w$]){re.escape(last)}(?![\w$])", text) is not None


def _has_heading(text: str, heading: str) -> bool:
    want = heading.strip().lower()
    for _i, _level, found in scan_markdown(text).headings():
        if found.strip().lower() == want or _slug(found) in (_slug(heading), want):
            return True
    return False


class _Tree:
    """The files at one commit, read lazily."""

    def __init__(self, repo: Path, rev: str) -> None:
        self.repo = repo
        self.rev = rev
        self.files = gitio.ls_tree(repo, rev)
        self._text: dict[str, str | None] = {}

    def has(self, path: str) -> bool:
        if path in self.files:
            return True
        prefix = path.rstrip("/") + "/"
        return any(f.startswith(prefix) for f in self.files)

    def text(self, path: str) -> str | None:
        if path not in self._text:
            data = gitio.show(self.repo, self.rev, path) if path in self.files else None
            self._text[path] = None if data is None else data.decode("utf-8", errors="replace")
        return self._text[path]

    def resolves(self, anchor: _Anchor) -> bool:
        if not self.has(anchor.path):
            return False
        if anchor.symbol is None and anchor.heading is None:
            return True
        text = self.text(anchor.path)
        if text is None:
            return False
        if anchor.symbol is not None:
            return _has_symbol(anchor.path, text, anchor.symbol)
        assert anchor.heading is not None
        return _has_heading(text, anchor.heading)


# --- the lint --------------------------------------------------------------------------------


class _Lint:
    def __init__(self, md: str, plan_path: str, cfg: Config | None, cap: int) -> None:
        self.md = md
        self.plan_path = plan_path
        self.cfg = cfg
        self.cap = cap
        self.messages: list[LintMessage] = []

    def add(self, level: Level, code: str, message: str, task: str | None = None) -> None:
        self.messages.append(LintMessage(level, code, message, task))

    def error(self, code: str, message: str, task: str | None = None) -> None:
        self.add("error", code, message, task)

    def warn(self, code: str, message: str, task: str | None = None) -> None:
        self.add("warning", code, message, task)


def _raw_tasks(md: str) -> list[Mapping[str, object]]:
    """The block's task mappings as written, to tell a missing key from a defaulted one."""
    for fence in scan_markdown(md).fences:
        if " ".join(fence.info.split()) != "yaml plan-waves":
            continue
        out: list[Mapping[str, object]] = []
        raw = parse_yaml_subset(fence.text)
        waves = raw.get("waves") if isinstance(raw, dict) else None
        for wave in waves if isinstance(waves, list) else []:
            subs = wave.get("subwaves") if isinstance(wave, dict) else None
            for sub in subs if isinstance(subs, list) else []:
                tasks = sub.get("tasks") if isinstance(sub, dict) else None
                out.extend(
                    t for t in (tasks if isinstance(tasks, list) else []) if isinstance(t, dict)
                )
        return out
    return []


def _ancestors(tasks: Mapping[str, BlockTask]) -> dict[str, frozenset[str]]:
    """Every task's transitive dependencies among known tasks; a cycle can't loop forever."""
    out: dict[str, frozenset[str]] = {}
    for tid in tasks:
        seen: set[str] = set()
        stack = [d for d in tasks[tid].depends_on if d in tasks]
        while stack:
            dep = stack.pop()
            if dep not in seen:
                seen.add(dep)
                stack.extend(d for d in tasks[dep].depends_on if d in tasks)
        out[tid] = frozenset(seen - {tid})
    return out


def _forbids(entry: str, path: str) -> bool:
    if path == entry:
        return True
    if entry.endswith("/") and path.startswith(entry):
        return True
    return any(c in entry for c in "*?") and glob_match(path, [entry])


def _check_fields(lint: _Lint, md: str, ordered: Sequence[BlockTask]) -> None:
    for raw, task in zip(_raw_tasks(md), ordered, strict=False):
        missing = [k for k in REQUIRED_FIELDS if raw.get(k) is None and k != "depends_on"]
        if "depends_on" not in raw:
            missing.append("depends_on")
        if task.risk == "" and "risk" not in missing:
            missing.append("risk")
        if missing:
            lint.error("missing-field", f"{task.id} has no {', '.join(missing)}", task.id)
        elif task.risk not in RISKS:
            lint.error("risk", f"{task.id}'s risk is {task.risk!r}, not low or high", task.id)


def _check_ids(lint: _Lint, ordered: Sequence[BlockTask]) -> None:
    seen: set[str] = set()
    for task in ordered:
        if task.id in seen:
            lint.error("dup-id", f"task id {task.id} appears more than once", task.id)
        seen.add(task.id)


def _risky(task: BlockTask, cfg: Config | None) -> bool:
    if task.risk == "high":
        return True
    patterns = tuple(cfg.risk_paths) if cfg is not None else ()
    return bool(patterns) and any(glob_match(p, patterns) for p in task.owns.all())


def _check_deps(
    lint: _Lint,
    positions: Mapping[str, tuple[int, int]],
    tasks: Mapping[str, BlockTask],
) -> None:
    for tid, task in tasks.items():
        mine = positions[tid]
        for dep in task.depends_on:
            if dep not in positions:
                lint.error("dep-order", f"{tid} depends on unknown task {dep}", tid)
            elif positions[dep] >= mine:
                lint.error(
                    "dep-order", f"{tid} depends on {dep}, which isn't in an earlier sub-wave", tid
                )
            elif positions[dep][0] == mine[0] and _risky(tasks[dep], lint.cfg):
                lint.error(
                    "risky-dep-same-wave",
                    f"{tid} depends on {dep} in the same wave, and {dep} is risky "
                    "(risk: high or a [risk.paths] file); move it to a later wave",
                    tid,
                )


def _check_sharing(
    lint: _Lint,
    positions: Mapping[str, tuple[int, int]],
    tasks: Mapping[str, BlockTask],
    ancestors: Mapping[str, frozenset[str]],
) -> None:
    ids = list(tasks)
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            pa, pb = positions[a], positions[b]
            if pa[0] != pb[0]:
                continue
            shared = sorted(tasks[a].owns.all() & tasks[b].owns.all())
            if not shared:
                continue
            if pa == pb:
                lint.error(
                    "subwave-share", f"{a} and {b} share {', '.join(shared)} in one sub-wave", b
                )
            elif a not in ancestors[b] and b not in ancestors[a]:
                lint.error(
                    "wave-share",
                    f"{a} and {b} share {', '.join(shared)} in one wave with no dependency path",
                    b,
                )


def _check_forbidden(lint: _Lint, block: PlanBlock, tasks: Mapping[str, BlockTask]) -> None:
    for tid, task in tasks.items():
        rules = (*block.forbidden, *task.forbidden, lint.plan_path)
        for path in sorted(task.owns.all()):
            hit = next((r for r in rules if _forbids(r, path)), None)
            if hit is not None:
                lint.error("forbidden", f"{tid} owns {path}, which {hit!r} forbids", tid)


def _check_base(
    lint: _Lint,
    tree: _Tree,
    tasks: Mapping[str, BlockTask],
    ancestors: Mapping[str, frozenset[str]],
) -> None:
    for tid, task in tasks.items():
        created = {p for dep in ancestors[tid] for p in tasks[dep].owns.create}
        for path in task.owns.modify:
            if not tree.has(path) and path not in created:
                lint.error(
                    "modify-missing",
                    f"{tid} modifies {path}, which isn't at base or created by a dependency",
                    tid,
                )
        for path in task.owns.create:
            if tree.has(path):
                lint.error("create-exists", f"{tid} creates {path}, which exists at base", tid)


def _check_tests(lint: _Lint, tasks: Mapping[str, BlockTask]) -> None:
    for tid, task in tasks.items():
        if task.owns.test:
            continue
        if task.tests_none is None:
            lint.error("no-tests", f"{tid} names no test path and no `tests: none`", tid)
        elif not task.tests_none:
            lint.error("no-tests", f"{tid}'s `tests: none` gives no reason", tid)


def _check_context(
    lint: _Lint,
    tree: _Tree | None,
    tasks: Mapping[str, BlockTask],
    ancestors: Mapping[str, frozenset[str]],
) -> None:
    prefix = lint.cfg.risk_high_context_prefix if lint.cfg is not None else None
    for tid, task in tasks.items():
        created = {p for dep in ancestors[tid] | {tid} for p in tasks[dep].owns.create}
        for entry in task.context:
            anchor = _anchor(entry)
            if anchor.lines:
                lint.warn("context-lines", f"{tid}'s context {entry!r} is a line range", tid)
            if tree is not None and anchor.path not in created and not tree.resolves(anchor):
                lint.error("context", f"{tid}'s context {entry!r} doesn't resolve at base", tid)
        if prefix and task.risk == "high" and not any(c.startswith(prefix) for c in task.context):
            lint.error(
                "risk-context", f"{tid} is risk: high with no context entry under {prefix!r}", tid
            )


def _check_prose(
    lint: _Lint, tasks: Mapping[str, BlockTask], sections: Mapping[str, ProseTask]
) -> None:
    for tid, task in tasks.items():
        sec = sections.get(tid)
        if sec is None:
            lint.error("prose-mismatch", f"{tid} has no `### Task {tid}` section", tid)
            continue
        prose = set(sec.files_create) | set(sec.files_modify) | set(sec.files_test)
        block = set(task.owns.all())
        if prose != block:
            parts = []
            if block - prose:
                parts.append(f"the block names {', '.join(sorted(block - prose))}")
            if prose - block:
                parts.append(f"the Files list names {', '.join(sorted(prose - block))}")
            lint.error("prose-mismatch", f"{tid}: {' and '.join(parts)} only", tid)
    for tid in sections:
        if tid not in tasks:
            lint.error("prose-mismatch", f"`### Task {tid}` has no plan-waves entry", tid)


def _check_warnings(
    lint: _Lint, block: PlanBlock, tasks: Mapping[str, BlockTask], sections: Mapping[str, ProseTask]
) -> None:
    for wave in block.waves:
        count = sum(len(s.tasks) for s in wave.subwaves)
        if count == 1 and len(block.waves) > 1:
            lint.warn("one-task-wave", f"{wave.id} has one task; could it be a sub-wave?")
        for sub in wave.subwaves:
            if len(sub.tasks) > lint.cap:
                lint.warn(
                    "wide-subwave",
                    f"{sub.id} has {len(sub.tasks)} tasks, more than the cap of {lint.cap}",
                )
    for tid, task in tasks.items():
        if task.risk != "high":
            continue
        sec = sections.get(tid)
        named = sec is not None and _TEST_NAME_RE.search(sec.section_text) is not None
        if not task.owns.test or not named:
            lint.warn("risk-high-no-test", f"{tid} is risk: high and its plan names no test", tid)


# --- estimates and the duration table ---------------------------------------------------------


def _critical_path(tasks: Sequence[BlockTask]) -> int:
    est = {t.id: t.estimate_min or 0 for t in tasks}
    deps = {t.id: [d for d in t.depends_on if d in est] for t in tasks}
    memo: dict[str, int] = {}

    def longest(tid: str, seen: frozenset[str]) -> int:
        if tid in memo:
            return memo[tid]
        best = max((longest(d, seen | {d}) for d in deps[tid] if d not in seen), default=0)
        memo[tid] = est[tid] + best
        return memo[tid]

    return max((longest(t, frozenset({t})) for t in est), default=0)


def estimate_waves(
    block: PlanBlock, *, cap: int = 5, gate_min: int = 10, review_min: int = 20
) -> tuple[WaveEstimate, ...]:
    out: list[WaveEstimate] = []
    for wave in block.waves:
        tasks = [t for s in wave.subwaves for t in s.tasks]
        total = sum(t.estimate_min or 0 for t in tasks)
        critical = _critical_path(tasks)
        throughput = math.ceil(total / cap) if cap > 0 else total
        review = review_min if any(t.risk == "high" for t in tasks) else 0
        wall = max(critical, throughput) + gate_min + review
        cell = "; ".join(
            (f"{s.id}: " if len(wave.subwaves) > 1 else "")
            + ", ".join(f"{t.id} {t.estimate_min or 0}" for t in s.tasks)
            for s in wave.subwaves
        )
        out.append(
            WaveEstimate(
                wave.id, total, critical, throughput, gate_min, review, wall, cell, len(tasks)
            )
        )
    return tuple(out)


def _minutes(cell: str) -> int | None:
    m = _HM_RE.search(cell)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = _MIN_RE.search(cell)
    if m:
        return int(m.group(1).replace(",", ""))
    m = _H_RE.search(cell)
    if m:
        return int(m.group(1)) * 60
    m = _BARE_RE.match(cell.replace("*", ""))
    return int(m.group(1).replace(",", "")) if m else None


def _cells(line: str) -> list[str]:
    body = line.strip().replace("\\|", "\0")
    body = body[1:] if body.startswith("|") else body
    body = body[:-1] if body.endswith("|") else body
    return [c.replace("\0", "|").strip() for c in body.split("|")]


def duration_table(md: str) -> dict[str, int | None] | None:
    """Wave id (first cell) to wall-clock minutes, from the first table after a heading that
    mentions "duration" with a column whose header mentions "wall"; None when there's none."""
    doc = scan_markdown(md)
    headings = [i for i, _lvl, text in doc.headings() if "duration" in text.lower()]
    if not headings:
        return None
    i = headings[0] + 1
    while i < len(doc.lines):
        if not doc.in_fence[i] and doc.lines[i].lstrip().startswith("|"):
            header = [c.lower() for c in _cells(doc.lines[i])]
            col = next((n for n, c in enumerate(header) if "wall" in c), None)
            if col is None:
                return None
            rows: dict[str, int | None] = {}
            j = i + 2
            while j < len(doc.lines) and doc.lines[j].lstrip().startswith("|"):
                cells = _cells(doc.lines[j])
                key = cells[0].replace("*", "").replace("`", "").strip()
                rows[key] = _minutes(cells[col]) if col < len(cells) else None
                j += 1
            return rows
        i += 1
    return None


def _check_duration(lint: _Lint, estimates: Sequence[WaveEstimate]) -> None:
    table = duration_table(lint.md)
    if table is None:
        lint.error("duration-table", "no duration table with a wall-clock column")
        return
    for est in estimates:
        if est.wave not in table:
            lint.error("duration-table", f"the duration table has no row for {est.wave}")
            continue
        stated = table[est.wave]
        if stated is None:
            lint.error("duration-table", f"{est.wave}'s wall-clock cell has no minutes")
        elif abs(stated - est.wall) > TOLERANCE * est.wall:
            lint.error(
                "duration-table",
                f"{est.wave}'s table wall-clock is {stated} min; the block gives {est.wall} min",
            )


# --- entry points ----------------------------------------------------------------------------


def lint_plan(
    md: str,
    *,
    plan_path: str,
    repo: Path | None = None,
    base: str | None = None,
    cfg: Config | None = None,
    cap: int = 5,
    gate_min: int = 10,
    review_min: int = 20,
) -> LintReport:
    """Check the plan's block against Appendix H; errors refuse the plan."""
    lint = _Lint(md, plan_path, cfg, cap)
    try:
        block = parse_plan_block(md)
    except PlanYamlError as err:
        lint.error("parse", f"line {err.line}: {err.message}")
        return LintReport(tuple(lint.messages), (), 0)
    if block is None:
        lint.error("no-block", "the plan has no ```yaml plan-waves block")
        return LintReport(tuple(lint.messages), (), 0)
    ordered = [t for _w, _s, t in block.ordered_tasks()]
    positions: dict[str, tuple[int, int]] = {}
    tasks: dict[str, BlockTask] = {}
    for wi, si, t in block.ordered_tasks():
        positions.setdefault(t.id, (wi, si))
        tasks.setdefault(t.id, t)
    ancestors = _ancestors(tasks)
    sections = parse_task_sections(md)
    tree = _Tree(repo, base) if repo is not None and base is not None else None
    _check_ids(lint, ordered)
    _check_fields(lint, md, ordered)
    _check_deps(lint, positions, tasks)
    _check_sharing(lint, positions, tasks, ancestors)
    _check_forbidden(lint, block, tasks)
    if tree is not None:
        _check_base(lint, tree, tasks, ancestors)
    _check_tests(lint, tasks)
    _check_context(lint, tree, tasks, ancestors)
    _check_prose(lint, tasks, sections)
    estimates = estimate_waves(block, cap=cap, gate_min=gate_min, review_min=review_min)
    _check_duration(lint, estimates)
    _check_warnings(lint, block, tasks, sections)
    return LintReport(tuple(lint.messages), estimates, sum(e.wall for e in estimates), cap)


def _hm(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes:,} min ({minutes // 60} h {minutes % 60} min)"


def render_lint(report: LintReport) -> str:
    """Errors, then warnings, then the computed duration table, ready to paste into a plan."""
    lines: list[str] = []
    for m in (*report.errors(), *report.warnings()):
        where = f" [{m.task}]" if m.task else ""
        lines.append(f"{m.level} {m.code}{where}: {m.message}")
    if report.estimates:
        if lines:
            lines.append("")
        lines += [
            "| Wave | Tasks (estimate, minutes) | Sum | Sub-wave critical path "
            f"| Throughput (sum ÷ {report.cap}) | Gate | Focused review | Wave wall-clock |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for e in report.estimates:
            review = str(e.review) if e.review else "—"
            lines.append(
                f"| {e.wave} | {e.tasks_cell or '—'} | {e.tasks_sum} | {e.critical_path} "
                f"| {e.throughput} | {e.gate} | {review} | {e.wall} min |"
            )
        count = sum(e.task_count for e in report.estimates)
        task_min = sum(e.tasks_sum for e in report.estimates)
        lines.append(
            f"| **Plan total** | {count} tasks, {task_min:,} task-minutes | | | | | "
            f"| **{_hm(report.total_min)}** |"
        )
    if not lines:
        lines.append("ok: no errors or warnings")
    return "\n".join(lines) + "\n"


def _flow(items: Sequence[str]) -> str:
    return "[" + ", ".join(items) + "]"


def plan_brief(md: str, task_id: str, *, repo: Path | None = None, head: str | None = None) -> str:
    """One task's block entry and prose section, for the controller to append to a brief.

    Context anchors are re-resolved at `head` (the current plan branch) when `repo` and
    `head` are given; an anchor that doesn't resolve is marked `(unresolved)`. Raises
    `PlanYamlError` for a malformed block and `KeyError` for an unknown task.
    """
    block = parse_plan_block(md)
    sections = parse_task_sections(md)
    found = None
    if block is not None:
        found = next(((w, s, t) for w, s, t in block.ordered_tasks() if t.id == task_id), None)
    section = sections.get(task_id)
    if found is None and section is None:
        raise KeyError(task_id)
    tree = _Tree(repo, head) if repo is not None and head is not None else None
    out: list[str] = []
    if found is None or block is None:
        out += [f"No plan-waves entry for {task_id}.", ""]
    else:
        wi, si, t = found
        wave = block.waves[wi]
        out += [f"## Plan block: {t.id} (wave {wave.id}, sub-wave {wave.subwaves[si].id})", ""]
        out += [
            f"id: {t.id}",
            f"risk: {t.risk or '(missing)'}",
            f"kind: {t.kind}",
            f"depends_on: {_flow(t.depends_on)}",
            f"estimate_min: {t.estimate_min if t.estimate_min is not None else '(missing)'}",
            "owns:",
            f"  create: {_flow(t.owns.create)}",
            f"  modify: {_flow(t.owns.modify)}",
            f"  test: {_flow(t.owns.test)}",
        ]
        if t.runs:
            out.append(f"runs: {_flow(t.runs)}")
        forbidden = [*block.forbidden, *t.forbidden]
        if forbidden:
            out.append(f"forbidden: {_flow(forbidden)}")
        if t.tests_none is not None:
            out.append(f"tests: none: {t.tests_none}" if t.tests_none else "tests: none")
        if t.context:
            out.append("context:")
            for entry in t.context:
                mark = ""
                if tree is not None and not tree.resolves(_anchor(entry)):
                    mark = " (unresolved)"
                out.append(f"  - {entry}{mark}")
        out.append("")
    if section is None:
        out.append(f"No `### Task {task_id}` section.")
    else:
        out += ["## Plan section", "", section.section_text.rstrip()]
    return "\n".join(out) + "\n"
