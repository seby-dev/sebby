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
