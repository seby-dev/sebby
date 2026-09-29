# Security guide

Use this guide when your charter makes you a `security-tester`. It gives the commands for each item on your checklist, and what a control that holds looks like. It covers the local instance only. `browser.md` covers the browser, and `report-format.md` covers findings.

## What the charter gives you

- The site origin, such as `https://127.0.0.1:<port>` (a run with Caddy) or `http://127.0.0.1:<port>`. Use it exactly, never `localhost`.
- For an `https://` origin: `<ca_file>`, the run's own root certificate, and `<browser config>`, the file for `open --config=`.
- Whether the run has Caddy. If it doesn't, headers and the content security policy (CSP) are skipped, and you say so in your report.
- Your state files (one per identity), your spare identity for a sign-out test, your magic-link budget, and whether you're the final rate-limit batch.
- A "direct backend port", if you may probe the backend without the proxy.
- The run's outbox (`outbox` in `instance.json`), the folder where each email the app sends lands as an `.eml` file, and the path of `<run folder>/env/identities.json`, which holds the seeded share's URL. Both hold live tokens: a script in your folder reads them for you, and you never print or copy their contents.
- The path of the app's source. Read `src/staff2solfa/api/auth.py` (`ROLE_RULES`) and the route you test, so you try the routes that exist. Reading is fine; never edit.

## Requests from the shell

Every command starts with `cd <run folder>/testers/<you> &&`, so files land in your folder.

**A cookie jar.** A state file holds a session cookie you must never print. Save this script as `jar.py` in your folder with the Write tool, then make one jar per identity. The script prints nothing, creates the jar readable by you alone, and marks the cookie `Secure` only for an `https://` origin, because `curl` sends a secure cookie only over `https`:

```python
import json
import os
import sys

state_file, jar_file, site = sys.argv[1:4]
secure = "TRUE" if site.startswith("https://") else "FALSE"
with open(state_file, encoding="utf-8") as handle:
    cookies = json.load(handle)["cookies"]
fd = os.open(jar_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as jar:
    jar.write("# Netscape HTTP Cookie File\n")
    for c in cookies:
        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        row = [c["domain"], "FALSE", c.get("path", "/"), secure, "0", c["name"], c["value"]]
        jar.write(prefix + "\t".join(row) + "\n")
```

    python3 jar.py <state file> singer.jar <site origin>

**Share tokens.** A share URL holds its token, and the guard logs every command, so a token never goes in one. Save this script as `share.py`. It reads a share's URL from `identities.json` (the seeded share) or from a publish response you saved with `-o`, and writes three `curl` config files, each readable by you alone: `<name>.cfg` with the real URL, `<name>-altered.cfg` with the token's last character changed, and `<name>-made-up.cfg` with a made-up token of the same length. It prints nothing:

```python
import json
import os
import secrets
import sys

source, site, name = sys.argv[1:4]
with open(source, encoding="utf-8") as handle:
    data = json.load(handle)
token = (data["share"]["url"] if "share" in data else data["url"]).rsplit("/", 1)[1]
altered = token[:-1] + ("B" if token[-1] == "A" else "A")
made_up = secrets.token_urlsafe(16)[: len(token)]
for suffix, value in (("", token), ("-altered", altered), ("-made-up", made_up)):
    fd = os.open(f"{name}{suffix}.cfg", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as cfg:
        cfg.write(f'url = "{site}/s/{value}"\n')
```

    python3 share.py <run folder>/env/identities.json <site origin> seeded
    curl -sS --cacert <ca_file> -K seeded-altered.cfg -o /dev/null -w '%{http_code}\n'

**Magic-link tokens.** A sign-in email holds a live token. Save this script as `consume.py`. It reads the newest `.eml` in the outbox addressed to one address, and writes the `POST /v1/auth/consume` body to `consume.json`, readable by you alone. It prints nothing, and exits 1 when no email to that address has arrived:

```python
import email
import email.policy
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

outbox, address = sys.argv[1:3]
for path in sorted(Path(outbox).glob("*.eml"), reverse=True):  # named by UTC time: newest first
    message = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    if str(message["To"]).strip().lower() == address.lower():
        break
else:
    sys.exit(1)
link = re.search(r"\S*/sign-in\?\S+", message.get_body(("plain",)).get_content())
if link is None:
    sys.exit(1)
query = parse_qs(urlsplit(link.group(0)).query)
fd = os.open("consume.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as out:
    json.dump({"token": query["token"][0], "email": query["email"][0]}, out)
```

    python3 consume.py <outbox> <address>

**Requests.** Add `--cacert <ca_file>` for an `https://` origin, and `-b singer.jar` to act as that identity:

    curl -sS --cacert <ca_file> -b singer.jar <site origin>/v1/auth/session
    curl -sS --cacert <ca_file> -o /dev/null -w '%{http_code}\n' <site origin>/v1/shares

