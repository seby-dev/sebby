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
from datetime import datetime, timezone
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
