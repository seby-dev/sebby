"""`TreeIndex`: every file's facts at one revision, linked across the tree.

`TreeIndex.build` reads the tree by revision (nothing is checked out), indexes each Python
file through the blob cache, sends TypeScript files to an optional indexer, and links call
sites: import aliases, re-export chains up to six hops, `self` and `cls` through the
method resolution order, and receivers typed by annotations, constructor assignments,
loops over annotated sequences, and annotated class fields. Name matching over project
methods is the fallback only when a receiver can't be typed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TypeGuard

from revgate.config import Config, is_prod_path, is_test_path
from revgate.gitio import cat_blobs, ls_tree
from revgate.spi import libsrc
from revgate.spi.cache import BlobCache
from revgate.spi.facts import (
    BindingFact,
    CallSite,
    ClassFact,
    FuncFact,
    PyFileFacts,
    Ref,
    TsFileFacts,
)
from revgate.spi.py_facts import (
    TypeHints,
    ann_type,
    index_python_file_with_types,
    module_name_for,
)

TsIndexerFn = Callable[[Mapping[str, str]], Mapping[str, TsFileFacts]]

_MAX_HOPS = 6
# Name matching over project methods, for an untyped receiver, gives up above this many
# candidates: a name that common says nothing about which method runs.
_NAME_FALLBACK_MAX = 8
_TS_SUFFIXES = (".ts", ".tsx")


def _git_blob_id(data: bytes) -> str:
    """The id git would give `data`, so an override equal to a committed file shares its
    cache entries."""
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data, usedforsecurity=False).hexdigest()


def _cache_key(blob: str, path: str, is_test: bool) -> str:
    """Facts depend on the path (module name, test flag), not only on the content."""
    text = f"{blob}\0{path}\0{int(is_test)}".encode()
    return hashlib.sha1(text, usedforsecurity=False).hexdigest()


def _is_ts(path: str) -> bool:
    return path.endswith(_TS_SUFFIXES) and "node_modules" not in path.split("/")


def _decode_python(
    path: str, data: bytes, module: str, is_test: bool
) -> tuple[PyFileFacts, TypeHints]:
    try:
        source = importlib.util.decode_source(data)
    except (SyntaxError, UnicodeDecodeError, LookupError) as exc:
        error = f"{type(exc).__name__}: {exc}"
        return PyFileFacts(path, module, is_test, error, (), (), (), (), (), (), ()), TypeHints()
    return index_python_file_with_types(path, source, module=module, is_test=is_test)


class TreeIndex:
    """Facts for every file at `rev`, with call sites resolved across the tree."""

    def __init__(
        self,
        repo: Path,
        rev: str,
        cfg: Config,
        py: Mapping[str, PyFileFacts],
        ts: Mapping[str, TsFileFacts],
        hints: Mapping[str, TypeHints],
        notes: Iterable[str],
    ) -> None:
        self.repo = repo
        self.rev = rev
        self.cfg = cfg
        self.ts: Mapping[str, TsFileFacts] = ts
        self._notes: set[str] = set(notes)
        self._by_module: dict[str, PyFileFacts] = {}
        for path in sorted(py):
            self._by_module.setdefault(py[path].module, py[path])
        self._index_definitions(py)
        self._index_scopes(py, hints)
        self.py: Mapping[str, PyFileFacts] = self._link(py)
        self._refs_by_name: dict[str, list[Ref]] | None = None

    # --- construction ---------------------------------------------------------------------

    @classmethod
    def build(
        cls,
        repo: Path,
        rev: str,
        cache: BlobCache,
        cfg: Config,
        *,
        ts_indexer: TsIndexerFn | None = None,
        overrides: Mapping[str, bytes | None] | None = None,
    ) -> TreeIndex:
        blobs = ls_tree(repo, rev)
        content: dict[str, bytes] = {}
        for path, data in (overrides or {}).items():
            if data is None:
                blobs.pop(path, None)
            else:
                sha = _git_blob_id(data)
                blobs[path] = sha
                content[sha] = data
        notes: list[str] = []

        py_paths = sorted(p for p in blobs if p.endswith(".py"))
        ts_paths = sorted(p for p in blobs if _is_ts(p))
        py_done: dict[str, tuple[PyFileFacts, TypeHints]] = {}
        py_todo: list[tuple[str, str, bool]] = []  # (path, key, is_test)
        for path in py_paths:
            is_test = is_test_path(path, cfg)
            key = _cache_key(blobs[path], path, is_test)
            hit = cache.get(key, "py")
            if _is_py_entry(hit):
                py_done[path] = hit
            else:
                py_todo.append((path, key, is_test))

        ts_done: dict[str, TsFileFacts] = {}
        ts_todo: list[tuple[str, str]] = []  # (path, key)
        if ts_paths and ts_indexer is None:
            notes.append("ts-unavailable")
        elif ts_paths:
            for path in ts_paths:
                key = _cache_key(blobs[path], path, is_test_path(path, cfg))
                hit = cache.get(key, "ts")
                if isinstance(hit, TsFileFacts) and hit.path == path:
                    ts_done[path] = hit
                else:
                    ts_todo.append((path, key))

        wanted = [blobs[p] for p, _k, _t in py_todo] + [blobs[p] for p, _k in ts_todo]
        read = cat_blobs(repo, [s for s in wanted if s not in content])
        read.update(content)

        for path, key, is_test in py_todo:
            entry = _decode_python(path, read[blobs[path]], module_name_for(path), is_test)
            cache.put(key, "py", entry)
            py_done[path] = entry

        if ts_todo and ts_indexer is not None:
            sources = {p: read[blobs[p]].decode("utf-8", errors="replace") for p, _k in ts_todo}
            produced = ts_indexer(sources)
            for path, key in ts_todo:
                facts = produced.get(path)
                if facts is not None:
                    cache.put(key, "ts", facts)
                    ts_done[path] = facts

        py = {p: entry[0] for p, entry in py_done.items()}
        hints = {p: entry[1] for p, entry in py_done.items()}
        notes.extend(f"parse-error:{p}" for p in sorted(py) if py[p].parse_error is not None)
        return cls(repo, rev, cfg, py, dict(sorted(ts_done.items())), hints, notes)

    def _index_definitions(self, py: Mapping[str, PyFileFacts]) -> None:
        functions: dict[str, FuncFact] = {}
        classes: dict[str, ClassFact] = {}
        bindings: dict[str, BindingFact] = {}
        for path in sorted(py):
            facts = py[path]
            for fn in facts.functions:
                functions.setdefault(fn.qualname, fn)
            for cl in facts.classes:
                classes.setdefault(cl.qualname, cl)
            own = {b.qualname: b for b in facts.bindings}  # the last assignment in a file wins
            for qualname, binding in own.items():
                bindings.setdefault(qualname, binding)
        self.functions: Mapping[str, FuncFact] = functions
        self.classes: Mapping[str, ClassFact] = classes
        self.bindings: Mapping[str, BindingFact] = bindings
        methods: dict[str, list[str]] = {}
        for qualname, fn in functions.items():
            if fn.class_qualname is not None:
                methods.setdefault(qualname.rsplit(".", 1)[-1], []).append(qualname)
        self._methods_by_name = {name: sorted(qs) for name, qs in methods.items()}

    def _index_scopes(self, py: Mapping[str, PyFileFacts], hints: Mapping[str, TypeHints]) -> None:
        self._aliases: dict[str, dict[str, str]] = {}
        self._stars: dict[str, list[str]] = {}
        self._defs: dict[str, dict[str, str]] = {}
        packages = {m.rsplit(".", i)[0] for m in self._by_module for i in range(m.count(".") + 1)}
        tops = {m.split(".", 1)[0] for m in self._by_module}
        for module, facts in self._by_module.items():
            aliases: dict[str, str] = {}
            here = module if facts.path.endswith("__init__.py") else module.rpartition(".")[0]
            for imp in facts.imports:
                target = _sibling(imp.module, here, packages, tops)
                if imp.name == "*":
                    self._stars.setdefault(module, []).append(target)
                elif imp.name is not None:
                    aliases[imp.alias] = f"{target}.{imp.name}"
                elif imp.alias != imp.module.split(".")[0]:
                    aliases[imp.alias] = target  # import a.b as x
                else:
                    head = imp.module.split(".")[0]  # import a.b binds a
                    aliases[imp.alias] = _sibling(head, here, packages, tops)
            self._aliases[module] = aliases
            defs: dict[str, str] = {}
            prefix = f"{module}."
            for q in [
                *(f.qualname for f in facts.functions),
                *(c.qualname for c in facts.classes),
                *(b.qualname for b in facts.bindings),
            ]:
                local = q[len(prefix) :] if q.startswith(prefix) else ""
                if local and "." not in local:
                    defs[local] = q
            self._defs[module] = defs
        self._local_types: dict[str, dict[str, str]] = {}
        self._field_types: dict[str, dict[str, str]] = {}
        for path in sorted(hints):
            if self._by_module.get(py[path].module) is not py[path]:
                continue  # a shadowed duplicate module
            for func, var, text in hints[path].local_types:
                self._local_types.setdefault(func, {})[var] = text
            for cls_q, name, ann in hints[path].field_types:
                self._field_types.setdefault(cls_q, {})[name] = ann
        self._module_of_def: dict[str, str] = {}
        for module, facts in self._by_module.items():
            for fn in facts.functions:
                self._module_of_def[fn.qualname] = module
            for cl in facts.classes:
                self._module_of_def[cl.qualname] = module

    def _link(self, py: Mapping[str, PyFileFacts]) -> dict[str, PyFileFacts]:
        """Resolve class bases first (the MRO needs them), then every call site."""
        linked_classes: dict[str, dict[str, ClassFact]] = {}  # path -> qualname -> class
        classes = dict(self.classes)
        for path in sorted(py):
            facts = py[path]
            per_file: dict[str, ClassFact] = {}
            for cl in facts.classes:
                new_cl = dataclasses.replace(cl, resolved_bases=self._resolve_bases(facts, cl))
                per_file[cl.qualname] = new_cl
                if self.classes.get(cl.qualname) is cl:
                    classes[cl.qualname] = new_cl
            linked_classes[path] = per_file
        self.classes = classes

        callers: dict[str, list[CallSite]] = {}
        functions = dict(self.functions)
        linked: dict[str, PyFileFacts] = {}
        for path in sorted(py):
            facts = py[path]
            if facts.parse_error is not None:
                linked[path] = facts
                continue
            new_funcs: list[FuncFact] = []
            for fn in facts.functions:
                calls = tuple(self._link_call(facts.module, c, callers) for c in fn.calls)
                new_fn = dataclasses.replace(fn, calls=calls)
                new_funcs.append(new_fn)
                if self.functions.get(fn.qualname) is fn:
                    functions[fn.qualname] = new_fn
            module_calls = tuple(
                self._link_call(facts.module, c, callers) for c in facts.module_calls
            )
            new_classes = tuple(linked_classes[path].get(c.qualname, c) for c in facts.classes)
            linked[path] = dataclasses.replace(
                facts, functions=tuple(new_funcs), classes=new_classes, module_calls=module_calls
            )
        self.functions = functions
        self._callers = {
            q: sorted(sites, key=lambda s: (s.path, s.line, s.caller))
            for q, sites in callers.items()
        }
        return linked

    def _link_call(
        self, module: str, site: CallSite, callers: dict[str, list[CallSite]]
    ) -> CallSite:
        resolved = self._resolve_callee(module, site.caller, site.callee)
        if not resolved:
            return site
        linked = dataclasses.replace(site, resolved=resolved)
        for target in resolved:
            callers.setdefault(target, []).append(linked)
        return linked

    # --- resolution ----------------------------------------------------------------------

    def _symbol(self, dotted: str, hops: int = _MAX_HOPS) -> str | None:
        """The project definition or module `dotted` names, following re-exports."""
        current = dotted
        while hops > 0:
            hops -= 1
            if (
                current in self.functions
                or current in self.classes
                or current in self.bindings
                or current in self._by_module
            ):
                return current
            owner, _, name = current.rpartition(".")
            if not owner:
                return None
            if owner in self.classes:
                return self._method(owner, name)
            aliases = self._aliases.get(owner)
            if aliases is None:
                parent = self._symbol(owner, hops)
                if parent is None or parent == owner:
                    return None
                current = f"{parent}.{name}"
                continue
            if name in aliases:
                current = aliases[name]
                continue
            for star in self._stars.get(owner, ()):
                found = self._symbol(f"{star}.{name}", hops)
                if found is not None:
                    return found
            return None
        return None

    def _method(self, class_qualname: str, name: str) -> str | None:
        for cls_q in self.mro(class_qualname):
            candidate = f"{cls_q}.{name}"
            if candidate in self.functions:
                return candidate
        return None

    def _scope_lookup(self, module: str, caller: str | None, head: str) -> str | None:
        """What `head` names inside `caller` (enclosing functions first), then the module."""
        scope = caller
        while scope is not None and scope in self.functions:
            candidate = f"{scope}.{head}"
            if candidate in self.functions or candidate in self.classes:
                return candidate
            scope = scope.rpartition(".")[0] or None
        defs = self._defs.get(module, {})
        if head in defs:
            return defs[head]
        return self._aliases.get(module, {}).get(head)

    def _resolve_bases(self, facts: PyFileFacts, cls: ClassFact) -> tuple[str, ...]:
        out: list[str] = []
        for base in cls.bases:
            target = self._resolve_text(facts.module, None, base)
            out.append(target if target is not None else base)
        return tuple(out)

    def _resolve_text(self, module: str, caller: str | None, text: str) -> str | None:
        """A dotted expression to a project definition, or to an external dotted name."""
        head, _, rest = text.partition(".")
        base = self._scope_lookup(module, caller, head)
        if base is None:
            return None
        full = f"{base}.{rest}" if rest else base
        found = self._symbol(full)
        if found is not None:
            return found
        if self._symbol(base) is not None:
            return None  # a project module or class without that member
        return full

    def _class_named(self, module: str, caller: str | None, text: str) -> str | None:
        target = self._resolve_text(module, caller, text)
        return target if target is not None and target in self.classes else None

    def _local_type_text(self, caller: str | None, var: str) -> tuple[str, str] | None:
        """`(scope, type text)` for `var` in `caller` or an enclosing function."""
        scope = caller
        while scope is not None and scope in self.functions:
            text = self._local_types.get(scope, {}).get(var)
            if text is not None:
                return scope, text
            scope = scope.rpartition(".")[0] or None
        return None

    def _instance_type(
        self, module: str, caller: str | None, var: str, depth: int = 0
    ) -> str | None:
        """The class a local variable holds: a project class qualname, or an external dotted
        name (`pathlib.Path`) when the source types it with a class from outside the tree."""
        found = self._local_type_text(caller, var) if depth <= _MAX_HOPS else None
        if found is None:
            return None
        scope, text = found
        if text.startswith("="):
            return text[1:] if text[1:] in self.classes else None
        if text.startswith("["):
            return None  # a sequence, not an instance
        if text.startswith("@"):
            return self._element_type(module, scope, text[1:], depth + 1)
        target = self._resolve_text(module, scope, text)
        if target is None:
            return None
        if target in self.classes or self._symbol(target) is None:
            return target  # a project class, or an external one
        return None  # a project function or binding: its result's type is unknown

    def _element_type(self, module: str, caller: str, iterable: str, depth: int) -> str | None:
        """The project class of one element of `iterable`: a local annotated as a sequence,
        or an attribute chain ending in a field annotated as one."""
        head, _, rest = iterable.partition(".")
        if not rest:
            found = self._local_type_text(caller, head)
            if found is None or not found[1].startswith("["):
                return None
            return self._class_named(module, found[0], found[1][1:-1])
        owner = self._instance_type(module, caller, head, depth)
        parts = rest.split(".")
        for part in parts[:-1]:
            owner = self._field_class(owner, part, sequence=False) if owner else None
        return self._field_class(owner, parts[-1], sequence=True) if owner else None

    def _field_class(self, class_qualname: str, name: str, *, sequence: bool) -> str | None:
        """The project class a field holds (`sequence=False`) or holds a sequence of."""
        for cls_q in self.mro(class_qualname):
            ann = self._field_types.get(cls_q, {}).get(name)
            if ann is None:
                continue
            text = ann_type(ann)
            module = self._module_of_def.get(cls_q)
            if text is None or module is None or text.startswith("[") != sequence:
                return None
            return self._class_named(module, None, text[1:-1] if sequence else text)
        return None

    def _resolve_callee(self, module: str, caller: str, callee: str) -> tuple[str, ...]:
        parts = callee.split(".")
        fn_caller = caller if caller in self.functions else None
        if parts[0] == "super()" and len(parts) == 2:
            cls_owner = self._owner_class(caller) if fn_caller is not None else None
            for cls_q in self.mro(cls_owner)[1:] if cls_owner is not None else ():
                if f"{cls_q}.{parts[1]}" in self.functions:
                    return (f"{cls_q}.{parts[1]}",)
            return ()
        if any(not p or p.endswith("()") for p in parts):
            return ()  # a call on a call's result: the receiver isn't typed
        head, rest = parts[0], parts[1:]
        instance = self._instance_type(module, fn_caller, head) if rest else None
        if instance is not None:
            if instance not in self.classes:
                return (".".join([instance, *rest]),)  # an external class's member
            owner: str | None = instance
            for part in rest[:-1]:
                owner = self._field_class(owner, part, sequence=False) if owner else None
            if owner is None:
                return self._name_fallback(rest[-1])
            method = self._method(owner, rest[-1])
            return (method,) if method is not None else ()
        if self._scope_lookup(module, fn_caller, head) is None:
            return self._name_fallback(rest[-1]) if rest else ()
        target = self._resolve_text(module, fn_caller, callee)
        if target is None or target in self._by_module:
            return ()
        return (target,)

    def _owner_class(self, func_qualname: str) -> str | None:
        scope: str | None = func_qualname
        while scope is not None and scope in self.functions:
            owner = self.functions[scope].class_qualname
            if owner is not None:
                return owner
            scope = scope.rpartition(".")[0] or None
        return None

    def _name_fallback(self, name: str) -> tuple[str, ...]:
        candidates = self._methods_by_name.get(name, [])
        if 0 < len(candidates) <= _NAME_FALLBACK_MAX:
            return tuple(candidates)
        return ()

    # --- queries -------------------------------------------------------------------------

    @property
    def unverified(self) -> tuple[str, ...]:
        return tuple(sorted(self._notes))

    def resolve(self, name: str, from_module: str | None = None) -> list[str]:
        """With `from_module`: what `name` means there (a project definition, or an external
        dotted name such as `music21.expressions.Turn`). Without: every project definition
        whose last component is `name`, or whose qualname ends with a dotted `name`."""
        if from_module is not None:
            target = self._resolve_text(from_module, None, name)
            return [target] if target is not None else []
        suffix = f".{name}"
        found = {
            q
            for table in (self.functions, self.classes, self.bindings)
            for q in table
            if q.endswith(suffix)
        }
        return sorted(found)

    def callers(self, qualname: str) -> list[CallSite]:
        """Every call site whose `resolved` contains `qualname`, TypeScript included."""
        sites = list(self._callers.get(qualname, ()))
        for facts in self.ts.values():
            sites.extend(c for c in facts.calls if qualname in c.resolved)
        return sites

    def references(self, name: str, *, prod_only: bool) -> list[Ref]:
        """Python and TypeScript refs whose `name` equals `name`, string literals included."""
        if self._refs_by_name is None:
            table: dict[str, list[Ref]] = {}
            for path in sorted(self.py):
                for ref in self.py[path].refs:
                    table.setdefault(ref.name, []).append(ref)
            for path in sorted(self.ts):
                for ref in self.ts[path].refs:
                    table.setdefault(ref.name, []).append(ref)
            self._refs_by_name = table
        refs = self._refs_by_name.get(name, [])
        if prod_only:
            return [r for r in refs if is_prod_path(r.path, self.cfg)]
        return list(refs)

    def mro(self, class_qualname: str) -> list[str]:
        """The class, then its bases breadth first; external bases are listed, not expanded."""
        out: list[str] = []
        todo = [class_qualname]
        while todo:
            current = todo.pop(0)
            if current in out:
                continue
            out.append(current)
            cls = self.classes.get(current)
            if cls is not None:
                todo.extend(cls.resolved_bases)
        return out if class_qualname in self.classes else []

    def subclasses(self, library_class: str) -> list[str]:
        """Library classes that inherit from `library_class`, for a package named in
        `[semantic].library_hierarchies`; empty, with a note, when its source isn't found."""
        package = library_class.split(".", 1)[0]
        if package not in self.cfg.library_hierarchies:
            return []
        site = libsrc.find_site_packages(self.repo)
        hierarchy = libsrc.library_hierarchy(site, package) if site is not None else {}
        if site is None or not hierarchy:
            self._notes.add(f"library-missing:{package}")
            return []
        canonical = libsrc.canonical_class(site, package, library_class)
        return list(libsrc.strict_subclasses(hierarchy, canonical))

    def module_of(self, path: str) -> str | None:
        facts = self.py.get(path)
        return facts.module if facts is not None else None


def _sibling(imported: str, here: str, packages: set[str], tops: set[str]) -> str:
    """An absolute import of a module next to the importer (`tests/helper.py` imported as
    `helper`, which pytest's rootdir and a script's own directory make work) resolves to
    that sibling when no project package of that name exists."""
    if not here or not imported or imported.split(".", 1)[0] in tops:
        return imported
    candidate = f"{here}.{imported}"
    return candidate if candidate in packages else imported


def _is_py_entry(value: object) -> TypeGuard[tuple[PyFileFacts, TypeHints]]:
    return (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], PyFileFacts)
        and isinstance(value[1], TypeHints)
    )
