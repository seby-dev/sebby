from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from conftest import MakeRepo, RepoFixture

from revgate import gitio
from revgate.change import ChangeModel, change_model
from revgate.config import Config, parse_config
from revgate.model import TaskScope
from revgate.spi import ts_bridge
from revgate.spi.cache import BlobCache
from revgate.spi.facts import TsFileFacts
from revgate.spi.link import TreeIndex, TsIndexerFn
from revgate.static.ctx import StaticCtx, build_static_ctx

_EXPORT_RE = re.compile(r"^export (?:function|const) (\w+)", re.MULTILINE)


def fake_ts(sources: Mapping[str, str]) -> Mapping[str, TsFileFacts]:
    """A stand-in for the Node process: exports are the `export function|const` names."""
    return {
        p: TsFileFacts(p, False, None, (), tuple(_EXPORT_RE.findall(src)), (), (), ())
        for p, src in sorted(sources.items())
    }


def cfg() -> Config:
    return parse_config(None)


def scope() -> TaskScope:
    return TaskScope(task_id=None, owns=frozenset(), runs=frozenset())


def model(
    repo: RepoFixture,
    tmp_path: Path,
    *,
    plan_kind: str | None = None,
    ts: TsIndexerFn | None = None,
) -> ChangeModel:
    cache = BlobCache(tmp_path / "cache", "test")
    base = TreeIndex.build(repo.path, repo.base, cache, cfg(), ts_indexer=ts)
    head = TreeIndex.build(repo.path, repo.head, cache, cfg(), ts_indexer=ts)
    status = gitio.diff_name_status(repo.path, repo.base, repo.head)
    return change_model(base, head, status, cfg(), plan_kind=plan_kind)


FUNC = "def f(a):\n    x = a + 1\n    return x\n"


def test_body_edit_is_modified_body_with_lines_inside(make_repo: MakeRepo, tmp_path: Path) -> None:
    head = "def f(a):\n    x = a + 2\n    return x\n"
    repo = make_repo({"pkg/__init__.py": "", "pkg/m.py": FUNC}, {"pkg/m.py": head})
    fc = model(repo, tmp_path).functions["pkg.m.f"]
    assert (fc.status, fc.kind, fc.signature_changed) == ("modified", "body", False)
    assert fc.changed_head_lines == (2,)
    assert fc.base is not None and fc.head is not None


def test_parameter_added_is_a_signature_change(make_repo: MakeRepo, tmp_path: Path) -> None:
    head = "def f(a, b=0):\n    x = a + 1\n    return x\n"
    repo = make_repo({"pkg/__init__.py": "", "pkg/m.py": FUNC}, {"pkg/m.py": head})
    fc = model(repo, tmp_path).functions["pkg.m.f"]
    assert (fc.status, fc.kind, fc.signature_changed) == ("modified", "signature", True)


def test_docstring_only_edit_is_doc_only(make_repo: MakeRepo, tmp_path: Path) -> None:
    base = 'def f(a):\n    """Old."""\n    x = a + 1\n    return x\n'
    head = 'def f(a):\n    """New words."""\n    x = a + 1\n    return x\n'
    repo = make_repo({"pkg/__init__.py": "", "pkg/m.py": base}, {"pkg/m.py": head})
    fc = model(repo, tmp_path).functions["pkg.m.f"]
    assert (fc.status, fc.kind) == ("doc_only", "doc_only")


def test_reformatted_function_is_format_only(make_repo: MakeRepo, tmp_path: Path) -> None:
    head = "def f(a):\n    x = (a + 1)  # same\n    return x\n"
    repo = make_repo({"pkg/__init__.py": "", "pkg/m.py": FUNC}, {"pkg/m.py": head})
    fc = model(repo, tmp_path).functions["pkg.m.f"]
    assert (fc.status, fc.kind) == ("modified", "format_only")


def test_untouched_function_in_a_changed_file_is_absent(
    make_repo: MakeRepo, tmp_path: Path
) -> None:
    base = FUNC + "\n\ndef g():\n    return 1\n"
    head = FUNC + "\n\ndef g():\n    return 2\n"
    repo = make_repo({"pkg/__init__.py": "", "pkg/m.py": base}, {"pkg/m.py": head})
    cm = model(repo, tmp_path)
    assert set(cm.functions) == {"pkg.m.g"}


