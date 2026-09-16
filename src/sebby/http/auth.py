import os
import secrets
from collections.abc import Callable
from typing import Annotated

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
