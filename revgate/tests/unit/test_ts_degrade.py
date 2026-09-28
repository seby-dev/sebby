"""TypeScript facts degrade to `None` without Node or the compiler; these run everywhere, CI too."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from conftest import MakeRepo

from revgate import gitio
from revgate.spi import ts_bridge


def _is_test(path: str) -> bool:
    return ".test." in path


def test_missing_typescript_lib_degrades(
    make_repo: MakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo({"src/a.py": "x = 1\n"}, {}).path
    assert ts_bridge.find_ts_lib(repo) is None
    indexer, close = ts_bridge.make_ts_indexer(repo, _is_test)
    assert indexer is None
    close()

    # With the library present but no Node on PATH, the indexer is still None.
    lib = repo / "web" / "node_modules" / "typescript"
    lib.mkdir(parents=True)
    (lib / "package.json").write_text('{"name": "typescript"}\n')
    assert ts_bridge.find_ts_lib(repo) == lib
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    indexer, close = ts_bridge.make_ts_indexer(repo, _is_test)
    assert indexer is None
    close()


def test_find_ts_lib_falls_back_to_the_main_worktree(make_repo: MakeRepo, tmp_path: Path) -> None:
    fixture = make_repo({"src/a.py": "x = 1\n"}, {})
    lib = fixture.path / "web" / "node_modules" / "typescript"
    lib.mkdir(parents=True)
    (lib / "package.json").write_text('{"name": "typescript"}\n')
    linked = tmp_path / "linked-wt"
    gitio.run_git(fixture.path, "worktree", "add", "-q", "--detach", str(linked))
    assert ts_bridge.find_ts_lib(linked) == lib.resolve()


def test_find_ts_lib_outside_a_repository_is_none(tmp_path: Path) -> None:
    assert ts_bridge.find_ts_lib(tmp_path / "nowhere") is None


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_a_broken_library_raises_ts_unavailable(tmp_path: Path) -> None:
    lib = tmp_path / "typescript"
    lib.mkdir()
    (lib / "package.json").write_text('{"name": "typescript", "main": "missing.js"}\n')
    with pytest.raises(ts_bridge.TsUnavailable), ts_bridge.TsIndexer(lib) as idx:
        idx.facts({"a.ts": "export const a = 1;\n"}, _is_test)
