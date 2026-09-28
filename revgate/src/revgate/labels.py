"""The findings log, explicit labels, and the precision bookkeeping they feed.

Every finding a run produces is logged once per run. A label is a later row naming the
finding's id; only explicit labels count toward a rule's precision, and a label given as
an adjudication ruling also lands in the plan's ledger.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from revgate.model import RunFile, finding_to_dict
from revgate.store import append_jsonl_locked, findings_log_path, ledger_append, read_jsonl

LabelValue = Literal["tp", "fp"]


def log_findings(state: Path, rf: RunFile) -> None:
    """Append one `finding` row per finding, carrying the run's task, head, and role."""
    path = findings_log_path(state)
    for f in rf.findings:
        row: dict[str, object] = {"type": "finding", **finding_to_dict(f)}
        row.update(task=rf.task, head=rf.head, role=rf.role)
        append_jsonl_locked(path, row)


def _rows(state: Path) -> list[dict[str, object]]:
    return read_jsonl(findings_log_path(state))


def find_finding(state: Path, finding_id: str) -> dict[str, object] | None:
    """The latest logged row for the finding, or `None` if it was never logged."""
    latest: dict[str, object] | None = None
    for row in _rows(state):
        if row.get("type") == "finding" and row.get("id") == finding_id:
            latest = row
    return latest


def mark(
    state: Path,
    finding_id: str,
    label: LabelValue,
    *,
    labeler: str,
    note: str = "",
    ruling: bool = False,
    plan_slug: str | None = None,
) -> dict[str, object]:
    """Record an explicit label; as a ruling, also record it in the plan's ledger."""
    if label not in ("tp", "fp"):
        raise ValueError(f"label must be 'tp' or 'fp', not {label!r}")
    if ruling and plan_slug is None:
        raise ValueError("a ruling needs the plan slug of the ledger it lands in")
    found = find_finding(state, finding_id)
    if found is None:
        raise KeyError(finding_id)
    row: dict[str, object] = {
        "type": "label",
        "id": finding_id,
        "rule": found.get("rule"),
        "task": found.get("task"),
        "head": found.get("head"),
        "cluster_key": found.get("cluster_key"),
        "label": label,
        "strength": "explicit",
        "labeler": labeler,
        "note": note,
        "ruling": ruling,
    }
    append_jsonl_locked(findings_log_path(state), row)
    if ruling and plan_slug is not None:
        ledger_append(state, plan_slug, _ruling_entry(found, finding_id, label, labeler, note))
    return row


def _ruling_entry(
    found: Mapping[str, object], finding_id: str, label: LabelValue, labeler: str, note: str
) -> dict[str, object]:
    return {
        "kind": "ruling",
        "task": found.get("task"),
        "finding": finding_id,
        "rule": found.get("rule"),
        "cluster_key": found.get("cluster_key"),
        "witness_digest": found.get("witness_digest"),
        "verdict": "explained" if label == "fp" else "upheld",
        "labeler": labeler,
        "note": note,
    }


def _is_explicit_label(row: Mapping[str, object]) -> bool:
    return row.get("type") == "label" and row.get("strength") == "explicit"


def unlabeled(state: Path, limit: int) -> list[dict[str, object]]:
    """Up to `limit` logged findings with no explicit label, latest row each, oldest first."""
    rows = _rows(state)
    labelled = {row.get("id") for row in rows if _is_explicit_label(row)}
    latest: dict[str, dict[str, object]] = {}
    for row in rows:
        fid = row.get("id")
        if row.get("type") == "finding" and isinstance(fid, str) and fid not in labelled:
            latest[fid] = row  # keeps the first-logged order, with the latest row
    return list(latest.values())[: max(limit, 0)]


def rule_stats(state: Path, last: int = 30) -> dict[str, tuple[int, int]]:
    """Per rule, `(true, false)` positives over its last `last` explicitly labelled findings.

    A finding labelled more than once counts once, by its latest label, at that label's
    position in the log.
    """
    latest: dict[str, tuple[str, LabelValue]] = {}
    for row in _rows(state):
        fid, rule, value = row.get("id"), row.get("rule"), row.get("label")
        if not (_is_explicit_label(row) and isinstance(fid, str) and isinstance(rule, str)):
            continue
        if value in ("tp", "fp"):
            latest.pop(fid, None)
            latest[fid] = (rule, "tp" if value == "tp" else "fp")
    per_rule: dict[str, list[LabelValue]] = {}
    for rule, value in latest.values():
        per_rule.setdefault(rule, []).append(value)
    out: dict[str, tuple[int, int]] = {}
    for rule, values in sorted(per_rule.items()):
        window = values[-last:] if last > 0 else []
        tp = sum(1 for v in window if v == "tp")
        out[rule] = (tp, len(window) - tp)
    return out


def wilson_lower_bound(tp: int, n: int, z: float = 1.645) -> float:
    """The one-sided Wilson score lower bound of a rule's precision, `tp` of `n` labels."""
    if n <= 0:
        return 0.0
    p = tp / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return (centre - margin) / (1 + z2 / n)
