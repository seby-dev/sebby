"""The change model: typed deltas between two indexed trees.

`change_model` compares only the files the diff names. It pairs a removed and an added
function with the same normalized body (two or more body lines) as a move, classifies
every other function it touched as new, deleted, a signature change, a body change, a
docstring-only change, or a formatting-only change, and records module bindings whose
value changed together with their readers. The task kind comes from the paths the diff
touched and the plan's declaration, never from a commit subject.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from revgate import gitio
from revgate.config import Config, glob_match, is_test_path
from revgate.spi.facts import BindingFact, ClassFact, FuncFact, PyFileFacts
from revgate.spi.link import TreeIndex

FuncStatus = Literal["added", "modified", "removed", "doc_only", "moved"]
FuncKind = Literal["new", "deleted", "signature", "body", "doc_only", "format_only", "rename"]
BindingStatus = Literal["added", "modified", "removed"]
TaskKind = Literal["feat", "fix", "refactor", "perf", "test", "docs"]
Hunks = Mapping[str, tuple[tuple[int, int], ...]]
StatusEntry = tuple[str, str, str | None]

_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
_MIN_RENAME_BODY_LINES = 2


@dataclass(frozen=True)
class FuncChange:
    qualname: str  # the head qualname; the base qualname for a removed function
    path: str
    status: FuncStatus
    kind: FuncKind
    base: FuncFact | None
    head: FuncFact | None
    changed_head_lines: tuple[int, ...]
    signature_changed: bool


@dataclass(frozen=True)
class BindingChange:
    qualname: str
    path: str
    status: BindingStatus
    base: BindingFact | None
    head: BindingFact | None
    readers: tuple[str, ...]  # function qualnames that name it, and modules that import it


@dataclass(frozen=True)
class FieldChange:
    class_qualname: str
    path: str
    added: tuple[str, ...]
    removed: tuple[str, ...]


@dataclass(frozen=True)
class ChangeModel:
    status: tuple[StatusEntry, ...]
    changed_paths: tuple[str, ...]
    hunks: Hunks
    functions: Mapping[str, FuncChange]
    bindings: Mapping[str, BindingChange]
    fields: Mapping[str, FieldChange]
    added_classes: tuple[str, ...]
    removed_classes: tuple[str, ...]
    renamed: Mapping[str, str]  # base qualname -> head qualname
    removed_names: Mapping[str, tuple[str, ...]]  # base module -> top-level names gone
    removed_ts_exports: Mapping[str, tuple[str, ...]]  # base path -> exports gone
    task_kind: TaskKind
    nontest_changed_lines: int

    def added_lines(self, path: str) -> frozenset[int]:
        """Head-side line numbers the diff added or rewrote in `path`."""
        return hunk_lines(self.hunks.get(path, ()))


def hunk_lines(ranges: Iterable[tuple[int, int]]) -> frozenset[int]:
    return frozenset(line for start, count in ranges for line in range(start, start + count))


def _touches(ranges: Iterable[tuple[int, int]], lineno: int, end: int) -> bool:
    """A hunk adds a line inside `lineno..end`, or deletes lines between two of them."""
    for start, count in ranges:
        if count == 0:
            if lineno <= start < end:
                return True
        elif start <= end and start + count - 1 >= lineno:
            return True
    return False


def _lines_in(ranges: Iterable[tuple[int, int]], lineno: int, end: int) -> tuple[int, ...]:
    return tuple(sorted(n for n in hunk_lines(ranges) if lineno <= n <= end))


def _signature(fn: FuncFact) -> tuple[object, ...]:
    return (fn.params, fn.returns, fn.decorators, fn.is_async)


def _body_lines(fn: FuncFact) -> int:
    """Lines after the `def` line, less the docstring's: a stand-in for a statement count."""
    doc = fn.docstring.count("\n") + 1 if fn.docstring else 0
    return fn.end_lineno - fn.lineno - doc


