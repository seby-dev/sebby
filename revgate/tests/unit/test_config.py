from __future__ import annotations

from pathlib import Path

import pytest
from conftest import MakeRepo

from revgate.config import (
    Config,
    ConfigError,
    GateCacheConfig,
    glob_match,
    is_prod_path,
    is_test_path,
    load_config,
    load_gate_cache_config,
)

# The shape of the pipeline spec's Appendix B file plus the algorithm spec's per-project
# block, with the project's own paths and libraries replaced by generic stand-ins (a
# public repository carries no private project's configuration). Appendix B's `[budget]`
# and the algorithm block's `[budget]` are merged into one table, and `report_glob` names
# the plan (amendment A8).
FULL_TOML = """
[budget]
rules_seconds = 60
gate_timeout_seconds = 600
wave_seconds = 300
behavior_seconds = 60          # T3's predicted-cost budget; twice this is the safety net

[gates.task]  # placeholders: see "Gate phase"; an empty expansion skips the command
commands = [
  "uv run ruff check {py_files}",
  "uv run ruff format --check {py_files}",
  "uv run mypy src scripts tests",
  "uv run bandit -ll {py_src_files}",
  "uv run pytest {run_tests}",
  "npx --prefix web tsc --noEmit -p web",        # when the diff touches web/
  "npm --prefix web run lint",                   # when the diff touches web/
  "npx --prefix web prettier --check {ts_files}",
  "npx --prefix web vitest run {ts_test_files} --maxWorkers=2",
]

[gates.wave]
commands = [
  "make pre-push",
  "npm --prefix web run lint",
  "npm --prefix web run format",
  "npx --prefix web tsc --noEmit -p web",
  "npm --prefix web test",
]

[e2e]
web_paths = ["web/"]
wave_command = ""
branch_command = ""

[gates.cache]
env_allowlist = ["UV_PYTHON"]
max_age = "24h"

[risk.paths]
"src/pkg/core.py" = "wave"
"src/pkg/checks.py" = "wave"
"src/pkg/family*.py" = "wave"
"web/src/review/port.ts" = "wave"

[routing]
max_lines = 400
branch_split_lines = 3000

[paths]
prod = ["src/**", "web/src/**"]
tests = ["tests/**", "web/src/**/*.test.ts", "web/src/**/*.test.tsx"]
history_docs = ["docs/project-log.md", "docs/superpowers/**", "docs/theory/**"]
normative_docs = ["CLAUDE.md", "docs/next-tasks.md", "docs/reference/**"]
prompts = ["src/pkg/vision_extract*.py"]      # never cleared

[plan]
report_glob = ".superpowers/sdd/{plan}/task-{id}-report.md"

[semantic]
library_hierarchies = ["notationlib"]
acknowledge_record_only = ["src/pkg/api/extract.py", "src/pkg/api/app.py",
                           "src/pkg/api/validation.py"]
bug_phrases = ["genuine bug", "should not happen", "internal error", "surface immediately"]
user_error_markers = ["422", "HTTPException", "ParseError"]
complementary_suffixes = [["start", "stop"], ["w", "h"], ["min", "max"]]
curated_distinct_subclasses = [
  "notationlib.expressions.A", "notationlib.articulations.B",
  "notationlib.articulations.C", "notationlib.articulations.D",
]

[lib.raise_kb]
"notationlib.pitch.Pitch" = ["notationlib.pitch.PitchException"]

[behave]
python = ".venv/bin/python"
env = { APP_KEY = "revgate-test-key" }
fork_reset = []

[rules]
"lib.subclass_unlisted" = { tier = "advisory" }

[policy]
protected = ["docs/frozen/**"]
"""


def write(tmp_path: Path, text: str) -> Path:
    (tmp_path / ".review.toml").write_text(text, encoding="utf-8")
    return tmp_path


def test_defaults_without_a_file(tmp_path: Path) -> None:
    cfg = load_config(tmp_path)
    assert cfg.is_default is True
    assert cfg.config_hash == ""
    assert cfg.gates_task == () and cfg.gates_wave == ()
    assert is_test_path("tests/test_x.py", cfg)
    assert is_test_path("web/src/a.test.ts", cfg)
    assert is_test_path("pkg/conftest.py", cfg)
    assert is_prod_path("pkg/mod.py", cfg)
    assert not is_prod_path("tests/test_x.py", cfg)
    assert is_prod_path("web/src/app.tsx", cfg)
    assert cfg.report_glob == ".superpowers/sdd/{plan}/task-{id}-report.md"
    assert cfg.normative_docs == ("CLAUDE.md", "README.md")
    assert cfg.gate_timeout_s == 600 and cfg.rules_seconds == 60
    assert cfg.wave_seconds == 300 and cfg.behavior_seconds == 60
    assert cfg.max_lines == 400 and cfg.branch_split_lines == 3000
    assert cfg.cache == GateCacheConfig()
    assert cfg.protected_extra == ()
    assert cfg.revgate_source_repo == "~/Developer/sebby"


