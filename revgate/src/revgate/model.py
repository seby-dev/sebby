"""Core types: the contract between every rule, engine, and the verdict layer.

These follow the algorithm spec's "Data model". Every type is a frozen dataclass; routing
changes a finding with `dataclasses.replace`. Serialization is canonical JSON, so a run
file is byte-identical for identical inputs.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Literal, cast

MESSAGE_MAX = 110
EVIDENCE_MAX = 220
FIX_MAX = 100
BUCKET_LINES = 25


class Grade(IntEnum):
    """Strength of the evidence; lower is stronger."""

    E0_EXECUTED = 0
    E1_EXACT = 1
    E2_STRUCTURAL = 2
    E3_HEURISTIC = 3
    E4_CONTEXT = 4


class Source(StrEnum):
    """Where a rule's knowledge comes from."""

    POLICY = "policy"
    GENERIC = "generic"
    DECLARED = "declared"
    STRUCTURAL = "structural"
    DISTILLED = "distilled"
    STATISTICAL = "statistical"


class Tier(StrEnum):
    BLOCKING = "blocking"
    ADVISORY = "advisory"
    SHADOW = "shadow"


class Provenance(StrEnum):
    """How a test the task touched came to be; decides whether it may explain a delta."""

    NEW_R1 = "new_r1"
    EDITED_PLANNED = "edited_planned"
    EDITED_UNPLANNED = "edited_unplanned"
    AFTER_FINDING = "after_finding"


Impact = Literal["critical", "important", "minor"]
Kind = Literal["defect", "spec_gap", "test_gap", "integration", "gaming", "doc"]
Status = Literal[
    "open", "explained", "acknowledged", "suppressed", "deferred", "fixed", "not_examined"
]
AudienceName = Literal["implementer", "wave", "controller", "log"]
ObligationStatus = Literal["open", "deferred", "discharged", "unverified"]

_IMPACTS: frozenset[str] = frozenset({"critical", "important", "minor"})
_KINDS: frozenset[str] = frozenset(
    {"defect", "spec_gap", "test_gap", "integration", "gaming", "doc"}
)
_STATUSES: frozenset[str] = frozenset(
    {"open", "explained", "acknowledged", "suppressed", "deferred", "fixed", "not_examined"}
)
_OBLIGATION_STATUSES: frozenset[str] = frozenset({"open", "deferred", "discharged", "unverified"})


