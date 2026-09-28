"""Triage (the verdict phase): explain, acknowledge, cluster, route, and order findings.

Follows the algorithm spec's "Verdict (phase T4)". Stage 1a has only the static evidence
for explanation: a ruling in the ledger and a finding present at base. The behavioral
explanations (pinning tests, zero-delta proofs) arrive with later stages.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence

from revgate.config import Config
from revgate.model import (
    Anchor,
    Finding,
    Obligation,
    Response,
    Status,
    TaskScope,
    Tier,
    Verdict,
)
from revgate.rules.registry import TierTable
from revgate.verdict.route import FocusInputs, focus_flags, route, sort_key

_IMPACT_ORDER = {"critical": 0, "important": 1, "minor": 2}
Ruling = tuple[str, str, str | None]


def baseline_difference(head: Sequence[Finding], base: Sequence[Finding]) -> list[Finding]:
    """The head findings whose `(rule, cluster_key)` didn't already fire at base."""
    at_base = {(f.rule, f.cluster_key) for f in base}
    return [f for f in head if (f.rule, f.cluster_key) not in at_base]


def _disclosed(f: Finding, disclosed: frozenset[str]) -> bool:
    """A report disclosure covers a plan rule's finding by symbol, finding id, or cluster key."""
    if not f.rule.startswith("plan."):
        return False
    keys = {f.id, f.cluster_key} | ({f.symbol} if f.symbol is not None else set())
    return not keys.isdisjoint(disclosed)


def explain_or_acknowledge(
    f: Finding,
    *,
    disclosed: frozenset[str],
    responses: Mapping[str, Response],
    rulings: frozenset[Ruling],
) -> Status:
    """`explained` on independent evidence, `acknowledged` on the implementer's word.

    A ruling matches the exact `(rule, cluster_key, witness_digest)` triple, so it never
    silences a different witness on the same function. Otherwise the status is unchanged.
    """
    if (f.rule, f.cluster_key, f.witness_digest) in rulings:
        return "explained"
    response = responses.get(f.id)
    if response is not None and response.action == "intentional":
        return "acknowledged"
    if _disclosed(f, disclosed):
        return "acknowledged"
    return f.status


def _strength(f: Finding) -> tuple[object, ...]:
    return (f.grade, _IMPACT_ORDER[f.impact], *sort_key(f))


def cluster(findings: Sequence[Finding]) -> list[Finding]:
    """One finding per anchor: the strongest (lowest grade, then impact), with the other
    findings' rules listed in `also` as corroboration."""
    groups: dict[str, list[Finding]] = {}
    for f in findings:
        groups.setdefault(Anchor.of(f.file, f.symbol, f.line).key(), []).append(f)
    heads: list[Finding] = []
    for members in groups.values():
        head = min(members, key=_strength)
        others = {m.rule for m in members if m is not head} | set(head.also)
        others.discard(head.rule)
        heads.append(dataclasses.replace(head, also=tuple(sorted(others))))
    return heads


def decide(
    findings: Sequence[Finding],
    *,
    scope: TaskScope,
    tiers: TierTable,
    cfg: Config,
    disclosed: frozenset[str],
    responses: Mapping[str, Response],
    rulings: frozenset[Ruling],
    focus_inputs: FocusInputs,
    obligations: Sequence[Obligation] = (),
    unverified: Sequence[str] = (),
) -> Verdict:
    """The verdict: explain or acknowledge, route, cluster per tier, focus, and order.

    Routing runs before clustering, and clustering stays inside a tier, so a stronger
    advisory or shadow finding never absorbs a blocking one at the same anchor.
    """
    routed: list[Finding] = []
    for f in findings:
        status = explain_or_acknowledge(
            f, disclosed=disclosed, responses=responses, rulings=rulings
        )
        response = responses.get(f.id)
        disputed = response is not None and response.action == "dispute"
        explained = dataclasses.replace(f, status=status)
        routed.append(route(explained, tiers, cfg, scope, disputed=disputed))
    live = [f for f in routed if f.status in ("open", "acknowledged")]
    rest = [f for f in routed if f.status not in ("open", "acknowledged")]
    heads: list[Finding] = []
    for tier in Tier:
        heads.extend(cluster([f for f in live if f.tier is tier]))
    focus, reasons = focus_flags(heads, focus_inputs, cfg)
    return Verdict(
        findings=tuple(sorted(heads + rest, key=sort_key)),
        focus=focus,
        focus_reasons=reasons,
        obligations=tuple(obligations),
        unverified=tuple(sorted(set(unverified))),
    )
