from __future__ import annotations

import textwrap

from revgate.spi.facts import CallSite, FuncFact, PyFileFacts
from revgate.spi.py_facts import index_python_file, module_name_for


def facts(source: str, path: str = "pkg/mod.py", *, is_test: bool = False) -> PyFileFacts:
    return index_python_file(
        path, textwrap.dedent(source), module=module_name_for(path), is_test=is_test
    )


def func(f: PyFileFacts, qualname: str) -> FuncFact:
    return next(fn for fn in f.functions if fn.qualname == qualname)


def call_to(f: PyFileFacts, callee: str) -> CallSite:
    calls = [c for fn in f.functions for c in fn.calls] + list(f.module_calls)
    return next(c for c in calls if c.callee == callee)


def test_module_name_for() -> None:
    assert module_name_for("src/myapp/api/app.py") == "myapp.api.app"
    assert module_name_for("scripts/x.py") == "scripts.x"
    assert module_name_for("pkg/__init__.py") == "pkg"


def test_parameters_decorators_and_return_annotation() -> None:
    f = facts(
        """
        import functools

        class C:
            @staticmethod
            @functools.lru_cache(maxsize=4)
            def m(a, /, b: int, c: str = "x", *args: int, d, e: float = 1.0, **kw: object) -> int:
                \"\"\"Doc.\"\"\"
                return 1
        """
    )
    fn = func(f, "pkg.mod.C.m")
    assert [(p.name, p.kind, p.default, p.annotation) for p in fn.params] == [
        ("a", "posonly", None, None),
        ("b", "pos", None, "int"),
        ("c", "pos", "'x'", "str"),
        ("args", "vararg", None, "int"),
        ("d", "kwonly", None, None),
        ("e", "kwonly", "1.0", "float"),
        ("kw", "kwarg", None, "object"),
    ]
    assert fn.decorators == ("staticmethod", "functools.lru_cache(maxsize=4)")
    assert fn.returns == "int"
    assert fn.docstring == "Doc."
    assert fn.class_qualname == "pkg.mod.C"
    assert not fn.is_nested
    assert f.classes[0].methods == ("m",)


def test_nested_function_qualname_and_flag() -> None:
    f = facts(
        """
        def outer():
            def inner():
                return 2
            return inner()
        """
    )
    inner = func(f, "pkg.mod.outer.inner")
    assert inner.is_nested
    assert not func(f, "pkg.mod.outer").is_nested


def test_body_hash_ignores_the_docstring_and_changes_with_the_body() -> None:
    a = func(facts('def f():\n    """One."""\n    return 1\n'), "pkg.mod.f")
    b = func(facts('def f():\n    """Two."""\n    return 1\n'), "pkg.mod.f")
    c = func(facts('def f():\n    """One."""\n    return 2\n'), "pkg.mod.f")
    assert a.body_hash == b.body_hash
    assert a.body_hash != c.body_hash


def test_cardinality() -> None:
    f = facts(
        """
        def optional(xs):
            for x in xs:
                if x.bad:
                    return x
            return None

        def many(xs):
            out = []
            for x in xs:
                if x.bad:
                    out.append(x)
            return out

        def comp(xs):
            return [x for x in xs if x.bad]

        def nothing(xs):
            print(xs)

        def single(xs):
            return xs[0]
        """
    )
    assert func(f, "pkg.mod.optional").cardinality == "optional"
    assert func(f, "pkg.mod.many").cardinality == "many"
    assert func(f, "pkg.mod.comp").cardinality == "many"
    assert func(f, "pkg.mod.nothing").cardinality == "unknown"
    assert func(f, "pkg.mod.single").cardinality == "one"


def test_call_uses() -> None:
    f = facts(
        """
        def user(x):
            first = f0(x)[0]
            a, b = f1(x)
            for r in f2(x):
                pass
            if f3(x):
                pass
            y = f4(x).y
            f5(x)
            g(f6(x))
            z = f7(x)
            return f8(x)
        """
    )
    uses = {c.callee: c.use for c in func(f, "pkg.mod.user").calls}
    assert uses == {
        "f0": "index0",
        "f1": "unpacked",
        "f2": "iterated",
        "f3": "truth",
        "f4": "attr",
        "f5": "discarded",
        "f6": "passed",
        "g": "discarded",
        "f7": "assigned",
        "f8": "returned",
    }


def test_call_site_try_and_keywords_and_module_calls() -> None:
    f = facts(
        """
        setup(name="x")

        def h():
            try:
                risky(1, key=2, **extra)
            except ValueError:
                recover()
        """
    )
    risky = call_to(f, "risky")
    assert risky.in_try
    assert risky.keywords == ("key", "**")
    assert risky.caller == "pkg.mod.h"
    assert not call_to(f, "recover").in_try
    setup = call_to(f, "setup")
    assert setup.caller == "pkg.mod.<module>"
    assert setup in f.module_calls


def test_raises_are_the_functions_own() -> None:
    f = facts(
        """
        def r(x):
            if x:
                raise ValueError("bad")
            try:
                pass
            except KeyError:
                raise
            def inner():
                raise TypeError
            raise errors.Custom
        """
    )
    assert func(f, "pkg.mod.r").raises == ("ValueError", "errors.Custom")


