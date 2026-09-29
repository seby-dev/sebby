# sebby.browser: a browser test agent with multi-actor collisions, replay, and oracles

## Problem

The QA swarm (staff2solfa, `2026-09-29-qa-swarm-design.md`) runs its
functional scenarios through executor v0, a vendored copy of jev-ultrafast.
jev works: it reads the page through one atomic DOM snapshot, asks TypeSafe
`system_one` to pick one operation from a small fixed set, and executes it in
code. Its limits now block the swarm:

- **One actor per process.** The swarm's multi-actor scenarios, such as a
  Director saving while a singer opens a share link, return `unsupported`
  from executor v0. The swarm reports them as a coverage gap.
- **No file uploads.** The snapshot excludes file inputs by design.
- **Every run pays full price.** A passing scenario replays every model call
  on every run. Nothing is recorded, so nothing is reused.
- **No checks beyond the page.** Nothing reads application state, and nothing
  watches for a 5xx response or an uncaught console error between steps.
- **A borrowed browser stack.** `browser_harness` needs a daemon, and its
  default mode drives the developer's own Chrome. The swarm has to override it
  with environment variables to get isolation.
- **A second TypeSafe client.** jev calls `api.typesafe.ai` through raw
  `httpx`, while sebby already has a supported path through `typesafe-sdk`.

Octomind, a hosted test-agent service, shut down in May 2026. That is a
reason to own the executor instead of renting one.

This spec designs `sebby.browser`, an LLM browser test agent behind a new
optional extra, `sebby[browser]`. Its step engine is jev-ultrafast, forked:
the snapshot, the questions, the choose-and-validate logic, and the step loop
are copied from jev and extended. The layers around the engine are new.
staff2solfa installs the package by git tag and plugs it in behind the
swarm's executor interface, in place of the vendored jev.

## Claim and prior art

The claim is modest: `sebby.browser` combines four things that a survey of
existing tools found only in part elsewhere.

1. Multi-actor scenarios with collision points as a first-class concept.
2. Record, replay, and heal, for tests with assertions.
3. Domain-state oracles that check the application's own data.
4. Invariants checked on every step.

What the survey found, and where this design differs:

| Tool | What it does | Difference |
|---|---|---|
| Stagehand (`cacheDir`) and Skyvern (`run_with="code"`) | Record, replay, and fall back for task automation. | They automate tasks. They have no assertions, so a heal can't be told apart from a regression. |
| Playwright's healer | Repairs the test file after a run. | It repairs between runs. `sebby.browser` heals during a run, and reports every heal. |
| MAdroid (ICSE 2026) | Coordinates several agents on one app, the closest work to collision points. | It's Android-only. |
| Shortest | Callbacks can check more than the DOM or pixels. | It has no multi-actor scenarios and no replay. |

No surveyed tool treats a collision point as a first-class concept. This spec
doesn't claim the combination is novel beyond that survey.

The step engine isn't part of the claim. It's jev-ultrafast, forked (see
"The jev fork"). The claim concerns the layers around it: the multi-actor
runtime, the trace store, the oracles, and the invariants.

## Goals

- Run a test written as a natural-language goal, or as a scripted step, in
  headless Chromium, with one decision per step from a fixed operation set.
- Run several actors in one scenario, and make two or more actions land
  within a measured, reported gap (a collision).
- Record each scenario as a JSON trace committed next to it. Replay the trace
  with no model call. Heal a drifted step during the run, and report it.
- Check invariants after every step, and domain state through plugins.
- Plug in behind the swarm's executor interface, and declare multi-actor
  support.
- Fork jev's step engine and change it in small, named steps, so the diff
  against upstream shows every change.
- Keep the dependency set small: `typesafe-sdk` (already in the `judgement`
  extra), `sebby[llm]` for the text model, and one WebSocket library.

## Non-goals

- No staff2solfa-specific code. staff2solfa's music oracle is a plugin it
  supplies; this spec designs only the plugin contract.
- No visual or pixel-based decisions. The agent decides from the DOM
  snapshot. Screenshots are evidence for people, and no model reads them.
- No cloud browsers, and no use of the developer's own Chrome.
- No mobile or non-Chromium browsers.
- No test-generation agent. A person or the swarm's `qa-tester` writes the
  scenario. This spec covers the executor and the runtime.

## Terms

- **Actor:** one user, tab, or device in a scenario, with its own browser.
- **Cast:** the named actors of one scenario.
- **Step:** one operation an actor performs, recorded in the trace.
- **Goal:** a natural-language instruction that an actor pursues over several
  steps until the model returns `DONE`.
- **Decider:** the component that picks the next operation and target.
- **Trace:** the JSON record of one scenario's steps, stored next to it.
- **Heal:** a step or goal remainder that the run resolved differently from
  the recording, either by a model call or by a lower rung of the locator
  ladder after a collision.
- **Refresh:** a step that resolved through a lower rung of the ladder
  because a higher rung matched nothing. A refresh isn't a heal.
- **Collision:** two or more actions, from different actors, released
  together with a measured gap between them.
- **Oracle:** a plugin that reads application state and answers questions
  about it, such as "what is alto bar 3 now?"
- **Invariant:** a check that runs after every step and doesn't depend on the
  scenario, such as "no 5xx response."

## The jev fork

The step engine is jev-ultrafast, forked. `sebby.browser` doesn't redesign
what jev proved: one atomic DOM snapshot, one operation chosen from a fixed
set by TypeSafe `system_one`, then execution in code. jev is MIT-licensed, so
the fork copies its code and extends it. What's new is the layers around the
engine: the isolated browser pool, the trace store, the multi-actor runtime,
and the oracles.

**Source and license.** The source is
`tests/ui_jev/vendor/jev_ultrafast/` in staff2solfa, which
`tests/ui_jev/vendor/VENDORED.md` there records as a verbatim copy of
`https://github.com/browser-use/jev-ultrafast` at commit
`452c1ad2dd628008f1d5608f28158d76e49e6cc0`. The fork ships upstream's MIT
license, "Copyright (c) 2026 Browser Use," as
`src/sebby/browser/LICENSE.jev-ultrafast`, which the license requires it to
keep with the copied code. `src/sebby/browser/FORKED.md` records the upstream
repository and commit, the list of forked files with the SHA-256 of each
verbatim upstream file, the license file, and the rule for changes (below).

**Where each piece goes.** All destinations are in `src/sebby/browser/`, and
the forked modules keep jev's file names, so a diff against upstream stays
readable.

| jev source | Destination | What happens |
|---|---|---|
| `snapshot.js` | `snapshot.js` | Forked and extended: the locator ladder, and file inputs as `upload` elements. |
| `questions.py` | `questions.py` | Forked and extended: `NEXT_ACTION` describes `UPLOAD` and `ASSERT`. |
| `model.py`: `action_space`, `validate_choice`, `field_context` | `model.py` | Forked. `action_space` is extended, and the other two are kept. |
| `model.py`: `choose` | `model.py` (build and interpret), `decider.py` (the call) | Forked and split. The call moves onto `typesafe-sdk`. |
| `model.py`: `field_text` | `model.py` | Body replaced by an Anthropic call. The signature is kept. |
| `model.py`: `post_json`, `CLIENT` | none | Deleted. The SDK replaces them. |
| `agent.py` | `agent.py` | Forked and extended: hooks, `UPLOAD` and `ASSERT`, async. |
| `browser.py` | `browser.py` | **Replaced.** See the following paragraph. |
| `__init__.py` | `__init__.py` | Replaced by this package's public API. |

**The one replaced file.** `browser.py` is the only jev file that the pool
replaces. Its `ensure_daemon()`, its `browser_harness` calls, and its target
on the developer's real, already-running Chrome are gone, and the isolated CDP
pool (layer 1) takes their place. The `Browser` class and its `observe`,
`fresh`, and `act` methods stay as the thin driver over one pool page, with
jev's logic for each ported over the pool's `call`: the settle script in
`observe`, `fresh`, `fingerprint`, and the hit test and input sequence in
`browser_operation`. `StalePage` stays as jev has it.

**Change record.** The first commit in slice A is the verbatim copy of the
five files and the license, and it adds `FORKED.md`. It exempts the copied
files from `sebby`'s ruff and mypy gates, because jev's style (120-character
lines, no types) doesn't meet them. The second commit is a mechanical
reformat and typing pass with no behavior change. After that, each change is
its own commit, and its message names the jev function, for example
`fork: split choose() into build, call, and interpret`. So `git diff` from the
first commit shows every change made to jev, and a reader can ignore the
second commit's whitespace.

### What changes and what stays

This table lists every jev function and constant, and its fate in the fork.

| jev function | Fate | Change |
|---|---|---|
| `Agent.__init__` | Changed | Takes a pool page and no URL, and runs one goal per call. |
| `Agent.snapshot` | Verbatim | |
| `Agent.command` (`tick`) | Verbatim | Structure kept; becomes `async`. |
| `Agent.command` (`predict`) | Changed | Asks a `DecisionSource` instead of calling `choose`. |
| `Agent.command` (`act`) | Extended | Hooks, `UPLOAD` and `ASSERT` branches, text sources, invariants, `prepare` and `dispatch`. The `DONE` and `BLOCKED` branch, the budget check, and the stall rule stay as they are. |
| `Agent.run` | Verbatim | Becomes an async generator. |
| `Agent.close`, `__enter__`, `__exit__` | Changed | Async; close the page and not a whole browser. |
| `StalePage` | Verbatim | |
| `Browser.__init__` | Replaced | The pool opens and configures the page. |
| `Browser.call`, `Browser.evaluate` | Verbatim logic | Route through the pool's `call`, which now has a timeout. |
| `Browser.observe` | Extended | Settle script kept; adds the `settle_timeout` record and the dialog and popup hooks. Async. |
| `Browser.fresh` | Verbatim logic | Async. |
| `Browser.act`, `browser_operation` (act branch) | Extended | Split into `prepare` (freshness and hit test) and `dispatch` (the input events). |
| `Browser.close` | Replaced | Closes the page target through the pool. |
| `fingerprint` | Verbatim | Kept for the stall rule. |
| `browser_operation` (observe branch) | Verbatim logic | Async. |
| `validate_choice` | Verbatim | Reads attributes of the SDK's answer object. Both response checks stay. |
| `action_space` | Extended | Adds `upload` and `assert` kinds. |
| `choose` | Split | `build_questions` and `interpret` verbatim; the call moves to the SDK. |
| `field_context` | Verbatim | |
| `field_text` | Body replaced | Anthropic, Haiku 4.5. Same signature and return value. |
| `post_json`, `CLIENT` | Deleted | The SDK replaces them. |
| `NEXT_ACTION` | Extended | Two added sentences. |
| `TARGET`, `TEXT_VALUE`, `MAX_STEPS` | Verbatim | |
| `snapshot.js` | Extended | Ladder, file inputs. The rest verbatim. |

### Where the fork isn't an extension

Most changes add to jev's code. These don't, and each has a reason.

