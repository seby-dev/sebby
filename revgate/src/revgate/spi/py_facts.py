"""Python facts for one file: one `ast.parse`, one visitor, and no imports of the code.

`index_python_file` never raises on bad source; a file that doesn't parse gets facts with
`parse_error` set and every tuple empty. Every collection in the output is a sorted or
source-ordered tuple, so the same source always gives equal facts.

`index_python_file_with_types` also returns the receiver-typing hints the linker needs
(`TypeHints`), which aren't part of `PyFileFacts`: local variable types per function and
annotated class fields.
"""

from __future__ import annotations

import ast
import hashlib
import itertools
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field

from revgate.spi.facts import (
    BindingFact,
    CallSite,
    CallUse,
    Cardinality,
    ClassFact,
    FuncFact,
    ImportFact,
    IsinstanceBranch,
    IsinstanceChain,
    Param,
    ParamKind,
    PyFileFacts,
    Ref,
    RefKind,
)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_STRING_REF = 80
_MANY_CALLS = frozenset({"list", "sorted", "tuple", "set", "frozenset", "reversed"})
_GROW_METHODS = frozenset({"append", "extend", "insert"})
_ANN_ELEM = re.compile(
    r"^(?:list|List|Sequence|Iterable|Iterator|tuple|Tuple|set|Set|frozenset|FrozenSet)"
    r"\[([A-Za-z_][\w.]*)(?:,\s*\.\.\.)?\]$"
)
_ANN_DIRECT = re.compile(r"^[A-Za-z_][\w.]*$")
_DefNode = ast.FunctionDef | ast.AsyncFunctionDef
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


@dataclass(frozen=True)
class TypeHints:
    """Receiver-typing hints for the linker, as written in the source.

    `local_types` rows are `(function qualname, variable, type)`, where type is a dotted
    class expression as written (`Note`, `schema.Note`), `=<qualname>` for a class already
    qualified (`self` and `cls`), `[Elem]` for a sequence of `Elem`, or `@<dotted>` for one
    element of the iterable `<dotted>` (a `for` loop target). `field_types` rows are
    `(class qualname, field, annotation)` for annotated class-level names.
    """

    local_types: tuple[tuple[str, str, str], ...] = ()
    field_types: tuple[tuple[str, str, str], ...] = ()


def module_name_for(path: str) -> str:
    """`src/pkg/api/app.py` -> `pkg.api.app`; `scripts/x.py` -> `scripts.x`; `pkg/__init__.py`
    -> `pkg`."""
    stem = path[:-3] if path.endswith(".py") else path
    parts = stem.split("/")
    if parts[0] == "src" and len(parts) > 1:
        parts = parts[1:]
    if parts[-1] == "__init__" and len(parts) > 1:
        parts = parts[:-1]
    return ".".join(parts)


def index_python_file(path: str, source: str, *, module: str, is_test: bool) -> PyFileFacts:
    return index_python_file_with_types(path, source, module=module, is_test=is_test)[0]


def index_python_file_with_types(
    path: str, source: str, *, module: str, is_test: bool
) -> tuple[PyFileFacts, TypeHints]:
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        return _empty(path, module, is_test, f"{type(exc).__name__}: {exc}"), TypeHints()
    visitor = _Visitor(path, module, is_test, path.endswith("__init__.py"))
    try:
        visitor.run(tree)
    except RecursionError as exc:  # pathologically deep expressions
        return _empty(path, module, is_test, f"RecursionError: {exc}"), TypeHints()
    return visitor.result(), visitor.hints()


def _empty(path: str, module: str, is_test: bool, error: str) -> PyFileFacts:
    return PyFileFacts(path, module, is_test, error, (), (), (), (), (), (), ())


# --- small helpers ------------------------------------------------------------------------