Send a body from a file, and an upload as a form:

    curl -sS --cacert <ca_file> -b tr.jar -H 'Content-Type: application/json' --data @payload.json <site origin>/v1/solfa/parse
    curl -sS --cacert <ca_file> -b tr.jar -F input_type=musicxml -F beats_per_bar=4 -F file=@xxe.musicxml <site origin>/v1/extract

A direct backend probe is the same request to `http://127.0.0.1:<direct backend port>`, with no `--cacert`. Label its finding "direct backend". The backend has no Caddy in front of it and no headers of its own to check.

## Access control between workspaces

Workspace `qa-a` holds a seeded run and share, and `qa-b` holds none. Act as a member of `qa-b` on `qa-a`'s IDs, and as a signed-out caller, and expect a refusal or a 404, never data.

- **Roles.** For each write rule in `ROLE_RULES`, send the request as a singer, then as a transcriber for the Director rules. Expect `403`. A write that matches no rule is refused too. A `2xx` is a finding.
- **IDOR.** Read a run, a render, and a share report of `qa-a` as a `qa-b` member: `GET /v1/runs/<run id>/open`, `GET /v1/shares/<share id>/reports`. Expect `404`.
- **Share tokens.** `GET <site origin>/s/<token>` works signed out for a live share. Run `share.py` on `identities.json`, then send `curl -sS --cacert <ca_file> -K seeded.cfg -o /dev/null -w '%{http_code}\n'` (expect `200`), and the same with `seeded-made-up.cfg` and `seeded-altered.cfg`. For the revoked token, publish a share of your own with `-o publish.json`, run `python3 share.py publish.json <site origin> mine`, revoke it, and send `-K mine.cfg`. Expect `404` (or `410`) for each but the live one.
- **Magic links.** Reuse a link the orchestrator's plan lets you request. Write `{"email": "<address>"}` to `link.json` and send it: `curl -sS --cacert <ca_file> -H 'Content-Type: application/json' -H 'Origin: <site origin>' --data @link.json -o /dev/null -w '%{http_code}\n' <site origin>/v1/auth/magic-link`. When the email reaches the outbox, run `python3 consume.py <outbox> <address>`, then send the same request twice: `curl -sS --cacert <ca_file> -H 'Content-Type: application/json' --data @consume.json -o /dev/null -w '%{http_code}\n' <site origin>/v1/auth/consume`. Expect `200`, then a `400` the second time. Each real link request counts against your budget.

## Session handling

- **Fixation and made-up sessions.** Send `Cookie: s2s_session=made-up-value` to `GET /v1/auth/session` and to a write route. Expect `401`.
- **Cookie flags.** A real sign-in's `Set-Cookie` header should carry `HttpOnly`, `Secure`, `SameSite=Lax`, and `Path=/`. Read it from the first consume of the magic-link test, with the value hidden: `curl -sS --cacert <ca_file> -D - -o /dev/null -H 'Content-Type: application/json' --data @consume.json <site origin>/v1/auth/consume | grep -i '^set-cookie' | sed 's/=[^;]*/=HIDDEN/'`. It costs a real link request, so check it once, or skip it if your budget is zero.
- **Sign-out.** Only with your spare identity: send `DELETE /v1/auth/session` with its jar, then use the same jar again. Expect `401`: a sign-out must end the session on the server, not just clear the cookie.
- **CSRF and origin.** A write with `-H 'Origin: https://evil.example'` and one with `-H 'Sec-Fetch-Site: cross-site'` must both get `403`. A write that sends neither header passes by design (only a browser sends them), so that isn't a finding.
- **API key.** Send `X-API-Key: <a made-up value>` and `X-Workspace-Id: <a workspace id>` to a route that needs a session. Expect `401`. Behind Caddy both headers are stripped before they reach the backend.

## Malicious uploads

Write each payload with the Write tool, and send it as the `file` field of `POST /v1/extract` with `input_type=musicxml`. Send every size and type probe with `input_type=musicxml`; never send a scan type (image or pdf) outside the cost-cap probe. Reading a photo or PDF stops at the guard, so you test only the upload and parse boundary.

- **External entity.** A `DOCTYPE` that declares `<!ENTITY xxe SYSTEM "file:///etc/hostname">` and uses `&xxe;` in a part name. The expected result is a `422` ("undefined entity"), and nothing from the file in any response.
- **Entity expansion.** A "billion laughs" chain of nested entities. Expect a refusal within seconds, not a hang or a memory spike.
- **`.mxl` archives.** A zip with a member named `../../escape.txt`, and one that expands to a very large file. Expect a refusal, and no file outside the run's data folders.
- **Size and type.** A file over the size limit, an empty file, a PNG with a `.musicxml` name, and a PDF with an image type. Expect a `4xx` for each.

## Injection into rendered fields