- **Async.** jev's `Agent` is synchronous: `Agent.run` is a generator that
  blocks on `browser_harness` calls, and `Browser` sleeps with `time.sleep`.
  The runtime runs every actor as an asyncio task in one process and releases
  a collision through an `asyncio.Barrier`, so a blocking step loop can't
  share the loop. The conversion is mechanical: `def` becomes `async def`,
  each call into the browser, the decider, or a hook takes `await`,
  `time.sleep` becomes `asyncio.sleep`, and `run` becomes an async generator.
  It touches every function in `agent.py` and `browser.py` without changing
  their logic.
- **One goal per `Agent`, and the browser is passed in.** `Agent.__init__`
  builds its own `Browser(url)` and takes one task. A scenario gives an actor
  several goals over the actor's lifetime on one browser that the pool owns.
- **The atomic act.** `browser_operation`'s `act` branch does the freshness
  check, the hit test, and the input events in one call. A collision has to do
  the first two, hold, and then do the third. The fork splits it at the
  `elementFromPoint` check.
- **The call in `choose`.** A raw HTTP call can't go behind the `Decider`
  protocol. The split of `choose` keeps the surrounding lines verbatim.
- **Popups.** jev holds one target. With the `follow` popup policy, the pool
  can change an actor's current page, so `Browser` reads its page from the pool
  on each call and doesn't cache a session ID.
- **The page hash.** jev's `fingerprint` is an exact SHA-256 that includes
  body text. Replay needs a closeness score over normalized elements, so
  `hashing.py` is new code beside `fingerprint`, not an edit to it.

## Architecture

Five layers. Each has its own tests, and a layer can be tested with fakes
for the others. Layers communicate through small interfaces and hooks: the
pool exposes hooks for dialogs, popups, and crashes, and the engine and the
runtime register handlers for them. A lower layer never imports a higher one.

Layer 2 is the jev fork. The other four layers are new.

```text
5. Oracles        invariants after every step; domain oracles by entry point
4. Scenario       @scenario, cast(), actor moves, collide(), pytest plugin
3. Trace store    <scenario>.trace.json; replay and heal rules
2. Step engine    snapshot -> Decider -> execute; operation set; text model
1. Browser pool   one Chromium per actor over CDP; capture; allowlist; teardown
```

The executor adapter sits beside layer 4 and drives the same layers through
`run_goals`.

### Layer 1: browser pool

The pool starts and owns one headless Chromium per actor, and drives it
directly over the Chrome DevTools Protocol (CDP). It replaces jev's
`browser.py`: there's no `browser_harness` daemon, and no use of the
developer's own Chrome.

**Launch mode.** Used by the pytest runtime. For each actor the pool:

- Starts `chromium` with `--headless=new`, its own `--user-data-dir` under a
  run directory, its own `--remote-debugging-port=0`, and the allowlist flags
  from `chromium_flags(allowed_hosts)` (see "Host allowlist"). It reads the
  chosen port from `DevToolsActivePort` in the profile directory.
- Starts the process with `start_new_session=True`, so the browser and its
  helpers share one process group that the pool can signal as a group.
- Loads the actor's storageState (cookies plus `localStorage`). Slice C
  builds this; see "Slices." Cookies go through `Network.setCookies`.
  `localStorage` goes through `Page.addScriptToEvaluateOnNewDocument`: a
  seeding script that runs before the page's own scripts and writes each
  entry only when `location.origin` matches the entry's origin.

**Attach mode.** Used by the executor adapter. The swarm's plugin launches
the browsers, with the flags that `Executor.launch_flags(allowed_hosts)`
returns, loads each state file, and passes each actor a `cdp_url`. The
interface says an executor "connects to an endpoint and never starts or
shares a browser." In attach mode the pool connects to that URL, opens its
own target, and closes only that target on exit. It doesn't load
storageState, doesn't kill the process, and installs no signal handlers.

Both modes present the same `ActorBrowser` interface to layer 2:

```python
class ActorBrowser(Protocol):
    name: str
    async def call(
        self, method: str, *, timeout: float | None = None, **params: Any
    ) -> dict[str, Any]: ...
    def send_nowait(self, method: str, **params: Any) -> None: ...  # no reply awaited
    async def evaluate(self, expression: str) -> Any: ...
    async def screenshot(self) -> bytes: ...
    def observations(self, since: int) -> Observations: ...  # console + network
    async def set_input_files(self, object_id: str, files: Sequence[Path]) -> None: ...
    async def close(self) -> None: ...
```

**CDP client.** A small client, `sebby.browser.cdp`, wraps one WebSocket
connection per browser: a request ID counter, a future per pending call, an
event dispatcher keyed by method and session ID, and flattened sessions for
child targets. Its settings:

- `max_size` is 64 MiB. The library's default of 1 MiB closes the connection
  on a large screenshot or snapshot result.
- `ping_interval` is `None`. Chromium's DevTools server doesn't need
  keepalive pings, and a missed pong would drop a healthy connection.
- Every `call` has a timeout, 30 seconds by default, with a per-method
  override for navigation. A timeout raises `CdpTimeout`, which the step
  engine reports as `error`.
- `send_nowait` writes a command without creating a future. The collision
  path uses it (see layer 4).

**Crashes and lost targets.** The pool listens for `Inspector.targetCrashed`,
`Target.detachedFromTarget`, and the WebSocket closing. Any of them fails
every pending call on that actor with `TargetGone`, marks the actor dead, and
ends the run as `error` after teardown. The evidence folder keeps the
last observation window.

**Dialogs.** The pool listens for `Page.javascriptDialogOpening` on every
page. The dialog policy covers `alert`, `confirm`, `prompt`, and
`beforeunload`. By default the pool dismisses every dialog
(`Page.handleJavaScriptDialog` with `accept=False`). It records each one as a
`DIALOG` step (type, normalized message, action taken) and as a `dialog`
finding. A dismissed `beforeunload` cancels its navigation, and the finding
says so. A scenario overrides the policy per type, for example
`dialogs={"confirm": "accept", "beforeunload": "accept"}`. A dialog that the
scenario's policy names explicitly produces a finding of severity `info`
and doesn't fail the run. Any other dialog is an `error` finding. The pool
closes targets with `Target.closeTarget` and applies the dialog policy to
any dialog that opens while it closes them.

**Popups.** `target=_blank` and `window.open` create a new target, which
`Target.setAutoAttach` reports with its `openerId`. The popup policy is
`follow` by default: the new page becomes the actor's current page, passes
through the same allowlist and navigation guard, and the step records
`opened_tab: true`. Closing it returns the actor to the opener. The policy
`block` closes the popup and records a `popup` finding. A scenario chooses
per actor.

**File uploads.** A file input takes files through `DOM.setFileInputFiles`,
which accepts the element's `objectId` directly, so the pool needs no
`backendNodeId` lookup. File inputs are often visually hidden, so the
snapshot lists them regardless of visibility (see layer 2). A drop-only zone
has no file input. For that case a directed step, such as
`actor.upload("Drop zone", files=["score"], via="drop")`, sends
`Input.dispatchDragEvent` events (`dragEnter`, `dragOver`, `drop`) with the
files in the drag data. The model chooses uploads only on file inputs, and the
scenario picks a drop zone. `showOpenFilePicker` (the File System Access API)
isn't supported: a page that calls it needs a file input or a drop zone
instead. Only files a scenario declares (`files={"score": Path(...)}`) can be
uploaded. The model picks a declared name, never a path.

**Capture.** After `Runtime.enable`, `Log.enable`, and `Network.enable`, the
pool keeps a bounded buffer per actor of:

- `Runtime.consoleAPICalled` (levels `error` and `warning`),
  `Runtime.exceptionThrown`, and `Log.entryAdded`.
- `Network.requestWillBeSent`, `Network.responseReceived`, and
  `Network.loadingFailed`, joined by request ID into one record with the URL,
  method, status, `wallTime`, and the monotonic times of the request and the
  response.
- `Network.webSocketCreated`, with the WebSocket's URL.

Each buffer entry carries a sequence number. A step reads
`observations(since=seq)` to get the entries from its own window.

**Host allowlist.** The pool enforces the allowlist. A scenario's
instructions to the model can't. An allowlist entry is a `host:port` pair. A
bare host means the default ports for its scheme (80 and 443). The default
allowlist is the scenario's base-URL origin, plus `gleitz.github.io` and
`smpldsnds.github.io`, the two hosts that staff2solfa's browser code fetches
sounds from (the swarm spec lists both under "Hosts and network").

Two facts shape the design. The `--host-resolver-rules` flag works on
hostnames only, so it can't tell `127.0.0.1:5173` from another loopback port.
The `Fetch` domain doesn't pause a WebSocket handshake. So four layers apply,
and a failure in one doesn't disable the others:

1. **A PAC file, the primary block.** `chromium_flags(allowed_hosts)` returns
   `--proxy-pac-url` and `--proxy-bypass-list=<-loopback>`. The bypass list
   removes Chromium's implicit loopback bypass, so the PAC file sees loopback
   requests too. The PAC file's `FindProxyForURL` returns `DIRECT` for an
   allowlisted `host:port` and a proxy address that refuses connections for
   everything else. Because it decides on host and port, it blocks another
   loopback port, an IP-literal URL, and a WebSocket. Chromium also applies it
   to workers and to `sendBeacon`, prefetch, and speculation-rules requests.
   The function also returns `--host-resolver-rules` as a hostname-only
   backstop. The swarm's plugin adds these flags when it launches a browser
   (see "Executor adapter"), and launch mode adds them itself.
2. **Browser-level auto-attach.** The pool connects to the browser target and
   calls `Target.setAutoAttach` there (`flatten=True`,
   `waitForDebuggerOnStart=True`), so it sees every page, iframe, worker, and
   service worker. On each child it sends `Network.enable` and, if
   interception is on, `Fetch.enable`, and then sends
   `Runtime.runIfWaitingForDebugger`. That order means no request from the
   child leaves before the pool is watching.
3. **Request interception and findings.** Every disallowed request that
   Chromium blocked appears as a `Network.loadingFailed` event, and the
   `host_allowlist` invariant turns it into a finding. `Network.webSocketCreated`
   for a disallowed URL raises the same finding, because `Fetch` can't see it.
   When `Fetch` interception is on, the pool answers a `Fetch.requestPaused`
   for a disallowed host with `Fetch.failRequest` (`BlockedByClient`).
4. **Navigation guard.** The engine refuses a `Page.navigate` to a
   disallowed origin before it sends the command. `file:`, `data:`, and
   `blob:` navigations are refused by default.

**Interception cost.** A `*` pattern makes `Fetch` pause every request and
wait for the pool's answer, which adds a round trip per request. A Vite
development server sends hundreds of module requests per page load. So the
PAC file is the primary block, and `Fetch` interception is a secondary layer.
At startup the pool runs a canary: it requests a disallowed loopback port and
expects the request to fail. If the canary fails as expected, the PAC file is
active, and `Fetch` stays off. If the flags are missing (an attach-mode
browser that the plugin launched without them), the pool turns `Fetch` on and
reports that the allowlist runs in the weaker mode. Slice A measures page-load
time against a Vite development server and against a production build, with
and without `Fetch`, and records the numbers in the report.

