"""TypeScript facts through the repository's own `typescript` package, parse-only.

One `node ts_facts.cjs <ts_lib>` process serves every request of an index build.
Without Node or the package, `make_ts_indexer` returns `None`, and the caller records
`ts-unavailable` instead of TypeScript facts.
"""

from __future__ import annotations

import contextlib
import importlib.resources
import json
import queue
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from types import TracebackType
from typing import IO, Any

from revgate import gitio
from revgate.spi.facts import CallSite, Ref, RefKind, TsFileFacts, TsFunctionFact

REQUEST_TIMEOUT_S = 10.0

TsIndexerFn = Callable[[Mapping[str, str]], Mapping[str, TsFileFacts]]


class TsUnavailable(Exception):
    """The TypeScript process couldn't start, died, timed out, or replied out of protocol."""


def _lib_at(root: Path) -> Path | None:
    lib = root / "web" / "node_modules" / "typescript"
    return lib if (lib / "package.json").is_file() else None


def find_ts_lib(repo: Path) -> Path | None:
    """The `typescript` package under `repo/web/node_modules`, else the main worktree's."""
    own = _lib_at(repo)
    if own is not None:
        return own
    if not repo.is_dir() or gitio.toplevel(repo) is None:
        return None
    try:
        main_root = gitio.common_dir(repo).parent
    except gitio.GitError:
        return None
    if main_root == gitio.home_dir():  # the ~/.git landmine: never read from it
        return None
    return _lib_at(main_root)


def _script_path() -> Path:
    return Path(str(importlib.resources.files("revgate.spi") / "ts_facts.cjs"))


def _pump(stream: IO[str], out: queue.Queue[str | None]) -> None:
    try:
        for line in stream:
            out.put(line)
    finally:
        out.put(None)


class TsIndexer:
    """A context manager around one Node process that parses TypeScript files on request."""

    def __init__(self, ts_lib: Path, *, node: str | None = None) -> None:
        self.ts_lib = ts_lib
        self._node = node
        self._proc: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._next_id = 0
        self.version: str | None = None

    def __enter__(self) -> TsIndexer:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def start(self) -> None:
        if self._proc is not None:
            return
        node = self._node or shutil.which("node")
        if node is None:
            raise TsUnavailable("node isn't on PATH")
        try:
            self._proc = subprocess.Popen(
                [node, str(_script_path()), str(self.ts_lib)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as exc:
            raise TsUnavailable(f"cannot start node: {exc}") from exc
        assert self._proc.stdout is not None
        threading.Thread(target=_pump, args=(self._proc.stdout, self._lines), daemon=True).start()
        hello = self._read()
        if "fatal" in hello:
            self.close()
            raise TsUnavailable(str(hello["fatal"]))
        if hello.get("ready") is not True:
            self.close()
            raise TsUnavailable(f"unexpected handshake: {hello!r}")
        self.version = str(hello.get("version"))

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        with contextlib.suppress(OSError):
            if proc.stdin is not None:
                proc.stdin.close()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.kill()
            proc.wait()

    def _read(self) -> dict[str, Any]:
        try:
            line = self._lines.get(timeout=REQUEST_TIMEOUT_S)
        except queue.Empty:
            self._kill()
            raise TsUnavailable(f"no reply within {REQUEST_TIMEOUT_S:g} s") from None
        if line is None:
            self._kill()
            raise TsUnavailable("the TypeScript process exited")
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            self._kill()
            raise TsUnavailable(f"unreadable reply: {line[:200]!r}") from exc
        if not isinstance(obj, dict):
            self._kill()
            raise TsUnavailable(f"unreadable reply: {line[:200]!r}")
        return obj

    def _request(self, path: str, source: str) -> dict[str, Any]:
        self.start()
        proc = self._proc
        assert proc is not None and proc.stdin is not None
        self._next_id += 1
        rid = self._next_id
        try:
            proc.stdin.write(json.dumps({"id": rid, "path": path, "source": source}) + "\n")
            proc.stdin.flush()
        except OSError as exc:
            self._kill()
            raise TsUnavailable(f"cannot write to the TypeScript process: {exc}") from exc
        reply = self._read()
        if reply.get("id") != rid:
            self._kill()
            raise TsUnavailable(f"reply {reply.get('id')!r} doesn't answer request {rid}")
        return reply

    def facts(
        self, files: Mapping[str, str], is_test: Callable[[str], bool]
    ) -> dict[str, TsFileFacts]:
        """Facts per path, in sorted order; a file the script can't handle gets `parse_error`."""
        out: dict[str, TsFileFacts] = {}
        for path in sorted(files):
            reply = self._request(path, files[path])
            test = is_test(path)
            if "error" in reply:
                out[path] = _empty(path, test, f"indexer error: {reply['error']}")
            else:
                out[path] = _convert(path, test, reply["facts"])
        return out


def _empty(path: str, is_test: bool, error: str) -> TsFileFacts:
    return TsFileFacts(
        path=path,
        is_test=is_test,
        parse_error=error,
        imports=(),
        exports=(),
        functions=(),
        calls=(),
        refs=(),
    )


def _ref_kind(raw: object) -> RefKind:
    if raw == "attribute":
        return "attribute"
    if raw == "string":
        return "string"
    if raw == "import":
        return "import"
    return "identifier"


def _convert(path: str, is_test: bool, raw: dict[str, Any]) -> TsFileFacts:
    functions = tuple(
        TsFunctionFact(
            qualname=f"{path}::{f['name']}",
            name=str(f["name"]),
            path=path,
            lineno=int(f["line"]),
            end_lineno=int(f["end"]),
            params=tuple(str(p) for p in f["params"]),
            exported=bool(f["exported"]),
        )
        for f in raw["functions"]
    )
    calls = tuple(
        CallSite(
            caller=str(c["caller"]),
            callee=str(c["callee"]),
            path=path,
            line=int(c["line"]),
            use="other",
            in_try=bool(c["in_try"]),
            keywords=(),
        )
        for c in raw["calls"]
    )
    refs: list[Ref] = []
    seen: set[Ref] = set()
    for r in raw["refs"]:
        ref = Ref(
            path=path,
            line=int(r["line"]),
            name=str(r["name"]),
            kind=_ref_kind(r["kind"]),
            in_symbol=None if r["in_symbol"] is None else str(r["in_symbol"]),
            text=str(r.get("text") or ""),
        )
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    error = raw.get("parse_error")
    return TsFileFacts(
        path=path,
        is_test=is_test,
        parse_error=None if error is None else str(error),
        imports=tuple((str(m), str(n), str(a)) for m, n, a in raw["imports"]),
        exports=tuple(str(e) for e in raw["exports"]),
        functions=functions,
        calls=calls,
        refs=tuple(refs),
    )


def make_ts_indexer(
    repo: Path, is_test: Callable[[str], bool]
) -> tuple[TsIndexerFn | None, Callable[[], None]]:
    """An indexer function over the repository's compiler, or `None` without Node or the library.

    The Node process starts on the first call, so a build whose TypeScript facts all hit the
    cache never starts it. The function raises `TsUnavailable` if the process fails mid-build;
    call `close` when the build is done.
    """
    lib = find_ts_lib(repo)
    node = shutil.which("node")
    if lib is None or node is None:
        return None, lambda: None
    indexer = TsIndexer(lib, node=node)

    def index(files: Mapping[str, str]) -> Mapping[str, TsFileFacts]:
        return indexer.facts(files, is_test)

    return index, indexer.close
