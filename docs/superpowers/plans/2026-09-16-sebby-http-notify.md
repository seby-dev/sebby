# Sebby HTTP & Notify Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `sebby.http` (a subpackage of FastAPI helpers: shared-secret auth, localhost CORS, upload-size limits, threadpool file writes, a bounded async job store) and `sebby.notify` (Telegram alert sending with MarkdownV2 escaping), continuing the spec's phase 2 rollout.

**Architecture:** `sebby.http` is a subpackage (`src/sebby/http/`) with one file per concern, matching `sebby.llm`'s existing subpackage precedent — `fastapi` is isolated to a new `http` optional extra. `sebby.notify` is a single stdlib-only file: its HTTP call is injected as a callable parameter rather than importing an HTTP client directly, so it needs no extra of its own and stays trivially testable.

**Tech Stack:** Python 3.11+, `uv`, `fastapi` (via the new `http` extra), `httpx`/`pytest-asyncio` (test-only, for `TestClient` and async tests), `pytest`, `ruff`, `mypy` (strict).

**Spec:** `docs/superpowers/specs/2026-09-16-shared-toolkit-design.md`

**Note on scope:** This is plan 3 of several. Two prior plans already shipped `sebby.retry`/`sebby.llm` and `sebby.storage`/`sebby.cache`/`sebby.cli`/`sebby.config`/`sebby.logging` plus shared config artifacts (all merged to `main`). This plan covers the remaining spec phase 2 module: the FastAPI-facing pieces, deliberately split out earlier for their added complexity. The Claude Code plugin, the `organist_bot` pilot migration, and the CLAUDE.md de-duplication each get their own plan later still.

**Lessons carried from the two merged plans (apply throughout):**
- Every task in both prior plans that had real type complexity needed at least one mypy-strict fix beyond its first draft (`retry.py`'s `on_attempt` closure, `client.py`'s `cast()`, `config.py`'s `get_settings`, `logging.py`'s two additions). This plan's code has been written more carefully to avoid the specific pattern that kept recurring (`dict[type, Any]`-style lookups returned from a function declared to return a narrower type) — `sebby.http.jobs`'s `Job.result: Any` is a plain dataclass field, not a lookup-then-return, which avoids it. Still expect the implementer may need a small adjustment; the brief pre-authorizes minimal, behavior-preserving fixes the same way prior tasks did.
- The `sebby.logging` plan shipped a secret-scrubber that missed an entire code path (stdlib-originated log records) on the first pass, caught only in final review. `sebby.notify` handles this differently: message content passed to `escape_markdown_v2` is escaped unconditionally by default, and there's no separate "scrub" concept to accidentally leave a gap in — read that task's tests carefully to confirm they actually prove escaping happens on the real, final string sent to `post_fn`, not an intermediate one.
- README updates are folded into the task that introduces each new top-level module (Task 1 adds the `sebby.http` row + `http` extra row; Task 6 adds the `sebby.notify` row), not deferred to a final task — a cross-repo reusable-workflow filename mismatch was caught only in final review last plan specifically because a doc update was batched separately from the code that made it accurate.

## Global Constraints

- Package is importable as `sebby`; `sebby.http` (a subpackage) and `sebby.notify` (a single file) must each be independently importable.
- `fastapi` lives in a new `http` optional-dependency group, not the base `dependencies` (which stays `[]`); `dev` depends on `sebby[http]` plus test-only `httpx` and `pytest-asyncio`.
- `sebby.notify` takes its HTTP-posting function as an injected parameter rather than importing an HTTP client directly — it needs no extra of its own.
- All tests use pytest; FastAPI tests use `TestClient` (in-process, no real network); `sebby.notify` tests inject a fake `post_fn`; no test makes a real network call.
- README gets a new row in `## Modules` and (for `sebby.http`) a new row in `## Optional extras` as part of the task that introduces the module, not a separate task.

---

