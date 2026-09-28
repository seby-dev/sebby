"""TypeScript facts from the repository's own compiler; needs Node and a `typescript` package."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from revgate import gitio
from revgate.spi import ts_bridge
from revgate.spi.facts import TsFileFacts


def _find_lib() -> Path | None:
    candidates = [
        os.environ.get("REVGATE_TEST_TSLIB"),
        str(Path.home() / "Developer/staff2solfa/web/node_modules/typescript"),
    ]
    for c in candidates:
        if c and (Path(c) / "package.json").is_file():
            return Path(c)
    return None


TS_LIB = _find_lib()
if shutil.which("node") is None or TS_LIB is None:
    pytest.skip("needs Node and a typescript package", allow_module_level=True)
assert TS_LIB is not None


def _is_test(path: str) -> bool:
    return ".test." in path


@pytest.fixture(scope="module")
def indexer() -> Iterator[ts_bridge.TsIndexer]:
    assert TS_LIB is not None
    with ts_bridge.TsIndexer(TS_LIB) as idx:
        yield idx


def _one(idx: ts_bridge.TsIndexer, path: str, source: str) -> TsFileFacts:
    out = idx.facts({path: source}, _is_test)
    assert set(out) == {path}
    return out[path]


def test_exported_function_with_params(indexer: ts_bridge.TsIndexer) -> None:
    f = _one(indexer, "src/pitch.ts", "export function modulateDegree(a: number, b: string) {}\n")
    assert f.parse_error is None
    assert f.is_test is False
    (fn,) = f.functions
    assert fn.qualname == "src/pitch.ts::modulateDegree"
    assert fn.name == "modulateDegree"
    assert fn.path == "src/pitch.ts"
    assert fn.params == ("a", "b")
    assert fn.exported is True
    assert fn.lineno == 1
    assert "modulateDegree" in f.exports


def test_const_arrow_is_a_function(indexer: ts_bridge.TsIndexer) -> None:
    f = _one(indexer, "src/a.ts", "const helper = (x: number) => x + 1;\nexport { helper as h };\n")
    (fn,) = f.functions
    assert fn.name == "helper"
    assert fn.params == ("x",)
    assert fn.exported is False
    assert f.exports == ("h",)


def test_import_triple(indexer: ts_bridge.TsIndexer) -> None:
    f = _one(
        indexer,
        "src/grid.ts",
        'import { voicePriority as vp } from "./voices";\n'
        'import React from "react";\n'
        'import * as M from "./modulate";\n',
    )
    assert ("./voices", "voicePriority", "vp") in f.imports
    assert ("react", "default", "React") in f.imports
    assert ("./modulate", "*", "M") in f.imports


def test_call_records_its_enclosing_function(indexer: ts_bridge.TsIndexer) -> None:
    f = _one(
        indexer,
        "src/b.ts",
        "export function outer() {\n  try {\n    inner(1);\n  } catch {}\n  a.b.c();\n}\ntop();\n",
    )
    calls = {c.callee: c for c in f.calls}
    assert calls["inner"].caller == "src/b.ts::outer"
    assert calls["inner"].line == 3
    assert calls["inner"].in_try is True
    assert calls["inner"].path == "src/b.ts"
    assert calls["a.b.c"].caller == "src/b.ts::outer"
    assert calls["a.b.c"].in_try is False
    assert calls["top"].caller == "src/b.ts::<module>"


def test_string_and_attribute_refs(indexer: ts_bridge.TsIndexer) -> None:
    f = _one(
        indexer,
        "src/c.ts",
        'const k = "relocate";\nconst s = "two words";\nfunction g() { return pitch.modulate; }\n',
    )
    strings = [r for r in f.refs if r.kind == "string"]
    assert [r.name for r in strings] == ["relocate"]
    assert strings[0].line == 1
    attrs = [r for r in f.refs if r.kind == "attribute"]
    assert [(r.name, r.text, r.in_symbol) for r in attrs] == [
        ("modulate", "pitch.modulate", "src/c.ts::g")
    ]
    idents = {r.name for r in f.refs if r.kind == "identifier"}
    assert {"k", "pitch"} <= idents


def test_syntax_error_sets_parse_error(indexer: ts_bridge.TsIndexer) -> None:
    f = _one(indexer, "src/bad.ts", "export function (\n")
    assert f.parse_error is not None
    assert f.parse_error.startswith("1:") or f.parse_error.startswith("2:")


def test_tsx_parses_jsx(indexer: ts_bridge.TsIndexer) -> None:
    f = _one(
        indexer,
        "src/View.test.tsx",
        'export const View = () => <div className="x">{label(1)}</div>;\n',
    )
    assert f.parse_error is None
    assert f.is_test is True
    assert [fn.name for fn in f.functions] == ["View"]
    assert [c.callee for c in f.calls] == ["label"]
    assert f.calls[0].caller == "src/View.test.tsx::View"


def test_many_files_in_one_call_are_deterministic(indexer: ts_bridge.TsIndexer) -> None:
    files = {f"src/m{i}.ts": f"export function f{i}() {{ g{i}(); }}\n" for i in range(30)}
    first = indexer.facts(files, _is_test)
    second = indexer.facts(files, _is_test)
    assert first == second
    assert sorted(first) == sorted(files)
    assert first["src/m7.ts"].functions[0].name == "f7"


def test_make_ts_indexer_finds_the_repository_library(tmp_path: Path, git_env: None) -> None:
    assert TS_LIB is not None
    repo = tmp_path / "repo"
    (repo / "web" / "node_modules").mkdir(parents=True)
    (repo / "web" / "node_modules" / "typescript").symlink_to(TS_LIB)
    gitio.run_git(repo, "init", "-q")
    index, close = ts_bridge.make_ts_indexer(repo, _is_test)
    try:
        assert index is not None
        out = index({"web/src/a.ts": "export const a = () => 1;\n"})
        assert out["web/src/a.ts"].functions[0].qualname == "web/src/a.ts::a"
    finally:
        close()
