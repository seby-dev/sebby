"""Fact types the index records per file; frozen, and tuples rather than lists throughout."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ParamKind = Literal["posonly", "pos", "vararg", "kwonly", "kwarg"]
CallUse = Literal[
    "index0",
    "unpacked",
    "iterated",
    "truth",
    "attr",
    "returned",
    "discarded",
    "passed",
    "assigned",
    "other",
]
Cardinality = Literal["one", "many", "optional", "unknown"]
RefKind = Literal["identifier", "attribute", "string", "import"]


@dataclass(frozen=True)
class Param:
    name: str
    kind: ParamKind
    default: str | None  # ast.unparse of the default; None when required
    annotation: str | None


@dataclass(frozen=True)
class CallSite:
    caller: str  # qualname of the enclosing function, or "<module>.<module>"
    callee: str  # dotted text as written: "check_final_cadence", "expressions.Turn"
    path: str
    line: int
    use: CallUse
    in_try: bool
    keywords: tuple[str, ...]
    resolved: tuple[str, ...] = ()  # qualnames the linker resolved the callee to


@dataclass(frozen=True)
class FuncFact:
    qualname: str  # "<module>.<Class>.<name>" or "<module>.<name>"
    path: str
    lineno: int
    end_lineno: int
    params: tuple[Param, ...]
    returns: str | None
    decorators: tuple[str, ...]
    body_hash: str  # sha1 of ast.dump of the body without its docstring
    cardinality: Cardinality
    calls: tuple[CallSite, ...]
    raises: tuple[str, ...]
    is_nested: bool
    docstring: str | None
    class_qualname: str | None
    is_async: bool


@dataclass(frozen=True)
class ClassFact:
    qualname: str
    path: str
    lineno: int
    end_lineno: int
    bases: tuple[str, ...]  # as written
    fields: tuple[str, ...]  # annotated class-level names
    methods: tuple[str, ...]
    resolved_bases: tuple[str, ...] = ()


@dataclass(frozen=True)
class BindingFact:
    qualname: str  # "<module>.NAME"
    name: str
    path: str
    lineno: int
    end_lineno: int
    value_hash: str


@dataclass(frozen=True)
class ImportFact:
    module: str  # absolute dotted module (relative imports resolved)
    name: str | None  # the imported name for `from m import name`
    alias: str  # the local name
    line: int


@dataclass(frozen=True)
class IsinstanceBranch:
    classes: tuple[str, ...]  # dotted class expressions as written
    line: int
    negated: bool


@dataclass(frozen=True)
class IsinstanceChain:
    func: str  # enclosing function qualname
    subject: str  # ast.unparse of the tested expression
    branches: tuple[IsinstanceBranch, ...]
    mentioned: tuple[str, ...]  # every dotted name the function mentions, sorted


@dataclass(frozen=True)
class Ref:
    path: str
    line: int
    name: str  # the last dotted component, or the literal's text
    kind: RefKind
    in_symbol: str | None  # enclosing function or class qualname
    text: str = ""  # full dotted text for attributes ("pitch.modulate")


@dataclass(frozen=True)
class PyFileFacts:
    path: str
    module: str
    is_test: bool
    parse_error: str | None
    imports: tuple[ImportFact, ...]
    functions: tuple[FuncFact, ...]
    classes: tuple[ClassFact, ...]
    bindings: tuple[BindingFact, ...]
    refs: tuple[Ref, ...]
    isinstance_chains: tuple[IsinstanceChain, ...]
    module_calls: tuple[CallSite, ...]


@dataclass(frozen=True)
class TsFunctionFact:
    qualname: str  # "<path>::<name>"
    name: str
    path: str
    lineno: int
    end_lineno: int
    params: tuple[str, ...]
    exported: bool


@dataclass(frozen=True)
class TsFileFacts:
    path: str
    is_test: bool
    parse_error: str | None
    imports: tuple[tuple[str, str, str], ...]  # (module specifier, imported name, local name)
    exports: tuple[str, ...]
    functions: tuple[TsFunctionFact, ...]
    calls: tuple[CallSite, ...]
    refs: tuple[Ref, ...]