**Teardown.** Teardown runs on every exit path in launch mode:

- The pool is an async context manager, and a `finally` block closes it.
- An `atexit` hook and `SIGINT` and `SIGTERM` handlers call a synchronous
  `kill_all()` that sends `SIGTERM` to each process group, waits two seconds,
  and then sends `SIGKILL`. Each handler chains to the handler that was
  installed before it, and restores it afterward, so it doesn't replace
  pytest's or the host's own handling.
- The pool writes a pidfile per browser to the run directory. The pidfile
  holds the group ID, the leader's PID, the leader's start time, and the
  expected `--user-data-dir`. At the next start, a sweep signals a group only
  if the leader's current start time and command line (read with
  `ps -o lstart=,command= -p <pid>`) match the pidfile and the command line
  contains a `--user-data-dir` under the run folder. A recycled PID or an
  unrelated process therefore isn't signaled.
- The pool removes each `--user-data-dir` it created, after the process
  exits.

In attach mode the pool owns no processes, so it installs no handlers and
runs no sweep. It closes only the targets it opened.

### Layer 2: step engine

The step engine is the fork of jev-ultrafast described in "The jev fork."
This section describes what the engine does and names, for each part, the
jev function it comes from and how the fork changes it.

**Snapshot (fork of `snapshot.js`).** One `Runtime.evaluate` call returns
everything the step needs, so the page can't change between reads. jev's
script already returns:

- The URL, title, and up to 6,000 characters of visible text.
- Up to 250 actionable elements, each with a role, an accessible name
  (`aria-labelledby`, `aria-label`, labels, then text, then `title`, then
  `placeholder`), state (`checked`, `expanded`, `selected`, `disabled`), and a
  current value.
- A stable node handle, kept in a page-side `WeakMap` (`window.__jevFast`),
  so an execute step reaches the same element the snapshot described.
- A per-element guard (`cache.guard`): a digest of the element's state and its
  enclosing form or row. The engine recomputes it just before acting, and a
  mismatch raises `StalePage`.
- A `marker` and a `page_key` for freshness checks.
- Excluded: password and hidden inputs (their values never reach the model).

The fork changes the script in three places, and leaves the rest verbatim:

- **The locator ladder.** For each element, the script adds a `ladder` to the
  action it already builds: a test ID (`data-testid`, or a configured
  attribute), the role plus accessible name it already computes, nearby text,
  and the position among siblings (a path of `nth-of-type` steps). Layer 3
  needs it, and capturing it here lets recording and replay share one code
  path. The `guards`, `marker`, and `page_key` computations don't read it, so
  freshness behaves as in jev.
- **File inputs.** jev's `safe()` filter drops `file` inputs. The fork lets a
  file input through with kind `upload` and no value, and skips the
  `visible()` test for it, because file inputs are often visually hidden. The
  filter still drops `password` and `hidden`.
- **Nothing else.** The visibility test, the accessible-name function, the
  role table, the 250-action cap, the scroll and wait controls, and the text
  walk stay as they are.

Every page also runs with `Emulation.setFocusEmulationEnabled`, as jev's
`Browser.__init__` does, so animation frames and menus render in a background
tab. The pool applies it, because the pool now owns page setup.

**Operation set (extends `model.action_space`).** jev's set, plus two.
`action_space` already maps each action `kind` to an operation and builds one
target question per operation, so the fork adds two kinds and their
operations:

| Operation | Meaning | Chosen by |
|---|---|---|
| `CLICK` | Click an element, menu option, or suggestion. | Decider |
| `TYPE_TEXT` | Replace the text in an editable field. | Decider picks the field; `field_text` or the scenario supplies the text. |
| `SELECT` | Choose an option in a `<select>`. | Decider |
| `SCROLL` | Scroll by a fixed amount. | Decider |
| `WAIT` | Wait for the page to settle. | Decider |
| `UPLOAD` | Attach a declared file to a file input. | Decider picks the input and the declared file name. |
| `ASSERT` | Check a predicate on the page. | Decider, only for a check that takes a value; the scenario can add any check. |
| `DONE` | Every requirement is visibly met. | Decider |
| `BLOCKED` | No supported operation can progress. | Decider |

`ASSERT` is deterministic once recorded. It has a check kind from a fixed
list (`visible`, `text_equals`, `text_contains`, `value_equals`, `checked`,
`enabled`, `url_matches`), an optional target, and an expected value. Two
rules keep the model from inventing what to verify:

- The model can choose `ASSERT` only for a check kind that takes a value
  (`text_equals`, `text_contains`, `value_equals`, `url_matches`). Kinds that
  take no value (`visible`, `checked`, `enabled`) come from the scenario's
  own code, such as `actor.assert_visible("Saved")`.
- The expected value must appear in the goal text as a quoted literal (single
  or double quotes) of at least three characters. If it doesn't, the engine
  treats the choice as `BLOCKED`.

The recorded predicate is what replay evaluates. `questions.py` changes to
match: `NEXT_ACTION` gains two sentences that describe `UPLOAD` and `ASSERT`,
and `TARGET` and `TEXT_VALUE` stay verbatim.

**Decider protocol.** The decision engine sits behind a small interface. A
`Decision` is a typed view of the dict that jev's `choose` already returns
(`choice`, `operation`, `target`, `confidence`, `probabilities`, `usage`,
and the raw answers), with three added fields:

```python
@dataclass(frozen=True)
class DecisionRequest:
    goal: str
    page: PageView                  # url, title, text, elements: jev's `state`
    history: Sequence[StepSummary]  # the last 10 steps, as jev sends them
    allowed: frozenset[Operation]
    files: Sequence[str]            # declared upload names
    observe: Callable[[], Awaitable[PageView]]   # a fresh snapshot, for replay

@dataclass(frozen=True)
class Decision:
    operation: Operation
    target: str | None              # an element index from the request
    file: str | None                # a declared upload name, for UPLOAD
    check: CheckKind | None         # for ASSERT
    expected: str | None            # for ASSERT: a quoted literal from the goal
    confidence: float
    probabilities: Mapping[str, float]
    usage: Usage                    # tokens and attempts, for the ledger

class Decider(Protocol):
    async def decide(self, request: DecisionRequest) -> Decision: ...
```

Replay is a `Decider` too. `ReplayDecider` resolves the recorded step against
the current snapshot and returns the decision that step encodes, and it holds a
live `Decider` to call for a heal (layer 3). The step loop doesn't know
whether a decision came from the model or the trace.

**Default decider: `choose` moves onto the SDK.** jev's `model.py` calls
TypeSafe through raw `httpx`: `post_json` posts to `/v1/systemone`, with its
own retry loop. The fork moves the call onto `typesafe-sdk`, behind
`TypeSafeDecider`, and doesn't keep the raw `httpx` path. The reasons:

- sebby already has a supported path (`sebby.judgement`), and the SDK's API and
  its retry header have been verified against the installed 0.6.0 package.
- The `VENDORED.md` in staff2solfa accepted two client paths only because the
  files were kept verbatim. A fork gives up that constraint.
- The SDK accepts jev's question dictionaries as they are (`QuestionModel`),
  so the code that builds them doesn't change.

The cost is that `model.py` no longer diffs cleanly against upstream. The
fork limits it by splitting `choose` into three pieces and leaving the first
and third verbatim:

1. `build_questions(state, goal, history)`: every line of `choose` from the
   start through the `body` dict, unchanged, returning the questions, the
   `state`, and the operation, target, and control tables.
2. The call: `client.system_one(state, questions, model=...)` on an
   `AsyncTypeSafeClient` from `sebby.judgement.make_client(api_key=...,
   client_cls=AsyncTypeSafeClient)`. This replaces the `post_json` line and
   the `os.environ["TYPESAFE_API_KEY"]` read. The model comes from
   `SEBBY_BROWSER_MODEL`, with `jev-latest` as the default that jev and
   `make_client` share.
3. `interpret(...)`: every line after the call, unchanged except that answer
   fields are read as attributes of the SDK's `ChoiceAnswer` and not as
   dictionary keys.

`validate_choice` stays and runs on every answer. It includes jev's two
response checks that the SDK doesn't perform: the probability keys equal the
offered labels, and the chosen label has the highest probability. A response
that fails a check is an error, and no action runs. For `ASSERT`, the call
adds `check` and `expected` questions, built by the extended `action_space`.

`TypeSafeDecider` reports usage through `sebby.judgement.record_usage`, so a
run's TypeSafe spend flows through the same `UsageRecord` callback as
`sebby.llm`.

**Retry accounting.** The SDK's default policy retries twice, within a
30-second budget, on HTTP 408, 429, and 5xx. It replaces `post_json`'s own
loop, which retried three times on 429, 503, and 529. The SDK sets an
`X-TypeSafe-Retry-Count` header on each retried attempt (`transport.py`,
`attempt`). The decider reads the header from
`response.raw_http_response.request.headers`, treats a missing header as zero,
and records `1 + count` attempts in `Usage`. The ledger counts each attempt as
one billed call, which is the conservative reading. An SDK error that
survives its retries (`TypeSafeAPIError` and its subclasses) ends the step as
`error`.

**Text model (replaces the body of `field_text`).** `TYPE_TEXT` needs a value.
The value comes from one of three places, in this order:

1. A literal in the scenario (`actor.type("Key", "G")`): no model call.
2. A literal in the goal text, when the goal quotes it.
3. `field_text(context)`.

`field_text` keeps jev's signature, `field_text(context) -> (text, helper)`,
and its contract: it returns the text and a `helper` dict with `model`,
`latency_ms`, and `usage`, and it raises `ValueError` when the model returns
no valid value. Its body changes. jev's DeepSeek call, with its `TEXT_MODEL_*`
environment variables and `post_json`, becomes an Anthropic call: Claude
Haiku 4.5 (`claude-haiku-4-5-20251001`) through `sebby.llm.LLMClient`. The
model ID is a setting, `SEBBY_BROWSER_TEXT_MODEL`, and the key comes from
`ANTHROPIC_API_KEY` in the environment the host provides. The prompt
(`TEXT_VALUE`), the context (`field_context`), and jev's validation of the
reply (exactly one key `text`, a nonempty string of at most 2,000 characters)
stay verbatim. Implementers load the `claude-api` skill before they write this
code.

`LLMClient` is synchronous, and `field_text` stays synchronous with it. The
step loop calls `await asyncio.to_thread(field_text, context)`, so a model call
never blocks the event loop that the other actors share. A host can pass any
other callable with the same signature. When `field_text` raises, the step
becomes `BLOCKED`. The engine never guesses.

**Keys belong to the host.** `sebby.browser` reads `TYPESAFE_API_KEY` and
`ANTHROPIC_API_KEY` from its environment and nothing else. It doesn't open a
`.env` file, and it doesn't look in another project. This follows a lesson
from jev, whose key handling was an ad hoc step in each harness. The host
decides where a key lives: staff2solfa reads the TypeSafe key from another
project's `.env` by reference, and passes it to the process that runs the
executor.

