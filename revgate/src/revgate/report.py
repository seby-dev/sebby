"""The implementer report model, from the algorithm spec's "Plan and report models".

A report gives three things. Claims: test counts ("234/234 passed") and gate claims
("ruff is clean"). Disclosures: identifiers and repository paths named in sections headed
deviation, concern, or changed; a disclosure only acknowledges a finding, it never
explains one. Responses: a fenced `revgate-responses` block with one `<id>: fixed`,
`<id>: intentional — <reason>`, or `<id>: dispute — <reason>` line per finding.

The report path comes from `[plan].report_glob`, which must name the plan (`{plan}`,
amendment A8); a pattern that doesn't, or one that matches more than one file, raises
`AmbiguousReport`, which the command line turns into exit 2.
"""

from __future__ import annotations

import glob
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from revgate.model import Response
from revgate.plan import Markdown, scan_markdown

# "changed" in the plan; widened to "change" and "changes", since real reports head the
# section "Changes made" or "Exact changes".
_DISCLOSURE_RE = re.compile(r"deviation|concern|\bchange[sd]?\b", re.I)
_RESPONSE_RE = re.compile(r"^([0-9a-f]{8}):\s*(fixed|intentional|dispute)(?:\s*[—-]+\s*(.*))?$")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_IDENT_RE = re.compile(r"[A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*")
_PATH_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]+)(?::\d+(?:-\d+)?)?")
_LINE_SUFFIX_RE = re.compile(r":\d+(?:\s*-\s*\d+)?$")
_TESTS_RE = re.compile(
    r"\b(\d+)\s*/\s*(\d+)\s+(?:tests?\s+)?passed\b|\b(\d+)\s+(?:tests?\s+)?passed\b"
)
_GATE_RE = re.compile(
    r"\b(ruff|mypy|eslint|tsc|bandit|semgrep|prettier|pyright|lint|make check|make pre-push)\b"
    r"[^.\n]{0,30}?\b(?:clean|green|passe[sd]|pass)\b",
    re.I,
)


class AmbiguousReport(Exception):
    """The report pattern doesn't name the plan, escapes the worktree, or matches twice."""


@dataclass(frozen=True)
class Claim:
    kind: str  # "tests_passed" or "gate_clean"
    text: str  # the line the claim was read from
    value: str  # "234/234" or "234" for tests; the tool's name, lower-cased, for a gate


@dataclass(frozen=True)
class ReportModel:
    path: str
    claims: tuple[Claim, ...]
    disclosed: frozenset[str]
    responses: Mapping[str, Response]
    text: str


def _disclosure_ranges(doc: Markdown) -> list[tuple[int, int]]:
    headings = doc.headings()
    ranges: list[tuple[int, int]] = []
    for n, (line, level, text) in enumerate(headings):
        if not _DISCLOSURE_RE.search(text):
            continue
        end = next((i for i, lvl, _t in headings[n + 1 :] if lvl <= level), len(doc.lines))
        ranges.append((line + 1, end))
    return ranges


def _mentions(line: str) -> set[str]:
    found: set[str] = set()
    for span in _BACKTICK_RE.findall(line):
        span = span.strip()
        if span.endswith("()"):
            span = span[:-2]
        if "/" in span:
            found.add(_LINE_SUFFIX_RE.sub("", span))
        elif _IDENT_RE.fullmatch(span):
            found.add(span)
            found.add(span.rsplit(".", 1)[-1])
    for path in _PATH_RE.findall(_BACKTICK_RE.sub(" ", line)):
        found.add(path)
    return found


def _disclosed(doc: Markdown) -> frozenset[str]:
    found: set[str] = set()
    for start, end in _disclosure_ranges(doc):
        for i in range(start, end):
            if not doc.in_fence[i]:
                found |= _mentions(doc.lines[i])
    return frozenset(found)


def _responses(doc: Markdown) -> dict[str, Response]:
    out: dict[str, Response] = {}
    for fence in doc.fences:
        if fence.info.split()[:1] != ["revgate-responses"]:
            continue
        for raw in fence.text.splitlines():
            m = _RESPONSE_RE.match(raw.strip())
            if m:
                action = cast(Literal["fixed", "intentional", "dispute"], m.group(2))
                out[m.group(1)] = Response(m.group(1), action, (m.group(3) or "").strip())
    return out


def _claims(doc: Markdown) -> tuple[Claim, ...]:
    claims: list[Claim] = []
    for i, raw in enumerate(doc.lines):
        if doc.in_fence[i]:
            continue
        line = raw.strip()
        for m in _TESTS_RE.finditer(line):
            value = f"{m.group(1)}/{m.group(2)}" if m.group(1) else m.group(3)
            claims.append(Claim("tests_passed", line, value))
        for m in _GATE_RE.finditer(line):
            claims.append(Claim("gate_clean", line, m.group(1).lower()))
    return tuple(dict.fromkeys(claims))


def parse_report(text: str, path: str) -> ReportModel:
    doc = scan_markdown(text)
    return ReportModel(
        path=path,
        claims=_claims(doc),
        disclosed=_disclosed(doc),
        responses=_responses(doc),
        text=text,
    )


def resolve_report_path(worktree: Path, pattern: str, plan_slug: str, task_id: str) -> Path | None:
    """The one report file the pattern names in `worktree`, or None when there's none."""
    if "{plan}" not in pattern:
        raise AmbiguousReport(f"report pattern {pattern!r} must contain {{plan}} (amendment A8)")
    rel = PurePosixPath(pattern)
    if rel.is_absolute() or ".." in rel.parts:
        raise AmbiguousReport(f"report pattern {pattern!r} must stay inside the worktree")
    filled = pattern.replace("{plan}", glob.escape(plan_slug)).replace("{id}", glob.escape(task_id))
    if ".." in PurePosixPath(filled).parts:
        raise AmbiguousReport(f"plan {plan_slug!r} or task {task_id!r} leaves the worktree")
    matches = sorted(p for p in worktree.glob(filled) if p.is_file())
    if len(matches) > 1:
        names = ", ".join(str(p.relative_to(worktree)) for p in matches)
        raise AmbiguousReport(f"report pattern {pattern!r} matches {len(matches)} files: {names}")
    return matches[0] if matches else None


def load_report(worktree: Path, pattern: str, plan_slug: str, task_id: str) -> ReportModel | None:
    path = resolve_report_path(worktree, pattern, plan_slug, task_id)
    if path is None:
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    return parse_report(text, path.relative_to(worktree).as_posix())
