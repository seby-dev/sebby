"""FastAPI helpers: shared-secret auth, CORS, upload limits, threadpool file writes, job store."""

from sebby.http.auth import MissingSharedSecretEnvVarError, require_shared_secret
from sebby.http.cors import add_localhost_cors

__all__ = [
    "MissingSharedSecretEnvVarError",
    "add_localhost_cors",
    "require_shared_secret",
]
