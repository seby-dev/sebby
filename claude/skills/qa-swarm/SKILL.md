---
name: qa-swarm
description: Use when a finished feature of a web app needs end-to-end testing through a real browser: start an isolated instance, dispatch qa-tester agents, and merge their findings into one report.
---

# QA swarm

This skill runs a swarm of testers against a feature in an isolated copy of the app and produces one report with reproduction steps and evidence. It changes no code and fixes nothing. It can show that the attacks it tried fail; it can't show the app is unbreakable.

This version dispatches only `qa-tester` (a Sonnet 5.5 agent that writes and runs reusable scenarios). It doesn't run adversarial or security testers, collision scenarios, load, or a Caddy layer.

## Before you start

- The project supplies `.claude/qa.toml`. Its `schema_version` must be one you know (`1`).
- The user gives the feature: its spec or plan, and a brief. Read `git diff main...HEAD --stat` for the changed routes and components.
- Read `browser.md`, `scenarios.md`, and `report-format.md` in this skill's folder.

## The run

1. Read `.claude/qa.toml`. If the file is missing, its `schema_version` is unknown, a needed section is absent, or `[load] enabled` is on, stop and tell the user. Then run `scripts/qa_env.sh scenario-run --check`. If a key is missing, it stops and names the variable (never its value); stop and tell the user which variable and file.
2. Choose a scratch folder for the run (`<scratchpad>/`). Run `scripts/qa_env.sh start --scratch <scratchpad>`. It prints one JSON object with `run_dir`, `run_id`, `site_url`, `allowed_hosts`, and `swept` (the stale runs it removed). Use a relative or absolute scratch path; it resolves against your current folder. If the start fails, it prints no run folder (its error goes to stderr) and stops what it started. Report its stderr and the `env/*.log` files it names, and know that the next `start` sweeps what's left.
3. Run `scripts/qa_env.sh seed --run-dir <run folder>`, which writes `<run folder>/env/identities.json` (two workspaces, `qa-a` and `qa-b`, each with a director, a transcriber, and a singer, plus one seeded run and share), then `scripts/qa_env.sh session --run-dir <run folder> --email <address>` once for each identity your testers need, serially, before you dispatch. Use the default `--out`, which writes `<run folder>/state/<address>.json`. Testers only load the state files. Plan any real magic-link sign-in and count it against the limits (5 per address and 20 per client each hour, and an over-limit request is silent).
4. Write a charter for each tester at `<run folder>/testers/<name>/charter.md`: the run folder; the absolute path of `scripts/qa_env.sh`; the tester's persona, identity, and state file; the absolute path of the folder that holds the pinned `playwright-cli` (`browser.md`'s `<cli folder>`); the site origin and the allowlist (the site origin, plus the two sound hosts for browser traffic); the feature links, diff stat, and your brief; the ordered scenarios; the `[knowledge]` pointers; the known-artifacts list from `[knowledge]`; the `[forbidden]` rules and this skill's safety rules; the scenario budget it gets; and the warnings (an over-limit magic-link request is silent; the guard hook logs every command; a headless session closes after an hour idle).
5. Dispatch up to three `qa-tester` agents at once, each by its typed agent with no `model` override. If there are more than three, run them in batches of three and start the next batch only when the previous one ends. The run's session cap in `qa.toml` limits live browsers.
6. When a batch finishes, run `git status --porcelain` in the repository under test and report any change as a violation. Read each tester's `testers/<name>/commands.log` and `testers/_unattributed/commands.log` (the guard logs a command with no `testers/<name>` path there) for off-allowlist hosts and denied commands.
7. Copy each passing scenario draft from `testers/<name>/scenarios/` into `tests/qa_swarm/scenarios/` in the repository, run it once more in place, and commit it. The orchestrator, not the tester, commits scenarios. A tester never writes inside `tests/`. Don't commit a flaky draft.
8. Reproduce every blocker and high finding once, from its stated steps, and fill in its `Reproduced` field (yes, no, or not attempted). Keep a finding that doesn't reproduce, and mark it not reproduced.
9. Merge and de-duplicate the findings into `<run folder>/REPORT.md`: two findings are one entry if their method, route template, and symptom class match, and the entry lists every tester who saw it. Open with a summary table (ID, severity, title, testers, reproduced), then the findings sorted by severity. Name any check skipped (headers and CSP without Caddy), the scenario and step totals against the budget, any `vision_forbidden` or `jev_forbidden` lines in the `forbidden_hits` list that `qa_env.sh stop` printed, and any label in its `orphaned` list (a process group that outlived its leader, which `stop` can't verify or signal).
10. Run `scripts/qa_env.sh stop --run-dir <run folder>`, and give the user the path to `REPORT.md`.

Stop the environment on every exit path: after a failed step, an interrupted run, or a decision to abort. `stop` prints one JSON summary (`stopped`, `skipped`, `orphaned`, `forbidden_hits`), it's safe to run twice, and `qa_env.sh start` sweeps stale runs the next time.

## Safety rules for every tester

1. Check every target against the allowlist before a request. The default is the run's site origin. Refuse any other host, and record the refusal.
2. No external services and no out-of-band callbacks.
3. No billed call from the app: the guarded backend enforces this, and testers use sol-fa text, MusicXML, or seeded runs instead of photo and PDF reads. The executor's own calls (jev, through the named keys) are the one sanctioned billed exception, and `[budget]` caps them.
4. No source edits, `git push`, or `git commit`, and no dev data or dev backend.
5. No `dev-key`, no `.env` reads, and no `scripts/extract_hymn.py` or `scripts/eval_*` scripts.
6. Write only to the tester's own folder in the run.

The guard hook enforces parts of these rules and logs every command. Both are best effort, so steps 6 and 9 check after the fact.