@dataclass(frozen=True)
class Anchor:
    """Unit of clustering, acknowledgment, and outcome tracking."""

    path: str
    symbol: str | None
    bucket: int | None

    @classmethod
    def of(cls, path: str, symbol: str | None, line: int) -> Anchor:
        return cls(path, symbol, None if symbol is not None else line // BUCKET_LINES)

    def key(self) -> str:
        if self.symbol is not None:
            return f"{self.path}::{self.symbol}"
        if self.bucket is not None:
            return f"{self.path}#{self.bucket}"
        return self.path


@dataclass(frozen=True)
class Obligation:
    oid: str
    kind: str
    rule: str
    anchor: Anchor
    source: str
    owner: str | None
    params: Mapping[str, str] = field(hash=False)
    engines: tuple[str, ...]
    impact: Impact
    question: str | None
    status: ObligationStatus = "open"


@dataclass(frozen=True)
class Witness:
    """Everything needed to replay a proof."""

    kind: Literal["excerpt", "command", "probe", "mutant", "delta", "corpus", "plan_quote"]
    summary: str
    input_ref: str | None
    base_out: str | None
    head_out: str | None
    command: str | None


@dataclass(frozen=True)
class Evidence:
    oid: str
    engine: str
    outcome: Literal["satisfied", "violated", "unknown", "incomplete"]
    grade: Grade
    corr_class: str
    witness: Witness | None
    units: int


@dataclass(frozen=True)
class Pin:
    """How strongly the tests observe one changed production function."""

    fid: str
    status: Literal["added", "modified", "removed", "doc_only", "moved"]
    executed_by_red_test: bool
    natural_mutant: Literal[
        "killed", "survived", "nonviable", "not_examined", "n/a:added", "n/a:signature"
    ]
    extreme_mutant: Literal["killed", "survived", "nonviable", "not_examined", "n/a"]
    decisions_killed: tuple[int, int]
    delta: Literal["none", "pinned", "silent", "unobserved"]
    pinned: bool


@dataclass(frozen=True)
class Finding:
    id: str
    rule: str
    tier: Tier
    review: bool
    kind: Kind
    grade: Grade
    source: Source
    impact: Impact
    file: str
    line: int
    end_line: int
    symbol: str | None
    message: str
    evidence: str
    fix: str | None
    verify: str
    witness_ref: str | None
    cluster_key: str
    witness_digest: str | None
    status: Status
    audience: frozenset[str]
    round: int
    also: tuple[str, ...] = ()


def _truncate(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[: cap - 1] + "…"


def new_finding(
    rule: str,
    *,
    file: str,
    line: int,
    message: str,
    evidence: str,
    grade: Grade,
    source: Source,
    kind: Kind = "defect",
    impact: Impact = "important",
    end_line: int | None = None,
    symbol: str | None = None,
    fix: str | None = None,
    evidence_key: str | None = None,
    witness_digest: str | None = None,
    round_: int = 1,
) -> Finding:
    """A shadow finding for the log; routing sets its tier and audience later."""
    anchor = Anchor.of(file, symbol, line)
    ev_key = evidence_key or evidence
    fid = hashlib.sha1(f"{rule}|{anchor.key()}|{ev_key}".encode()).hexdigest()[:8]
    cluster_key = f"{rule}|{symbol or anchor.key()}|{re.sub(r'\d+', '#', ev_key)}"
    return Finding(
        id=fid,
        rule=rule,
        tier=Tier.SHADOW,
        review=False,
        kind=kind,
        grade=grade,
        source=source,
        impact=impact,
        file=file,
        line=line,
        end_line=end_line or line,
        symbol=symbol,
        message=_truncate(message, MESSAGE_MAX),
        evidence=_truncate(evidence, EVIDENCE_MAX),
        fix=_truncate(fix, FIX_MAX) if fix is not None else None,
        verify=f"revgate recheck {fid}",
        witness_ref=None,
        cluster_key=cluster_key,
        witness_digest=witness_digest,
        status="open",
        audience=frozenset({"log"}),
        round=round_,
    )


@dataclass(frozen=True)
class Response:
    """One line of a report's `revgate-responses` block."""

    finding_id: str
    action: Literal["fixed", "intentional", "dispute"]
    reason: str


@dataclass(frozen=True)
class TaskScope:
    task_id: str | None
    owns: frozenset[str]
    runs: frozenset[str]
    ledger_assigned: frozenset[str] = frozenset()

    def covers(self, path: str) -> bool:
        """In scope: owned, run by the task, or assigned to it by a ledger entry."""
        return path in self.owns or path in self.runs or path in self.ledger_assigned


@dataclass(frozen=True)
class Unverified:
    """A check that couldn't reach a verdict because an input was missing."""

    rule: str
    note: str


RuleOutput = Finding | Obligation | Unverified


@dataclass(frozen=True)
class Verdict:
    findings: tuple[Finding, ...]
    focus: bool
    focus_reasons: tuple[str, ...]
    obligations: tuple[Obligation, ...]
    unverified: tuple[str, ...]

    def blocking(self) -> tuple[Finding, ...]:
        return tuple(
            f
            for f in self.findings
            if f.tier is Tier.BLOCKING
            and f.status in ("open", "acknowledged")
            and "implementer" in f.audience
        )


@dataclass(frozen=True)
class RunFile:
    task: str | None
    role: str
    base: str
    head: str
    plan: str | None
    plan_task_hash: str | None
    revgate_version: str
    source_hash: str
    config_hash: str
    focus: bool
    focus_reasons: tuple[str, ...]
    findings: tuple[Finding, ...]
    obligations: tuple[Obligation, ...]
    unverified: tuple[str, ...]
    incomplete: tuple[str, ...]
    exit_code: int
    provisional: bool = False


# --- serialization -------------------------------------------------------------------


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _get(d: Mapping[str, object], key: str) -> object:
    if key not in d:
        raise ValueError(f"missing field {key!r}")
    return d[key]


def _str(d: Mapping[str, object], key: str) -> str:
    value = _get(d, key)
    if not isinstance(value, str):
        raise ValueError(f"{key!r} must be a string, not {type(value).__name__}")
    return value


def _opt_str(d: Mapping[str, object], key: str) -> str | None:
    value = d.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key!r} must be a string or null, not {type(value).__name__}")
    return value


def _int(d: Mapping[str, object], key: str) -> int:
    value = _get(d, key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key!r} must be an integer, not {type(value).__name__}")
    return value


def _opt_int(d: Mapping[str, object], key: str) -> int | None:
    value = d.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key!r} must be an integer or null, not {type(value).__name__}")
    return value


