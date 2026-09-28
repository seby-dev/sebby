"""G11 policy ratchets, computed from a task's fork to its tip, never per commit.

Two measures live here. `ratchet_findings` runs the Stage 1a policy rules (protected
paths, suppressions, disabled, deleted, and weakened tests, and chased expected values),
counting assertions rather than lines. `raw_signal_counts` reproduces the r5 prototype's
looser counting exactly, so the benchmark can compare like with like; its definitions
stay private to it.
"""

from __future__ import annotations

import ast
import io
import os
import re
import tokenize
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from revgate import gitio
from revgate.config import Config, glob_match, is_test_path
from revgate.model import Finding, Grade, Source, new_finding

PROTECTED_GLOBS: tuple[str, ...] = (
    ".review.toml",
    "review-rules.yaml",
    ".github/workflows/**",
    "Makefile",
    "**/Makefile",
    "ruff.toml",
    ".ruff.toml",
    "mypy.ini",
    "setup.cfg",
    "tox.ini",
    ".pre-commit-config.yaml",
    "**/.eslintrc*",
    "**/eslint.config.*",
    "**/.prettierrc*",
    "**/vitest.config.*",
    "**/tsconfig*.json",
    "revgate/src/**",
)

RAW_SIGNALS: tuple[str, ...] = (
    "suppress_new",
    "suppress_no_reason",
    "test_disabled",
    "test_deleted",
    "test_weakened",
    "assert_edited",
    "config_touched",
)

_PY_EXT = (".py",)
_TS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")


@dataclass(frozen=True)
class RatchetResult:
    findings: tuple[Finding, ...]
    suppression_added: bool
    review_toml_edited: bool
    raw_counts: Mapping[str, int]


# --- reading the two trees -------------------------------------------------------------


class _Trees:
    """File text at the fork and at the tip, read through one `cat-file --batch` each."""

    def __init__(self, repo: Path, fork: str, tip: str) -> None:
        self._repo = repo
        self._fork = gitio.ls_tree(repo, fork)
        self._tip = gitio.ls_tree(repo, tip)
        self._blobs: dict[str, bytes] = {}

    def prefetch(self, fork_paths: Iterable[str], tip_paths: Iterable[str]) -> None:
        shas = [self._fork[p] for p in fork_paths if p in self._fork]
        shas += [self._tip[p] for p in tip_paths if p in self._tip]
        missing = [s for s in shas if s not in self._blobs]
        self._blobs.update(gitio.cat_blobs(self._repo, missing))

    def _text(self, tree: Mapping[str, str], path: str | None) -> str | None:
        if path is None or path not in tree:
            return None
        sha = tree[path]
        if sha not in self._blobs:
            self._blobs.update(gitio.cat_blobs(self._repo, [sha]))
        return self._blobs[sha].decode("utf-8", errors="replace")

    def fork(self, path: str | None) -> str | None:
        return self._text(self._fork, path)

    def tip(self, path: str | None) -> str | None:
        return self._text(self._tip, path)


def _added_lines(hunks: Iterable[tuple[int, int]]) -> set[int]:
    """With `-U0`, a hunk's head-side range holds exactly the added lines."""
    return {n for start, count in hunks for n in range(start, start + count)}


def _lines(text: str | None) -> list[str]:
    return text.splitlines() if text is not None else []


# --- test functions and assertions ---------------------------------------------------------


@dataclass(frozen=True)
class _TestInfo:
    name: str
    line: int
    count: int
    calls: frozenset[str]
    expectations: tuple[tuple[str, str] | None, ...]


_PY_IGNORED_CALLS = frozenset(
    {
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "float",
        "frozenset",
        "getattr",
        "int",
        "isinstance",
        "len",
        "list",
        "max",
        "min",
        "print",
        "range",
        "repr",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
        "type",
        "zip",
    }
)


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return f"{inner}.{node.attr}" if inner is not None else None
    return None


