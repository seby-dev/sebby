from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from conftest import MakeRepo, git

from revgate.config import Config, load_config, parse_config
from revgate.spi import libsrc
from revgate.spi.cache import BlobCache
from revgate.spi.facts import TsFileFacts
from revgate.spi.link import TreeIndex

BASE = {
    "pkg/__init__.py": "",
    "pkg/a.py": "def f():\n    return 1\n",
    "pkg/b.py": "from pkg.a import f as g\n\n\ndef use_b():\n    return g()\n",
    "pkg/c.py": "import pkg.a as A\n\n\ndef use_c():\n    A.f()\n",
    "pkg/other.py": "def f():\n    return 2\n",
    "pkg/shapes.py": (
        "class Base:\n"
        "    def m(self):\n"
        "        return 1\n"
        "\n"
        "\n"
        "class Sub(Base):\n"
        "    def go(self):\n"
        "        return self.m()\n"
    ),
    "tests/test_a.py": "from pkg.a import f\n\n\ndef test_f():\n    assert f() == 1\n",
}


def cfg() -> Config:
    return parse_config(None)


def build(
    repo: Path,
    rev: str,
    tmp_path: Path,
    overrides: Mapping[str, bytes | None] | None = None,
) -> TreeIndex:
    cache = BlobCache(tmp_path / "cache", "test")
    return TreeIndex.build(repo, rev, cache, cfg(), overrides=overrides)


