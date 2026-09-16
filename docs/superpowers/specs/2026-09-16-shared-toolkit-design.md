# Shared toolkit design

## Problem

Sebby maintains five active Python projects (`organist_bot`, `pmp-project`,
`staff2solfa`, `varrick-chorus-uk`, and this repo). An audit of all four
code-bearing projects found the same problems solved independently multiple
times — most notably three separate multi-provider LLM clients, each with its
own retry, failover, and (in two cases) prompt-caching logic. This spec
defines `sebby`: a shared Python package and Claude Code plugin that centralize
these patterns so future projects, and eventually the existing ones, share one
implementation instead of reinventing it.

## Audit findings

Duplication found across `organist_bot`, `pmp-project`, `staff2solfa`, and
`varrick-chorus-uk`:

- **Multi-provider LLM clients**: `organist_bot` (`integrations/unified_agent.py`),
  `pmp-project` (`pmp_bots/ai_providers/`), and `staff2solfa`
  (`vision_extract.py`'s provider/model/effort registry) each independently
  wrap Anthropic/OpenAI/Gemini behind a failover cascade.
- **Prompt caching**: `organist_bot` applies a single-shot cache breakpoint
  on the system prompt and last tool. `pmp-project` (in an unmerged worktree,
  `pmp_bots/ai_agent.py`) has a more correct two-tier implementation — it
  marks only the last message per turn, builds a shallow copy rather than
  mutating stored history, and respects Anthropic's four-breakpoint-per-request
  cap. `staff2solfa` and `varrick-chorus-uk` have no prompt caching at all.
- **Retry/backoff**: reimplemented independently in `organist_bot`
  (`scraper.py`), `staff2solfa` (`vision_extract.py::_call_with_retry`),
  `pmp-project` (the AI client's timeout-budget cascade), and
  `varrick-chorus-uk` (`geocoding.py::_query_batch`).
- **FastAPI helpers**: `staff2solfa` has shared-secret header auth, an
  upload-size cap (middleware plus streaming-read cap), safe CORS regex
  setup, and a bounded async job-polling store — all generic, currently used
  in one project only.
- **Logging setup**: `organist_bot` (`logging_config.py`) and `pmp-project`
  (`logging_setup.py`) both build structlog-to-JSON logging with a
  secret-scrubbing hook and optional Sentry init.
- **Config loading**: `varrick-chorus-uk`, `pmp-project`, and `staff2solfa`
  all use a `pydantic-settings` class with an `APP_ENV`-driven dotenv file
  and a singleton accessor — same shape, independently written.
- **Lint/type config**: ruff, mypy, and bandit config blocks in
  `pyproject.toml` are near-identical across all four projects.
- **`.claude/settings.json` deny-list**: byte-for-byte identical between
  `staff2solfa` and `varrick-chorus-uk`; `pmp-project` has the same list plus
  extras.
- **CLAUDE.md prose**: `staff2solfa` and `varrick-chorus-uk` have
  byte-identical "Git conventions" and near-identical "Quality standards"
  sections. `pmp-project` and `staff2solfa` both describe the same
  "advisor review" pattern (a Fable subagent reviewing specs/plans before
  risky work) in different words. All three restate the global
  `~/.claude/CLAUDE.md` "Full Feature Workflow" pipeline instead of
  referencing it.
- **Generic `.claude/hooks/`**: `organist_bot` has nine hooks; about seven
  are domain-independent (env-file git blocking, transcript secret
  scrubbing, post-edit lint, stop-time quality check, post-PR-created
  nudge, post-merge docs nudge, a PR-reviewer-sentinel mechanism) but
  hardcode this project's paths. No other project has equivalent hooks yet.

## Scope

### In scope

**`sebby` Python package** (importable as `sebby`, submodules independently
importable so a project pulls in only what it needs):

- `sebby.llm` — multi-provider client (Anthropic primary, OpenAI and Gemini
  failover) presenting an Anthropic-messages-shaped interface. Built-in
  prompt caching using pmp-project's safer technique: mark only the last
  message and the last tool schema per turn, build a shallow copy rather
  than mutate stored history, and enforce the four-breakpoint cap. Usage and
  cache-hit telemetry go through a pluggable callback rather than a
  hardcoded store.
- `sebby.retry` — a retry/backoff decorator and a "retry once on 5xx,
  fail fast on other 4xx" HTTP helper, replacing the four independent
  reimplementations found in the audit.
- `sebby.http` — FastAPI helpers extracted from `staff2solfa`: shared-secret
  header auth using a constant-time comparison, an upload-size-capping
  middleware plus streaming-read cap, a safe localhost-only CORS regex
  setup, a threadpool file-write helper, and a bounded async job-polling
  store for long-running request patterns.
- `sebby.logging` — structlog-to-JSON console and file logging setup with a
  pluggable secret-scrub regex and optional Sentry initialization.
- `sebby.config` — a `pydantic-settings` base class with `APP_ENV`-driven
  dotenv file selection and a singleton accessor helper.
- `sebby.storage` — an atomic, file-locked JSON/text store (from
  `organist_bot`'s `atomic_store.py`).
- `sebby.cache` — a small monotonic-clock TTL cache.
- `sebby.notify` — Telegram alert sending plus Markdown-V2 escaping.
- `sebby.cli` — a small helper enforcing the convention found in
  `varrick-chorus-uk` and `staff2solfa`: each stage raises a
  domain-specific exception, `main()` catches it, prints to stderr, and
  returns a nonzero exit code.

**Shared config artifacts** (files projects point at, not Python code):

- A shared ruff and mypy configuration fragment.
- A reusable GitHub Actions workflow (`python-ci.yml`) covering lint, type
  check, and test, callable from a consuming project's own workflow file
  with `uses: seby-dev/sebby/.github/workflows/python-ci.yml@main`.

**Claude Code plugin** (installable in consuming projects, distributed via
a `marketplace.json` in this repo):

- Parameterized versions of `organist_bot`'s generic hooks: env-file git
  blocking, transcript secret scrubbing, post-edit lint, stop-time quality
  check, post-PR-created nudge, post-merge docs nudge, and a
  worktree-redirect-on-`SessionStart` hook generalized from `pmp-project`.
  Each takes its project-specific values (paths, target modules) from
  project settings rather than being hardcoded.
- A generalized version of `organist_bot`'s `feature` skill (the
  autopilot → simplify → code-review → security-review → verify quality
  gate).
- A `settings.json` deny-list template matching the baseline already shared
  by `staff2solfa`, `varrick-chorus-uk`, and `pmp-project`.
- A CLAUDE.md snippet document covering the conventions already duplicated
  verbatim: git conventions, quality standards (test-driven development,
  function and file size limits, docstring philosophy), and the advisor
  review pattern.

**CLAUDE.md de-duplication**: once the plugin's snippet document exists,
trim `pmp-project`, `staff2solfa`, and `varrick-chorus-uk`'s CLAUDE.md files
to reference it and the global `~/.claude/CLAUDE.md` Full Feature Workflow
instead of restating either.

### Out of scope (backlog for a later phase)

These came up in the audit but have thinner evidence — one project only, or
a pattern worth documenting rather than extracting as code:

- `sebby.health` — a minimal threaded HTTP health-check server
  (`pmp-project` only).
- `sebby.email` — SMTP and MIME attachment-sending mechanics
  (`organist_bot` only).
- A `.claude/launch.json` template shape (`staff2solfa` only).
- Documenting the "pre-PR cross-file invariants reviewer" agent pattern
  (`organist_bot`'s `pipeline-impact-reviewer`) as a template, not shipping
  its actual content.
- Deployment templates (launchd plus supervisord) — a real pattern in
  `pmp-project`, but heavily hardcoded to specific paths and bot names.
  Revisit once a third project needs the same kind of deployment.

## Consumption model

- **Python package**: each project adds it as a git dependency —
  `uv add git+https://github.com/seby-dev/sebby`, pinned to a tag or commit,
  bumped deliberately.
- **Claude Code plugin**: installed into a project like any other Claude
  Code plugin, versioned and updated independently of manual copying.

## Rollout order

1. Scaffold the `sebby` repo (this spec, the implementation plan, package
   layout, CI) and build `sebby.llm` and `sebby.retry` first, since prompt
   caching was the original pain point driving this project.
2. Build out the remaining package modules (`http`, `logging`, `config`,
   `storage`, `cache`, `notify`, `cli`) and the shared lint/CI config.
3. Build the Claude Code plugin.
4. Pilot-migrate `organist_bot` — replace its LLM integration and generic
   hooks with the shared package and plugin — as the first real-world
   validation.
5. Roll out to the remaining projects incrementally, and do the CLAUDE.md
   trims once the plugin's snippet document is in place.

## Testing

- Each `sebby` module gets unit tests (pytest); tests mock provider SDKs and
  make no real network calls.
- The plugin gets validated with the plugin-dev plugin-validator before each
  release.
- The pilot migration of `organist_bot` is the end-to-end validation for the
  package: if it can fully replace `organist_bot`'s existing LLM
  integration and hooks without behavior regressions, the design holds.