def _bool(d: Mapping[str, object], key: str) -> bool:
    value = _get(d, key)
    if not isinstance(value, bool):
        raise ValueError(f"{key!r} must be a boolean, not {type(value).__name__}")
    return value


def _strs(
    d: Mapping[str, object], key: str, default: tuple[str, ...] | None = None
) -> tuple[str, ...]:
    if key not in d and default is not None:
        return default
    value = _get(d, key)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{key!r} must be a list of strings")
    return tuple(cast(list[str], value))


def _mapping(d: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = _get(d, key)
    if not isinstance(value, dict):
        raise ValueError(f"{key!r} must be an object")
    return cast(dict[str, object], value)


def _choice(d: Mapping[str, object], key: str, allowed: frozenset[str]) -> str:
    value = _str(d, key)
    if value not in allowed:
        raise ValueError(f"{key!r} has an unknown value {value!r}")
    return value


def finding_to_dict(f: Finding) -> dict[str, object]:
    return {
        "id": f.id,
        "rule": f.rule,
        "tier": f.tier.value,
        "review": f.review,
        "kind": f.kind,
        "grade": int(f.grade),
        "source": f.source.value,
        "impact": f.impact,
        "file": f.file,
        "line": f.line,
        "end_line": f.end_line,
        "symbol": f.symbol,
        "message": f.message,
        "evidence": f.evidence,
        "fix": f.fix,
        "verify": f.verify,
        "witness_ref": f.witness_ref,
        "cluster_key": f.cluster_key,
        "witness_digest": f.witness_digest,
        "status": f.status,
        "audience": sorted(f.audience),
        "round": f.round,
        "also": list(f.also),
    }


def finding_from_dict(d: Mapping[str, object]) -> Finding:
    return Finding(
        id=_str(d, "id"),
        rule=_str(d, "rule"),
        tier=Tier(_str(d, "tier")),
        review=_bool(d, "review"),
        kind=cast(Kind, _choice(d, "kind", _KINDS)),
        grade=Grade(_int(d, "grade")),
        source=Source(_str(d, "source")),
        impact=cast(Impact, _choice(d, "impact", _IMPACTS)),
        file=_str(d, "file"),
        line=_int(d, "line"),
        end_line=_int(d, "end_line"),
        symbol=_opt_str(d, "symbol"),
        message=_str(d, "message"),
        evidence=_str(d, "evidence"),
        fix=_opt_str(d, "fix"),
        verify=_str(d, "verify"),
        witness_ref=_opt_str(d, "witness_ref"),
        cluster_key=_str(d, "cluster_key"),
        witness_digest=_opt_str(d, "witness_digest"),
        status=cast(Status, _choice(d, "status", _STATUSES)),
        audience=frozenset(_strs(d, "audience")),
        round=_int(d, "round"),
        also=_strs(d, "also", ()),
    )


def _anchor_to_dict(a: Anchor) -> dict[str, object]:
    return {"path": a.path, "symbol": a.symbol, "bucket": a.bucket}


def _anchor_from_dict(d: Mapping[str, object]) -> Anchor:
    return Anchor(_str(d, "path"), _opt_str(d, "symbol"), _opt_int(d, "bucket"))


def obligation_to_dict(o: Obligation) -> dict[str, object]:
    return {
        "oid": o.oid,
        "kind": o.kind,
        "rule": o.rule,
        "anchor": _anchor_to_dict(o.anchor),
        "source": o.source,
        "owner": o.owner,
        "params": dict(o.params),
        "engines": list(o.engines),
        "impact": o.impact,
        "question": o.question,
        "status": o.status,
    }


def obligation_from_dict(d: Mapping[str, object]) -> Obligation:
    params = _mapping(d, "params")
    if not all(isinstance(v, str) for v in params.values()):
        raise ValueError("'params' values must be strings")
    return Obligation(
        oid=_str(d, "oid"),
        kind=_str(d, "kind"),
        rule=_str(d, "rule"),
        anchor=_anchor_from_dict(_mapping(d, "anchor")),
        source=_str(d, "source"),
        owner=_opt_str(d, "owner"),
        params={k: cast(str, v) for k, v in params.items()},
        engines=_strs(d, "engines"),
        impact=cast(Impact, _choice(d, "impact", _IMPACTS)),
        question=_opt_str(d, "question"),
        status=cast(ObligationStatus, _choice(d, "status", _OBLIGATION_STATUSES)),
    )


def run_file_to_dict(rf: RunFile) -> dict[str, object]:
    return {
        "task": rf.task,
        "role": rf.role,
        "base": rf.base,
        "head": rf.head,
        "plan": rf.plan,
        "plan_task_hash": rf.plan_task_hash,
        "revgate": {"version": rf.revgate_version, "source_hash": rf.source_hash},
        "config_hash": rf.config_hash,
        "focus": rf.focus,
        "focus_reasons": list(rf.focus_reasons),
        "findings": [finding_to_dict(f) for f in rf.findings],
        "obligations": [obligation_to_dict(o) for o in rf.obligations],
        "unverified": list(rf.unverified),
        "incomplete": list(rf.incomplete),
        "exit_code": rf.exit_code,
        "provisional": rf.provisional,
    }


def run_file_to_json(rf: RunFile) -> str:
    """Canonical JSON: sorted keys, no spaces, no timings (those go in the meta sidecar)."""
    return canonical(run_file_to_dict(rf))


def _objects(d: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    value = _get(d, key)
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise ValueError(f"{key!r} must be a list of objects")
    return cast(list[Mapping[str, object]], value)


def run_file_from_json(text: str) -> RunFile:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("a run file is a JSON object")
    d = cast(dict[str, object], data)
    rg = _mapping(d, "revgate")
    return RunFile(
        task=_opt_str(d, "task"),
        role=_str(d, "role"),
        base=_str(d, "base"),
        head=_str(d, "head"),
        plan=_opt_str(d, "plan"),
        plan_task_hash=_opt_str(d, "plan_task_hash"),
        revgate_version=_str(rg, "version"),
        source_hash=_str(rg, "source_hash"),
        config_hash=_str(d, "config_hash"),
        focus=_bool(d, "focus"),
        focus_reasons=_strs(d, "focus_reasons"),
        findings=tuple(finding_from_dict(f) for f in _objects(d, "findings")),
        obligations=tuple(obligation_from_dict(o) for o in _objects(d, "obligations")),
        unverified=_strs(d, "unverified"),
        incomplete=_strs(d, "incomplete"),
        exit_code=_int(d, "exit_code"),
        provisional=_bool(d, "provisional") if "provisional" in d else False,
    )
