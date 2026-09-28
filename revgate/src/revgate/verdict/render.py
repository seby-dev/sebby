"""The implementer's summary on stdout, and the exit code.

Follows the algorithm spec's "The implementer's summary" with amendment A1: a status line,
every blocking finding, up to five E0 or E1 advisory findings marked `advisory`, evidence
lines while the budget lasts (blocking ones first), then `details:` and the instruction
line. Shadow findings never print. The whole summary is at most `max_lines` lines.
"""

from __future__ import annotations

from revgate.model import Finding, Grade, Tier, Verdict
from revgate.verdict.route import sort_key

ADVISORY_PRINT_MAX = 5
INSTRUCTION = (
    "fix blocking findings, or add `<id>: dispute — <reason>` to your report's "
    "revgate-responses block. Advisory lines don't block; a test you add in response is "
    "shown to the wave reviewer. Don't suppress, skip, or weaken a test to clear a finding."
)
_LIVE = ("open", "acknowledged")


def exit_code_for(v: Verdict) -> int:
    return 1 if v.blocking() else 0


def _printable_advisory(f: Finding) -> bool:
    return (
        f.tier is Tier.ADVISORY
        and f.status in _LIVE
        and f.grade <= Grade.E1_EXACT
        and "implementer" in f.audience
    )


def _counted_advisory(f: Finding) -> bool:
    """An advisory finding for this task: printed, or shown to the implementer as a count.
    A finding routed away (to the controller, not the implementer) isn't this task's."""
    routed_away = "controller" in f.audience and "implementer" not in f.audience
    return f.tier is Tier.ADVISORY and f.status in _LIVE and not routed_away


def _finding_line(f: Finding, advisory: bool) -> str:
    line = f"{f.file}:{f.line} {f.rule} {f.message}"
    if f.also:
        line += f"  also: {', '.join(f.also)}"
    return f"advisory {line}" if advisory else line


def _evidence_line(f: Finding) -> str:
    return f"    evidence: {f.evidence}   verify: {f.verify}"


def _status_line(
    v: Verdict,
    *,
    body: str,
    task: str | None,
    head: str,
    round_: int | None,
    checks: int | None,
    provisional: bool,
) -> str:
    parts = [f"revgate: {body}"]
    if v.focus:
        parts[0] += f", focus ({', '.join(v.focus_reasons)})"
    if task is not None:
        parts.append(f"task {task}")
    parts.append(head[:7])
    if round_ is not None:
        parts.append(f"round {round_}")
    if checks is not None:
        parts.append(f"({checks} checks)")
    if provisional:
        parts.append("provisional")
    return "  ".join(parts)


def render_summary(
    v: Verdict,
    *,
    task: str | None,
    head: str,
    round_: int,
    run_file_path: str,
    checks: int,
    provisional: bool = False,
    max_lines: int = 20,
) -> str:
    blocking = sorted(v.blocking(), key=sort_key)
    advisory = sorted((f for f in v.findings if _counted_advisory(f)), key=sort_key)
    if not blocking and not advisory:
        body = "clean"
        return _status_line(
            v,
            body=body,
            task=task,
            head=head,
            round_=None,
            checks=checks,
            provisional=provisional,
        )

    # Fixed lines: status, details, and the instruction (when anything prints).
    budget = max(max_lines - 3, 0)
    shown_blocking = blocking
    overflow: str | None = None
    if len(blocking) > budget:
        keep = max(budget - 1, 0)
        shown_blocking = blocking[:keep]
        overflow = f"… and {len(blocking) - keep} more blocking"
        budget = 0
    else:
        budget -= len(blocking)
    candidates = [f for f in advisory if _printable_advisory(f)][:ADVISORY_PRINT_MAX]
    shown_advisory = candidates[:budget]
    budget -= len(shown_advisory)

    with_evidence: set[str] = set()
    for f in [*shown_blocking, *shown_advisory]:
        if budget <= 0:
            break
        if f.evidence:
            with_evidence.add(f.id)
            budget -= 1

    lines: list[str] = []
    for f in shown_blocking:
        lines.append(_finding_line(f, advisory=False))
        if f.id in with_evidence:
            lines.append(_evidence_line(f))
    if overflow is not None:
        lines.append(overflow)
    for f in shown_advisory:
        lines.append(_finding_line(f, advisory=True))
        if f.id in with_evidence:
            lines.append(_evidence_line(f))

    counted = len(advisory) - len(shown_advisory)
    body = f"{len(blocking)} blocking, {len(shown_advisory)} advisory printed"
    if counted:
        body += f" (+{counted} counted)"
    status = _status_line(
        v,
        body=body,
        task=task,
        head=head,
        round_=round_,
        checks=None,
        provisional=provisional,
    )
    out = [status, *lines, f"details: {run_file_path}"]
    if lines:
        out.append(INSTRUCTION)
    return "\n".join(out[: max(max_lines, 1)])