def _src(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    return ast.unparse(node)


def dotted(node: ast.AST) -> str | None:
    """`a.b.c` for a chain of names and attributes, else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base is not None else None
    return None


def _callee_text(node: ast.AST) -> str:
    """Dotted text as written, with `()` marking a call inside the chain (`f().g`)."""

    def walk(n: ast.AST) -> str | None:
        if isinstance(n, ast.Name):
            return n.id
        if isinstance(n, ast.Attribute):
            base = walk(n.value)
            return f"{base}.{n.attr}" if base is not None else None
        if isinstance(n, ast.Call):
            base = walk(n.func)
            return f"{base}()" if base is not None else None
        return None

    return walk(node) or ast.unparse(node)


def _hash_stmts(stmts: Sequence[ast.stmt]) -> str:
    dump = ast.dump(ast.Module(body=list(stmts), type_ignores=[]), annotate_fields=False)
    return hashlib.sha1(dump.encode("utf-8"), usedforsecurity=False).hexdigest()


def _hash_expr(node: ast.AST) -> str:
    return hashlib.sha1(
        ast.dump(node, annotate_fields=False).encode("utf-8"), usedforsecurity=False
    ).hexdigest()


def _own_nodes(stmts: Sequence[ast.AST]) -> Iterator[ast.AST]:
    """Every node under `stmts`, without descending into nested scopes."""
    stack: list[ast.AST] = list(reversed(stmts))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _SCOPE_NODES):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def ann_type(ann: str | None) -> str | None:
    """A dotted class from an annotation, `[Elem]` for a sequence of `Elem`, else None."""
    if not ann:
        return None
    text = ann.replace("typing.", "").strip().strip("'\"")
    text = re.sub(r"\s*\|\s*None$", "", text)
    text = re.sub(r"^None\s*\|\s*", "", text)
    text = re.sub(r"^Optional\[(.*)\]$", r"\1", text)
    match = _ANN_ELEM.match(text)
    if match:
        return f"[{match.group(1)}]"
    if _ANN_DIRECT.match(text) and text not in ("None", "Any", "object"):
        return text
    return None


def _terminates(stmts: Sequence[ast.stmt]) -> bool:
    """True when control can't fall off the end of `stmts`."""
    if not stmts:
        return False
    last = stmts[-1]
    if isinstance(last, ast.Return | ast.Raise):
        return True
    if isinstance(last, ast.If):
        return _terminates(last.body) and _terminates(last.orelse)
    if isinstance(last, ast.With | ast.AsyncWith):
        return _terminates(last.body)
    if isinstance(last, ast.Try | ast.TryStar):
        if _terminates(last.finalbody):
            return True
        body_ends = _terminates(last.body) or _terminates(last.orelse)
        return body_ends and all(_terminates(h.body) for h in last.handlers)
    if isinstance(last, ast.While):
        test_true = isinstance(last.test, ast.Constant) and bool(last.test.value)
        return test_true and not any(isinstance(n, ast.Break) for n in _own_nodes(last.body))
    if isinstance(last, ast.Match):
        irrefutable = any(
            isinstance(c.pattern, ast.MatchAs) and c.pattern.pattern is None and c.guard is None
            for c in last.cases
        )
        return irrefutable and all(_terminates(c.body) for c in last.cases)
    return False


def _is_none(node: ast.expr | None) -> bool:
    return node is None or (isinstance(node, ast.Constant) and node.value is None)


def cardinality_of(fn: _DefNode) -> Cardinality:
    """`many` when the function can return several items (a comprehension, a list grown in a
    loop, a generator), `optional` when it returns a value or None, `one` when it returns a
    value on every path, and `unknown` when it returns no value at all."""
    body = fn.body
    grown: set[str] = set()
    many_names: set[str] = set()
    returns: list[ast.Return] = []
    for node in _own_nodes(body):
        if isinstance(node, ast.Yield | ast.YieldFrom):
            return "many"
        if isinstance(node, ast.Return):
            returns.append(node)
        elif isinstance(node, ast.For | ast.AsyncFor | ast.While):
            for inner in _own_nodes(node.body):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr in _GROW_METHODS
                    and isinstance(inner.func.value, ast.Name)
                ):
                    grown.add(inner.func.value.id)
                elif isinstance(inner, ast.AugAssign) and isinstance(inner.target, ast.Name):
                    grown.add(inner.target.id)
        elif isinstance(node, ast.Assign) and _is_many_expr(node.value, set()):
            many_names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    many_names |= grown
    value_returns = [r for r in returns if not _is_none(r.value)]
    if any(r.value is not None and _is_many_expr(r.value, many_names) for r in value_returns):
        return "many"
    if not value_returns:
        return "unknown"
    returns_none = len(value_returns) < len(returns) or not _terminates(body)
    return "optional" if returns_none else "one"


