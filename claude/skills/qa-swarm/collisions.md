# Collision scenarios

Use this guide when your charter gives you a collision scenario: two or more participants (or two tabs of one participant) act on the same thing at the same moment, to find out what the app does when they collide. It covers the target file, the barrier command, and the four ways to make an action land together.

## What a collision scenario is

A collision scenario names a shared resource (a piece, a share), the participants who act on it, the action each one takes, and what the app should do. The orchestrator prepares the scenario and writes a `target.json` file. You read it, get into position, and act at the barrier.

You never write in the run's `shared/` folder. The guard denies a Write-tool call there, but it can't catch a Bash write, so the orchestrator lists the folder after the batch. Only the orchestrator's prepare step and `barrier.sh` write in it.

## The target file

The charter gives you the path of `<run>/shared/<scenario>/target.json`. Read it before you do anything else. It's version 1:

```json
{
  "version": 1,
  "scenario": "concurrent-save",
  "kind": "two-users",
  "method": "barrier-click",
  "summary": "Two users save one piece at once.",
  "expected": "Neither edit is lost silently.",
  "site_url": "http://127.0.0.1:5xxxx",
  "target": {
    "workspace_id": "...",
    "run_id": "...",
    "run_title": "QA seed hymn ...",
    "run_unshared_id": "...",
    "run_unshared_title": "QA seed hymn ...",
    "library_url": "http://127.0.0.1:5xxxx/library",
    "share_id": "...",
    "share_url": "http://127.0.0.1:5xxxx/s/..."
  },
  "consumes": ["share"],
  "barrier": {"dir": "<run>/shared/concurrent-save", "count": 2, "timeout_seconds": 540},
  "participants": [
    {
      "slot": "a",
      "tester": "adv-1",
      "identity": "director-a@choir.test",
      "role": "director",
      "workspace": "qa-a",
      "state_file": "<run>/state/director-a@choir.test.json",
      "sessions": 1,
      "action": "..."
    }
  ]
}
```

- `target` holds only what the scenario needs. `run_id`, `run_title`, `run_unshared_id`, `run_unshared_title`, `share_id`, and `share_url` appear only when the scenario uses them. `run_unshared_id` and `run_unshared_title` name the seeded run that has no share, for a scenario that needs `run_unshared`, such as `double-publish`.
- `barrier` is `null` unless `method` is `barrier-click`.
- Find your entry in `participants` by your tester name. Its `state_file` signs your session in (see `browser.md`), and `action` says what you do.
- `consumes` lists resources the scenario uses up, such as a share it revokes. Don't try to reuse one afterward.

## The barrier

The barrier is a folder of small files. Each participant writes an arrival file and waits until the last one arrives. Then every participant continues at once. Use it through one command:

```bash
bash "${SEBBY_ROOT:-$HOME/Developer/sebby}/claude/skills/qa-swarm/barrier.sh" wait <barrier.dir> <your tester name> <barrier.count> <barrier.timeout_seconds>
```

Run the barrier command exactly as your charter writes it. The form here, with `${SEBBY_ROOT:-$HOME/Developer/sebby}`, is only the fallback when a charter gives none. For the fallback, take the directory, count, and timeout from `target.json`'s `barrier`, and use your own tester name, the same one that `target.json` gives you.

The command exits with one of four codes:

- `0`: every participant arrived, and you can act now.
- `75`: the barrier abandoned, or you already ran the command. Someone didn't arrive before the timeout, another participant's timeout came first, a signal stopped the command, or the shell that would run your action was gone. If you already ran the command, a second run doesn't act: not after you acted, and not after the tool stopped the command after the release (the barrier leaves a `stopped` marker for your name). Record the scenario as abandoned, not passed.
- `64`: you mistyped an argument. The command creates nothing. Fix the command.
- `1`: `barrier.sh` can't write in the barrier folder.

Any code other than `0`, including `127` from a wrong path, means you didn't act: record the scenario as abandoned.

A participant that can't get into position before the timeout lets the barrier abandon. That's by design: a stuck participant must never leave the others waiting forever.

## Snapshot first, then one command

Take a snapshot and note the ref of the element you'll click before you run the barrier command. Then run the barrier and the click as one Bash command, chained with `&&`, so nothing runs between the release and the click:

```bash
cd <run>/testers/<me> && bash "${SEBBY_ROOT:-$HOME/Developer/sebby}/claude/skills/qa-swarm/barrier.sh" wait <barrier.dir> <me> <barrier.count> <barrier.timeout_seconds> && npm --prefix <cli folder> exec -- playwright-cli -s=<session> click <ref>
```

If the barrier exits `75`, the `&&` chain stops and the click never runs. That's what you want.

