"""loadgen.py: the QA swarm's load generator, against a local HTTP server."""

from __future__ import annotations

import ast
import http.server
import importlib.util
import json
import shutil
import ssl
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "claude" / "skills" / "qa-swarm" / "loadgen.py"
COOKIE = "SECRETCOOKIE-0123456789"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("qa_loadgen", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["qa_loadgen"] = module
    spec.loader.exec_module(module)
    return module


loadgen = _load_module()


class Site:
    """A loopback server that records every request and the most in flight at once."""

    def __init__(
        self,
        delay: float = 0.0,
        status: dict[str, int] | None = None,
        location: str = "http://127.0.0.1:1/elsewhere",
    ) -> None:
        self.requests: list[dict[str, object]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.lock = threading.Lock()
        site = self
        codes = status or {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with site.lock:
                    site.in_flight += 1
                    site.max_in_flight = max(site.max_in_flight, site.in_flight)
                    site.requests.append(
                        {
                            "method": self.command,
                            "path": self.path,
                            "cookie": self.headers.get("Cookie"),
                            "content_type": self.headers.get("Content-Type"),
                            "body": body,
                            "at": time.monotonic(),
                        }
                    )
                if delay:
                    time.sleep(delay)
                code = codes.get(self.path, 200)
                self.send_response(code)
                if code == 302:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
                with site.lock:
                    site.in_flight -= 1

            do_GET = do_POST = do_HEAD = _handle  # noqa: N815

            def log_message(self, *args: object) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def host_port(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def url(self) -> str:
        return f"http://{self.host_port}"

    def paths(self) -> Counter[str]:
        return Counter(str(r["path"]) for r in self.requests)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def site() -> Iterator[Site]:
    server = Site()
    yield server
    server.stop()


def make_run(
    tmp_path: Path,
    site_url: str,
    *,
    allowed: list[str] | None = None,
    load: str | None = None,
    schema: int = 1,
    ca_file: str | None = None,
) -> tuple[Path, Path]:
    """A repository with `.claude/qa.toml` and a run folder with env/instance.json."""
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True, exist_ok=True)
    (repo / "fixtures").mkdir(exist_ok=True)
    (repo / "fixtures" / "body.json").write_text('{"solfa_text": "d"}')
    body = load if load is not None else LOAD
    config = repo / ".claude" / "qa.toml"
    config.write_text(f"schema_version = {schema}\n{body}")
    run = tmp_path / "qa-run"
    (run / "env").mkdir(parents=True, exist_ok=True)
    host_port = site_url.split("//", 1)[1]
    info: dict[str, object] = {
        "site_url": site_url,
        "allowed_hosts": allowed if allowed is not None else [host_port, "gleitz.github.io"],
    }
    if ca_file:
        info["ca_file"] = ca_file
    (run / "env" / "instance.json").write_text(json.dumps(info))
    return config, run


LOAD = """
[load]
enabled = true
concurrency = 4
rate_per_second = 20
duration_seconds = 1

[[load.requests]]
method = "GET"
path = "/health"
weight = 1

[[load.requests]]
method = "POST"
path = "/parse"
weight = 3
body_file = "fixtures/body.json"
"""


def cli(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=120,
    )


def state_file(tmp_path: Path, *, domain: str = "127.0.0.1") -> Path:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "s2s_session", "value": COOKIE, "domain": domain, "path": "/"},
                    {"name": "other", "value": "OTHERSITE", "domain": "example.com", "path": "/"},
                ],
                "origins": [
                    {
                        "origin": "http://127.0.0.1",
                        "localStorage": [{"name": "staff2solfa_signed_in_as", "value": "LSVALUE"}],
                    }
                ],
            }
        )
    )
    return path


# -- what it sends ----------------------------------------------------------------------------