def _changed_py(index: TreeIndex, paths: Iterable[str]) -> list[PyFileFacts]:
    return [index.py[p] for p in sorted(paths) if p in index.py]


def _parse_failed(index: TreeIndex, path: str) -> bool:
    facts = index.py.get(path)
    return facts is not None and facts.parse_error is not None


def _functions(files: Sequence[PyFileFacts]) -> dict[str, FuncFact]:
    out: dict[str, FuncFact] = {}
    for facts in files:
        for fn in facts.functions:
            out.setdefault(fn.qualname, fn)
    return out


def _classes(files: Sequence[PyFileFacts]) -> dict[str, ClassFact]:
    out: dict[str, ClassFact] = {}
    for facts in files:
        for cl in facts.classes:
            out.setdefault(cl.qualname, cl)
    return out


def _bindings(files: Sequence[PyFileFacts]) -> dict[str, BindingFact]:
    out: dict[str, BindingFact] = {}
    for facts in files:
        own = {b.qualname: b for b in facts.bindings}  # the last assignment in a file wins
        for qualname, binding in own.items():
            out.setdefault(qualname, binding)
    return out


def _short(qualname: str) -> str:
    return qualname.rsplit(".", 1)[-1]


def _pair_moves(base: Mapping[str, FuncFact], head: Mapping[str, FuncFact]) -> dict[str, str]:
    """Removed base function -> added head function with the same normalized body."""
    pool: dict[str, list[str]] = {}
    for q in sorted(q for q in base if q not in head):
        if _body_lines(base[q]) >= _MIN_RENAME_BODY_LINES:
            pool.setdefault(base[q].body_hash, []).append(q)
    moves: dict[str, str] = {}
    for q in sorted(q for q in head if q not in base):
        fn = head[q]
        candidates = pool.get(fn.body_hash)
        if not candidates or _body_lines(fn) < _MIN_RENAME_BODY_LINES:
            continue
        same_name = [c for c in candidates if _short(c) == _short(q)]
        old = (same_name or candidates)[0]
        candidates.remove(old)
        moves[old] = q
    return moves


def _function_changes(
    base: Mapping[str, FuncFact], head: Mapping[str, FuncFact], hunks: Hunks
) -> tuple[dict[str, FuncChange], dict[str, str]]:
    moves = _pair_moves(base, head)
    moved_to = set(moves.values())
    out: dict[str, FuncChange] = {}

    def lines(fn: FuncFact) -> tuple[int, ...]:
        return _lines_in(hunks.get(fn.path, ()), fn.lineno, fn.end_lineno)

    for old, new in moves.items():
        b, h = base[old], head[new]
        changed = _signature(b) != _signature(h)
        out[new] = FuncChange(new, h.path, "moved", "rename", b, h, lines(h), changed)
    for q, b in base.items():
        if q not in head and q not in moves:
            out[q] = FuncChange(q, b.path, "removed", "deleted", b, None, (), False)
    for q, h in head.items():
        if q in moved_to:
            continue
        b_or_none = base.get(q)
        if b_or_none is None:
            out[q] = FuncChange(q, h.path, "added", "new", None, h, lines(h), False)
            continue
        b = b_or_none
        if _signature(b) != _signature(h):
            out[q] = FuncChange(q, h.path, "modified", "signature", b, h, lines(h), True)
        elif b.body_hash != h.body_hash:
            out[q] = FuncChange(q, h.path, "modified", "body", b, h, lines(h), False)
        elif (b.docstring or "") != (h.docstring or ""):
            out[q] = FuncChange(q, h.path, "doc_only", "doc_only", b, h, lines(h), False)
        elif _touches(hunks.get(h.path, ()), h.lineno, h.end_lineno):
            out[q] = FuncChange(q, h.path, "modified", "format_only", b, h, lines(h), False)
    return dict(sorted(out.items())), dict(sorted(moves.items()))


