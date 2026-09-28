"""`docs.dangling_code_ref`: code that still imports or reads a removed name."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from conftest import MakeRepo, RepoFixture

from revgate.config import parse_config
from revgate.model import Finding, Grade, TaskScope, Tier
from revgate.rules.registry import load_tier_table
from revgate.spi.cache import BlobCache
from revgate.static.ctx import StaticCtx, build_static_ctx
from revgate.static.docs_refs import DanglingCodeRefRule
from revgate.verdict.route import route

UTIL_BASE = "def helper():\n    return 1\n\n\ndef other():\n    return 2\n"
UTIL_HEAD = "def other():\n    return 2\n"
SCRIPT = "from pkg.util import helper\n\nhelper()\n"
BASE = {"pkg/__init__.py": "", "pkg/util.py": UTIL_BASE}


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
    return [o for o in DanglingCodeRefRule().check(ctx) if isinstance(o, Finding)]


def test_rule_ids() -> None:
    rule = DanglingCodeRefRule()
    assert rule.ids == frozenset({"docs.dangling_code_ref"})
    assert rule.languages == frozenset({"py", "ts"})


def test_dangling_code_ref_scope(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = make_repo({**BASE, "scripts/x.py": SCRIPT}, {"pkg/util.py": UTIL_HEAD})
    found = _findings(_ctx(repo, tmp_path))
    assert [(f.rule, f.file, f.line) for f in found] == [
        ("docs.dangling_code_ref", "scripts/x.py", 1)
    ]
    (f,) = found
    assert f.grade is Grade.E1_EXACT
    assert "helper" in f.message

    tiers = load_tier_table()
    cfg = parse_config(b"")  # a .review.toml exists, so the table's tiers apply
    owned = TaskScope("T1", owns=frozenset({"scripts/x.py"}), runs=frozenset())
    unowned = TaskScope("T1", owns=frozenset({"pkg/util.py"}), runs=frozenset())
    blocking = route(f, tiers, cfg, owned)
    assert blocking.tier is Tier.BLOCKING
    assert "implementer" in blocking.audience
    advisory = route(f, tiers, cfg, unowned)
    assert advisory.tier is Tier.ADVISORY
    assert "controller" in advisory.audience
    assert "implementer" not in advisory.audience


def test_local_rebinding_is_silent(make_repo: MakeRepo, tmp_path: Path) -> None:
    script = (
        "try:\n"
        "    from pkg.util import helper\n"
        "except ImportError:\n"
        "\n"
        "    def helper():\n"
        "        return 0\n"
        "\n"
        "\n"
        "helper()\n"
    )
    repo = make_repo({**BASE, "scripts/x.py": script}, {"pkg/util.py": UTIL_HEAD})
    assert _findings(_ctx(repo, tmp_path)) == []


def test_module_attribute_read_fires(make_repo: MakeRepo, tmp_path: Path) -> None:
    script = "from pkg import util\n\n\ndef run():\n    return util.helper()\n"
    repo = make_repo({**BASE, "scripts/y.py": script}, {"pkg/util.py": UTIL_HEAD})
    found = _findings(_ctx(repo, tmp_path))
    assert [(f.file, f.line) for f in found] == [("scripts/y.py", 5)]


def test_reexported_name_is_silent(make_repo: MakeRepo, tmp_path: Path) -> None:
    head = {
        "pkg/util.py": "from pkg.helpers import helper\n\n\n" + UTIL_HEAD,
        "pkg/helpers.py": "def helper():\n    return 1\n",
    }
    repo = make_repo({**BASE, "scripts/x.py": SCRIPT}, head)
    assert _findings(_ctx(repo, tmp_path)) == []


def test_still_defined_name_is_silent(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = make_repo({**BASE, "scripts/x.py": SCRIPT}, {"pkg/util.py": UTIL_BASE + "\n"})
    assert _findings(_ctx(repo, tmp_path)) == []


def _find_ts_lib() -> Path | None:
    candidates = [
        os.environ.get("REVGATE_TEST_TSLIB"),
        *(
            str(p)
            for p in sorted((Path.home() / "Developer").glob("*/web/node_modules/typescript"))
        ),
    ]
    for c in candidates:
        if c and (Path(c) / "package.json").is_file():
            return Path(c)
    return None


def test_typescript_import_of_a_removed_export_fires(make_repo: MakeRepo, tmp_path: Path) -> None:
    lib = _find_ts_lib()
    if shutil.which("node") is None or lib is None:
        pytest.skip("needs Node and a typescript package")
    base = {
        "web/src/a.ts": "export function foo() {}\nexport function bar() {}\n",
        "web/src/b.ts": 'import { foo } from "./a";\n\nfoo();\n',
        "web/src/c.ts": 'import { bar } from "./a";\n\nbar();\n',
    }
    repo = make_repo(base, {"web/src/a.ts": "export function bar() {}\n"})
    (repo.path / "web" / "node_modules").mkdir(parents=True)
    (repo.path / "web" / "node_modules" / "typescript").symlink_to(lib)
    found = _findings(_ctx(repo, tmp_path))
    assert [(f.file, f.line) for f in found] == [("web/src/b.ts", 1)]
    assert "foo" in found[0].message
