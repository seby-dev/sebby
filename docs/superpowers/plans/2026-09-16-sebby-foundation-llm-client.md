# Sebby Foundation & LLM Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scaffold the `sebby` Python package and implement `sebby.retry` and `sebby.llm` (a multi-provider LLM client with built-in Anthropic prompt caching), producing an installable, tested package other projects can depend on via git.

**Architecture:** A standard `src/`-layout Python package built with hatchling and managed with `uv`. `sebby.retry` is a small, dependency-free module providing generic retry/backoff helpers. `sebby.llm` builds on it: `providers.py` holds caller-supplied provider/model configuration (no hardcoded model IDs, since those go stale), `cache.py` is a pure, network-free module implementing the prompt-cache breakpoint logic, and `client.py` ties them together into `LLMClient`, which calls an injected `completion_fn` (defaulting to `litellm.completion`) so it's fully testable without real API calls.

**Tech Stack:** Python 3.11+, `uv`, `litellm` (default completion backend), `pytest`, `ruff`, `mypy` (strict).

**Spec:** `docs/superpowers/specs/2026-09-16-shared-toolkit-design.md`

**Note on scope:** This is plan 1 of several. It covers only the "scaffold the repo, build `sebby.llm` and `sebby.retry`" step of the spec's rollout order. The remaining package modules (`http`, `logging`, `config`, `storage`, `cache`, `notify`, `cli`), the Claude Code plugin, the `organist_bot` pilot migration, and the CLAUDE.md de-duplication each get their own plan once this one ships.

## Global Constraints

- Package is importable as `sebby`; every submodule (`sebby.retry`, `sebby.llm`, ...) must also be independently importable.
- `sebby.llm` presents an Anthropic-messages-shaped interface (separate `system` argument, not folded into the message list at the public API level) and supports Anthropic as primary with OpenAI/Gemini as failover.
- Prompt caching marks only the last message and the last tool schema per turn, builds a shallow copy rather than mutating stored history, and enforces Anthropic's cap of 4 `cache_control` breakpoints per request.
- Usage/cache telemetry is delivered through a pluggable callback, never a hardcoded store.
- `sebby.retry` provides a generic retry/backoff helper and a "retry once on server error" helper.
- All tests use pytest, mock provider SDKs, and make no real network calls.
- Consumption model: projects add this package with `uv add git+https://github.com/seby-dev/sebby`, pinned to a tag or commit.

---

### Task 1: Repo scaffolding and quality gates

**Files:**
- Create: `pyproject.toml`
- Create: `src/sebby/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/test_package.py`
- Create: `.gitignore`
- Create: `Makefile`
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: `sebby.__version__: str`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "sebby"
version = "0.1.0"
description = "Shared Python utilities across sebby's projects: multi-provider LLM client with prompt caching, retry helpers, and more."
requires-python = ">=3.11"
dependencies = [
    "litellm>=1.50.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "ruff>=0.6.0",
    "mypy>=1.11.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/sebby"]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "W", "I", "UP", "B"]

[tool.mypy]
python_version = "3.11"
strict = true

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: Write `src/sebby/__init__.py` without a version yet**

```python
"""Shared Python utilities: LLM client with prompt caching, retry helpers, and more."""
```

- [ ] **Step 3: Write `tests/__init__.py` (empty) and the failing test**

`tests/__init__.py`: empty file.

`tests/test_package.py`:

```python
import sebby


def test_version_is_exported() -> None:
    assert sebby.__version__ == "0.1.0"
```

- [ ] **Step 4: Sync the environment and run the test to verify it fails**

Run: `cd /Users/sebby/Developer/sebby && uv sync --extra dev && uv run pytest tests/test_package.py -v`
Expected: FAIL with `AttributeError: module 'sebby' has no attribute '__version__'`

- [ ] **Step 5: Add the version to make the test pass**

Update `src/sebby/__init__.py`:

```python
"""Shared Python utilities: LLM client with prompt caching, retry helpers, and more."""

__version__ = "0.1.0"
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/test_package.py -v`
Expected: PASS