### Task 1: `sebby.http` scaffolding and `sebby.http.auth`

**Files:**
- Modify: `pyproject.toml` (add `http` extra, `dev` additions, `asyncio_mode` pytest setting)
- Create: `src/sebby/http/__init__.py`
- Create: `src/sebby/http/auth.py`
- Create: `tests/http/__init__.py`
- Create: `tests/http/test_auth.py`
- Modify: `README.md` (add `sebby.http` row to Modules, `http` row to Optional extras)

**Interfaces:**
- Consumes: nothing from earlier tasks in this plan
- Produces: `MissingSharedSecretEnvVarError`, `require_shared_secret(env_var: str, *, header_name: str = "X-API-Key") -> Callable[[str | None], None]`

- [ ] **Step 1: Add the `http` extra and test dependencies to `pyproject.toml`**

Edit `[project.optional-dependencies]` and `[tool.pytest.ini_options]`:

```toml
[project.optional-dependencies]
llm = [
    "litellm>=1.50.0",
]
config = [
    "pydantic-settings>=2.0.0",
]
logging = [
    "structlog>=24.0.0",
]
http = [
    "fastapi>=0.115.0",
]
dev = [
    "sebby[llm]",
    "sebby[config]",
    "sebby[logging]",
    "sebby[http]",
    "pytest>=8.0.0",
    "pytest-asyncio>=0.24.0",
    "httpx>=0.27.0",
    "ruff>=0.6.0",
    "mypy>=1.11.0",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

- [ ] **Step 2: Sync the environment**

Run: `cd /Users/sebby/Developer/sebby && uv sync --extra dev`
Expected: installs `fastapi`, `httpx`, `pytest-asyncio` with no errors

- [ ] **Step 3: Write the failing tests**

`tests/http/__init__.py`: empty file.

`tests/http/test_auth.py`:

```python
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from sebby.http.auth import MissingSharedSecretEnvVarError, require_shared_secret


def test_require_shared_secret_raises_when_env_var_missing(monkeypatch):
    monkeypatch.delenv("MY_SECRET", raising=False)

    with pytest.raises(MissingSharedSecretEnvVarError):
        require_shared_secret("MY_SECRET")


def test_dependency_accepts_correct_secret(monkeypatch):
    monkeypatch.setenv("MY_SECRET", "correct-horse")
    dependency = require_shared_secret("MY_SECRET")

    dependency(provided="correct-horse")  # must not raise


def test_dependency_rejects_wrong_secret(monkeypatch):
    monkeypatch.setenv("MY_SECRET", "correct-horse")
    dependency = require_shared_secret("MY_SECRET")

    with pytest.raises(HTTPException) as exc_info:
        dependency(provided="wrong-guess")

    assert exc_info.value.status_code == 401


def test_dependency_rejects_missing_header(monkeypatch):
    monkeypatch.setenv("MY_SECRET", "correct-horse")
    dependency = require_shared_secret("MY_SECRET")

    with pytest.raises(HTTPException) as exc_info:
        dependency(provided=None)

    assert exc_info.value.status_code == 401


