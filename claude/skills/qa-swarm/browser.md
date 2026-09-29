# Browser guide for QA testers

Use this guide when a charter tells you to drive a browser with `playwright-cli`. The adversarial and security testers use it for their steps. The `qa-tester` uses it only for a setup step, such as a file upload that its executor can't do.

## Version and command

The project pins `@playwright/cli` at version 0.1.22. Run it as follows, where `<cli folder>` is the absolute path your charter gives (the `tools/qa-cli` folder of the repository under test, which holds the pinned CLI):

    cd <run folder>/testers/<tester> && npm --prefix <cli folder> exec -- playwright-cli -s=<tester>-<n> <command>

Every Bash call starts in the repository, so start each command with the `cd`. The CLI writes snapshots, such as `page-<timestamp>.yml`, to a `.playwright-cli/` folder in the directory you run it from. Without the `cd`, that folder lands in the repository tree, and the guard logs the command as `_unattributed`. The guard doesn't catch a run from the wrong folder, so `git status` is the backstop.

The CLI's install command is `install-browser`. Don't run it in a cloud session: the machine already links a Chromium build. The guard doesn't enforce this, so the rule is yours to keep. As root, set `PLAYWRIGHT_MCP_SANDBOX=false` in the command's environment, or Chromium's sandbox fails.

## Sessions

- Name each session `<tester>-<n>` and pass it as `-s=<tester>-<n>` (for example `-s=qa-1-2`). Open only the sessions your charter lists.
- Register every session you open, so `qa_env.sh stop` can close it. Read `testers/<tester>/sessions.json`, add the session name to its `"playwright"` list, and write the file back with every other key (such as `groups`, `daemons`, and `bh_dirs`) unchanged.
- Always close every session you open before you finish, with `close`. A headless session also closes itself after it sits an hour idle. If a run outlasts that, open the session again and load its state file again.
- `list` shows the open sessions. Never run `close-all` or `kill-all`: they close every tester's sessions, and the guard denies them. Close each of your own sessions with `close`.

## Signing in

Never sign in with a link or a key. The orchestrator wrote a state file for each identity in your charter. Load it in three steps:

1. Run `open about:blank`. `state-load` needs an open session.
2. Run `state-load <state file>`, using its absolute path.
3. Run `goto <url>` to visit the site.

The state file holds the session cookie and the app's signed-in record, so the app starts signed in.

Run `open` once per session. A second `open` restarts the session and drops its cookies and storage. To move to another page, use `goto`.

## Commands you'll use

- `goto <url>` navigates; `snapshot` prints the page text with element refs.
- `click <ref>`, `fill <ref> <text>`, `type <text>`, and `press <key>` act on a ref from the latest snapshot.
- `upload <files...>` attaches files (absolute paths) to the file chooser that's open.
- `screenshot`, `console`, `requests`, `request <index>`, and `eval <js>` collect evidence.
- `state-save <file>` writes the session's state.

Use the exact `127.0.0.1` origin in your charter, never `localhost`.

## Rules

- Check every URL against the charter's allowlist before you open it.
- Keep evidence under `testers/<tester>/screenshots/` and `testers/<tester>/findings/`.
- If a command prints a page that asks you to sign in, stop and report an expired or missing state file. Don't request a magic link. The guard doesn't enforce that, or a write into a sibling tester's folder, so the log review and `git status` are the backstop.
