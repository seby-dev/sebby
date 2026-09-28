"""`revgate wave`: the W0 integrity checks over a wave's controller run files."""

from __future__ import annotations

import io
from pathlib import Path

from conftest import MakeRepo, RepoFixture

from revgate import __version__
from revgate.gates import wave_gate_failures
from revgate.gitio import common_dir
from revgate.model import RunFile, run_file_from_json
from revgate.store import run_file_path, write_run_file
from revgate.task_cmd import source_hash
from revgate.wave_cmd import run_wave

FENCE = "```"
PLAN_PATH = "docs/plans/plan.md"
PLAN = f"""# Plan

{FENCE}yaml plan-waves
waves:
  - id: W1
    subwaves:
      - id: W1a
        tasks:
          - id: T1
            risk: low
            depends_on: []
            estimate_min: 30
            owns: {{create: [src/t1.py], modify: [], test: []}}
          - id: T2
            risk: low
            depends_on: []
            estimate_min: 30
            owns: {{create: [src/t2.py], modify: [], test: []}}
{FENCE}

### Task T1: one

**Files:**
- Create: `src/t1.py`

### Task T2: two

**Files:**
- Create: `src/t2.py`
"""
REVIEW_TOML = '[policy]\nrevgate_source_repo = "/nonexistent/revgate-source"\n'


class Wave:
    """A base, T1's commit, and T2's commit on one line of history."""

    def __init__(self, make_repo: MakeRepo, t2_files: dict[str, str | None] | None = None) -> None:
        fx = make_repo(
            {"src/__init__.py": ""},
            {"src/t1.py": "ONE = 1\n"},
            plan=PLAN,
            plan_path=PLAN_PATH,
            review_toml=REVIEW_TOML,
        )
        self.fx = fx
        self.t1 = fx.head
        self.t2 = fx.commit(t2_files or {"src/t2.py": "TWO = 2\n"}, "T2")
        self.state = common_dir(fx.path) / "revgate"

    def controller_run(self, task: str, base: str, head: str, src_hash: str | None = None) -> None:
        rf = RunFile(
            task=task,
            role="controller",
            base=base,
            head=head,
            plan=PLAN_PATH,
            plan_task_hash=None,
            revgate_version=__version__,
            source_hash=src_hash or source_hash(),
            config_hash="",
            focus=False,
            focus_reasons=(),
            findings=(),
            obligations=(),
            unverified=(),
            incomplete=(),
            exit_code=0,
        )
        write_run_file(self.state, rf)

    def both_runs(self) -> None:
        self.controller_run("T1", self.fx.base, self.t1)
        self.controller_run("T2", self.t1, self.t2)

    def run(self, gate_log: Path | None = None) -> tuple[int, str]:
        out = io.StringIO()
        code = run_wave(
            self.fx.path, self.fx.base, self.t2, PLAN_PATH, "W1", gate_log=gate_log, out=out
        )
        return code, out.getvalue()


def _wave_rules(w: Wave) -> list[str]:
    path = run_file_path(w.state, "wave-W1", w.t2, "wave")
    rf = run_file_from_json(path.read_text(encoding="utf-8"))
    return [f.rule for f in rf.findings if f.tier.value == "blocking"]


def test_clean_wave_passes(make_repo: MakeRepo) -> None:
    w = Wave(make_repo)
    w.both_runs()
    code, text = w.run()
    assert code == 0, text
    assert text.splitlines()[0] == f"revgate wave W1 (T1, T2) merged={w.t2[:7]}: 0 blocking"
    assert _wave_rules(w) == []


def test_missing_controller_run_blocks(make_repo: MakeRepo) -> None:
    w = Wave(make_repo)
    w.controller_run("T1", w.fx.base, w.t1)
    code, text = w.run()
    assert code == 1
    assert "wave.integrity" in text and "T2" in text
    assert _wave_rules(w) == ["wave.integrity"]


def test_unowned_edit_blocks(make_repo: MakeRepo) -> None:
    w = Wave(make_repo, {"src/t2.py": "TWO = 2\n", "src/other.py": "X = 0\n"})
    w.both_runs()
    code, text = w.run()
    assert code == 1
    assert "src/other.py" in text


def test_lockfile_edit_is_allowed(make_repo: MakeRepo) -> None:
    w = Wave(make_repo, {"src/t2.py": "TWO = 2\n", "uv.lock": "version = 1\n"})
    w.both_runs()
    code, text = w.run()
    assert code == 0, text


def test_two_source_hashes_block(make_repo: MakeRepo) -> None:
    w = Wave(make_repo)
    w.controller_run("T1", w.fx.base, w.t1)
    w.controller_run("T2", w.t1, w.t2, src_hash="0" * 64)
    code, text = w.run()
    assert code == 1
    assert "source hash" in text


def test_no_block_exits_2(make_repo: MakeRepo) -> None:
    fx: RepoFixture = make_repo({"a.py": ""}, {"a.py": "x = 1\n"}, plan="# Plan\n")
    out = io.StringIO()
    code = run_wave(fx.path, fx.base, fx.head, PLAN_PATH, "W1", gate_log=None, out=out)
    assert code == 2
    assert "revgate wave needs a plan-waves block" in out.getvalue()


def test_gate_log_records_failures(make_repo: MakeRepo, tmp_path: Path) -> None:
    w = Wave(make_repo)
    w.both_runs()
    log = tmp_path / "gate.log"
    log.write_text(
        "tests/test_a.py::test_one PASSED\n"
        "FAILED tests/test_b.py::test_two - AssertionError: nope\n"
        "=== 1 failed, 1 passed in 0.12s ===\n",
        encoding="utf-8",
    )
    w.run(gate_log=log)
    assert wave_gate_failures(w.state, w.t2) == frozenset({"tests/test_b.py::test_two"})
