#!/usr/bin/env python3
"""A small load generator for the QA swarm, driven by `[load]` in a project's `.claude/qa.toml`.

    python3 loadgen.py --config <qa.toml> --run-dir <run folder> [--state <state file>]
                       [--seed N] [--dry-run]

It sends a weighted mix of requests to the run's own site, at a rate and concurrency the file sets
and the caps in `Caps` limit, to provoke concurrency bugs (not to size a deployment). It needs
Python 3.11 or newer and nothing outside the standard library.

It refuses, before it sends anything and with exit code 64, when:

- `schema_version` isn't 1, `[load] enabled` isn't true, or the mix is empty or malformed;
- a request's method isn't GET, HEAD, or POST;
- a target URL's host and port aren't in the run's `allowed_hosts` (`env/instance.json`), the
  host isn't a loopback address, or the port can't be read. A `path` is relative to `site_url`
  unless it starts with a scheme and `://`; an absolute URL is checked the same way and must use
  `site_url`'s scheme. The raw backend port isn't in `allowed_hosts`, and the sound hosts are for
  browser traffic only, so neither is ever a target;
- a `body_file` is absolute or outside the repository (the parent of the `.claude/` folder);
- `instance.json` names a `ca_file` that doesn't exist, or a `--state` cookie can't be sent
  as a header.

It never follows a redirect, sends the session cookie from `--state` without printing or logging
it, and stops early after `STOP_AFTER_FAILURES` connection failures in a row (safety rule 6: stop
if the instance stops responding). Once `duration + DURATION_GRACE_S` has passed it starts no
request, and it waits at most `DRAIN_TIMEOUT_S` more for the ones in flight: any still running
then is cut off (its socket shut) and counted as an error and in `cancelled`. So a run lasts at
most `duration + DURATION_GRACE_S + DRAIN_TIMEOUT_S`, plus a moment. It writes
`<run folder>/load/summary.json` and prints it.

Exit codes: 0 finished, 3 stopped early, 64 refused or a usage error (nothing was sent).
"""

from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import math
import random
import re
import socket
import ssl
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit

try:
    import tomllib
except ImportError:  # Python older than 3.11
    tomllib = None  # type: ignore[assignment]

SUPPORTED_SCHEMA = 1
METHODS = ("GET", "HEAD", "POST")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
STOP_AFTER_FAILURES = 5
DURATION_GRACE_S = 1.0
DRAIN_TIMEOUT_S = 10.0  # how long the in-flight requests may run on after the last one starts
REQUEST_TIMEOUT_S = 10.0
MAX_BODY_BYTES = 1024 * 1024
EXIT_STOPPED_EARLY = 3
EXIT_REFUSED = 64


@dataclass(frozen=True)
class Caps:
    """The hard limits. Whatever `[load]` asks for, the run uses at most these."""

    max_concurrency: int = 8
    max_rate_per_second: int = 20
    max_duration_seconds: int = 300


CAPS = Caps()


class Refused(Exception):
    """The load run must not start; the message says why and never holds a cookie."""


@dataclass(frozen=True)
class Target:
    key: str  # "GET /v1/health": the summary's row name
    method: str
    url: str
    weight: int
    body: bytes | None
    content_type: str | None


@dataclass
class Plan:
    site_url: str
    ca_file: str | None
    targets: list[Target]
    concurrency: int
    rate_per_second: int
    duration_seconds: int
    cookie: str | None = field(default=None, repr=False)  # never in a repr, a log, or a trace
    clamped: list[str] = field(default_factory=list)