def test_function_moved_to_another_module_is_a_rename(make_repo: MakeRepo, tmp_path: Path) -> None:
    base = {"pkg/__init__.py": "", "pkg/a.py": FUNC + "\n\ndef keep():\n    return 1\n"}
    head = {"pkg/a.py": "def keep():\n    return 1\n", "pkg/b.py": FUNC}
    repo = make_repo(base, head)
    cm = model(repo, tmp_path)
    fc = cm.functions["pkg.b.f"]
    assert (fc.status, fc.kind, fc.path) == ("moved", "rename", "pkg/b.py")
    assert cm.renamed == {"pkg.a.f": "pkg.b.f"}
    assert "pkg.a.f" not in cm.functions
    assert cm.removed_names["pkg.a"] == ("f",)


def test_one_line_function_moved_is_removed_and_added(make_repo: MakeRepo, tmp_path: Path) -> None:
    small = "def f():\n    return 1\n"
    repo = make_repo(
        {"pkg/__init__.py": "", "pkg/a.py": small}, {"pkg/a.py": "", "pkg/b.py": small}
    )
    cm = model(repo, tmp_path)
    assert cm.renamed == {}
    assert (cm.functions["pkg.a.f"].status, cm.functions["pkg.a.f"].kind) == ("removed", "deleted")
    assert (cm.functions["pkg.b.f"].status, cm.functions["pkg.b.f"].kind) == ("added", "new")


def test_changed_constant_is_a_binding_change_with_readers(
    make_repo: MakeRepo, tmp_path: Path
) -> None:
    base = {
        "pkg/__init__.py": "",
        "pkg/consts.py": "LIMIT = 3\n\n\ndef over(x):\n    return x > LIMIT\n",
        "pkg/use.py": "from pkg.consts import LIMIT\n\n\ndef cap(x):\n    return min(x, LIMIT)\n",
        "pkg/other.py": "def unrelated():\n    return 1\n",
    }
    repo = make_repo(base, {"pkg/consts.py": "LIMIT = 4\n\n\ndef over(x):\n    return x > LIMIT\n"})
    bc = model(repo, tmp_path).bindings["pkg.consts.LIMIT"]
    assert bc.status == "modified"
    assert "pkg.consts.over" in bc.readers
    assert "pkg.use" in bc.readers
    assert "pkg.use.cap" in bc.readers
    assert not any(r.startswith("pkg.other") for r in bc.readers)


def test_removed_top_level_function_is_in_removed_names(
    make_repo: MakeRepo, tmp_path: Path
) -> None:
    base = FUNC + "\n\ndef helper():\n    return 1\n"
    repo = make_repo({"pkg/__init__.py": "", "pkg/m.py": base}, {"pkg/m.py": FUNC})
    cm = model(repo, tmp_path)
    assert cm.removed_names == {"pkg.m": ("helper",)}
    assert cm.functions["pkg.m.helper"].status == "removed"


def test_removed_method_is_not_a_removed_top_level_name(
    make_repo: MakeRepo, tmp_path: Path
) -> None:
    base = "class C:\n    x: int\n    y: int\n\n    def m(self):\n        return 1\n"
    head = "class C:\n    x: int\n    z: int\n"
    repo = make_repo({"pkg/__init__.py": "", "pkg/m.py": base}, {"pkg/m.py": head})
    cm = model(repo, tmp_path)
    assert cm.removed_names == {}
    fields = cm.fields["pkg.m.C"]
    assert (fields.added, fields.removed) == (("z",), ("y",))


def test_classes_added_and_removed(make_repo: MakeRepo, tmp_path: Path) -> None:
    base = "class Old:\n    pass\n"
    head = "class New:\n    pass\n"
    repo = make_repo({"pkg/__init__.py": "", "pkg/m.py": base}, {"pkg/m.py": head})
    cm = model(repo, tmp_path)
    assert cm.added_classes == ("pkg.m.New",)
    assert cm.removed_classes == ("pkg.m.Old",)
    assert cm.removed_names == {"pkg.m": ("Old",)}


