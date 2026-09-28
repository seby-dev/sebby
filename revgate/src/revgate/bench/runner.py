"""`revgate bench`: replay recorded cases against the static reviewer.

A case is one TOML file in the cases directory (by default `<repo>/.revgate/bench/cases`,
kept in the repository whose history the cases replay). Each has an `id`, a `stage`, a
`tier` (`fast` or `full`), and a `kind`, which picks the checks:

- `task`: one or more replays of a base..head range (optionally with a plan task and a
  seed file that replaces a path at head), each with `[[expect]]` findings that must
  fire, `[[silent]]` rules that mustn't, and `[[deferred]]` obligations that must exist.
- `corpus`: every commit in a list against its first parent, with rules that must stay
  silent; with `owned = true`, each commit owns the files it changed.
- `ratchet_corpus`: the G11 ratchets' raw signal counts against a recorded table.
- `plan_edges`: plan tasks whose planned call edges never landed; nothing may block.

In `corpus` and `plan_edges` cases, every blocking finding fails the case unless an
`[[accepted_blocking]]` entry (`at`, `rule`, `file`, `line`, `judgment`, `reason`) records
its hand judgment as true. An entry judged false still fails the case, and so does an
entry that no longer matches a finding, so the judged list can't go stale.
- `perf`: the static phase's cold and warm timings, and its determinism.
- `delivery`: delivered recall on a normalized findings list.

Data files (`commits_file` and the like) resolve against the benchmark data directory
(`--data`, else `$REVGATE_BENCH_DATA`, else `~/Developer/revgate-bench-data`). A replay
never checks anything out: every tree is read by revision.
"""

from __future__ import annotations

import io
import json
import os
import statistics
import sys
import tempfile
import time
import tokenize
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from revgate import gitio
from revgate.config import Config, glob_match, load_config, parse_config
from revgate.gitio import GitError
from revgate.model import Finding, Grade, Obligation, RuleOutput, TaskScope, Tier
from revgate.plan import PlanTask, load_plan_task
from revgate.rules.registry import TierTable, load_tier_table
from revgate.spi.cache import BlobCache
from revgate.static.ratchets import ratchet_findings
from revgate.store import state_dir
from revgate.task_cmd import blob_cache, output_to_dict, review_static
from revgate.verdict.route import route

DEFAULT_DATA = Path("~/Developer/revgate-bench-data")
CASES_SUBDIR = Path(".revgate/bench/cases")
REVIEW_TOML = ".review.toml"
# Where a case repository's configuration comes from when the replayed base predates it:
# the first of these revisions that has one (override per case with `config_rev`).
CONFIG_REVS = ("feat/wave-pipeline-overhaul", "main")
LINE_SLACK = 5
SEVERITY_WEIGHT = {"critical": 3.0, "important": 2.0, "minor": 0.5}
# A rule's findings match a normalized item of these categories (the spec's "a matching
# category"); the first matching prefix wins.
RULE_CATEGORIES: tuple[tuple[str, frozenset[str]], ...] = (
    ("wiring.", frozenset({"wiring"})),
    ("plan.", frozenset({"plan", "wiring"})),
    ("contract.", frozenset({"contract"})),
    ("docs.", frozenset({"docs"})),
    ("policy.", frozenset({"test"})),
    ("lib.", frozenset({"other", "domain"})),
    ("gate.", frozenset({"other", "config", "test"})),
)
PERF_COLD_S = 20.0
PERF_MEDIAN_S = 5.0
PERF_P95_S = 10.0
PERF_MAX_LOAD = 8.0


class CaseError(Exception):
    """A case file that can't be read or names an input that doesn't exist."""


# --- case files -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Expect:
    rule: str
    file: str
    symbol: str | None = None
    line: int | None = None
    tier: str | None = None
    printed: bool | None = None
    contains: str | None = None
    grade: int | None = None
    review: bool | None = None


@dataclass(frozen=True)
class Silent:
    rule: str
    file: str | None = None
    symbol: str | None = None
    contains: str | None = None


@dataclass(frozen=True)
class Deferred:
    symbol: str
    owner: str


@dataclass(frozen=True)
class Replay:
    label: str
    base: str
    head: str
    plan: str | None = None
    plan_rev: str | None = None
    task: str | None = None
    owns: tuple[str, ...] = ()
    overrides: Mapping[str, str] = field(default_factory=dict)  # repo path -> seed file
    expect: tuple[Expect, ...] = ()
    silent: tuple[Silent, ...] = ()
    deferred: tuple[Deferred, ...] = ()
    deferred_count: int | None = None


@dataclass(frozen=True)
class Case:
    id: str
    stage: str
    tier: str
    kind: str
    repo: Path
    path: Path
    config_rev: str | None
    replays: tuple[Replay, ...]
    fields: Mapping[str, object]


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    passed: bool
    details: tuple[str, ...]
    seconds: float


_KINDS = frozenset({"task", "corpus", "ratchet_corpus", "plan_edges", "perf", "delivery"})