def test_isinstance_elif_chain() -> None:
    f = facts(
        """
        def dispatch(n):
            if isinstance(n, A):
                return 1
            elif isinstance(n, (B, mod.C)):
                return 2
            return 3
        """
    )
    [chain] = f.isinstance_chains
    assert chain.func == "pkg.mod.dispatch"
    assert chain.subject == "n"
    assert [(b.classes, b.negated) for b in chain.branches] == [
        (("A",), False),
        (("B", "mod.C"), False),
    ]
    assert chain.branches[0].line < chain.branches[1].line
    assert chain.mentioned == ("A", "B", "isinstance", "mod", "mod.C", "n")


def test_isinstance_early_return_run_and_negation() -> None:
    f = facts(
        """
        def guard(o):
            if not isinstance(o, expressions.InvertedTurn):
                return None
            if isinstance(o, expressions.Turn):
                return "turn"
            return "other"
        """
    )
    [chain] = f.isinstance_chains
    assert [(b.classes, b.negated) for b in chain.branches] == [
        (("expressions.InvertedTurn",), True),
        (("expressions.Turn",), False),
    ]


def test_bindings_and_string_refs_and_imports() -> None:
    f = facts(
        """
        from .pitch import modulate
        import os.path
        import numpy as np

        ORNAMENT_CODES = {"t": 1, "m": 2}

        def g():
            return "relocate"
        """,
        path="src/pkg/io.py",
    )
    [binding] = f.bindings
    assert binding.qualname == "pkg.io.ORNAMENT_CODES"
    assert binding.name == "ORNAMENT_CODES"
    imports = {i.alias: (i.module, i.name) for i in f.imports}
    assert imports == {
        "modulate": ("pkg.pitch", "modulate"),
        "os": ("os.path", None),
        "np": ("numpy", None),
    }
    strings = [(r.name, r.in_symbol) for r in f.refs if r.kind == "string"]
    assert strings == [("t", None), ("m", None), ("relocate", "pkg.io.g")]


def test_relative_import_from_a_package_init() -> None:
    f = facts("from . import sub\nfrom ..top import t\n", path="src/pkg/inner/__init__.py")
    assert {(i.module, i.name) for i in f.imports} == {("pkg.inner", "sub"), ("pkg.top", "t")}


def test_refs_cover_identifiers_attributes_and_imports() -> None:
    f = facts(
        """
        from pkg.a import f

        def use():
            return f(pitch.modulate)
        """
    )
    kinds = {(r.name, r.kind) for r in f.refs}
    assert ("f", "import") in kinds
    assert ("f", "identifier") in kinds
    assert ("modulate", "attribute") in kinds
    attr = next(r for r in f.refs if r.kind == "attribute")
    assert attr.text == "pitch.modulate"
    assert attr.in_symbol == "pkg.mod.use"


def test_same_source_gives_equal_facts() -> None:
    src = "class A:\n    x: int\n    def m(self):\n        return [i for i in self.x]\n"
    assert facts(src) == facts(src)


def test_syntax_error_file_is_recorded_not_raised() -> None:
    f = index_python_file("x.py", "def (:\n", module="x", is_test=False)
    assert f.parse_error is not None
    assert f.path == "x.py"
    assert f.module == "x"
    assert f.imports == ()
    assert f.functions == ()
    assert f.classes == ()
    assert f.bindings == ()
    assert f.refs == ()
    assert f.isinstance_chains == ()
    assert f.module_calls == ()


def test_null_bytes_are_a_parse_error_too() -> None:
    f = index_python_file("y.py", "x = 1\0\n", module="y", is_test=False)
    assert f.parse_error is not None


def test_a_result_held_in_a_name_and_read_at_zero_is_index0() -> None:
    # The prototype's consumer idiom (bench case C1): `x = f(...)` followed by `x[0]` in
    # the same function reads only the first item, the same as `f(...)[0]`.
    f = facts(
        """
        def user(item):
            found = check(item)
            if found:
                report(found[0])
            rows = other(item)
            for r in rows:
                pass
            rows2 = third(item)
            later = rows2[1]

        def elsewhere(found):
            return found[0]
        """
    )
    uses = {c.callee: c.use for c in func(f, "pkg.mod.user").calls}
    assert uses["check"] == "index0"
    assert uses["other"] == "assigned"
    assert uses["third"] == "assigned"


def test_isinstance_through_a_dispatch_table_and_a_constant_tuple() -> None:
    f = facts(
        """
        TABLE = [
            (mod.A, "a"),
            (mod.B, "b"),
        ]
        SKIP = (mod.C, mod.D)

        def label(o):
            if isinstance(o, SKIP):
                return None
            for cls, name in TABLE:
                if isinstance(o, cls):
                    return name
        """
    )
    chains = [c for c in f.isinstance_chains if c.func == "pkg.mod.label"]
    assert [[(b.classes, b.line) for b in c.branches] for c in chains] == [
        [(("mod.A",), 3), (("mod.B",), 4)],
        [(("mod.C", "mod.D"), 9)],
    ]
    assert {"mod.A", "mod.C", "mod.D"} <= set(chains[1].mentioned)
