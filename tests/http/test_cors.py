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