Get into position before you run the command, because the other participants are waiting: the page open, the snapshot taken, and the ref noted. Run the barrier command with the Bash tool's `timeout` set to 600000 (its maximum). If the tool stops the command, record the scenario as abandoned, and never re-run the barrier command: a second run would arrive at a barrier that has moved on. A stopped command abandons the barrier for everyone: it writes `abandoned` (unless another participant is already deciding the outcome), so the others exit `75` at once instead of waiting out their timeouts. A waiter whose shell dies does the same and never writes its `acted` entry. If a decider stalls, `barrier.sh spread` reports `pending`.

After you act, take a snapshot and run `playwright-cli requests`. Record what the page shows and what the requests were.

## The four methods

Your charter names the method. Timing is coarse: agent steps take seconds, so only the barrier and the in-page methods land actions close together. The project's timing test measures the barrier: read the runbook's "Barrier timing" table for the measured release and action spreads, and don't guess a number.

- **Barrier click.** Two or more participants each wait at the barrier and then click, as the barrier section shows. Use it for collisions between users or between one user's sessions.
- **Repeat click.** One participant clicks twice in one command: `dblclick <ref>`, or `click <ref> && click <ref>`. Use it for anything that saves, renders, or spends.
- **Parallel fetch.** One `eval` sends several requests from inside the page at once. It's the tightest race, and it reuses the page's own cookies. Use a request the page itself makes: do the action once in the page, find its request with `playwright-cli requests`, and open it with `request <index>`. Copy its method, URL, headers, and JSON body. Save the body as `body.json` in your folder, and build it into the eval string with `"$(cat body.json)"`: the substitution sits inside the eval string's double quotes, so the shell pastes the file in as one argument before the page runs the code. For example:

  ```bash
  cd <run>/testers/<me> && npm --prefix <cli folder> exec -- playwright-cli -s=<session> eval "async () => { const init = {method: '<method>', headers: {'Content-Type': 'application/json'}, body: JSON.stringify($(cat body.json))}; const r = await Promise.all([fetch('<url>', init), fetch('<url>', init)]); return r.map(x => x.status); }"
  ```

  Add any other header the request carried to `headers`. Never invent a request the page's own code doesn't make.
- **Parallel sessions.** One participant with two sessions runs both commands in the background, in one group after the `cd`, because `&&` binds tighter than `&`, and waits for each one by its process ID, so the group reports each exit code: `cd <run>/testers/<me> && { cmd1 & p1=$!; cmd2 & p2=$!; wait $p1; echo "a=$?"; wait $p2; echo "b=$?"; }`. Use it for two tabs of one user. A code other than `0` means that command didn't act.

## Measuring the spread

After you act, run the spread command from your own folder, so the guard attributes it, with the same `barrier.sh` path as your barrier command:

```bash
cd <run>/testers/<me> && bash "${SEBBY_ROOT:-$HOME/Developer/sebby}/claude/skills/qa-swarm/barrier.sh" spread <barrier.dir>
```

It prints one JSON line: `outcome` (`go`, `abandoned`, or `pending`), `count` (how many arrived), `released_count` (how many had arrived when the barrier released, or `null`), `acted` (each participant's return time in milliseconds), `spread_ms` (the gap between the first and last return), `clock` (the clock that timed the release, as the `go` file records it: `bash`, `perl`, `python3`, or `date`, or `null` without a `go` file), `skipped` (how many `acted` files it ignored for a bad name or value), and `stopped` (each participant a signal stopped after the release and before it acted, such as a tool timeout). Quote the line in every finding from a barrier scenario. A large spread means the collision wasn't tight, so say so. A `date` clock counts whole seconds, so its spread says little.

`spread_ms` is the release spread: how far apart the participants left the barrier. The action lands later, after `playwright-cli` starts and the click reaches the page, so the actions land further apart than `spread_ms` says. The runbook's "Barrier timing" table measures that action spread, and it's the real bound on how close the actions landed.

The outcome file and `spread` are authoritative, not one participant's exit code. A participant that times out waits a short grace period for the decider's outcome, and if the decider writes `go` only after that, the late participant has returned `75` while the others returned `0`. Trust `outcome`.

`released_count` must equal the number of `acted` entries. A mismatch goes one of two ways:

- A `released_count` greater than the number of `acted` entries means a released participant never recorded acting: a signal stopped it after the release (`stopped` names it), a SIGKILL stopped it (a SIGKILL can't be trapped, and `stopped` doesn't name it), or a failed write lost its `acted` entry.
- Fewer means a participant arrived after the release. It returned `0` and acted, but alone, so it isn't a real collision for that participant.

Either way, the orchestrator reports the scenario as "not a real collision", not as passed.

## Rules

- Never write in `shared/`. Only `barrier.sh` writes there, and only the files it owns.
- Don't run `barrier.sh` on a folder that isn't in your `target.json`.
- Close every session you open, and keep it in `sessions.json`, as `browser.md` says.