def _str(d: Mapping[str, object], key: str, where: str) -> str:
    value = d.get(key)
    if not isinstance(value, str) or not value:
        raise CaseError(f"{where}: `{key}` must be a non-empty string")
    return value


def _opt_str(d: Mapping[str, object], key: str, where: str) -> str | None:
    value = d.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CaseError(f"{where}: `{key}` must be a string")
    return value


def _tables(d: Mapping[str, object], key: str, where: str) -> list[Mapping[str, object]]:
    value = d.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise CaseError(f"{where}: `{key}` must be an array of tables")
    return value


def _strs(d: Mapping[str, object], key: str, where: str) -> tuple[str, ...]:
    value = d.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise CaseError(f"{where}: `{key}` must be a list of strings")
    return tuple(value)


def _expect(t: Mapping[str, object], where: str) -> Expect:
    line, printed, grade, review = t.get("line"), t.get("printed"), t.get("grade"), t.get("review")
    for name, value in (("line", line), ("grade", grade)):
        if value is not None and not isinstance(value, int):
            raise CaseError(f"{where}: expect `{name}` must be an integer")
    for name, value in (("printed", printed), ("review", review)):
        if value is not None and not isinstance(value, bool):
            raise CaseError(f"{where}: expect `{name}` must be true or false")
    return Expect(
        rule=_str(t, "rule", where),
        file=_str(t, "file", where),
        symbol=_opt_str(t, "symbol", where),
        line=line if isinstance(line, int) else None,
        tier=_opt_str(t, "tier", where),
        printed=printed if isinstance(printed, bool) else None,
        contains=_opt_str(t, "contains", where),
        grade=grade if isinstance(grade, int) else None,
        review=review if isinstance(review, bool) else None,
    )


def _silent(t: Mapping[str, object], where: str) -> Silent:
    return Silent(
        rule=_str(t, "rule", where),
        file=_opt_str(t, "file", where),
        symbol=_opt_str(t, "symbol", where),
        contains=_opt_str(t, "contains", where),
    )


def _replay(t: Mapping[str, object], label: str, where: str) -> Replay:
    overrides = t.get("overrides", {})
    if not isinstance(overrides, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in overrides.items()
    ):
        raise CaseError(f"{where}: `overrides` must map a repository path to a seed file")
    count = t.get("deferred_count")
    if count is not None and not isinstance(count, int):
        raise CaseError(f"{where}: `deferred_count` must be an integer")
    return Replay(
        label=_opt_str(t, "label", where) or label,
        base=_str(t, "base", where),
        head=_str(t, "head", where),
        plan=_opt_str(t, "plan", where),
        plan_rev=_opt_str(t, "plan_rev", where),
        task=_opt_str(t, "task", where),
        owns=_strs(t, "owns", where),
        overrides=dict(overrides),
        expect=tuple(_expect(e, where) for e in _tables(t, "expect", where)),
        silent=tuple(_silent(s, where) for s in _tables(t, "silent", where)),
        deferred=tuple(
            Deferred(_str(x, "symbol", where), _str(x, "owner", where))
            for x in _tables(t, "deferred", where)
        ),
        deferred_count=count,
    )