def _is_many_expr(node: ast.expr, many_names: set[str]) -> bool:
    if isinstance(node, ast.ListComp | ast.SetComp | ast.GeneratorExp):
        return True
    if isinstance(node, ast.Name):
        return node.id in many_names
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id in _MANY_CALLS
    if isinstance(node, ast.List | ast.Set):
        return any(isinstance(e, ast.Starred) for e in node.elts)
    return False


def _call_use(node: ast.Call, parents: dict[int, ast.AST]) -> CallUse:
    child: ast.AST = node
    parent = parents.get(id(child))
    while isinstance(parent, ast.Await):
        child, parent = parent, parents.get(id(parent))
    if isinstance(parent, ast.Starred):
        child, parent = parent, parents.get(id(parent))
        return "passed" if isinstance(parent, ast.Call) else "other"
    if isinstance(parent, ast.Subscript) and parent.value is child:
        s = parent.slice
        is_zero = isinstance(s, ast.Constant) and s.value == 0 and not isinstance(s.value, bool)
        return "index0" if is_zero else "other"
    if isinstance(parent, ast.Attribute):
        return "attr"
    if isinstance(parent, ast.Expr):
        return "discarded"
    if isinstance(parent, ast.Return):
        return "returned"
    if isinstance(parent, ast.Assign):
        unpack = len(parent.targets) == 1 and isinstance(parent.targets[0], ast.Tuple | ast.List)
        return "unpacked" if unpack else "assigned"
    if isinstance(parent, ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
        return "assigned"
    if isinstance(parent, ast.For | ast.AsyncFor | ast.comprehension):
        if parent.iter is child:
            return "iterated"
        if isinstance(parent, ast.comprehension) and child in parent.ifs:
            return "truth"
        return "other"
    if isinstance(parent, ast.If | ast.While | ast.IfExp | ast.Assert):
        return "truth" if parent.test is child else "other"
    if isinstance(parent, ast.BoolOp):
        return "truth"
    if isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not):
        return "truth"
    if isinstance(parent, ast.Call):
        return "other" if parent.func is child else "passed"
    if isinstance(parent, ast.keyword):
        return "passed"
    return "other"


def _index0_reads(scope: ast.AST) -> dict[str, list[int]]:
    """Lines of each `name[0]` read in `scope`'s own body, not in nested scopes."""
    body: list[ast.AST]
    if isinstance(scope, ast.Lambda):
        body = [scope.body]
    elif isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Module):
        body = list(scope.body)
    else:
        body = [scope]
    out: dict[str, list[int]] = {}
    for node in _own_nodes(body):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Name)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == 0
            and not isinstance(node.slice.value, bool)
        ):
            out.setdefault(node.value.id, []).append(node.lineno)
    return out


def _isinstance_test(test: ast.expr) -> tuple[str, tuple[str, ...], bool] | None:
    """`(subject, classes, negated)` when `test` is `[not] isinstance(x, ...)`, or an `and`
    whose first operand is."""
    node: ast.expr = test
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        node = node.values[0]
    negated = False
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        negated, node = True, node.operand
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "isinstance"
        and len(node.args) == 2
        and not node.keywords
    ):
        target = node.args[1]
        elts = target.elts if isinstance(target, ast.Tuple) else [target]
        classes = tuple(dotted(e) or ast.unparse(e) for e in elts)
        return ast.unparse(node.args[0]), classes, negated
    return None


def _exits_early(stmt: ast.If) -> bool:
    return (
        not stmt.orelse
        and bool(stmt.body)
        and isinstance(stmt.body[-1], ast.Return | ast.Raise | ast.Continue)
    )