- [ ] **Step 7: Run lint and type checks, fix any findings**

Run: `cd /Users/sebby/Developer/sebby && uv run ruff check . && uv run mypy src`
Expected: both clean (no findings)

- [ ] **Step 8: Write `.gitignore`**

```
__pycache__/
*.pyc
.venv/
.mypy_cache/
.ruff_cache/
.pytest_cache/
dist/
*.egg-info/
```

- [ ] **Step 9: Write `Makefile`**

```makefile
.PHONY: pre-push
pre-push:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src
	uv run pytest
```

- [ ] **Step 10: Write `.github/workflows/ci.yml`**

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync --extra dev
      - run: uv run ruff check .
      - run: uv run mypy src
      - run: uv run pytest
```

- [ ] **Step 11: Commit**

```bash
git -C /Users/sebby/Developer/sebby add pyproject.toml src/sebby/__init__.py tests/__init__.py tests/test_package.py .gitignore Makefile .github/workflows/ci.yml
git -C /Users/sebby/Developer/sebby commit -m "chore: scaffold sebby package with quality gates"
```

---

### Task 2: `sebby.retry`

**Files:**
- Create: `src/sebby/retry.py`
- Create: `tests/test_retry.py`

**Interfaces:**
- Consumes: nothing (stdlib only)
- Produces: `RetryExhaustedError`, `retry_with_backoff(fn, *, max_attempts=3, base_delay=1.0, retryable_exceptions=(Exception,), sleep=time.sleep, on_attempt=None) -> T`, `retry_once_on_server_error(fn, *, is_server_error, sleep=time.sleep, delay=1.0) -> T`

- [ ] **Step 1: Write the failing tests**

`tests/test_retry.py`:

```python
from __future__ import annotations

import pytest

from sebby.retry import RetryExhaustedError, retry_once_on_server_error, retry_with_backoff


def test_retry_with_backoff_returns_on_first_success() -> None:
    calls = []

    def fn() -> str:
        calls.append(1)
        return "ok"

    result = retry_with_backoff(fn, max_attempts=3, base_delay=0.0, sleep=lambda _: None)

    assert result == "ok"
    assert len(calls) == 1


def test_retry_with_backoff_retries_then_succeeds() -> None:
    attempts = {"n": 0}

    def fn() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ValueError("transient")
        return "ok"

    result = retry_with_backoff(
        fn,
        max_attempts=3,
        base_delay=0.0,
        retryable_exceptions=(ValueError,),
        sleep=lambda _: None,
    )

    assert result == "ok"
    assert attempts["n"] == 3


def test_retry_with_backoff_raises_retry_exhausted_after_max_attempts() -> None:
    def fn() -> str:
        raise ValueError("always fails")

    with pytest.raises(RetryExhaustedError) as exc_info:
        retry_with_backoff(
            fn,
            max_attempts=3,
            base_delay=0.0,
            retryable_exceptions=(ValueError,),
            sleep=lambda _: None,
        )

    assert isinstance(exc_info.value.__cause__, ValueError)


def test_retry_with_backoff_does_not_retry_non_retryable_exception() -> None:
    calls = []

    def fn() -> str:
        calls.append(1)
        raise KeyError("not retryable")

    with pytest.raises(KeyError):
        retry_with_backoff(
            fn,
            max_attempts=3,
            base_delay=0.0,
            retryable_exceptions=(ValueError,),
            sleep=lambda _: None,
        )

    assert len(calls) == 1


def test_retry_with_backoff_calls_on_attempt_callback() -> None:
    seen: list[int] = []

    def fn() -> str:
        if len(seen) < 2:
            raise ValueError("transient")
        return "ok"

    retry_with_backoff(
        fn,
        max_attempts=3,
        base_delay=0.0,
        retryable_exceptions=(ValueError,),
        sleep=lambda _: None,
        on_attempt=lambda n: seen.append(n),
    )

    assert seen == [1, 2, 3]


