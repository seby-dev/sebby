"""G8's dangling code references: a name the task removed that code still uses.

A top-level Python name defined at base and gone at head (`ChangeModel.removed_names`),
which no longer resolves from its module (a re-export keeps it alive), is dangling
wherever a head file imports it (`from m import name`) or reads it as `m.name` through a
module alias. A file that binds the name itself at top level (a `try`/`except ImportError`
fallback, say) is left alone. TypeScript follows the same idea for a relative import of an
export the task removed (`ChangeModel.removed_ts_exports`).

Every file at head is scanned, not only the changed ones: the usual escape is a script
outside the type check that nobody touched. Routing sends an anchor the task owns to the
implementer and any other to the controller.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable, Iterator, Mapping

from revgate.model import Finding, Grade, RuleOutput, Source, new_finding
from revgate.spi.facts import PyFileFacts, TsFileFacts
from revgate.static.ctx import StaticCtx

RULE = "docs.dangling_code_ref"
_TS_SUFFIXES = ("", ".ts", ".tsx", "/index.ts", "/index.tsx")


def _gone(ctx: StaticCtx) -> dict[str, frozenset[str]]:
    """Base module -> removed names that no longer resolve from that module at head."""
    out: dict[str, frozenset[str]] = {}
    for module, names in ctx.change.removed_names.items():
        dead = frozenset(n for n in names if not ctx.head.resolve(n, module))
        if dead:
            out[module] = dead
    return out


def _own_names(facts: PyFileFacts) -> frozenset[str]:
    """Names the file binds at its own top level."""
    prefix = f"{facts.module}."
    quals = [
        *(f.qualname for f in facts.functions if f.class_qualname is None and not f.is_nested),
        *(c.qualname for c in facts.classes),
        *(b.qualname for b in facts.bindings),
    ]
    return frozenset(q.removeprefix(prefix) for q in quals if q.startswith(prefix))


def _finding(path: str, line: int, module: str, name: str, how: str) -> Finding:
    return new_finding(
        RULE,
        file=path,
        line=line,
        message=f"{name} was removed from {module}, but this file still {how}",
        evidence=f"{module}.{name} is defined at base and absent at head",
        evidence_key=f"{module}.{name}",
        grade=Grade.E1_EXACT,
        source=Source.GENERIC,
        fix=f"import {name} from where it lives now, or drop this use",
    )


def _module_aliases(facts: PyFileFacts, gone: Mapping[str, frozenset[str]]) -> dict[str, str]:
    """Dotted prefix as written in this file -> a module that lost names."""
    out: dict[str, str] = {}
    for imp in facts.imports:
        if imp.name is not None:
            full = f"{imp.module}.{imp.name}" if imp.module else imp.name
            if full in gone:
                out[imp.alias] = full  # from pkg import util
        elif imp.module in gone:
            first = imp.module.split(".")[0]
            out[imp.module if imp.alias == first else imp.alias] = imp.module
    return out


def _python(facts: PyFileFacts, gone: Mapping[str, frozenset[str]]) -> Iterator[Finding]:
    own = _own_names(facts)
    for imp in facts.imports:
        names = gone.get(imp.module, frozenset())
        if imp.name in names and imp.module != facts.module and imp.alias not in own:
            yield _finding(facts.path, imp.line, imp.module, imp.name, "imports it")
    aliases = _module_aliases(facts, gone)
    if not aliases:
        return
    for ref in facts.refs:
        if ref.kind != "attribute" or not ref.text:
            continue
        prefix, _, name = ref.text.rpartition(".")
        module = aliases.get(prefix)
        if module is not None and name in gone[module]:
            yield _finding(facts.path, ref.line, module, name, f"reads it as {ref.text}")


def _ts_target(importer: str, spec: str, known: Iterable[str]) -> str | None:
    if not spec.startswith("."):
        return None  # a package or a path alias: not something this task's diff removed from
    stem = posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))
    if stem.endswith(".js"):
        stem = stem[:-3]
    paths = set(known)
    return next((stem + s for s in _TS_SUFFIXES if stem + s in paths), None)


def _typescript(
    facts: TsFileFacts, removed: Mapping[str, tuple[str, ...]], known: frozenset[str]
) -> Iterator[Finding]:
    for spec, imported, _local in facts.imports:
        if imported in ("*", "default"):
            continue
        target = _ts_target(facts.path, spec, known)
        if target is None or imported not in removed.get(target, ()):
            continue
        lines = [r.line for r in facts.refs if r.kind == "import" and r.name == imported]
        yield _finding(facts.path, min(lines, default=1), target, imported, "imports it")


class DanglingCodeRefRule:
    """`docs.dangling_code_ref`: E1, generic; blocking in scope, the controller's otherwise."""

    ids = frozenset({RULE})
    languages = frozenset({"py", "ts"})

    def check(self, ctx: StaticCtx) -> Iterable[RuleOutput]:
        found: dict[str, Finding] = {}
        gone = _gone(ctx)
        if gone:
            for path in sorted(ctx.head.py):
                facts = ctx.head.py[path]
                if facts.parse_error is None:
                    for f in _python(facts, gone):
                        found.setdefault(f.id, f)
        removed_ts = ctx.change.removed_ts_exports
        if removed_ts:
            known = frozenset(ctx.base.ts) | frozenset(ctx.head.ts)
            for path in sorted(ctx.head.ts):
                facts_ts = ctx.head.ts[path]
                if facts_ts.parse_error is None:
                    for f in _typescript(facts_ts, removed_ts, known):
                        found.setdefault(f.id, f)
        return sorted(found.values(), key=lambda f: (f.file, f.line, f.id))