def _blocks(stmts: Sequence[ast.stmt]) -> Iterator[list[ast.stmt]]:
    """`stmts` and every statement block nested in it, without entering nested scopes."""
    yield list(stmts)
    for node in _own_nodes(stmts):
        if isinstance(node, _SCOPE_NODES):
            continue
        if isinstance(node, ast.ExceptHandler | ast.match_case):
            yield node.body
            continue
        for name in ("body", "orelse", "finalbody"):
            block = getattr(node, name, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                yield block


def _chains(
    stmts: Sequence[ast.stmt], func: str, mentioned: tuple[str, ...]
) -> list[IsinstanceChain]:
    seen: set[int] = set()
    found: list[tuple[int, IsinstanceChain]] = []
    for block in _blocks(stmts):
        i = 0
        while i < len(block):
            stmt = block[i]
            test = _isinstance_test(stmt.test) if isinstance(stmt, ast.If) else None
            if not isinstance(stmt, ast.If) or test is None or id(stmt) in seen:
                i += 1
                continue
            subject = test[0]
            branches: list[IsinstanceBranch] = []
            if _exits_early(stmt):
                j = i
                while j < len(block):
                    cur = block[j]
                    t = _isinstance_test(cur.test) if isinstance(cur, ast.If) else None
                    if not isinstance(cur, ast.If) or t is None or t[0] != subject:
                        break
                    if not _exits_early(cur):
                        break
                    seen.add(id(cur))
                    branches.append(IsinstanceBranch(t[1], cur.lineno, t[2]))
                    j += 1
                i = j
            else:
                cur_if: ast.If | None = stmt
                while cur_if is not None:
                    t = _isinstance_test(cur_if.test)
                    if t is None or t[0] != subject:
                        break
                    seen.add(id(cur_if))
                    branches.append(IsinstanceBranch(t[1], cur_if.test.lineno, t[2]))
                    nxt = cur_if.orelse
                    cur_if = nxt[0] if len(nxt) == 1 and isinstance(nxt[0], ast.If) else None
                i += 1
            chain = IsinstanceChain(func, subject, tuple(branches), mentioned)
            found.append((branches[0].line, chain))
    found.sort(key=lambda pair: pair[0])
    return [chain for _line, chain in found]


def _module_constants(tree: ast.Module) -> dict[str, ast.List | ast.Tuple]:
    """Module-level `NAME = [...]` or `NAME = (...)` bindings, by name."""
    out: dict[str, ast.List | ast.Tuple] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        else:
            continue
        if isinstance(target, ast.Name) and isinstance(value, ast.List | ast.Tuple):
            out[target.id] = value
    return out


def _class_entries(const: ast.List | ast.Tuple) -> list[tuple[str, int]]:
    """A table's class column: each element's first item (or the element itself), as dotted
    text with its line, when every entry is a dotted name."""
    out: list[tuple[str, int]] = []
    for elt in const.elts:
        head = elt.elts[0] if isinstance(elt, ast.Tuple | ast.List) and elt.elts else elt
        text = dotted(head)
        if text is None:
            return []
        out.append((text, head.lineno))
    return out


def _dispatch_tables(
    fn: _DefNode, consts: Mapping[str, ast.List | ast.Tuple]
) -> dict[str, list[tuple[str, int]]]:
    """Loop variables bound to a constant table's class column: `for cls, label in TABLE`."""
    out: dict[str, list[tuple[str, int]]] = {}
    for node in _own_nodes(fn.body):
        if not (isinstance(node, ast.For | ast.AsyncFor) and isinstance(node.iter, ast.Name)):
            continue
        const = consts.get(node.iter.id)
        target = node.target
        first = target.elts[0] if isinstance(target, ast.Tuple) and target.elts else target
        if const is None or not isinstance(first, ast.Name):
            continue
        entries = _class_entries(const)
        if entries:
            out[first.id] = entries
    return out


def _expand_chain(
    chain: IsinstanceChain,
    tables: Mapping[str, list[tuple[str, int]]],
    consts: Mapping[str, ast.List | ast.Tuple],
) -> IsinstanceChain:
    """A branch on a table's loop variable becomes one branch per entry, in table order,
    each at its entry's line; a class that names a constant tuple becomes its classes."""
    branches: list[IsinstanceBranch] = []
    for b in chain.branches:
        if len(b.classes) == 1 and b.classes[0] in tables:
            branches.extend(
                IsinstanceBranch((cls,), line, b.negated) for cls, line in tables[b.classes[0]]
            )
            continue
        classes: list[str] = []
        for cls in b.classes:
            const = consts.get(cls)
            entries = _class_entries(const) if const is not None else []
            if entries:
                classes.extend(text for text, _line in entries)
            else:
                classes.append(cls)
        branches.append(IsinstanceBranch(tuple(classes), b.line, b.negated))
    return IsinstanceChain(chain.func, chain.subject, tuple(branches), chain.mentioned)


def _constant_names(fn: _DefNode, consts: Mapping[str, ast.List | ast.Tuple]) -> Iterator[ast.AST]:
    """The nodes of every module constant the function reads, so its classes count as
    mentioned (a guard through `isinstance(x, EXCLUDED)` mentions each excluded class)."""
    read = {
        n.id
        for n in _own_nodes(fn.body)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in consts
    }
    for name in sorted(read):
        yield from ast.walk(consts[name])


def _mentioned(nodes: Iterator[ast.AST]) -> tuple[str, ...]:
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Name | ast.Attribute):
            text = dotted(node)
            if text is not None:
                names.add(text)
    return tuple(sorted(names))