def test_callers_follow_aliases_and_module_imports(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = make_repo(BASE, {})
    idx = build(repo.path, repo.head, tmp_path)
    sites = {(c.path, c.caller) for c in idx.callers("pkg.a.f")}
    assert ("pkg/b.py", "pkg.b.use_b") in sites
    assert ("pkg/c.py", "pkg.c.use_c") in sites
    assert ("tests/test_a.py", "tests.test_a.test_f") in sites
    assert all(c.path != "pkg/other.py" for c in idx.callers("pkg.a.f"))


def test_self_call_resolves_to_the_base_method(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = make_repo(BASE, {})
    idx = build(repo.path, repo.head, tmp_path)
    [site] = idx.callers("pkg.shapes.Base.m")
    assert site.caller == "pkg.shapes.Sub.go"
    assert idx.mro("pkg.shapes.Sub") == ["pkg.shapes.Sub", "pkg.shapes.Base"]
    assert idx.classes["pkg.shapes.Sub"].resolved_bases == ("pkg.shapes.Base",)


def test_receiver_typed_by_annotation_and_constructor(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = make_repo(
        {
            **BASE,
            "pkg/typed.py": (
                "from pkg.shapes import Base, Sub\n"
                "\n"
                "\n"
                "def by_annotation(b: Base):\n"
                "    return b.m()\n"
                "\n"
                "\n"
                "def by_constructor():\n"
                "    s = Sub()\n"
                "    return s.go()\n"
            ),
        },
        {},
    )
    idx = build(repo.path, repo.head, tmp_path)
    assert {c.caller for c in idx.callers("pkg.shapes.Base.m")} == {
        "pkg.shapes.Sub.go",
        "pkg.typed.by_annotation",
    }
    assert [c.caller for c in idx.callers("pkg.shapes.Sub.go")] == ["pkg.typed.by_constructor"]
    assert [c.caller for c in idx.callers("pkg.shapes.Sub")] == ["pkg.typed.by_constructor"]


def test_references_exclude_tests_when_prod_only(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = make_repo(BASE, {})
    idx = build(repo.path, repo.head, tmp_path)
    prod = {r.path for r in idx.references("f", prod_only=True)}
    every = {r.path for r in idx.references("f", prod_only=False)}
    assert "tests/test_a.py" not in prod
    assert "tests/test_a.py" in every
    assert "pkg/c.py" in prod


def test_resolve(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = make_repo(
        {**BASE, "pkg/lib.py": "from music21 import expressions\nfrom pkg.a import f\n"}, {}
    )
    idx = build(repo.path, repo.head, tmp_path)
    assert idx.resolve("f") == ["pkg.a.f", "pkg.other.f"]
    assert idx.resolve("g", from_module="pkg.b") == ["pkg.a.f"]
    assert idx.resolve("f", from_module="pkg.lib") == ["pkg.a.f"]
    assert idx.resolve("A.f", from_module="pkg.c") == ["pkg.a.f"]
    assert idx.resolve("expressions.Turn", from_module="pkg.lib") == ["music21.expressions.Turn"]
    assert idx.resolve("nowhere", from_module="pkg.lib") == []
    assert idx.module_of("pkg/b.py") == "pkg.b"
    assert idx.module_of("README.md") is None


def test_re_export_chain(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = make_repo(
        {
            "pkg/__init__.py": "from pkg.a import f\n",
            "pkg/a.py": "def f():\n    return 1\n",
            "use.py": "import pkg\nfrom pkg import f as ff\n\n\ndef u():\n    pkg.f()\n    ff()\n",
        },
        {},
    )
    idx = build(repo.path, repo.head, tmp_path)
    assert [c.line for c in idx.callers("pkg.a.f")] == [6, 7]


def test_overrides_replace_add_and_remove(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = make_repo(BASE, {})
    idx = build(
        repo.path,
        repo.head,
        tmp_path,
        overrides={"pkg/a.py": b"def h(): pass\n", "pkg/other.py": None, "new.py": b"X = 1\n"},
    )
    assert "pkg.a.h" in idx.functions
    assert "pkg.a.f" not in idx.functions
    assert "pkg/other.py" not in idx.py
    assert "new.X" in idx.bindings


def test_parse_error_is_unverified_not_raised(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = make_repo({**BASE, "scripts/old.py": "print 'hello'\n"}, {})
    idx = build(repo.path, repo.head, tmp_path)
    assert "parse-error:scripts/old.py" in idx.unverified
    assert idx.py["scripts/old.py"].parse_error is not None
    assert "pkg.a.f" in idx.functions


def test_typescript_without_an_indexer_is_unverified(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = make_repo({**BASE, "web/src/a.ts": "export const x = 1;\n"}, {})
    idx = build(repo.path, repo.head, tmp_path)
    assert "ts-unavailable" in idx.unverified
    assert idx.ts == {}


def test_typescript_indexer_called_once_and_cached(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = make_repo(
        {
            **BASE,
            "web/src/a.ts": "export const x = 1;\n",
            "web/src/b.tsx": "export const y = 2;\n",
            "web/node_modules/lib/index.ts": "export {};\n",
        },
        {},
    )
    batches: list[list[str]] = []

    def indexer(sources: Mapping[str, str]) -> Mapping[str, TsFileFacts]:
        batches.append(sorted(sources))
        return {p: TsFileFacts(p, False, None, (), ("x",), (), (), ()) for p in sorted(sources)}

    cache = BlobCache(tmp_path / "cache", "test")
    first = TreeIndex.build(repo.path, repo.head, cache, cfg(), ts_indexer=indexer)
    second = TreeIndex.build(repo.path, repo.head, cache, cfg(), ts_indexer=indexer)
    assert batches == [["web/src/a.ts", "web/src/b.tsx"]]
    assert sorted(first.ts) == sorted(second.ts) == ["web/src/a.ts", "web/src/b.tsx"]
    assert "ts-unavailable" not in first.unverified


def test_warm_build_equals_cold_build(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = make_repo(BASE, {})
    cache = BlobCache(tmp_path / "cache", "test")
    cold = TreeIndex.build(repo.path, repo.head, cache, cfg())
    warm = TreeIndex.build(repo.path, repo.head, cache, cfg())
    assert cold.functions == warm.functions
    assert cold.py == warm.py


def test_same_blob_at_two_paths_keeps_each_module(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = make_repo({"one/x.py": "def f():\n    pass\n", "two/x.py": "def f():\n    pass\n"}, {})
    idx = build(repo.path, repo.head, tmp_path)
    assert {"one.x.f", "two.x.f"} <= set(idx.functions)


def test_subclasses_from_a_library(
    make_repo: MakeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site"
    (site / "fakelib").mkdir(parents=True)
    (site / "fakelib" / "__init__.py").write_text("")
    (site / "fakelib" / "expressions.py").write_text(
        "class Turn:\n    pass\n\n\nclass InvertedTurn(Turn):\n    pass\n"
    )
    monkeypatch.setattr(libsrc, "find_site_packages", lambda repo: site)
    repo = make_repo(BASE, {}, review_toml='[semantic]\nlibrary_hierarchies = ["fakelib"]\n')
    config = load_config(repo.path, rev=repo.head)
    idx = TreeIndex.build(repo.path, repo.head, BlobCache(tmp_path / "c", "t"), config)
    assert idx.subclasses("fakelib.expressions.Turn") == ["fakelib.expressions.InvertedTurn"]
    assert idx.subclasses("otherlib.X") == []


def test_subclasses_missing_library_is_unverified(
    make_repo: MakeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(libsrc, "find_site_packages", lambda repo: None)
    repo = make_repo(BASE, {}, review_toml='[semantic]\nlibrary_hierarchies = ["fakelib"]\n')
    config = load_config(repo.path, rev=repo.head)
    idx = TreeIndex.build(repo.path, repo.head, BlobCache(tmp_path / "c", "t"), config)
    assert idx.subclasses("fakelib.expressions.Turn") == []
    assert "library-missing:fakelib" in idx.unverified


def test_sibling_module_import(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = make_repo(
        {
            "tests/helper.py": "def render():\n    return 1\n",
            "tests/test_x.py": "from helper import render\n\n\ndef test_r():\n    render()\n",
            "json_user.py": "import json\n\n\ndef u():\n    json.dumps(1)\n",
        },
        {},
    )
    idx = build(repo.path, repo.head, tmp_path)
    assert [c.caller for c in idx.callers("tests.helper.render")] == ["tests.test_x.test_r"]
    assert [c.caller for c in idx.callers("json.dumps")] == ["json_user.u"]


TYPED = {
    "m/model.py": (
        "class Note:\n"
        "    def pitch(self):\n"
        "        return 1\n"
        "\n"
        "\n"
        "class Bar:\n"
        "    notes: list[Note]\n"
        "    first: Note\n"
        "\n"
        "    def walk(self):\n"
        "        for n in self.notes:\n"
        "            n.pitch()\n"
        "        self.first.pitch()\n"
    ),
    "m/child.py": (
        "from m.model import Bar\n"
        "\n"
        "\n"
        "class Loud(Bar):\n"
        "    def walk(self):\n"
        "        super().walk()\n"
        "\n"
        "\n"
        "def outer(bars: list[Bar]):\n"
        "    def inner():\n"
        "        return 1\n"
        "\n"
        "    for b in bars:\n"
        "        b.walk()\n"
        "    return inner()\n"
        "\n"
        "\n"
        "def untyped(x, p):\n"
        "    x.pitch()\n"
        "    x.walk()\n"
        "\n"
        "\n"
        "def external():\n"
        "    import pathlib\n"
        "    q = pathlib.Path('.')\n"
        "    q.pitch()\n"
    ),
}


def test_loops_over_annotated_fields_and_sequences(make_repo: MakeRepo, tmp_path: Path) -> None:
    idx = build(make_repo(TYPED, {}).path, "HEAD", tmp_path)
    pitch = {(c.caller, c.line) for c in idx.callers("m.model.Note.pitch")}
    assert ("m.model.Bar.walk", 12) in pitch  # for n in self.notes
    assert ("m.model.Bar.walk", 13) in pitch  # self.first.pitch()
    assert ("m.child.outer", 14) in {(c.caller, c.line) for c in idx.callers("m.model.Bar.walk")}


def test_super_and_nested_functions(make_repo: MakeRepo, tmp_path: Path) -> None:
    idx = build(make_repo(TYPED, {}).path, "HEAD", tmp_path)
    assert "m.child.Loud.walk" in {c.caller for c in idx.callers("m.model.Bar.walk")}
    assert [c.caller for c in idx.callers("m.child.outer.inner")] == ["m.child.outer"]
    assert idx.mro("m.child.Loud") == ["m.child.Loud", "m.model.Bar"]


def test_name_fallback_only_for_untyped_receivers(make_repo: MakeRepo, tmp_path: Path) -> None:
    idx = build(make_repo(TYPED, {}).path, "HEAD", tmp_path)
    untyped = [c for c in idx.callers("m.model.Note.pitch") if c.caller == "m.child.untyped"]
    assert len(untyped) == 1
    walks = [c for c in idx.callers("m.model.Bar.walk") if c.caller == "m.child.untyped"]
    [site] = walks
    assert site.resolved == ("m.child.Loud.walk", "m.model.Bar.walk")
    external = [c for c in idx.callers("m.model.Note.pitch") if c.caller == "m.child.external"]
    assert external == []
    assert [c.caller for c in idx.callers("pathlib.Path.pitch")] == ["m.child.external"]


def test_library_hierarchy_follows_re_exports(tmp_path: Path) -> None:
    site = tmp_path / "site"
    (site / "lib" / "sub").mkdir(parents=True)
    (site / "lib" / "__init__.py").write_text("from lib.base import Root\n")
    (site / "lib" / "base.py").write_text("class Root:\n    pass\n\n\nclass Mid(Root):\n    pass\n")
    (site / "lib" / "sub" / "__init__.py").write_text("")
    (site / "lib" / "sub" / "leaf.py").write_text(
        "import lib\nfrom ..base import Mid\n\n\nclass Leaf(Mid):\n    pass\n\n\n"
        "class Other(lib.Root):\n    pass\n"
    )
    (site / "lib" / "broken.py").write_text("class (:\n")
    hier = libsrc.library_hierarchy(site, "lib")
    assert hier["lib.sub.leaf.Leaf"] == ("lib.base.Mid",)
    assert hier["lib.sub.leaf.Other"] == ("lib.base.Root",)
    assert libsrc.strict_subclasses(hier, "lib.base.Root") == (
        "lib.base.Mid",
        "lib.sub.leaf.Leaf",
        "lib.sub.leaf.Other",
    )
    assert libsrc.canonical_class(site, "lib", "lib.Root") == "lib.base.Root"


def test_find_site_packages_prefers_the_worktree_then_the_main_checkout(
    git_repo: Path, tmp_path: Path
) -> None:
    assert libsrc.find_site_packages(git_repo) is None
    main_site = git_repo / ".venv" / "lib" / "python3.12" / "site-packages"
    main_site.mkdir(parents=True)
    wt = tmp_path / "wt"
    git(git_repo, "worktree", "add", "-q", str(wt))
    assert libsrc.find_site_packages(wt) == main_site
    own = wt / ".venv" / "lib" / "python3.13" / "site-packages"
    own.mkdir(parents=True)
    assert libsrc.find_site_packages(wt) == own