Put script and markup text in a title, a lyric, and sol-fa text, parse it through `POST /v1/solfa/parse`, then open the piece in the browser and read the page with `snapshot`, `eval`, and `console`.

- **Markup.** `<script>document.title='x'</script>` and `"><img src=x onerror=document.title='x'>`. The page must show the text, never run it: `document.title` stays as it was, and the Review grid, the choir-sheet preview, and the singer page (`/sing/<token>`) each show it as text.
- **LilyPond.** A title with `\include "/etc/hostname"` and `#(system "id")`, rendered through `POST /v1/render-preview` with `"view": "staff"`. The staff PDF must print the text, never run it.

## Cost-cap races

Only when your charter says your batch runs alone: the probe changes the seeded workspace's spend cap, which other testers' reads share. It sends `POST /v1/extract` with an image, the one sanctioned photo read. It bills nothing, because the guarded backend sets `STAFF2SOLFA_FORBID_BILLED=1` and holds no provider key, so the vision call stops at the guard, and each read leaves a `vision_forbidden` line in the backend log that the orchestrator expects. A read reserves its ceiling in the spend ledger before that call, so the cap can be tested.

1. As a Director, set a small cap. Write `{"cap_usd": 1.5}` to `cap.json`, then:

        curl -sS --cacert <ca_file> -b dir.jar -X PUT -H 'Content-Type: application/json' --data @cap.json <site origin>/v1/workspaces/<workspace id>/spend-cap

2. Make a real PNG, since a file that isn't one is refused before the cap is checked. Save this script as `png.py` and run `python3 png.py`; it writes a valid 1x1 image to `one.png`, with the standard library only:

    ```python
    import struct
    import zlib


    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1, 8-bit RGB
    pixels = zlib.compress(b"\x00\xff\xff\xff")  # one row: no filter, one white pixel
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    with open("one.png", "wb") as out:
        out.write(png)
    ```

3. Send the same read six times at once, in one command, and count the results:

        for i in 1 2 3 4 5 6; do curl -sS --cacert <ca_file> -b dir.jar -F input_type=image -F key=C -F beats_per_bar=4 -F files=@one.png <site origin>/v1/extract -o out-$i.json -w '%{http_code}\n' & done; wait

4. Read each `out-<n>.json`. A job id means the read was reserved. A `402` whose body says "this month's reading budget has" what's left means the cap held for that read. A `422` means the probe is malformed: fix the request and send it again, and never record it as held. At the default unknown-page cost of $1.00 a page, a $1.5 cap lets exactly one job id through, so a second one is a finding. Report the counts you saw. Then put the cap back: write `{"cap_usd": null}` to `cap.json` and send the same `PUT`. Only the orchestrator runs `loadgen.py`: if you want a bigger burst, ask for it in your report, or send more parallel `curl` requests.

## Rate-limit abuse

Only when your charter says you're the final batch and alone. Send `POST /v1/auth/magic-link` for one seeded address more than five times, with `link.json` as the body, `-H 'Content-Type: application/json'`, and the site's own `Origin` header, as in the magic-link test. Then count the emails to that address in the run's outbox (`outbox` in `instance.json`), for example with `grep -il '^To: <address>' <outbox>/*.eml | wc -l`. The route answers `202` every time, so a missing email is the limit working. If fewer than 5 emails arrived, the per-client limit (20 an hour, shared by every tester, the session sign-ins, and the restart probe) may be the one you hit; say so. Never test the limit any other way: every tester shares it. The orchestrator restarts the backend afterward and checks that the limit cleared.

## Headers and CSP

Only when the run has Caddy. Read the headers of a `GET` (a `HEAD` gets a `405` from the share routes) with `curl -sS -D - -o /dev/null --cacert <ca_file> <site origin>/`, and again for `/sing/x`, `/s/x`, and `/sign-in`:

- `Content-Security-Policy` on the app's pages, with `default-src 'self'`, `object-src 'none'`, and `frame-ancestors 'self'`.
- `X-Content-Type-Options: nosniff` and `X-Frame-Options: SAMEORIGIN`, and no `Server` header.
- `Referrer-Policy: no-referrer` on `/sing/*`, `/s/*`, and `/sign-in`.
- No `Strict-Transport-Security` on a loopback origin (by design).
- A dot segment is refused before it reaches the backend: `curl -sS --path-as-is --cacert <ca_file> -o /dev/null -w '%{http_code}\n' '<site origin>/v1/runs/../../s/x'` gives `400`.

A missing header the Caddyfile promises is a finding. Say "skipped: no Caddy" when the run has none.

## What to write

- A finding for each control that gave way, with the "Security detail" field: the affected route, why it matters, and "direct backend" if you skipped the proxy.
- `held.md` for each attack that failed, one line each: `<area>: <attack> -> <what the app did>`. An attack you didn't try isn't in it, and an empty report proves nothing about it.
