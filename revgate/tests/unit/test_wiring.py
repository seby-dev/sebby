"""`WiringRule`: a new production symbol with no production reference, classified by the plan."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from conftest import MakeRepo, RepoFixture

from revgate.config import Config, parse_config
from revgate.model import Finding, Grade, Obligation, RuleOutput, Source, TaskScope, Tier
from revgate.plan import task_from_plan
from revgate.rules.registry import StaticRule, load_tier_table
from revgate.spi.cache import BlobCache
from revgate.static.ctx import StaticCtx, build_static_ctx
from revgate.static.wiring import WiringRule, production_references
from revgate.verdict.route import route

PLAN_PATH = "docs/plans/plan.md"
BASE_MARKS = "def bar_cells(x):\n    return [x]\n"
RELOCATE = "\n\ndef relocate(cells):\n    return list(reversed(cells))\n"
TEST_MARKS = "from pkg.marks import relocate\n\n\ndef test_relocate():\n    assert relocate([1])\n"

PLANNED_HERE = """# Plan

### Task 1: Relocate marks

**Files:**
- Modify: `pkg/marks.py`
- Test: `tests/test_marks.py`

- [ ] **Step 1: Implement**

Replace `bar_cells`' body:

```python
cells = [x]
return relocate(cells)
```
"""

DEFERRED = """# Plan

### Task 1: Add relocate

**Files:**
- Modify: `pkg/marks.py`
- Test: `tests/test_marks.py`

```python
def relocate(cells):
    return list(reversed(cells))
```

### Task 2: Something else

**Files:**
- Modify: `pkg/other.py`

### Task 3: Wire relocate

**Files:**
- Modify: `pkg/cells.py`

