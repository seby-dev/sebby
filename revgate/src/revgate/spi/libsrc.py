"""Class hierarchies of installed third-party packages, read from their source with `ast`.

Nothing here imports the library, so no dependency code runs during a review. Results are
cached in memory per `(site, package)` for the life of the process.
"""

from __future__ import annotations

import ast
import importlib.util
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from revgate.gitio import GitError, common_dir

_MAX_HOPS = 6


def _venv_site(root: Path) -> Path | None:
    for candidate in sorted((root / ".venv" / "lib").glob("python3*/site-packages")):
        if candidate.is_dir():
            return candidate
    return None


def find_site_packages(repo: Path) -> Path | None:
    """The repository's own `.venv` site-packages, else the main worktree's, else None."""
    site = _venv_site(repo)
    if site is not None:
        return site
    try:
        main = common_dir(repo).parent
    except GitError:
        return None
    return _venv_site(main) if main != repo else None


@dataclass(frozen=True)
class _Scan:
    hierarchy: tuple[tuple[str, tuple[str, ...]], ...]
    aliases: tuple[tuple[str, str], ...]  # "<module>.<local name>" -> imported target


def _module_files(site: Path, package: str) -> Iterator[tuple[str, Path]]:
    single = site / f"{package}.py"
    if single.is_file():
        yield package, single
    root = site / package
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(site).with_suffix("").parts
        parts = rel[:-1] if rel[-1] == "__init__" else rel
        yield ".".join(parts), path


def _top_level(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Module-level statements, including those inside top-level `if` and `try` blocks."""
    for stmt in body:
        yield stmt
        if isinstance(stmt, ast.If):
            yield from _top_level([*stmt.body, *stmt.orelse])
        elif isinstance(stmt, ast.Try | ast.TryStar):
            handlers = [s for h in stmt.handlers for s in h.body]
            yield from _top_level([*stmt.body, *handlers, *stmt.orelse, *stmt.finalbody])


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base is not None else None
    if isinstance(node, ast.Subscript):  # Generic[T] -> Generic
        return _dotted(node.value)
    return None


def _imports(stmt: ast.Import | ast.ImportFrom, module: str, is_pkg: bool) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(stmt, ast.Import):
        for alias in stmt.names:
            if alias.asname:
                out[alias.asname] = alias.name
            else:
                head = alias.name.split(".")[0]
                out[head] = head
        return out
    target = stmt.module or ""
    if stmt.level:
        base = module.split(".")
        keep = len(base) - stmt.level + (1 if is_pkg else 0)
        target = ".".join(base[: max(keep, 0)] + ([target] if target else []))
    for alias in stmt.names:
        if alias.name != "*":
            out[alias.asname or alias.name] = f"{target}.{alias.name}"
    return out


@lru_cache(maxsize=32)
def _scan(site: str, package: str) -> _Scan:
    classes: dict[str, dict[str, list[str]]] = {}  # module -> class name -> bases as written
    imports: dict[str, dict[str, str]] = {}
    for module, path in _module_files(Path(site), package):
        try:
            tree = ast.parse(importlib.util.decode_source(path.read_bytes()), filename=str(path))
        except (OSError, SyntaxError, ValueError, UnicodeDecodeError, RecursionError):
            continue  # one unreadable library file leaves the rest of the hierarchy usable
        mod_classes: dict[str, list[str]] = {}
        mod_imports: dict[str, str] = {}
        is_pkg = path.name == "__init__.py"
        for stmt in _top_level(tree.body):
            if isinstance(stmt, ast.ClassDef):
                mod_classes[stmt.name] = [d for b in stmt.bases if (d := _dotted(b))]
            elif isinstance(stmt, ast.Import | ast.ImportFrom):
                mod_imports.update(_imports(stmt, module, is_pkg))
        classes[module] = mod_classes
        imports[module] = mod_imports

    known = {f"{m}.{c}" for m, names in classes.items() for c in names}
    aliases = {
        f"{m}.{local}": target for m, table in imports.items() for local, target in table.items()
    }

    def qualify(module: str, text: str) -> str:
        head, _, rest = text.partition(".")
        if head in classes.get(module, {}):
            base = f"{module}.{head}"
        elif head in imports.get(module, {}):
            base = imports[module][head]
        else:
            return text
        return _canonical(f"{base}.{rest}" if rest else base, known, aliases)

    hierarchy = {
        f"{m}.{c}": tuple(qualify(m, b) for b in bases)
        for m, names in classes.items()
        for c, bases in names.items()
    }
    return _Scan(tuple(sorted(hierarchy.items())), tuple(sorted(aliases.items())))


def _canonical(dotted: str, known: set[str] | frozenset[str], aliases: Mapping[str, str]) -> str:
    """Follow re-exports (`pkg.Name` imported into `pkg/__init__.py`) to the defining module."""
    current = dotted
    for _ in range(_MAX_HOPS):
        if current in known:
            return current
        target = aliases.get(current)
        if target is None:
            module, _, name = current.rpartition(".")
            parent = aliases.get(module)
            if parent is None or not name:
                return current
            target = f"{parent}.{name}"
        current = target
    return current


def library_hierarchy(site: Path, package: str) -> dict[str, tuple[str, ...]]:
    """Class qualname to its direct bases, dotted and resolved through each module's imports."""
    return dict(_scan(str(site), package).hierarchy)


def canonical_class(site: Path, package: str, dotted: str) -> str:
    """`dotted` followed through the package's re-exports to the module that defines it."""
    scan = _scan(str(site), package)
    known = frozenset(name for name, _bases in scan.hierarchy)
    return _canonical(dotted, known, dict(scan.aliases))


def strict_subclasses(hier: Mapping[str, tuple[str, ...]], cls: str) -> tuple[str, ...]:
    """Every class that inherits from `cls`, directly or not, sorted; `cls` itself excluded."""
    children: dict[str, list[str]] = {}
    for name, bases in hier.items():
        for base in bases:
            children.setdefault(base, []).append(name)
    found: set[str] = set()
    todo = [cls]
    while todo:
        for child in children.get(todo.pop(), ()):
            if child not in found and child != cls:
                found.add(child)
                todo.append(child)
    return tuple(sorted(found))
