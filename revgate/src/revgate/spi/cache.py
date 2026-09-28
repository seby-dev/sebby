"""A content-addressed cache of index facts, one pickle file per blob, kind, and salt.

Callers pass the salt `f"{revgate.__version__}:{FACTS_VERSION}"`, so a new release or a
changed fact type never reads an old entry. Writes go through a temporary file and
`os.replace`, so concurrent writers of identical content are harmless, and a reader never
sees a half-written entry. A corrupt or truncated entry is a miss, never an exception.

The cache lives in the repository's own git state directory and holds only what revgate
wrote there; it isn't a boundary against a hostile local user.
"""

from __future__ import annotations

import os
import pickle
import re
import tempfile
from pathlib import Path

# Bump when any fact dataclass in `revgate.spi.facts` changes shape or meaning, or when the
# indexer starts recording different facts for the same source.
FACTS_VERSION = "1"

_KEY_RE = re.compile(r"^[0-9A-Za-z_-]+$")
_SALT_UNSAFE = re.compile(r"[^0-9A-Za-z_.:+-]")


class BlobCache:
    """`root / sha[:2] / f"{sha}.{kind}.{salt}.pkl"`, holding one pickled value each."""

    def __init__(self, root: Path, salt: str) -> None:
        self.root = root
        self.salt = _SALT_UNSAFE.sub("_", salt)

    def _path(self, sha: str, kind: str) -> Path:
        if not _KEY_RE.match(sha) or len(sha) < 3 or not _KEY_RE.match(kind):
            raise ValueError(f"unsafe cache key {sha!r}/{kind!r}")
        return self.root / sha[:2] / f"{sha}.{kind}.{self.salt}.pkl"

    def get(self, sha: str, kind: str) -> object | None:
        path = self._path(sha, kind)
        try:
            with path.open("rb") as fh:
                value: object = pickle.load(fh)  # noqa: S301 -- revgate's own entries only
            return value
        except FileNotFoundError:
            return None
        except Exception:  # noqa: BLE001 -- any unreadable entry is a miss by contract
            return None

    def put(self, sha: str, kind: str, value: object) -> None:
        """Store `value`; an unwritable cache directory degrades to no caching."""
        path = self._path(sha, kind)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=f".{sha}.", suffix=".tmp", dir=path.parent)
        except OSError:
            return
        replaced = False
        try:
            with os.fdopen(fd, "wb") as fh:
                pickle.dump(value, fh, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, path)
            replaced = True
        except OSError:
            return  # a full disk or a vanished directory: the next build indexes again
        finally:
            if not replaced:
                Path(tmp).unlink(missing_ok=True)
