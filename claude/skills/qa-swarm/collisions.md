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

- `target` holds only what the scenario needs. `run_id`, `run_title`, `share_id`, and `share_url` appear only when the scenario uses them, and `run_title` is `null` when the run predates title recording.
- `barrier` is `null` unless `method` is `barrier-click`.
- Find your entry in `participants` by your tester name. Its `state_file` signs your session in (see `browser.md`), and `action` says what you do.
- `consumes` lists resources the scenario uses up, such as a share it revokes. Don't try to reuse one afterward.

## The barrier

The barrier is a folder of small files. Each participant writes an arrival file and waits until the last one arrives. Then every participant continues at once. Use it through one command:

```bash
bash "${SEBBY_ROOT:-$HOME/Developer/sebby}/claude/skills/qa-swarm/barrier.sh" wait <barrier.dir> <your tester name> <barrier.count> <barrier.timeout_seconds>
```

Take the directory, count, and timeout from `target.json`'s `barrier`. Use your own tester name, the same one that `target.json` gives you, so a second run of the command counts once.

The command exits with one of three codes:

- `0`: every participant arrived, and you can act now.
- `75`: the barrier was abandoned. Someone didn't arrive before the timeout, another participant's timeout came first, or a signal stopped the command. Record the scenario as abandoned, not passed.
- `64`: you mistyped an argument. Nothing was created. Fix the command.

A participant that can't get into position before the timeout lets the barrier abandon. That's by design: a stuck participant must never leave the others waiting forever.

## Snapshot first, then one command

Take a snapshot and note the ref of the element you'll click before you run the barrier command. Then run the barrier and the click as one Bash command, chained with `&&`, so nothing runs between the release and the click:

```bash
cd <run>/testers/<me> && bash "${SEBBY_ROOT:-$HOME/Developer/sebby}/claude/skills/qa-swarm/barrier.sh" wait <barrier.dir> <me> <barrier.count> <barrier.timeout_seconds> && npm --prefix <cli folder> exec -- playwright-cli -s=<session> click <ref>
```

If the barrier exits `75`, the `&&` chain stops and the click never runs. That's what you want.

Get into position before you run the command, because the other participants are waiting: the page open, the snapshot taken, and the ref noted. Run the barrier command with the Bash tool's `timeout` set to 600000 (its maximum). If the tool stops the command, record the scenario as abandoned, and never re-run the barrier command: a second run would arrive at a barrier that has moved on. A stopped command withdraws its own arrival, unless the decider had already started, and a stalled decider leaves `barrier.sh spread` reporting `pending`.

After you act, take a snapshot and run `playwright-cli requests`. Record what the page shows and what the requests were.

## The four methods

Your charter names the method. Timing is coarse: agent steps take seconds, so only the barrier and the in-page methods land actions close together. The project's timing test measures the barrier's own spread: see the project's measurement, and don't guess a number.

- **Barrier click.** Two or more participants each wait at the barrier and then click, as the barrier section shows. Use it for collisions between users or between one user's sessions.
- **Repeat click.** One participant clicks twice in one command: `dblclick <ref>`, or `click <ref> && click <ref>`. Use it for anything that saves, renders, or spends.
- **Parallel fetch.** One `eval` sends several requests from inside the page at once. It's the tightest race, and it reuses the page's own cookies. For example:

  ```bash
  cd <run>/testers/<me> && npm --prefix <cli folder> exec -- playwright-cli -s=<session> eval "async () => { const r = await Promise.all([fetch('<url>', {method: 'POST'}), fetch('<url>', {method: 'POST'})]); return r.map(x => x.status); }"
  ```

  Put a long or odd request body in a file under your folder, and read it in the `eval`. Never invent a request the page's own code doesn't make.
- **Parallel sessions.** One participant with two sessions runs both commands in the background and waits, in one group after the `cd`, because `&&` binds tighter than `&`: `cd <run>/testers/<me> && { cmd1 & cmd2 & wait; }`. Use it for two tabs of one user.

## Measuring the spread

After the batch, or after you act, run:

```bash
bash "${SEBBY_ROOT:-$HOME/Developer/sebby}/claude/skills/qa-swarm/barrier.sh" spread <barrier.dir>
```

It prints one JSON line: `outcome` (`go`, `abandoned`, or `pending`), `count` (how many arrived), `acted` (each participant's return time in milliseconds), and `spread_ms` (the gap between the first and last return). Quote the line in every finding from a barrier scenario. A large spread means the collision wasn't tight, so say so.

## Rules

- Never write in `shared/`. Only `barrier.sh` writes there, and only the files it owns.
- Don't run `barrier.sh` on a folder that isn't in your `target.json`.
- Close every session you open, and keep it in `sessions.json`, as `browser.md` says.
