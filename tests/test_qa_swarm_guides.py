"""The qa-swarm guides carry the rules the testers depend on."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent / "claude" / "skills" / "qa-swarm"


def text(name: str) -> str:
    return (SKILL / name).read_text(encoding="utf-8")


def test_browser_guide_pins_the_cli_and_names_its_sessions_and_output() -> None:
    body = text("browser.md")
    needles = (
        "@playwright/cli",
        "0.1.22",
        "-s=<tester>-<n>",
        "state-load",
        "goto",
        "close every session",
        "sessions.json",
        "idle",
        "absolute path",
        "<cli folder>",
        "install-browser",
        ".playwright-cli",
        "cd <run folder>/testers/<tester> && npm --prefix <cli folder> exec --",
        "Never run `close-all` or `kill-all`",
        "an hour idle",
        "`groups`, `daemons`, and `bh_dirs`",
        "sibling tester's folder",
    )
    for needle in needles:
        assert needle in body, needle


def test_browser_guide_says_a_second_open_restarts_the_session() -> None:
    body = text("browser.md")
    assert "second `open` restarts" in body
    assert "state-load` needs an open session" in body


def test_scenarios_guide_defines_the_scenario_fields_and_names_no_executor() -> None:
    body = text("scenarios.md")
    needles = (
        "Scenario(",
        "goal",
        "verify",
        "PageState",
        "actors",
        "Budget",
        "run_scenario",
        "assert_passed",
        "start_url",
    )
    for needle in needles:
        assert needle in body, needle
    assert "jev" not in body.lower().replace("no executor", "")
    assert "field_text" not in body


def test_scenarios_guide_says_verify_never_trusts_the_agent_and_upload_needs_setup() -> None:
    body = text("scenarios.md")
    assert "never trusts" in body.lower() and "upload" in body.lower()


def test_scenarios_guide_reads_the_site_and_state_from_the_environment() -> None:
    body = text("scenarios.md")
    assert 'SITE_URL = os.environ["QA_SITE_URL"]' in body
    assert 'Path(os.environ["QA_RUN_DIR"]) / "state"' in body
    assert "/results" not in body and "illustrative" in body


def test_report_format_has_every_finding_field_and_the_severity_scale() -> None:
    body = text("report-format.md")
    fields = (
        "ID",
        "Tester and role",
        "Severity",
        "Title",
        "Steps to reproduce",
        "Expected and actual",
        "Evidence",
        "Reproduced",
    )
    for field in fields:
        assert field in body, field
    for level in ("blocker", "high", "medium", "low"):
        assert level in body
    assert "reachability" in body


@pytest.mark.parametrize(
    "name", ["browser.md", "scenarios.md", "report-format.md", "collisions.md", "security.md"]
)
def test_guides_use_sentence_case_headings_and_no_directional_words(name: str) -> None:
    allowed = {"Playwright", "CLI"}
    for line in text(name).splitlines():
        if line.startswith("#"):
            words = line.lstrip("# ").split()
            for i, w in enumerate(words):
                if w[0].isalpha() and i > 0:
                    assert not w[0].isupper() or w.isupper() or w in allowed, line
    lowered = text(name).lower()
    for word in (" above ", " below "):
        assert word not in lowered, word


def test_collisions_guide_defines_the_target_file_and_the_barrier_command() -> None:
    body = text("collisions.md")
    needles = (
        "target.json",
        '"version": 1',
        '"barrier"',
        '"participants"',
        "`null` unless `method` is `barrier-click`",
        'bash "${SEBBY_ROOT:-$HOME/Developer/sebby}/claude/skills/qa-swarm/barrier.sh"'
        " wait <barrier.dir>",
        "`timeout` set to 600000",
        "never re-run the barrier command",
        "<barrier.count> <barrier.timeout_seconds>",
        "cd <run>/testers/<me> && bash",
        "-s=<session> click <ref>",
        "Snapshot first, then one command",
    )
    for needle in needles:
        assert needle in body, needle


def test_collisions_guide_says_what_each_exit_code_means() -> None:
    body = text("collisions.md")
    for needle in ("`0`", "`75`", "`64`", "`1`", "abandoned", "not passed"):
        assert needle in body, needle
    for needle in (
        "Any code other than `0`",
        "`127` from a wrong path",
        "means you didn't act: record the scenario as abandoned",
        "you already ran the command",
    ):
        assert needle in body, needle
    assert "counts once" not in body


def test_collisions_guide_names_the_four_methods() -> None:
    body = text("collisions.md")
    for needle in (
        "**Barrier click.**",
        "**Repeat click.**",
        "`dblclick <ref>`",
        "`click <ref> && click <ref>`",
        "**Parallel fetch.**",
        "Promise.all([fetch(",
        "**Parallel sessions.**",
        'cd <run>/testers/<me> && { cmd1 & p1=$!; cmd2 & p2=$!; wait $p1; echo "a=$?";'
        ' wait $p2; echo "b=$?"; }',
        "cd <run>/testers/<me> && npm --prefix <cli folder> exec --"
        " playwright-cli -s=<session> eval",
    ):
        assert needle in body, needle


def test_collisions_guide_keeps_testers_out_of_shared_and_names_the_spread_command() -> None:
    body = text("collisions.md")
    for needle in (
        "You never write in the run's `shared/` folder",
        'barrier.sh" spread <barrier.dir>',
        "`spread_ms`",
        'the runbook\'s "Barrier timing" table',
        "lets the barrier abandon",
    ):
        assert needle in body, needle
    assert not re.search(r"\d+\s*ms\b", body), "the guide must not promise a timing number"


def test_browser_guide_lists_the_adversarial_testers_commands() -> None:
    body = text("browser.md")
    for command in (
        "dblclick",
        "go-back",
        "go-forward",
        "reload",
        "tab-new",
        "tab-select",
        "cookie-delete",
        "requests",
    ):
        assert f"`{command}" in body, command
    assert "The adversarial tester uses" in body
    assert "cookie-delete s2s_session" in body


def test_report_format_has_the_collision_row_and_the_adv_prefix() -> None:
    body = text("report-format.md")
    assert "| Collision |" in body and "`barrier.sh spread` output, or `abandoned`" in body
    assert "`adv-1-02`" in body and "`qa-1-03`" in body
    assert "`adv-02`" not in body and "`qa-03`" not in body


def test_every_barrier_path_in_the_guides_is_run_with_bash() -> None:
    for name in ("browser.md", "collisions.md", "report-format.md", "scenarios.md"):
        body = text(name)
        for match in re.finditer(r'[^\s"`]*/barrier\.sh', body):
            assert re.search(r'bash "?$', body[: match.start()]), (name, match.group(0))


def test_collisions_guide_says_a_signal_abandons_and_the_outcome_file_is_authoritative() -> None:
    body = text("collisions.md")
    for needle in (
        "A stopped command abandons the barrier for everyone",
        "never writes its `acted` entry",
        "The outcome file and `spread` are authoritative, not one participant's exit code",
        "the late participant has returned `75` while the others returned `0`",
    ):
        assert needle in body, needle
    assert "withdraws its own arrival" not in body


def test_collisions_guide_checks_released_count_against_the_acted_entries() -> None:
    body = text("collisions.md")
    for needle in (
        "`released_count` (how many had arrived when the barrier released, or `null`)",
        "`released_count` must equal the number of `acted` entries",
        "a SIGKILL can't be trapped",
        '"not a real collision", not as passed',
    ):
        assert needle in body, needle


def test_collisions_guide_names_the_unshared_run_keys() -> None:
    body = text("collisions.md")
    assert '"run_unshared_id": "...",' in body and '"run_unshared_title":' in body
    assert (
        "`run_id`, `run_title`, `run_unshared_id`, `run_unshared_title`, `share_id`, and"
        " `share_url` appear only when the scenario uses them"
    ) in body
    assert "a scenario that needs `run_unshared`, such as `double-publish`" in body


def test_collisions_guide_describes_both_mismatch_directions() -> None:
    body = text("collisions.md")
    for needle in (
        "`released_count` greater than the number of `acted` entries means a released"
        " participant never recorded acting",
        "a failed write",
        "Fewer means a participant arrived after the release",
        "isn't a real collision for that participant",
        'Either way, the orchestrator reports the scenario as "not a real collision"',
    ):
        assert needle in body, needle


def test_collisions_guide_runs_the_charters_command_and_starts_spread_in_the_testers_folder() -> (
    None
):
    body = text("collisions.md")
    assert "Run the barrier command exactly as your charter writes it" in body
    assert "only the fallback when a charter gives none" in body
    assert 'cd <run>/testers/<me> && bash "${SEBBY_ROOT:-$HOME/Developer/sebby}' in body
    assert 'barrier.sh" spread <barrier.dir>' in body.split("## Measuring the spread", 1)[1]
    spread_block = body.split("## Measuring the spread", 1)[1].split("```bash", 1)[1]
    assert spread_block.lstrip().startswith("cd <run>/testers/<me> && bash")


def test_collisions_guide_copies_a_parallel_fetch_from_a_request_the_page_makes() -> None:
    body = text("collisions.md")
    for needle in (
        "a request the page itself makes",
        "`playwright-cli requests`",
        "`request <index>`",
        "method, URL, headers, and JSON body",
        '"$(cat body.json)"',
        "'Content-Type': 'application/json'",
    ):
        assert needle in body, needle
    assert "read it in the `eval`" not in body


def test_collisions_guide_says_spread_is_the_release_spread_and_names_clock_and_skipped() -> None:
    body = text("collisions.md")
    for needle in (
        "`spread_ms` is the release spread",
        "The action lands later",
        "action spread",
        "`clock`",
        "`skipped`",
    ):
        assert needle in body, needle


def test_collisions_guide_uses_active_voice_and_drops_the_old_title_rule() -> None:
    body = text("collisions.md")
    for phrase in ("was abandoned", "Nothing was created", "run_title` is `null`"):
        assert phrase not in body, phrase


def test_browser_guide_forbids_the_apps_sign_out() -> None:
    body = text("browser.md")
    for needle in (
        "Never use the app's Sign out",
        "Testers share one server session per identity",
        "ends every tester's session on that identity",
        "only with `cookie-delete s2s_session`, in your own session",
    ):
        assert needle in body, needle


def test_security_guide_covers_the_checklist_with_commands() -> None:
    body = text("security.md")
    for needle in (
        "## Access control between workspaces",
        "## Session handling",
        "## Malicious uploads",
        "## Injection into rendered fields",
        "## Cost-cap races",
        "## Rate-limit abuse",
        "## Headers and CSP",
        "`ROLE_RULES`",
        "--cacert <ca_file>",
        "--data @cap.json",
        "-F file=@xxe.musicxml",
        "-H 'Origin: https://evil.example'",
        "Sec-Fetch-Site: cross-site",
        "--path-as-is",
        "`held.md`",
        "wait",
    ):
        assert needle in body, needle


def test_security_guide_builds_a_cookie_jar_without_printing_the_cookie() -> None:
    body = text("security.md")
    assert "prints nothing" in body and "chmod" not in body
    assert "python3 jar.py <state file> singer.jar <site origin>\n" in body
    assert "`Secure` only for an `https://` origin" in body
    script = re.search(r"```python\n(.*?)```", body, re.DOTALL)
    assert script and "print(" not in script.group(1)
    assert 'json.load(handle)["cookies"]' in script.group(1)
    assert "os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600" in script.group(1)


def test_security_guide_runs_the_script_it_ships(tmp_path: Path) -> None:
    body = text("security.md")
    script = re.search(r"```python\n(.*?)```", body, re.DOTALL)
    assert script
    path = tmp_path / "jar.py"
    path.write_text(script.group(1), encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "s2s_session",
                        "value": "COOKIEVALUE",
                        "domain": "127.0.0.1",
                        "path": "/",
                        "httpOnly": True,
                    }
                ]
            }
        )
    )
    for site, flag in (("https://127.0.0.1:1", "TRUE"), ("http://127.0.0.1:1", "FALSE")):
        jar = tmp_path / f"{flag}.jar"
        done = subprocess.run(
            [sys.executable, str(path), str(state), str(jar), site],
            capture_output=True,
            text=True,
            check=True,
        )
        assert done.stdout == "" and done.stderr == ""
        assert jar.stat().st_mode & 0o777 == 0o600
        rows = jar.read_text().splitlines()
        assert rows[0] == "# Netscape HTTP Cookie File"
        assert rows[1] == f"#HttpOnly_127.0.0.1\tFALSE\t/\t{flag}\t0\ts2s_session\tCOOKIEVALUE"


def test_security_guide_says_what_holds_and_what_to_write() -> None:
    body = text("security.md")
    for needle in (
        "expect a refusal or a 404, never data",
        "A write that matches no rule is refused too.",
        "undefined entity",
        "A missing header the Caddyfile promises is a finding.",
        "skipped: no Caddy",
        "an empty report proves nothing about it",
        "Only with your spare identity",
        "Only when your charter says you're the final batch and alone.",
    ):
        assert needle in body, needle


def test_browser_guide_has_the_https_section_with_the_certificate_option() -> None:
    body = text("browser.md")
    for needle in (
        "## HTTPS origins",
        "--config=<browser config>",
        "--ignore-certificate-errors-spki-list",
        "blocks service workers",
        "net::ERR_CERT_AUTHORITY_INVALID",
        "Never turn on a blanket switch such as `--ignore-certificate-errors`",
        "A run without Caddy has an `http://` origin and needs no config.",
    ):
        assert needle in body, needle
    assert body.index("## HTTPS origins") < body.index("## Commands you'll use")


def test_report_format_has_the_security_detail_row_the_sec_prefix_and_the_held_section() -> None:
    body = text("report-format.md")
    assert "| Security detail |" in body and '"direct backend"' in body
    assert "Never a cookie, a token, or a key." in body
    assert "`sec-1-02`" in body
    assert "## Attacks that held" in body
    assert "`testers/<tester>/held.md`" in body
    assert "`<area>: <attack> -> <what the app did>`" in body
    assert "a control with no line wasn't tested" in body


def test_security_guide_names_the_photo_read_and_leaves_loadgen_to_the_orchestrator() -> None:
    body = text("security.md")
    for needle in (
        "the one sanctioned photo read",
        "`STAFF2SOLFA_FORBID_BILLED=1` and holds no provider key",
        "`vision_forbidden` line in the backend log that the orchestrator expects",
        "Only when your charter says your batch runs alone",
        '{"cap_usd": null}',
        "Only the orchestrator runs `loadgen.py`",
    ):
        assert needle in body, needle
    assert "`loadgen.py` can send the burst" not in body


# -- the security guide's token scripts and probes (slice 3 branch-gate fixes) ----------------

TOKEN = "AbCdEfGhIjKlMnOpQrStUv"  # a share token's shape: 22 URL-safe characters
TOKEN_SHAPE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{22}(?![A-Za-z0-9_-])")


def security_script(name: str) -> str:
    """The Python block that follows "Save this script as `<name>`" in security.md."""
    body = text("security.md")
    at = body.index(f"Save this script as `{name}`")
    block = re.search(r"```python\n(.*?)\n\s*```", body[at:], re.DOTALL)
    assert block, name
    lines = block.group(1).splitlines()
    indent = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
    return "\n".join(line[indent:] for line in lines) + "\n"


def run_script(tmp_path: Path, name: str, *args: str) -> subprocess.CompletedProcess[str]:
    script = tmp_path / name
    script.write_text(security_script(name), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(script), *args], capture_output=True, text=True, cwd=tmp_path
    )


def test_share_py_writes_private_curl_configs_and_prints_nothing(tmp_path: Path) -> None:
    identities = tmp_path / "identities.json"
    identities.write_text(json.dumps({"share": {"workspace": "w", "url": f"/s/{TOKEN}"}}))
    done = run_script(tmp_path, "share.py", str(identities), "https://127.0.0.1:5173", "seeded")
    assert done.returncode == 0 and done.stdout == "" and done.stderr == ""
    urls = {}
    for suffix in ("", "-altered", "-made-up"):
        cfg = tmp_path / f"seeded{suffix}.cfg"
        assert cfg.stat().st_mode & 0o777 == 0o600
        match = re.fullmatch(r'url = "https://127\.0\.0\.1:5173/s/([^"]+)"\n', cfg.read_text())
        assert match, suffix
        urls[suffix] = match.group(1)
    assert urls[""] == TOKEN
    assert urls["-altered"][:-1] == TOKEN[:-1] and urls["-altered"] != TOKEN
    assert len(urls["-made-up"]) == len(TOKEN) and urls["-made-up"] != TOKEN
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{22}", value) for value in urls.values())
    published = tmp_path / "publish.json"
    published.write_text(json.dumps({"share_id": "s1", "url": f"/s/{TOKEN}"}))
    done = run_script(tmp_path, "share.py", str(published), "http://127.0.0.1:1", "mine")
    assert done.returncode == 0 and done.stdout == ""
    assert (tmp_path / "mine.cfg").read_text() == f'url = "http://127.0.0.1:1/s/{TOKEN}"\n'


def _mail(outbox: Path, name: str, to: str, token: str) -> None:
    from email.message import EmailMessage
    from urllib.parse import urlencode

    message = EmailMessage()
    message["From"] = "staff2solfa <noreply@localhost>"
    message["To"] = to
    message["Subject"] = "Your sign-in link"
    link = "https://127.0.0.1:5173/sign-in?" + urlencode({"token": token, "email": to})
    message.set_content(f"Use this link to sign in to QA A on staff2solfa:\n\n{link}\n\nBye.\n")
    (outbox / name).write_bytes(bytes(message))


def test_consume_py_reads_the_newest_mail_to_the_address(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    _mail(outbox, "20260929T100000000000Z-000001.eml", "singer-b@choir.test", "old-token")
    _mail(outbox, "20260929T100001000000Z-000002.eml", "singer-b@choir.test", "new-token")
    _mail(outbox, "20260929T100002000000Z-000003.eml", "other@choir.test", "other-token")
    done = run_script(tmp_path, "consume.py", str(outbox), "singer-b@choir.test")
    assert done.returncode == 0 and done.stdout == "" and done.stderr == ""
    body = tmp_path / "consume.json"
    assert body.stat().st_mode & 0o777 == 0o600
    assert json.loads(body.read_text()) == {"token": "new-token", "email": "singer-b@choir.test"}
    body.unlink()
    missing = run_script(tmp_path, "consume.py", str(outbox), "nobody@choir.test")
    assert missing.returncode == 1 and missing.stdout == "" and not body.exists()


def test_png_py_writes_a_valid_one_pixel_png(tmp_path: Path) -> None:
    import struct
    import zlib

    done = run_script(tmp_path, "png.py")
    assert done.returncode == 0 and done.stdout == ""
    data = (tmp_path / "one.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    at, chunks = 8, []
    while at < len(data):
        (length,) = struct.unpack(">I", data[at : at + 4])
        kind, payload = data[at + 4 : at + 8], data[at + 8 : at + 8 + length]
        (crc,) = struct.unpack(">I", data[at + 8 + length : at + 12 + length])
        assert crc == zlib.crc32(kind + payload), kind
        chunks.append((kind, payload))
        at += 12 + length
    assert [kind for kind, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    assert struct.unpack(">IIBBBBB", chunks[0][1]) == (1, 1, 8, 2, 0, 0, 0)
    assert zlib.decompress(chunks[1][1]) == b"\x00\xff\xff\xff"
    script = security_script("png.py")
    assert set(re.findall(r"^import (\w+)", script, re.MULTILINE)) == {"struct", "zlib"}


def test_the_cost_cap_probe_is_well_formed_and_says_what_a_422_means() -> None:
    body = text("security.md")
    section = body.split("## Cost-cap races", 1)[1].split("\n## ", 1)[0]
    burst = next(line for line in section.splitlines() if "for i in 1 2 3 4 5 6" in line)
    for needle in ("-F input_type=image", "-F key=C", "-F beats_per_bar=4", "-F files=@one.png"):
        assert needle in burst, needle
    assert "file=@one.png" not in section.replace("files=@one.png", "")
    puts = [line for line in section.splitlines() if "-X PUT" in line]
    assert puts and all("-H 'Content-Type: application/json'" in line for line in puts)
    for needle in (
        "A `402` whose body says \"this month's reading budget has\" what's left means the cap"
        " held",
        "A `422` means the probe is malformed: fix the request and send it again, and never"
        " record it as held.",
        "At the default unknown-page cost of $1.00 a page, a $1.5 cap lets exactly one job id"
        " through",
        "Save this script as `png.py`",
        'Record a second job id as "needs orchestrator check", not as a breach.',
    ):
        assert needle in section, needle
    assert "so a second one is a finding" not in section


def test_the_rate_limit_and_upload_steps_name_the_header_the_outbox_and_the_scan_rule() -> None:
    body = text("security.md")
    for needle in (
        "`-H 'Content-Type: application/json'`, and the site's own `Origin` header",
        "the run's outbox (`outbox` in `instance.json`)",
        "If fewer than 5 emails arrived, the per-client limit (20 an hour, shared by every tester,"
        " the session sign-ins, and the restart probe) may be the one you hit; say so.",
        "Send every size and type probe with `input_type=musicxml`; never send a scan type"
        " (image or pdf) outside the cost-cap probe.",
    ):
        assert needle in body, needle


def test_the_charter_section_names_the_outbox_and_the_identities_file() -> None:
    charter = text("security.md").split("## What the charter gives you", 1)[1].split("\n## ")[0]
    assert "The run's outbox (`outbox` in `instance.json`)" in charter
    assert "`<run folder>/env/identities.json`" in charter


def _documented_commands() -> list[str]:
    """Every shell command security.md gives: indented command lines and inline curl/python3."""
    body = text("security.md")
    commands = [
        line.strip()
        for line in body.splitlines()
        if re.match(r"\s{4,}(?:curl|python3|for i) ", line)
    ]
    commands += re.findall(r"`((?:curl|python3|grep) [^`]+)`", body)
    return commands


def test_the_guard_allows_every_documented_command_and_none_holds_a_secret(
    tmp_path: Path,
) -> None:
    import importlib.util

    hook_path = SKILL.parent.parent / "hooks" / "qa_tester_guard.py"
    spec = importlib.util.spec_from_file_location("qa_tester_guard_for_guides", hook_path)
    assert spec is not None and spec.loader is not None
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    run = tmp_path / "qa-20260929T101010-ab12"
    active = {
        "run_dir": str(run),
        "repo_root": str(tmp_path / "repo"),
        "ports": [5173],
        "direct_ports": [6543],
    }
    values = {
        "<run folder>": str(run),
        "<site origin>": "https://127.0.0.1:5173",
        "<ca_file>": f"{run}/env/caddy/root.crt",
        "<outbox>": f"{run}/env/data/outbox",
        "<address>": "singer-b@choir.test",
        "<state file>": f"{run}/state/singer-b@choir.test.json",
        "<workspace id>": "ws_0123",
        "<direct backend port>": "6543",
    }
    commands = _documented_commands()
    names = " ".join(commands)
    for needle in ("share.py", "consume.py", "-K seeded", "@consume.json", "png.py", "grep -il"):
        assert needle in names, needle
    for command in commands:
        assert "env/private" not in command and ".env" not in command, command
        assert not TOKEN_SHAPE.search(command), command
        for placeholder, value in values.items():
            command = command.replace(placeholder, value)
        command = f"cd {run}/testers/sec-1 && {command}"
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(run / "testers" / "sec-1"),
            "agent_type": "security-tester",
        }
        assert hook.decide(payload, active) is None, command