def _raises(fn: _DefNode) -> tuple[str, ...]:
    out: dict[str, None] = {}
    for node in _own_nodes(fn.body):
        if isinstance(node, ast.Raise) and node.exc is not None:
            target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            out[dotted(target) or ast.unparse(target)] = None
    return tuple(out)


def _params(args: ast.arguments) -> tuple[Param, ...]:
    out: list[Param] = []
    positional = [*args.posonlyargs, *args.args]
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults))
    defaults.extend(args.defaults)
    for i, arg in enumerate(positional):
        kind: ParamKind = "posonly" if i < len(args.posonlyargs) else "pos"
        out.append(Param(arg.arg, kind, _src(defaults[i]), _src(arg.annotation)))
    if args.vararg is not None:
        out.append(Param(args.vararg.arg, "vararg", None, _src(args.vararg.annotation)))
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        out.append(Param(arg.arg, "kwonly", _src(default), _src(arg.annotation)))
    if args.kwarg is not None:
        out.append(Param(args.kwarg.arg, "kwarg", None, _src(args.kwarg.annotation)))
    return tuple(out)


def _decorator_text(node: ast.expr) -> str:
    return dotted(node) or ast.unparse(node)


# --- the visitor ---------------------------------------------------------------------------


@dataclass
class _Frame:
    kind: str  # "func" or "class"
    qualname: str
    try_depth: int = 0
    calls: list[CallSite] = field(default_factory=list)