def test_the_mix_follows_the_weights_and_the_seed(tmp_path: Path, site: Site) -> None:
    load = LOAD.replace("rate_per_second = 20", "rate_per_second = 100").replace(
        "duration_seconds = 1", "duration_seconds = 2"
    )
    config, run = make_run(tmp_path, site.url, load=load)
    caps = loadgen.Caps(max_concurrency=8, max_rate_per_second=100, max_duration_seconds=5)
    assert loadgen.main(["--config", str(config), "--run-dir", str(run), "--seed", "7"], caps) == 0
    first = site.paths()
    assert sum(first.values()) == 200
    assert 0.15 <= first["/health"] / 200 <= 0.35  # weight 1 of 4
    assert 0.65 <= first["/parse"] / 200 <= 0.85  # weight 3 of 4
    site.requests.clear()
    loadgen.main(["--config", str(config), "--run-dir", str(run), "--seed", "7"], caps)
    assert site.paths() == first  # the same seed picks the same mix
    site.requests.clear()
    loadgen.main(["--config", str(config), "--run-dir", str(run), "--seed", "8"], caps)
    assert site.paths() != first


def test_a_post_sends_its_body_file_and_content_type(tmp_path: Path, site: Site) -> None:
    config, run = make_run(tmp_path, site.url)
    assert cli("--config", config, "--run-dir", run).returncode == 0
    posts = [r for r in site.requests if r["method"] == "POST"]
    assert posts and all(r["body"] == b'{"solfa_text": "d"}' for r in posts)
    assert all(r["content_type"] == "application/json" for r in posts)
    assert all(r["body"] == b"" for r in site.requests if r["method"] == "GET")


def test_the_rate_and_concurrency_caps_hold_whatever_the_file_asks(tmp_path: Path) -> None:
    slow = Site(delay=0.15)
    try:
        load = LOAD.replace("concurrency = 4", "concurrency = 100").replace(
            "rate_per_second = 20", "rate_per_second = 5000"
        )
        config, run = make_run(tmp_path, slow.url, load=load)
        began = time.monotonic()
        result = cli("--config", config, "--run-dir", run)
        elapsed = time.monotonic() - began
    finally:
        slow.stop()
    assert result.returncode == 0, result.stderr
    summary = json.loads((run / "load" / "summary.json").read_text())
    assert (
        summary["effective"]["concurrency"] == 8 and summary["effective"]["rate_per_second"] == 20
    )
    assert summary["effective"]["clamped"] == [
        "concurrency 100 -> 8",
        "rate_per_second 5000 -> 20",
    ]
    assert len(slow.requests) <= 20 * 1  # rate * duration, not 5000
    assert slow.max_in_flight <= 8
    assert elapsed < 30


def test_the_duration_cap_stops_a_long_run(tmp_path: Path, site: Site) -> None:
    load = LOAD.replace("duration_seconds = 1", "duration_seconds = 100").replace(
        "rate_per_second = 20", "rate_per_second = 40"
    )
    config, run = make_run(tmp_path, site.url, load=load)
    caps = loadgen.Caps(max_concurrency=2, max_rate_per_second=40, max_duration_seconds=1)
    began = time.monotonic()
    assert loadgen.main(["--config", str(config), "--run-dir", str(run)], caps) == 0
    assert time.monotonic() - began <= 1 + loadgen.DURATION_GRACE_S + 1.5  # the cap, plus slack
    assert len(site.requests) <= 40 * 1
    times = [float(str(r["at"])) for r in site.requests]
    assert max(times) - min(times) <= 1.0 + 0.3  # no request lands after the duration cap
    summary = json.loads((run / "load" / "summary.json").read_text())
    assert summary["effective"]["duration_seconds"] == 1
    assert "duration_seconds 100 -> 1" in summary["effective"]["clamped"]


def test_no_one_second_window_holds_more_requests_than_the_rate_cap(
    tmp_path: Path, site: Site
) -> None:
    load = LOAD.replace("rate_per_second = 20", "rate_per_second = 500").replace(
        "duration_seconds = 1", "duration_seconds = 3"
    )
    config, run = make_run(tmp_path, site.url, load=load)
    caps = loadgen.Caps(max_concurrency=8, max_rate_per_second=10, max_duration_seconds=3)
    assert loadgen.main(["--config", str(config), "--run-dir", str(run)], caps) == 0
    times = sorted(float(str(r["at"])) for r in site.requests)
    assert len(times) == 30  # rate 10 (clamped from 500) for 3 seconds
    assert times[-1] - times[0] >= 2.7  # paced: 30 requests spread over about 2.9 seconds
    worst = max(sum(1 for t in times if start <= t < start + 1.0) for start in times)
    assert worst <= 10 + 1  # the cap, plus one for a request on the window's edge


