from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi.concurrency import run_in_threadpool


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
