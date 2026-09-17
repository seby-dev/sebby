# Jev-Backed PR Review Triage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Jev-backed advisory triage script and a new `sebby-toolkit:review` skill that narrows `pr-review-toolkit:review-pr`'s optional aspects (tests/types/comments) on diffs Jev confidently scores as low-risk, while keeping `code-reviewer` and `semgrep` unconditional and `silent-failure-hunter` gated by a deterministic (non-Jev) error-handling check — never reducing coverage below today's baseline on anything not confidently low-risk.

**Architecture:** A standalone CLI script (`plugin/hooks/scripts/review_triage.py`, not a Claude Code hook — hooks don't have diff context) that a new skill invokes with an explicit `--base <ref>` and consumes as advisory JSON, never as a gate. Follows the same optional-import, fail-open pattern as the repo's three existing Jev hooks.

**Tech Stack:** Python 3 stdlib only for the script itself (optional `typesafe_sdk` import, exactly like `stop_quality_check.py`/`semantic_destructive_check.py`/`block_env_git.py`), `pytest` with `subprocess`-based integration tests against real temporary git repos, Claude Code skill markdown format.

**Spec:** `docs/superpowers/specs/2026-09-17-jev-pr-review-triage-design.md`

**Note on scope refinement:** The spec's decision-logic section names "review-pr's other four aspects (tests/types/comments/simplify)" as narrowable. `review-pr`'s own command doc (`~/.claude/plugins/marketplaces/claude-plugins-official/plugins/pr-review-toolkit/commands/review-pr.md`) only auto-triggers three of those by file-type heuristic — `simplify` (`code-simplifier`) is a manual "after passing review" polish step `review-pr` itself never auto-selects. This plan narrows exactly three candidate aspects — `tests`, `types`, `comments` — and leaves `simplify` out of triage entirely (unaffected, exactly as `review-pr` already treats it). This refines an ambiguity in the spec; it does not change its intent.

## Global Constraints

- `code-reviewer` is a hardcoded floor: always dispatched by the new skill, never gated by triage output.
- `silent-failure-hunter` is gated by a deterministic error-handling/fallback pattern match in `review_triage.py`, never by the Jev risk score. This formalizes CLAUDE.md's existing "any error handling or fallback code → always run silent-failure-hunter" wording rather than loosening it.
- `semgrep` always runs, unconditionally — it's a static-analysis MCP tool call, not a subagent dispatch, so it isn't part of `review_triage.py`'s decision at all.
- Jev's *only* role is deciding whether `tests`/`types`/`comments` can be dropped from `review-pr`'s aspect list. It never affects `code-reviewer`, `silent-failure-hunter`, or `semgrep`.
- Narrowing happens only when `SEBBY_REVIEW_TRIAGE_MODE=active` **and** the Jev risk tier is `trivial` or `low`. Anything `moderate`/`high`, or any tier Jev fails to produce, keeps the full candidate set — provably no worse than today's baseline.
- Fail-open is structurally one code path: no `TYPESAFE_API_KEY`, no `typesafe_sdk`, any exception, an unrecognized score value, or `SEBBY_REVIEW_TRIAGE_MODE=off` all resolve to `risk_tier="high"`. `off` additionally skips the Jev network call entirely rather than just discarding its result.
- `SEBBY_REVIEW_TRIAGE_MODE` defaults to `shadow`: computes and logs what triage would have narrowed (to `~/.claude/sebby-triage.jsonl`), but the returned `recommended_aspects` always equals `candidate_aspects` regardless of tier. Only `active` actually narrows.
- Nothing is ever silently skipped: `review_triage.py`'s output always carries machine-readable reasons, and the skill's final report must state what ran and what was skipped or would-have-been-skipped.
- `review_triage.py` is stdlib-only Python plus an optional `typesafe_sdk` import (same convention as the three existing Jev hooks) — no new hard dependency on the `sebby` package or its extras.
- Tests mirror the existing convention exactly: load the script via `importlib.util.spec_from_file_location` (not a package import), inject a fake `typesafe_sdk` module into `sys.modules` for Jev-path tests, and use real temporary git repos (via `subprocess`) for the deterministic-heuristic and end-to-end tests — not mocked git output.
- The CLAUDE.md edit (Task 5) touches `~/.claude/CLAUDE.md`, not this repo, and is shared across every project the user works in. It must not be applied automatically — show the exact diff and get explicit confirmation before writing it, regardless of how the rest of this plan is authorized.

---

### Task 1: Deterministic triage core (`review_triage.py`, part 1)

**Files:**
- Create: `plugin/hooks/scripts/review_triage.py`
- Test: `tests/plugin/test_review_triage.py`

**Interfaces:**
- Consumes: nothing
- Produces (for Task 2 to extend and Task 3 to invoke as a CLI):
  - `get_changed_files(base: str, project_dir: Path) -> list[str]`
  - `get_diff_text(base: str, project_dir: Path, max_chars: int = 20000) -> str`
  - `candidate_aspects(changed_files: list[str], diff_text: str) -> set[str]`
  - `silent_failure_hunter_needed(diff_text: str) -> bool`
  - `recommend(base: str, project_dir: Path) -> dict` (stub: always `risk_tier="high"`, no narrowing — Task 2 replaces the body, not the signature)
  - `main()` — CLI entrypoint, `--base` (required), `--project-dir` (default `$CLAUDE_PROJECT_DIR` or `.`), prints `recommend()`'s dict as JSON to stdout.

- [ ] **Step 1: Write the failing tests**

Create `tests/plugin/test_review_triage.py`:

```python
"""Tests for the deterministic parts of review_triage.py: candidate aspect
detection, the silent-failure-hunter trigger, and the CLI end-to-end.
Jev-backed risk scoring and rollout modes are covered separately in
test_review_triage_jev.py.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "plugin" / "hooks" / "scripts" / "review_triage.py"


def _load_script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("review_triage", SCRIPT)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def make_repo(
    tmp_path: Path, base_files: dict[str, str], branch_files: dict[str, str]
) -> tuple[Path, str]:
    """Create a real git repo: commit `base_files`, then `branch_files` on
    top. Returns (repo_path, base_sha) so callers can pass `base_sha` as
    `--base` and get a diff scoped to exactly the branch_files changes.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    for name, content in base_files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "base"], repo)
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    for name, content in branch_files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "branch changes"], repo)

    return repo, base_sha


class TestCandidateAspects:
    def test_test_file_changed_triggers_tests_aspect(self, tmp_path: Path) -> None:
        repo, base = make_repo(
            tmp_path,
            base_files={"README.md": "hello\n"},
            branch_files={"tests/test_widget.py": "def test_x():\n    assert True\n"},
        )
        mod = _load_script()
        files = mod.get_changed_files(base, repo)
        diff = mod.get_diff_text(base, repo)
        assert mod.candidate_aspects(files, diff) == {"tests"}

    def test_new_class_triggers_types_aspect(self, tmp_path: Path) -> None:
        repo, base = make_repo(
            tmp_path,
            base_files={"main.py": "x = 1\n"},
            branch_files={"main.py": "x = 1\n\nclass Widget:\n    pass\n"},
        )
        mod = _load_script()
        files = mod.get_changed_files(base, repo)
        diff = mod.get_diff_text(base, repo)
        assert "types" in mod.candidate_aspects(files, diff)

    def test_new_comment_triggers_comments_aspect(self, tmp_path: Path) -> None:
        repo, base = make_repo(
            tmp_path,
            base_files={"main.py": "x = 1\n"},
            branch_files={"main.py": "# Explains a subtle invariant\nx = 1\n"},
        )
        mod = _load_script()
        files = mod.get_changed_files(base, repo)
        diff = mod.get_diff_text(base, repo)
        assert "comments" in mod.candidate_aspects(files, diff)

    def test_trivial_docs_change_triggers_nothing(self, tmp_path: Path) -> None:
        repo, base = make_repo(
            tmp_path,
            base_files={"README.md": "hello\n"},
            branch_files={"README.md": "hello world\n"},
        )
        mod = _load_script()
        files = mod.get_changed_files(base, repo)
        diff = mod.get_diff_text(base, repo)
        assert mod.candidate_aspects(files, diff) == set()


class TestSilentFailureHunterTrigger:
    def test_new_try_except_triggers(self, tmp_path: Path) -> None:
        repo, base = make_repo(
            tmp_path,
            base_files={"main.py": "x = 1\n"},
            branch_files={"main.py": "try:\n    x = 1\nexcept Exception:\n    x = 0\n"},
        )
        mod = _load_script()
        diff = mod.get_diff_text(base, repo)
        assert mod.silent_failure_hunter_needed(diff) is True

    def test_no_error_handling_does_not_trigger(self, tmp_path: Path) -> None:
        repo, base = make_repo(
            tmp_path,
            base_files={"main.py": "x = 1\n"},
            branch_files={"main.py": "x = 2\n"},
        )
        mod = _load_script()
        diff = mod.get_diff_text(base, repo)
        assert mod.silent_failure_hunter_needed(diff) is False


class TestCLIEndToEnd:
    def test_cli_prints_recommendation_json(self, tmp_path: Path) -> None:
        repo, base = make_repo(
            tmp_path,
            base_files={"README.md": "hello\n"},
            branch_files={"README.md": "hello world\n"},
        )
        # Explicitly drop TYPESAFE_API_KEY so this test never makes a real
        # network call regardless of the ambient environment it runs in --
        # once Task 2 lands, recommend() checks this var for real.
        env = {k: v for k, v in os.environ.items() if k != "TYPESAFE_API_KEY"}
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--base", base, "--project-dir", str(repo)],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["candidate_aspects"] == []
        assert payload["silent_failure_hunter"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/plugin/test_review_triage.py -v`
Expected: FAIL — `plugin/hooks/scripts/review_triage.py` doesn't exist yet.

- [ ] **Step 3: Write `plugin/hooks/scripts/review_triage.py`**

```python
#!/usr/bin/env python3
"""Advisory PR-review triage: recommends which review-pr aspects to run.

Not a Claude Code hook — a plain CLI script invoked directly (via Bash) by
the `sebby-toolkit:review` skill with an explicit `--base <ref>`, so the
diff being scored is never ambiguous. This is advisory only: nothing in
this repo denies or intercepts a subagent dispatch. The caller decides what
to do with the recommendation.

`code-reviewer` and `semgrep` are never part of this script's output —
they're unconditional floors the calling skill always runs. This script
only narrows `review-pr`'s optional tests/types/comments aspects, and
separately reports (deterministically, never via Jev) whether the diff
touches error-handling code that `silent-failure-hunter` should see.

If Jev (TypeSafe) is available (TYPESAFE_API_KEY set and typesafe_sdk
installed), the whole-diff risk score in Task 2 narrows the candidate
aspects on diffs it confidently calls trivial/low risk. This file's
`recommend()` is a stub until Task 2: it always reports risk_tier="high"
and never narrows.
"""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

_TEST_FILE_PATTERN = re.compile(
    r"(^|/)(tests?)/|(^|/)test_[^/]+\.py$|_test\.py$|\.test\.[jt]sx?$|\.spec\.[jt]sx?$",
    re.IGNORECASE,
)
_TYPE_PATTERN = re.compile(
    r"^\+\s*(class\s+\w+|interface\s+\w+|enum\s+\w+|struct\s+\w+|@dataclass|type\s+\w+\s*=)",
    re.MULTILINE,
)
_COMMENT_PATTERN = re.compile(
    r'^[+-]\s*(#|//|/\*|\*(?!/)|""")',
    re.MULTILINE,
)
_SILENT_FAILURE_PATTERN = re.compile(
    r"^\+.*\b(try\s*:|except\b|\.catch\s*\(|rescue\b|panic\s*\(|recover\s*\()",
    re.IGNORECASE | re.MULTILINE,
)


def get_changed_files(base: str, project_dir: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def get_diff_text(base: str, project_dir: Path, max_chars: int = 20000) -> str:
    result = subprocess.run(
        ["git", "diff", f"{base}...HEAD"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout[:max_chars]


def _added_or_removed_body(diff_text: str) -> str:
    """Strip `+++`/`---` file-header lines so they don't false-positive the
    comment/type/error-handling patterns below (which key off leading `+`/`-`)."""
    return "\n".join(
        line
        for line in diff_text.splitlines()
        if not line.startswith("+++") and not line.startswith("---")
    )


def candidate_aspects(changed_files: list[str], diff_text: str) -> set[str]:
    body = _added_or_removed_body(diff_text)
    aspects: set[str] = set()
    if any(_TEST_FILE_PATTERN.search(f) for f in changed_files):
        aspects.add("tests")
    if _TYPE_PATTERN.search(body):
        aspects.add("types")
    if _COMMENT_PATTERN.search(body):
        aspects.add("comments")
    return aspects


def silent_failure_hunter_needed(diff_text: str) -> bool:
    body = _added_or_removed_body(diff_text)
    return bool(_SILENT_FAILURE_PATTERN.search(body))


def recommend(base: str, project_dir: Path) -> dict:
    """Compute the triage recommendation for the diff between `base` and HEAD.

    This initial version always returns risk_tier="high" and never narrows
    the candidate aspects -- Task 2 replaces this function's body with real
    Jev scoring, rollout-mode handling, and shadow-mode logging, keeping this
    exact signature and return-dict shape.
    """
    files = get_changed_files(base, project_dir)
    diff = get_diff_text(base, project_dir)
    candidates = candidate_aspects(files, diff)
    needs_silent_failure_hunter = silent_failure_hunter_needed(diff)

    return {
        "risk_tier": "high",
        "mode": "active",
        "candidate_aspects": sorted(candidates),
        "recommended_aspects": sorted(candidates),
        "silent_failure_hunter": needs_silent_failure_hunter,
        "reasons": {
            "risk_tier": "Jev not yet wired in (Task 2)",
            "silent_failure_hunter": (
                "diff touches error-handling/fallback code"
                if needs_silent_failure_hunter
                else "no error-handling patterns detected"
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Jev-backed PR review triage")
    parser.add_argument(
        "--base", required=True, help="Base ref/SHA to diff against (e.g. origin/main)"
    )
    parser.add_argument(
        "--project-dir",
        default=os.environ.get("CLAUDE_PROJECT_DIR", "."),
        help="Project directory to run git commands in",
    )
    args = parser.parse_args()
    result = recommend(args.base, Path(args.project_dir))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/plugin/test_review_triage.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugin/hooks/scripts/review_triage.py tests/plugin/test_review_triage.py
git commit -m "feat(plugin/hooks): add deterministic PR review triage core"
```

---

### Task 2: Jev risk scoring, rollout modes, and shadow logging (`review_triage.py`, part 2)

**Files:**
- Modify: `plugin/hooks/scripts/review_triage.py` (replace `recommend()`'s body; add `_jev_risk_tier`, `resolve_mode`, `log_shadow_decision`, `RISK_TIERS`, `NARROW_TIERS`)
- Test: `tests/plugin/test_review_triage_jev.py`

**Interfaces:**
- Consumes: `get_changed_files`, `get_diff_text`, `candidate_aspects`, `silent_failure_hunter_needed` from Task 1 (unchanged)
- Produces:
  - `RISK_TIERS: tuple[str, ...]` = `("trivial", "low", "moderate", "high")`
  - `NARROW_TIERS: frozenset[str]` = `{"trivial", "low"}`
  - `_jev_risk_tier(diff_text: str) -> str` (fails open to `"high"`)
  - `resolve_mode() -> str` (reads `SEBBY_REVIEW_TRIAGE_MODE`; returns `"off"`/`"shadow"`/`"active"`, defaulting invalid/unset values to `"shadow"`)
  - `log_shadow_decision(entry: dict) -> None` (appends JSONL to `Path.home() / ".claude" / "sebby-triage.jsonl"`, best-effort)
  - `recommend(base: str, project_dir: Path) -> dict` — same signature as Task 1, now with real logic

- [ ] **Step 1: Write the failing tests**

Create `tests/plugin/test_review_triage_jev.py`:

```python
"""Tests for the Jev-backed risk scoring and rollout-mode handling in
review_triage.py. Deterministic aspect/trigger detection is covered in
test_review_triage.py -- these tests cover only _jev_risk_tier, resolve_mode,
log_shadow_decision, and how recommend() combines them.
"""

from __future__ import annotations

import builtins
import importlib
import json
import subprocess
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

SCRIPT = Path(__file__).parents[2] / "plugin" / "hooks" / "scripts" / "review_triage.py"


def _load_script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("review_triage", SCRIPT)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def make_repo(
    tmp_path: Path, base_files: dict[str, str], branch_files: dict[str, str]
) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    for name, content in base_files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "base"], repo)
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    for name, content in branch_files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "branch changes"], repo)

    return repo, base_sha


@dataclass
class FakeScore:
    score: str


def _make_fake_sdk(score_value: str) -> types.ModuleType:
    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def system_one(self, *args: Any, **kwargs: Any) -> Any:
            class Resp:
                scores = {"risk": FakeScore(score_value)}

            return Resp()

    fake_sdk = types.ModuleType("typesafe_sdk")
    fake_sdk.TypeSafeClient = FakeClient  # type: ignore[attr-defined]
    fake_sdk.Score = MagicMock()  # type: ignore[attr-defined]
    return fake_sdk


class TestJevRiskTierFailOpen:
    def test_no_api_key_returns_high(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        mod = _load_script()
        assert mod._jev_risk_tier("some diff") == "high"

    def test_sdk_not_installed_returns_high(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delitem(sys.modules, "typesafe_sdk", raising=False)
        real_import = builtins.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "typesafe_sdk":
                raise ImportError("No module named 'typesafe_sdk'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        mod = _load_script()
        assert mod._jev_risk_tier("some diff") == "high"

    def test_jev_exception_returns_high(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class ExplodingClient:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def system_one(self, *args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("network error")

        fake_sdk = types.ModuleType("typesafe_sdk")
        fake_sdk.TypeSafeClient = ExplodingClient  # type: ignore[attr-defined]
        fake_sdk.Score = MagicMock()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "typesafe_sdk", fake_sdk)
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        mod = _load_script()
        assert mod._jev_risk_tier("some diff") == "high"

    def test_unrecognized_score_value_returns_high(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "typesafe_sdk", _make_fake_sdk("not_a_real_tier"))
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        mod = _load_script()
        assert mod._jev_risk_tier("some diff") == "high"


class TestJevRiskTierSuccess:
    @pytest.mark.parametrize("tier", ["trivial", "low", "moderate", "high"])
    def test_returns_tier_from_jev(self, monkeypatch: pytest.MonkeyPatch, tier: str) -> None:
        monkeypatch.setitem(sys.modules, "typesafe_sdk", _make_fake_sdk(tier))
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

        mod = _load_script()
        assert mod._jev_risk_tier("some diff") == tier


class TestResolveMode:
    def test_default_is_shadow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SEBBY_REVIEW_TRIAGE_MODE", raising=False)
        mod = _load_script()
        assert mod.resolve_mode() == "shadow"

    def test_invalid_value_falls_back_to_shadow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SEBBY_REVIEW_TRIAGE_MODE", "bogus")
        mod = _load_script()
        assert mod.resolve_mode() == "shadow"

    @pytest.mark.parametrize("mode", ["off", "shadow", "active"])
    def test_valid_values_pass_through(self, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
        monkeypatch.setenv("SEBBY_REVIEW_TRIAGE_MODE", mode)
        mod = _load_script()
        assert mod.resolve_mode() == mode


class TestRecommendModes:
    def test_off_mode_never_calls_jev_and_never_narrows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        class ExplodingClient:
            def __init__(self, **kwargs: Any) -> None:
                raise AssertionError("Jev must not be called when mode is 'off'")

        fake_sdk = types.ModuleType("typesafe_sdk")
        fake_sdk.TypeSafeClient = ExplodingClient  # type: ignore[attr-defined]
        fake_sdk.Score = MagicMock()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "typesafe_sdk", fake_sdk)
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
        monkeypatch.setenv("SEBBY_REVIEW_TRIAGE_MODE", "off")

        repo, base = make_repo(
            tmp_path,
            base_files={"README.md": "hello\n"},
            branch_files={"README.md": "hello world\n\nclass Widget:\n    pass\n"},
        )
        mod = _load_script()
        result = mod.recommend(base, repo)
        assert result["risk_tier"] == "high"
        assert result["recommended_aspects"] == result["candidate_aspects"]

    def test_shadow_mode_logs_but_never_narrows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setitem(sys.modules, "typesafe_sdk", _make_fake_sdk("trivial"))
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
        monkeypatch.setenv("SEBBY_REVIEW_TRIAGE_MODE", "shadow")

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        repo, base = make_repo(
            tmp_path,
            base_files={"README.md": "hello\n"},
            branch_files={"README.md": "hello world\n\nclass Widget:\n    pass\n"},
        )
        mod = _load_script()
        result = mod.recommend(base, repo)

        assert result["risk_tier"] == "trivial"
        assert result["recommended_aspects"] == result["candidate_aspects"]
        assert result["candidate_aspects"] != []

        log_path = fake_home / ".claude" / "sebby-triage.jsonl"
        assert log_path.exists()
        entry = json.loads(log_path.read_text().splitlines()[-1])
        assert entry["would_recommend"] == []

    def test_active_mode_narrows_on_low_risk(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setitem(sys.modules, "typesafe_sdk", _make_fake_sdk("low"))
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
        monkeypatch.setenv("SEBBY_REVIEW_TRIAGE_MODE", "active")

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        repo, base = make_repo(
            tmp_path,
            base_files={"README.md": "hello\n"},
            branch_files={"README.md": "hello world\n\nclass Widget:\n    pass\n"},
        )
        mod = _load_script()
        result = mod.recommend(base, repo)

        assert result["risk_tier"] == "low"
        assert result["recommended_aspects"] == []
        assert result["candidate_aspects"] != []

    def test_active_mode_keeps_full_set_on_moderate_risk(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setitem(sys.modules, "typesafe_sdk", _make_fake_sdk("moderate"))
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
        monkeypatch.setenv("SEBBY_REVIEW_TRIAGE_MODE", "active")

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        repo, base = make_repo(
            tmp_path,
            base_files={"README.md": "hello\n"},
            branch_files={"README.md": "hello world\n\nclass Widget:\n    pass\n"},
        )
        mod = _load_script()
        result = mod.recommend(base, repo)

        assert result["risk_tier"] == "moderate"
        assert result["recommended_aspects"] == result["candidate_aspects"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/plugin/test_review_triage_jev.py -v`
Expected: FAIL — `_jev_risk_tier`, `resolve_mode`, `log_shadow_decision` don't exist yet.

- [ ] **Step 3: Update `plugin/hooks/scripts/review_triage.py`**

Add these imports at the top (alongside the existing `argparse`, `json`, `os`, `re`, `subprocess`, `pathlib.Path`):

```python
from datetime import datetime, timezone
```

Add after the existing regex constants:

```python
RISK_TIERS = ("trivial", "low", "moderate", "high")
NARROW_TIERS = frozenset({"trivial", "low"})


def _jev_risk_tier(diff_text: str) -> str:
    """Return a Jev risk tier for `diff_text`, or "high" if Jev is
    unavailable.

    Fails open to "high" on any missing SDK, missing API key, unrecognized
    score value, or SDK/network exception. "high" is the same value used
    when Jev isn't consulted at all, so there's no separate skip-boolean
    to get backwards -- "unscored" and "high" are one code path.
    """
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        return "high"

    try:
        from typesafe_sdk import Score, TypeSafeClient  # type: ignore[import]
    except ImportError:
        return "high"

    try:
        client = TypeSafeClient(api_key=api_key, model="jev-latest")
        response = client.system_one(
            {"diff": diff_text},
            {
                "risk": Score(
                    instructions=(
                        "Rate how likely this diff is to introduce a regression "
                        "or bug that needs careful review, versus being a "
                        "trivial, low-risk change (docs, formatting, config, "
                        "comment-only, or a simple rename)."
                    ),
                    criteria=list(RISK_TIERS),
                )
            },
        )
        tier = response.scores["risk"].score
        return tier if tier in RISK_TIERS else "high"
    except Exception:  # noqa: BLE001
        # Fail open: any network, auth, or SDK error is swallowed silently.
        return "high"


def resolve_mode() -> str:
    mode = os.environ.get("SEBBY_REVIEW_TRIAGE_MODE", "shadow").strip().lower()
    if mode not in ("off", "shadow", "active"):
        return "shadow"
    return mode


def log_shadow_decision(entry: dict) -> None:
    """Append one JSONL entry recording a triage decision. Best-effort --
    any filesystem error is swallowed, since this is a diagnostic log, not
    part of the decision path."""
    log_path = Path.home() / ".claude" / "sebby-triage.jsonl"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass
```

Replace the Task 1 stub `recommend()` function entirely with:

```python
def recommend(base: str, project_dir: Path) -> dict:
    """Compute the triage recommendation for the diff between `base` and
    HEAD. Never narrows below `candidate_aspects` unless
    SEBBY_REVIEW_TRIAGE_MODE=active and Jev confidently scores the diff
    trivial/low risk."""
    files = get_changed_files(base, project_dir)
    diff = get_diff_text(base, project_dir)
    candidates = candidate_aspects(files, diff)
    needs_silent_failure_hunter = silent_failure_hunter_needed(diff)

    mode = resolve_mode()
    tier = "high" if mode == "off" else _jev_risk_tier(diff)

    would_narrow = tier in NARROW_TIERS
    if mode == "active" and would_narrow:
        recommended = set()
    else:
        recommended = set(candidates)

    result = {
        "risk_tier": tier,
        "mode": mode,
        "candidate_aspects": sorted(candidates),
        "recommended_aspects": sorted(recommended),
        "silent_failure_hunter": needs_silent_failure_hunter,
        "reasons": {
            "risk_tier": (
                "triage mode is 'off'" if mode == "off" else f"Jev scored this diff as '{tier}'"
            ),
            "silent_failure_hunter": (
                "diff touches error-handling/fallback code"
                if needs_silent_failure_hunter
                else "no error-handling patterns detected"
            ),
        },
    }

    if mode in ("shadow", "active"):
        log_shadow_decision(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "project_dir": str(project_dir),
                "mode": mode,
                "risk_tier": tier,
                "candidate_aspects": sorted(candidates),
                "would_recommend": sorted(set() if would_narrow else candidates),
                "actually_recommended": sorted(recommended),
            }
        )

    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/plugin/test_review_triage.py tests/plugin/test_review_triage_jev.py -v`
Expected: PASS (all of Task 1's tests still pass unchanged, plus Task 2's new tests)

- [ ] **Step 5: Commit**

```bash
git add plugin/hooks/scripts/review_triage.py tests/plugin/test_review_triage_jev.py
git commit -m "feat(plugin/hooks): add Jev risk scoring and rollout modes to review triage"
```

---

### Task 3: `sebby-toolkit:review` skill

**Files:**
- Create: `plugin/skills/review/SKILL.md`

**Interfaces:**
- Consumes: `review_triage.py`'s CLI contract from Tasks 1-2 — invoked as
  `python3 "$CLAUDE_PLUGIN_ROOT/hooks/scripts/review_triage.py" --base <ref>`,
  producing JSON on stdout with keys `risk_tier`, `mode`, `candidate_aspects`,
  `recommended_aspects`, `silent_failure_hunter`, `reasons`.
- Produces: the `/sebby-toolkit:review` skill invocation, referenced by
  Task 4 (README) and Task 5 (CLAUDE.md).

- [ ] **Step 1: Write `plugin/skills/review/SKILL.md`**

```markdown
---
name: review
description: Pre-PR review pass with Jev-backed triage -- runs code-reviewer and semgrep unconditionally, silent-failure-hunter when the diff touches error handling, and review-pr's tests/types/comments aspects narrowed only on diffs Jev confidently scores as low-risk. Use before opening a PR.
---

# Pre-PR Review Triage

## Overview

Runs the full pre-PR review pass, but skips `review-pr`'s optional aspects
(tests/types/comments) on diffs a Jev risk score confidently calls trivial
or low risk. `code-reviewer` and `semgrep` always run. `silent-failure-hunter`
runs whenever the diff touches error-handling or fallback code, determined
deterministically -- never by the Jev score. Nothing is ever silently
skipped: this skill's report always states what ran and what triage
skipped or would have skipped.

## Steps

### 1. Determine the base ref

Resolve the ref to diff against:
- If this branch tracks a remote, use its merge-base:
  `git merge-base HEAD origin/<default-branch>`.
- Otherwise try `git merge-base HEAD main`, then `git merge-base HEAD master`.
- If neither exists, ask the user which ref to diff against rather than
  guessing.

### 2. Run triage

```
python3 "$CLAUDE_PLUGIN_ROOT/hooks/scripts/review_triage.py" --base <resolved-ref>
```

Parse the JSON on stdout: `risk_tier`, `mode`, `candidate_aspects`,
`recommended_aspects`, `silent_failure_hunter` (bool), `reasons`.

**If the command exits non-zero** (bad `--base` ref, git failure, or any
other error), do not trust a partial or malformed result. Treat this
exactly like `pr-review-toolkit` being unavailable in step 5: fall back to
the full review set — dispatch `silent-failure-hunter` unconditionally,
and pass every candidate aspect to `review-pr` unfiltered — and note the
triage failure in the final report rather than silently proceeding as if
it recommended a narrow set.

### 3. Dispatch code-reviewer (always)

Dispatch `pr-review-toolkit:code-reviewer`. If the plugin isn't installed
in this project, review the diff manually for bugs, convention violations,
and missed edge cases instead of skipping this step. This step never
depends on triage output.

### 4. Dispatch silent-failure-hunter (conditional, deterministic)

If `silent_failure_hunter` is `true`, dispatch
`pr-review-toolkit:silent-failure-hunter`. If the plugin isn't installed,
manually check the diff's catch blocks and fallback branches for swallowed
errors and inadequate logging instead of skipping this step. If `false`,
skip it and say so in the report -- this decision never depends on the Jev
risk score.

### 5. Run review-pr for the narrowed aspects

Build the aspect list: `"code"` plus `recommended_aspects` (which may be
empty on a low-risk diff in `active` mode). Invoke:

```
/pr-review-toolkit:review-pr <aspect list, space-separated>
```

If `pr-review-toolkit` isn't installed, manually review the diff for each
aspect in `candidate_aspects` (not just `recommended_aspects`) instead of
skipping this step -- the same "unavailable plugin" fallback
`plugin/skills/feature/SKILL.md` already uses for its own review steps.

### 6. Run semgrep (always)

Run the semgrep MCP tool's SAST/secrets/supply-chain findings tools
unconditionally. If semgrep isn't set up in this session, note that in the
report rather than skipping silently -- the user can run
`/setup-semgrep-plugin`.

### 7. Report

Summarize in one message:
- What ran: code-reviewer, semgrep, and (if applicable)
  silent-failure-hunter and each review-pr aspect actually dispatched.
- **If triage's JSON couldn't be parsed** (step 2's non-zero-exit case),
  report that explicitly -- e.g. "Triage failed to run (git error); ran the
  full review set as a fallback" -- rather than silently omitting this
  bullet because there was no `reasons` field to quote.
- **Otherwise, what triage skipped**, quoting `reasons` from the JSON --
  e.g. "Skipped: tests, types, comments (risk_tier: low, mode: active)". If
  `mode` is `shadow`, report it as "would have skipped" instead, since
  shadow mode runs everything regardless of tier.
- Findings from the dispatched agents/tools, using the same
  critical/important/suggestion structure `review-pr` itself uses.

## Usage

```
/review
```

When installed as a plugin, invoke namespaced: `/sebby-toolkit:review`.

## Notes

- Triage is inert (recommends everything, `risk_tier` always `"high"`)
  unless `TYPESAFE_API_KEY` is set and `typesafe_sdk` (the `judgement`
  extra) is importable by this project's `python3` -- see
  `plugin/README.md`'s configuration table.
- `SEBBY_REVIEW_TRIAGE_MODE` defaults to `shadow`: triage logs what it
  would have skipped to `~/.claude/sebby-triage.jsonl` without actually
  skipping anything, so real PRs can be sampled before trusting it. Set it
  to `active` to apply real narrowing, or `off` to disable triage entirely
  (equivalent to Jev being unavailable).
- Only committed commits on this branch are scored -- commit local changes
  before running this skill, the same expectation any PR review already has.
```

- [ ] **Step 2: Commit**

```bash
git add plugin/skills/review/SKILL.md
git commit -m "feat(plugin/skills): add sebby-toolkit:review triage skill"
```

---

### Task 4: Document the new script/skill and the undocumented `TYPESAFE_API_KEY` gate

**Files:**
- Modify: `plugin/README.md`

**Interfaces:**
- Consumes: `SEBBY_REVIEW_TRIAGE_MODE` (Task 2), the `review` skill (Task 3), and `TYPESAFE_API_KEY` (already used by three existing hooks but never documented in this table)
- Produces: nothing consumed by later tasks

- [ ] **Step 1: Update the "What's included" section**

In `plugin/README.md`, change:

```markdown
- **Skill**: `feature` — an autopilot → simplify → code-review →
  security-review → verify pipeline for shipping a feature end-to-end.
```

to:

```markdown
- **Skills**:
  - `feature` — an autopilot → simplify → code-review → security-review →
    verify pipeline for shipping a feature end-to-end.
  - `review` — a pre-PR review pass with Jev-backed triage: runs
    code-reviewer and semgrep unconditionally, silent-failure-hunter when
    the diff touches error handling, and narrows `review-pr`'s optional
    aspects only on diffs Jev confidently scores as low-risk.
```

- [ ] **Step 2: Add rows to the configuration table**

In `plugin/README.md`, change the existing table:

```markdown
| Variable | Used by | Default | Purpose |
|---|---|---|---|
| `SEBBY_LINT_COMMAND` | `post_edit_lint.py`, `stop_quality_check.py` | `ruff check --output-format=concise` | Lint command run after edits and at Stop; the project root is appended as the last argument. |
| `SEBBY_TYPECHECK_COMMAND` | `stop_quality_check.py` | unset (skipped) | Type-check command run at Stop, e.g. `mypy src`. Pass the target path explicitly — it is NOT auto-appended. |
| `SEBBY_DOCS_FILES` | `post_merge_docs_update.py` | `README.md` | Comma-separated list of doc files to check for staleness after a PR merge. |
| `SEBBY_WORKTREE_DEV_PATH` | `worktree_redirect.py` | unset (hook no-ops) | Absolute path to a dev worktree to redirect a session into, if the session started at the project root. |
```

to:

```markdown
| Variable | Used by | Default | Purpose |
|---|---|---|---|
| `SEBBY_LINT_COMMAND` | `post_edit_lint.py`, `stop_quality_check.py` | `ruff check --output-format=concise` | Lint command run after edits and at Stop; the project root is appended as the last argument. |
| `SEBBY_TYPECHECK_COMMAND` | `stop_quality_check.py` | unset (skipped) | Type-check command run at Stop, e.g. `mypy src`. Pass the target path explicitly — it is NOT auto-appended. |
| `SEBBY_DOCS_FILES` | `post_merge_docs_update.py` | `README.md` | Comma-separated list of doc files to check for staleness after a PR merge. |
| `SEBBY_WORKTREE_DEV_PATH` | `worktree_redirect.py` | unset (hook no-ops) | Absolute path to a dev worktree to redirect a session into, if the session started at the project root. |
| `SEBBY_REVIEW_TRIAGE_MODE` | `review_triage.py` (via the `review` skill) | `shadow` | `shadow` logs would-be triage decisions without narrowing review; `active` applies real narrowing; `off` disables triage entirely. |
| `TYPESAFE_API_KEY` | `stop_quality_check.py`, `semantic_destructive_check.py`, `block_env_git.py`, `review_triage.py` | unset (Jev-backed enhancements inert) | Enables all Jev (TypeSafe) semantic checks across these hooks/scripts. Also requires `typesafe_sdk` (the `sebby[judgement]` extra) to be importable by whatever `python3` runs on `PATH` in the consuming project — plugin scripts don't carry a bundled venv. Without both, every Jev-backed check silently fails open (never blocks, never narrows). |
```

- [ ] **Step 3: Commit**

```bash
git add plugin/README.md
git commit -m "docs(plugin): document review triage config and the TYPESAFE_API_KEY gate"
```

---

### Task 5: Update the global CLAUDE.md review-workflow instruction (requires explicit confirmation)

**Files:**
- Modify: `~/.claude/CLAUDE.md` (outside this repo — global, shared across every project)

**Interfaces:**
- Consumes: the `/sebby-toolkit:review` skill name from Task 3
- Produces: nothing consumed by later tasks (this is the final task)

**This task must not be applied automatically.** `~/.claude/CLAUDE.md` is
the user's personal global instruction file, not part of this repo, and
affects every project they work in. Show the exact diff below and get
explicit confirmation before writing it, regardless of how the rest of
this plan was authorized.

- [ ] **Step 1: Show the diff and get confirmation**

Current text (in the "Security & Review Workflow" section):

```markdown
After implementation and before shipping, run these in parallel:

- **`pr-review-toolkit:silent-failure-hunter`** — hunt for swallowed exceptions, silent fallbacks, and inadequate error handling
- **`pr-review-toolkit:code-reviewer`** — bugs, security vulnerabilities, convention violations
- **`semgrep`** — static analysis for known vulnerability patterns (SQL injection, hardcoded secrets, insecure API usage); requires `/setup-semgrep-plugin` first run

Passive (always on, no invocation needed):
- **`security-guidance`** — injects OWASP-style security reminders automatically each session

When to invoke each:
- Any error handling or fallback code → always run `silent-failure-hunter`
- Before every PR → run `code-reviewer`
- New external integrations or auth flows → also run `semgrep`
```

Proposed replacement:

```markdown
Before shipping, run `/sebby-toolkit:review`. It runs
`pr-review-toolkit:code-reviewer` and `semgrep` unconditionally, and
`pr-review-toolkit:silent-failure-hunter` whenever the diff touches
error-handling or fallback code — narrowing only `review-pr`'s optional
tests/types/comments aspects, and only on diffs a Jev risk score
confidently calls low-risk. It never skips silently: it reports what it
skipped and why. If the `sebby-toolkit` plugin isn't installed in a given
project, fall back to running `pr-review-toolkit:silent-failure-hunter`,
`pr-review-toolkit:code-reviewer`, and `semgrep` directly, in parallel.

Passive (always on, no invocation needed):
- **`security-guidance`** — injects OWASP-style security reminders automatically each session

For new external integrations or auth flows, confirm `semgrep` actually
ran as part of `/sebby-toolkit:review`'s step six — check its output if
the session shows semgrep wasn't configured; it requires
`/setup-semgrep-plugin` on first use.
```

- [ ] **Step 2: Apply the edit** (only after explicit user confirmation of Step 1)

Use the Edit tool on `~/.claude/CLAUDE.md` to replace the current text
shown in Step 1 with the proposed replacement shown in Step 1.

- [ ] **Step 3: Report**

No commit — this file isn't tracked in this repo's git history. Confirm
to the user that the edit was applied and where (`~/.claude/CLAUDE.md`).