def _readers(index: TreeIndex, binding: BindingFact) -> tuple[str, ...]:
    """Functions whose identifiers name the binding, and every module importing it."""
    module = binding.qualname[: -len(binding.name) - 1]
    found: set[str] = set()
    for path in sorted(index.py):
        facts = index.py[path]
        names: set[str] = set()  # local names bound to the binding itself
        modules: set[str] = set()  # local names bound to its module
        if facts.module == module:
            names.add(binding.name)
        for imp in facts.imports:
            if imp.module == module and imp.name == binding.name:
                names.add(imp.alias)
                found.add(facts.module)
            elif imp.name is None and imp.module == module:
                modules.add(imp.alias)
            elif imp.name is not None and f"{imp.module}.{imp.name}" == module:
                modules.add(imp.alias)
        if not names and not modules:
            continue
        for ref in facts.refs:
            if ref.in_symbol is None:
                continue
            if ref.kind == "identifier" and ref.name in names:
                found.add(ref.in_symbol)
            elif ref.kind == "attribute" and ref.name == binding.name and modules:
                head = ref.text.rsplit(".", 1)[0] if ref.text else ""
                if head in modules:
                    found.add(ref.in_symbol)
    return tuple(sorted(found))


def _binding_changes(
    base_idx: TreeIndex,
    head_idx: TreeIndex,
    base: Mapping[str, BindingFact],
    head: Mapping[str, BindingFact],
) -> dict[str, BindingChange]:
    out: dict[str, BindingChange] = {}
    for q in sorted(set(base) | set(head)):
        b, h = base.get(q), head.get(q)
        if b is None and h is not None:
            out[q] = BindingChange(q, h.path, "added", None, h, _readers(head_idx, h))
        elif h is None and b is not None:
            out[q] = BindingChange(q, b.path, "removed", b, None, _readers(base_idx, b))
        elif b is not None and h is not None and b.value_hash != h.value_hash:
            out[q] = BindingChange(q, h.path, "modified", b, h, _readers(head_idx, h))
    return out


def _field_changes(
    base: Mapping[str, ClassFact], head: Mapping[str, ClassFact]
) -> dict[str, FieldChange]:
    out: dict[str, FieldChange] = {}
    for q in sorted(set(base) & set(head)):
        b, h = base[q], head[q]
        added = tuple(f for f in h.fields if f not in b.fields)
        removed = tuple(f for f in b.fields if f not in h.fields)
        if added or removed:
            out[q] = FieldChange(q, h.path, added, removed)
    return out


def _top_level_names(facts: PyFileFacts) -> set[str]:
    prefix = f"{facts.module}."
    names: set[str] = set()
    for fn in facts.functions:
        if fn.class_qualname is None and not fn.is_nested:
            names.add(fn.qualname.removeprefix(prefix))
    names.update(cl.qualname.removeprefix(prefix) for cl in facts.classes)
    names.update(b.name for b in facts.bindings)
    return {n for n in names if "." not in n}


def _removed_names(
    base_files: Sequence[PyFileFacts], head_idx: TreeIndex
) -> dict[str, tuple[str, ...]]:
    head_by_module: dict[str, PyFileFacts] = {}
    for path in sorted(head_idx.py):
        head_by_module.setdefault(head_idx.py[path].module, head_idx.py[path])
    out: dict[str, tuple[str, ...]] = {}
    for facts in base_files:
        after = head_by_module.get(facts.module)
        if after is not None and after.parse_error is not None:
            continue  # a half-written file says nothing about what it still defines
        gone = _top_level_names(facts) - (_top_level_names(after) if after else set())
        if gone:
            out[facts.module] = tuple(sorted(gone))
    return dict(sorted(out.items()))