def load_case(path: Path, default_repo: Path) -> Case:
    where = str(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CaseError(f"{where}: {exc}") from exc
    kind = _str(data, "kind", where)
    if kind not in _KINDS:
        raise CaseError(f"{where}: unknown kind {kind!r}")
    tier = _str(data, "tier", where)
    if tier not in ("fast", "full"):
        raise CaseError(f"{where}: `tier` must be fast or full")
    repo_text = _opt_str(data, "repo", where)
    repo = Path(repo_text).expanduser() if repo_text else default_repo
    replays: list[Replay] = []
    if kind == "task":
        if "base" in data:
            replays.append(_replay(data, "main", where))
        for n, t in enumerate(_tables(data, "replay", where), start=1):
            replays.append(_replay(t, f"replay {n}", where))
        if not replays:
            raise CaseError(f"{where}: a task case needs `base` and `head` or a [[replay]]")
    return Case(
        id=_str(data, "id", where),
        stage=_str(data, "stage", where),
        tier=tier,
        kind=kind,
        repo=repo,
        path=path,
        config_rev=_opt_str(data, "config_rev", where),
        replays=tuple(replays),
        fields=data,
    )


def load_cases(directory: Path, stage: str, default_repo: Path | None = None) -> list[Case]:
    """Every case in `directory` for `stage` (a case's `stage` field may list several,
    such as "1a, 3"), sorted by id."""
    repo = default_repo if default_repo is not None else directory
    cases = [load_case(p, repo) for p in sorted(directory.glob("*.toml"))]
    wanted = [c for c in cases if stage in (s.strip() for s in c.stage.split(","))]
    return sorted(wanted, key=lambda c: c.id)


def bench_data_dir(explicit: Path | None = None) -> Path:
    """`explicit`, else `$REVGATE_BENCH_DATA`, else `~/Developer/revgate-bench-data`."""
    env = os.environ.get("REVGATE_BENCH_DATA")
    path = explicit if explicit is not None else Path(env) if env else DEFAULT_DATA
    path = path.expanduser()
    if not (path / "inputs").is_dir():
        raise FileNotFoundError(f"no benchmark inputs at {path / 'inputs'}")
    return path


# --- the shared replay machinery --------------------------------------------------------------


@dataclass
class _Env:
    """What every case in one bench run shares: the data directory, caches, the tiers."""

    data: Path | None
    tiers: TierTable = field(default_factory=load_tier_table)
    configs: dict[tuple[str, str, str | None], tuple[Config, str]] = field(default_factory=dict)

    def input_path(self, rel: str) -> Path:
        if self.data is None:
            raise CaseError(f"this case reads {rel}, and no benchmark data directory was given")
        path = self.data / rel
        if not path.exists():
            raise CaseError(f"missing benchmark input {path}")
        return path

    def config(self, repo: Path, base: str, config_rev: str | None) -> tuple[Config, str]:
        """The case repository's configuration at `base`, else at the first configured
        revision that has one, with a note naming which was used."""
        key = (str(repo), base, config_rev)
        if key not in self.configs:
            self.configs[key] = _resolve_config(repo, base, config_rev)
        return self.configs[key]


def _resolve_config(repo: Path, base: str, config_rev: str | None) -> tuple[Config, str]:
    cfg = load_config(repo, rev=base)
    if not cfg.is_default:
        return cfg, f"config {base[:7]}:{REVIEW_TOML}"
    for rev in (config_rev,) if config_rev else CONFIG_REVS:
        try:
            raw = gitio.show(repo, rev, REVIEW_TOML)
        except GitError:
            continue
        if raw is not None:
            return parse_config(raw, f"{rev}:{REVIEW_TOML}"), f"config {rev}:{REVIEW_TOML}"
    return cfg, "config: defaults (no .review.toml found)"


def _short(name: str | None) -> str | None:
    return name.rsplit(".", 1)[-1] if name else name


def _same_symbol(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and _short(a) == _short(b)


@dataclass(frozen=True)
class _Review:
    findings: tuple[Finding, ...]  # routed
    obligations: tuple[Obligation, ...]
    outputs: tuple[RuleOutput, ...]  # as the rules produced them, for determinism checks
    seconds: float
    config_note: str


def _review(
    env: _Env,
    repo: Path,
    base: str,
    head: str,
    *,
    config_rev: str | None,
    cache: BlobCache,
    plan: PlanTask | None = None,
    owns: Iterable[str] = (),
    overrides: Mapping[str, bytes | None] | None = None,
) -> _Review:
    cfg, note = env.config(repo, base, config_rev)
    extra = frozenset(owns)
    if plan is not None:
        scope = TaskScope(plan.task_id, plan.owns.all() | extra, frozenset(plan.runs))
    else:
        scope = TaskScope(None, extra, frozenset())
    start = time.monotonic()
    outputs, _ctx, _ratchet = review_static(
        repo,
        base,
        head,
        cfg=cfg,
        plan=plan,
        report=None,
        scope=scope,
        cache=cache,
        overrides=overrides,
    )
    seconds = time.monotonic() - start
    findings = tuple(route(o, env.tiers, cfg, scope) for o in outputs if isinstance(o, Finding))
    obligations = tuple(o for o in outputs if isinstance(o, Obligation))
    return _Review(findings, obligations, tuple(outputs), seconds, note)


def _describe(f: Finding) -> str:
    return f"{f.file}:{f.line} {f.rule} [{f.tier.value}] {f.message}"


def _expect_matches(e: Expect, f: Finding) -> bool:
    if f.rule != e.rule or f.file != e.file:
        return False
    if e.symbol is not None and not _same_symbol(e.symbol, f.symbol):
        return False
    if e.line is not None and abs(f.line - e.line) > LINE_SLACK:
        return False
    if e.tier is not None and f.tier.value != e.tier:
        return False
    if e.printed is not None and ("implementer" in f.audience) != e.printed:
        return False
    if e.grade is not None and int(f.grade) != e.grade:
        return False
    if e.review is not None and f.review != e.review:
        return False
    return e.contains is None or e.contains in f.message


def _rule_matches(pattern: str, rule: str) -> bool:
    """An exact rule id, or a family written `wiring.*`."""
    return rule.startswith(pattern[:-1]) if pattern.endswith(".*") else rule == pattern


def _silent_matches(s: Silent, f: Finding) -> bool:
    if not _rule_matches(s.rule, f.rule):
        return False
    if s.file is not None and f.file != s.file:
        return False
    if s.symbol is not None and not _same_symbol(s.symbol, f.symbol):
        return False
    return s.contains is None or s.contains in f.message


@dataclass(frozen=True)
class Accepted:
    """One hand-judged blocking finding: `at` is a prefix of the replayed commit or tip."""

    at: str
    rule: str
    file: str
    line: int
    judgment: str
    reason: str


def _accepted(case: Case) -> tuple[Accepted, ...]:
    where = str(case.path)
    out: list[Accepted] = []
    for t in _tables(case.fields, "accepted_blocking", where):
        line = t.get("line")
        judgment = _str(t, "judgment", where)
        if not isinstance(line, int) or judgment not in ("true", "false"):
            raise CaseError(
                f"{where}: accepted_blocking needs an integer `line` and a "
                "`judgment` of true or false"
            )
        out.append(
            Accepted(
                at=_str(t, "at", where),
                rule=_str(t, "rule", where),
                file=_str(t, "file", where),
                line=line,
                judgment=judgment,
                reason=_str(t, "reason", where),
            )
        )
    return tuple(out)


class _Judge:
    """Sorts a case's blocking findings against its `[[accepted_blocking]]` entries."""

    def __init__(self, accepted: tuple[Accepted, ...]) -> None:
        self.accepted = accepted
        self.used: set[Accepted] = set()
        self.problems: list[str] = []
        self.judged: list[str] = []

    def blocking(self, rev: str, label: str, f: Finding) -> None:
        match = next(
            (
                a
                for a in self.accepted
                if rev.startswith(a.at)
                and a.rule == f.rule
                and a.file == f.file
                and a.line == f.line
            ),
            None,
        )
        if match is None:
            self.problems.append(f"! {label}: unjudged blocking {_describe(f)}")
            return
        self.used.add(match)
        if match.judgment == "false":
            self.problems.append(f"! {label}: false block {_describe(f)} ({match.reason})")
        else:
            self.judged.append(f"{label}: blocking, judged true ({match.reason}) {_describe(f)}")

    def stale(self) -> list[str]:
        return [
            f"! accepted_blocking matched nothing: {a.at[:9]} {a.file}:{a.line} {a.rule}"
            for a in self.accepted
            if a not in self.used
        ]


def _changed_files(repo: Path, base: str, head: str) -> frozenset[str]:
    out = gitio.run_git(repo, "diff", "--name-only", "-z", "--no-ext-diff", base, head)
    return frozenset(os.fsdecode(p) for p in out.split(b"\0") if p)


def _expect_text(e: Expect) -> str:
    optional = {
        "symbol": e.symbol,
        "line": e.line,
        "tier": e.tier,
        "printed": e.printed,
        "contains": e.contains,
        "grade": e.grade,
        "review": e.review,
    }
    return " ".join([e.rule, e.file, *(f"{k}={v}" for k, v in optional.items() if v is not None)])


def check_replay(replay: Replay, review: _Review) -> list[str]:
    """Every way the replay's outputs differ from what its case expects."""
    problems: list[str] = []
    for e in replay.expect:
        if not any(_expect_matches(e, f) for f in review.findings):
            near = [_describe(f) for f in review.findings if f.rule == e.rule][:2]
            seen = f" (fired: {'; '.join(near)})" if near else " (didn't fire)"
            problems.append(f"missing {_expect_text(e)}{seen}")
    for s in replay.silent:
        for f in review.findings:
            if _silent_matches(s, f):
                problems.append(f"not silent: {_describe(f)}")
    deferred = [o for o in review.obligations if o.status == "deferred"]
    for d in replay.deferred:
        if not any(
            _same_symbol(d.symbol, o.anchor.symbol) and o.owner == d.owner for o in deferred
        ):
            problems.append(f"no deferred obligation for {d.symbol} owned by Task {d.owner}")
    if replay.deferred_count is not None and len(deferred) != replay.deferred_count:
        names = ", ".join(sorted(f"{o.anchor.symbol}->{o.owner}" for o in deferred))
        problems.append(
            f"{len(deferred)} deferred obligations, expected {replay.deferred_count}: {names}"
        )
    return problems


# --- kinds ------------------------------------------------------------------------------------


def _load_plan(repo: Path, rev: str, plan: str, task: str) -> PlanTask:
    loaded = load_plan_task(repo, rev, plan, task)
    if loaded is None:
        raise CaseError(f"{plan} at {rev[:9]} has no task {task}")
    return loaded


def _overrides(case: Case, replay: Replay) -> dict[str, bytes | None]:
    out: dict[str, bytes | None] = {}
    for repo_path, seed in replay.overrides.items():
        seed_path = case.path.parent / seed
        if not seed_path.is_file():
            raise CaseError(f"{case.path.name}: seed file {seed_path} doesn't exist")
        out[repo_path] = seed_path.read_bytes()
    return out


def _run_task_case(case: Case, env: _Env, cache: BlobCache) -> tuple[bool, list[str]]:
    details: list[str] = []
    ok = True
    for replay in case.replays:
        plan = None
        if replay.plan is not None:
            if replay.task is None:
                raise CaseError(f"{case.path.name}: {replay.label} has a plan and no task")
            plan = _load_plan(case.repo, replay.plan_rev or replay.base, replay.plan, replay.task)
        review = _review(
            env,
            case.repo,
            replay.base,
            replay.head,
            config_rev=case.config_rev,
            cache=cache,
            plan=plan,
            owns=replay.owns,
            overrides=_overrides(case, replay) or None,
        )
        problems = check_replay(replay, review)
        ok = ok and not problems
        details.append(
            f"{replay.label}: {replay.base[:9]}..{replay.head[:9]} {review.config_note}"
            f" {'ok' if not problems else 'FAIL'}"
        )
        details.extend(f"! {replay.label}: {p}" for p in problems)
    return ok, details


def _first_parent(repo: Path, commit: str) -> str | None:
    try:
        return gitio.rev_parse(repo, f"{commit}^1")
    except GitError:
        return None  # a root commit


def _corpus_commits(case: Case, env: _Env) -> list[str]:
    path = env.input_path(_str(case.fields, "commits_file", str(case.path)))
    limit = case.fields.get("limit")
    commits = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return commits[:limit] if isinstance(limit, int) else commits


def _silents(case: Case) -> tuple[Silent, ...]:
    return tuple(_silent(s, str(case.path)) for s in _tables(case.fields, "silent", str(case.path)))


def _run_corpus(case: Case, env: _Env, cache: BlobCache) -> tuple[bool, list[str]]:
    silents = _silents(case)
    commits = _corpus_commits(case, env)
    # `owned = true`: each commit owns the files it changed, as a controller run of a task
    # whose plan names them would, so the rules that block only in owned files can block.
    owned = case.fields.get("owned", False) is True
    judge = _Judge(_accepted(case))
    problems: list[str] = []
    count = 0
    for commit in commits:
        base = _first_parent(case.repo, commit)
        if base is None:
            continue
        owns = _changed_files(case.repo, base, commit) if owned else frozenset()
        review = _review(
            env, case.repo, base, commit, config_rev=case.config_rev, cache=cache, owns=owns
        )
        for f in review.findings:
            if any(_silent_matches(s, f) for s in silents):
                problems.append(f"! not silent at {commit[:9]}: {_describe(f)}")
            if f.tier is Tier.BLOCKING:
                count += 1
                judge.blocking(commit, f"at {commit[:9]}", f)
    problems += judge.problems + judge.stale()
    details = [f"commits {len(commits)}, blocking {count}", *problems, *judge.judged]
    return not problems, details


def _run_ratchet_corpus(case: Case, env: _Env) -> tuple[bool, list[str]]:
    where = str(case.path)
    units_data = json.loads(env.input_path(_str(case.fields, "units_file", where)).read_text())
    units = units_data["units"] if isinstance(units_data, dict) else units_data
    rows = json.loads(env.input_path(_str(case.fields, "signals_file", where)).read_text())
    if len(rows) != len(units):
        raise CaseError(f"{where}: {len(rows)} signal rows for {len(units)} units")
    tolerance = case.fields.get("tolerance", 1)
    assert isinstance(tolerance, int)
    accepted = {
        (str(t.get("unit")), str(t.get("signal")))
        for t in _tables(case.fields, "accepted_mismatches", where)
    }
    signals = _strs(case.fields, "signals", where)
    # `owned = true`: each unit's plan owns the files it changed, as a controller run of
    # that task with a matching plan would; the raw counts don't depend on it.
    owned = case.fields.get("owned", False) is True
    totals_ours: dict[str, int] = dict.fromkeys(signals, 0)
    totals_theirs: dict[str, int] = dict.fromkeys(signals, 0)
    problems: list[str] = []
    blocking: list[str] = []
    for n, (unit, row) in enumerate(zip(units, rows, strict=True)):
        fork, tip = str(unit["fork"]), str(unit["tip"])
        cfg, _note = env.config(case.repo, fork, case.config_rev)
        files = frozenset(str(p) for p in unit.get("files", []))
        result = ratchet_findings(
            case.repo,
            fork,
            tip,
            cfg=cfg,
            plan_owns=files if owned else frozenset(),
            plan_text="",
            plan_path=None,
        )
        for sig in signals:
            ours, theirs = result.raw_counts.get(sig, 0), int(row.get(sig, 0))
            totals_ours[sig] += ours
            totals_theirs[sig] += theirs
            if abs(ours - theirs) > tolerance and (str(n), sig) not in accepted:
                problems.append(f"! unit {n} {tip[:9]} {sig}: {ours} vs r5 {theirs}")
        scope = TaskScope(None, files, frozenset())
        for f in result.findings:
            routed = route(f, env.tiers, cfg, scope)
            if routed.tier is Tier.BLOCKING:
                blocking.append(f"blocking unit {n} {tip[:9]}: {_describe(routed)}")
    sums = ", ".join(f"{s} {totals_ours[s]}/{totals_theirs[s]}" for s in signals)
    details = [f"units {len(units)}; ours/r5: {sums}", *problems, *blocking]
    return not problems, details


def _prod_reference_count(repo: Path, rev: str, name: str, cfg: Config) -> int:
    """Uses of `name` as a Python name token in production files at `rev`, less the name
    in its own `def` or `class` line; a mention in a comment or docstring isn't a use."""
    try:
        out = gitio.run_git(repo, "grep", "-l", "-w", "-I", "-e", name, rev, "--", "*.py")
    except GitError:
        return 0  # git grep exits 1 when nothing matches
    count = 0
    for hit in out.decode("utf-8", errors="replace").splitlines():
        path = hit.split(":", 1)[1] if ":" in hit else hit
        if not glob_match(path, cfg.prod_globs) or glob_match(path, cfg.test_globs):
            continue
        raw = gitio.show(repo, rev, path)
        if raw is None:
            continue
        previous = ""
        try:
            tokens = tokenize.generate_tokens(io.StringIO(raw.decode("utf-8", "replace")).readline)
            for tok in tokens:
                if (
                    tok.type == tokenize.NAME
                    and tok.string == name
                    and previous
                    not in (
                        "def",
                        "class",
                    )
                ):
                    count += 1
                if tok.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT):
                    previous = tok.string
        except (tokenize.TokenError, SyntaxError):
            continue
    return count


def _run_plan_edges(case: Case, env: _Env, cache: BlobCache) -> tuple[bool, list[str]]:
    where = str(case.path)
    edges = json.loads(env.input_path(_str(case.fields, "edges_file", where)).read_text())
    prefix = str(case.fields.get("plan_dir", "docs/superpowers/plans/"))
    wanted: dict[tuple[str, str], list[Mapping[str, object]]] = {}
    for e in edges["missing"]:
        if not e.get("ever_present_since_plan"):
            wanted.setdefault((prefix + str(e["plan"]), str(e["task"])), []).append(e)
    records = {(str(t["plan"]), str(t["task"])): t for t in _tables(case.fields, "task", where)}
    skipped_ok = {
        (str(t["plan"]), str(t["task"])): str(t.get("reason", ""))
        for t in _tables(case.fields, "skipped", where)
    }
    judge = _Judge(_accepted(case))
    problems: list[str] = []
    notes: list[str] = []
    evaluated = skipped = 0
    for key, entries in wanted.items():
        plan_path, task = key
        rec = records.get(key)
        reason = None
        plan: PlanTask | None = None
        if rec is None:
            reason = "no recorded tip and base"
        else:
            try:
                plan = load_plan_task(case.repo, str(rec["plan_rev"]), plan_path, task)
            except GitError as exc:
                reason = f"plan unreadable: {exc}"
            if plan is None and reason is None:
                reason = f"no task {task} in the plan at {str(rec['plan_rev'])[:9]}"
        if plan is None or rec is None:
            skipped += 1
            if skipped_ok.get(key):
                notes.append(f"skipped {plan_path} Task {task}: {skipped_ok[key]}")
            else:
                problems.append(f"! skipped {plan_path} Task {task}: {reason}")
            continue
        evaluated += 1
        base, tip = str(rec["base"]), str(rec["tip"])
        review = _review(
            env, case.repo, base, tip, config_rev=case.config_rev, cache=cache, plan=plan
        )
        cfg, _note = env.config(case.repo, base, case.config_rev)
        label = f"{plan_path.rsplit('/', 1)[-1]} Task {task} {base[:9]}..{tip[:9]}"
        for f in review.findings:
            if f.tier is Tier.BLOCKING:
                judge.blocking(tip, label, f)
            if f.rule == "plan.call_edge_missing" and (
                f.tier is not Tier.ADVISORY or f.grade is not Grade.E2_STRUCTURAL
            ):
                problems.append(f"! {label}: call edge not advisory E2: {_describe(f)}")
            if f.rule == "wiring.unwired_planned_here" and f.symbol is not None:
                refs = _prod_reference_count(case.repo, tip, f.symbol, cfg)
                if refs:
                    problems.append(f"! {label}: {f.symbol} has {refs} production refs")
        edges_here = ", ".join(sorted({f"{e['caller']}->{e['callee']}" for e in entries}))
        found = sorted({f.rule for f in review.findings if f.tier is not Tier.SHADOW})
        notes.append(f"{label}: edges {edges_here}; rules {', '.join(found) or 'none'}")
    problems += judge.problems + judge.stale()
    head = f"evaluated {evaluated}/{len(wanted)} tasks, skipped {skipped}"
    return not problems, [head, *problems, *judge.judged, *notes]


def _percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    k = max(0, min(len(ordered) - 1, round(q * (len(ordered) - 1))))
    return ordered[k]


def _run_perf(case: Case, env: _Env) -> tuple[bool, list[str]]:
    load = os.getloadavg()[0]
    commits = _corpus_commits(case, env)
    pairs = [(b, c) for c in commits if (b := _first_parent(case.repo, c)) is not None]
    if not pairs:
        raise CaseError(f"{case.path}: no commits to time")
    with tempfile.TemporaryDirectory(prefix="revgate-bench-") as tmp:
        cache = BlobCache(Path(tmp) / "spi", "bench-perf")
        base0, head0 = pairs[0]
        cold = _review(env, case.repo, base0, head0, config_rev=case.config_rev, cache=cache)
        passes: list[list[tuple[float, str]]] = []
        for _ in range(2):
            this: list[tuple[float, str]] = []
            for base, head in pairs:
                r = _review(env, case.repo, base, head, config_rev=case.config_rev, cache=cache)
                payload = json.dumps(
                    [output_to_dict(o) for o in r.outputs], sort_keys=True, separators=(",", ":")
                )
                this.append((r.seconds, payload))
            passes.append(this)
    warm = [s for s, _p in passes[1]]
    median, p95 = statistics.median(warm), _percentile(warm, 0.95)
    differ = [pairs[i][1][:9] for i in range(len(pairs)) if passes[0][i][1] != passes[1][i][1]]
    load_after = os.getloadavg()[0]
    details = [
        f"cold {cold.seconds:.2f} s; warm median {median:.2f} s, p95 {p95:.2f} s over "
        f"{len(warm)} commits; first pass median {statistics.median(s for s, _ in passes[0]):.2f}"
        f" s; load {load:.1f} -> {load_after:.1f}",
    ]
    problems = []
    if cold.seconds > PERF_COLD_S:
        problems.append(f"! cold run {cold.seconds:.2f} s > {PERF_COLD_S} s")
    if median > PERF_MEDIAN_S:
        problems.append(f"! warm median {median:.2f} s > {PERF_MEDIAN_S} s")
    if p95 > PERF_P95_S:
        problems.append(f"! warm p95 {p95:.2f} s > {PERF_P95_S} s")
    if differ:
        problems.append(f"! outputs differ between runs at {', '.join(differ[:10])}")
    if problems and not differ and max(load, load_after) > PERF_MAX_LOAD:
        return True, [f"SKIP (load {max(load, load_after):.1f})", *details, *problems]
    return not problems, [*details, *problems]


@dataclass(frozen=True)
class _Item:
    id: str
    file: str | None
    symbol: str | None
    lines: tuple[int, int] | None
    category: str
    weight: float


def _item_matches(item: _Item, path: str, symbol: str | None, line: int | None, rule: str) -> bool:
    if item.file is None or path != item.file:
        return False
    cats = next((c for p, c in RULE_CATEGORIES if rule.startswith(p)), frozenset())
    if item.category not in cats:
        return False
    if item.symbol is not None and symbol is not None:
        return _same_symbol(item.symbol, symbol)
    if item.lines is not None and line is not None:
        lo, hi = item.lines
        return lo - LINE_SLACK <= line <= hi + LINE_SLACK
    return False


def _run_delivery(case: Case, env: _Env, cache: BlobCache) -> tuple[bool, list[str]]:
    where = str(case.path)
    rows = json.loads(env.input_path(_str(case.fields, "findings_file", where)).read_text())
    ranges = {str(t["id"]): t for t in _tables(case.fields, "range", where)}
    must = _strs(case.fields, "must_reach_implementer", where)
    total = sum(SEVERITY_WEIGHT[str(r["severity"])] for r in rows)
    task_w = any_w = 0.0
    delivered_task: list[str] = []
    delivered_any: list[str] = []
    replayed = 0
    reviews: dict[tuple[str, ...], _Review] = {}
    for row in rows:
        rid = str(row["id"])
        rng: Mapping[str, object] = ranges.get(rid, row)
        base, head = rng.get("base"), rng.get("head")
        if not (isinstance(base, str) and isinstance(head, str) and row.get("file")):
            continue
        replayed += 1
        plan_path, task = rng.get("plan"), rng.get("task")
        key = (base, head, str(plan_path), str(task))
        if key not in reviews:
            plan = None
            owns: tuple[str, ...] = ()
            if isinstance(plan_path, str) and isinstance(task, str):
                plan_rev = rng.get("plan_rev")
                rev = plan_rev if isinstance(plan_rev, str) else base
                plan = _load_plan(case.repo, rev, plan_path, task)
            else:  # a range with no plan: the task owns what it changed
                owns = tuple(p for _l, p, _o in gitio.diff_name_status(case.repo, base, head))
            reviews[key] = _review(
                env,
                case.repo,
                base,
                head,
                config_rev=case.config_rev,
                cache=cache,
                plan=plan,
                owns=owns,
            )
        review = reviews[key]
        lines = row.get("lines")
        item = _Item(
            rid,
            str(row["file"]),
            str(row["symbol"]) if row.get("symbol") else None,
            (int(lines[0]), int(lines[1])) if isinstance(lines, list) else None,
            str(row["category"]),
            SEVERITY_WEIGHT[str(row["severity"])],
        )
        hits = [f for f in review.findings if _item_matches(item, f.file, f.symbol, f.line, f.rule)]
        to_task = any("implementer" in f.audience for f in hits)
        to_any = to_task or any(f.audience - {"log"} for f in hits)
        to_any = to_any or any(
            _item_matches(item, o.anchor.path, o.anchor.symbol, None, o.rule)
            for o in review.obligations
        )
        if to_task:
            task_w += item.weight
            delivered_task.append(rid)
        if to_any:
            any_w += item.weight
            delivered_any.append(rid)
    frr_task, frr_any = task_w / total, any_w / total
    missing = [m for m in must if m not in delivered_task]
    details = [
        f"FRR_delivered_task {frr_task:.3f} ({', '.join(delivered_task) or 'none'}); "
        f"FRR_delivered_any {frr_any:.3f} ({', '.join(delivered_any) or 'none'}); "
        f"replayed {replayed}/{len(rows)}, weight {total:g}",
        *(f"! {m} didn't reach the implementer" for m in missing),
    ]
    return not missing, details


# --- running ----------------------------------------------------------------------------------


def run_case(case: Case, *, cache: BlobCache, data: Path | None = None) -> CaseResult:
    """One case's result; an unreadable input fails the case with the reason."""
    env = _Env(data)
    return _run_case_in(case, env, cache)


def _run_case_in(case: Case, env: _Env, cache: BlobCache) -> CaseResult:
    start = time.monotonic()
    runners: dict[str, Callable[[], tuple[bool, list[str]]]] = {
        "task": lambda: _run_task_case(case, env, cache),
        "corpus": lambda: _run_corpus(case, env, cache),
        "ratchet_corpus": lambda: _run_ratchet_corpus(case, env),
        "plan_edges": lambda: _run_plan_edges(case, env, cache),
        "perf": lambda: _run_perf(case, env),
        "delivery": lambda: _run_delivery(case, env, cache),
    }
    try:
        passed, details = runners[case.kind]()
    except (CaseError, GitError, KeyError, ValueError) as exc:
        passed, details = False, [f"! {type(exc).__name__}: {exc}"]
    return CaseResult(case.id, passed, tuple(details), round(time.monotonic() - start, 2))


def result_line(result: CaseResult) -> str:
    """`PASS C2 2.1 s`, `SKIP C30 (load 9.3)`, or `FAIL C2: <the first problem>`."""
    if result.details and result.details[0].startswith("SKIP"):
        return f"SKIP {result.case_id} {result.details[0].removeprefix('SKIP ')}"
    if result.passed:
        return f"PASS {result.case_id} {result.seconds:.1f} s"
    problem = next((d[2:] for d in result.details if d.startswith("! ")), "failed")
    return f"FAIL {result.case_id}: {problem}"


def default_cases_dir(repo: Path) -> Path:
    return repo / CASES_SUBDIR


def run_bench(
    stage: str,
    tier: str,
    only: Sequence[str] | None,
    *,
    write_baseline: bool,
    data: Path | None = None,
    cases: Path | None = None,
    repo: Path | None = None,
    out: TextIO | None = None,
    verbose: bool = False,
) -> int:
    """Run the stage's cases for `tier` (`full` includes the fast cases) and print one line
    per case; exit 1 when any case fails, 2 when the cases or the data can't be read."""
    stream = out if out is not None else sys.stdout
    top = gitio.safe_toplevel(repo if repo is not None else Path.cwd())
    directory = cases if cases is not None else default_cases_dir(top)
    try:
        data_dir: Path | None = bench_data_dir(data)
    except FileNotFoundError as exc:
        if data is not None:
            print(f"revgate bench: {exc}", file=stream)
            return 2
        data_dir = None  # cases that read no data still run; the rest fail with the reason
    try:
        selected = load_cases(directory, stage, top)
    except CaseError as exc:
        print(f"revgate bench: {exc}", file=stream)
        return 2
    if tier == "fast":
        selected = [c for c in selected if c.tier == "fast"]
    if only:
        selected = [c for c in selected if c.id in set(only)]
    if not selected:
        print(f"revgate bench: no stage {stage} cases in {directory}", file=stream)
        return 2
    env = _Env(data_dir)
    cache = blob_cache(state_dir(top))
    results: list[CaseResult] = []
    for case in selected:
        result = _run_case_in(case, env, cache)
        results.append(result)
        print(result_line(result), file=stream)
        if verbose or not result.passed:
            for line in result.details:
                print(f"    {line}", file=stream)
        stream.flush()
    if write_baseline:
        path = directory.parent / "baselines" / f"stage-{stage}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "stage": stage,
            "tier": tier,
            "cases": [
                {"id": r.case_id, "passed": r.passed, "details": list(r.details)} for r in results
            ],
        }
        path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"baseline: {path}", file=stream)
    return 0 if all(r.passed for r in results) else 1
