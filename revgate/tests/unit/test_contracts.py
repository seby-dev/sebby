"""`contract.cardinality_consumer`: a function that now returns several items, and the
untouched callers that still read one."""

from __future__ import annotations

from pathlib import Path

from conftest import MakeRepo, RepoFixture

from revgate.config import parse_config
from revgate.model import Finding, Grade, Source, TaskScope
from revgate.spi.cache import BlobCache
from revgate.static.contracts import CardinalityRule
from revgate.static.ctx import StaticCtx, build_static_ctx

BASE_FIND = (
    "def find(xs):\n    for x in xs:\n        if x < 0:\n            return x\n    return None\n"
)
HEAD_FIND = (
    "def find(xs):\n"
    "    out = []\n"
    "    for x in xs:\n"
    "        if x < 0:\n"
    "            out.append(x)\n"
    "    return out\n"
)
INDEX0 = "from pkg.check import find\n\n\ndef first(xs):\n    return find(xs)[0]\n"
ITERATES = (
    "from pkg.check import find\n\n\ndef each(xs):\n    for r in find(xs):\n        print(r)\n"
)


def _ctx(repo: RepoFixture, tmp_path: Path) -> StaticCtx:
    return build_static_ctx(
        repo.path,
        repo.base,
        repo.head,
        cfg=parse_config(None),
        plan=None,
        report=None,
        scope=TaskScope(task_id=None, owns=frozenset(), runs=frozenset()),
        cache=BlobCache(tmp_path / "cache", "test"),
    )


def _findings(ctx: StaticCtx) -> list[Finding]:
    return [o for o in CardinalityRule().check(ctx) if isinstance(o, Finding)]


def test_rule_ids() -> None:
    rule = CardinalityRule()
    assert rule.ids == frozenset({"contract.cardinality_consumer"})
    assert rule.languages == frozenset({"py"})


def test_cardinality_consumer(make_repo: MakeRepo, tmp_path: Path) -> None:
    base = {
        "pkg/__init__.py": "",
        "pkg/check.py": BASE_FIND,
        "pkg/a.py": INDEX0,
        "pkg/b.py": ITERATES,
    }
    # pkg/c.py is new in this task: its [0] read is on an added line and stays silent.
    head: dict[str, str | None] = {"pkg/check.py": HEAD_FIND, "pkg/c.py": INDEX0}
    repo = make_repo(base, head)
    found = _findings(_ctx(repo, tmp_path))
    assert [(f.rule, f.file, f.line) for f in found] == [
        ("contract.cardinality_consumer", "pkg/a.py", 5)
    ]
    (f,) = found
    assert f.grade is Grade.E2_STRUCTURAL
    assert f.source is Source.GENERIC
    assert f.impact == "critical"
    assert "find" in f.message


def test_no_finding_when_cardinality_is_unchanged(make_repo: MakeRepo, tmp_path: Path) -> None:
    edited = BASE_FIND.replace("x < 0", "x <= 0")
    repo = make_repo(
        {"pkg/__init__.py": "", "pkg/check.py": BASE_FIND, "pkg/a.py": INDEX0},
        {"pkg/check.py": edited},
    )
    assert _findings(_ctx(repo, tmp_path)) == []


def test_test_consumers_are_not_reported(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = make_repo(
        {"pkg/__init__.py": "", "pkg/check.py": BASE_FIND, "tests/test_a.py": INDEX0},
        {"pkg/check.py": HEAD_FIND},
    )
    assert _findings(_ctx(repo, tmp_path)) == []