def _removed_ts_exports(
    base_idx: TreeIndex, head_idx: TreeIndex, status: Sequence[StatusEntry]
) -> dict[str, tuple[str, ...]]:
    gone_paths: set[str] = set()
    for letter, path, old in status:
        if letter == "D":
            gone_paths.add(path)
        elif letter == "R" and old is not None:
            gone_paths.add(old)
    out: dict[str, tuple[str, ...]] = {}
    for path in sorted({p for _l, p, _o in status} | gone_paths):
        before = base_idx.ts.get(path)
        if before is None:
            continue
        if path in gone_paths:
            remaining: set[str] = set()
        else:
            after = head_idx.ts.get(path)
            if after is None or after.parse_error is not None or before.parse_error is not None:
                continue
            remaining = set(after.exports)
        gone = tuple(sorted(set(before.exports) - remaining))
        if gone:
            out[path] = gone
    return out


def _is_doc(path: str, cfg: Config) -> bool:
    return path.endswith(_DOC_SUFFIXES) or glob_match(
        path, (*cfg.normative_docs, *cfg.history_docs)
    )


def _task_kind(paths: Sequence[str], cfg: Config, plan_kind: str | None) -> TaskKind:
    if plan_kind == "refactor":
        return "refactor"
    if plan_kind == "perf":
        return "perf"
    if paths:
        tests = [is_test_path(p, cfg) for p in paths]
        docs = [_is_doc(p, cfg) and not t for p, t in zip(paths, tests, strict=True)]
        if all(docs):
            return "docs"
        if any(tests) and all(t or d for t, d in zip(tests, docs, strict=True)):
            return "test"
    return "fix" if plan_kind == "fix" else "feat"


def _nontest_lines(numstat: Sequence[tuple[int | None, int | None, str]], cfg: Config) -> int:
    return sum((a or 0) + (d or 0) for a, d, path in numstat if not is_test_path(path, cfg))


def change_model(
    base: TreeIndex,
    head: TreeIndex,
    status: Sequence[StatusEntry],
    cfg: Config,
    *,
    hunks: Hunks | None = None,
    numstat: Sequence[tuple[int | None, int | None, str]] | None = None,
    plan_kind: str | None = None,
) -> ChangeModel:
    """Typed deltas for the files `status` names; `hunks` and `numstat` default to git's
    diff between the two indexes' revisions."""
    if hunks is None:
        hunks = gitio.diff_hunks(head.repo, base.rev, head.rev)
    if numstat is None:
        numstat = gitio.diff_numstat(head.repo, base.rev, head.rev)
    entries = tuple((letter, path, old) for letter, path, old in status)
    paths: set[str] = set()
    for letter, path, old in entries:
        paths.add(path)
        if letter == "R" and old is not None:
            paths.add(old)
    changed = tuple(sorted(paths))

    # A file that doesn't parse on either side is skipped whole; the index already records
    # `parse-error:<path>` for it.
    broken = {p for index in (base, head) for p in changed if _parse_failed(index, p)}
    readable = [p for p in changed if p not in broken]
    base_files = _changed_py(base, readable)
    head_files = _changed_py(head, readable)
    functions, renamed = _function_changes(_functions(base_files), _functions(head_files), hunks)
    base_classes, head_classes = _classes(base_files), _classes(head_files)
    return ChangeModel(
        status=entries,
        changed_paths=changed,
        hunks={p: tuple(hunks[p]) for p in sorted(hunks)},
        functions=functions,
        bindings=_binding_changes(base, head, _bindings(base_files), _bindings(head_files)),
        fields=_field_changes(base_classes, head_classes),
        added_classes=tuple(sorted(q for q in head_classes if q not in base_classes)),
        removed_classes=tuple(sorted(q for q in base_classes if q not in head_classes)),
        renamed=renamed,
        removed_names=_removed_names(base_files, head),
        removed_ts_exports=_removed_ts_exports(base, head, entries),
        task_kind=_task_kind(changed, cfg, plan_kind),
        nontest_changed_lines=_nontest_lines(numstat, cfg),
    )
