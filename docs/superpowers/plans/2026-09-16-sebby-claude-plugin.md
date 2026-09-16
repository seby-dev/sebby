# Sebby Claude Code Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Claude Code plugin (`sebby-toolkit`) generalizing the hooks, skill, and settings.json baseline already duplicated verbatim or near-verbatim across `organist_bot`, `pmp-project`, `staff2solfa`, and `varrick-chorus-uk`, distributed via a marketplace.json in this repo.

**Architecture:** A standard Claude Code plugin directory (`plugin/`) at the sebby repo root, separate from the Python package (`src/sebby/`). Each hook is parameterized via environment variables (set by the consuming project's own `.claude/settings.json`) rather than hardcoding project-specific paths/commands, following the same pattern the spec called for. A repo-root `.claude-plugin/marketplace.json` lists the plugin so `/plugin marketplace add seby-dev/sebby` can install it. Hook scripts are tested by invoking them as real subprocesses with JSON stdin (exactly how Claude Code actually calls them), not by importing them as library code.

**Tech Stack:** Python 3 (stdlib only — no new `sebby` package dependency; these are standalone scripts, not part of the `sebby` package), `pytest` (for the hook-script test suite, using `subprocess.run`), the Claude Code plugin manifest format (`.claude-plugin/plugin.json`, `hooks/hooks.json`, `skills/<name>/SKILL.md`).

**Spec:** `docs/superpowers/specs/2026-09-16-shared-toolkit-design.md`

**Note on scope:** This is plan 4 of several. Three prior plans shipped the `sebby` Python package (`retry`, `llm`, `storage`, `cache`, `cli`, `config`, `logging`, `http`, `notify`), all merged to `main`. This plan covers the spec's "Claude Code plugin" phase. The `organist_bot` pilot migration and the CLAUDE.md de-duplication each get their own plan after this one.

**Source material:** Every hook/skill/settings file below is a real, verbatim-sourced generalization of files read directly from `organist_bot` (hooks, the `feature` skill, `settings.json`) and `pmp-project` (the `SessionStart` worktree-redirect hook), plus the settings.json deny-list already shared byte-for-byte by `staff2solfa`/`varrick-chorus-uk`/`pmp-project`. Two hooks (`block_env_git.py`, `scrub_env_transcript.py`) needed no generalization at all — they already use `${CLAUDE_PROJECT_DIR}` and contain no project-specific values. The rest had one or two hardcoded values (a lint tool, a mypy target path, a doc file list, an absolute worktree path) pulled out into environment variables the consuming project sets.

## Global Constraints

- Plugin lives at `plugin/` (repo root), separate from `src/sebby/` — the Python package and the Claude Code plugin are two independent deliverables sharing one repo, per the spec.
- Every hook that had a project-specific hardcoded value in its source (lint command, type-check target, doc file list, worktree path) is parameterized via an environment variable, with a sensible default where one exists, and a graceful no-op where it doesn't (never a crash on an unset variable).
- Hook scripts are stdlib-only Python (no new dependency) and are tested via `subprocess.run` invoking the real script with JSON on stdin — the same interface Claude Code itself uses — not via library-style imports.
- Hook tests live under `tests/plugin/` (separate from `tests/` covering the `sebby` package itself, since these test standalone scripts, not importable package code).
- The finished plugin gets validated with the `plugin-dev:plugin-validator` skill before the plan is considered done.

---

### Task 1: Plugin scaffolding

**Files:**
- Create: `plugin/.claude-plugin/plugin.json`
- Create: `.claude-plugin/marketplace.json` (repo root — separate from the plugin's own manifest)
- Create: `plugin/templates/settings.json`
- Create: `plugin/README.md`

**Interfaces:**
- Consumes: nothing
- Produces: the plugin's manifest and directory skeleton that later tasks add hooks/skills into

- [ ] **Step 1: Write `plugin/.claude-plugin/plugin.json`**

```json
{
  "name": "sebby-toolkit",
  "version": "0.1.0",
  "description": "Shared Claude Code hooks, skills, and settings: secret-safety hooks, quality-gate hooks, PR/docs workflow hooks, a worktree-redirect hook, and a generalized feature-development skill.",
  "author": {
    "name": "seby-dev"
  },
  "keywords": ["hooks", "quality-gates", "git-safety", "feature-workflow"]
}
```

- [ ] **Step 2: Write `.claude-plugin/marketplace.json`**

```json
{
  "name": "sebby-toolkit-marketplace",
  "owner": {
    "name": "seby-dev"
  },
  "plugins": [
    {
      "name": "sebby-toolkit",
      "source": "./plugin",
      "description": "Shared Claude Code hooks, skills, and settings for seby-dev's projects."
    }
  ]
}
```

- [ ] **Step 3: Write `plugin/templates/settings.json`**

This is the deny-list baseline already shared byte-for-byte by `staff2solfa`, `varrick-chorus-uk`, and (as a superset) `pmp-project` — a template a consuming project copies into its own `.claude/settings.json` (plugins cannot silently merge into a project's own settings file, so this ships as a copyable template, documented in Step 4's README):

```json
{
  "permissions": {
    "defaultMode": "dontAsk",
    "deny": [
      "Bash(rm -rf *)",
      "Bash(rm -fr *)",
      "Bash(* --force *)",
      "Bash(git push --force*)",
      "Bash(git push -f *)",
      "Bash(git reset --hard*)",
      "Bash(git clean -f*)",
      "Bash(chmod 777 *)",
      "Bash(sudo *)",
      "Bash(su *)",
      "Bash(* DROP *)",
      "Bash(* DROP TABLE*)",
      "Bash(* DELETE FROM*)",
      "Bash(mkfs*)",
      "Bash(dd if=*)",
      "Bash(shutdown*)",
      "Bash(reboot*)",
      "Bash(kill -9 *)",
      "Bash(pkill *)"
    ]
  }
}
```

- [ ] **Step 4: Write `plugin/README.md`**

```markdown
# sebby-toolkit (Claude Code plugin)

Shared Claude Code hooks, a feature-development skill, and a settings.json
template, generalized from patterns duplicated across several projects.

## Install

    /plugin marketplace add seby-dev/sebby
    /plugin install sebby-toolkit

## What's included

- **Hooks** (see `hooks/hooks.json`): env-file git-add blocking, transcript
  secret scrubbing, post-edit lint, stop-time quality gate, a post-PR-created
  nudge, a post-merge docs-sync nudge, and an optional worktree-redirect on
  session start.
- **Skill**: `feature` — an autopilot → simplify → code-review →
  security-review → verify pipeline for shipping a feature end-to-end.
- **Template**: `templates/settings.json` — a safe-defaults permission
  deny-list. Copy its contents into your project's own
  `.claude/settings.json` (plugins can't write to a consuming project's
  settings file automatically).

## Configuring the hooks

Several hooks read environment variables to avoid hardcoding project-specific
values — set these in your project's own `.claude/settings.json` under an
`"env"` block, or in your shell environment:

| Variable | Used by | Default | Purpose |
|---|---|---|---|
| `SEBBY_LINT_COMMAND` | `post_edit_lint.py`, `stop_quality_check.py` | `ruff check --output-format=concise` | Lint command run after edits and at Stop; the project root is appended as the last argument. |
| `SEBBY_TYPECHECK_COMMAND` | `stop_quality_check.py` | unset (skipped) | Type-check command run at Stop, e.g. `mypy src`. Pass the target path explicitly — it is NOT auto-appended. |
| `SEBBY_DOCS_FILES` | `post_merge_docs_update.py` | `README.md` | Comma-separated list of doc files to check for staleness after a PR merge. |
| `SEBBY_WORKTREE_DEV_PATH` | `worktree_redirect.py` | unset (hook no-ops) | Absolute path to a dev worktree to redirect a session into, if the session started at the project root. |
```

- [ ] **Step 5: Commit**

```bash
git add plugin/.claude-plugin/plugin.json .claude-plugin/marketplace.json plugin/templates/settings.json plugin/README.md
git commit -m "chore: scaffold sebby-toolkit Claude Code plugin"
```

---

### Task 2: Secret-safety hooks

**Files:**
- Create: `plugin/hooks/scripts/block_env_git.py`
- Create: `plugin/hooks/scripts/scrub_env_transcript.py`
- Create: `tests/plugin/__init__.py`
- Create: `tests/plugin/test_block_env_git.py`
- Create: `tests/plugin/test_scrub_env_transcript.py`

**Interfaces:**
- Consumes: nothing
- Produces: two standalone hook scripts, invoked as subprocesses by Claude Code (no Python-importable interface — tested via `subprocess.run`)

These two hooks needed no generalization — they were already fully generic in `organist_bot` (no hardcoded project-specific values, only `${CLAUDE_PROJECT_DIR}`), so they're copied essentially verbatim.

- [ ] **Step 1: Write `plugin/hooks/scripts/block_env_git.py`**

```python
#!/usr/bin/env python3
"""PreToolUse hook: block git operations that would commit .env files.

A .gitignore already excludes .env and .env.*, so this guards the remaining
paths around it: explicit `git add .env`, force-adds (`git add -f`), and
commits where a .env file somehow ended up staged.
"""

import json
import os
import re
import shlex
import subprocess
import sys


def deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def is_env_path(path: str) -> bool:
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return name == ".env" or name.startswith(".env.")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    command = (payload.get("tool_input") or {}).get("command", "")
    if "git" not in command:
        sys.exit(0)

    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    if "add" in tokens and any(is_env_path(t) for t in tokens):
        deny(
            "Blocked: .env files contain secrets and must never be staged. "
            "They are gitignored — do not add them, with or without -f."
        )

    if re.search(r"\bgit\b[^|;&]*\bcommit\b", command):
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            cwd=os.environ.get("CLAUDE_PROJECT_DIR", "."),
        )
        offenders = [p for p in result.stdout.splitlines() if is_env_path(p)]
        if offenders:
            deny(
                "Blocked: staged .env files detected "
                f"({', '.join(offenders)}). Run `git restore --staged <file>` "
                "before committing."
            )

    sys.exit(0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write `plugin/hooks/scripts/scrub_env_transcript.py`**

```python
#!/usr/bin/env python3
"""Scrub .env secret values from the session transcript on disk.

Registered as a PostToolUse hook on Read|Write|Edit (fires when a .env file
is touched) and as a Stop hook (end-of-turn safety net). Every value defined
in .env / .env.* at the project root is replaced with [REDACTED:<KEY>] in the
transcript JSONL, so secrets never persist in transcript files.
"""

import json
import os
import sys
import time
from pathlib import Path

# Values shorter than this are skipped to avoid redacting trivial strings
# like "true", port numbers, or fee defaults that happen to appear elsewhere.
MIN_VALUE_LEN = 6


def collect_secrets(root: Path) -> dict[str, str]:
    secrets: dict[str, str] = {}
    for env_file in root.glob(".env*"):
        if not env_file.is_file():
            continue
        try:
            lines = env_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip("'\"")
            if len(value) >= MIN_VALUE_LEN:
                secrets[value] = key.strip()
    return secrets


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}

    tool_name = payload.get("tool_name")
    if tool_name:
        # PostToolUse: only act when the touched file is .env or .env.*
        file_path = (payload.get("tool_input") or {}).get("file_path", "")
        if not Path(file_path).name.startswith(".env"):
            sys.exit(0)

    transcript = payload.get("transcript_path")
    if not transcript or not os.path.exists(transcript):
        sys.exit(0)

    root = Path(os.environ.get("CLAUDE_PROJECT_DIR", "."))
    secrets = collect_secrets(root)
    if not secrets:
        sys.exit(0)

    if tool_name:
        # Hook runs async; give the transcript writer a moment to flush
        # the tool result before rewriting the file.
        time.sleep(2)

    with open(transcript, encoding="utf-8") as fh:
        text = fh.read()

    original = text
    # Longest values first so overlapping substrings can't leave fragments.
    for value in sorted(secrets, key=len, reverse=True):
        replacement = f"[REDACTED:{secrets[value]}]"
        text = text.replace(value, replacement)
        # Transcripts are JSONL — also match the JSON-escaped form.
        escaped = json.dumps(value)[1:-1]
        if escaped != value:
            text = text.replace(escaped, replacement)

    if text != original:
        tmp = transcript + ".scrub.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, transcript)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Write the failing tests**

`tests/plugin/__init__.py`: empty file.

`tests/plugin/test_block_env_git.py`:

```python
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parents[2] / "plugin" / "hooks" / "scripts" / "block_env_git.py"


def run_hook(payload: dict[str, object], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(cwd)}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )


def test_denies_git_add_of_env_file(tmp_path):
    result = run_hook({"tool_input": {"command": "git add .env"}}, tmp_path)

    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_denies_forced_add_of_env_file(tmp_path):
    result = run_hook({"tool_input": {"command": "git add -f .env.production"}}, tmp_path)

    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_allows_normal_git_add(tmp_path):
    result = run_hook({"tool_input": {"command": "git add src/main.py"}}, tmp_path)

    assert result.stdout.strip() == ""


def test_ignores_non_git_commands(tmp_path):
    result = run_hook({"tool_input": {"command": "cat .env"}}, tmp_path)

    assert result.stdout.strip() == ""
```

`tests/plugin/test_scrub_env_transcript.py`:

```python
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parents[2] / "plugin" / "hooks" / "scripts" / "scrub_env_transcript.py"


def run_hook(payload: dict[str, object], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(cwd)}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )


def test_redacts_secret_value_from_transcript(tmp_path):
    (tmp_path / ".env").write_text("API_KEY=sk-supersecret123\n")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"text": "using key sk-supersecret123"}\n')

    result = run_hook(
        {
            "tool_name": "Read",
            "tool_input": {"file_path": str(tmp_path / ".env")},
            "transcript_path": str(transcript),
        },
        tmp_path,
    )

    assert result.returncode == 0
    content = transcript.read_text()
    assert "sk-supersecret123" not in content
    assert "[REDACTED:API_KEY]" in content


def test_skips_short_values(tmp_path):
    (tmp_path / ".env").write_text("PORT=8080\nFLAG=true\n")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"text": "listening on 8080 with flag true"}\n')

    run_hook(
        {
            "tool_name": "Read",
            "tool_input": {"file_path": str(tmp_path / ".env")},
            "transcript_path": str(transcript),
        },
        tmp_path,
    )

    content = transcript.read_text()
    assert "8080" in content
    assert "true" in content


def test_stop_hook_variant_scrubs_without_tool_name(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=abcdef123456\n")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"text": "token is abcdef123456"}\n')

    result = run_hook({"transcript_path": str(transcript)}, tmp_path)

    assert result.returncode == 0
    assert "abcdef123456" not in transcript.read_text()


def test_noop_when_no_env_files_exist(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"text": "nothing to scrub here"}\n')
    original = transcript.read_text()

    run_hook({"transcript_path": str(transcript)}, tmp_path)

    assert transcript.read_text() == original
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/plugin/test_block_env_git.py tests/plugin/test_scrub_env_transcript.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Run lint and type checks over the new files**

Run: `cd /Users/sebby/Developer/sebby && uv run ruff check plugin tests/plugin && uv run mypy plugin/hooks/scripts tests/plugin`
Expected: both clean. These new files aren't part of the `sebby` package's own `mypy src` scope, so check them explicitly here; if mypy complains about anything structural (e.g. no `py.typed` context), fix minimally or note why in your report.

- [ ] **Step 6: Commit**

```bash
git add plugin/hooks/scripts/block_env_git.py plugin/hooks/scripts/scrub_env_transcript.py tests/plugin/__init__.py tests/plugin/test_block_env_git.py tests/plugin/test_scrub_env_transcript.py
git commit -m "feat: add plugin secret-safety hooks (block_env_git, scrub_env_transcript)"
```

---

### Task 3: Quality-gate hooks

**Files:**
- Create: `plugin/hooks/scripts/post_edit_lint.py`
- Create: `plugin/hooks/scripts/stop_quality_check.py`
- Create: `tests/plugin/fixtures/fake_lint_fail.py`
- Create: `tests/plugin/fixtures/fake_lint_pass.py`
- Create: `tests/plugin/test_post_edit_lint.py`
- Create: `tests/plugin/test_stop_quality_check.py`

**Interfaces:**
- Consumes: nothing
- Produces: two standalone hook scripts, configured via `SEBBY_LINT_COMMAND` and `SEBBY_TYPECHECK_COMMAND` env vars

- [ ] **Step 1: Write the test fixtures**

`tests/plugin/fixtures/fake_lint_fail.py`:

```python
#!/usr/bin/env python3
import sys

print("fake.py:1:1: E999 fake lint issue")
sys.exit(1)
```

`tests/plugin/fixtures/fake_lint_pass.py`:

```python
#!/usr/bin/env python3
import sys

sys.exit(0)
```

- [ ] **Step 2: Write the failing tests**

`tests/plugin/test_post_edit_lint.py`:

```python
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parents[2] / "plugin" / "hooks" / "scripts" / "post_edit_lint.py"
FIXTURES = Path(__file__).parent / "fixtures"


def run_hook(env_overrides: dict[str, str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path), **env_overrides}
    return subprocess.run(
        [sys.executable, str(HOOK)], input="{}", capture_output=True, text=True, env=env
    )


def test_reports_issues_when_lint_command_fails(tmp_path):
    result = run_hook(
        {"SEBBY_LINT_COMMAND": f"{sys.executable} {FIXTURES / 'fake_lint_fail.py'}"}, tmp_path
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert "fake lint issue" in payload["systemMessage"]


def test_silent_when_lint_command_passes(tmp_path):
    result = run_hook(
        {"SEBBY_LINT_COMMAND": f"{sys.executable} {FIXTURES / 'fake_lint_pass.py'}"}, tmp_path
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_defaults_to_ruff_when_no_command_configured(tmp_path):
    result = run_hook({}, tmp_path)

    assert result.returncode == 0
```

`tests/plugin/test_stop_quality_check.py`:

```python
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parents[2] / "plugin" / "hooks" / "scripts" / "stop_quality_check.py"
FIXTURES = Path(__file__).parent / "fixtures"


def run_hook(env_overrides: dict[str, str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path), **env_overrides}
    return subprocess.run(
        [sys.executable, str(HOOK)], input="{}", capture_output=True, text=True, env=env
    )


def test_blocks_when_lint_fails(tmp_path):
    result = run_hook(
        {"SEBBY_LINT_COMMAND": f"{sys.executable} {FIXTURES / 'fake_lint_fail.py'}"}, tmp_path
    )

    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    assert "fake lint issue" in payload["reason"]


def test_blocks_when_typecheck_fails(tmp_path):
    result = run_hook(
        {
            "SEBBY_LINT_COMMAND": f"{sys.executable} {FIXTURES / 'fake_lint_pass.py'}",
            "SEBBY_TYPECHECK_COMMAND": f"{sys.executable} {FIXTURES / 'fake_lint_fail.py'}",
        },
        tmp_path,
    )

    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    assert "fake lint issue" in payload["reason"]


def test_silent_when_both_configured_and_passing(tmp_path):
    result = run_hook(
        {
            "SEBBY_LINT_COMMAND": f"{sys.executable} {FIXTURES / 'fake_lint_pass.py'}",
            "SEBBY_TYPECHECK_COMMAND": f"{sys.executable} {FIXTURES / 'fake_lint_pass.py'}",
        },
        tmp_path,
    )

    assert result.stdout.strip() == ""


def test_skips_typecheck_when_not_configured(tmp_path):
    result = run_hook(
        {"SEBBY_LINT_COMMAND": f"{sys.executable} {FIXTURES / 'fake_lint_pass.py'}"}, tmp_path
    )

    assert result.stdout.strip() == ""
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/plugin/test_post_edit_lint.py tests/plugin/test_stop_quality_check.py -v`
Expected: FAIL (hook scripts don't exist yet)

- [ ] **Step 4: Implement `plugin/hooks/scripts/post_edit_lint.py`**

```python
#!/usr/bin/env python3
"""PostToolUse hook: run a configurable lint command after each file write/edit.

Configure via the SEBBY_LINT_COMMAND environment variable (shell-split; the
project root is appended as the last argument) — defaults to `ruff check
--output-format=concise`.
"""

import json
import os
import shlex
import subprocess
from pathlib import Path

DEFAULT_LINT_COMMAND = "ruff check --output-format=concise"


def build_command(proj_root: Path) -> list[str]:
    raw = os.environ.get("SEBBY_LINT_COMMAND", DEFAULT_LINT_COMMAND)
    return [*shlex.split(raw), str(proj_root)]


def main() -> None:
    proj_root = Path(os.environ.get("CLAUDE_PROJECT_DIR", "."))
    command = build_command(proj_root)

    result = subprocess.run(command, capture_output=True, text=True, cwd=proj_root)

    if result.returncode != 0 and result.stdout.strip():
        lines = result.stdout.strip().splitlines()
        preview = "\n".join(lines[:20])
        if len(lines) > 20:
            preview += f"\n… ({len(lines) - 20} more issues)"
        print(json.dumps({"systemMessage": f"Lint found issues — fix before committing:\n{preview}"}))


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Implement `plugin/hooks/scripts/stop_quality_check.py`**

```python
#!/usr/bin/env python3
"""Stop hook: run a configurable lint + type-check when Claude stops; block
if issues remain.

Configure via SEBBY_LINT_COMMAND (see post_edit_lint.py) and
SEBBY_TYPECHECK_COMMAND (shell-split; the project root is NOT auto-appended
— pass the target path explicitly, e.g. "mypy src"). If
SEBBY_TYPECHECK_COMMAND is unset, the type-check step is skipped entirely.
"""

import json
import os
import shlex
import subprocess
from pathlib import Path

DEFAULT_LINT_COMMAND = "ruff check --output-format=concise"


def _run_and_collect(command: list[str], cwd: Path, label: str, issues: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0 and result.stdout.strip():
        lines = result.stdout.strip().splitlines()
        preview = "\n".join(lines[:20])
        if len(lines) > 20:
            preview += f"\n… ({len(lines) - 20} more)"
        issues.append(f"{label}:\n{preview}")


def main() -> None:
    proj_root = Path(os.environ.get("CLAUDE_PROJECT_DIR", "."))
    issues: list[str] = []

    lint_command = [
        *shlex.split(os.environ.get("SEBBY_LINT_COMMAND", DEFAULT_LINT_COMMAND)),
        str(proj_root),
    ]
    _run_and_collect(lint_command, proj_root, "lint", issues)

    typecheck_raw = os.environ.get("SEBBY_TYPECHECK_COMMAND")
    if typecheck_raw:
        _run_and_collect(shlex.split(typecheck_raw), proj_root, "typecheck", issues)

    if issues:
        reason = "Quality gate failed — fix these before finishing:\n\n" + "\n\n".join(issues)
        print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/plugin/test_post_edit_lint.py tests/plugin/test_stop_quality_check.py -v`
Expected: PASS (7 passed)

- [ ] **Step 7: Run lint and type checks**

Run: `cd /Users/sebby/Developer/sebby && uv run ruff check plugin tests/plugin && uv run mypy plugin/hooks/scripts tests/plugin`
Expected: both clean

- [ ] **Step 8: Run the full suite to confirm no regressions**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest`
Expected: all previous tests plus these 15 pass

- [ ] **Step 9: Commit**

```bash
git add plugin/hooks/scripts/post_edit_lint.py plugin/hooks/scripts/stop_quality_check.py tests/plugin/fixtures tests/plugin/test_post_edit_lint.py tests/plugin/test_stop_quality_check.py
git commit -m "feat: add plugin quality-gate hooks (post_edit_lint, stop_quality_check)"
```

---

### Task 4: PR/docs workflow hooks

**Files:**
- Create: `plugin/hooks/scripts/post_pr_created.py`
- Create: `plugin/hooks/scripts/post_merge_docs_update.py`
- Create: `tests/plugin/test_post_pr_created.py`
- Create: `tests/plugin/test_post_merge_docs_update.py`

**Interfaces:**
- Consumes: nothing
- Produces: two standalone hook scripts; `post_merge_docs_update.py` is configured via `SEBBY_DOCS_FILES`

- [ ] **Step 1: Write the failing tests**

`tests/plugin/test_post_pr_created.py`:

```python
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parents[2] / "plugin" / "hooks" / "scripts" / "post_pr_created.py"


def run_hook(payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True
    )


def test_extracts_pr_url_from_tool_response():
    result = run_hook({"tool_response": "Created https://github.com/seby-dev/sebby/pull/42"})

    payload = json.loads(result.stdout)
    assert "https://github.com/seby-dev/sebby/pull/42" in payload["systemMessage"]


def test_falls_back_to_generic_reference_when_no_url_found():
    result = run_hook({"tool_response": "some unrelated output"})

    payload = json.loads(result.stdout)
    assert "the PR" in payload["systemMessage"]


def test_always_mentions_auto_merge_step():
    result = run_hook({"tool_response": ""})

    payload = json.loads(result.stdout)
    assert "auto-merge" in payload["systemMessage"]
```

`tests/plugin/test_post_merge_docs_update.py`:

```python
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parents[2] / "plugin" / "hooks" / "scripts" / "post_merge_docs_update.py"


def run_hook(payload: dict[str, object], env_overrides: dict[str, str] | None = None):
    env = {**os.environ, **(env_overrides or {})}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def test_extracts_pr_number_from_command():
    result = run_hook({"tool_input": {"command": "gh pr merge 42 --squash"}})

    payload = json.loads(result.stdout)
    assert "PR #42" in payload["systemMessage"]
    assert "docs/post-merge-sync-42" in payload["systemMessage"]


def test_falls_back_when_no_pr_number_found():
    result = run_hook({"tool_input": {"command": "gh pr merge --squash"}})

    payload = json.loads(result.stdout)
    assert "most recently merged PR" in payload["systemMessage"]


def test_uses_configured_docs_files():
    result = run_hook(
        {"tool_input": {"command": "gh pr merge 1"}},
        env_overrides={"SEBBY_DOCS_FILES": "README.md,ARCHITECTURE.md"},
    )

    payload = json.loads(result.stdout)
    assert "`README.md`" in payload["systemMessage"]
    assert "`ARCHITECTURE.md`" in payload["systemMessage"]


def test_defaults_to_readme_when_docs_files_unconfigured():
    result = run_hook({"tool_input": {"command": "gh pr merge 1"}})

    payload = json.loads(result.stdout)
    assert "`README.md`" in payload["systemMessage"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/plugin/test_post_pr_created.py tests/plugin/test_post_merge_docs_update.py -v`
Expected: FAIL (hook scripts don't exist yet)

- [ ] **Step 3: Implement `plugin/hooks/scripts/post_pr_created.py`**

```python
#!/usr/bin/env python3
"""PostToolUse hook: after `gh pr create`, nudge the auto-merge step.

Assumes review/quality gates are enforced elsewhere (before or during PR
creation); this hook only drives the post-create merge flow via the `gh`
CLI.
"""

import json
import re
import sys

try:
    payload = json.load(sys.stdin)
    blob = json.dumps(payload.get("tool_response", ""))
    m = re.search(r"https://github\.com/[^\s\"']+/pull/\d+", blob)
    pr_ref = m.group(0) if m else "the PR"
except Exception:
    pr_ref = "the PR"

print(
    json.dumps(
        {
            "systemMessage": (
                f"{pr_ref} created. Finish the ship step (gh CLI):\n"
                "1. Confirm CI: `gh pr checks` (or `gh pr checks --watch`).\n"
                "2. If every check passed, enable squash auto-merge: "
                "`gh pr merge --squash --auto --delete-branch`.\n"
                "3. If any check failed, report it to the user and do NOT merge."
            )
        }
    )
)
```

- [ ] **Step 4: Implement `plugin/hooks/scripts/post_merge_docs_update.py`**

```python
#!/usr/bin/env python3
"""PostToolUse hook: after `gh pr merge`, drive a docs-synchronisation pass.

Configure the doc files to check via SEBBY_DOCS_FILES (comma-separated,
default "README.md").
"""

import json
import os
import re
import sys

DEFAULT_DOCS_FILES = "README.md"


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}

    command = (payload.get("tool_input") or {}).get("command", "")
    response = json.dumps(payload.get("tool_response", ""))
    m = re.search(r"gh pr merge\s+(\d+)", command) or re.search(r"/pull/(\d+)", response)
    pr_num = m.group(1) if m else None

    docs_files = [
        f.strip()
        for f in os.environ.get("SEBBY_DOCS_FILES", DEFAULT_DOCS_FILES).split(",")
        if f.strip()
    ]
    docs_list = ", ".join(f"`{f}`" for f in docs_files)

    if pr_num:
        pr_ref = f"PR #{pr_num}"
        resolve_step = f"`gh pr view {pr_num} --json state,mergedAt`"
        diff_step = f"`gh pr diff {pr_num}` and `gh pr view {pr_num} --json files,title,body`"
        branch_name = f"docs/post-merge-sync-{pr_num}"
    else:
        pr_ref = "the most recently merged PR"
        resolve_step = (
            "first resolve the PR number with "
            "`gh pr list --state merged --limit 1 --json number,mergedAt,title` "
            "(call this $PR), then check state with `gh pr view $PR --json state,mergedAt`"
        )
        diff_step = "`gh pr diff $PR` and `gh pr view $PR --json files,title,body`"
        branch_name = "docs/post-merge-sync-$PR"

    print(
        json.dumps(
            {
                "systemMessage": (
                    f"`gh pr merge` just ran for {pr_ref}. Drive the docs-sync follow-up:\n"
                    f"1. Confirm the merge actually landed (auto-merge may be waiting on CI): "
                    f"{resolve_step}.\n"
                    "2. If state != MERGED, stop here — the docs sync only runs after the "
                    "merge completes.\n"
                    f"3. Read the merged diff: {diff_step} for context.\n"
                    f"4. Update any of {docs_list} whose documented surface changed (setup "
                    "steps, env vars, commands, feature list, architecture, integrations).\n"
                    "5. If none need an update, say so and stop.\n"
                    "6. If you edited any doc, spawn an Agent (subagent_type=general-purpose) "
                    'with a prompt like: "Read <the edited docs> against the latest code on '
                    'main. Report any factual inconsistency in under 250 words." Address any '
                    "findings before declaring done.\n"
                    f"7. Commit on a `{branch_name}` branch with a `docs:` conventional commit "
                    "and open a PR (same workflow as any other change)."
                )
            }
        )
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/plugin/test_post_pr_created.py tests/plugin/test_post_merge_docs_update.py -v`
Expected: PASS (7 passed)

- [ ] **Step 6: Run lint and type checks**

Run: `cd /Users/sebby/Developer/sebby && uv run ruff check plugin tests/plugin && uv run mypy plugin/hooks/scripts tests/plugin`
Expected: both clean

- [ ] **Step 7: Commit**

```bash
git add plugin/hooks/scripts/post_pr_created.py plugin/hooks/scripts/post_merge_docs_update.py tests/plugin/test_post_pr_created.py tests/plugin/test_post_merge_docs_update.py
git commit -m "feat: add plugin PR/docs workflow hooks"
```

---

### Task 5: Worktree-redirect hook

**Files:**
- Create: `plugin/hooks/scripts/worktree_redirect.py`
- Create: `tests/plugin/test_worktree_redirect.py`

**Interfaces:**
- Consumes: nothing
- Produces: a standalone `SessionStart` hook script, configured via `SEBBY_WORKTREE_DEV_PATH`

This generalizes `pmp-project`'s inline jq `SessionStart` hook (hardcoded to `/Users/sebby/Developer/pmp-project`) into a small, testable, parameterized Python script.

- [ ] **Step 1: Write the failing tests**

`tests/plugin/test_worktree_redirect.py`:

```python
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parents[2] / "plugin" / "hooks" / "scripts" / "worktree_redirect.py"


def run_hook(payload: dict[str, object], env_overrides: dict[str, str]):
    env = {**os.environ, **env_overrides}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def test_redirects_when_session_starts_at_project_root(tmp_path):
    result = run_hook(
        {"cwd": str(tmp_path)},
        {"CLAUDE_PROJECT_DIR": str(tmp_path), "SEBBY_WORKTREE_DEV_PATH": str(tmp_path / "dev")},
    )

    payload = json.loads(result.stdout)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert str(tmp_path / "dev") in context
    assert "SessionStart" == payload["hookSpecificOutput"]["hookEventName"]


def test_noop_when_cwd_is_not_project_root(tmp_path):
    result = run_hook(
        {"cwd": str(tmp_path / "subdir")},
        {"CLAUDE_PROJECT_DIR": str(tmp_path), "SEBBY_WORKTREE_DEV_PATH": str(tmp_path / "dev")},
    )

    assert result.stdout.strip() == ""


def test_noop_when_env_var_unset(tmp_path):
    result = run_hook({"cwd": str(tmp_path)}, {"CLAUDE_PROJECT_DIR": str(tmp_path)})

    assert result.stdout.strip() == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/plugin/test_worktree_redirect.py -v`
Expected: FAIL (hook script doesn't exist yet)

- [ ] **Step 3: Implement `plugin/hooks/scripts/worktree_redirect.py`**

```python
#!/usr/bin/env python3
"""SessionStart hook: redirect a session opened at the project root into a
configured dev worktree.

Configure via SEBBY_WORKTREE_DEV_PATH (absolute path to the worktree to
redirect into). No-ops if unset, or if the session's cwd isn't exactly
CLAUDE_PROJECT_DIR (i.e. already inside a worktree or subdirectory).
"""

import json
import os
import sys


def main() -> None:
    dev_path = os.environ.get("SEBBY_WORKTREE_DEV_PATH")
    if not dev_path:
        sys.exit(0)

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    cwd = payload.get("cwd", "")
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not project_dir or cwd != project_dir:
        sys.exit(0)

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": (
                        "Session started in the project root. Per project convention, run "
                        f"'cd {dev_path}' via Bash now, before any other action, to switch "
                        "into the dev worktree."
                    ),
                }
            }
        )
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/plugin/test_worktree_redirect.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Run lint and type checks**