def test_retry_once_on_server_error_retries_once_then_succeeds() -> None:
    attempts = {"n": 0}

    class ServerError(Exception):
        pass

    def fn() -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ServerError("500")
        return "ok"

    result = retry_once_on_server_error(
        fn,
        is_server_error=lambda exc: isinstance(exc, ServerError),
        sleep=lambda _: None,
    )

    assert result == "ok"
    assert attempts["n"] == 2


def test_retry_once_on_server_error_does_not_retry_client_error() -> None:
    calls = []

    class ClientError(Exception):
        pass

    def fn() -> str:
        calls.append(1)
        raise ClientError("400")

    with pytest.raises(ClientError):
        retry_once_on_server_error(
            fn,
            is_server_error=lambda _exc: False,
            sleep=lambda _: None,
        )

    assert len(calls) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/test_retry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sebby.retry'`

- [ ] **Step 3: Implement `src/sebby/retry.py`**

```python
from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")


class RetryExhaustedError(Exception):
    """Raised when all retry attempts of `retry_with_backoff` are exhausted."""


def retry_with_backoff(
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
    sleep: Callable[[float], None] = time.sleep,
    on_attempt: Callable[[int], None] | None = None,
) -> T:
    """Call `fn` up to `max_attempts` times with exponential backoff.

    Only exceptions matching `retryable_exceptions` are retried; anything
    else propagates immediately. Raises `RetryExhaustedError` (chained to
    the last exception) if every attempt fails.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        if on_attempt is not None:
            on_attempt(attempt)
        try:
            return fn()
        except retryable_exceptions as exc:
            last_exc = exc
            if attempt == max_attempts:
                break
            sleep(base_delay * (2 ** (attempt - 1)))
    raise RetryExhaustedError(f"failed after {max_attempts} attempts") from last_exc


def retry_once_on_server_error(
    fn: Callable[[], T],
    *,
    is_server_error: Callable[[Exception], bool],
    sleep: Callable[[float], None] = time.sleep,
    delay: float = 1.0,
) -> T:
    """Call `fn`; on an exception where `is_server_error(exc)` is true, wait
    `delay` seconds and try exactly once more. Any other exception, or a
    second failure, propagates immediately.
    """
    try:
        return fn()
    except Exception as exc:
        if not is_server_error(exc):
            raise
        sleep(delay)
        return fn()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/test_retry.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Run lint and type checks**

Run: `cd /Users/sebby/Developer/sebby && uv run ruff check . && uv run mypy src`
Expected: both clean

- [ ] **Step 6: Commit**

```bash
git -C /Users/sebby/Developer/sebby add src/sebby/retry.py tests/test_retry.py
git -C /Users/sebby/Developer/sebby commit -m "feat: add retry_with_backoff and retry_once_on_server_error"
```

---

### Task 3: `sebby.llm.providers`

**Files:**
- Create: `src/sebby/llm/__init__.py`
- Create: `src/sebby/llm/providers.py`
- Create: `tests/llm/__init__.py`
- Create: `tests/llm/test_providers.py`

**Interfaces:**
- Consumes: nothing
- Produces: `UnknownProviderError`, `UnknownEffortError`, `PROVIDER_API_KEY_ENV: dict[str, str]`, `VALID_EFFORT_LEVELS: tuple[str, ...]`, `ProviderConfig(provider: str, model: str, effort: str | None = None)` with methods `.api_key_env_var() -> str` and `.litellm_model_string() -> str`

- [ ] **Step 1: Write the failing tests**

`tests/llm/__init__.py`: empty file.

`tests/llm/test_providers.py`:

```python
from __future__ import annotations

import pytest

from sebby.llm.providers import ProviderConfig, UnknownEffortError, UnknownProviderError


def test_provider_config_builds_litellm_model_string() -> None:
    config = ProviderConfig(provider="anthropic", model="claude-sonnet-5")

    assert config.litellm_model_string() == "anthropic/claude-sonnet-5"


def test_provider_config_exposes_api_key_env_var() -> None:
    config = ProviderConfig(provider="openai", model="gpt-5.1")

    assert config.api_key_env_var() == "OPENAI_API_KEY"