def test_require_shared_secret_wires_into_a_real_app(monkeypatch):
    monkeypatch.setenv("MY_SECRET", "correct-horse")
    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(require_shared_secret("MY_SECRET"))])
    def protected() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)

    assert client.get("/protected", headers={"X-API-Key": "correct-horse"}).status_code == 200
    assert client.get("/protected", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/protected").status_code == 401
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/http/test_auth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sebby.http'`

- [ ] **Step 5: Implement `src/sebby/http/__init__.py` and `src/sebby/http/auth.py`**

`src/sebby/http/__init__.py`:

```python
"""FastAPI helpers: shared-secret auth, CORS, upload limits, threadpool file writes, job store."""

from sebby.http.auth import MissingSharedSecretEnvVarError, require_shared_secret

__all__ = [
    "MissingSharedSecretEnvVarError",
    "require_shared_secret",
]
```

`src/sebby/http/auth.py`:

```python
from __future__ import annotations

import os
import secrets
from typing import Annotated, Callable

from fastapi import Header, HTTPException, status


class MissingSharedSecretEnvVarError(Exception):
    pass


def require_shared_secret(
    env_var: str, *, header_name: str = "X-API-Key"
) -> Callable[[str | None], None]:
    """Build a FastAPI dependency requiring a shared-secret header.

    Reads the expected secret from `env_var` at CALL time (when this
    factory runs, typically once at router-setup time) — a missing or
    empty secret raises immediately, so misconfiguration fails loudly at
    startup instead of silently rejecting every request. Compares with
    `secrets.compare_digest` (constant-time) rather than `==`, so a wrong
    guess can't be distinguished from a right one by response timing.
    """
    expected = os.environ.get(env_var)
    if not expected:
        raise MissingSharedSecretEnvVarError(
            f"{env_var} is not set; required for shared-secret auth"
        )

    def dependency(
        provided: Annotated[str | None, Header(alias=header_name)] = None,
    ) -> None:
        if provided is None or not secrets.compare_digest(provided, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    return dependency
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/http/test_auth.py -v`
Expected: PASS (5 passed)

- [ ] **Step 7: Run lint and type checks**

Run: `cd /Users/sebby/Developer/sebby && uv run ruff check . && uv run mypy src`
Expected: both clean. If mypy flags `fastapi`/`starlette` as missing stubs, both ship `py.typed` as of the pinned versions — if it still complains, add a scoped `[[tool.mypy.overrides]]` block rather than a blanket ignore, and note why in your report.

- [ ] **Step 8: Update `README.md`**

Add a row to the `## Modules` table:

```markdown
| `sebby.http` | FastAPI helpers: shared-secret auth, localhost CORS, upload-size limits, threadpool file writes, bounded async job store |
```

Add a row to the `## Optional extras` table:

```markdown
| `http` | `sebby.http` |
```

- [ ] **Step 9: Run the full suite to confirm no regressions**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest`
Expected: all previous tests plus these 5 pass

- [ ] **Step 10: Commit**

```bash
git add pyproject.toml uv.lock src/sebby/http/__init__.py src/sebby/http/auth.py tests/http/__init__.py tests/http/test_auth.py README.md
git commit -m "feat: add sebby.http.auth shared-secret dependency"
```

---

### Task 2: `sebby.http.cors`

**Files:**
- Modify: `src/sebby/http/__init__.py` (add export)
- Create: `src/sebby/http/cors.py`
- Create: `tests/http/test_cors.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `add_localhost_cors(app: FastAPI, *, allow_credentials: bool = True, extra_origin_regex: str | None = None) -> None`

- [ ] **Step 1: Write the failing tests**

`tests/http/test_cors.py`:

```python
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sebby.http.cors import add_localhost_cors


def _build_app(*, extra_origin_regex: str | None = None) -> FastAPI:
    app = FastAPI()
    add_localhost_cors(app, extra_origin_regex=extra_origin_regex)

    @app.get("/ping")
    def ping() -> dict[str, bool]:
        return {"ok": True}

    return app


def test_add_localhost_cors_allows_localhost_origin():
    client = TestClient(_build_app())

    response = client.get("/ping", headers={"Origin": "http://localhost:3000"})

    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_add_localhost_cors_rejects_non_localhost_origin():
    client = TestClient(_build_app())

    response = client.get("/ping", headers={"Origin": "https://evil.example.com"})

    assert "access-control-allow-origin" not in response.headers


def test_add_localhost_cors_allows_extra_regex_when_given():
    client = TestClient(_build_app(extra_origin_regex=r"^https://staging\.example\.com$"))

    response = client.get("/ping", headers={"Origin": "https://staging.example.com"})

    assert response.headers.get("access-control-allow-origin") == "https://staging.example.com"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/http/test_cors.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sebby.http.cors'`

- [ ] **Step 3: Implement `src/sebby/http/cors.py`**

```python
from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

_LOCALHOST_ORIGIN_PATTERN = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"


def add_localhost_cors(
    app: FastAPI, *, allow_credentials: bool = True, extra_origin_regex: str | None = None
) -> None:
    """Add a CORS policy scoped to localhost origins only (any port).

    Safe to combine with `allow_credentials=True` because it never uses a
    wildcard origin — `allow_origins=["*"]` plus credentials is a common
    misconfiguration (and one browsers reject outright) that a regex
    scoped to specific origins avoids by construction.
    """
    pattern = _LOCALHOST_ORIGIN_PATTERN
    if extra_origin_regex is not None:
        pattern = f"({_LOCALHOST_ORIGIN_PATTERN})|({extra_origin_regex})"
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=pattern,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )
```

- [ ] **Step 4: Update `src/sebby/http/__init__.py`**

```python
"""FastAPI helpers: shared-secret auth, CORS, upload limits, threadpool file writes, job store."""

from sebby.http.auth import MissingSharedSecretEnvVarError, require_shared_secret
from sebby.http.cors import add_localhost_cors

__all__ = [
    "MissingSharedSecretEnvVarError",
    "add_localhost_cors",
    "require_shared_secret",
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/http/test_cors.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Run lint and type checks**

Run: `cd /Users/sebby/Developer/sebby && uv run ruff check . && uv run mypy src`
Expected: both clean

- [ ] **Step 7: Commit**

```bash
git add src/sebby/http/__init__.py src/sebby/http/cors.py tests/http/test_cors.py
git commit -m "feat: add sebby.http.cors localhost-scoped CORS setup"
```

---

### Task 3: `sebby.http.limits`

**Files:**
- Modify: `src/sebby/http/__init__.py` (add exports)
- Create: `src/sebby/http/limits.py`
- Create: `tests/http/test_limits.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `PayloadTooLargeError`, `add_content_length_limit(app: FastAPI, *, max_bytes: int) -> None`, `read_capped(request: Request, *, max_bytes: int) -> bytes`

- [ ] **Step 1: Write the failing tests**

`tests/http/test_limits.py`:

```python
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from sebby.http.limits import PayloadTooLargeError, add_content_length_limit, read_capped


def _build_app() -> FastAPI:
    app = FastAPI()
    add_content_length_limit(app, max_bytes=10)

    @app.post("/upload")
    async def upload(request: Request) -> dict[str, int]:
        body = await request.body()
        return {"size": len(body)}

    @app.post("/upload-streamed")
    async def upload_streamed(request: Request):
        try:
            body = await read_capped(request, max_bytes=10)
        except PayloadTooLargeError:
            return JSONResponse({"detail": "too large"}, status_code=413)
        return {"size": len(body)}

    return app


def test_add_content_length_limit_allows_small_body():
    client = TestClient(_build_app())

    response = client.post("/upload", content=b"small")

    assert response.status_code == 200
    assert response.json() == {"size": 5}


def test_add_content_length_limit_rejects_declared_oversized_body():
    client = TestClient(_build_app())

    response = client.post("/upload", content=b"x" * 20)

    assert response.status_code == 413


def test_read_capped_allows_body_within_cap():
    client = TestClient(_build_app())

    response = client.post("/upload-streamed", content=b"x" * 5)

    assert response.status_code == 200
    assert response.json() == {"size": 5}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/http/test_limits.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sebby.http.limits'`

- [ ] **Step 3: Implement `src/sebby/http/limits.py`**

```python
from __future__ import annotations

from typing import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse


class PayloadTooLargeError(Exception):
    pass


def add_content_length_limit(app: FastAPI, *, max_bytes: int) -> None:
    """Reject requests whose declared Content-Length exceeds `max_bytes`
    before the body is read at all — a cheap first line of defense.

    This does NOT protect against a request that lies about its
    Content-Length or omits it (e.g. chunked transfer encoding); pair with
    `read_capped` when actually consuming the body to close that gap.
    """

    @app.middleware("http")
    async def _content_length_limit(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None and int(content_length) > max_bytes:
            return JSONResponse({"detail": "payload too large"}, status_code=413)
        return await call_next(request)


async def read_capped(request: Request, *, max_bytes: int) -> bytes:
    """Stream-read a request body, raising `PayloadTooLargeError` as soon
    as more than `max_bytes` have been read — protects against a body
    larger than its declared (or missing, or lying) Content-Length.
    """
    size = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise PayloadTooLargeError(f"body exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)
```

- [ ] **Step 4: Update `src/sebby/http/__init__.py`**

```python
"""FastAPI helpers: shared-secret auth, CORS, upload limits, threadpool file writes, job store."""

from sebby.http.auth import MissingSharedSecretEnvVarError, require_shared_secret
from sebby.http.cors import add_localhost_cors
from sebby.http.limits import PayloadTooLargeError, add_content_length_limit, read_capped

__all__ = [
    "MissingSharedSecretEnvVarError",
    "PayloadTooLargeError",
    "add_content_length_limit",
    "add_localhost_cors",
    "read_capped",
    "require_shared_secret",
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/http/test_limits.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Run lint and type checks**

Run: `cd /Users/sebby/Developer/sebby && uv run ruff check . && uv run mypy src`
Expected: both clean

- [ ] **Step 7: Commit**

```bash
git add src/sebby/http/__init__.py src/sebby/http/limits.py tests/http/test_limits.py
git commit -m "feat: add sebby.http.limits upload-size capping"
```

---

### Task 4: `sebby.http.files`

**Files:**
- Modify: `src/sebby/http/__init__.py` (add export)
- Create: `src/sebby/http/files.py`
- Create: `tests/http/test_files.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `write_temp_file(data: bytes, *, suffix: str = "") -> Path` (async)

- [ ] **Step 1: Write the failing tests**

`tests/http/test_files.py`:

```python
from __future__ import annotations

import pytest

from sebby.http.files import write_temp_file


async def test_write_temp_file_writes_data_and_returns_readable_path():
    path = await write_temp_file(b"hello world", suffix=".bin")
    try:
        assert path.read_bytes() == b"hello world"
        assert path.suffix == ".bin"
    finally:
        path.unlink()


async def test_write_temp_file_propagates_mkstemp_failure(monkeypatch):
    def _boom(*args: object, **kwargs: object) -> tuple[int, str]:
        raise OSError("no space left on device")

    monkeypatch.setattr("tempfile.mkstemp", _boom)

    with pytest.raises(OSError):
        await write_temp_file(b"data")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/http/test_files.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sebby.http.files'`

- [ ] **Step 3: Implement `src/sebby/http/files.py`**

```python
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from starlette.concurrency import run_in_threadpool


async def write_temp_file(data: bytes, *, suffix: str = "") -> Path:
    """Write `data` to a new temporary file off the event loop (via
    Starlette's thread pool), returning its path. The caller owns cleanup
    (`path.unlink()`) — this function only creates the file.
    """

    def _write() -> Path:
        fd, name = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise
        return Path(name)

    return await run_in_threadpool(_write)
```

- [ ] **Step 4: Update `src/sebby/http/__init__.py`**

```python
"""FastAPI helpers: shared-secret auth, CORS, upload limits, threadpool file writes, job store."""

from sebby.http.auth import MissingSharedSecretEnvVarError, require_shared_secret
from sebby.http.cors import add_localhost_cors
from sebby.http.files import write_temp_file
from sebby.http.limits import PayloadTooLargeError, add_content_length_limit, read_capped

__all__ = [
    "MissingSharedSecretEnvVarError",
    "PayloadTooLargeError",
    "add_content_length_limit",
    "add_localhost_cors",
    "read_capped",
    "require_shared_secret",
    "write_temp_file",
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/http/test_files.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Run lint and type checks**

Run: `cd /Users/sebby/Developer/sebby && uv run ruff check . && uv run mypy src`
Expected: both clean

- [ ] **Step 7: Commit**

```bash
git add src/sebby/http/__init__.py src/sebby/http/files.py tests/http/test_files.py
git commit -m "feat: add sebby.http.files threadpool temp-file writer"
```

---

### Task 5: `sebby.http.jobs`

**Files:**
- Modify: `src/sebby/http/__init__.py` (add exports)
- Create: `src/sebby/http/jobs.py`
- Create: `tests/http/test_jobs.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `JobStatus` (str enum: `PENDING`, `RUNNING`, `DONE`, `ERROR`), `Job` (dataclass: `id`, `status`, `result`, `error`, `created_at`), `JobNotFoundError`, `JobStore(*, max_jobs: int = 1000, clock: Callable[[], float] = time.monotonic)` with `.create() -> str`, `.get(job_id: str) -> Job`, `.mark_running(job_id: str) -> None`, `.mark_done(job_id: str, result: Any) -> None`, `.mark_error(job_id: str, error: str) -> None`

- [ ] **Step 1: Write the failing tests**

`tests/http/test_jobs.py`:

```python
from __future__ import annotations

import pytest

from sebby.http.jobs import Job, JobNotFoundError, JobStatus, JobStore


def test_create_returns_unique_job_ids():
    store = JobStore()

    first = store.create()
    second = store.create()

    assert first != second


def test_get_returns_pending_job_after_create():
    store = JobStore()

    job_id = store.create()
    job = store.get(job_id)

    assert job.status == JobStatus.PENDING
    assert job.result is None
    assert job.error is None


def test_mark_running_updates_status():
    store = JobStore()
    job_id = store.create()

    store.mark_running(job_id)

    assert store.get(job_id).status == JobStatus.RUNNING


def test_mark_done_sets_result_and_status():
    store = JobStore()
    job_id = store.create()

    store.mark_done(job_id, {"answer": 42})

    job = store.get(job_id)
    assert job.status == JobStatus.DONE
    assert job.result == {"answer": 42}


def test_mark_error_sets_error_and_status():
    store = JobStore()
    job_id = store.create()

    store.mark_error(job_id, "something broke")

    job = store.get(job_id)
    assert job.status == JobStatus.ERROR
    assert job.error == "something broke"


def test_get_raises_for_unknown_job_id():
    store = JobStore()

    with pytest.raises(JobNotFoundError):
        store.get("does-not-exist")


def test_create_evicts_oldest_finished_job_when_at_capacity():
    clock = {"t": 0.0}
    store = JobStore(max_jobs=2, clock=lambda: clock["t"])

    clock["t"] = 1.0
    old_job_id = store.create()
    store.mark_done(old_job_id, "first")

    clock["t"] = 2.0
    other_job_id = store.create()
    store.mark_done(other_job_id, "second")

    clock["t"] = 3.0
    new_job_id = store.create()  # at capacity — should evict old_job_id (oldest finished)

    with pytest.raises(JobNotFoundError):
        store.get(old_job_id)
    assert store.get(other_job_id).result == "second"
    assert store.get(new_job_id).status == JobStatus.PENDING


def test_create_raises_when_full_and_nothing_finished_to_evict():
    store = JobStore(max_jobs=1)
    store.create()  # still PENDING — nothing finished to evict

    with pytest.raises(RuntimeError):
        store.create()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/http/test_jobs.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sebby.http.jobs'`

- [ ] **Step 3: Implement `src/sebby/http/jobs.py`**

```python
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


@dataclass
class Job:
    id: str
    status: JobStatus = JobStatus.PENDING
    result: Any = None
    error: str | None = None
    created_at: float = field(default_factory=time.monotonic)


class JobNotFoundError(Exception):
    pass


class JobStore:
    """A bounded, in-memory job store for async job-polling APIs.

    Not process-shared — one instance per running server process. When at
    capacity, the oldest DONE or ERROR job is evicted to make room for a
    new one; PENDING/RUNNING jobs are never evicted, so a store that's full
    of still-in-flight jobs raises `RuntimeError` on `create()` rather than
    silently dropping in-flight work.

    Status is always written last in `mark_done`/`mark_error` (after
    `result`/`error`), so a caller polling `get(job_id).status` and seeing
    DONE/ERROR is guaranteed to also see the corresponding result/error
    already set — no lock needed for that read.
    """

    def __init__(
        self, *, max_jobs: int = 1000, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._max_jobs = max_jobs
        self._clock = clock
        self._jobs: dict[str, Job] = {}

    def create(self) -> str:
        if len(self._jobs) >= self._max_jobs:
            self._evict_oldest_finished()
        job_id = str(uuid.uuid4())
        self._jobs[job_id] = Job(id=job_id, created_at=self._clock())
        return job_id

    def _evict_oldest_finished(self) -> None:
        finished = [j for j in self._jobs.values() if j.status in (JobStatus.DONE, JobStatus.ERROR)]
        if not finished:
            raise RuntimeError("job store is full and no finished jobs can be evicted")
        oldest = min(finished, key=lambda j: j.created_at)
        del self._jobs[oldest.id]

    def get(self, job_id: str) -> Job:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise JobNotFoundError(job_id) from None

    def mark_running(self, job_id: str) -> None:
        self.get(job_id).status = JobStatus.RUNNING

    def mark_done(self, job_id: str, result: Any) -> None:
        job = self.get(job_id)
        job.result = result
        job.status = JobStatus.DONE

    def mark_error(self, job_id: str, error: str) -> None:
        job = self.get(job_id)
        job.error = error
        job.status = JobStatus.ERROR
```

- [ ] **Step 4: Update `src/sebby/http/__init__.py`**

```python
"""FastAPI helpers: shared-secret auth, CORS, upload limits, threadpool file writes, job store."""

from sebby.http.auth import MissingSharedSecretEnvVarError, require_shared_secret
from sebby.http.cors import add_localhost_cors
from sebby.http.files import write_temp_file
from sebby.http.jobs import Job, JobNotFoundError, JobStatus, JobStore
from sebby.http.limits import PayloadTooLargeError, add_content_length_limit, read_capped

__all__ = [
    "Job",
    "JobNotFoundError",
    "JobStatus",
    "JobStore",
    "MissingSharedSecretEnvVarError",
    "PayloadTooLargeError",
    "add_content_length_limit",
    "add_localhost_cors",
    "read_capped",
    "require_shared_secret",
    "write_temp_file",
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/http/test_jobs.py -v`
Expected: PASS (8 passed)

- [ ] **Step 6: Run lint and type checks**

Run: `cd /Users/sebby/Developer/sebby && uv run ruff check . && uv run mypy src`
Expected: both clean

- [ ] **Step 7: Run the full suite to confirm no regressions**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest`
Expected: all previous tests plus these 8 pass

- [ ] **Step 8: Commit**

```bash
git add src/sebby/http/__init__.py src/sebby/http/jobs.py tests/http/test_jobs.py
git commit -m "feat: add sebby.http.jobs bounded async job store"
```

---

### Task 6: `sebby.notify`

**Files:**
- Create: `src/sebby/notify.py`
- Create: `tests/test_notify.py`
- Modify: `README.md` (add `sebby.notify` row to Modules)

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `escape_markdown_v2(text: str) -> str`, `send_telegram_alert(message: str, *, bot_token: str, chat_id: str, post_fn: HttpPostFn, escape: bool = True) -> None`

- [ ] **Step 1: Write the failing tests**

`tests/test_notify.py`:

```python
from __future__ import annotations

from typing import Any

from sebby.notify import escape_markdown_v2, send_telegram_alert


def test_escape_markdown_v2_escapes_all_special_chars():
    result = escape_markdown_v2("a_b*c[d](e)~f`g>h#i+j-k=l|m{n}o.p!q")

    assert result == r"a\_b\*c\[d\]\(e\)\~f\`g\>h\#i\+j\-k\=l\|m\{n\}o\.p\!q"


def test_escape_markdown_v2_leaves_plain_text_unchanged():
    assert escape_markdown_v2("hello world 123") == "hello world 123"


def test_send_telegram_alert_posts_to_correct_url_with_escaped_text():
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any]) -> None:
        calls.append({"url": url, "json": json})

    send_telegram_alert(
        "price is $5 (was $10)",
        bot_token="TOKEN123",
        chat_id="chat-1",
        post_fn=fake_post,
    )

    assert len(calls) == 1
    assert calls[0]["url"] == "https://api.telegram.org/botTOKEN123/sendMessage"
    assert calls[0]["json"]["chat_id"] == "chat-1"
    assert calls[0]["json"]["parse_mode"] == "MarkdownV2"
    assert calls[0]["json"]["text"] == escape_markdown_v2("price is $5 (was $10)")