**Step loop (fork of `agent.py`).** `Agent.command` already runs a goal as
`tick`, which is `predict` then `act`, until `DONE`, `BLOCKED`, or a cap. The
fork keeps that structure and the caps (`MAX_STEPS` of 60 actions and twice
that in decisions, the stall rule) and changes these points:

1. `predict` snapshots the page if it isn't fresh, then asks a
   `DecisionSource` for a decision. The source is a live `Decider` or a
   `ReplayDecider`. In jev this line calls `choose` directly.
2. `act` keeps its order: consume the decision once, handle `DONE` and
   `BLOCKED`, look up the action, check the budget, generate text for a
   `fill`, execute, record, and observe again. The fork adds:
   - a `before_step` hook after the decision and an `after_step` hook after
     the step record is appended, which the trace store uses to record and
     to compare hashes;
   - `UPLOAD` and `ASSERT` branches beside the `fill` branch;
   - the three text sources listed earlier, in front of the
     `field_text` call;
   - a call to the invariants after the second `observe`, with the step's
     observation window.
3. Execution is split in two, `prepare` and `dispatch` (see "Where the fork
   isn't an extension"), so a collision can hold a prepared command.
4. The settle rule is jev's, in `Browser.observe`: after an action it waits
   for two animation frames, or for a timer that ends the wait at 50 ms (200
   ms for an autocomplete). The timer is the timeout, so a page that never
   paints doesn't stall a step. The fork records `settle_timeout` on the step
   when the timer and not the frames ended the wait. It's not a failure.
5. Late content: `ReplayDecider` calls `request.observe` to snapshot again
   every 100 ms for up to 2 seconds before it classifies a target as zero or
   several (layer 3). Live decisions don't wait.

A goal also stops after three consecutive steps that leave the page
unchanged, which is jev's stall rule. jev's `fingerprint` (a SHA-256 of the
URL, text, actions, and scroll) stays for this rule. It's exact and includes
body text, so it isn't the page hash that layer 3 uses.

**Directed steps.** `actor.click("Save")` is a one-step goal. The engine
first tries to resolve the target by role plus accessible name against the
snapshot. If exactly one element matches, no model call occurs. If none or
several match, the Decider picks within the `CLICK` operation. This is a new
function beside `Agent`, and it calls the fork's `prepare` and `dispatch`.

### Layer 3: trace store

**File.** One JSON trace per scenario, next to the scenario module, named
`<scenario_id>.trace.json`. For a parametrized scenario, the parameter ID
joins the name: `<scenario_id>.<param_id>.trace.json`, with the parameter ID
reduced to letters, digits, dots, hyphens, and underscores. It's committed and
reviewed in a diff. The scenario ID defaults to the test function name, and
the plugin refuses two scenarios with one ID in the same directory. The file
holds no timestamps, latencies, or confidence values. When the app and the
browser are unchanged, rewriting a trace after a replay produces a
byte-identical file. A new recording through the model can differ, because
the model can choose differently. Timing goes into the run report.

Every trace carries `schema_version`. The reader refuses a major version it
doesn't know, and runs registered migrations for older ones. An unreadable or
unknown trace is an `error`, not a silent re-record.

An example, abbreviated:

```json
{
  "schema_version": 1,
  "scenario": "director_saves_while_singer_opens_share",
  "recorded_with": {
    "sebby": "0.4.0",
    "chromium": "141.0.7390.54",
    "decider": "typesafe:jev-latest"
  },
  "pages": {
    "9f2c41ab": ["a1b2c3d4", "0f9e8d7c", "…"],
    "41ab77e0": ["a1b2c3d4", "5c6d7e8f", "…"]
  },
  "actors": {
    "director": {
      "goals": [
        {
          "id": "director#0",
          "text": "open the piece 'Adeste' and change alto bar 3 to s f m",
          "verified_by": ["oracle:piece"],
          "steps": [
            {
              "n": 1,
              "op": "CLICK",
              "target": {
                "testid": null,
                "role": "link",
                "name": "Adeste",
                "near_text": "Library",
                "path": "main>ul>li:nth-of-type(2)>a"
              },
              "found_by": "role_name",
              "text": null,
              "before": "9f2c41ab",
              "after": "41ab77e0"
            }
          ]
        }
      ]
    }
  },
  "collisions": [
    {"id": "c1", "actors": ["director", "singer"], "moves": ["director#1.1", "singer#1.1"]}
  ]
}
```

**Step record.** Each step records:

- The actor and the goal ID it belongs to.
- The operation.
- The target and how it was found, in order of stability: a test ID, role
  plus accessible name, nearby text, then position among siblings. The record
  keeps every rung it could capture, plus `found_by`, the rung that
  identified it uniquely at record time.
- The text typed. A value marked `secret=True` in the scenario is stored as
  the name of the environment variable that supplies it, never the value.
- Hashes of the page before and after the step, as keys into the `pages`
  table.

**Page hashes.** A hash can only say "same or different," but replay needs
"close." The trace therefore stores, per distinct page, the set of hashes of
its elements, and computes closeness from the sets:

- **The element set.** Each actionable element contributes one entry: the
  BLAKE2b hash (four bytes, written as eight hex characters) of its role and
  accessible name after normalization. Normalization replaces each run of
  digits with `#`, and each ID-like token (a UUID, a hex string of eight or
  more characters, or a base64-style token of 16 or more) with `<id>`. So a
  changing count, timestamp, or generated ID doesn't change the entry.
- **The page key.** BLAKE2b (eight hex characters) of the normalized URL and
  the sorted element set. The normalized URL keeps the path with ID-like
  segments replaced by `:id` (`/piece/9c1f…` becomes `/piece/:id`) and the
  sorted query keys with no values. The page key is what `before` and `after`
  hold, and `pages` maps each key to its element set. The table stores each
  distinct page once, which keeps the trace small and its diffs readable.
- **Similarity.** The exact Jaccard index of two element sets. A page holds
  at most 250 elements, so the exact value is cheap to compute, and the
  package needs no sketch.
- **Determinism.** The package uses BLAKE2b and never Python's built-in
  `hash()`, whose output changes between processes.

**Replay rules.** In `auto` mode, if a trace exists, the engine replays it.
For each recorded step the engine resolves the target against the current
snapshot, then measures the similarity between the recorded `before` element
set and the current page:

| Situation | Action |
|---|---|
| The target resolves to exactly one element, and the page is identical or close (similarity at least 0.80). | Run the step. No model call. |
| The target resolves to exactly one element, and the page is in the middle band (0.50 up to 0.80). | Re-decide that one step within its goal. Record a heal. |
| The target resolves to zero elements or to several. | Re-decide that one step within its original goal. Record a heal. |
| Similarity is below 0.50, whatever the target does. | The page has drifted too far. Re-run the rest of that goal through the model. Record a heal. |

The two thresholds are defaults, exposed as settings. The middle band is this
spec's answer to a gap in the agreed rules, which didn't say what happens
when the target is unique but the page changed materially.

**Waiting for late content.** Before it classifies a target as zero or
several, the engine snapshots again every 100 ms for up to 2 seconds, and
resolves each snapshot. It stops at the first snapshot with exactly one
match. Only after the wait does it apply the rows for zero or several.

**Target resolution.** The resolver walks the rungs in order of stability
(test ID, role plus name, nearby text, sibling position), keeping a
candidate set:

- A rung with no recorded value is skipped.
- The first rung with a recorded value sets the candidate set to its matches.
- Each later rung with a recorded value intersects the candidate set with its
  matches. If the intersection would be empty, the resolver ignores that
  rung and keeps the set.
- A rung whose recorded value matches nothing on the current page is
  ignored for narrowing.
- The resolver stops as soon as one candidate remains. It reports zero if the
  set is empty after the last rung, and several if more than one candidate
  remains.

A step that resolves through a lower rung than the one it was found by
counts in one of two ways:

- **A refresh**, if the higher rung matched nothing (the test ID or the name
  is gone). The step runs, and no model call happens.
- **A heal**, if the higher rung matched several elements (a role-plus-name
  collision) and a lower rung, such as nearby text or sibling position,
  chose one. It runs without a model call. It counts against the heal cap and
  makes the run `passed_healed`, because a position guess is the kind of
  choice a person should review.

`Result.message` and `Cost.detail` list every refresh and heal, each with its
step and the rung used.

**Re-decide one step.** The engine sends the goal, the current snapshot, and
the steps done so far to the Decider, restricted to the recorded operation
family (a recorded `CLICK` may become a different `CLICK`, not a `TYPE_TEXT`).
The new step replaces the old one in the healed trace.

**Re-run the rest of a goal.** The engine discards the recorded remainder of
that goal, runs the goal from the current page through the model until
`DONE`, and replaces the remainder with the new steps. The re-run is
available only when every `ASSERT` step in the discarded remainder comes after
the last action step. Those trailing `ASSERT` steps stay at the end, in their
recorded order, with their predicates unchanged. If the remainder holds an
`ASSERT` step before an action step, the re-run would move it, and the goal
fails with `drift_before_assert`.

**Outcome checks.** A replay must prove that it ended where the recording
did:

- At the end of a replayed goal, the engine compares the current page's
  element set with the recorded `after` set of the goal's last step, and
  fails the goal with `outcome_mismatch` if similarity is below 0.80.
- A healed step's actual `after` set is compared with the recorded `after` set
  of that step, and the step fails with `heal_changed_outcome` if similarity
  is below 0.80. A heal that reaches a different page is a regression, not a
  repair.
- After a heal, the next step compares its recorded `before` against the
  healed step's actual `after`, which becomes the baseline. One heal doesn't
  cascade into a heal at every later step.
- A goal must end in at least one check. It's *verified* if it holds an
  `ASSERT` step, or an oracle or ledger check runs after it and before the
  same actor's next goal or the end of the scenario, or the caller declares
  an external check (`externally_verified`, which the swarm adapter sets
  because `Scenario.verify` runs after the goal). The trace records the
  source in `verified_by`. A goal that isn't verified fails the recording
  with `unverified_goal`, and the reader refuses a trace with such a goal.

**Heal rules.**

- Heals are capped per run (default 3). A heal that would exceed the cap
  isn't attempted, and the step fails with `heal_budget_exceeded`.
- A heal never changes an assertion. It changes how a target is found and
  which action is taken, and nothing about what a check expects. An `ASSERT`
  step's target resolves through the ladder only, with no model call. If it
  resolves to zero or several elements after the wait, the assertion fails.
  Each `ASSERT` keeps its recorded position in the goal.
- A run with any heal reports `passed (healed)`. It's never a plain pass.
- A failed assertion fails the run, whatever happened with healing.
- The engine writes a healed trace to the run's evidence directory as
  `<scenario_id>.trace.healed.json` (with the parameter ID, if any) and never
  overwrites the committed file. The orchestrator can write it back with
  `sebby.browser.traces.promote(...)`, which a person or the swarm calls
  after review.

**Modes.** `--browser-mode` selects how traces and models are used:

| Mode | Behavior |
|---|---|
| `replay-only` | Uses the trace. Any model call is a failure, and no `TypeSafeDecider` is constructed. A scenario with no trace ends as `no_trace`. The default with `--run-browser-agent`, so a run costs nothing. |
| `auto` | Replays a trace if one exists, records otherwise, and heals. Needs `--browser-allow-billed`. |
| `record` | Ignores any existing trace and writes a new one. Needs `--browser-allow-billed`. |
| `live` | Ignores traces and writes none. Needs `--browser-allow-billed`. |

### Layer 4: scenario runtime

A scenario is an async pytest test written in a Python DSL:

```python
@scenario
async def director_saves_while_singer_opens_share(cast, expect):
    director, singer = cast("director@A", "singer@A")
    await director.goal("open the piece 'Adeste' and change alto bar 3 to s f m")
    await singer.goal("open the share link for 'Adeste'", url=cast.share_url("Adeste"))
    await collide(director.click("Save"), singer.reload())
    await expect.piece("Adeste").voice("alto").bar(3).equals("s f m")
    await expect.singer_sees(singer, version="latest")
```

In staff2solfa, the `piece` oracle parses the expected value `"s f m"` through
`solfa_io`, and compares it with the stored `Piece` by scale degree,
accidental, octave offset, and duration. It doesn't compare strings. A string
comparison would call `s f m` and `s, f, m` different, and would miss a
pitch that a different spelling changed.

**`@scenario`.** The decorator wraps the async function in a synchronous
pytest test that runs its own event loop, so a host project doesn't need
`pytest-asyncio`. The wrapper keeps the original signature, so pytest injects
`cast` and `expect` as fixtures. The wrapper binds them to its loop, and its
`finally` block closes every browser.

**`cast` and identities.** `cast("director@A", "singer@A")` takes actor
specs of the form `<role>@<workspace>` and returns actors in the same order.
The core package doesn't know what a role or a workspace is. The host
project's conftest supplies a `CastResolver`:

```python
class CastResolver(Protocol):
    def resolve(self, spec: str) -> Identity: ...        # "director@A" -> Identity
    helpers: object                                      # extra methods on `cast`

@dataclass(frozen=True)
class Identity:
    name: str                          # the cast spec: "director@A"
    email: str | None                  # the seeded account, when there is one
    state_file: Path | None            # storageState JSON, or None to start signed out
    base_url: str
    allowed_hosts: frozenset[str] = frozenset()   # "host:port" entries
```

The plugin gets the resolver from a fixture the host defines, named
`browser_cast_resolver`. Any attribute `cast` doesn't have, such as
`cast.share_url`, falls through to `resolver.helpers`. That's how the host
adds `share_url("Adeste")`, which needs the seeded piece's share token. The
swarm adapter supplies both from its own configuration (see "Executor
adapter").

**Actors.** Each actor runs as its own asyncio task in the one test process.
The task drains a queue of moves, so one actor's moves run in order while
different actors run at once. `await director.goal(...)` enqueues a goal and
waits for its result. To run goals at the same time, use
`await parallel(a.goal(...), b.goal(...))`.

A move method (`click`, `type`, `upload`, `select`, `reload`, `goto`, `goal`)
returns a lazy `Move`. Awaiting a `Move` runs it. Passing it to `collide`
arms it.

**`collide`.** `collide(*moves, order=None, repeat=1, max_gap_ms=25,
prepare_timeout=30, trusted=True, reset=None, observe=None)` has two phases.

1. **Prepare.** Each actor's task resolves its move, from the trace or the
   model, and builds the exact CDP commands it will send. For a click, the
   last steps of prepare, just before the actor reaches the barrier, are:
   recompute the element's guard, hit-test its center with
   `elementFromPoint`, and hold the coordinates and the element's `objectId`.
   It also installs a capture-phase listener in an isolated world
   (`Page.createIsolatedWorld`) that records each trusted `mousedown` and
   `click`: the event's target, `isTrusted`, and `timeStamp`. Prepare stops
   there and sends nothing.
2. **Fire.** Every actor task waits at one shared release point, an
   `asyncio.Barrier`. When the last actor arrives, each task sends its held
   commands with no check and no await between them. For a click, it uses
   `send_nowait` for `mousePressed` and then `mouseReleased`, so both are
   written back to back on the connection before any reply returns. The engine
   records the monotonic time of each write.

**Prepare failures.** The barrier has a prepare timeout (`prepare_timeout`,
30 seconds). If an actor fails to prepare, or the timeout expires, the engine
calls `Barrier.abort()`. The other actors get `BrokenBarrierError` and don't
fire. The collision ends as `error`, and nothing was sent.

**Checking the fire.** After the fire settles, the engine reads each isolated
world's record and verifies the event:

- The trusted event's target must be the intended element. The engine
  compares the target's `backendNodeId` (from `DOM.describeNode` on the
  listener's recorded node) with the element that prepare held. A different
  target, or no trusted event, is `misfired`. A misfired collision ends as
  `error`, because the test didn't do what it claims. A page that moved an
  overlay over the target between the hit test and the fire is the case this
  check exists for.
- The fallback is `trusted=False`, which sends
  `Runtime.callFunctionOn` with `el.click()` on the held `objectId` instead
  of mouse events. It skips the hit test and produces an untrusted event
  (`isTrusted` is false). Use it for a target that can't be hit-tested, such
  as an element that's clipped. The report marks the move `untrusted`. The
  listener still checks the event's target.

**The gap.** The client-side spread of the write times is only an upper
bound on what the app sees, because it doesn't include the browser's own
delay. The report gives three measures, and the gate uses the strongest one
available:

1. The spread of the write times on the client.
2. The spread of the in-page event times, from `performance.timeOrigin +
   event.timeStamp` in each isolated world.
3. The spread of `Network.requestWillBeSent` `wallTime` for the first request
   that each move triggers (the save request for a click on **Save**, the
   navigation request for a reload).

If every move produced a request, a gap in measure 3 above `max_gap_ms` makes
the collision `missed`. If some move produced none, the gate uses measure 2
and the report says so. A `missed` collision ends as `error`. The engine
can't retry it, because a save isn't repeatable without a reset. The engine
also measures each connection's round-trip time with a no-op evaluate before
the fire, and reports it.

**Ordering.** `order=` forces a sequence. `order=before(director, singer,
ms=50)` fires the director's command, then the singer's about 50 ms later,
and the report records the offset actually achieved, measured the same way as
the gap. `order="both"` runs the collision once in each order.

**`repeat=N` and `order="both"`.** Each iteration after the first needs the
app and the pages back at the state before the collision. For each iteration
the engine:

1. Calls the host's `reset` (an async callable, for example "restore the piece
   to version 1").
2. Closes each actor's page and opens a fresh one in the same browser, so
   in-memory page state doesn't carry over.
3. Re-runs that actor's prefix, the moves the scenario ran before `collide`,
   from the trace or the move log, with no model call.
4. Prepares and fires the collision again.

`repeat` and `order="both"` need `reset`. Without one, `repeat` is allowed
only when every move is idempotent (`reload`, `goto`). For `order="both"`,
the `observe=` callback returns a value after each order, and the report
sets `order_dependent=True` if the two values differ. That shows whether the
outcome depends on ordering.

**Assertions on state.** `expect.piece(...)` isn't a page assertion. It's an
oracle query (layer 5). `expect` exposes every query that an installed oracle
registers.

**pytest plugin.** `sebby.browser.pytest_plugin` is a plain module. It isn't
registered through the `pytest11` entry point, because an entry point applies
to every installation of the `sebby` distribution, not to a single extra, and
would load the plugin in projects that never installed `sebby[browser]`. A
host project opts in from its top-level `conftest.py`:

```python
pytest_plugins = ["sebby.browser.pytest_plugin"]
```

The plugin imports nothing optional at module import time. `websockets`,
`typesafe_sdk`, and `litellm` (through `sebby.llm`) load inside the functions that use them, so a
collection error can't come from a missing extra. It adds:

- The `browser_agent` marker, which `@scenario` applies.
- `--run-browser-agent`: without it, every `browser_agent` test is skipped.
- `--browser-allow-billed`: without it, no billed model call runs. It
  defaults to off, so `--run-browser-agent` alone runs replay only.
- `--browser-mode`, `--browser-max-heals`, and `--browser-report-dir`.
- The `cast` and `expect` fixtures.

### Layer 5: oracles

**Invariants.** After every step of every actor, in record, replay, and live
modes, the engine runs the invariants against the step's observation window.
Each is configurable, and the defaults are:

| Invariant | Fails when | Configuration |
|---|---|---|
| `no_5xx` | A response in the window has a status of 500 or more. | URL globs to ignore. |
| `no_console_errors` | An uncaught exception or a `console.error` appears. | Regexes to ignore; whether `console.error` counts. |
| `host_allowlist` | A request or WebSocket to a host outside the allowlist was attempted, whether or not the browser blocked it. | The allowlist itself. |
| `latency_budget` | The step took longer than its budget, from action to settled page. | A step budget in ms (default 5,000), a separate allowance for each actor's first navigation (default 15,000), and per-operation overrides. |

The cold-load allowance exists because a Vite development server compiles
modules on the first request, so a first page load takes much longer than a
later step.

`no_console_errors` ships with a default ignore list, `DEFAULT_CONSOLE_IGNORE`,
for messages that a React and Vite development build prints on a healthy page:
Vite's `[vite]` connection lines, the React DevTools download hint, React
Router's future-flag warnings, and React's `Warning:`-prefixed development
warnings. A host replaces or extends the list. A host that wants React
warnings to fail a run removes that entry.

A failed invariant produces a `Finding` (invariant name, actor, step number,
the offending URL or message). By default the run continues to the end of the
scenario so the report holds all findings, then fails. `fail_fast=True` stops
at the first finding. Invariants attach to steps and not to scenarios, so a
5xx that a background poll triggers still fails the run.

Custom invariants implement `Invariant.check(step, observations) ->
list[Finding]`. Inside a collision, the engine runs invariants and
`after_step` only after the fire, so no state read runs between prepare and
fire.

**Domain oracles.** A domain oracle is a plugin. It's found through the
Python entry point group `sebby.browser.oracles`, and the entry point name
becomes the oracle's name. The package ships a base class, `OracleBase`, with
`oracle_api = 1` and no-op `start`, `close`, and `after_step` methods, so an
oracle overrides only what it needs. The contract:

```python
class Oracle(Protocol):
    oracle_api: ClassVar[int]           # 1

    async def start(self, ctx: OracleContext) -> None: ...
    def queries(self) -> Mapping[str, Callable[..., Query]]: ...  # names on `expect`
    async def after_step(self, step: StepRecord, ctx: OracleContext) -> list[Finding]: ...
    async def close(self) -> None: ...

class Query:
    async def evaluate(self) -> Observed: ...
    def __eq__(self, other: object) -> NoReturn:
        raise TypeError("await the query, then compare: await q.equals(x)")

@dataclass(frozen=True)
class OracleContext:
    actors: Mapping[str, ActorHandle]   # read a page, evaluate JS, fetch as the actor
    base_url: str
    http: AllowlistedHttp               # a client bound to the allowlist
    config: Mapping[str, object]        # this oracle's section of the host's settings
    async def settled(self, timeout: float = 5.0) -> None: ...
```

- `queries()` names the methods on `expect`. staff2solfa's plugin can register
  `piece` and `singer_sees`. Two oracles that register the same name make
  startup fail with `oracle_name_clash`, naming both entry points. No oracle
  wins by load order.
- `after_step` lets an oracle check a domain invariant on every step, such as
  "no `Piece` ever loses a voice." The engine never runs it between prepare
  and fire.
- **Waiting for the app.** State can lag a click that a save request hasn't
  finished. Before it calls `Query.evaluate`, the engine runs
  `ctx.settled()`, which waits until no actor has an in-flight request for
  500 ms, up to a timeout. For state that becomes true a moment later, a
  scenario wraps the query: `await expect.eventually(lambda:
  expect.piece("Adeste").voice("alto").bar(3).equals("s f m"), timeout=5)`
  retries until the check passes or the timeout ends, and writes one ledger
  entry for the final result.
- **Credentials.** An oracle reads state two ways. `ctx.http.for_actor(name)`
  returns a client whose cookie jar the pool fills from that actor's cookies
  (`Network.getCookies`) at the time of the call, and which sends them only
  to allowlisted hosts. `ActorHandle.fetch(url, ...)` runs `fetch()` inside
  that actor's page, so the request carries the page's own credentials and
  origin. The plain `ctx.http` sends no credentials. An API key that the host
  needs comes through `config`, and the core package doesn't log it.
- **Where `config` comes from.** In a pytest scenario, the host's fixture
  `browser_oracle_config` returns a mapping from oracle name to that oracle's
  settings. In the adapter path, the adapter's constructor takes the same
  mapping and passes it to `run_goals` as `oracle_config`. In staff2solfa,
  that is built from the swarm run's `env/instance.json`.
- An oracle reads state through the application's API or a test probe, and
  through `ActorHandle` for what an actor's page shows. The core package
  doesn't interpret any of it. staff2solfa's plugin compares `Piece`s with
  its relative-pitch model. That plugin is out of scope here.

**The `expect` comparison.** In Python, `await x == y` parses as
`(await x) == y`, so a comparison written that way would compare and discard
the result, and a mismatch would pass silently. Ruff's `B015` rule (pointless
comparison), which `sebby`'s config enables, flags the same line. So the
canonical form is `await query.equals(expected)`, and the example scenario
uses it. The design also makes the `==` form safe:

- `await query` returns an `Observed` value.
- `Observed.equals(expected)` records a check in the run's ledger, a pass or
  a failure with both values, and returns the boolean.
- `Observed.__eq__` and `Observed.__ne__` are both defined, and both record a
  check (`__ne__` records the negation), so neither operator discards its
  result. `Observed.__hash__` is `None`, because a value with a recording
  `__eq__` mustn't act as a dictionary key.
- `Query.__eq__` raises `TypeError`, so a comparison written without `await`
  fails loudly and doesn't return `False`.
- At the end of the scenario, any failed ledger check fails the run. A
  scenario that finished with no ledger check and no `ASSERT` step fails with
  `no_assertions`, so a scenario can't pass by asserting nothing.

## Executor adapter

The swarm's executor interface (`tests/qa_swarm/executor.py` in staff2solfa,
"Scenario executor interface" section of its spec) defines `PageState`,
`Actor`, `BrowserEndpoint`, `Budget`, `Scenario`, `Cost`, `Result`, and the
`Executor` protocol with `name`, `max_actors`, `launch_flags`, and
`run(scenario, endpoints, evidence_dir)`. `sebby.browser` implements it with
those names. The swarm spec already carries the extensions this design needs:
`Executor.max_actors`, `Executor.launch_flags`, `Scenario.script`, the
`passed_healed` status, and the `invariant.` and `oracle.` check prefixes.