Run: `cd /Users/sebby/Developer/sebby && uv run ruff check plugin tests/plugin && uv run mypy plugin/hooks/scripts tests/plugin`
Expected: both clean

- [ ] **Step 6: Run the full suite to confirm no regressions**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest`
Expected: all previous tests plus these 3 pass

- [ ] **Step 7: Commit**

```bash
git add plugin/hooks/scripts/worktree_redirect.py tests/plugin/test_worktree_redirect.py
git commit -m "feat: add plugin worktree-redirect SessionStart hook"
```

---

### Task 6: Wire up `hooks/hooks.json`

**Files:**
- Create: `plugin/hooks/hooks.json`

**Interfaces:**
- Consumes: all six hook scripts from Tasks 2-5 (by file path, referenced via `$CLAUDE_PLUGIN_ROOT`)
- Produces: the plugin's hook registration, auto-discovered by Claude Code

- [ ] **Step 1: Write `plugin/hooks/hooks.json`**

```json
{
  "description": "sebby-toolkit hooks: secret safety, quality gates, PR/docs workflow, worktree redirect",
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PLUGIN_ROOT/hooks/scripts/block_env_git.py\""
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PLUGIN_ROOT/hooks/scripts/post_pr_created.py\"",
            "if": "Bash(gh pr create:*)"
          },
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PLUGIN_ROOT/hooks/scripts/post_merge_docs_update.py\"",
            "if": "Bash(gh pr merge:*)"
          }
        ]
      },
      {
        "matcher": "Write|Edit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PLUGIN_ROOT/hooks/scripts/post_edit_lint.py\""
          }
        ]
      },
      {
        "matcher": "Read|Write|Edit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PLUGIN_ROOT/hooks/scripts/scrub_env_transcript.py\"",
            "async": true
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PLUGIN_ROOT/hooks/scripts/stop_quality_check.py\""
          },
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PLUGIN_ROOT/hooks/scripts/scrub_env_transcript.py\""
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PLUGIN_ROOT/hooks/scripts/worktree_redirect.py\""
          }
        ]
      }
    ]
  }
}
```

A plugin's `hooks/hooks.json` must nest the event-keyed object one level
deeper, under a top-level `"hooks"` key (plus a `"description"` field) —
this differs from a project's `.claude/settings.json`, where the
event-keyed object IS the top level of that file's own `"hooks"` field.

- [ ] **Step 2: Validate the JSON is well-formed**

Run: `cd /Users/sebby/Developer/sebby && python3 -c "import json; json.load(open('plugin/hooks/hooks.json'))" && echo OK`
Expected: `OK`

Well-formed JSON isn't sufficient proof this file is correct — it says
nothing about the required `"hooks"` wrapper shape. The real verification
is `claude plugin validate --strict ./plugin`, which checks the file
against the plugin schema; run it too and confirm it reports validation
passed.

- [ ] **Step 3: Commit**

```bash
git add plugin/hooks/hooks.json
git commit -m "feat: wire up plugin hooks.json"
```

---

### Task 7: `feature` skill

**Files:**
- Create: `plugin/skills/feature/SKILL.md`

**Interfaces:**
- Consumes: nothing (references the hooks from Tasks 2-6 by name in prose, not by import)
- Produces: a Claude Code skill, auto-discovered by Claude Code, invoked within the plugin's namespace (e.g. `sebby-toolkit:feature`)

This generalizes `organist_bot`'s `feature` skill: the original references `mcp__github__merge_pull_request` (a GitHub MCP server organist_bot has configured but this plugin can't assume every consumer has) and a hardcoded ruff-only lint assumption — both generalized to plain `gh` CLI usage and a reference to whatever `SEBBY_LINT_COMMAND` the consumer configured.

- [ ] **Step 1: Write `plugin/skills/feature/SKILL.md`**

```markdown
---
name: feature
description: End-to-end feature workflow — autopilot (plan+implement+PR) → simplify → code-review → security-review → verify. Use when implementing a new feature from scratch with full quality gates.
---