def test_the_concurrency_never_passes_the_effective_cap(tmp_path: Path) -> None:
    slow = Site(delay=0.1)
    try:
        load = LOAD.replace("concurrency = 4", "concurrency = 2")
        config, run = make_run(tmp_path, slow.url, load=load)
        assert cli("--config", config, "--run-dir", run).returncode == 0
    finally:
        slow.stop()
    assert 1 <= slow.max_in_flight <= 2


def test_the_summary_counts_status_codes_and_latency_per_request(
    tmp_path: Path, site: Site
) -> None:
    config, run = make_run(tmp_path, site.url)
    result = cli("--config", config, "--run-dir", run)
    printed = json.loads(result.stdout)
    assert printed == json.loads((run / "load" / "summary.json").read_text())
    total = printed["total"]
    assert total["count"] == 20 and total["errors"] == 0 and total["status"] == {"200": 20}
    assert 0 < total["p50_ms"] <= total["p95_ms"]
    rows = printed["requests"]
    assert set(rows) == {"GET /health", "POST /parse"}
    assert sum(row["count"] for row in rows.values()) == 20
    assert printed["stopped_early"] is False and printed["stop_reason"] is None


def test_an_http_error_is_a_status_not_a_connection_failure(tmp_path: Path) -> None:
    failing = Site(status={"/health": 500, "/parse": 500})
    try:
        config, run = make_run(tmp_path, failing.url)
        result = cli("--config", config, "--run-dir", run)
    finally:
        failing.stop()
    assert result.returncode == 0  # a busy app isn't a dead one
    summary = json.loads(result.stdout)
    assert summary["total"]["status"] == {"500": 20} and summary["stopped_early"] is False


def test_it_never_follows_a_redirect(tmp_path: Path) -> None:
    elsewhere = Site()  # the redirect target: a request here would show a followed redirect
    redirecting = Site(
        status={"/health": 302, "/parse": 302}, location=f"{elsewhere.url}/elsewhere"
    )
    try:
        config, run = make_run(tmp_path, redirecting.url)
        assert cli("--config", config, "--run-dir", run).returncode == 0
    finally:
        redirecting.stop()
        elsewhere.stop()
    assert elsewhere.requests == []
    assert json.loads((run / "load" / "summary.json").read_text())["total"]["status"] == {"302": 20}


# -- the cookie -------------------------------------------------------------------------------


def test_the_session_cookie_is_sent_and_never_printed_or_written(
    tmp_path: Path, site: Site
) -> None:
    config, run = make_run(tmp_path, site.url)
    result = cli("--config", config, "--run-dir", run, "--state", state_file(tmp_path))
    assert result.returncode == 0, result.stderr
    assert all(r["cookie"] == f"s2s_session={COOKIE}" for r in site.requests)  # only this site's
    everything = result.stdout + result.stderr + (run / "load" / "summary.json").read_text()
    for path in run.rglob("*"):
        if path.is_file():
            everything += path.read_text()
    assert (
        COOKIE not in everything and "OTHERSITE" not in everything and "LSVALUE" not in everything
    )
    assert json.loads(result.stdout)["session"] == "cookie from --state"


def test_a_cookie_for_another_domain_is_not_sent(tmp_path: Path, site: Site) -> None:
    config, run = make_run(tmp_path, site.url)
    state = state_file(tmp_path, domain="example.com")
    assert cli("--config", config, "--run-dir", run, "--state", state).returncode == 0
    assert all(r["cookie"] is None for r in site.requests)
    assert json.loads((run / "load" / "summary.json").read_text())["session"] == "none"


def test_an_unreadable_state_file_is_refused_without_naming_a_value(
    tmp_path: Path, site: Site
) -> None:
    config, run = make_run(tmp_path, site.url)
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    result = cli("--config", config, "--run-dir", run, "--state", bad)
    assert result.returncode == 64 and "bad.json" in result.stderr and site.requests == []


