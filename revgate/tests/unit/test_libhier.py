"""`lib.isinstance_shadowing`, `lib.subclass_unhandled`, and `lib.subclass_unlisted`."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import MakeRepo, RepoFixture

import revgate.spi.libsrc
from revgate.config import Config, parse_config
from revgate.model import Finding, Grade, Source, TaskScope, Unverified
from revgate.spi.cache import BlobCache
from revgate.static.ctx import StaticCtx, build_static_ctx
from revgate.static.libhier import LibHierarchyRule

SHAPES = "class Base:\n    pass\n\n\nclass Sub(Base):\n    pass\n"
USE_BASE = 'from pkg.shapes import Base, Sub\n\n\ndef kind(x):\n    return "none"\n'
SHADOWED = (
    "from pkg.shapes import Base, Sub\n"
    "\n"
    "\n"
    "def kind(x):\n"
    "    if isinstance(x, Base):\n"
    '        return "base"\n'
    "    elif isinstance(x, Sub):\n"
    '        return "sub"\n'
    '    return "none"\n'
)
ORDERED = (
    "from pkg.shapes import Base, Sub\n"
    "\n"
    "\n"
    "def kind(x):\n"
    "    if isinstance(x, Sub):\n"
    '        return "sub"\n'
    "    elif isinstance(x, Base):\n"
    '        return "base"\n'
    '    return "none"\n'
)

FAKELIB_EXPRESSIONS = (
    "class Turn:\n    pass\n\n\nclass InvertedTurn(Turn):\n    pass\n\n\n"
    "class Trill:\n    pass\n\n\nclass HalfStepTrill(Trill):\n    pass\n"
)
LIB_CFG = (
    b"[semantic]\n"
    b'library_hierarchies = ["fakelib"]\n'
    b'curated_distinct_subclasses = ["fakelib.expressions.InvertedTurn"]\n'
)


def _ctx(repo: RepoFixture, tmp_path: Path, cfg: Config | None = None) -> StaticCtx:
    return build_static_ctx(
        repo.path,
        repo.base,
        repo.head,
        cfg=cfg or parse_config(None),
        plan=None,
        report=None,
        scope=TaskScope(task_id=None, owns=frozenset(), runs=frozenset()),
        cache=BlobCache(tmp_path / "cache", "test"),
    )


def _run(ctx: StaticCtx) -> tuple[list[Finding], list[Unverified]]:
    out = list(LibHierarchyRule().check(ctx))
    return (
        [o for o in out if isinstance(o, Finding)],
        [o for o in out if isinstance(o, Unverified)],
    )


def test_rule_ids() -> None:
    rule = LibHierarchyRule()
    assert rule.ids == frozenset(
        {"lib.isinstance_shadowing", "lib.subclass_unhandled", "lib.subclass_unlisted"}
    )
    assert rule.languages == frozenset({"py"})


def test_isinstance_shadowing(make_repo: MakeRepo, tmp_path: Path) -> None:
    base = {"pkg/__init__.py": "", "pkg/shapes.py": SHAPES, "pkg/use.py": USE_BASE}
    repo = make_repo(base, {"pkg/use.py": SHADOWED})
    found, _ = _run(_ctx(repo, tmp_path))
    assert [(f.rule, f.file, f.line, f.symbol) for f in found] == [
        ("lib.isinstance_shadowing", "pkg/use.py", 7, "pkg.use.kind")
    ]
    assert found[0].grade is Grade.E1_EXACT
    assert "Sub" in found[0].message and "Base" in found[0].message


def test_isinstance_in_subclass_first_order_is_silent(make_repo: MakeRepo, tmp_path: Path) -> None:
    base = {"pkg/__init__.py": "", "pkg/shapes.py": SHAPES, "pkg/use.py": USE_BASE}
    repo = make_repo(base, {"pkg/use.py": ORDERED})
    assert _run(_ctx(repo, tmp_path)) == ([], [])


def test_isinstance_shadowing_with_early_returns(make_repo: MakeRepo, tmp_path: Path) -> None:
    head = (
        "from pkg.shapes import Base, Sub\n\n\ndef kind(x):\n"
        '    if isinstance(x, Base):\n        return "base"\n'
        '    if isinstance(x, Sub):\n        return "sub"\n'
        '    return "none"\n'
    )
    base = {"pkg/__init__.py": "", "pkg/shapes.py": SHAPES, "pkg/use.py": USE_BASE}
    repo = make_repo(base, {"pkg/use.py": head})
    found, _ = _run(_ctx(repo, tmp_path))
    assert [(f.rule, f.line) for f in found] == [("lib.isinstance_shadowing", 7)]


@pytest.mark.parametrize("second", ["elif", "if"])
def test_a_guarded_isinstance_branch_shadows_nothing(
    make_repo: MakeRepo, tmp_path: Path, second: str
) -> None:
    # `isinstance(x, Base) and x.flag` is false for a Sub whose flag is false, so the Sub
    # branch after it still runs, in the elif form and in the early-exit form alike.
    head = (
        "from pkg.shapes import Base, Sub\n\n\ndef kind(x):\n"
        '    if isinstance(x, Base) and x.flag:\n        return "flagged"\n'
        f'    {second} isinstance(x, Sub):\n        return "sub"\n'
        '    return "none"\n'
    )
    base = {"pkg/__init__.py": "", "pkg/shapes.py": SHAPES, "pkg/use.py": USE_BASE}
    repo = make_repo(base, {"pkg/use.py": head})
    assert _run(_ctx(repo, tmp_path)) == ([], [])


def test_a_guarded_branch_is_still_shadowed_by_an_earlier_plain_one(
    make_repo: MakeRepo, tmp_path: Path
) -> None:
    head = (
        "from pkg.shapes import Base, Sub\n\n\ndef kind(x):\n"
        '    if isinstance(x, Base):\n        return "base"\n'
        '    elif isinstance(x, Sub) and x.flag:\n        return "sub"\n'
        '    return "none"\n'
    )
    base = {"pkg/__init__.py": "", "pkg/shapes.py": SHAPES, "pkg/use.py": USE_BASE}
    repo = make_repo(base, {"pkg/use.py": head})
    found, _ = _run(_ctx(repo, tmp_path))
    assert [(f.rule, f.line) for f in found] == [("lib.isinstance_shadowing", 7)]


def test_baseline_differencing(make_repo: MakeRepo, tmp_path: Path) -> None:
    edited = SHADOWED.replace('return "none"', 'return "neither"')
    base = {"pkg/__init__.py": "", "pkg/shapes.py": SHAPES, "pkg/use.py": SHADOWED}
    repo = make_repo(base, {"pkg/use.py": edited})
    ctx = _ctx(repo, tmp_path)
    assert "pkg.use.kind" in ctx.change.functions
    assert _run(ctx) == ([], [])


def _fakelib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    site = tmp_path / "site"
    (site / "fakelib").mkdir(parents=True)
    (site / "fakelib" / "__init__.py").write_text("")
    (site / "fakelib" / "expressions.py").write_text(FAKELIB_EXPRESSIONS)
    monkeypatch.setattr(revgate.spi.libsrc, "find_site_packages", lambda repo: site)
    return site


def _dispatch(body: str) -> str:
    return f"from fakelib import expressions\n\n\ndef label(o):\n{body}    return None\n"


LABEL_BASE = _dispatch("")
TURN_ONLY = _dispatch('    if isinstance(o, expressions.Turn):\n        return "turn"\n')


def _lib_findings(
    make_repo: MakeRepo, tmp_path: Path, head: str
) -> tuple[list[Finding], list[Unverified]]:
    repo = make_repo({"pkg/__init__.py": "", "pkg/lab.py": LABEL_BASE}, {"pkg/lab.py": head})
    return _run(_ctx(repo, tmp_path, parse_config(LIB_CFG)))


def test_subclass_unhandled(
    make_repo: MakeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fakelib(tmp_path, monkeypatch)
    found, unverified = _lib_findings(make_repo, tmp_path, TURN_ONLY)
    assert unverified == []
    assert [(f.rule, f.file, f.line, f.symbol) for f in found] == [
        ("lib.subclass_unhandled", "pkg/lab.py", 5, "pkg.lab.label")
    ]
    (f,) = found
    assert f.grade is Grade.E1_EXACT
    assert f.source is Source.DECLARED
    assert "InvertedTurn" in f.message


def test_subclass_handled_by_an_earlier_branch(
    make_repo: MakeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fakelib(tmp_path, monkeypatch)
    head = _dispatch(
        "    if isinstance(o, expressions.InvertedTurn):\n        return None\n"
        '    if isinstance(o, expressions.Turn):\n        return "turn"\n'
    )
    assert _lib_findings(make_repo, tmp_path, head) == ([], [])


def test_subclass_excluded_by_a_guard(
    make_repo: MakeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fakelib(tmp_path, monkeypatch)
    head = _dispatch(
        "    if isinstance(o, expressions.Turn) and not isinstance(o, expressions.InvertedTurn):\n"
        '        return "turn"\n'
    )
    assert _lib_findings(make_repo, tmp_path, head) == ([], [])


def test_uncurated_subclass_is_unlisted_only(
    make_repo: MakeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fakelib(tmp_path, monkeypatch)
    head = _dispatch('    if isinstance(o, expressions.Trill):\n        return "trill"\n')
    found, _ = _lib_findings(make_repo, tmp_path, head)
    assert [(f.rule, f.line) for f in found] == [("lib.subclass_unlisted", 5)]
    assert found[0].grade is Grade.E3_HEURISTIC
    assert "HalfStepTrill" in found[0].message


def test_library_shadowing(
    make_repo: MakeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fakelib(tmp_path, monkeypatch)
    head = _dispatch(
        '    if isinstance(o, expressions.Turn):\n        return "turn"\n'
        "    if isinstance(o, expressions.InvertedTurn):\n        return None\n"
    )
    found, _ = _lib_findings(make_repo, tmp_path, head)
    assert [(f.rule, f.line) for f in found] == [("lib.isinstance_shadowing", 7)]


def test_missing_library_is_unverified(
    make_repo: MakeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(revgate.spi.libsrc, "find_site_packages", lambda repo: None)
    found, unverified = _lib_findings(make_repo, tmp_path, TURN_ONLY)
    assert found == []
    assert [u.note for u in unverified] == ["library-missing:fakelib"]


TABLE = (
    "from fakelib import expressions\n"
    "\n"
    "_TABLE = [\n"
    '    (expressions.Trill, "trill"),\n'
    '    (expressions.Turn, "turn"),\n'
    "]\n"
    "_SKIP = (expressions.InvertedTurn,)\n"
    "\n"
    "\n"
    "def label(o):\n"
    "{guard}"
    "    for cls, name in _TABLE:\n"
    "        if isinstance(o, cls):\n"
    "            return name\n"
    "    return None\n"
)


def test_a_dispatch_table_is_read_entry_by_entry(
    make_repo: MakeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A loop over a module-level (class, label) table dispatches on each entry in order,
    # as the r1 and r3 prototypes read it (bench case C3); the finding sits on the entry.
    _fakelib(tmp_path, monkeypatch)
    found, _ = _lib_findings(make_repo, tmp_path, TABLE.format(guard=""))
    assert [(f.rule, f.line, f.symbol) for f in found] == [
        ("lib.subclass_unlisted", 4, "pkg.lab.label"),
        ("lib.subclass_unhandled", 5, "pkg.lab.label"),
    ]
    assert "InvertedTurn" in found[1].message


def test_a_guard_through_a_constant_tuple_excludes_its_classes(
    make_repo: MakeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `isinstance(o, _SKIP)` over a module-level tuple names each class in it, and a
    # constant the function reads counts as mentioning its classes.
    _fakelib(tmp_path, monkeypatch)
    guard = "    if isinstance(o, _SKIP):\n        return None\n"
    found, _ = _lib_findings(make_repo, tmp_path, TABLE.format(guard=guard))
    assert [f.rule for f in found] == ["lib.subclass_unlisted"]