# Feature Implementation Workflow

## Overview

Full pipeline for shipping a new feature with quality gates baked in. Steps
run sequentially; any issues found in review steps are fixed before
proceeding to the next step.

## Steps

### 1. Implement — `/autopilot`

Invoke the `autopilot` skill (if available in this project) with the feature
description from args. Wait for it to complete — it scopes, plans,
implements, and opens a PR. If `autopilot` isn't available, use
`superpowers:brainstorming` → `superpowers:writing-plans` →
`superpowers:subagent-driven-development` instead.

### 2. Simplify — `/simplify`

Invoke the `simplify` skill on the changed files. Apply all suggested
cleanups (reuse, dead code, altitude). Re-run only this step if fixes are
needed.

### 3. Code review — `/code-review`

Invoke the `code-review` skill at **medium** effort with `--fix` to apply
findings automatically. If significant rework is needed, loop back to
step 2 after fixing.

### 4. Security review — `/security-review`

Invoke the `security-review` skill on the pending branch diff. Fix any
findings, then re-run steps 3-4 until both pass clean.

### 5. Verify — `/verify`

Invoke the `verify` skill to run the app and confirm the feature works
end-to-end on the golden path and key edge cases. Document any regressions
found and fix them before marking the workflow complete.

### 6. Report

Summarise in a single message:
- What was built (feature name, files changed)
- What each review step caught and fixed
- PR URL