# -- refusals: nothing is sent ----------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "http://example.com/x",
        "https://gleitz.github.io/sounds",  # allowed for the browser only, and not loopback
        "//example.com/x",
        "http://127.0.0.1:1/other-port",  # a loopback port that isn't in allowed_hosts
        "http://user@127.0.0.1:1/x",
        "ftp://127.0.0.1/x",
        "health",  # neither relative to the site nor a URL
    ],
)
def test_a_target_outside_the_allowlist_is_refused_before_any_request(
    tmp_path: Path, site: Site, path: str
) -> None:
    load = LOAD.replace('path = "/health"', f'path = "{path}"')
    config, run = make_run(tmp_path, site.url, load=load)
    result = cli("--config", config, "--run-dir", run)
    assert result.returncode == 64 and "refused" in result.stderr
    assert site.requests == [] and not (run / "load").exists()


def test_an_absolute_url_whose_origin_is_allowed_is_accepted(tmp_path: Path, site: Site) -> None:
    load = LOAD.replace(
        'path = "/health"\nweight = 1', f'path = "{site.url}/absolute"\nweight = 99'
    )
    config, run = make_run(tmp_path, site.url, load=load)
    assert cli("--config", config, "--run-dir", run).returncode == 0
    assert site.paths()["/absolute"] > 10


def test_a_non_loopback_host_is_refused_even_when_allowed_hosts_lists_it(
    tmp_path: Path, site: Site
) -> None:
    config, run = make_run(tmp_path, "http://example.com:8080", allowed=["example.com:8080"])
    result = cli("--config", config, "--run-dir", run)
    assert result.returncode == 64 and "loopback" in result.stderr


def test_a_site_url_outside_allowed_hosts_is_refused(tmp_path: Path, site: Site) -> None:
    config, run = make_run(tmp_path, site.url, allowed=["127.0.0.1:1"])
    assert cli("--config", config, "--run-dir", run).returncode == 64
    assert site.requests == []


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("enabled = true", "enabled = false", "enabled"),
        ("enabled = true", 'enabled = "true"', "enabled"),
        ('method = "GET"', 'method = "DELETE"', "method"),
        ('method = "GET"', 'method = "PUT"', "method"),
        ("weight = 1", "weight = 0", "weight"),
        ("concurrency = 4", "concurrency = 0", "concurrency"),
        ('body_file = "fixtures/body.json"', 'body_file = "/etc/passwd"', "inside the repository"),
        ('body_file = "fixtures/body.json"', 'body_file = "../qa-run/env/instance.json"', "inside"),
        ('body_file = "fixtures/body.json"', 'body_file = "fixtures/missing.json"', "isn't a file"),
    ],
)
def test_a_bad_load_section_is_refused_before_any_request(
    tmp_path: Path, site: Site, old: str, new: str, message: str
) -> None:
    config, run = make_run(tmp_path, site.url, load=LOAD.replace(old, new))
    result = cli("--config", config, "--run-dir", run)
    assert result.returncode == 64 and message in result.stderr
    assert site.requests == []


def test_a_section_with_no_mix_a_missing_section_or_a_bad_schema_is_refused(
    tmp_path: Path, site: Site
) -> None:
    no_mix = "[load]\nenabled = true\n"
    config, run = make_run(tmp_path, site.url, load=no_mix)
    assert cli("--config", config, "--run-dir", run).returncode == 64
    config, run = make_run(tmp_path, site.url, load="[env]\nstart = 'x'\n")
    assert cli("--config", config, "--run-dir", run).returncode == 64
    config, run = make_run(tmp_path, site.url, schema=2)
    result = cli("--config", config, "--run-dir", run)
    assert result.returncode == 64 and "schema_version" in result.stderr
    assert site.requests == []


def test_a_missing_instance_file_or_config_is_refused(tmp_path: Path, site: Site) -> None:
    config, run = make_run(tmp_path, site.url)
    (run / "env" / "instance.json").unlink()
    assert cli("--config", config, "--run-dir", run).returncode == 64
    assert cli("--config", tmp_path / "nope.toml", "--run-dir", run).returncode == 64
    assert site.requests == []


# -- dry run and early stop -------------------------------------------------------------------


