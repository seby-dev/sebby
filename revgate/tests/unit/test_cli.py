"""The command line: `--version`, `plan-lint`, `mark`, `label`, and `recheck`."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import MakeRepo, RepoFixture, git
from test_plan_lint import build_plan, one, valid_waves

from revgate.cli import main
from revgate.gitio import common_dir
from revgate.labels import rule_stats
from revgate.model import run_file_from_json
from revgate.store import ledger_entries, run_file_path

SHAPES = "class Base:\n    pass\n\n\nclass Sub(Base):\n    pass\n"
USE_BASE = 'from pkg.shapes import Base, Sub\n\n\ndef kind(x):\n    return "none"\n'
SHADOWED = (
    "from pkg.shapes import Base, Sub\n\n\ndef kind(x):\n"
    '    if isinstance(x, Base):\n        return "base"\n'
    '    elif isinstance(x, Sub):\n        return "sub"\n'
    '    return "none"\n'
)
ORDERED = (
    "from pkg.shapes import Base, Sub\n\n\ndef kind(x):\n"
    '    if isinstance(x, Sub):\n        return "sub"\n'
    '    elif isinstance(x, Base):\n        return "base"\n'
    '    return "none"\n'
)
PLAN_PATH = "docs/plans/plan.md"
PLAN = "# Plan\n\n### Task T1: dispatch\n\n**Files:**\n- Modify: `pkg/use.py`\n"
REVIEW_TOML = '[policy]\nrevgate_source_repo = "/nonexistent/revgate-source"\n'


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == "revgate 0.2.0"


def test_plan_lint_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = tmp_path / "good.md"
    good.write_text(build_plan(valid_waves()), encoding="utf-8")
    assert main(["plan-lint", str(good)]) == 0
    waves = valid_waves()
    waves[0][1][0][1][0].deps = ["T9"]
    bad = tmp_path / "bad.md"
    bad.write_text(build_plan(waves), encoding="utf-8")
    assert main(["plan-lint", str(bad)]) == 1
    assert "error" in capsys.readouterr().out


def test_plan_brief(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text(build_plan([("W1", [("W1a", [one("T1")])])]), encoding="utf-8")
    assert main(["plan", "brief", str(plan), "T1"]) == 0
    assert "id: T1" in capsys.readouterr().out
    assert main(["plan", "brief", str(plan), "T9"]) == 2


def _task(fx: RepoFixture, head: str | None = None) -> int:
    return main(
        [
            "task",
            "--repo",
            str(fx.path),
            "--base",
            fx.base,
            "--head",
            head or fx.head,
            "--role",
            "implementer",
            "--plan",
            PLAN_PATH,
            "--task",
            "T1",
        ]
    )


def _finding_id(fx: RepoFixture, rule: str) -> str:
    path = run_file_path(common_dir(fx.path) / "revgate", "T1", fx.head, "implementer")
    rf = run_file_from_json(path.read_text(encoding="utf-8"))
    return next(f.id for f in rf.findings if f.rule == rule)


@pytest.fixture
def blocking(make_repo: MakeRepo) -> RepoFixture:
    base = {"pkg/__init__.py": "", "pkg/shapes.py": SHAPES, "pkg/use.py": USE_BASE}
    return make_repo(
        base, {"pkg/use.py": SHADOWED}, plan=PLAN, plan_path=PLAN_PATH, review_toml=REVIEW_TOML
    )


def test_task_mark_and_label(blocking: RepoFixture, capsys: pytest.CaptureFixture[str]) -> None:
    fx = blocking
    assert _task(fx) == 1
    fid = _finding_id(fx, "lib.isinstance_shadowing")
    capsys.readouterr()
    args = ["mark", fid, "fp", "--ruling", "--plan", PLAN_PATH, "--note", "intended"]
    assert main([*args, "--repo", str(fx.path)]) == 0
    state = common_dir(fx.path) / "revgate"
    assert rule_stats(state)["lib.isinstance_shadowing"] == (0, 1)
    rulings = [e for e in ledger_entries(state, "plan") if e.get("kind") == "ruling"]
    assert len(rulings) == 1 and rulings[0]["finding"] == fid
    assert main(["label", "--stats", "--repo", str(fx.path)]) == 0
    assert "lib.isinstance_shadowing" in capsys.readouterr().out
    assert main(["mark", "0" * 8, "tp", "--repo", str(fx.path)]) == 2


def test_recheck(blocking: RepoFixture, capsys: pytest.CaptureFixture[str]) -> None:
    fx = blocking
    assert _task(fx) == 1
    fid = _finding_id(fx, "lib.isinstance_shadowing")
    capsys.readouterr()
    assert main(["recheck", "--repo", str(fx.path), "--head", fx.head, fid]) == 1
    assert f"{fid}: still present" in capsys.readouterr().out
    fixed = fx.commit({"pkg/use.py": ORDERED}, "fix")
    assert main(["recheck", "--repo", str(fx.path), "--head", fixed, fid]) == 0
    assert f"{fid}: fixed" in capsys.readouterr().out
    assert git(fx.path, "rev-parse", "HEAD") == fixed
