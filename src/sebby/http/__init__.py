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