def test_provider_config_rejects_unknown_provider() -> None:
    with pytest.raises(UnknownProviderError):
        ProviderConfig(provider="not-a-real-provider", model="whatever")


def test_provider_config_accepts_valid_effort() -> None:
    config = ProviderConfig(provider="anthropic", model="claude-sonnet-5", effort="high")

    assert config.effort == "high"


def test_provider_config_rejects_invalid_effort() -> None:
    with pytest.raises(UnknownEffortError):
        ProviderConfig(provider="anthropic", model="claude-sonnet-5", effort="extreme")


def test_provider_config_effort_defaults_to_none() -> None:
    config = ProviderConfig(provider="gemini", model="gemini-2.5-pro")

    assert config.effort is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/llm/test_providers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sebby.llm'`

- [ ] **Step 3: Implement `src/sebby/llm/__init__.py` and `src/sebby/llm/providers.py`**

`src/sebby/llm/__init__.py`:

```python
"""Multi-provider LLM client with Anthropic prompt caching."""
```

`src/sebby/llm/providers.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


class UnknownProviderError(Exception):
    pass


class UnknownEffortError(Exception):
    pass


VALID_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high")

PROVIDER_API_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


@dataclass(frozen=True)
class ProviderConfig:
    """One provider's model choice and effort level.

    Model IDs are supplied by the caller rather than hardcoded here, since
    they go stale independently of this library's release cycle.
    """

    provider: str
    model: str
    effort: str | None = None

    def __post_init__(self) -> None:
        if self.provider not in PROVIDER_API_KEY_ENV:
            raise UnknownProviderError(
                f"unknown provider: {self.provider!r}; "
                f"expected one of {tuple(PROVIDER_API_KEY_ENV)}"
            )
        if self.effort is not None and self.effort not in VALID_EFFORT_LEVELS:
            raise UnknownEffortError(
                f"invalid effort {self.effort!r}; expected one of {VALID_EFFORT_LEVELS}"
            )

    def api_key_env_var(self) -> str:
        return PROVIDER_API_KEY_ENV[self.provider]

    def litellm_model_string(self) -> str:
        return f"{self.provider}/{self.model}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/llm/test_providers.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Run lint and type checks**

Run: `cd /Users/sebby/Developer/sebby && uv run ruff check . && uv run mypy src`
Expected: both clean

- [ ] **Step 6: Commit**

```bash
git -C /Users/sebby/Developer/sebby add src/sebby/llm/__init__.py src/sebby/llm/providers.py tests/llm/__init__.py tests/llm/test_providers.py
git -C /Users/sebby/Developer/sebby commit -m "feat: add sebby.llm.providers with ProviderConfig"
```

---

### Task 4: `sebby.llm.cache` (prompt-cache breakpoint marking)

**Files:**
- Create: `src/sebby/llm/cache.py`
- Create: `tests/llm/test_cache.py`

**Interfaces:**
- Consumes: nothing
- Produces: `MAX_CACHE_BREAKPOINTS: int`, `TooManyCacheBreakpointsError`, `mark_cache_breakpoint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]`, `mark_cache_breakpoint_on_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]`, `count_cache_breakpoints(messages, tools) -> int`, `assert_within_breakpoint_cap(messages, tools) -> None`

- [ ] **Step 1: Write the failing tests**

`tests/llm/test_cache.py`:

```python
from __future__ import annotations

import pytest

from sebby.llm.cache import (
    MAX_CACHE_BREAKPOINTS,
    TooManyCacheBreakpointsError,
    assert_within_breakpoint_cap,
    count_cache_breakpoints,
    mark_cache_breakpoint,
    mark_cache_breakpoint_on_tools,
)


def test_mark_cache_breakpoint_adds_cache_control_to_last_message_only() -> None:
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]

    result = mark_cache_breakpoint(messages)

    assert result[0]["content"] == "first"
    assert result[1]["content"] == "second"
    assert result[2]["content"] == [
        {"type": "text", "text": "third", "cache_control": {"type": "ephemeral"}}
    ]


