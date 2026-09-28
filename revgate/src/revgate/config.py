"""`.review.toml` loading: the gate cache's section and the full per-project `Config`."""

from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import cast

from revgate.gitio import show

_AGE_RE = re.compile(r"^(\d+)([smhd])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

REVIEW_TOML = ".review.toml"
DEFAULT_REPORT_GLOB = ".superpowers/sdd/{plan}/task-{id}-report.md"


class ConfigError(Exception):
    """.review.toml is malformed."""


@dataclass(frozen=True)
class GateCacheConfig:
    env_allowlist: tuple[str, ...] = ()
    max_age_s: int = 86_400


def parse_age(text: str) -> int:
    match = _AGE_RE.match(text.strip())
    if match is None:
        raise ConfigError(f"bad age {text!r}; use a number followed by s, m, h, or d")
    return int(match.group(1)) * _UNITS[match.group(2)]


def _cache_from_data(data: Mapping[str, object]) -> GateCacheConfig:
    gates = data.get("gates", {})
    if not isinstance(gates, dict):
        raise ConfigError("[gates] must be a table")
    section = gates.get("cache", {})
    if not isinstance(section, dict):
        raise ConfigError("[gates.cache] must be a table")
    allow = tuple(str(v) for v in section.get("env_allowlist", ()))
    max_age = parse_age(str(section["max_age"])) if "max_age" in section else 86_400
    return GateCacheConfig(env_allowlist=allow, max_age_s=max_age)


def _parse_toml(text: str, where: str) -> dict[str, object]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{where}: {exc}") from exc


def load_gate_cache_config(top: Path) -> GateCacheConfig:
    path = top / REVIEW_TOML
    if not path.exists():
        return GateCacheConfig()
    return _cache_from_data(_parse_toml(path.read_text(encoding="utf-8"), str(path)))


@dataclass(frozen=True)
class Config:
    is_default: bool
    config_hash: str  # sha256 of the file's bytes; "" when default
    prod_globs: tuple[str, ...]  # [paths].prod
    test_globs: tuple[str, ...]  # [paths].tests
    history_docs: tuple[str, ...]  # [paths].history_docs
    normative_docs: tuple[str, ...]  # [paths].normative_docs
    prompt_globs: tuple[str, ...]  # [paths].prompts
    report_glob: str | None  # [plan].report_glob; must contain {plan} (A8)
    risk_high_context_prefix: str | None  # [plan].risk_high_context_prefix
    gates_task: tuple[str, ...]  # [gates.task].commands
    gates_wave: tuple[str, ...]  # [gates.wave].commands
    gate_timeout_s: int  # [budget].gate_timeout_seconds, default 600
    rules_seconds: int  # [budget].rules_seconds, default 60
    wave_seconds: int  # [budget].wave_seconds, default 300
    behavior_seconds: int  # [budget].behavior_seconds, default 60 (A2)
    e2e_web_paths: tuple[str, ...]  # [e2e].web_paths
    e2e_wave_command: str  # [e2e].wave_command
    e2e_branch_command: str  # [e2e].branch_command
    cache: GateCacheConfig  # [gates.cache], with load_gate_cache_config's rules
    risk_paths: Mapping[str, str]  # [risk.paths] (path or glob to "wave")
    max_lines: int  # [routing].max_lines, default 400
    branch_split_lines: int  # [routing].branch_split_lines, default 3000
    library_hierarchies: tuple[str, ...]  # [semantic].library_hierarchies
    curated_distinct_subclasses: tuple[str, ...]  # [semantic].curated_distinct_subclasses
    bug_phrases: tuple[str, ...]  # [semantic].bug_phrases
    acknowledge_record_only: tuple[str, ...]  # [semantic].acknowledge_record_only
    rule_overrides: Mapping[str, Mapping[str, str]]  # [rules]
    protected_extra: tuple[str, ...]  # [policy].protected, default ()
    revgate_source_repo: str  # [policy].revgate_source_repo, default "~/Developer/sebby"


DEFAULT_PROD_GLOBS = ("**/*.py", "web/src/**")
DEFAULT_TEST_GLOBS = (
    "tests/**",
    "test/**",
    "**/*.test.ts",
    "**/*.test.tsx",
    "**/test_*.py",
    "**/conftest.py",
)
DEFAULT_HISTORY_DOCS = ("docs/project-log.md", "docs/superpowers/**")
DEFAULT_NORMATIVE_DOCS = ("CLAUDE.md", "README.md")
DEFAULT_REVGATE_SOURCE_REPO = "~/Developer/sebby"


class _Reader:
    """Typed access to the parsed TOML; every type mismatch is a ConfigError."""

    def __init__(self, data: Mapping[str, object]) -> None:
        self.data = data

    def table(self, dotted: str) -> Mapping[str, object]:
        node: object = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict):
                raise ConfigError(f"[{dotted}] must be a table")
            node = node.get(part, {})
        if not isinstance(node, dict):
            raise ConfigError(f"[{dotted}] must be a table")
        return cast(dict[str, object], node)

    def strs(self, dotted: str, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        section = self.table(dotted)
        if key not in section:
            return default
        value = section[key]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"[{dotted}].{key} must be a list of strings")
        return tuple(cast(list[str], value))

    def string(self, dotted: str, key: str, default: str) -> str:
        value = self.opt_string(dotted, key)
        return default if value is None else value

    def opt_string(self, dotted: str, key: str) -> str | None:
        section = self.table(dotted)
        if key not in section:
            return None
        value = section[key]
        if not isinstance(value, str):
            raise ConfigError(f"[{dotted}].{key} must be a string")
        return value

    def integer(self, dotted: str, key: str, default: int) -> int:
        section = self.table(dotted)
        if key not in section:
            return default
        value = section[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(f"[{dotted}].{key} must be a non-negative integer")
        return value

    def str_map(self, dotted: str) -> Mapping[str, str]:
        section = self.table(dotted)
        for key, value in section.items():
            if not isinstance(value, str):
                raise ConfigError(f"[{dotted}] {key!r} must map to a string")
        return MappingProxyType({k: cast(str, v) for k, v in section.items()})

    def rule_overrides(self) -> Mapping[str, Mapping[str, str]]:
        out: dict[str, Mapping[str, str]] = {}
        for rule_id, value in self.table("rules").items():
            if not isinstance(value, dict):
                raise ConfigError(f"[rules] {rule_id!r} must be a table, such as {{ tier = ... }}")
            out[rule_id] = MappingProxyType({str(k): str(v) for k, v in value.items()})
        return MappingProxyType(out)


def _config_from_data(data: Mapping[str, object], config_hash: str, is_default: bool) -> Config:
    r = _Reader(data)
    report_glob = r.string("plan", "report_glob", DEFAULT_REPORT_GLOB)
    if "{plan}" not in report_glob:
        raise ConfigError(
            f"[plan].report_glob {report_glob!r} must contain {{plan}} (amendment A8)"
        )
    return Config(
        is_default=is_default,
        config_hash=config_hash,
        prod_globs=r.strs("paths", "prod", DEFAULT_PROD_GLOBS),
        test_globs=r.strs("paths", "tests", DEFAULT_TEST_GLOBS),
        history_docs=r.strs("paths", "history_docs", DEFAULT_HISTORY_DOCS),
        normative_docs=r.strs("paths", "normative_docs", DEFAULT_NORMATIVE_DOCS),
        prompt_globs=r.strs("paths", "prompts", ()),
        report_glob=report_glob,
        risk_high_context_prefix=r.opt_string("plan", "risk_high_context_prefix"),
        gates_task=r.strs("gates.task", "commands", ()),
        gates_wave=r.strs("gates.wave", "commands", ()),
        gate_timeout_s=r.integer("budget", "gate_timeout_seconds", 600),
        rules_seconds=r.integer("budget", "rules_seconds", 60),
        wave_seconds=r.integer("budget", "wave_seconds", 300),
        behavior_seconds=r.integer("budget", "behavior_seconds", 60),
        e2e_web_paths=r.strs("e2e", "web_paths", ()),
        e2e_wave_command=r.string("e2e", "wave_command", ""),
        e2e_branch_command=r.string("e2e", "branch_command", ""),
        cache=_cache_from_data(data),
        risk_paths=r.str_map("risk.paths"),
        max_lines=r.integer("routing", "max_lines", 400),
        branch_split_lines=r.integer("routing", "branch_split_lines", 3000),
        library_hierarchies=r.strs("semantic", "library_hierarchies", ()),
        curated_distinct_subclasses=r.strs("semantic", "curated_distinct_subclasses", ()),
        bug_phrases=r.strs("semantic", "bug_phrases", ()),
        acknowledge_record_only=r.strs("semantic", "acknowledge_record_only", ()),
        rule_overrides=r.rule_overrides(),
        protected_extra=r.strs("policy", "protected", ()),
        revgate_source_repo=r.string("policy", "revgate_source_repo", DEFAULT_REVGATE_SOURCE_REPO),
    )


def parse_config(raw: bytes | None, where: str = REVIEW_TOML) -> Config:
    """A `Config` from a file's bytes, or the defaults when there's no file."""
    if raw is None:
        return _config_from_data({}, "", True)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{where}: not UTF-8") from exc
    data = _parse_toml(text, where)
    return _config_from_data(data, hashlib.sha256(raw).hexdigest(), False)


def load_config(repo: Path, rev: str | None = None) -> Config:
    """Read `.review.toml` at `rev` when given (never the working tree), else the working tree.

    Unknown sections and keys are ignored, so a later stage's sections never fail to load.
    """
    if rev is not None:
        return parse_config(show(repo, rev, REVIEW_TOML), f"{rev}:{REVIEW_TOML}")
    path = repo / REVIEW_TOML
    return parse_config(path.read_bytes() if path.is_file() else None, str(path))


@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def glob_match(path: str, patterns: Iterable[str]) -> bool:
    """Whole-path match: `**/` is any directory prefix, `**` is anything, and `*` and `?`
    stay inside one path component."""
    return any(_glob_regex(p).fullmatch(path) for p in patterns)


def is_test_path(path: str, cfg: Config) -> bool:
    return glob_match(path, cfg.test_globs)


def is_prod_path(path: str, cfg: Config) -> bool:
    return glob_match(path, cfg.prod_globs) and not is_test_path(path, cfg)
