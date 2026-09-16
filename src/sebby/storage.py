from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None  # type: ignore[assignment]


class AtomicJSONStore:
    """A file-backed JSON store with atomic writes.

    Writes go to a temp file in the same directory, then `os.replace` swaps
    it into place, so a reader never observes a partially-written file —
    that's the guarantee this class provides.

    This class does NOT provide cross-process mutual exclusion for
    concurrent writers. On POSIX, `write()` does take an `fcntl` lock, but
    on the throwaway `mkstemp` temp file, not on the target path — no other
    process can ever open that temp file, so the lock doesn't coordinate
    anything between processes. Two processes doing a concurrent
    read-modify-write can still lose an update. A future version may add a
    sidecar lockfile for that; this one doesn't.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self, default: Any = None) -> Any:
        if not self.path.exists():
            return default
        with self.path.open("r", encoding="utf-8") as f:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                return json.load(f)
            finally:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def write(self, data: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