def test_mark_cache_breakpoint_does_not_mutate_input() -> None:
    messages = [{"role": "user", "content": "only message"}]

    mark_cache_breakpoint(messages)

    assert messages == [{"role": "user", "content": "only message"}]


def test_mark_cache_breakpoint_handles_empty_list() -> None:
    assert mark_cache_breakpoint([]) == []


def test_mark_cache_breakpoint_on_tools_marks_last_tool_only() -> None:
    tools = [{"name": "search"}, {"name": "fetch"}]

    result = mark_cache_breakpoint_on_tools(tools)

    assert result[0] == {"name": "search"}
    assert result[1] == {"name": "fetch", "cache_control": {"type": "ephemeral"}}


def test_mark_cache_breakpoint_on_tools_does_not_mutate_input() -> None:
    tools = [{"name": "search"}]

    mark_cache_breakpoint_on_tools(tools)

    assert tools == [{"name": "search"}]


def test_mark_cache_breakpoint_on_tools_handles_empty_list() -> None:
    assert mark_cache_breakpoint_on_tools([]) == []


def test_count_cache_breakpoints_counts_messages_and_tools() -> None:
    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}}],
        },
    ]
    tools = [{"name": "search", "cache_control": {"type": "ephemeral"}}]

    assert count_cache_breakpoints(messages, tools) == 2


