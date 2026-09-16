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


def test_read_capped_rejects_oversized_body_without_content_length():
    client = TestClient(_build_app())

    def body_generator():
        yield b"x" * 20

    response = client.post("/upload-streamed", content=body_generator())

    assert response.status_code == 413
