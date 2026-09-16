"""FastAPI helpers: shared-secret auth, CORS, upload limits, threadpool file writes, job store."""

from sebby.http.auth import MissingSharedSecretEnvVarError, require_shared_secret

__all__ = [
    "MissingSharedSecretEnvVarError",
    "require_shared_secret",
]