## Usage

```
/feature <task description>
```

**Examples:**
```
/feature Add a keyword filter that rejects records whose name matches a configurable blocklist
/feature Add a get_pending_items tool so the user can ask what's awaiting a reply
```

## Notes

- If this plugin's hooks are installed, the `post_pr_created` hook fires
  automatically when a PR is opened and reminds Claude to check CI and
  merge — steps 3-4 of this skill are for the pre-PR review pass; the
  post-PR merge nudge comes from the hook, not this skill.
- After steps 2-4 pass clean, check CI status (`gh pr checks`) and merge
  via `gh pr merge --squash --auto --delete-branch` once CI is green.
- Push any fix commits to the same branch before CI completes — Claude will
  wait for the updated run.
- Skip `/verify` only if the change is purely internal (no user-visible
  behaviour, no runtime path changed) and say so explicitly in the report.
- If this plugin's `post_edit_lint` hook is installed and configured (via
  `SEBBY_LINT_COMMAND`), lint issues surface automatically after every file
  write — you don't need a separate manual lint pass.
```

- [ ] **Step 2: Commit**

```bash
git add plugin/skills/feature/SKILL.md
git commit -m "feat: add plugin feature skill"
```

---

### Task 8: CLAUDE.md snippet document, plugin validation, final docs

**Files:**
- Create: `docs/claude-md-snippets.md`
- Modify: `README.md` (mention the plugin's existence, one short paragraph pointing at `plugin/README.md`)

**Interfaces:**
- Consumes: all prior tasks' output (validates the whole plugin)
- Produces: the CLAUDE.md snippet document referenced by the spec

- [ ] **Step 1: Write `docs/claude-md-snippets.md`**

This covers the conventions already duplicated verbatim or near-verbatim across `staff2solfa`, `varrick-chorus-uk`, and `pmp-project`'s CLAUDE.md files (per the earlier audit): git conventions, quality standards, and the advisor-review pattern. A consuming project copies whichever sections apply into its own CLAUDE.md rather than restating them from scratch.

```markdown
# Shared CLAUDE.md snippets

