# sebby

Shared Python utilities: a multi-provider LLM client with built-in Anthropic
prompt caching, plus retry helpers. Used across sebby's projects instead of
reimplementing the same patterns in each one.

## Install

    uv add git+https://github.com/seby-dev/sebby --tag v0.1.0

Pin to a tag or commit; bump deliberately.

## `sebby.retry`

    from sebby.retry import retry_with_backoff

    result = retry_with_backoff(
        lambda: call_something_flaky(),
        max_attempts=3,
        retryable_exceptions=(ConnectionError,),
    )

## `sebby.llm`

    from sebby.llm import LLMClient, ProviderConfig

    client = LLMClient(
        providers=[
            ProviderConfig(provider="anthropic", model="claude-sonnet-5"),
            ProviderConfig(provider="openai", model="gpt-5.1"),
        ],
        on_usage=lambda record: print(record),
    )

    response = client.complete(
        system="You are a helpful assistant.",
        messages=[{"role": "user", "content": "Hello"}],
    )

`LLMClient` tries providers in order, retrying each with backoff before
failing over to the next. For the `anthropic` provider, it automatically
marks a prompt-cache breakpoint on the last message and the last tool
schema, respecting Anthropic's four-breakpoint-per-request cap.

## Modules

| Module | Purpose |
|---|---|
| `sebby.retry` | Generic retry/backoff and retry-once-on-server-error helpers |
| `sebby.llm` | Multi-provider LLM client with Anthropic prompt caching |
| `sebby.storage` | Atomic, file-locked JSON store (no cross-process write exclusion — see docstring) |
| `sebby.cache` | Trivial in-memory TTL cache |
| `sebby.cli` | `run_main` — catch-print-exit-code convention for CLI entry points |
| `sebby.config` | Environment-driven settings base class (`Settings`) and a singleton accessor (`get_settings`) |
| `sebby.logging` | structlog-to-stdlib logging setup with secret scrubbing and optional Sentry |
| `sebby.http` | FastAPI helpers: shared-secret auth, localhost CORS, upload-size limits, threadpool file writes, bounded async job store (`JobStore.create()` is thread-safe via an internal lock, but not process-shared; register `add_content_length_limit` before `add_localhost_cors` so a 413 response still carries CORS headers) |
| `sebby.notify` | Telegram alert sending with MarkdownV2 escaping (no extra needed — the HTTP client is injected by the caller) |

**Note:** `sebby.config`'s `Settings` deliberately does NOT read ambient OS
environment variables (only `APP_ENV` itself, and only to pick a dotenv
file) — even a field with a default silently ignores a same-named env var.
If you need standard env-var precedence, override
`settings_customise_sources` in your subclass to include `env_settings`.

## Optional extras

Some modules need extra dependencies, installed via `uv add 'sebby[extra-name]'` (or add multiple: `uv add 'sebby[llm,config,logging]'`):

| Extra | Needed for |
|---|---|
| `llm` | `sebby.llm` |
| `config` | `sebby.config` |
| `logging` | `sebby.logging` |
| `http` | `sebby.http` |

`sebby.storage`, `sebby.cache`, `sebby.cli`, `sebby.retry`, and `sebby.notify` are stdlib-only and need no extra.

## Shared config

- `configs/ruff.toml` — the ruff config this repo uses, usable standalone via `ruff check --config path/to/sebby/configs/ruff.toml .` in another project, or copy the `[lint] select = [...]` block into that project's own `pyproject.toml`.
- `configs/mypy.ini` — likewise, via `mypy --config-file path/to/sebby/configs/mypy.ini src`, or copy the `[mypy]` block into that project's own config.
- `.github/workflows/ci.yml` is a reusable workflow other repos can call directly:

      jobs:
        ci:
          uses: seby-dev/sebby/.github/workflows/ci.yml@main
          with:
            mypy-target: your_package_dir
            # Defaults to "--extra dev". Override for a repo whose
            # pyproject.toml doesn't declare a "dev" extra — for example a
            # repo using PEP 735 dependency groups instead:
            # sync-args: "--group dev"