```python
def bar_cells(x):
    return relocate([x])
```
"""


def nondefault_cfg() -> Config:
    return parse_config(b"[paths]\n")


def build(
    repo: RepoFixture, tmp_path: Path, plan_md: str | None, task_id: str = "1", **owns: str
) -> StaticCtx:
    plan = task_from_plan(plan_md, PLAN_PATH, task_id) if plan_md is not None else None
    owned = frozenset(plan.owns.all()) if plan is not None else frozenset()
    return build_static_ctx(
        repo.path,
        repo.base,
        repo.head,
        cfg=nondefault_cfg(),
        plan=plan,
        report=None,
        scope=TaskScope(task_id=task_id, owns=owned, runs=frozenset()),
        cache=BlobCache(tmp_path / "cache", "test"),
    )


def outputs(ctx: StaticCtx) -> list[RuleOutput]:
    rule = WiringRule()
    assert isinstance(rule, StaticRule)
    return list(rule.check(ctx))


def findings(out: list[RuleOutput]) -> list[Finding]:
    return [o for o in out if isinstance(o, Finding)]


def planned_repo(make_repo: MakeRepo, extra: Mapping[str, str | None] | None = None) -> RepoFixture:
    head: dict[str, str | None] = {
        "pkg/marks.py": BASE_MARKS + RELOCATE,
        "tests/test_marks.py": TEST_MARKS,
    }
    head.update(extra or {})
    return make_repo({"pkg/__init__.py": "", "pkg/marks.py": BASE_MARKS}, head, plan=PLANNED_HERE)


def test_ids_and_languages() -> None:
    rule = WiringRule()
    assert rule.ids == frozenset({"wiring.unwired_planned_here", "wiring.unwired_new_symbol"})
    assert rule.languages == frozenset({"py", "ts"})


def test_unwired_planned_here_fires(make_repo: MakeRepo, tmp_path: Path) -> None:
    ctx = build(planned_repo(make_repo), tmp_path, PLANNED_HERE)
    found = findings(outputs(ctx))
    assert [f.rule for f in found] == ["wiring.unwired_planned_here"]
    f = found[0]
    assert (f.file, f.line, f.symbol) == ("pkg/marks.py", 5, "relocate")
    assert (f.grade, f.source, f.impact) == (Grade.E1_EXACT, Source.DECLARED, "critical")
    assert f.message == "relocate is new and nothing in production calls it"
    assert f.fix == "call relocate from bar_cells, as the brief's code block does"
    assert "0 production refs" in f.evidence and "bar_cells -> relocate" in f.evidence
    routed = route(f, load_tier_table(), ctx.cfg, ctx.scope)
    assert routed.tier is Tier.ADVISORY and routed.review
    assert routed.audience == frozenset({"implementer", "wave", "log"})


def test_unwired_planned_here_silent_when_wired(make_repo: MakeRepo, tmp_path: Path) -> None:
    wired = "def bar_cells(x):\n    return relocate([x])\n" + RELOCATE
    repo = planned_repo(make_repo, {"pkg/marks.py": wired})
    assert findings(outputs(build(repo, tmp_path, PLANNED_HERE))) == []


def test_unwired_planned_here_silent_with_string_registry(
    make_repo: MakeRepo, tmp_path: Path
) -> None:
    registry = 'HANDLERS = {"relocate": "pkg.marks"}\n'
    repo = planned_repo(make_repo, {"pkg/registry.py": registry})
    assert findings(outputs(build(repo, tmp_path, PLANNED_HERE))) == []


def test_unwired_deferred_to_later_owner(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = make_repo(
        {"pkg/__init__.py": "", "pkg/marks.py": BASE_MARKS},
        {"pkg/marks.py": BASE_MARKS + RELOCATE, "tests/test_marks.py": TEST_MARKS},
        plan=DEFERRED,
    )
    out = outputs(build(repo, tmp_path, DEFERRED))
    assert findings(out) == []
    obligations = [o for o in out if isinstance(o, Obligation)]
    assert len(obligations) == 1
    ob = obligations[0]
    assert (ob.status, ob.owner) == ("deferred", "3")
    assert ob.anchor.path == "pkg/marks.py"
    assert ob.params["symbol"] == "relocate"


def test_unplanned_new_symbol_is_generic(make_repo: MakeRepo, tmp_path: Path) -> None:
    plan = PLANNED_HERE.replace("return relocate(cells)", "return cells")
    repo = make_repo(
        {"pkg/__init__.py": "", "pkg/marks.py": BASE_MARKS},
        {"pkg/marks.py": BASE_MARKS + RELOCATE, "tests/test_marks.py": TEST_MARKS},
        plan=plan,
    )
    found = findings(outputs(build(repo, tmp_path, plan)))
    assert [f.rule for f in found] == ["wiring.unwired_new_symbol"]
    assert found[0].source is Source.GENERIC
    assert "tests/test_marks.py" in found[0].evidence


def test_excluded_candidates_are_skipped(make_repo: MakeRepo, tmp_path: Path) -> None:
    base = BASE_MARKS + "\n\nclass Base:\n    def run(self):\n        return 1\n"
    head = (
        base
        + "\n\nclass Child(Base):\n    def run(self):\n        return 2\n"
        + "\n    def __repr__(self):\n        return 'c'\n"
        + "\n\n@app.route('/x')\ndef handler():\n    return 1\n"
        + "\n\ndef outer():\n    def inner():\n        return 1\n    return inner\n"
        + "\n\nBOTH = [Child, outer]\n"
    )
    plan = PLANNED_HERE.replace("return relocate(cells)", "return cells")
    repo = make_repo(
        {"pkg/__init__.py": "", "pkg/marks.py": base}, {"pkg/marks.py": head}, plan=plan
    )
    assert findings(outputs(build(repo, tmp_path, plan))) == []


def test_production_references_exclude_own_definition(make_repo: MakeRepo, tmp_path: Path) -> None:
    recursive = "\n\ndef relocate(cells):\n    return relocate(cells[1:]) if cells else []\n"
    repo = planned_repo(make_repo, {"pkg/marks.py": BASE_MARKS + recursive})
    ctx = build(repo, tmp_path, PLANNED_HERE)
    assert production_references(ctx, "relocate", "pkg/marks.py", (5, 6)) == []
    assert len(production_references(ctx, "relocate", "other.py", (1, 1))) == 1


def test_planless_context_yields_nothing(make_repo: MakeRepo, tmp_path: Path) -> None:
    ctx = build(planned_repo(make_repo), tmp_path, None)
    assert outputs(ctx) == []