def test_send_telegram_alert_skips_escaping_when_escape_false():
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any]) -> None:
        calls.append({"url": url, "json": json})

    send_telegram_alert(
        "raw *text*",
        bot_token="TOKEN123",
        chat_id="chat-1",
        post_fn=fake_post,
        escape=False,
    )

    assert calls[0]["json"]["text"] == "raw *text*"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/test_notify.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sebby.notify'`

- [ ] **Step 3: Implement `src/sebby/notify.py`**

```python
from __future__ import annotations

import re
from typing import Any, Protocol

_MARKDOWN_V2_SPECIAL_CHARS = r"_*[]()~`>#+-=|{}.!"


def escape_markdown_v2(text: str) -> str:
    """Escape a string for Telegram's MarkdownV2 parse mode.

    Escapes every character MarkdownV2 treats as special
    (`_*[]()~`>#+-=|{}.!`), per Telegram's Bot API documentation.
    """
    return re.sub(f"([{re.escape(_MARKDOWN_V2_SPECIAL_CHARS)}])", r"\\\1", text)


class HttpPostFn(Protocol):
    def __call__(self, url: str, *, json: dict[str, Any]) -> Any: ...


def send_telegram_alert(
    message: str,
    *,
    bot_token: str,
    chat_id: str,
    post_fn: HttpPostFn,
    escape: bool = True,
) -> None:
    """Send `message` to a Telegram chat via the Bot API's `sendMessage`
    endpoint, MarkdownV2-escaped by default.

    `post_fn` is injected rather than this module importing an HTTP client
    directly — pass e.g. `functools.partial(httpx.post, timeout=10)`. This
    keeps `sebby.notify` dependency-free and network-call-free in tests.
    """
    text = escape_markdown_v2(message) if escape else message
    post_fn(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"},
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/test_notify.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Run lint and type checks**

Run: `cd /Users/sebby/Developer/sebby && uv run ruff check . && uv run mypy src`
Expected: both clean

- [ ] **Step 6: Update `README.md`**

Add a row to the `## Modules` table:

```markdown
| `sebby.notify` | Telegram alert sending with MarkdownV2 escaping (no extra needed — the HTTP client is injected by the caller) |
```

- [ ] **Step 7: Run the full suite to confirm no regressions**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest`
Expected: all previous tests plus these 4 pass

- [ ] **Step 8: Commit**

```bash
git add src/sebby/notify.py tests/test_notify.py README.md
git commit -m "feat: add sebby.notify Telegram alerts with MarkdownV2 escaping"
```