@dataclass
class _FuncDraft:
    node: _DefNode
    qualname: str
    class_qualname: str | None
    is_nested: bool
    frame: _Frame


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str, module: str, is_test: bool, is_package: bool) -> None:
        self.path = path
        self.module = module
        self.is_test = is_test
        self.is_package = is_package
        self.parents: dict[int, ast.AST] = {}
        self.frames: list[_Frame] = []
        self.module_frame = _Frame("module", f"{module}.<module>")
        self.imports: list[ImportFact] = []
        self.drafts: list[_FuncDraft] = []
        self.classes: list[ClassFact] = []
        self.bindings: list[BindingFact] = []
        self.refs: list[Ref] = []
        self.chains: list[IsinstanceChain] = []
        self.local_types: list[tuple[str, str, str]] = []
        self.field_types: list[tuple[str, str, str]] = []
        self.tree: ast.Module | None = None
        self._index0_reads: dict[int, dict[str, list[int]]] = {}
        self.constants: dict[str, ast.List | ast.Tuple] = {}

    def run(self, tree: ast.Module) -> None:
        self.tree = tree
        self.constants = _module_constants(tree)
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self.parents[id(child)] = node
        self.visit(tree)
        top = [s for s in tree.body if not isinstance(s, _SCOPE_NODES)]
        mentioned = _mentioned(_own_nodes(top))
        self.chains.extend(_chains(top, self.module_frame.qualname, mentioned))

    # --- scope helpers
    def _frame(self) -> _Frame:
        return self.frames[-1] if self.frames else self.module_frame

    def _func_frame(self) -> _Frame:
        for frame in reversed(self.frames):
            if frame.kind == "func":
                return frame
        return self.module_frame

    def _qual(self, name: str) -> str:
        return f"{self.frames[-1].qualname}.{name}" if self.frames else f"{self.module}.{name}"

    def _in_symbol(self) -> str | None:
        return self.frames[-1].qualname if self.frames else None

    def _ref(self, line: int, name: str, kind: RefKind, text: str = "") -> None:
        self.refs.append(Ref(self.path, line, name, kind, self._in_symbol(), text))

    def _local_type(self, var: str, type_text: str | None) -> None:
        frame = self._func_frame()
        if frame.kind == "func" and type_text:
            self.local_types.append((frame.qualname, var, type_text))

    # --- imports
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            self.imports.append(ImportFact(alias.name, None, local, node.lineno))
            self._ref(node.lineno, alias.name.split(".")[-1], "import", alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level:
            base = self.module.split(".")
            keep = len(base) - node.level + (1 if self.is_package else 0)
            parts = base[: max(keep, 0)] + ([module] if module else [])
            module = ".".join(parts)
        for alias in node.names:
            local = alias.asname or alias.name
            self.imports.append(ImportFact(module, alias.name, local, node.lineno))
            self._ref(node.lineno, alias.name, "import", f"{module}.{alias.name}")

    # --- definitions
    def _visit_func(self, node: _DefNode) -> None:
        for dec in node.decorator_list:
            self.visit(dec)
        self.generic_visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        qualname = self._qual(node.name)
        enclosing = self.frames[-1] if self.frames else None
        class_q = (
            enclosing.qualname if enclosing is not None and enclosing.kind == "class" else None
        )
        nested = any(f.kind == "func" for f in self.frames)
        frame = _Frame("func", qualname)
        self.drafts.append(_FuncDraft(node, qualname, class_q, nested, frame))
        params = _params(node.args)
        self.frames.append(frame)
        for p in params:
            self._local_type(p.name, ann_type(p.annotation))
        if class_q is not None and params and params[0].name in ("self", "cls"):
            self._local_type(params[0].name, f"={class_q}")
        for stmt in node.body:
            self.visit(stmt)
        self.frames.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_func(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expr in [*node.bases, *(k.value for k in node.keywords), *node.decorator_list]:
            self.visit(expr)
        qualname = self._qual(node.name)
        fields: list[str] = []
        methods: list[str] = []
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                ann = ast.unparse(stmt.annotation)
                if ann.startswith(("ClassVar", "typing.ClassVar")):
                    continue
                fields.append(stmt.target.id)
                self.field_types.append((qualname, stmt.target.id, ann))
            elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                methods.append(stmt.name)
        bases = tuple(dotted(b) or ast.unparse(b) for b in node.bases)
        self.classes.append(
            ClassFact(
                qualname,
                self.path,
                node.lineno,
                node.end_lineno or node.lineno,
                bases,
                tuple(fields),
                tuple(methods),
            )
        )
        self.frames.append(_Frame("class", qualname))
        for stmt in node.body:
            self.visit(stmt)
        self.frames.pop()

    # --- module-level bindings and local types
    def _binding(self, target: ast.expr, value: ast.expr, stmt: ast.stmt) -> None:
        names = target.elts if isinstance(target, ast.Tuple | ast.List) else [target]
        for name in names:
            if isinstance(name, ast.Name):
                self.bindings.append(
                    BindingFact(
                        f"{self.module}.{name.id}",
                        name.id,
                        self.path,
                        stmt.lineno,
                        stmt.end_lineno or stmt.lineno,
                        _hash_expr(value),
                    )
                )

    def visit_Assign(self, node: ast.Assign) -> None:
        if not self.frames:
            for target in node.targets:
                self._binding(target, node.value, node)
        elif self.frames[-1].kind == "func" and isinstance(node.value, ast.Call):
            callee = dotted(node.value.func)
            last = callee.rsplit(".", 1)[-1] if callee else ""
            if callee and last[:1].isupper():
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self._local_type(target.id, callee)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if not self.frames and node.value is not None:
            self._binding(node.target, node.value, node)
        if isinstance(node.target, ast.Name) and self.frames and self.frames[-1].kind == "func":
            self._local_type(node.target.id, ann_type(ast.unparse(node.annotation)))
        self.generic_visit(node)

    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        if not self.frames:
            self._binding(node.name, node.value, node)
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self._loop_type(node)
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._loop_type(node)
        self.generic_visit(node)

    def _loop_type(self, node: ast.For | ast.AsyncFor) -> None:
        if not isinstance(node.target, ast.Name):
            return
        if isinstance(node.iter, ast.Name):
            self._local_type(node.target.id, f"@{node.iter.id}")
        elif isinstance(node.iter, ast.Attribute):
            text = dotted(node.iter)
            if text is not None:
                self._local_type(node.target.id, f"@{text}")

    # --- try blocks
    def _visit_try(self, node: ast.Try | ast.TryStar) -> None:
        frame = self._func_frame()
        frame.try_depth += 1
        for stmt in node.body:
            self.visit(stmt)
        frame.try_depth -= 1
        for handler in node.handlers:
            self.visit(handler)
        for stmt in [*node.orelse, *node.finalbody]:
            self.visit(stmt)

    def visit_Try(self, node: ast.Try) -> None:
        self._visit_try(node)

    def visit_TryStar(self, node: ast.TryStar) -> None:
        self._visit_try(node)

    # --- expressions
    def _held_index0(self, node: ast.Call) -> bool:
        """`name = f(...)` whose enclosing scope later reads `name[0]` (the prototype's
        consumer idiom: the result is held, then only its first item is read)."""
        parent = self.parents.get(id(node))
        if not (
            isinstance(parent, ast.Assign)
            and len(parent.targets) == 1
            and isinstance(parent.targets[0], ast.Name)
        ):
            return False
        name = parent.targets[0].id
        scope: ast.AST | None = parent
        while scope is not None and not isinstance(scope, (*_SCOPE_NODES, ast.Module)):
            scope = self.parents.get(id(scope))
        if scope is None:
            return False
        reads = self._index0_reads.get(id(scope))
        if reads is None:
            reads = _index0_reads(scope)
            self._index0_reads[id(scope)] = reads
        return any(line >= node.lineno for line in reads.get(name, ()))

    def visit_Call(self, node: ast.Call) -> None:
        frame = self._func_frame()
        keywords = tuple(k.arg if k.arg is not None else "**" for k in node.keywords)
        use = _call_use(node, self.parents)
        if use == "assigned" and self._held_index0(node):
            use = "index0"
        frame.calls.append(
            CallSite(
                caller=frame.qualname,
                callee=_callee_text(node.func),
                path=self.path,
                line=node.lineno,
                use=use,
                in_try=frame.try_depth > 0,
                keywords=keywords,
            )
        )
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Store):
            self._ref(node.lineno, node.id, "identifier", node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        text = dotted(node) or f"{_callee_text(node.value)}.{node.attr}"
        self._ref(node.lineno, node.attr, "attribute", text)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        value = node.value
        if isinstance(value, str) and len(value) <= _MAX_STRING_REF and _IDENT_RE.match(value):
            self._ref(node.lineno, value, "string", value)

    # --- output
    def result(self) -> PyFileFacts:
        functions: list[FuncFact] = []
        chains = list(self.chains)
        for draft in self.drafts:
            node = draft.node
            body = node.body
            doc = ast.get_docstring(node, clean=True)
            code = body[1:] if doc is not None else body
            mentioned = _mentioned(
                itertools.chain(ast.walk(node), _constant_names(node, self.constants))
            )
            tables = _dispatch_tables(node, self.constants)
            chains.extend(
                _expand_chain(c, tables, self.constants)
                for c in _chains(body, draft.qualname, mentioned)
            )
            functions.append(
                FuncFact(
                    qualname=draft.qualname,
                    path=self.path,
                    lineno=node.lineno,
                    end_lineno=node.end_lineno or node.lineno,
                    params=_params(node.args),
                    returns=_src(node.returns),
                    decorators=tuple(_decorator_text(d) for d in node.decorator_list),
                    body_hash=_hash_stmts(code),
                    cardinality=cardinality_of(node),
                    calls=tuple(draft.frame.calls),
                    raises=_raises(node),
                    is_nested=draft.is_nested,
                    docstring=doc,
                    class_qualname=draft.class_qualname,
                    is_async=isinstance(node, ast.AsyncFunctionDef),
                )
            )
        chains.sort(key=lambda c: (c.branches[0].line, c.func))
        return PyFileFacts(
            path=self.path,
            module=self.module,
            is_test=self.is_test,
            parse_error=None,
            imports=tuple(self.imports),
            functions=tuple(functions),
            classes=tuple(self.classes),
            bindings=tuple(self.bindings),
            refs=tuple(self.refs),
            isinstance_chains=tuple(chains),
            module_calls=tuple(self.module_frame.calls),
        )

    def hints(self) -> TypeHints:
        return TypeHints(tuple(self.local_types), tuple(self.field_types))