def test_removed_typescript_export(make_repo: MakeRepo, tmp_path: Path) -> None:
    base = "export function keep() {}\nexport function gone() {}\n"
    head = "export function keep() {}\n"
    repo = make_repo(
        {"web/src/a.ts": base, "web/src/b.ts": "export const z = 1;\n"},
        {"web/src/a.ts": head, "web/src/b.ts": None},
    )
    cm = model(repo, tmp_path, ts=fake_ts)
    assert cm.removed_ts_exports == {"web/src/a.ts": ("gone",), "web/src/b.ts": ("z",)}


def test_tests_only_diff_is_test_kind(make_repo: MakeRepo, tmp_path: Path) -> None:
    base = {"pkg/__init__.py": "", "pkg/m.py": FUNC, "tests/test_m.py": "def test_a():\n    pass\n"}
    repo = make_repo(base, {"tests/test_m.py": "def test_a():\n    assert True\n"})
    assert model(repo, tmp_path).task_kind == "test"
    assert model(repo, tmp_path, plan_kind="fix").task_kind == "test"


def test_docs_only_diff_is_docs_kind(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = make_repo({"README.md": "a\n"}, {"README.md": "b\n", "docs/guide.md": "c\n"})
    assert model(repo, tmp_path).task_kind == "docs"


def test_plan_kind_sets_refactor_and_fix_but_not_the_commit(
    make_repo: MakeRepo, tmp_path: Path
) -> None:
    head = "def f(a):\n    y = a + 1\n    return y\n"
    repo = make_repo({"pkg/__init__.py": "", "pkg/m.py": FUNC}, {"pkg/m.py": head})
    assert model(repo, tmp_path, plan_kind="refactor").task_kind == "refactor"
    assert model(repo, tmp_path, plan_kind="fix").task_kind == "fix"
    assert model(repo, tmp_path).task_kind == "feat"
    assert model(repo, tmp_path, plan_kind="docs").task_kind == "feat"


def test_nontest_changed_lines_counts_only_nontest_paths(
    make_repo: MakeRepo, tmp_path: Path
) -> None:
    base = {"pkg/__init__.py": "", "pkg/m.py": FUNC, "tests/test_m.py": "x = 1\n"}
    head = {
        "pkg/m.py": "def f(a):\n    x = a + 2\n    return x\n",  # 1 added, 1 deleted
        "tests/test_m.py": "x = 2\ny = 3\nz = 4\n",
    }
    repo = make_repo(base, head)
    assert model(repo, tmp_path).nontest_changed_lines == 2


def test_added_lines_and_changed_paths(make_repo: MakeRepo, tmp_path: Path) -> None:
    head = "def f(a):\n    x = a + 2\n    return x\n\n\ndef g():\n    return 1\n"
    repo = make_repo({"pkg/__init__.py": "", "pkg/m.py": FUNC}, {"pkg/m.py": head, "n.txt": "n\n"})
    cm = model(repo, tmp_path)
    assert cm.added_lines("pkg/m.py") == frozenset({2, 4, 5, 6, 7})
    assert cm.added_lines("pkg/none.py") == frozenset()
    assert cm.changed_paths == ("n.txt", "pkg/m.py")
    assert ("A", "n.txt", None) in cm.status


def test_build_static_ctx_returns_indexes_and_change(make_repo: MakeRepo, tmp_path: Path) -> None:
    head = "def f(a):\n    x = a + 2\n    return x\n"
    repo = make_repo(
        {"pkg/__init__.py": "", "pkg/m.py": FUNC, "web/src/a.ts": "export const a = 1;\n"},
        {"pkg/m.py": head},
    )
    ctx = build_static_ctx(
        repo.path,
        repo.base,
        repo.head,
        cfg=cfg(),
        plan=None,
        report=None,
        scope=scope(),
        cache=BlobCache(tmp_path / "cache", "test"),
    )
    assert isinstance(ctx, StaticCtx)
    assert ctx.base.rev == repo.base and ctx.head.rev == repo.head
    assert "pkg.m.f" in ctx.base.functions and "pkg.m.f" in ctx.head.functions
    assert ctx.change.functions["pkg.m.f"].kind == "body"
    assert "ts-unavailable" in ctx.unverified
    assert (ctx.fork, ctx.tip, ctx.repo) == (repo.base, repo.head, repo.path)


def test_build_static_ctx_without_typescript_records_nothing(
    make_repo: MakeRepo, tmp_path: Path
) -> None:
    repo = make_repo({"pkg/__init__.py": "", "pkg/m.py": FUNC}, {"pkg/m.py": FUNC + "\n"})
    ctx = build_static_ctx(
        repo.path,
        repo.base,
        repo.head,
        cfg=cfg(),
        plan=None,
        report=None,
        scope=scope(),
        cache=BlobCache(tmp_path / "cache", "test"),
    )
    assert ctx.unverified == ()


def _patch_ts(monkeypatch: pytest.MonkeyPatch, indexer: TsIndexerFn) -> tuple[list[int], list[str]]:
    closed: list[int] = []
    seen: list[str] = []

    def index(sources: Mapping[str, str]) -> Mapping[str, TsFileFacts]:
        seen.extend(sorted(sources))
        return indexer(sources)

    def make(
        repo: Path, is_test: Callable[[str], bool]
    ) -> tuple[TsIndexerFn | None, Callable[[], None]]:
        return index, lambda: closed.append(1)

    monkeypatch.setattr(ts_bridge, "make_ts_indexer", make)
    return closed, seen


def test_build_static_ctx_shares_one_ts_process_and_closes_it(
    make_repo: MakeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(
        {"web/src/a.ts": "export function a() {}\nexport function b() {}\n"},
        {"web/src/a.ts": "export function a() {}\n"},
    )
    closed, seen = _patch_ts(monkeypatch, fake_ts)
    ctx = build_static_ctx(
        repo.path,
        repo.base,
        repo.head,
        cfg=cfg(),
        plan=None,
        report=None,
        scope=scope(),
        cache=BlobCache(tmp_path / "cache", "test"),
    )
    assert closed == [1]
    assert seen == ["web/src/a.ts", "web/src/a.ts"]
    assert ctx.change.removed_ts_exports == {"web/src/a.ts": ("b",)}
    assert "ts-unavailable" not in ctx.unverified


def test_build_static_ctx_closes_the_ts_process_when_a_build_fails(
    make_repo: MakeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo({"web/src/a.ts": "export const a = 1;\n"}, {})

    def broken(sources: Mapping[str, str]) -> Mapping[str, TsFileFacts]:
        raise ts_bridge.TsUnavailable("node died")

    closed, _seen = _patch_ts(monkeypatch, broken)
    ctx = build_static_ctx(
        repo.path,
        repo.base,
        repo.head,
        cfg=cfg(),
        plan=None,
        report=None,
        scope=scope(),
        cache=BlobCache(tmp_path / "cache", "test"),
    )
    assert closed == [1]
    assert "ts-unavailable" in ctx.unverified


def test_build_static_ctx_applies_overrides(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = make_repo(
        {"pkg/__init__.py": "", "pkg/m.py": FUNC, "pkg/gone.py": "X = 1\n"},
        {"README.md": "r\n"},
    )
    seeded = b"def f(a):\n    x = a - 1\n    return x\n"
    ctx = build_static_ctx(
        repo.path,
        repo.base,
        repo.head,
        cfg=cfg(),
        plan=None,
        report=None,
        scope=scope(),
        cache=BlobCache(tmp_path / "cache", "test"),
        overrides={"pkg/m.py": seeded, "pkg/new.py": b"Y = 2\n", "pkg/gone.py": None},
    )
    cm = ctx.change
    assert ("M", "pkg/m.py", None) in cm.status
    assert ("A", "pkg/new.py", None) in cm.status
    assert ("D", "pkg/gone.py", None) in cm.status
    assert ("A", "README.md", None) in cm.status
    assert cm.added_lines("pkg/m.py") == frozenset({2})
    assert cm.added_lines("pkg/new.py") == frozenset({1})
    assert cm.functions["pkg.m.f"].kind == "body"
    assert cm.functions["pkg.m.f"].changed_head_lines == (2,)
    assert cm.removed_names == {"pkg.gone": ("X",)}
    assert cm.nontest_changed_lines == 1 + 2 + 1 + 1  # README, m.py (+1 -1), new, gone


def test_file_that_stops_parsing_reports_no_function_changes(
    make_repo: MakeRepo, tmp_path: Path
) -> None:
    repo = make_repo({"pkg/__init__.py": "", "pkg/m.py": FUNC}, {"pkg/m.py": "def f(:\n"})
    cm = model(repo, tmp_path)
    assert cm.functions == {}
    assert cm.removed_names == {}
    assert cm.changed_paths == ("pkg/m.py",)
