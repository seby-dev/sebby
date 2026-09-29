# Security guide

Use this guide when your charter makes you a `security-tester`. It gives the commands for each item on your checklist, and what a control that holds looks like. It covers the local instance only. `browser.md` covers the browser, and `report-format.md` covers findings.

## What the charter gives you

- The site origin, such as `https://127.0.0.1:<port>` (a run with Caddy) or `http://127.0.0.1:<port>`. Use it exactly, never `localhost`.
- For an `https://` origin: `<ca_file>`, the run's own root certificate, and `<browser config>`, the file for `open --config=`.
- Whether the run has Caddy. If it doesn't, headers and the content security policy (CSP) are skipped, and you say so in your report.
- Your state files (one per identity), your spare identity for a sign-out test, your magic-link budget, and whether you're the final rate-limit batch.
- A "direct backend port", if you may probe the backend without the proxy.
- The path of the app's source. Read `src/staff2solfa/api/auth.py` (`ROLE_RULES`) and the route you test, so you try the routes that exist. Reading is fine; never edit.

## Requests from the shell

Every command starts with `cd <run folder>/testers/<you> &&`, so files land in your folder.

**A cookie jar.** A state file holds a session cookie you must never print. Save this script as `jar.py` in your folder with the Write tool, then make one jar per identity. The script prints nothing and marks the cookie `Secure` only for an `https://` origin, because `curl` sends a secure cookie only over `https`:

```python
import json
import sys

state_file, jar_file, site = sys.argv[1:4]
secure = "TRUE" if site.startswith("https://") else "FALSE"
with open(state_file, encoding="utf-8") as handle:
    cookies = json.load(handle)["cookies"]
with open(jar_file, "w", encoding="utf-8") as jar:
    jar.write("# Netscape HTTP Cookie File\n")
    for c in cookies:
        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        row = [c["domain"], "FALSE", c.get("path", "/"), secure, "0", c["name"], c["value"]]
        jar.write(prefix + "\t".join(row) + "\n")
```

    python3 jar.py <state file> singer.jar <site origin> && chmod 600 singer.jar

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
- **Share tokens.** `GET <site origin>/s/<token>` works signed out for a live share. Try a made-up token of the same length, a token with a changed last character, and, for a share you published yourself and then revoked, the old token. Expect `404` (or `410`) for each.
- **Magic links.** Reuse a link the orchestrator's plan lets you request: open its `/sign-in?...` page, press the button once (it sends `POST /v1/auth/consume`), then send the same `POST` again. Expect a failure the second time. Each real link request counts against your budget.

## Session handling

- **Fixation and made-up sessions.** Send `Cookie: s2s_session=made-up-value` to `GET /v1/auth/session` and to a write route. Expect `401`.
- **Cookie flags.** A real sign-in's `Set-Cookie` header (from `POST /v1/auth/consume`, with `curl -D -`) should carry `HttpOnly`, `Secure`, `SameSite=Lax`, and `Path=/`. It costs a real link request, so check it once, or skip it if your budget is zero.
- **Sign-out.** Only with your spare identity: send `DELETE /v1/auth/session` with its jar, then use the same jar again. Expect `401`: a sign-out must end the session on the server, not just clear the cookie.
- **CSRF and origin.** A write with `-H 'Origin: https://evil.example'` and one with `-H 'Sec-Fetch-Site: cross-site'` must both get `403`. A write that sends neither header passes by design (only a browser sends them), so that isn't a finding.
- **API key.** Send `X-API-Key: <a made-up value>` and `X-Workspace-Id: <a workspace id>` to a route that needs a session. Expect `401`. Behind Caddy both headers are stripped before they reach the backend.

## Malicious uploads

Write each payload with the Write tool, and send it as the `file` field of `POST /v1/extract` with `input_type=musicxml`. Reading a photo or PDF stops at the guard, so you test only the upload and parse boundary.

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

1. As a Director, set a small cap: `PUT /v1/workspaces/<workspace id>/spend-cap` with `--data @cap.json`, a file that holds `{"cap_usd": 1.5}`.
2. Send the same read six times at once, in one command, and count the results:

        for i in 1 2 3 4 5 6; do curl -sS --cacert <ca_file> -b dir.jar -F input_type=image -F beats_per_bar=4 -F file=@one.png <site origin>/v1/extract -o out-$i.json -w '%{http_code}\n' & done; wait

3. A job id in a response means the read was reserved; a refusal sentence means the cap held. The number of job ids must not pass what fits under the cap. Report the counts you saw. Then put the cap back with `{"cap_usd": null}`. Only the orchestrator runs `loadgen.py`: if you want a bigger burst, ask for it in your report, or send more parallel `curl` requests.

## Rate-limit abuse

Only when your charter says you're the final batch and alone. Send `POST /v1/auth/magic-link` for one seeded address more than five times (with a JSON body from a file and the site's own `Origin` header), and count the emails that reach the run's outbox. The route answers `202` every time, so a missing email is the limit working. Never test the limit any other way: every tester shares it. The orchestrator restarts the backend afterward and checks that the limit cleared.

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
