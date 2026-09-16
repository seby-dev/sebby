from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse


class PayloadTooLargeError(Exception):
    pass


def add_content_length_limit(app: FastAPI, *, max_bytes: int) -> None:
    """Reject requests whose declared Content-Length exceeds `max_bytes`
    before the body is read at all — a cheap first line of defense.

    This does NOT protect against a request that lies about its
    Content-Length or omits it (e.g. chunked transfer encoding); pair with
    `read_capped` when actually consuming the body to close that gap —
    and catch `PayloadTooLargeError` in the endpoint (or register a
    FastAPI exception handler for it) to turn it into a clean response
    rather than an unhandled 500.

    Registration order matters: call this BEFORE `add_localhost_cors` (or
    any other middleware you want a 413 response to still carry headers
    from) — middleware registered later runs outermost, so a middleware
    registered after this one won't see this one's 413 responses.
    """

    @app.middleware("http")
    async def _content_length_limit(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                return JSONResponse({"detail": "invalid Content-Length header"}, status_code=400)
            if declared_size < 0:
                return JSONResponse({"detail": "invalid Content-Length header"}, status_code=400)
            if declared_size > max_bytes:
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