def _int(section: dict[str, Any], key: str, default: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise Refused(f"[load] {key} must be a positive integer")
    return value


def _host_port(netloc: str) -> tuple[str, str]:
    """(host, host:port) of a URL's network location, lowercased, with no user info."""
    try:
        parts = urlsplit(f"//{netloc}")
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError as error:  # a port out of range or not a number, or a bad IPv6 bracket
        raise Refused(f"{netloc} doesn't have a host and port that can be read") from error
    return host, f"{host}:{port}" if port else host


def check_target(url: str, allowed_hosts: set[str]) -> None:
    """Raises Refused unless `url` is http(s) to a loopback host and a `host:port` in the
    allowlist. This is the only place a target is approved."""
    try:
        parts = urlsplit(url)
    except ValueError as error:
        raise Refused(f"{url} isn't a URL that can be read") from error
    if parts.scheme not in ("http", "https") or not parts.netloc or "@" in parts.netloc:
        raise Refused(f"{url} isn't an http or https URL for a host and port")
    host, host_port = _host_port(parts.netloc)
    if host not in LOOPBACK_HOSTS:
        raise Refused(f"{host_port} isn't a loopback address: the load run is local only")
    if host_port not in allowed_hosts:
        raise Refused(f"{host_port} isn't in this run's allowed_hosts")


_ABSOLUTE = re.compile(r"[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def _target_url(site_url: str, path: str) -> str:
    """`path` against the site, or `path` itself when it's an absolute URL (a scheme, then
    `://`, at the start: a `://` later on, in a query, say, leaves it relative)."""
    if _ABSOLUTE.match(path):
        return path
    if path.startswith("//"):
        return f"{urlsplit(site_url).scheme}:{path}"
    if not path.startswith("/"):
        raise Refused(f"path {path!r} must start with / (relative to the site) or be a full URL")
    return site_url + path


def _read_body(root: Path, name: str) -> bytes:
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise Refused(f"body_file {name!r} must be a path inside the repository")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise Refused(f"body_file {name!r} isn't a file inside {root}")
    if path.stat().st_size > MAX_BODY_BYTES:
        raise Refused(f"body_file {name!r} is larger than {MAX_BODY_BYTES} bytes")
    return path.read_bytes()


_COOKIE_NAME = re.compile(r"[A-Za-z0-9_.!#$%&'*+^`|~-]+")
_COOKIE_VALUE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]*")  # RFC 6265 octets


def _cookie_header(state_file: Path, host: str) -> str:
    """The `Cookie` header value for `host` from a Playwright storageState file. The value is
    never printed: a failure names the file only, and a cookie whose name or value a header
    can't hold (a line break, say) is refused without being echoed."""
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        pairs = [
            (str(c["name"]), str(c["value"]))
            for c in state["cookies"]
            if str(c.get("domain", "")).lstrip(".").lower() == host
        ]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise Refused(f"can't read cookies from {state_file}") from error
    for name, value in pairs:
        if not _COOKIE_NAME.fullmatch(name) or not _COOKIE_VALUE.fullmatch(value):
            raise Refused(
                f"a cookie in {state_file} has a name or value a Cookie header can't hold"
            )
    return "; ".join(f"{name}={value}" for name, value in pairs)


def build_plan(config: Path, run_dir: Path, state_file: Path | None, caps: Caps = CAPS) -> Plan:
    """Validates everything a run needs and returns it, or raises Refused. Sends nothing."""
    if tomllib is None:
        raise Refused("loadgen.py needs Python 3.11 or newer (tomllib)")
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise Refused(f"can't read {config}: {error}") from error
    if data.get("schema_version") != SUPPORTED_SCHEMA:
        raise Refused(f"qa.toml schema_version must be {SUPPORTED_SCHEMA}")
    load = data.get("load")
    if not isinstance(load, dict) or load.get("enabled") is not True:
        raise Refused("[load] enabled isn't true: load is off unless the user asks")
    try:
        instance = json.loads((run_dir / "env" / "instance.json").read_text(encoding="utf-8"))
        site_url = str(instance["site_url"]).rstrip("/")
        allowed = {str(host).lower() for host in instance["allowed_hosts"]}
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Refused(f"can't read env/instance.json under {run_dir}") from error
    check_target(site_url, allowed)
    root = config.resolve().parent.parent  # the repository: the parent of `.claude/`
    entries = load.get("requests")
    if not isinstance(entries, list) or not entries:
        raise Refused("[load] needs at least one [[load.requests]] entry")
    targets = [_target(entry, i, site_url, allowed, root) for i, entry in enumerate(entries)]
    ca_file = instance.get("ca_file")
    if ca_file is not None and not (isinstance(ca_file, str) and Path(ca_file).is_file()):
        raise Refused("env/instance.json names a ca_file that doesn't exist")
    plan = Plan(
        site_url=site_url,
        ca_file=ca_file,
        targets=targets,
        concurrency=_int(load, "concurrency", 4),
        rate_per_second=_int(load, "rate_per_second", 10),
        duration_seconds=_int(load, "duration_seconds", 60),
    )
    for name, cap in (
        ("concurrency", caps.max_concurrency),
        ("rate_per_second", caps.max_rate_per_second),
        ("duration_seconds", caps.max_duration_seconds),
    ):
        if getattr(plan, name) > cap:
            plan.clamped.append(f"{name} {getattr(plan, name)} -> {cap}")
            setattr(plan, name, cap)
    if state_file is not None:
        host, _ = _host_port(urlsplit(site_url).netloc)
        plan.cookie = _cookie_header(state_file, host) or None
    return plan


def _target(entry: object, index: int, site_url: str, allowed: set[str], root: Path) -> Target:
    where = f"[[load.requests]] #{index + 1}"
    if not isinstance(entry, dict):
        raise Refused(f"{where} must be a table")
    method = str(entry.get("method", "")).upper()
    if method not in METHODS:
        raise Refused(f"{where} method must be one of {', '.join(METHODS)}")
    path = entry.get("path")
    weight = entry.get("weight")
    if not isinstance(path, str) or not path.strip():
        raise Refused(f"{where} path must be a non-empty string")
    if isinstance(weight, bool) or not isinstance(weight, int) or weight <= 0:
        raise Refused(f"{where} weight must be a positive integer")
    url = _target_url(site_url, path)
    check_target(url, allowed)
    if urlsplit(url).scheme != urlsplit(site_url).scheme:
        raise Refused(f"{where} {url} must use the site's scheme, {urlsplit(site_url).scheme}")
    body_name = entry.get("body_file")
    body = _read_body(root, body_name) if isinstance(body_name, str) else None
    content_type = entry.get("content_type")
    if body is not None and not isinstance(content_type, str):
        content_type = "application/json"
    return Target(
        key=f"{method} {path}",
        method=method,
        url=url,
        weight=weight,
        body=body,
        content_type=content_type if isinstance(content_type, str) else None,
    )


# -- sending ----------------------------------------------------------------------------------


@dataclass
class Outcome:
    key: str
    status: int | None  # None: no response (a connection failure)
    ms: float


class _Live:
    """The sockets in flight, so a run past its drain timeout can shut them. (The socket itself,
    not the connection: `http.client` hands it to the response and drops it from the connection.)
    A request adds its socket, then checks `aborted`; `abort` sets `aborted`, then reads the set,
    so no socket slips past both."""

    def __init__(self) -> None:
        self.sockets: set[socket.socket] = set()
        self.aborted = threading.Event()
        self.lock = threading.Lock()

    def add(self, sock: socket.socket) -> None:
        with self.lock:
            self.sockets.add(sock)
        if self.aborted.is_set():
            raise OSError("the run's drain timeout passed")

    def discard(self, sock: socket.socket) -> None:
        with self.lock:
            self.sockets.discard(sock)

    def abort(self) -> None:
        self.aborted.set()
        with self.lock:
            sockets = list(self.sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)  # wakes a blocked read, which then fails
            except OSError:
                pass


def _send(
    target: Target, plan: Plan, context: ssl.SSLContext | None, live: _Live | None = None
) -> Outcome:
    """One request on its own connection, with no redirect followed."""
    parts = urlsplit(target.url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    headers = {"User-Agent": "qa-swarm-loadgen"}
    if plan.cookie:
        headers["Cookie"] = plan.cookie
    if target.body is not None:
        headers["Content-Type"] = target.content_type or "application/json"
    begin = time.monotonic()
    connection: http.client.HTTPConnection
    if parts.scheme == "https":
        connection = http.client.HTTPSConnection(
            parts.hostname or "", parts.port, timeout=REQUEST_TIMEOUT_S, context=context
        )
    else:
        connection = http.client.HTTPConnection(
            parts.hostname or "", parts.port, timeout=REQUEST_TIMEOUT_S
        )
    sock: socket.socket | None = None
    try:
        connection.connect()
        sock = connection.sock
        if live is not None and sock is not None:
            live.add(sock)
        connection.request(target.method, path, body=target.body, headers=headers)
        response = connection.getresponse()
        response.read()
        status: int | None = response.status
    except (OSError, ValueError, http.client.HTTPException):  # ValueError: a bad header
        status = None
    finally:
        if live is not None and sock is not None:
            live.discard(sock)
        connection.close()
    return Outcome(target.key, status, (time.monotonic() - begin) * 1000.0)


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile; 0.0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(len(ordered) * fraction))
    return round(ordered[rank - 1], 2)


def _row(outcomes: list[Outcome]) -> dict[str, Any]:
    times = [o.ms for o in outcomes if o.status is not None]
    return {
        "count": len(outcomes),
        "errors": sum(1 for o in outcomes if o.status is None),
        "status": dict(sorted(Counter(str(o.status) for o in outcomes if o.status).items())),
        "p50_ms": _percentile(times, 0.50),
        "p95_ms": _percentile(times, 0.95),
    }


async def run_load(plan: Plan, seed: int) -> dict[str, Any]:
    """Sends the mix and returns the summary."""
    rng = random.Random(seed)
    total = plan.rate_per_second * plan.duration_seconds
    picks = rng.choices(range(len(plan.targets)), weights=[t.weight for t in plan.targets], k=total)
    context = ssl.create_default_context(cafile=plan.ca_file) if plan.ca_file else None
    slots = asyncio.Semaphore(plan.concurrency)
    stop = asyncio.Event()
    outcomes: list[Outcome] = []
    live = _Live()
    streak = 0

    async def one(target: Target) -> None:
        nonlocal streak
        try:
            outcome = await asyncio.to_thread(_send, target, plan, context, live)
        finally:
            slots.release()
        outcomes.append(outcome)
        streak = streak + 1 if outcome.status is None else 0
        if streak >= STOP_AFTER_FAILURES:
            stop.set()

    began = time.monotonic()
    last_start = began + plan.duration_seconds + DURATION_GRACE_S
    tasks: dict[asyncio.Task[None], Target] = {}
    for index, pick in enumerate(picks):
        wait = began + index / plan.rate_per_second - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        # The plan holds rate * duration requests, paced one every 1/rate seconds. The clock only
        # stops a run that has fallen far behind (a slow instance keeps every slot busy).
        if stop.is_set() or time.monotonic() >= last_start:
            break
        try:  # every slot may be held by a stuck request: wait no later than the last start
            await asyncio.wait_for(slots.acquire(), max(0.0, last_start - time.monotonic()))
        except TimeoutError:
            break
        if stop.is_set():
            slots.release()
            break
        target = plan.targets[pick]
        tasks[asyncio.create_task(one(target))] = target
    cancelled = await _drain(tasks, live, outcomes)
    elapsed = time.monotonic() - began
    by_key: dict[str, list[Outcome]] = {t.key: [] for t in plan.targets}
    for outcome in outcomes:
        by_key[outcome.key].append(outcome)
    return {
        "schema": 1,
        "started": datetime.now(timezone.utc).isoformat(),  # noqa: UP017 (UTC needs 3.11)
        "site_url": plan.site_url,
        "seed": seed,
        "effective": {
            "concurrency": plan.concurrency,
            "rate_per_second": plan.rate_per_second,
            "duration_seconds": plan.duration_seconds,
            "clamped": plan.clamped,
        },
        "session": "cookie from --state" if plan.cookie else "none",
        "requests": {key: _row(rows) for key, rows in by_key.items()},
        "total": _row(outcomes),
        "cancelled": cancelled,
        "elapsed_s": round(elapsed, 2),
        "stopped_early": stop.is_set(),
        "stop_reason": (
            f"{STOP_AFTER_FAILURES} connection failures in a row: the instance stopped responding"
            if stop.is_set()
            else None
        ),
    }


async def _drain(
    tasks: dict[asyncio.Task[None], Target], live: _Live, outcomes: list[Outcome]
) -> int:
    """Waits up to `DRAIN_TIMEOUT_S` for the requests in flight, then cuts off the rest, records
    each as a connection failure, and returns how many it cut off."""
    if not tasks:
        return 0
    began = time.monotonic()
    _, pending = await asyncio.wait(tasks, timeout=DRAIN_TIMEOUT_S)
    if not pending:
        return 0
    live.abort()
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    ms = (time.monotonic() - began) * 1000.0
    outcomes.extend(Outcome(tasks[task].key, None, ms) for task in pending)
    return len(pending)


def plan_summary(plan: Plan, caps: Caps) -> dict[str, Any]:
    """What a dry run prints: the effective limits and every resolved target (no cookie)."""
    return {
        "dry_run": True,
        "site_url": plan.site_url,
        "caps": {
            "max_concurrency": caps.max_concurrency,
            "max_rate_per_second": caps.max_rate_per_second,
            "max_duration_seconds": caps.max_duration_seconds,
        },
        "effective": {
            "concurrency": plan.concurrency,
            "rate_per_second": plan.rate_per_second,
            "duration_seconds": plan.duration_seconds,
            "clamped": plan.clamped,
        },
        "session": "cookie from --state" if plan.cookie else "none",
        "targets": [
            {"request": t.key, "url": t.url, "weight": t.weight, "body_bytes": len(t.body or b"")}
            for t in plan.targets
        ],
    }


class _Parser(argparse.ArgumentParser):
    """argparse, but a usage error exits with EXIT_REFUSED (64) rather than 2."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_REFUSED, f"{self.prog}: error: {message}\n")


def main(argv: list[str] | None = None, caps: Caps = CAPS) -> int:
    parser = _Parser(prog="loadgen.py", description=__doc__.split("\n")[0])
    parser.add_argument("--config", type=Path, required=True, help="the project's qa.toml")
    parser.add_argument("--run-dir", type=Path, required=True, help="the run folder")
    parser.add_argument("--state", type=Path, default=None, help="a storageState file to sign in")
    parser.add_argument("--seed", type=int, default=0, help="seeds the request mix")
    parser.add_argument("--dry-run", action="store_true", help="check everything, send nothing")
    args = parser.parse_args(argv)
    try:
        plan = build_plan(args.config, args.run_dir, args.state, caps)
    except Refused as error:
        print(f"loadgen: refused: {error}", file=sys.stderr)
        return EXIT_REFUSED
    if args.dry_run:
        print(json.dumps(plan_summary(plan, caps), indent=2))
        return 0
    summary = asyncio.run(run_load(plan, args.seed))
    folder = args.run_dir / "load"
    folder.mkdir(parents=True, exist_ok=True)
    text = json.dumps(summary, indent=2)
    (folder / "summary.json").write_text(text + "\n", encoding="utf-8")
    print(text)
    return EXIT_STOPPED_EARLY if summary["stopped_early"] else 0


if __name__ == "__main__":
    sys.exit(main())
