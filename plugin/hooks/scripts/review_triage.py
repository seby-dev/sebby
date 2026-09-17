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
        raise RuntimeError(
            f"git diff failed for base={base} in {project_dir}: {result.stderr.strip()}"
        )
    return [line for line in result.stdout.splitlines() if line]


def get_diff_text(base: str, project_dir: Path, max_chars: int = 20000) -> str:
    result = subprocess.run(
        ["git", "diff", f"{base}...HEAD"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git diff failed for base={base} in {project_dir}: {result.stderr.strip()}"
        )
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
