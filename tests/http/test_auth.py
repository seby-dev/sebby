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