def test_a_cookie_a_header_cant_hold_is_refused_without_echoing_it(
    tmp_path: Path, site: Site
) -> None:
    config, run = make_run(tmp_path, site.url)
    state = tmp_path / "evil.json"
    state.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "s2s_session",
                        "value": "LEAKME\r\nX-Injected: 1",
                        "domain": "127.0.0.1",
                        "path": "/",
                    }
                ]
            }
        )
    )
    result = cli("--config", config, "--run-dir", run, "--state", state)
    assert result.returncode == 64 and "evil.json" in result.stderr
    assert "LEAKME" not in result.stdout + result.stderr and site.requests == []


def test_a_ca_file_that_doesnt_exist_is_refused(tmp_path: Path, site: Site) -> None:
    config, run = make_run(tmp_path, site.url, ca_file=str(tmp_path / "no-such-root.crt"))
    result = cli("--config", config, "--run-dir", run)
    assert result.returncode == 64 and "ca_file" in result.stderr and site.requests == []


def test_without_tomllib_it_refuses_with_exit_64_instead_of_crashing(
    tmp_path: Path, site: Site
) -> None:
    config, run = make_run(tmp_path, site.url)
    code = (
        "import runpy, sys; sys.modules['tomllib'] = None; "
        f"sys.argv = ['loadgen.py', '--config', {str(config)!r}, '--run-dir', {str(run)!r}]; "
        f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 64 and "Python 3.11 or newer" in result.stderr
    assert site.requests == []


def test_a_dry_run_checks_everything_prints_the_plan_and_sends_nothing(
    tmp_path: Path, site: Site
) -> None:
    config, run = make_run(tmp_path, site.url)
    result = cli("--config", config, "--run-dir", run, "--state", state_file(tmp_path), "--dry-run")
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["dry_run"] is True and plan["caps"]["max_concurrency"] == 8
    assert [t["request"] for t in plan["targets"]] == ["GET /health", "POST /parse"]
    assert plan["targets"][1]["body_bytes"] == len('{"solfa_text": "d"}')
    assert COOKIE not in result.stdout
    assert site.requests == [] and not (run / "load").exists()
    bad = make_run(
        tmp_path / "bad", site.url, load=LOAD.replace("enabled = true", "enabled = false")
    )
    assert cli("--config", bad[0], "--run-dir", bad[1], "--dry-run").returncode == 64


def test_it_stops_early_after_consecutive_connection_failures(tmp_path: Path) -> None:
    dead = Site()
    dead.stop()  # nothing listens on its port any more
    config, run = make_run(
        tmp_path, dead.url, load=LOAD.replace("concurrency = 4", "concurrency = 1")
    )
    result = cli("--config", config, "--run-dir", run)
    assert result.returncode == 3
    summary = json.loads(result.stdout)
    assert summary["stopped_early"] is True and "stopped responding" in summary["stop_reason"]
    total = summary["total"]
    assert total["errors"] == total["count"] == loadgen.STOP_AFTER_FAILURES
    assert (run / "load" / "summary.json").is_file()


def test_the_early_stop_threshold_is_five_connection_failures_in_a_row() -> None:
    assert loadgen.STOP_AFTER_FAILURES == 5


# -- https with the run's certificate authority -----------------------------------------------


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl command")
def test_https_verifies_against_the_runs_ca_file_and_fails_without_it(tmp_path: Path) -> None:
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", "/CN=qa",
            "-addext", "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            seen.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"https://127.0.0.1:{server.server_port}"
    only_get = LOAD.replace('method = "POST"', 'method = "GET"').replace(
        'body_file = "fixtures/body.json"', ""
    )
    try:
        config, run = make_run(tmp_path / "ok", url, load=only_get, ca_file=str(cert))
        assert cli("--config", config, "--run-dir", run).returncode == 0
        assert len(seen) == 20
        config, run = make_run(tmp_path / "bad", url, load=only_get)  # no ca_file
        result = cli("--config", config, "--run-dir", run)
    finally:
        server.shutdown()
        server.server_close()
    assert result.returncode == 3  # the system trust store doesn't know the authority
    assert len(seen) == 20  # and no request got through


# -- the file itself --------------------------------------------------------------------------


def test_it_imports_only_the_standard_library() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)


def test_the_caps_are_the_documented_ones() -> None:
    assert loadgen.CAPS == loadgen.Caps(8, 20, 300)