Copy whichever sections below apply into your project's own `CLAUDE.md`
rather than writing them from scratch — these are the conventions found
duplicated verbatim (or near-verbatim) across `staff2solfa`,
`varrick-chorus-uk`, and `pmp-project`'s CLAUDE.md files.

## Git conventions

```markdown
## Git conventions

- Branch names: `feat/<topic>`, `fix/<topic>`, `chore/<topic>`.
- Conventional commits: `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`,
  `test:`.
- Never force-push. Never skip the pre-push hook.
```

## Quality standards

```markdown
## Quality standards

- Test-driven development: write the failing test before the
  implementation.
- No function over 40 lines; no file over 300 lines, for new code.
- Docstrings explain why, not what — only when that's not evident from the
  name and the code itself.
```

## Advisor & plan review

For complex or risky work, spawn a fresh advisor subagent — pointed at the
specific existing files it needs, not the whole codebase — to review a spec
or plan before implementation starts. The advisor reviews and comments; it
never implements. Skip this for small, well-scoped changes.

```markdown
## Advisor & Plan Review

For complex or risky work, before implementing:
1. Spawn a fresh subagent as an *advisor, not an implementer*, pointed at
   the specific existing files relevant to the change.
2. Ask it to review the spec (before writing the plan) and the plan
   (before implementation starts) for gaps, risks, and alternative
   approaches.
3. Skip this step for small, well-scoped changes — reserve it for work
   with real design ambiguity or blast radius.
```