**Where the code lives.** The interface types belong to staff2solfa, and
`sebby` can't import them. So the adapter has two parts:

- `sebby.browser.executor.run_goals(...)`: a generic, synchronous function
  that takes plain values and returns a `RunOutcome`. It has no swarm names.
- `tests/qa_swarm/browser_executor.py` in staff2solfa: a thin class, about
  100 lines, that implements `Executor` with `name = "sebby-browser"` and
  `max_actors = 4`, and maps between the swarm types and `run_goals`. The
  registry in `tests/qa_swarm/plugin.py` resolves it by that name.

`max_actors = 4` stays within the swarm's session cap of 6 live sessions per
run, and within the global rule that no more than three agents run a browser
at once. (The swarm's interface comment still says eight; see "Open
questions.")

**`run_goals`.** The function is synchronous and owns its event loop. It
calls `asyncio.run` and raises `RuntimeError` if it's called from a running
loop, which fits the swarm's synchronous `Executor.run`. Its signature:

```python
def run_goals(
    *,
    scenario_id: str,
    cast: Mapping[str, Identity],            # keyed by cast spec
    start_url: str,
    script: Callable[..., Awaitable[None]] | None = None,
    goals: Mapping[str, str] | None = None,  # cast spec -> goal, one actor only
    endpoints: Mapping[str, str] | None = None,   # cast spec -> cdp_url; None means launch mode
    allowed_hosts: frozenset[str],           # "host:port" entries
    files: Mapping[str, Path] | None = None,
    helpers: object | None = None,           # extra methods on `cast`
    oracle_config: Mapping[str, Mapping[str, object]] | None = None,
    mode: Mode = "replay-only",
    limits: Limits = Limits(),               # max_steps, max_model_calls, max_heals, timeouts
    trace_dir: Path,
    evidence_dir: Path,
    on_usage: Callable[[UsageRecord], None] | None = None,
    externally_verified: bool = False,
) -> RunOutcome: ...
```

Exactly one of `script` and `goals` is set. `RunOutcome` is a dataclass:

```python
@dataclass
class RunOutcome:
    status: Literal["passed", "passed_healed", "failed", "blocked",
                    "budget_exhausted", "no_trace", "error"]
    reason: str                       # a short code: "misfired", "outcome_mismatch", ...
    final_page: FinalPage | None      # the primary actor's url, text, actions
    findings: list[Finding]           # invariants, dialogs, allowlist, oracle
    checks: list[LedgerCheck]         # each expect and ASSERT: name, passed, detail
    steps: list[StepRecord]           # the step log, secrets masked
    collisions: list[CollisionReport]
    evidence: list[Path]              # under evidence_dir
    model_steps: int                  # steps the model decided, heals included
    replayed_steps: int
    model_calls: int                  # billed decisions and text calls
    tokens: int
    heals: list[HealRecord]           # step, kind ("model" or "ladder"), rung
    refreshes: list[HealRecord]
```

**How the adapter maps swarm values.** A `cast()` spec string equals the
swarm's `Actor.name`. A script that calls `cast("director@A")` finds the
`Actor` whose `name` is `"director@A"`. The adapter builds each `Identity` from
the swarm's `Actor`:

| `Actor` field | `Identity` field |
|---|---|
| `name` | `name` (the cast spec) |
| `identity` (an email) | `email` |
| `state_file` | `state_file` (the plugin has already loaded it into the browser; attach mode doesn't load it again) |
| none | `base_url`, the origin of `Scenario.start_url` |
| none | `allowed_hosts`, the run's allowlist |

A spec with no matching `Actor` fails the run with `error`. The `endpoints`
mapping is keyed by `Actor.name` already, so it passes straight through.

**Helpers and oracle config in the adapter path.** The pytest path gets
`cast.share_url`-style helpers from the `browser_cast_resolver` fixture and
oracle settings from `browser_oracle_config`. The adapter has no fixtures.
staff2solfa's adapter takes both in its constructor: a `helpers` object (the
same class the fixture returns, built from the run's `env/instance.json` and
seeding) and an `oracle_config` mapping. The adapter passes them to
`run_goals` as `helpers` and `oracle_config`.

**What the adapter does.** `run` builds a pool in attach mode from
`endpoints`, one actor per `scenario.actors` entry. It then:

1. Runs the scenario. If `scenario.script` is set, the executor runs it. If
   it isn't set and there's one actor, the primary actor pursues
   `scenario.goal` from `scenario.start_url`. If it isn't set and there are
   several actors, the adapter returns `unsupported`.
2. Reads the primary actor's final page (`RunOutcome.final_page`) into a
   `PageState` (`url`, `text`, and `actions` as `{"label", "role"}` dicts from
   the snapshot), calls `scenario.verify`, and returns the dict in
   `Result.checks`.
3. Adds the invariants and oracle checks to `Result.checks` under
   `invariant.<name>` and `oracle.<name>`, so the contract's rule that
   `passed` needs every check true holds.
4. Writes screenshots, the healed trace, and redacted console and network
   logs only under `evidence_dir`, and lists them in `Result.evidence`.

**Launch flags.** `launch_flags(allowed_hosts)` returns
`sebby.browser.launch.chromium_flags(allowed_hosts)`: the PAC file flags and
the resolver-rules backstop. The plugin adds them when it launches each
browser, so attach mode gets the same port-aware block as launch mode. This
answers the swarm's rule that "the flags that enforce its network allowlist
must be set at launch."

**Mode.** The adapter runs in `auto` when the swarm's `[budget]` still allows
billed steps, and in `replay-only` otherwise. A new scenario with no trace
and no billed budget ends as `no_trace`, and the adapter reports
`unsupported`, which the swarm treats as a coverage gap and not a failure.

**Status mapping.**

| Outcome in `sebby.browser` | `Result.status` |
|---|---|
| Every goal `DONE`, every check true, no heals. | `passed` |
| The same, with one or more heals. | `passed_healed` |
| A check, invariant, or oracle expectation failed, or a replay's outcome check failed (`outcome_mismatch`, `heal_changed_outcome`). | `failed` |
| The heal cap was exceeded, or `drift_before_assert`. | `failed` |
| A goal returned `BLOCKED`. | `blocked` |
| The billed-step budget ran out. | `budget_exhausted` |
| More actors than `max_actors`, or several actors with no `script`. | `unsupported` |
| No trace and no billed budget (`no_trace`). | `unsupported` |
| A CDP failure or timeout, a crashed or lost target, a stale page that didn't recover, a missed or misfired collision, a prepare failure, an unreadable trace, an unverified goal, or an unresolved cast spec. | `error` |

**Cost.** `Cost.steps` counts the steps that the model decided, including
heals. Replayed steps cost nothing, so `plugin.py`'s reserve-and-refund
budget doesn't tie up steps for them. `Cost.provider_calls` counts billed
calls: decisions, retried attempts, and text-model calls. `Cost.detail`
carries `replayed_steps`, `heals`, `refreshes`, and `tokens`. The executor
stops at `Budget.max_steps` on model-decided steps, and a separate cap of
four times that value bounds replayed steps, so a loop can't run forever for
free. `Result.message` lists each heal and refresh with its step and rung.

**No secrets.** The adapter, like jev's, holds its own provider keys
(`TYPESAFE_API_KEY` and `ANTHROPIC_API_KEY`). It never writes them
to `history`, `evidence`, or `message`. Values marked `secret=True` are
masked in the history.

## Package and dependencies

### Layout

The files marked "fork" come from jev and keep its file names. See "The jev
fork" for what each one changes.

```text
src/sebby/browser/
  LICENSE.jev-ultrafast  upstream's MIT license (Copyright (c) 2026 Browser Use)
  FORKED.md            upstream repository and commit, file list and hashes
  __init__.py          public API: scenario, collide, run_goals, Decider, Oracle
  snapshot.js          fork: the page-side snapshot script
  questions.py         fork: the prompts and MAX_STEPS
  model.py             fork: action_space, validate_choice, build_questions,
                       interpret, field_context, field_text
  agent.py             fork: the step loop, with hooks
  browser.py           the driver over one pool page (replaces jev's Browser)
  decider.py           Decider, DecisionRequest, TypeSafeDecider (the SDK call)
  cdp/
    client.py          the WebSocket client, timeouts, send_nowait, events
    session.py         flattened sessions, target management, auto-attach
  pool.py              launch and attach modes, the ActorBrowser interface
  launch.py            Chromium discovery, flags (chromium_flags), process groups
  pac.py               the PAC file for an allowlist
  allowlist.py         entry parsing, canary, Fetch interception, navigation guard
  dialogs.py           the dialog and popup policies
  capture.py           console and network buffers
  directed.py          directed steps (actor.click("Save"))
  locators.py          the locator ladder and target resolution
  trace.py             the trace schema, reader, writer, migrations
  replay.py            ReplayDecider, replay rules, heals, outcome checks
  hashing.py           normalization, BLAKE2b element sets, Jaccard
  runtime/
    dsl.py             @scenario, Move, cast, parallel
    actor.py           the per-actor task and queue
    collide.py         prepare, fire, misfire check, gap, ordering, repeat
    expect.py          expect, Observed, the ledger, eventually
  oracles.py           Oracle, OracleBase, Query, entry-point loading
  invariants.py        the four built-in invariants and DEFAULT_CONSOLE_IGNORE
  pytest_plugin.py     the marker, flags, and fixtures (opt-in; no entry point)
  executor.py          run_goals and RunOutcome
  report.py            the run report
```

### The `sebby[browser]` extra

```toml
browser = [
    "sebby[judgement]",
    "sebby[llm]",
    "websockets>=13.0",
]
```

`sebby[judgement]` brings `typesafe-sdk`, and with it `httpx2`, `msgspec`, and
`tenacity`. `sebby[llm]` brings `litellm`, which the default text model
needs. The fork drops what jev's imports pulled in: `browser-harness` and
`httpx[http2]`. The only dependency that no existing extra carries is `websockets`.
`pytest` isn't in the extra, because only a project that uses the DSL loads
the plugin, and that project has pytest. The `dev` extra adds
`sebby[browser]`. The package registers no `pytest11` entry point (see layer
4).

### Raw CDP over `websockets`, not Playwright

Playwright for Python has a `connect_over_CDP` mode. This design uses a small
CDP client over `websockets` instead, for four reasons:

- **Dependency weight.** Playwright ships a Node driver and a large package,
  and installs its own browsers. `websockets` is one pure-Python package. The
  spec's constraint is minimal dependencies.
- **Timing.** A collision needs the shortest, most predictable path from the
  Python process to Chromium. Playwright puts a driver process and its own
  protocol between them. A direct WebSocket lets each actor write its held
  commands within microseconds of the release.
- **Fidelity.** Playwright documents that `connect_over_CDP` has lower
  fidelity than its own launch mode. The design needs `Fetch`,
  `Target.setAutoAttach`, `Input.dispatchDragEvent`, and
  `DOM.setFileInputFiles` exactly as CDP defines them.
- **No duplicate work.** The snapshot, hit test, and guard run in page-side
  JavaScript, and the engine's freshness rules replace Playwright's
  actionability checks.

The price is real. The project owns roughly 600 lines of CDP client,
connection handling, and target management (dialogs, popups, crashes, and
auto-attach included), and Playwright's mature handling of edge cases isn't
inherited. Two mitigations apply: the code uses a short, listed set of CDP
methods, and an integration test (see "Testing") runs each against a real
Chromium, so protocol drift fails a test and not a run. Playwright stays a
possible fallback for the pool layer behind the `ActorBrowser` interface, if
CDP churn costs too much.

The package finds a Chromium and doesn't install one, and it doesn't import
Playwright. It looks in this order and uses the first match:

1. `SEBBY_BROWSER_CHROMIUM`, an explicit path to the executable. It overrides
   the other two sources. If it's set and the path doesn't exist, the lookup
   fails and doesn't fall through.
2. Playwright's installed Chromium: the newest `chromium-*` folder in
   `PLAYWRIGHT_BROWSERS_PATH`, or in Playwright's default cache
   (`~/Library/Caches/ms-playwright` on macOS, `~/.cache/ms-playwright` on
   Linux, `%LOCALAPPDATA%\ms-playwright` on Windows). A
   `PLAYWRIGHT_BROWSERS_PATH` of `0` means Playwright's package folder, which
   the lookup doesn't search.
3. System Google Chrome, at its standard path for the platform.

If none is found, the lookup raises an error that names both options: install
Playwright's Chromium (in staff2solfa, `make e2e-install`), or install Google
Chrome, and set `SEBBY_BROWSER_CHROMIUM` to use a browser elsewhere. The
integration tests skip when the lookup fails.

## Cost and budget controls

TypeSafe calls are billed, and so are text-model calls (Anthropic, by default). Controls apply at
five points:

- **Opt-in.** `--run-browser-agent` runs scenarios, and by default only in
  `replay-only` mode. A billed call needs `--browser-allow-billed` as well.
  A billed live check needs both flags and its own marker.
- **Replay first.** Once a trace exists, a passing run makes zero model
  calls. Cost is paid at record time and at each model heal.
- **Caps per run.** `max_steps` (model-decided), `max_model_calls`,
  `max_heals`, a per-goal step cap (the 60 that jev uses), and a wall-clock
  timeout per scenario.
- **Small requests.** At most 250 elements and 6,000 characters of text per
  decision, and the last 10 steps of history.
- **A ledger.** `Usage` from every decision and text call goes through
  `record_usage` to an `on_usage` callback and into `Cost.detail` and the run
  report. TypeSafe reports tokens and not money, so the ledger holds tokens,
  and a price table in settings is optional.

The swarm's own reserve-and-refund budget wraps this. `Budget.max_steps` is
the reservation, and the executor reports what it spent.

## Safety

- **The allowlist is enforced in the browser** by a port-aware PAC file set at
  launch, browser-level auto-attach with interception when the PAC file isn't
  active, and a navigation guard, as layer 1 describes. A blocked or
  attempted request becomes a finding.
- **Page text is untrusted data.** The Decider gets it as state, and the
  prompt says so, as jev's does. The model can only choose an operation from
  the fixed set and a target from the observed elements. It can't produce a
  selector, a script, a URL, or a file path, so injected text on a page can
  steer a choice but can't make the agent do something outside the set.
- **Typed text comes from the scenario, the goal, or the text model,** and
  the model's text is length-bounded and checked as a single value.
- **Uploads use declared fixtures only.**
- **Secrets stay out of traces.** A `secret=True` value is stored as an
  environment variable name. Password inputs never reach the snapshot. The
  pool doesn't write storageState into a trace or the evidence folder.
- **Dialogs, popups, downloads, and permissions.** The pool dismisses dialogs
  by default, guards popups, sets `Browser.setDownloadBehavior` to deny, and
  denies permission prompts.
- **Process safety.** Teardown covers every exit path in launch mode, the
  sweep verifies a process before it signals it, and profile folders are
  removed.

## Testing

Tests live in `sebby`'s `tests/browser/` and run under `sebby`'s pytest, ruff,
and strict mypy gates.

**Unit tests, with no browser and no model.**

- **Target resolution ranking.** The ladder picks a test ID over a role plus
  name, intersects rungs to break ties, skips a rung with no recorded value,
  ignores a rung that matches nothing, reports zero and several correctly, and
  gives the same answer for the same snapshot. A collision that a lower rung
  breaks is a heal, and a missing higher rung is a refresh.
- **Trace read and write.** A round trip is byte-stable, the file has no
  timing fields, a parameter ID reaches the filename, an unknown major
  `schema_version` is refused, and an older version migrates.
- **Heal rules.** Each row of the replay table, the cap, the rule that a heal
  never changes an `ASSERT` predicate or position, `drift_before_assert`,
  `passed_healed` for any heal, a failed assertion failing a healed run,
  `heal_changed_outcome`, and the healed `after` becoming the next baseline.
- **Outcome checks.** `outcome_mismatch` at the end of a replayed goal, and
  the `unverified_goal` rule for recording and for reading a trace.
- **Barrier and timing logic.** With fake connections, the release point
  fires all held commands, the write, event, and request spreads are computed,
  a request spread over `max_gap_ms` becomes `missed`, the gate falls back to
  event times when a move made no request, a failed prepare and a prepare
  timeout abort the barrier, and `order=` produces the requested offset.
- **Invariant checks.** Each built-in invariant against synthetic observation
  windows, including its ignore lists, the default console ignore list, and
  the cold-load allowance.
- **Hashing.** Normalization of digits and ID-like tokens and of path IDs,
  identical output for the same input across two processes with different
  `PYTHONHASHSEED` values, and exact Jaccard values.
- **The PAC file** for a set of `host:port` entries, evaluated against sample
  URLs (an allowed origin, another loopback port, an IP literal, a WebSocket
  URL).
- **The CDP client** against a fake WebSocket server: a result larger than
  1 MiB arrives intact, a call times out, and a lost connection fails pending
  calls with `TargetGone`.
- **`Observed` and `Query`.** `__eq__` and `__ne__` record checks, the value
  is unhashable, `Query.__eq__` raises `TypeError`, and a scenario with no
  checks fails with `no_assertions`.
- **Oracle loading.** A name clash between two oracles fails startup, and
  `OracleBase` supplies a default `after_step`.
- **The fork's characterization tests.** Each jev function that the fork
  keeps or extends has a test that fixes jev's behavior before and after the
  change: `validate_choice` accepts and rejects the same answers, including
  both response checks; `action_space` returns the same tables for the
  original kinds, and adds `UPLOAD` and `ASSERT` for the new ones; `interpret`
  returns the dict that jev's `choose` returned for the same answers;
  `field_text` keeps its signature and its validation of the reply; and the
  snapshot script's `marker`, `page_key`, and `guards` don't change when the
  ladder is added.
- **`FORKED.md`.** A test checks that it names the upstream commit, and that
  every forked file and the license file exist and are listed.
- **Retry accounting.** The decider reads `X-TypeSafe-Retry-Count` and
  records the attempts.
- **The stale-pidfile sweep.** It signals a group only when the start time
  and command line match, and not for a recycled PID.

**Integration tests.** Headless Chromium against local fixture pages, one
with a deliberate race, and a fake `Decider` that returns scripted choices.
They make no TypeSafe calls. They skip when no Chromium is found, and they
run serially in one group, because each browser counts against the global
limit of three browser-running agents (and the multi-actor tests start up to
four browsers themselves).

- Replay makes zero calls to the fake `Decider` and zero calls to
  `field_text`, and in `replay-only` mode no `TypeSafeDecider` is constructed.
- A changed page (a renamed button) triggers exactly one heal. The test
  asserts the healed step's `op` and `target`, not its hashes, and the trace
  the run writes differs from the original in that one step.
- Collisions land within the target gap on an idle machine, and the race page
  produces a different result for each order.
- A collision's fire is checked: a planted overlay that covers the target
  after the hit test produces `misfired`. The `trusted=False` fallback clicks
  the element and the report marks it untrusted.
- Invariants catch a planted 5xx and a planted console error.
- An upload reaches a file input through `DOM.setFileInputFiles`, and a
  drop-only zone receives a file through `Input.dispatchDragEvent`.
- **Allowlist fixtures.** In both launch and attach mode, each of these is
  blocked and produces a `host_allowlist` finding: a WebSocket, a service
  worker registration, a prefetch, a speculation-rules prefetch, a
  `sendBeacon`, a request to another loopback port, and a request to an
  IP-literal URL. The canary distinguishes a browser launched with the PAC
  flags from one launched without.
- **Dialogs and popups.** An `alert`, a `confirm`, and a `beforeunload` are
  dismissed and recorded as steps and findings, and a scenario override
  accepts one. A `target=_blank` link is followed by default and closed under
  the `block` policy.
- **Crashes.** A page that crashes (`chrome://crash`) and a browser that's
  killed both end the run as `error` and leave no process behind.
- Each CDP method the package uses succeeds against the installed Chromium,
  and the PAC path works with the installed version.
- Teardown leaves no Chromium process after a normal exit, a test failure, and
  a `SIGTERM`.

**A billed live check.** One test against TypeSafe, behind
`--browser-allow-billed` and its own marker, and never in the default gate.
It runs a goal on a fixture page and checks that the decision is valid and
that `Usage` is recorded.

**Acceptance in staff2solfa.** Repeat the swarm's planted-defect run
(verification item 8 in the swarm spec) with `sebby-browser` in place of jev.
It must find at least as many planted defects as jev did, and its cost per
run must be lower once traces exist. The measurement:

1. Run the batch with jev and record the defects found and the summed
   `Result.cost`.
2. Run it with `sebby-browser` in `record` mode. This run pays for the
   traces.
3. Run it again in `replay-only` mode. Compare its `provider_calls` and
   tokens to jev's.
4. Confirm that a multi-actor collision scenario now runs and finds its
   planted double-submit. Executor v0 could only report it as a coverage gap.

## Risks

- **TypeSafe is a single vendor.** A price change, an outage, or a change in
  the API affects every model-driven run. The `Decider` protocol contains it:
  a different decider replaces `TypeSafeDecider`, and replay doesn't call the
  vendor at all.
- **CDP churn.** Chromium changes protocol behavior between releases. The
  package uses a short, listed set of methods, the integration suite tests
  each against the installed browser, and a trace records the Chromium
  version for diagnosis only.
- **The PAC path depends on Chromium behavior.** The design relies on the
  bypass-list flag removing the implicit loopback bypass, and on the PAC file
  applying to WebSockets and workers. The canary and the allowlist fixtures
  test both on the installed Chromium, and the weaker `Fetch` mode is the
  fallback.
- **Hash thresholds need tuning.** The 0.80 and 0.50 similarity thresholds
  are guesses. Too high causes needless heals, and too low replays steps on
  a page the recording didn't see. The report lists each step's similarity so
  the values can be tuned from real runs.
- **Collision timing is flaky on a loaded machine.** One process and one
  event loop drive every actor, so a busy machine widens the gap. The report
  states the measured gap, a miss fails the run, and the global limit of
  three browser-running agents applies.
- **Client-side timing is an upper bound.** The write-time spread doesn't
  include the browser's own delay, which is why the gate uses the request
  spread and reports all three measures.
- **A healed run can hide a regression.** The heal that finds a moved button
  is the same heal that finds a removed one. Assertions never heal, a heal
  must reach the recorded outcome, the cap bounds it, and `passed (healed)` is
  never a plain pass, so a person reviews each healed trace before promoting
  it.
- **The fork drifts from upstream.** jev-ultrafast is a single-maintainer
  project with no tagged releases, and the fork's `model.py` and `agent.py`
  no longer diff cleanly against it. `FORKED.md` records the upstream commit
  and the verbatim hashes, and each change is its own commit named for the jev
  function it touches, so a future upstream fix is a small, findable merge. The
  project accepts that fixes don't arrive on their own.
- **License notice.** MIT requires the copyright and permission notice to
  travel with the copied code. `LICENSE.jev-ultrafast` and `FORKED.md` carry
  it, and the fork's files keep jev's notice.
- **Trace size.** A page's element set can hold 250 four-byte hashes, about
  2 KB. The `pages` table stores each distinct page once, so a trace stays
  small, but a long scenario on many distinct pages grows.

## Slices

Three slices, each shippable and tagged in `sebby`. staff2solfa moves to a
slice by bumping its git tag. Estimates are wall-clock best guesses for
agent-driven work with wave review, not measurements.

| Slice | Contents | Estimate |
|---|---|---|
| A. Single actor | **The fork, first:** the verbatim copy of jev's files with the license and `FORKED.md` (the first commit), the mechanical reformat and typing pass, then the named changes: async, `choose` onto the SDK behind `TypeSafeDecider` with retry accounting, `field_text` on Haiku 4.5, the ladder and file inputs in `snapshot.js`, `UPLOAD` and `ASSERT` in `action_space` and `questions.py`, the `prepare` and `dispatch` split, and the `before_step` and `after_step` and invariant hooks. **The new code:** the CDP client (timeouts, crash handling, large results), the browser pool in launch and attach modes with the driver in `browser.py`, browser-level auto-attach with child `Fetch` and `runIfWaitingForDebugger`, the PAC allowlist and canary, the `Fetch` cost measurement, dialogs and popups, capture, uploads (input and drop), teardown with the verified sweep, the four invariants with the default ignore list, `run_goals` for one actor, and the single-actor swarm adapter in staff2solfa with `launch_flags`. Launch-mode storageState is left out. | About 17 hours |
| B. Traces | Trace schema and store, the trace hooks, `ReplayDecider`, the locator ladder and its refresh and heal rules, normalization and Jaccard hashing, replay rules and the late-content wait, heals and the cap, outcome checks and the verified-goal rule, the healed-trace write and `promote`, modes, the budget ledger, and their tests. | About 10 hours |
| C. Multi-actor | The DSL and per-actor tasks, `cast` and `CastResolver`, launch-mode storageState, `collide` (held commands from the `prepare` and `dispatch` split, isolated-world misfire check, three-way gap measure, barrier abort, ordering, `repeat` with prefix re-run), the oracle contract with `OracleBase`, `settled` and `eventually`, `expect` and `Observed`, the opt-in pytest plugin, the multi-actor adapter path with `script`, and the planted-defect acceptance run. | About 16 hours |

Total: about 43 hours, down from about 49 before the fork. The saving is in
slice A: the snapshot, the questions, the choose-and-validate logic, and the
step loop already exist, so slice A extends them and doesn't build them. The
CDP pool, the allowlist, and the adapter don't shrink, because jev has nothing
like them.

Slice A alone replaces jev for single-actor scenarios, because it ships the
staff2solfa adapter. That gives uploads, invariants, the port-aware allowlist,
and a supported TypeSafe client, and it needs no change to the scenarios. Slice
B adds the cost reduction. Slice C adds what jev can't do. The acceptance run
happens at the end of slice C, in staff2solfa, and depends on the swarm's own
slices being merged.

Slice A is still the largest because the CDP work under it (dialogs, timeouts,
auto-attach, child `Fetch`, the `runIfWaitingForDebugger` ordering, and crash
handling) is on the critical path for every later slice. Slice C splits at the
oracle contract if it runs long: collisions and the DSL (about 9 hours), then
oracles, the multi-actor adapter, and acceptance (about 7 hours).

## Open questions

Each item has a proposed answer, used in this spec until the user decides.

1. **Cross-spec follow-up.** The swarm spec's interface comment says
   `sebby-browser` declares `max_actors = 8`, and its adapter-placement text
   says 4. This spec uses 4. The comment in the swarm spec needs the same
   change.
2. **Default numbers.** Similarity of 0.80 and 0.50, a heal cap of 3, a
   collision gap of 25 ms, a step latency budget of 5 seconds, a cold-load
   allowance of 15 seconds, a three-character minimum for an `ASSERT` literal,
   and a 64 MiB message limit are placeholders. Confirm or change them.
3. **Middle-band, refresh, and ladder-heal rules.** The spec re-decides one
   step when the target is unique but the page similarity is between 0.50 and
   0.80. It reports a lower-rung resolution after a missing higher rung as a
   `refresh`, and after a role-plus-name collision as a heal with no model
   call. The agreed rules don't cover these cases.
4. **Assertion authoring.** A model-chosen `ASSERT` accepts only a
   value-taking check whose expected value is a quoted literal from the goal.
   An `ASSERT` target that resolves to zero or several elements fails and
   doesn't heal. Is this stricter than intended?
5. **The verified-goal rule.** Every recorded goal must end in an `ASSERT`, an
   oracle or ledger check, or an external check, and a goal without one fails
   recording. That forces each scenario to check something after every goal.
   Is that the right strictness?
6. **Billed-step counting.** `Cost.steps` counts model-decided steps only,
   so replays don't consume the swarm's `max_steps_total`. The interface
   says the budget counts "executor steps."
7. **Collision reset.** `repeat` and `order="both"` need a host-supplied
   `reset`, and they re-run each actor's prefix on a fresh page. Does
   staff2solfa's seeding support restoring one piece between iterations?
8. **The popup default.** The popup policy defaults to `follow`. `block`
    is stricter, but a share link or a download link that opens a tab would
    then fail. Which default do you want?
9. **The `Fetch` default.** The PAC file is the primary block, and `Fetch`
    stays off when the canary shows the PAC file is active. Slice A measures
    the cost of `Fetch` against Vite. If the cost is small, should `Fetch`
    stay on as a second layer?
10. **Acceptance threshold.** "Lower cost per run" has no number. A proposal:
    a replay run makes at most one fifth of jev's `provider_calls` on the
    same batch.
11. **Release tags.** The spec assumes one `sebby` tag per slice, following
    the current `v0.2.0`, but doesn't fix the numbers.
12. **Upstream contributions.** The fork's changes to jev are mostly
    additions. Should the generic ones (file inputs, the ladder, the async
    conversion) go back to `browser-use/jev-ultrafast` as pull requests, or
    does the fork stay private?