def _is_pytest_raises(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _dotted(node.func) == "pytest.raises"


def _is_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return isinstance(node.operand, ast.Constant)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_literal(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is not None and _is_literal(k) for k in node.keys) and all(
            _is_literal(v) for v in node.values
        )
    return False


def _py_expectation(node: ast.Assert) -> tuple[str, str] | None:
    """`(observed expression, expected literal)` for `assert <expr> <op> <literal>`."""
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return None
    left, right = test.left, test.comparators[0]
    if _is_literal(left) and not _is_literal(right):
        left, right = right, left
    if not _is_literal(right) or _is_literal(left):
        return None
    op = type(test.ops[0]).__name__
    return f"{ast.dump(left, annotate_fields=False)}|{op}", ast.unparse(right)


def _py_test_info(qualname: str, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> _TestInfo:
    count = 0
    calls: set[str] = set()
    asserts: list[ast.Assert] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            count += 1
            asserts.append(node)
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            if isinstance(node.func, ast.Attribute) and node.func.attr.startswith("assert"):
                count += 1
            elif name is not None and not name.startswith("pytest."):
                last = name.rsplit(".", 1)[-1]
                if last not in _PY_IGNORED_CALLS:
                    calls.add(last)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            count += sum(1 for item in node.items if _is_pytest_raises(item.context_expr))
    asserts.sort(key=lambda a: (a.lineno, a.col_offset))
    return _TestInfo(
        qualname,
        fn.lineno,
        count,
        frozenset(calls),
        tuple(_py_expectation(a) for a in asserts),
    )


def _py_tests(source: str | None) -> dict[str, _TestInfo] | None:
    """`test_*` functions at module level or in `Test*` classes; None when unparsable."""
    if source is None:
        return {}
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    out: dict[str, _TestInfo] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                out[node.name] = _py_test_info(node.name, node)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name.startswith("test_"):
                        qual = f"{node.name}.{item.name}"
                        out[qual] = _py_test_info(qual, item)
    return out


# `it.only(`, `it.each(...)(` and `xit(` still name a test, so a test that gains or loses
# a modifier keeps its identity.
_TS_TEST_RE = re.compile(r"""\b[xf]?(?:it|test)(?:\.\w+)*\(\s*(['"`])(.+?)\1""")
_TS_EXPECT_RE = re.compile(r"\bexpect\(")
_TS_EXPECTATION_RE = re.compile(r"expect\((.*)\)\.((?:not\.)?to\w*)\((.*)\)\s*;?\s*$")
_TS_CALL_RE = re.compile(r"(?<![\w$])(\.?)([A-Za-z_$][\w$]*)\s*\(")
_TS_IGNORED_CALLS = frozenset(
    {"expect", "it", "test", "describe", "if", "for", "while", "switch", "return", "function"}
)


def _ts_tests(source: str | None) -> dict[str, _TestInfo]:
    if source is None:
        return {}
    matches = list(_TS_TEST_RE.finditer(source))
    out: dict[str, _TestInfo] = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(source)
        body = source[match.end() : end]
        calls = {
            m.group(2)
            for m in _TS_CALL_RE.finditer(body)
            if m.group(2) not in _TS_IGNORED_CALLS
            and not (m.group(1) and m.group(2).startswith(("to", "not")))
        }
        expectations: list[tuple[str, str] | None] = []
        for line in body.splitlines():
            for _ in _TS_EXPECT_RE.finditer(line):
                em = _TS_EXPECTATION_RE.search(line)
                if em is None or _TS_EXPECT_RE.search(em.group(1)):
                    expectations.append(None)
                else:
                    left = f"{em.group(1).strip()}|{em.group(2)}"
                    expectations.append((left, em.group(3).strip()))
        name = match.group(2)
        n = 2
        while name in out:
            name = f"{match.group(2)}#{n}"
            n += 1
        out[name] = _TestInfo(
            name,
            source.count("\n", 0, match.start()) + 1,
            len(expectations),
            frozenset(calls),
            tuple(expectations),
        )
    return out


def count_assertions_py(source: str) -> dict[str, int]:
    """Test name to assertion count: `assert`s, `.assert*()` calls, `pytest.raises` blocks."""
    return {name: info.count for name, info in (_py_tests(source) or {}).items()}


def count_assertions_ts(source: str) -> dict[str, int]:
    """Test name to the number of `expect(` calls in its body."""
    return {name: info.count for name, info in _ts_tests(source).items()}


def _tests_for(path: str, source: str | None) -> dict[str, _TestInfo] | None:
    if path.endswith(_PY_EXT):
        return _py_tests(source)
    if path.endswith(_TS_EXT):
        return _ts_tests(source)
    return None


# --- the finding rules ----------------------------------------------------------------------


def _finding(
    rule: str,
    *,
    file: str,
    line: int,
    message: str,
    evidence: str,
    symbol: str | None = None,
    fix: str | None = None,
) -> Finding:
    return new_finding(
        rule,
        file=file,
        line=max(line, 1),
        message=message,
        evidence=evidence,
        grade=Grade.E1_EXACT,
        source=Source.POLICY,
        kind="gaming",
        symbol=symbol,
        fix=fix,
    )


def _owned(paths: Iterable[str | None], plan_owns: frozenset[str]) -> bool:
    return any(p is not None and p in plan_owns for p in paths)


def _names(text: str, token: str) -> bool:
    """Whether `text` names `token` as a whole word, not as part of a longer one."""
    return re.search(rf"(?<![\w.]){re.escape(token)}(?![\w])", text) is not None


def _short(text: str, cap: int = 60) -> str:
    return text if len(text) <= cap else text[: cap - 1] + "…"


_TOML_HEADER_RE = re.compile(r"^\s*\[")


def _tool_table_lines(text: str | None, line_numbers: Iterable[int]) -> list[int]:
    """The given lines that sit under a `[tool.*]` table header."""
    lines = _lines(text)
    header_of: list[str] = []
    header = ""
    for line in lines:
        if _TOML_HEADER_RE.match(line):
            header = line.strip().lstrip("[").lstrip()
        header_of.append(header)
    hits: list[int] = []
    for n in line_numbers:
        if 1 <= n <= len(lines) and header_of[n - 1].startswith("tool."):
            hits.append(n)
    return hits


def _changed_or_context(hunks: Iterable[tuple[int, int]]) -> list[int]:
    """Changed lines; a pure deletion (count 0) contributes the line before it."""
    out: list[int] = []
    for start, count in hunks:
        out.extend(range(start, start + count) if count else [max(start, 1)])
    return out


def _protected_findings(
    trees: _Trees,
    changes: list[tuple[str, str, str | None]],
    head_hunks: Mapping[str, tuple[tuple[int, int], ...]],
    base_hunks: Mapping[str, tuple[tuple[int, int], ...]],
    *,
    globs: tuple[str, ...],
    plan_owns: frozenset[str],
) -> list[Finding]:
    out: list[Finding] = []
    for letter, path, old in changes:
        paths = [p for p in (path, old) if p is not None]
        if _owned(paths, plan_owns):
            continue
        first_line = min((s for s, _ in head_hunks.get(path, ())), default=1)
        if any(glob_match(p, globs) for p in paths):
            out.append(
                _finding(
                    "policy.protected_path",
                    file=path,
                    line=first_line,
                    message=f"protected path {path} changed without a plan entry",
                    evidence=f"{letter} {path}"
                    + (f" (from {old})" if old else "")
                    + "; the plan's owns doesn't name it",
                    fix="name the path in the plan's owns, or revert the change",
                )
            )
            continue
        if not any(os.path.basename(p) == "pyproject.toml" for p in paths):
            continue
        tip_hits = _tool_table_lines(
            trees.tip(path) if letter != "D" else None,
            _changed_or_context(head_hunks.get(path, ())),
        )
        base_path = old or path
        base_hits = _tool_table_lines(
            trees.fork(base_path) if letter != "A" else None,
            _changed_or_context(base_hunks.get(base_path, ())),
        )
        if tip_hits or base_hits:
            line = tip_hits[0] if tip_hits else first_line
            out.append(
                _finding(
                    "policy.protected_path",
                    file=path,
                    line=line,
                    message=f"a [tool.*] table in {path} changed without a plan entry",
                    evidence=f"{path}: tool configuration changed; the plan's owns doesn't name it",
                    fix="name the path in the plan's owns, or revert the change",
                )
            )
    return out


_PY_SUPPRESSION_RE = re.compile(r"#\s*noqa\b|#\s*type:\s*ignore\b|#\s*pragma:\s*no\s*cover")
_TS_SUPPRESSION_RE = re.compile(r"eslint-disable|@ts-expect-error|@ts-ignore|\bas any\b")
# A reason after the marker: `-- why`, `— why`, a spaced `- why` (`# pragma: no cover -
# filesystem race`), or a second comment that isn't itself a marker (`# type:
# ignore[assignment]  # shadows tuple.index`). A bare error code is never a reason.
_REASON_RE = re.compile(r"(?:--|—|\s-\s|#(?!\s*(?:noqa\b|type:\s*ignore\b|pragma:)))\s*[A-Za-z]")
# TypeScript adds a trailing `// why` comment that isn't itself a directive.
_TS_REASON_RE = re.compile(r"(?:--|—|\s-\s|//(?!\s*(?:eslint-|@ts-)))\s*[A-Za-z]")
# After `@ts-expect-error` or `@ts-ignore`, the directive's own text is the reason, with or
# without a colon, when it holds a word (`TS2322` alone is a code, not a reason).
_TS_DIRECTIVE_REASON_RE = re.compile(r"^\s*:?\s*(?=[^\n]*[a-z]{2})\S")


def _suppression_re(path: str) -> re.Pattern[str] | None:
    if path.endswith(_PY_EXT):
        return _PY_SUPPRESSION_RE
    if path.endswith(_TS_EXT):
        return _TS_SUPPRESSION_RE
    return None


def _py_comment_columns(text: str | None) -> dict[int, int] | None:
    """Each line's comment start column, or None when the file doesn't tokenize (then
    every marker counts, as before)."""
    if text is None:
        return None
    out: dict[int, int] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                out[tok.start[0]] = tok.start[1]
    except (tokenize.TokenError, SyntaxError):
        return None
    return out


def _ts_char_classes(text: str) -> list[str]:
    """Each line of a TypeScript file as one class letter per character: `c` code, `m`
    comment (`//`, `/* */`, and JSDoc, across lines), `s` string or template literal. A
    lexical scan, not a parse: a regex literal holding a quote or `//` can misclassify the
    rest of its line."""
    out: list[str] = []
    state = "c"  # c code, l line comment, b block comment, or the open quote character
    for line in text.splitlines():
        classes: list[str] = []
        i = 0
        if state == "l":
            state = "c"
        while i < len(line):
            ch, nxt = line[i], line[i + 1 : i + 2]
            if state == "c":
                if ch == "/" and nxt == "/":
                    state = "l"
                    classes.extend("m" * (len(line) - i))
                    break
                if ch == "/" and nxt == "*":
                    state = "b"
                    classes.extend("mm")
                    i += 2
                    continue
                if ch in "'\"`":
                    state = ch
                    classes.append("s")
                else:
                    classes.append("c")
            elif state == "b":
                if ch == "*" and nxt == "/":
                    state = "c"
                    classes.extend("mm")
                    i += 2
                    continue
                classes.append("m")
            else:
                classes.append("s")
                if ch == "\\":
                    classes.append("s")
                    i += 2
                    continue
                if ch == state:
                    state = "c"
            i += 1
        if state in "'\"":
            state = "c"  # an unterminated one-line string ends with its line
        out.append("".join(classes[: len(line)]))
    return out


@dataclass(frozen=True)
class _Marker:
    kind: str  # the marker without spacing, hashes, or case, so spellings of one match
    text: str
    end: int  # column after the marker on its line


def _kind(marker: str) -> str:
    return re.sub(r"[\s#]", "", marker).lower()


def _file_markers(path: str, text: str | None) -> dict[int, list[_Marker]]:
    """The markers that suppress something, by line: a Python marker in a comment, `as
    any` in TypeScript code, and a TypeScript directive in a comment."""
    pattern = _suppression_re(path)
    if pattern is None or text is None:
        return {}
    lines = _lines(text)
    out: dict[int, list[_Marker]] = {}
    if path.endswith(_PY_EXT):
        columns = _py_comment_columns(text)
        for n, line in enumerate(lines, start=1):
            col = columns.get(n) if columns is not None else None
            found = [
                m
                for m in pattern.finditer(line)
                if columns is None or (col is not None and m.start() >= col)
            ]
            if found:
                out[n] = [_Marker(_kind(m.group(0)), m.group(0), m.end()) for m in found]
        return out
    classes = _ts_char_classes(text)
    for n, line in enumerate(lines, start=1):
        cls = classes[n - 1] if n - 1 < len(classes) else ""
        found = []
        for m in pattern.finditer(line):
            where = cls[m.start()] if m.start() < len(cls) else "c"
            wanted = "c" if m.group(0).startswith("as") else "m"
            if where == wanted:
                found.append(m)
        if found:
            out[n] = [_Marker(_kind(m.group(0)), m.group(0), m.end()) for m in found]
    return out


def _has_reason(path: str, marker: _Marker, line: str) -> bool:
    rest = line[marker.end :]
    if path.endswith(_PY_EXT):
        return _REASON_RE.search(rest) is not None
    if marker.kind.startswith("@ts-") and _TS_DIRECTIVE_REASON_RE.search(rest):
        return True
    return _TS_REASON_RE.search(rest) is not None


def _suppression_findings(
    trees: _Trees,
    changes: list[tuple[str, str, str | None]],
    head_hunks: Mapping[str, tuple[tuple[int, int], ...]],
) -> tuple[list[Finding], bool]:
    """A marker is added when its file gains one of its kind: the fork's markers that the
    tip's untouched lines no longer hold were edited or moved, and they cover the same
    number of markers on added lines, in line order. So editing a line that keeps its
    marker adds nothing, and a new marker beside an untouched one does."""
    out: list[Finding] = []
    added_any = False
    for letter, path, old in changes:
        if _suppression_re(path) is None or letter == "D" or path not in head_hunks:
            continue
        added = _added_lines(head_hunks[path])
        fork = _file_markers(path, trees.fork(old or path) if letter != "A" else None)
        tip_text = trees.tip(path)
        tip = _file_markers(path, tip_text)
        tip_lines = _lines(tip_text)
        budget = Counter(m.kind for ms in fork.values() for m in ms)
        budget.subtract(m.kind for n, ms in tip.items() if n not in added for m in ms)
        for n in sorted(n for n in tip if n in added):
            new: list[_Marker] = []
            for m in tip[n]:
                if budget[m.kind] > 0:
                    budget[m.kind] -= 1
                else:
                    new.append(m)
            if not new:
                continue
            added_any = True
            line = tip_lines[n - 1]
            unjustified = [m for m in new if not _has_reason(path, m, line)]
            if unjustified:
                marker = unjustified[0].text
                out.append(
                    _finding(
                        "policy.unjustified_suppression",
                        file=path,
                        line=n,
                        message=f"suppression `{marker}` added without a reason",
                        evidence=f"added `{_short(line.strip(), 150)}`",
                        fix=f"add `-- <reason>` after `{marker}`, or remove it",
                    )
                )
    return out, added_any


def _parents(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _enclosing(node: ast.AST, parents: Mapping[int, ast.AST]) -> list[ast.AST]:
    chain: list[ast.AST] = []
    current = parents.get(id(node))
    while current is not None:
        chain.append(current)
        current = parents.get(id(current))
    return chain


def _symbol_of(chain: list[ast.AST]) -> str | None:
    names = [n.name for n in chain if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    classes = [n.name for n in chain if isinstance(n, ast.ClassDef)]
    if not names:
        return None
    return f"{classes[0]}.{names[0]}" if classes else names[0]


def _py_disabling(node: ast.AST, parents: Mapping[int, ast.AST]) -> bool:
    """An unconditional skip, xfail, or skip call; environment-conditional ones pass."""
    if isinstance(node, ast.Call):
        name = _dotted(node.func)
        if name in ("pytest.mark.skip", "unittest.skip"):
            return True
        if name == "pytest.mark.xfail":
            return not node.args and all(k.arg != "condition" for k in node.keywords)
        if name == "pytest.skip":
            chain = _enclosing(node, parents)
            return not any(isinstance(n, (ast.If, ast.IfExp, ast.ExceptHandler)) for n in chain)
        return False
    if isinstance(node, ast.Attribute) and _dotted(node) in (
        "pytest.mark.skip",
        "pytest.mark.xfail",
    ):
        parent = parents.get(id(node))
        return not (isinstance(parent, ast.Call) and parent.func is node)
    return False


_PY_DISABLE_FALLBACK_RE = re.compile(
    r"@pytest\.mark\.skip\b(?!if)|@unittest\.skip\(|^\s*pytest\.skip\("
    r"|@pytest\.mark\.xfail\s*(?:\(\s*(?:reason\s*=[^)]*)?\))?\s*$"
)
_TS_DISABLE_RE = re.compile(r"\b(?:it|test|describe)\.(?:skip|only|todo)\(|\b[xf](?:it|describe)\(")


def _disabled_findings(
    trees: _Trees,
    changes: list[tuple[str, str, str | None]],
    head_hunks: Mapping[str, tuple[tuple[int, int], ...]],
    *,
    cfg: Config,
    plan_owns: frozenset[str],
) -> list[Finding]:
    out: list[Finding] = []
    for letter, path, old in changes:
        if letter == "D" or path not in head_hunks or not is_test_path(path, cfg):
            continue
        added = _added_lines(head_hunks[path])
        source = trees.tip(path) or ""
        lines = source.splitlines()
        rule = (
            "policy.test_disabled.owned"
            if _owned((path, old), plan_owns)
            else "policy.test_disabled.unowned"
        )
        hits: list[tuple[int, str | None]] = []
        if path.endswith(_PY_EXT):
            try:
                tree = ast.parse(source)
            except (SyntaxError, ValueError):
                hits = [
                    (n, None)
                    for n in sorted(added)
                    if n <= len(lines) and _PY_DISABLE_FALLBACK_RE.search(lines[n - 1])
                ]
            else:
                parents = _parents(tree)
                seen: set[int] = set()
                for node in ast.walk(tree):
                    start = getattr(node, "lineno", None)
                    if start is None or not _py_disabling(node, parents):
                        continue
                    end = getattr(node, "end_lineno", None) or start
                    if start in seen or not added.intersection(range(start, end + 1)):
                        continue
                    seen.add(start)
                    hits.append((start, _symbol_of(_enclosing(node, parents))))
        elif path.endswith(_TS_EXT):
            hits = [
                (n, None)
                for n in sorted(added)
                if n <= len(lines) and _TS_DISABLE_RE.search(lines[n - 1])
            ]
        for n, symbol in sorted(hits, key=lambda h: h[0]):
            text = lines[n - 1].strip() if n <= len(lines) else ""
            out.append(
                _finding(
                    rule,
                    file=path,
                    line=n,
                    symbol=symbol,
                    message="a test is disabled unconditionally",
                    evidence=f"added `{_short(text, 150)}`",
                    fix="make the skip environment-conditional, or restore the test",
                )
            )
    return out


def _test_findings(
    trees: _Trees,
    changes: list[tuple[str, str, str | None]],
    *,
    cfg: Config,
    plan_owns: frozenset[str],
    plan_text: str,
) -> list[Finding]:
    pairs: list[tuple[str | None, str | None]] = []
    for letter, path, old in changes:
        fork_path = None if letter == "A" else (old or path)
        tip_path = None if letter == "D" else path
        if any(p is not None and is_test_path(p, cfg) for p in (fork_path, tip_path)):
            pairs.append((fork_path, tip_path))

    parsed: list[tuple[str | None, str | None, dict[str, _TestInfo], dict[str, _TestInfo]]] = []
    new_tests: list[_TestInfo] = []
    for fork_path, tip_path in pairs:
        kind_path = tip_path or fork_path
        assert kind_path is not None
        before = _tests_for(kind_path, trees.fork(fork_path))
        after = _tests_for(kind_path, trees.tip(tip_path))
        if before is None or after is None:
            continue  # not a test language, or a side that doesn't parse
        parsed.append((fork_path, tip_path, before, after))
        new_tests.extend(info for name, info in after.items() if name not in before)

    out: list[Finding] = []
    for fork_path, tip_path, before, after in parsed:
        owned = _owned((fork_path, tip_path), plan_owns)
        suffix = "owned" if owned else "unowned"
        for name, prior in before.items():
            bare = name.rsplit(".", 1)[-1]
            current = after.get(name)
            if current is None:
                if _names(plan_text, bare) or any(
                    prior.calls <= t.calls and t.count >= prior.count for t in new_tests
                ):
                    continue
                file = fork_path if tip_path is None else tip_path
                assert file is not None
                out.append(
                    _finding(
                        f"policy.test_deleted.{suffix}",
                        file=file,
                        line=prior.line if tip_path is None else 1,
                        symbol=name,
                        message=f"pre-existing test {name} deleted with no replacement",
                        evidence=f"{name} ({prior.count} assertions) is gone and no new test "
                        "covers the same calls with as many assertions",
                        fix="restore the test, or name its deletion in the plan",
                    )
                )
            elif current.count < prior.count:
                assert tip_path is not None
                out.append(
                    _finding(
                        f"policy.test_weakened.{suffix}",
                        file=tip_path,
                        line=current.line,
                        symbol=name,
                        message=f"test {name} lost assertions",
                        evidence=f"{name}: {prior.count} assertions at the fork, "
                        f"{current.count} at the tip",
                        fix="restore the assertions",
                    )
                )
            elif (
                current.count == prior.count
                and len(current.expectations) == len(prior.expectations)
                and not _names(plan_text, bare)
            ):
                assert tip_path is not None
                for was, now in zip(prior.expectations, current.expectations, strict=True):
                    if was is None or now is None or was[0] != now[0] or was[1] == now[1]:
                        continue
                    if _names(plan_text, now[1]):
                        continue
                    out.append(
                        _finding(
                            "policy.expected_value_chased",
                            file=tip_path,
                            line=current.line,
                            symbol=name,
                            message=f"expected value in {name} changed; the plan names neither",
                            evidence=f"{name}: expected «{_short(was[1])}» is now "
                            f"«{_short(now[1])}»; the plan names neither the test nor the value",
                            fix="name the test or its new value in the plan, or restore it",
                        )
                    )
    return out


def ratchet_findings(
    repo: Path,
    fork: str,
    tip: str,
    *,
    cfg: Config,
    plan_owns: frozenset[str],
    plan_text: str,
    plan_path: str | None,
) -> RatchetResult:
    """Every G11 finding from `fork` to `tip`; tiers come later, from routing."""
    changes = gitio.diff_name_status(repo, fork, tip)
    head_hunks = gitio.diff_hunks(repo, fork, tip)
    base_hunks = gitio.diff_hunks(repo, tip, fork)
    trees = _Trees(repo, fork, tip)
    trees.prefetch((old or path for _, path, old in changes), (path for _, path, _old in changes))
    globs = PROTECTED_GLOBS + tuple(cfg.protected_extra)
    if plan_path is not None:
        globs += (plan_path,)

    findings = _protected_findings(
        trees, changes, head_hunks, base_hunks, globs=globs, plan_owns=plan_owns
    )
    suppressions, suppression_added = _suppression_findings(trees, changes, head_hunks)
    findings += suppressions
    findings += _disabled_findings(trees, changes, head_hunks, cfg=cfg, plan_owns=plan_owns)
    findings += _test_findings(trees, changes, cfg=cfg, plan_owns=plan_owns, plan_text=plan_text)
    findings.sort(key=lambda f: (f.file, f.line, f.rule, f.id))
    review_toml_edited = any(".review.toml" in (path, old) for _, path, old in changes)
    return RatchetResult(
        findings=tuple(findings),
        suppression_added=suppression_added,
        review_toml_edited=review_toml_edited,
        raw_counts=raw_signal_counts(repo, fork, tip),
    )


# --- the r5 prototype's counting, for benchmark parity ----------------------------------------


def raw_signal_counts(repo: Path, fork: str, tip: str) -> dict[str, int]:
    """r5's gaming signals, counted exactly as `gaming_signals.py` counts them.

    Looser than the finding rules on purpose (`skipif` counts as disabling, `-` counts as
    a reason, and `cast(Any, ...)` counts as a suppression), so T28 compares like with like.
    """
    supp = re.compile(
        r"#\s*noqa|#\s*type:\s*ignore|pragma:\s*no\s*cover|eslint-disable|@ts-ignore"
        r"|@ts-expect-error|\bas any\b|cast\(\s*Any"
    )
    supp_reason = re.compile(
        r"(#\s*noqa(:\s*[A-Z0-9, ]+)?|#\s*type:\s*ignore(\[[^\]]*\])?"
        r"|eslint-disable[-\w]*(\s+[\w/@-]+)?|@ts-expect-error|@ts-ignore)"
        r"\s*(--|—|-|:|because|\()\s*\S+",
        re.I,
    )
    disable = re.compile(
        r"pytest\.mark\.(skip|xfail)|pytest\.skip\(|\b(it|test|describe)\.(skip|only|todo)\("
        r"|\bxit\(|\bxdescribe\("
    )
    config = re.compile(
        r"(^pyproject\.toml$|tsconfig.*\.json$|eslint|vitest\.config|vite\.config|^Makefile$"
        r"|^\.github/|conftest\.py$|setup\.cfg$|\.pre-commit|ruff\.toml|mypy\.ini)"
    )
    is_test = re.compile(r"(^tests/.*\.py$|\.test\.tsx?$)")

    gitio.rev_parse(repo, fork)
    gitio.rev_parse(repo, tip)
    diff = gitio.run_git(
        repo,
        "diff",
        "-U0",
        "--no-renames",
        "--no-color",
        "--no-ext-diff",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        fork,
        tip,
    ).decode("utf-8", errors="replace")
    current: str | None = None
    added: dict[str, list[str]] = {}
    for ln in diff.splitlines():
        if ln.startswith("+++ "):
            current = ln[6:] if ln.startswith("+++ b/") else None
        elif current and ln.startswith("+") and not ln.startswith("+++"):
            added.setdefault(current, []).append(ln[1:])

    sig: Counter[str] = Counter()
    for path, lines in added.items():
        for line in lines:
            if supp.search(line) and not is_test.search(path):
                sig["suppress_new"] += 1
                if not supp_reason.search(line):
                    sig["suppress_no_reason"] += 1
            if disable.search(line):
                sig["test_disabled"] += 1

    names = gitio.run_git(repo, "diff", "--name-only", "-z", "--no-ext-diff", fork, tip)
    files = [os.fsdecode(p) for p in names.split(b"\0") if p]
    trees = _Trees(repo, fork, tip)
    for path in files:
        if config.search(path):
            sig["config_touched"] += 1
        if is_test.search(path):
            parse = _r5_py_tests if path.endswith(".py") else _r5_ts_tests
            before, after = parse(trees.fork(path)), parse(trees.tip(path))
            for name, (na, sa) in before.items():
                if name not in after:
                    sig["test_deleted"] += 1
                else:
                    nb, sb = after[name]
                    if nb < na:
                        sig["test_weakened"] += 1
                    elif sa != sb and na == nb and sa:
                        sig["assert_edited"] += 1
    return {key: sig[key] for key in RAW_SIGNALS}


_R5_CALL_ATTRS = frozenset(
    {"raises", "assert_called_with", "assert_called_once_with", "assert_not_called", "warns"}
)


def _r5_py_tests(src: str | None) -> dict[str, tuple[int, str]]:
    if not src:
        return {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    out: dict[str, tuple[int, str]] = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test"):
            asserts = sum(isinstance(x, ast.Assert) for x in ast.walk(n)) + sum(
                1
                for x in ast.walk(n)
                if isinstance(x, ast.Call)
                and isinstance(x.func, ast.Attribute)
                and x.func.attr in _R5_CALL_ATTRS
            )
            asrc = "\n".join(ast.unparse(x) for x in ast.walk(n) if isinstance(x, ast.Assert))
            out[n.name] = (asserts, asrc)
    return out


def _r5_ts_tests(src: str | None) -> dict[str, tuple[int, str]]:
    if not src:
        return {}
    out: dict[str, tuple[int, str]] = {}
    blocks = re.split(r"\n\s*(?:it|test)\(\s*['\"`]", src)
    for block in blocks[1:]:
        name = re.split(r"['\"`]", block, maxsplit=1)[0]
        body = block[:4000]
        nxt = re.search(r"\n\s*(?:it|test|describe)\(", body)
        body = body[: nxt.start()] if nxt else body
        exp = re.findall(r"expect\([^\n]*", body)
        out[name] = (len(exp), "\n".join(exp))
    return out