def test_full_file_sets_every_field(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, FULL_TOML))
    assert isinstance(cfg, Config)
    assert cfg.is_default is False
    assert len(cfg.config_hash) == 64
    assert cfg.prod_globs == ("src/**", "web/src/**")
    assert cfg.test_globs == ("tests/**", "web/src/**/*.test.ts", "web/src/**/*.test.tsx")
    assert cfg.history_docs[-1] == "docs/theory/**"
    assert cfg.normative_docs == ("CLAUDE.md", "docs/next-tasks.md", "docs/reference/**")
    assert cfg.prompt_globs == ("src/pkg/vision_extract*.py",)
    assert cfg.report_glob is not None and "{plan}" in cfg.report_glob
    assert cfg.risk_high_context_prefix is None
    assert cfg.gates_task[0] == "uv run ruff check {py_files}"
    assert len(cfg.gates_task) == 9
    assert cfg.gates_wave[0] == "make pre-push"
    assert cfg.gate_timeout_s == 600
    assert cfg.rules_seconds == 60
    assert cfg.wave_seconds == 300
    assert cfg.behavior_seconds == 60
    assert cfg.e2e_web_paths == ("web/",)
    assert cfg.e2e_wave_command == "" and cfg.e2e_branch_command == ""
    assert cfg.cache.env_allowlist == ("UV_PYTHON",)
    assert cfg.cache.max_age_s == 86_400
    assert cfg.risk_paths["src/pkg/core.py"] == "wave"
    assert cfg.risk_paths["src/pkg/family*.py"] == "wave"
    assert len(cfg.risk_paths) == 4
    assert cfg.max_lines == 400
    assert cfg.branch_split_lines == 3000
    assert cfg.library_hierarchies == ("notationlib",)
    assert len(cfg.curated_distinct_subclasses) == 4
    assert cfg.bug_phrases[0] == "genuine bug"
    assert len(cfg.acknowledge_record_only) == 3
    assert cfg.rule_overrides["lib.subclass_unlisted"]["tier"] == "advisory"
    assert cfg.protected_extra == ("docs/frozen/**",)
    assert cfg.revgate_source_repo == "~/Developer/sebby"
    assert is_test_path("web/src/review/a.test.ts", cfg)
    assert not is_prod_path("web/src/review/a.test.ts", cfg)
    assert is_prod_path("src/pkg/core.py", cfg)
    assert not is_prod_path("scripts/tool.py", cfg)


def test_optional_keys(tmp_path: Path) -> None:
    text = (
        '[plan]\nreport_glob = "r/{plan}/{id}.md"\nrisk_high_context_prefix = "ctx:"\n'
        '[policy]\nrevgate_source_repo = "/src/revgate"\n'
    )
    cfg = load_config(write(tmp_path, text))
    assert cfg.risk_high_context_prefix == "ctx:"
    assert cfg.revgate_source_repo == "/src/revgate"
    assert cfg.prod_globs == ("**/*.py", "web/src/**")  # missing sections keep defaults


def test_gate_cache_loader_still_agrees(tmp_path: Path) -> None:
    write(tmp_path, FULL_TOML)
    assert load_config(tmp_path).cache == load_gate_cache_config(tmp_path)


def test_load_config_reads_the_file_at_a_revision(make_repo: MakeRepo) -> None:
    repo = make_repo(
        {"a.py": "x = 1\n"},
        {".review.toml": "[routing]\nmax_lines = 7\n"},
        review_toml="[routing]\nmax_lines = 5\n",
    )
    (repo.path / ".review.toml").write_text("[routing]\nmax_lines = 9\n")
    assert load_config(repo.path, rev=repo.base).max_lines == 5
    assert load_config(repo.path, rev=repo.head).max_lines == 7
    assert load_config(repo.path).max_lines == 9


def test_load_config_at_a_revision_without_the_file_is_default(make_repo: MakeRepo) -> None:
    repo = make_repo({"a.py": "x = 1\n"}, {})
    assert load_config(repo.path, rev=repo.base).is_default


def test_glob_match() -> None:
    assert glob_match("src/pkg/family_assign.py", ["src/pkg/family*.py"])
    assert glob_match("web/src/review/a/b.ts", ["web/src/**"])
    assert not glob_match("web/src2/x.ts", ["web/src/**"])
    assert glob_match("a.py", ["**/*.py"])
    assert glob_match("x/y/test_a.py", ["**/test_*.py"])
    assert not glob_match("src/pkg/sub/family.py", ["src/pkg/family*.py"])
    assert glob_match("a1.py", ["a?.py"]) and not glob_match("a/.py", ["a?.py"])
    assert glob_match("docs/a+b.md", ["docs/a+b.md"])
    assert not glob_match("anything", [])


@pytest.mark.parametrize(
    "text",
    [
        "[routing\nmax_lines = 1\n",
        "[routing]\nmax_lines = 'many'\n",
        "[paths]\nprod = 'src/**'\n",
        "[paths]\nprod = [1, 2]\n",
        '[plan]\nreport_glob = "task-{id}-report.md"\n',
        '[gates.cache]\nmax_age = "soon"\n',
        '[rules]\n"a.b" = "blocking"\n',
        '[risk.paths]\n"a.py" = 3\n',
    ],
)
def test_malformed_file_raises(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))
