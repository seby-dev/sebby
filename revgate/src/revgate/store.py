"""State directory and file primitives, shared by every worktree of a repository."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping
from pathlib import Path

from revgate.gitio import common_dir
from revgate.model import RunFile, run_file_from_json, run_file_to_json


def state_dir(top: Path) -> Path:
    d = common_dir(top) / "revgate"
    d.mkdir(parents=True, exist_ok=True)
    return d


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl_locked(path: Path, obj: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(line)
            fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            value = json.loads(raw)
            if isinstance(value, dict):
                rows.append(value)
    return rows


# --- run files, the ledger, and the run cache (Stage 1a) ---------------------------------

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


def canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _component(value: str, what: str) -> str:
    if not _SAFE_COMPONENT.match(value) or ".." in value:
        raise ValueError(f"{what} {value!r} can't be used in a state-directory path")
    return value


def _run_dir(state: Path, task: str | None) -> Path:
    return state / "runs" / _component(task if task is not None else "adhoc", "task")


def run_file_path(state: Path, task: str | None, head: str, role: str) -> Path:
    """`runs/<task or "adhoc">/<head>.<role>.json` in the state directory."""
    name = f"{_component(head, 'head')}.{_component(role, 'role')}.json"
    return _run_dir(state, task) / name


def write_run_file(state: Path, rf: RunFile) -> Path:
    path = run_file_path(state, rf.task, rf.head, rf.role)
    atomic_write_text(path, run_file_to_json(rf))
    return path


def read_run_file(path: Path) -> RunFile:
    return run_file_from_json(path.read_text(encoding="utf-8"))


def write_meta(
    state: Path, task: str | None, head: str, role: str, meta: Mapping[str, object]
) -> Path:
    """The run's sidecar for timings and gate detail, which the run file never holds."""
    path = run_file_path(state, task, head, role).with_suffix(".meta.json")
    atomic_write_text(path, canonical_json(dict(meta)))
    return path


def _ledger_path(state: Path, plan_slug: str) -> Path:
    return state / "ledger" / f"{_component(plan_slug, 'plan slug')}.jsonl"


def ledger_append(state: Path, plan_slug: str, entry: Mapping[str, object]) -> None:
    append_jsonl_locked(_ledger_path(state, plan_slug), entry)


def ledger_entries(state: Path, plan_slug: str) -> list[dict[str, object]]:
    return read_jsonl(_ledger_path(state, plan_slug))


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def ledger_rulings(
    state: Path, plan_slug: str, task_id: str | None
) -> frozenset[tuple[str, str, str | None]]:
    """`(rule, cluster_key, witness_digest)` of every `explained` ruling for the task."""
    out: set[tuple[str, str, str | None]] = set()
    for e in ledger_entries(state, plan_slug):
        if e.get("kind") != "ruling" or e.get("task") != task_id:
            continue
        if e.get("verdict") != "explained":
            continue
        rule, key = e.get("rule"), e.get("cluster_key")
        if isinstance(rule, str) and isinstance(key, str):
            out.add((rule, key, _opt_str(e.get("witness_digest"))))
    return frozenset(out)


def ledger_assigned(state: Path, plan_slug: str, task_id: str | None) -> frozenset[str]:
    """Paths a ledger `assign` entry gave the task, which W0 accepts as in scope."""
    return frozenset(
        path
        for e in ledger_entries(state, plan_slug)
        if e.get("kind") == "assign"
        and e.get("task") == task_id
        and isinstance(path := e.get("path"), str)
    )


def latest_controller_runs(state: Path, plan_slug: str) -> dict[str, dict[str, object]]:
    """Per task, the last `run` entry whose role is `controller`."""
    latest: dict[str, dict[str, object]] = {}
    for e in ledger_entries(state, plan_slug):
        task = e.get("task")
        if e.get("kind") == "run" and e.get("role") == "controller" and isinstance(task, str):
            latest[task] = e
    return latest


def _run_cache_path(state: Path, key: str) -> Path:
    return state / "results" / "runs" / f"{hashlib.sha256(key.encode()).hexdigest()}.json"


def run_cache_get(state: Path, key: str) -> str | None:
    path = _run_cache_path(state, key)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def run_cache_put(state: Path, key: str, payload: str) -> None:
    atomic_write_text(_run_cache_path(state, key), payload)


def findings_log_path(state: Path) -> Path:
    return state / "findings.jsonl"