## Full feature workflow

If a global `~/.claude/CLAUDE.md` already documents a brainstorm → spec →
plan → implement pipeline (e.g. via `superpowers:brainstorming` /
`superpowers:writing-plans` / `superpowers:subagent-driven-development`),
reference it instead of restating it:

```markdown
## Development workflow

Follow the brainstorm → spec → plan → implement pipeline documented in
`~/.claude/CLAUDE.md`'s Full Feature Workflow. Specs and plans go in
`docs/superpowers/specs/` and `docs/superpowers/plans/` respectively.
```
```

- [ ] **Step 2: Add a short pointer to the plugin in the repo's own `README.md`**

Add a new section after `## Shared config`:

```markdown
## Claude Code plugin

`plugin/` is a Claude Code plugin (`sebby-toolkit`) bundling hooks, a
feature-development skill, and a settings.json template — see
[`plugin/README.md`](plugin/README.md) for what's included and how to
install it. `docs/claude-md-snippets.md` has copy-paste CLAUDE.md sections
for the conventions the plugin's hooks assume.
```

- [ ] **Step 3: Run the full test suite one more time**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest`
Expected: all tests pass (previous package tests plus all `tests/plugin/` tests)

- [ ] **Step 4: Validate the plugin structure**

Use the `plugin-dev:plugin-validator` skill/agent against `plugin/` (per this repo's Global Constraints). Report any findings; fix straightforward ones (e.g. manifest field issues) inline. If a finding requires a design decision beyond this plan's scope (e.g. the exact `marketplace.json` schema can't be fully confirmed without a live install test), note it clearly in your report as a follow-up rather than guessing further.

- [ ] **Step 5: Commit**

```bash
git add docs/claude-md-snippets.md README.md
git commit -m "docs: add CLAUDE.md snippets and plugin README pointer"
```