def test_assert_within_breakpoint_cap_raises_when_exceeded() -> None:
    block = {"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}
    messages = [{"role": "user", "content": [block]} for _ in range(MAX_CACHE_BREAKPOINTS + 1)]

    with pytest.raises(TooManyCacheBreakpointsError):
        assert_within_breakpoint_cap(messages, [])


def test_assert_within_breakpoint_cap_passes_at_exactly_the_cap() -> None:
    block = {"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}
    messages = [{"role": "user", "content": [block]} for _ in range(MAX_CACHE_BREAKPOINTS)]

    assert_within_breakpoint_cap(messages, [])  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/llm/test_cache.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sebby.llm.cache'`

- [ ] **Step 3: Implement `src/sebby/llm/cache.py`**

```python
from __future__ import annotations

from typing import Any

MAX_CACHE_BREAKPOINTS = 4
_CACHE_CONTROL = {"type": "ephemeral"}


class TooManyCacheBreakpointsError(Exception):
    pass


def _as_content_blocks(content: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content)


def mark_cache_breakpoint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a shallow copy of `messages` with a cache_control breakpoint on
    the last message only. Never mutates the input list or its entries.

    Anthropic allows at most `MAX_CACHE_BREAKPOINTS` cache_control
    breakpoints per request; this function only ever adds one, so callers
    may combine its result with breakpoints added elsewhere (e.g. on tools)
    up to that cap.
    """
    if not messages:
        return []

    *head, last = messages
    blocks = _as_content_blocks(last["content"])
    if not blocks:
        return [*head, last]

    marked_blocks = [*blocks[:-1], {**blocks[-1], "cache_control": dict(_CACHE_CONTROL)}]
    marked_last = {**last, "content": marked_blocks}
    return [*head, marked_last]


def mark_cache_breakpoint_on_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a shallow copy of `tools` with a cache_control breakpoint on
    the last tool definition only. Never mutates the input.
    """
    if not tools:
        return []

    *head, last = tools
    marked_last = {**last, "cache_control": dict(_CACHE_CONTROL)}
    return [*head, marked_last]


def count_cache_breakpoints(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
    """Count cache_control breakpoints already present, so callers can check
    against Anthropic's per-request cap before adding more.
    """
    count = 0
    for message in messages:
        for block in _as_content_blocks(message.get("content", "")):
            if "cache_control" in block:
                count += 1
    for tool in tools:
        if "cache_control" in tool:
            count += 1
    return count


def assert_within_breakpoint_cap(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> None:
    count = count_cache_breakpoints(messages, tools)
    if count > MAX_CACHE_BREAKPOINTS:
        raise TooManyCacheBreakpointsError(
            f"{count} cache_control breakpoints exceeds Anthropic's cap of {MAX_CACHE_BREAKPOINTS}"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/llm/test_cache.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: Run lint and type checks**

Run: `cd /Users/sebby/Developer/sebby && uv run ruff check . && uv run mypy src`
Expected: both clean

- [ ] **Step 6: Commit**

```bash
git -C /Users/sebby/Developer/sebby add src/sebby/llm/cache.py tests/llm/test_cache.py
git -C /Users/sebby/Developer/sebby commit -m "feat: add sebby.llm.cache prompt-cache breakpoint marking"
```

---

### Task 5: `sebby.llm.client` (multi-provider failover + cache wiring + telemetry)

**Files:**
- Create: `src/sebby/llm/client.py`
- Modify: `src/sebby/llm/__init__.py` (export the public API)
- Create: `tests/llm/test_client.py`

**Interfaces:**
- Consumes: `ProviderConfig`, `.litellm_model_string()` (Task 3); `mark_cache_breakpoint`, `mark_cache_breakpoint_on_tools`, `assert_within_breakpoint_cap` (Task 4); `RetryExhaustedError`, `retry_with_backoff` (Task 2)
- Produces: `UsageRecord(provider, model, input_tokens, output_tokens, cache_creation_input_tokens=0, cache_read_input_tokens=0)`, `AllProvidersFailedError`, `LLMClient(providers, *, completion_fn=None, on_usage=None, max_attempts_per_provider=2)` with `.complete(*, system, messages, tools=None) -> Any`

- [ ] **Step 1: Write the failing tests**

`tests/llm/test_client.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from sebby.llm.client import AllProvidersFailedError, LLMClient, UsageRecord
from sebby.llm.providers import ProviderConfig


@dataclass
class FakeUsage:
    prompt_tokens: int
    completion_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class FakeResponse:
    usage: FakeUsage
    text: str = "ok"


def test_complete_calls_first_provider_and_returns_response() -> None:
    calls: list[dict[str, Any]] = []

    def fake_completion(**kwargs: Any) -> FakeResponse:
        calls.append(kwargs)
        return FakeResponse(usage=FakeUsage(prompt_tokens=10, completion_tokens=5))

    client = LLMClient(
        providers=[ProviderConfig(provider="anthropic", model="claude-sonnet-5")],
        completion_fn=fake_completion,
    )

    response = client.complete(system="be helpful", messages=[{"role": "user", "content": "hi"}])

    assert response.text == "ok"
    assert len(calls) == 1
    assert calls[0]["model"] == "anthropic/claude-sonnet-5"


def test_complete_marks_cache_breakpoint_for_anthropic_only() -> None:
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> FakeResponse:
        captured.update(kwargs)
        return FakeResponse(usage=FakeUsage(prompt_tokens=1, completion_tokens=1))

    client = LLMClient(
        providers=[ProviderConfig(provider="anthropic", model="claude-sonnet-5")],
        completion_fn=fake_completion,
    )

    client.complete(system="be helpful", messages=[{"role": "user", "content": "hi"}])

    last_message = captured["messages"][-1]
    assert last_message["content"] == [
        {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
    ]


def test_complete_does_not_mark_cache_breakpoint_for_non_anthropic() -> None:
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> FakeResponse:
        captured.update(kwargs)
        return FakeResponse(usage=FakeUsage(prompt_tokens=1, completion_tokens=1))

    client = LLMClient(
        providers=[ProviderConfig(provider="openai", model="gpt-5.1")],
        completion_fn=fake_completion,
    )

    client.complete(system="be helpful", messages=[{"role": "user", "content": "hi"}])

    last_message = captured["messages"][-1]
    assert last_message["content"] == "hi"


def test_complete_fails_over_to_next_provider_on_exhausted_retries() -> None:
    attempts: list[str] = []

    def fake_completion(**kwargs: Any) -> FakeResponse:
        attempts.append(kwargs["model"])
        if kwargs["model"].startswith("anthropic/"):
            raise RuntimeError("anthropic is down")
        return FakeResponse(usage=FakeUsage(prompt_tokens=1, completion_tokens=1))

    client = LLMClient(
        providers=[
            ProviderConfig(provider="anthropic", model="claude-sonnet-5"),
            ProviderConfig(provider="openai", model="gpt-5.1"),
        ],
        completion_fn=fake_completion,
        max_attempts_per_provider=1,
    )

    response = client.complete(system="be helpful", messages=[{"role": "user", "content": "hi"}])

    assert response.text == "ok"
    assert attempts == ["anthropic/claude-sonnet-5", "openai/gpt-5.1"]


def test_complete_raises_when_all_providers_fail() -> None:
    def fake_completion(**kwargs: Any) -> FakeResponse:
        raise RuntimeError("down")

    client = LLMClient(
        providers=[ProviderConfig(provider="anthropic", model="claude-sonnet-5")],
        completion_fn=fake_completion,
        max_attempts_per_provider=1,
    )

    with pytest.raises(AllProvidersFailedError):
        client.complete(system="be helpful", messages=[{"role": "user", "content": "hi"}])


def test_complete_invokes_on_usage_callback_with_token_counts() -> None:
    records: list[UsageRecord] = []

    def fake_completion(**kwargs: Any) -> FakeResponse:
        return FakeResponse(
            usage=FakeUsage(
                prompt_tokens=100,
                completion_tokens=20,
                cache_creation_input_tokens=80,
                cache_read_input_tokens=0,
            )
        )

    client = LLMClient(
        providers=[ProviderConfig(provider="anthropic", model="claude-sonnet-5")],
        completion_fn=fake_completion,
        on_usage=records.append,
    )

    client.complete(system="be helpful", messages=[{"role": "user", "content": "hi"}])

    assert records == [
        UsageRecord(
            provider="anthropic",
            model="claude-sonnet-5",
            input_tokens=100,
            output_tokens=20,
            cache_creation_input_tokens=80,
            cache_read_input_tokens=0,
        )
    ]


def test_complete_raises_value_error_when_no_providers_configured() -> None:
    with pytest.raises(ValueError):
        LLMClient(providers=[], completion_fn=lambda **_: None)


def test_llm_package_exports_public_api() -> None:
    from sebby.llm import AllProvidersFailedError as ExportedError
    from sebby.llm import LLMClient as ExportedClient
    from sebby.llm import ProviderConfig as ExportedConfig

    assert ExportedError is AllProvidersFailedError
    assert ExportedClient is LLMClient
    assert ExportedConfig is ProviderConfig
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/llm/test_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sebby.llm.client'`

- [ ] **Step 3: Implement `src/sebby/llm/client.py`**

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from sebby.llm.cache import (
    assert_within_breakpoint_cap,
    mark_cache_breakpoint,
    mark_cache_breakpoint_on_tools,
)
from sebby.llm.providers import ProviderConfig
from sebby.retry import RetryExhaustedError, retry_with_backoff


@dataclass(frozen=True)
class UsageRecord:
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class AllProvidersFailedError(Exception):
    pass


class CompletionFn(Protocol):
    def __call__(self, **kwargs: Any) -> Any: ...


def _default_completion_fn() -> CompletionFn:
    import litellm

    return litellm.completion


class LLMClient:
    """Multi-provider chat-completion client with Anthropic-first prompt
    caching and automatic failover across configured providers, tried in
    the order given.
    """

    def __init__(
        self,
        providers: list[ProviderConfig],
        *,
        completion_fn: CompletionFn | None = None,
        on_usage: Callable[[UsageRecord], None] | None = None,
        max_attempts_per_provider: int = 2,
    ) -> None:
        if not providers:
            raise ValueError("at least one provider must be configured")
        self._providers = providers
        self._completion_fn = completion_fn or _default_completion_fn()
        self._on_usage = on_usage
        self._max_attempts_per_provider = max_attempts_per_provider

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        tools = tools or []
        errors: list[Exception] = []

        for provider in self._providers:
            call_messages = messages
            call_tools = tools
            if provider.provider == "anthropic":
                call_messages = mark_cache_breakpoint(messages)
                call_tools = mark_cache_breakpoint_on_tools(tools)
                assert_within_breakpoint_cap(call_messages, call_tools)

            def _call(
                _provider: ProviderConfig = provider,
                _messages: list[dict[str, Any]] = call_messages,
                _tools: list[dict[str, Any]] = call_tools,
            ) -> Any:
                return self._completion_fn(
                    model=_provider.litellm_model_string(),
                    messages=[{"role": "system", "content": system}, *_messages],
                    tools=_tools or None,
                )

            try:
                response = retry_with_backoff(_call, max_attempts=self._max_attempts_per_provider)
            except RetryExhaustedError as exc:
                errors.append(exc)
                continue

            self._record_usage(provider, response)
            return response

        raise AllProvidersFailedError(f"all {len(self._providers)} provider(s) failed: {errors}")

    def _record_usage(self, provider: ProviderConfig, response: Any) -> None:
        if self._on_usage is None:
            return
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self._on_usage(
            UsageRecord(
                provider=provider.provider,
                model=provider.model,
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            )
        )
```

- [ ] **Step 4: Update `src/sebby/llm/__init__.py` to export the public API**

```python
"""Multi-provider LLM client with Anthropic prompt caching."""

from sebby.llm.client import AllProvidersFailedError, LLMClient, UsageRecord
from sebby.llm.providers import ProviderConfig, UnknownEffortError, UnknownProviderError

__all__ = [
    "AllProvidersFailedError",
    "LLMClient",
    "ProviderConfig",
    "UnknownEffortError",
    "UnknownProviderError",
    "UsageRecord",
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/llm/test_client.py -v`
Expected: PASS (8 passed)

- [ ] **Step 6: Run the full test suite, lint, and type checks**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest && uv run ruff check . && uv run mypy src`
Expected: all clean

- [ ] **Step 7: Commit**

```bash
git -C /Users/sebby/Developer/sebby add src/sebby/llm/client.py src/sebby/llm/__init__.py tests/llm/test_client.py
git -C /Users/sebby/Developer/sebby commit -m "feat: add LLMClient with multi-provider failover and usage telemetry"
```

---

### Task 6: Release prep for v0.1.0

**Files:**
- Create: `README.md`
- Create: `CHANGELOG.md`

**Interfaces:**
- Consumes: `LLMClient`, `ProviderConfig` (Task 5); `retry_with_backoff`, `retry_once_on_server_error` (Task 2)
- Produces: nothing new (documentation only)

- [ ] **Step 1: Write `README.md`**

```markdown
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
```

- [ ] **Step 2: Write `CHANGELOG.md`**

```markdown
# Changelog

## 0.1.0

- Add `sebby.retry`: `retry_with_backoff` and `retry_once_on_server_error`.
- Add `sebby.llm`: `LLMClient` with multi-provider failover, Anthropic
  prompt-cache breakpoint marking, and pluggable usage telemetry.
```

- [ ] **Step 3: Build the package to confirm packaging metadata is correct**

Run: `cd /Users/sebby/Developer/sebby && uv build`
Expected: creates `dist/sebby-0.1.0-py3-none-any.whl` and `dist/sebby-0.1.0.tar.gz` with no errors

- [ ] **Step 4: Run the full quality gate one last time**

Run: `cd /Users/sebby/Developer/sebby && make pre-push`
Expected: all steps pass

- [ ] **Step 5: Commit**

```bash
git -C /Users/sebby/Developer/sebby add README.md CHANGELOG.md
git -C /Users/sebby/Developer/sebby commit -m "docs: add README and changelog for v0.1.0"
```

- [ ] **Step 6: Push and tag the release** (once this plan's changes are on `main`, e.g. after PR merge per the standard Git & PR workflow)

```bash
git -C /Users/sebby/Developer/sebby push -u origin main
git -C /Users/sebby/Developer/sebby tag v0.1.0
git -C /Users/sebby/Developer/sebby push origin v0.1.0
```
