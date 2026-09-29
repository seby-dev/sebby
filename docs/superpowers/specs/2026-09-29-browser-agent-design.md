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
optional extra, `sebby[browser]`. staff2solfa installs it by git tag and
plugs it in behind the swarm's executor interface, in place of jev.

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
- Keep the dependency set small: `typesafe-sdk` (already in the `judgement`
  extra) and one WebSocket library.

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
- **Heal:** a step or goal remainder that the model decided again during a
  replay because the recorded step no longer applied.
- **Collision:** two or more actions, from different actors, released
  together with a measured gap between them.
- **Oracle:** a plugin that reads application state and answers questions
  about it, such as "what is alto bar 3 now?"
- **Invariant:** a check that runs after every step and doesn't depend on the
  scenario, such as "no 5xx response."

## Architecture

Five layers. Each depends only on the layers before it and has its own tests.

```text
5. Oracles        invariants after every step; domain oracles by entry point
4. Scenario       @scenario, cast(), actor moves, collide(), pytest plugin
3. Trace store    <scenario>.trace.json; replay and heal rules
2. Step engine    snapshot -> Decider -> execute; operation set; text model
1. Browser pool   one Chromium per actor over CDP; capture; allowlist; teardown
```

The executor adapter sits beside layer 4. It calls layers 1 to 3 and 5 to
run a swarm `Scenario`.

### Layer 1: browser pool

The pool starts and owns one headless Chromium per actor, and drives it
directly over the Chrome DevTools Protocol (CDP). It uses no
`browser_harness` daemon.

**Launch mode.** Used by the pytest runtime. For each actor the pool:

- Starts `chromium` with `--headless=new`, its own `--user-data-dir` under a
  run directory, and its own `--remote-debugging-port=0`. It reads the chosen
  port from `DevToolsActivePort` in the profile directory.
- Starts the process with `start_new_session=True`, so the browser and its
  helpers share one process group that the pool can signal as a group.
- Loads the actor's storageState (cookies plus `localStorage`) before the
  first navigation. It sets cookies with `Network.setCookies`. It sets
  `localStorage` with `Page.addScriptToEvaluateOnNewDocument`: a seeding
  script that runs before the page's own scripts and writes each entry only
  when `location.origin` matches the entry's origin.
- Passes `--host-resolver-rules="MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"` (with
  the allowlisted hosts substituted), so the browser can't resolve any other
  host, WebSockets and workers included.

**Attach mode.** Used by the executor adapter. The swarm's plugin launches
the browsers and passes each actor a `cdp_url`; the interface says an
executor "connects to an endpoint and never starts or shares a browser." In
attach mode the pool connects to that URL, opens its own target, and closes
only that target on exit. It doesn't load storageState (the plugin already
did) and doesn't kill the process.

Both modes present the same `ActorBrowser` interface to layer 2:

```python
class ActorBrowser(Protocol):
    name: str
    async def call(self, method: str, **params: Any) -> dict[str, Any]: ...
    async def evaluate(self, expression: str) -> Any: ...
    async def screenshot(self) -> bytes: ...
    def observations(self, since: int) -> Observations: ...  # console + network
    async def set_input_files(self, node: NodeRef, files: Sequence[Path]) -> None: ...
    async def close(self) -> None: ...
```

**File uploads.** The pool resolves the observed element to a remote object,
asks `DOM.describeNode` for its `backendNodeId`, and calls
`DOM.setFileInputFiles`. File inputs are often visually hidden, so the
snapshot lists them regardless of visibility (see layer 2). Only files a
scenario declares (`files={"score": Path(...)}`) can be uploaded. The model
picks a declared name, never a path.

**Capture.** After `Runtime.enable`, `Log.enable`, and `Network.enable`, the
pool keeps a bounded buffer per actor of:

- `Runtime.consoleAPICalled` (levels `error` and `warning`),
  `Runtime.exceptionThrown`, and `Log.entryAdded`.
- `Network.requestWillBeSent`, `Network.responseReceived`, and
  `Network.loadingFailed`, joined by request ID into one record with the URL,
  method, status, and the monotonic times of the request and the response.

Each buffer entry carries a sequence number. A step reads
`observations(since=seq)` to get the entries from its own window.

**Host allowlist.** The pool enforces the allowlist. A scenario's
instructions to the model can't. Three layers apply, and any one that fails
doesn't disable the others:

1. **Resolver rules** (launch mode): the resolver-rules flag blocks every other host at
   the browser's network stack, which covers WebSockets, WebRTC signalling
   that uses DNS, and service workers.
2. **Request interception**: `Fetch.enable` with a `*` pattern runs on every
   target, including pages and workers that `Target.setAutoAttach`
   (`flatten=True`, `waitForDebuggerOnStart=True`) attaches. The pool answers
   each `Fetch.requestPaused` for a disallowed host with `Fetch.failRequest`
   (`BlockedByClient`) and records an `allowlist` finding. This is the layer
   that works in attach mode.
3. **Navigation guard**: the engine refuses a `Page.navigate` to a
   disallowed origin before it sends the command. `file:`, `data:`, and
   `blob:` navigations are refused by default.

An allowlist entry is a `host:port` pair. The default is the scenario's
base-URL origin and nothing else.

**Teardown.** Teardown runs on every exit path:

- The pool is an async context manager, and a `finally` block closes it.
- An `atexit` hook and `SIGINT` and `SIGTERM` handlers call a synchronous
  `kill_all()` that sends `SIGTERM` to each process group, waits two seconds,
  and then sends `SIGKILL`.
- The pool writes each browser's process-group ID to a pidfile in the run
  directory. At the next start, a sweep stops any group from a dead run.
- The pool removes each `--user-data-dir` it created, after the process
  exits.

**CDP client.** A small client, `sebby.browser.cdp`, wraps one WebSocket
connection: a request ID counter, a future per pending call, an event
dispatcher keyed by method and session ID, and flattened sessions for child
targets. See "Package and dependencies" for why it isn't Playwright.

### Layer 2: step engine

The engine rebuilds jev's proven core. Its snapshot builds on the ideas in
jev's `snapshot.js`, rewritten for this package and not vendored.

**Snapshot.** One `Runtime.evaluate` call returns everything the step needs,
so the page can't change between reads. It returns:

- The URL, title, and up to 6,000 characters of visible text.
- Up to 250 actionable elements, each with a role, an accessible name
  (`aria-labelledby`, `aria-label`, labels, then text, then `title`, then
  `placeholder`), state (`checked`, `expanded`, `selected`, `disabled`), and a
  current value.
- For each element, the **locator ladder** the trace needs, captured now so
  recording and replay share one code path (see layer 3): a test ID
  (`data-testid`, or a configured attribute), role plus accessible name, nearby
  text, and the position among siblings.
- A stable node handle, kept in a page-side `WeakMap`, so an execute step
  reaches the same element the snapshot described.
- A per-element guard: a digest of the element's state and its enclosing
  form or row. The engine recomputes it just before acting, and a mismatch
  raises `StalePage`, as jev does.
- Excluded: password and hidden inputs (their values never reach the model).
  File inputs are included with kind `upload` and no value.

**Operation set.** jev's set, plus two:

| Operation | Meaning | Chosen by |
|---|---|---|
| `CLICK` | Click an element, menu option, or suggestion. | Decider |
| `TYPE_TEXT` | Replace the text in an editable field. | Decider picks the field; text model or scenario supplies the text. |
| `SELECT` | Choose an option in a `<select>`. | Decider |
| `SCROLL` | Scroll by a fixed amount. | Decider |
| `WAIT` | Wait for the page to settle. | Decider |
| `UPLOAD` | Attach a declared file to a file input. | Decider picks the input and the declared file name. |
| `ASSERT` | Check a predicate on the page. | Decider, only when the goal contains a check. |
| `DONE` | Every requirement is visibly met. | Decider |
| `BLOCKED` | No supported operation can progress. | Decider |

`ASSERT` is deterministic once recorded. It has a check kind from a fixed
list (`visible`, `text_equals`, `text_contains`, `value_equals`, `checked`,
`enabled`, `url_matches`), an optional target, and an expected value. The
engine accepts an `ASSERT` from the model only if the expected value appears
as a literal in the goal text. Otherwise it treats the choice as `BLOCKED`,
so the model can't invent what to verify. The recorded predicate is what
replay evaluates.

**Decider protocol.** The decision engine sits behind a small interface:

```python
@dataclass(frozen=True)
class DecisionRequest:
    goal: str
    page: PageView                  # url, title, text, elements
    history: Sequence[StepSummary]  # the last 10 steps
    allowed: frozenset[Operation]
    files: Sequence[str]            # declared upload names

@dataclass(frozen=True)
class Decision:
    operation: Operation
    target: str | None              # an element index from the request
    file: str | None
    confidence: float
    probabilities: Mapping[str, float]
    usage: Usage                    # tokens, for the ledger

class Decider(Protocol):
    async def decide(self, request: DecisionRequest) -> Decision: ...
```

**Default decider.** `TypeSafeDecider` uses TypeSafe `system_one`, through
sebby's existing path and not raw `httpx`. The SDK supports this. The
`typesafe-sdk` 0.6.0 package, already the `judgement` extra, ships an
`AsyncTypeSafeClient` whose `system_one(state, questions, model=..., retry=...)`
accepts `Choice` questions, and its `ChoiceAnswer` carries `choice`,
`confidence`, and `probabilities`, which is what jev's request needs. The
decider:

- Builds the client with `sebby.judgement.make_client(api_key=...,
  client_cls=AsyncTypeSafeClient)`.
- Sends one request per step, as jev does: an `operation` question over the
  allowed operations, and one target question per operation that has
  candidates (`click_target`, `type_text_target`, and so on). It reads the
  target question that matches the chosen operation.
- Keeps jev's response checks that the SDK doesn't perform: the chosen label
  is one of the offered labels, and the probabilities sum to about one.
- Reports usage through `sebby.judgement.record_usage`, so a run's TypeSafe
  spend flows through the same `UsageRecord` callback as `sebby.llm`.
- Sets the model from `SEBBY_BROWSER_MODEL`, with `jev-latest` as the default
  that `make_client` already uses.

The SDK's retry policy applies to each call. `TypeSafeDecider` counts
attempts through the SDK's response metadata when available, so retries
show in the ledger. See "Open questions."

**Text model.** `TYPE_TEXT` needs a value. The value comes from one of three
places, in this order:

1. A literal in the scenario (`actor.type("Key", "G")`): no model call.
2. A literal in the goal text, when the goal quotes it.
3. A `TextModel`, behind the same kind of interface as the decider:

```python
class TextModel(Protocol):
    async def text_for(self, request: TextRequest) -> str | None: ...
```

The default `TextModel` wraps `sebby.llm.LLMClient`, which needs the `llm`
extra. jev used a DeepSeek model for this call. Returning `None` means the
model found no valid value, and the step becomes `BLOCKED`. The engine never
guesses.

**Step loop.** For a goal, until `DONE`, `BLOCKED`, or a cap:

1. Snapshot the page.
2. Ask the Decider for a decision. (In replay, resolve a recorded step
   instead. See layer 3.)
3. Check the guard for the chosen element.
4. Execute: mouse events for `CLICK` after a hit test, `Input.insertText` for
   `TYPE_TEXT`, the pool's file call for `UPLOAD`.
5. Wait for the page to settle (two animation frames, or 200 ms for a
   combobox, as jev does).
6. Snapshot again, write the step record, and run the invariants.

A goal also stops after three consecutive steps that leave the page
unchanged, which is jev's stall rule.

**Directed steps.** `actor.click("Save")` is a one-step goal. The engine
first tries to resolve the target by role plus accessible name against the
snapshot. If exactly one element matches, no model call occurs. If none or
several match, the Decider picks within the `CLICK` operation.

### Layer 3: trace store

**File.** One JSON trace per scenario, next to the scenario module, named
`<scenario_id>.trace.json`. It's committed and reviewed in a diff. The
scenario ID defaults to the test function name, and the plugin refuses two
scenarios with one ID in the same directory. The committed file holds no
timestamps, latencies, or confidence values, so a re-record with no real
change produces no diff. Timing goes into the run report.

Every trace carries `schema_version`. The reader refuses a major version it
doesn't know, and runs registered migrations for older ones. An unreadable or
unknown trace is an `error`, not a silent re-record.

An example, abbreviated:

```json
{
  "schema_version": 1,
  "scenario": "director_saves_while_singer_opens_share",
  "recorded_with": {"sebby": "0.4.0", "decider": "typesafe:jev-latest"},
  "actors": {
    "director": {
      "goals": [
        {
          "id": "director#0",
          "text": "open the piece 'Adeste' and change alto bar 3 to s f m",
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
              "before": {"structure": "9f2c…", "sketch": "…"},
              "after": {"structure": "41ab…", "sketch": "…"}
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
- Hashes of the page before and after the step.

**Page hashes.** A hash can only say "same or different," but replay needs
"close." Each of `before` and `after` therefore holds two values:

- `structure`: SHA-256 of the canonical URL path, the title, and the ordered
  list of `(role, accessible name)` pairs of the actionable elements. It
  ignores body text, so a changing count or timestamp doesn't change it.
- `sketch`: a 32-value MinHash of the same `(role, name)` set. The estimated
  Jaccard similarity of two sketches is the closeness score.

**Replay rules.** In `auto` mode, if a trace exists, the engine replays it.
For each recorded step the engine resolves the target against the current
snapshot, then measures similarity between the recorded `before` sketch and
the current page:

| Situation | Action |
|---|---|
| The target resolves to exactly one element, and the page is identical or close (similarity at least 0.80). | Run the step. No model call. |
| The target resolves to exactly one element, and the page is in the middle band (0.50 up to 0.80). | Re-decide that one step within its goal. Record a heal. |
| The target resolves to zero elements or to several. | Re-decide that one step within its original goal. Record a heal. |
| Similarity is below 0.50, whatever the target does. | The page has drifted too far. Re-run the rest of that goal through the model. Record a heal. |

The two thresholds are defaults, exposed as settings. The middle band is this
spec's answer to a gap in the agreed rules, which didn't say what happens
when the target is unique but the page changed materially (see "Open
questions").

**Target resolution.** The resolver tries the rungs in order of stability
(test ID, role plus name, nearby text, sibling position). It keeps the
candidate set and intersects it with the next rung until one element remains.
It reports zero if the intersection empties, and several if candidates
remain after the last rung. A step that resolves through a lower rung than
recorded still runs. The run report lists it as a `refresh`, not a heal,
because no model call replaced the step (see "Open questions").

**Re-decide one step.** The engine sends the goal, the current snapshot, and
the steps done so far to the Decider, restricted to the recorded operation
family (a recorded `CLICK` may become a different `CLICK`, not a `TYPE_TEXT`).
The new step replaces the old one in the healed trace.

**Re-run the rest of a goal.** The engine discards the recorded remainder of
that goal, runs the goal from the current page through the model until
`DONE`, and replaces the remainder with the new steps. Any `ASSERT` step in
the discarded remainder is kept, with its predicate unchanged, and appended
after the new steps.

**Heal rules.**

- Heals are capped per run (default 3). A heal that would exceed the cap
  isn't attempted, and the step fails with `heal_budget_exceeded`.
- A heal never changes an assertion. It changes how a target is found and
  which action is taken, and nothing about what a check expects. An `ASSERT`
  step's target resolves through the ladder only; if it resolves to zero or
  several elements the assertion fails, and the model isn't asked.
- A run with any heal reports `passed (healed)`. It's never a plain pass.
- A failed assertion fails the run, whatever happened with healing.
- The engine writes a healed trace to the run's evidence directory as
  `<scenario_id>.trace.healed.json` and never overwrites the committed file.
  The orchestrator can write it back with `sebby.browser.traces.promote(...)`,
  which a person or the swarm calls after review.

**Modes.** `--browser-mode` selects how traces and models are used:

| Mode | Behavior |
|---|---|
| `replay-only` | Uses the trace. Any model call is a failure. The default with `--run-browser-agent`, so a run costs nothing. |
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
    await expect.piece("Adeste").voice("alto").bar(3) == "s f m"
    await expect.singer_sees(singer, version="latest")
```

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
    name: str                 # "director-A"
    state_file: Path | None   # storageState JSON, or None to start signed out
    base_url: str
    allowed_hosts: frozenset[str] = frozenset()
```

The plugin gets the resolver from a fixture the host defines, named
`browser_cast_resolver`. Any attribute `cast` doesn't have, such as
`cast.share_url`, falls through to `resolver.helpers`. That's how the host
adds `share_url("Adeste")`, which needs the seeded piece's share token.

**Actors.** Each actor runs as its own asyncio task in the one test process.
The task drains a queue of moves, so one actor's moves run in order while
different actors run at once. `await director.goal(...)` enqueues a goal and
waits for its result. To run goals at the same time, use
`await parallel(a.goal(...), b.goal(...))`.

A move method (`click`, `type`, `upload`, `select`, `reload`, `goto`, `goal`)
returns a lazy `Move`. Awaiting a `Move` runs it. Passing it to `collide`
arms it.

**`collide`.** `collide(*moves, order=None, repeat=1, max_gap_ms=25)` has two
phases:

1. **Prepare.** Each actor's task resolves its move, from the trace or the
   model, and builds the exact CDP commands it will send. For a click, that's
   the hit-tested coordinates and the mouse events. It stops just short of
   sending them.
2. **Fire.** Every actor task waits on one shared release point, an
   `asyncio.Barrier`. When the last actor arrives, all tasks send their
   prepared commands over their own CDP connections. The engine records the
   monotonic time just before each send.

The measured gap is the spread between the send times, reported in the run
report and on the `CollisionReport`. The engine also measures each
connection's round-trip time with a no-op evaluate before the fire, and
reports it. The gap is the client-side spread, and it doesn't show when each
browser handled its event. The report says so.

If the measured gap exceeds `max_gap_ms`, the collision is `missed`. A missed
collision fails the run with `error`, because the test didn't do what it
claims. The engine can't retry it after firing, since a save isn't
repeatable without a reset.

**Ordering.** `order=` forces a sequence. `order=before(director, singer,
ms=50)` fires the director's command, then the singer's about 50 ms later,
and the report records the offset actually achieved. `order="both"` runs the
scenario's collision twice, once in each order, and needs `reset=` (see
`repeat`). Its result compares the state the `observe=` callback returns
after each order, and reports `order_dependent=True` if they differ. That
shows whether the outcome depends on the ordering.

**`repeat=N`** repeats the collision. Because a second save may not act on
the same state, `repeat` needs a `reset=` async callable that the host
provides (for example, "restore the piece to version 1"). Without a `reset`,
`repeat` is allowed only when every move is idempotent (`reload`, `goto`).

**Assertions on state.** `expect.piece(...)` isn't a page assertion. It's an
oracle query (layer 5). `expect` exposes every query that an installed oracle
registers.

**pytest plugin.** `sebby.browser.pytest_plugin`, registered through the
`pytest11` entry point, adds:

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
| `host_allowlist` | A request to a host outside the allowlist was attempted (the pool blocked it). | The allowlist itself. |
| `latency_budget` | The step took longer than its budget, from action to settled page. | A default in ms, and per-operation overrides. |

A failed invariant produces a `Finding` (invariant name, actor, step number,
the offending URL or message). By default the run continues to the end of the
scenario so the report holds all findings, then fails. `fail_fast=True` stops
at the first finding. Invariants attach to steps and not to scenarios, so a
5xx that a background poll triggers still fails the run.

Custom invariants implement `Invariant.check(step, observations) ->
list[Finding]`.

**Domain oracles.** A domain oracle is a plugin. It's found through the
Python entry point group `sebby.browser.oracles`, and the entry point name
becomes the oracle's name. The contract:

```python
class Oracle(Protocol):
    oracle_api: ClassVar[int]           # 1

    async def start(self, ctx: OracleContext) -> None: ...
    def queries(self) -> Mapping[str, Callable[..., Query]]: ...  # names on `expect`
    async def after_step(self, step: StepRecord, ctx: OracleContext) -> list[Finding]: ...
    async def close(self) -> None: ...

class Query(Protocol):
    async def evaluate(self) -> Observed: ...

@dataclass(frozen=True)
class OracleContext:
    actors: Mapping[str, ActorHandle]   # read a page, evaluate JS, read cookies
    base_url: str
    http: AllowlistedHttp               # a client bound to the allowlist
    config: Mapping[str, object]        # from the host project's settings
```

- `queries()` names the methods on `expect`. staff2solfa's plugin can register
  `piece` and `singer_sees`.
- `after_step` lets an oracle check a domain invariant on every step, such as
  "no `Piece` ever loses a voice." It's optional; the default returns nothing.
- An oracle reads state through the application's API or a test probe, and
  through `ActorHandle` for what an actor's page shows. The core package
  doesn't interpret any of it. staff2solfa's plugin compares `Piece`s with
  its relative-pitch model. That plugin is out of scope here.

**The `expect` comparison.** In Python, `await x == y` parses as
`(await x) == y`, so the result of the comparison in the example scenario
would be discarded, and a mismatch would pass silently. To make the example
line assert, `await query` returns an `Observed` value. `Observed.__eq__`
records a check in the run's ledger, a pass or a failure with both values,
and returns the boolean. At the end of the scenario, any failed check in the
ledger fails the run. `Observed` also offers `.equals(expected)`, which does
the same thing and doesn't trip the `B015` lint rule (pointless comparison)
that `sebby`'s ruff config enables. The spec's example uses `==` as agreed,
and the report recommends `.equals` (see "Open questions").

## Executor adapter

The swarm's executor interface (`tests/qa_swarm/executor.py` in staff2solfa,
"Scenario executor interface" section of its spec) defines `PageState`,
`Actor`, `BrowserEndpoint`, `Budget`, `Scenario`, `Cost`, `Result`, and the
`Executor` protocol with `name` and `run(scenario, endpoints, evidence_dir)`.
`sebby.browser` implements it with those names.

**Where the code lives.** The interface types belong to staff2solfa, and
`sebby` can't import them. So the adapter has two parts:

- `sebby.browser.executor.run_goals(...)`: a generic function that takes
  plain values (actors, goals, endpoints, caps, an evidence directory) and
  returns a `RunOutcome` dataclass. It has no swarm names.
- `tests/qa_swarm/browser_executor.py` in staff2solfa: a thin class, about
  100 lines, that implements `Executor` and maps between the swarm types and
  `run_goals`. The registry in `tests/qa_swarm/plugin.py` resolves it by the
  name `sebby-browser`.

**What the adapter does.** `run` builds a pool in attach mode from
`endpoints`, one actor per `scenario.actors` entry, keyed by `Actor.name`. It
then:

1. Runs the scenario's goals. The primary actor (the first in
   `scenario.actors`) pursues `scenario.goal` from `scenario.start_url`. For
   a multi-actor scenario it runs the script described next.
2. Reads the primary actor's final page into a `PageState` (`url`, `text`,
   and `actions` as `{"label", "role"}` dicts from the snapshot), calls
   `scenario.verify`, and returns the dict in `Result.checks`.
3. Evaluates the invariants and oracles. Their outcomes join `Result.checks`
   as extra named checks (`invariant.no_5xx`, `oracle.piece`), so the
   contract's rule that `passed` needs every check true still holds.
4. Writes screenshots, the healed trace, and redacted console and network
   logs only under `evidence_dir`, and lists them in `Result.evidence`.

**Extensions the interface needs.** The interface as written can't carry a
multi-actor scenario, because `Scenario.goal` is one string and `verify` sees
one page. The swarm spec allows "a versioned extension" of `executor.py`, and
the jev adapter ignores it. This spec asks for three additions, each optional
so jev is unaffected:

- `Executor.max_actors: int = 1`. jev keeps the default of one. The
  `sebby-browser` executor declares `max_actors = 8`, and this is how it
  declares multi-actor support. The plugin launches an endpoint per actor up
  to that number and returns `unsupported` past it.
- `Scenario.script: Callable[..., Awaitable[None]] | None = None`. It's a
  function in this spec's DSL (taking `cast` and `expect`). If it's set, the
  executor runs it, and `goal` is descriptive text. If it's `None` and the
  scenario has one actor, the executor runs `goal`. If it's `None` and the
  scenario has several actors, the executor returns `unsupported`, because
  one natural-language string can't say which actor does what.
- `Result.status` gains `"passed_healed"`, for a run whose checks all passed
  after one or more heals. Until the swarm adopts it, the adapter returns
  `"passed"` with `message` starting `passed (healed)` and `cost.detail["heals"]`
  set, and the report must read that field.

**Status mapping.**

| Outcome in `sebby.browser` | `Result.status` |
|---|---|
| Every goal `DONE`, every check true, no heals. | `passed` |
| The same, with one or more heals. | `passed_healed` |
| A check, invariant, or oracle expectation failed. | `failed` |
| The heal cap was exceeded. | `failed` |
| A goal returned `BLOCKED`. | `blocked` |
| The billed-step budget ran out. | `budget_exhausted` |
| More actors than `max_actors`, or several actors with no `script`. | `unsupported` |
| A CDP failure, a stale page that didn't recover, a missed collision, an unreadable trace, or a missing billed-mode opt-in. | `error` |

**Cost.** `Cost.steps` counts the steps that the model decided, including
heals. Replayed steps cost nothing, so `plugin.py`'s reserve-and-refund
budget doesn't tie up steps for them. `Cost.provider_calls` counts billed
calls: decisions plus text-model calls. `Cost.detail` carries
`replayed_steps`, `heals`, `refreshes`, and `tokens`. The executor stops at
`Budget.max_steps` on model-decided steps, and a separate cap of four times
that value bounds replayed steps, so a loop can't run forever for free.

**No secrets.** The adapter, like jev's, holds its own provider keys
(`TYPESAFE_API_KEY`, and the text model's key if used). It never writes them
to `history`, `evidence`, or `message`. Values marked `secret=True` are
masked in the history.

## Package and dependencies

### Layout

```text
src/sebby/browser/
  __init__.py          public API: scenario, collide, run_goals, Decider, Oracle
  cdp/
    client.py          the WebSocket client and event dispatcher
    session.py         flattened sessions, target management
  pool.py              launch and attach modes, the ActorBrowser interface
  launch.py            Chromium discovery, flags, process groups, teardown
  allowlist.py         resolver rules, Fetch interception, navigation guard
  capture.py           console and network buffers
  snapshot.js          the page-side snapshot script
  snapshot.py          the snapshot's Python types and hashing helpers
  engine.py            the step loop, the operation set, the guard
  decider.py           Decider, DecisionRequest, TypeSafeDecider
  text.py              TextModel and the default wrapper
  locators.py          the locator ladder and target resolution
  trace.py             the trace schema, reader, writer, migrations
  replay.py            replay rules and heals
  hashing.py           the structure hash and the MinHash sketch
  runtime/
    dsl.py             @scenario, Move, cast, parallel
    actor.py           the per-actor task and queue
    collide.py         prepare, fire, ordering, the collision report
    expect.py          expect, Observed, the ledger
  oracles.py           Oracle, Query, invariants, entry-point loading
  invariants.py        the four built-in invariants
  pytest_plugin.py     the marker, flags, and fixtures
  executor.py          run_goals and RunOutcome
  report.py            the run report
```

### The `sebby[browser]` extra

```toml
browser = [
    "sebby[judgement]",
    "websockets>=13.0",
]
```

`sebby[judgement]` brings `typesafe-sdk`, and with it `httpx2`, `msgspec`, and
`tenacity`. The only new dependency is `websockets`. `pytest` isn't in the
extra, because only a project that uses the DSL loads the plugin, and that
project has pytest. The default text model needs the `llm` extra, which a
project installs separately if it uses model-written text (see "Open
questions"). The `dev` extra adds `sebby[browser]`.

### Raw CDP over `websockets`, not Playwright

Playwright for Python has a `connect_over_CDP` mode. This design uses a small
CDP client over `websockets` instead, for four reasons:

- **Dependency weight.** Playwright ships a Node driver and a large package,
  and installs its own browsers. `websockets` is one pure-Python package. The
  spec's constraint is minimal dependencies.
- **Timing.** A collision needs the shortest, most predictable path from the
  Python process to Chromium. Playwright puts a driver process and its own
  protocol between them. A direct WebSocket lets each actor send its prepared
  command within microseconds of the release.
- **Fidelity.** Playwright documents that `connect_over_CDP` has lower
  fidelity than its own launch mode. The design needs `Fetch`,
  `Target.setAutoAttach`, and `DOM.setFileInputFiles` exactly as CDP defines
  them.
- **No duplicate work.** The snapshot, hit test, and guard run in page-side
  JavaScript, and the engine's freshness rules replace Playwright's
  actionability checks.

The price is real. The project owns roughly 400 lines of CDP client,
connection handling, and target management, and Playwright's mature handling
of edge cases such as frames and popups isn't inherited. Two mitigations
apply: the code uses a short, listed set of CDP methods, and an integration
test (see "Testing") runs each against a real Chromium, so protocol drift
fails a test and not a run. Playwright stays a possible fallback for the pool
layer behind the `ActorBrowser` interface, if CDP churn costs too much.

Chromium comes from `SEBBY_BROWSER_CHROMIUM`, or else the first match among
common install paths and a Playwright browser cache. The package doesn't
import Playwright and doesn't install a browser.

## Cost and budget controls

TypeSafe calls are billed, and so are text-model calls. Controls apply at
five points:

- **Opt-in.** `--run-browser-agent` runs scenarios, and by default only in
  `replay-only` mode. A billed call needs `--browser-allow-billed` as well.
  A billed live check needs both flags and its own marker.
- **Replay first.** Once a trace exists, a passing run makes zero model
  calls. Cost is paid at record time and at each heal.
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

- **The allowlist is enforced in the browser pool** by resolver rules,
  request interception, and a navigation guard, as layer 1 describes. A
  blocked request becomes a finding.
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
- **Downloads and permissions.** The pool sets `Browser.setDownloadBehavior`
  to deny, and denies permission prompts.
- **Process safety.** Teardown covers every exit path, and profile folders
  are removed.

## Testing

Tests live in `sebby`'s `tests/browser/` and run under `sebby`'s pytest, ruff,
and strict mypy gates.

**Unit tests, with no browser and no model.**

- **Target resolution ranking.** The ladder picks a test ID over a role plus
  name, intersects rungs to break ties, reports zero and several correctly,
  and gives the same answer for the same snapshot.
- **Trace read and write.** A round trip is byte-stable, the committed file
  has no timing fields, an unknown major `schema_version` is refused, and an
  older version migrates.
- **Heal rules.** Each row of the replay table, the cap, the rule that a heal
  never changes an `ASSERT` predicate, `passed_healed` for any heal, and a
  failed assertion failing a healed run.
- **Barrier and timing logic.** With fake connections, the release point
  fires all prepared commands, the gap is computed from the send times, a
  gap over `max_gap_ms` becomes `missed`, and `order=` produces the requested
  offset.
- **Invariant checks.** Each built-in invariant against synthetic observation
  windows, including its ignore lists.
- **Hashing.** The structure hash ignores body text, and the sketch's
  similarity is stable and monotonic as elements change.
- **Allowlist and the CDP client** against a fake WebSocket server.

**Integration tests.** Headless Chromium against local fixture pages, one
with a deliberate race, and a fake `Decider` that returns scripted choices.
They make no TypeSafe calls.

- Replay makes zero calls to the fake `Decider`.
- A changed page (a renamed button) triggers exactly one heal, and the trace
  the run writes differs from the original in one step.
- Collisions land within the target gap on an idle machine, and the race page
  produces a different result for each order.
- Invariants catch a planted 5xx and a planted console error.
- An upload reaches a file input through `DOM.setFileInputFiles`.
- A request to a host outside the allowlist is blocked in both launch and
  attach mode.
- Each CDP method the package uses succeeds against the installed Chromium.
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
- **Hash thresholds need tuning.** The 0.80 and 0.50 similarity thresholds
  are guesses. Too high causes needless heals, and too low replays steps on
  a page the recording didn't see. The report lists each step's similarity so
  the values can be tuned from real runs.
- **Collision timing is flaky on a loaded machine.** One process and one
  event loop drive every actor, so a busy machine widens the gap. The report
  states the measured gap, a miss fails the run, and the project's machine
  rule (five processes at most) applies to the browsers too.
- **Client-side gap only.** The measured gap is the spread of send times. It
  doesn't include the browser's own event handling.
- **A healed run can hide a regression.** The heal that finds a moved button
  is the same heal that finds a removed one. Assertions never heal, the cap
  bounds it, and `passed (healed)` is never a plain pass, so a person reviews
  each healed trace before promoting it.
- **The interface changes.** The extensions the adapter section lists touch the swarm's
  interface. The swarm spec expects this and allows a versioned extension.

## Slices

Three slices, each shippable and tagged in `sebby`. staff2solfa moves to a
slice by bumping its git tag. Estimates are wall-clock best guesses for
agent-driven work with wave review, not measurements.

| Slice | Contents | Estimate |
|---|---|---|
| A. Single actor | The CDP client, browser pool (launch and attach modes, capture, allowlist, uploads, teardown), the snapshot, the step engine, `Decider` and `TypeSafeDecider`, the text model, the four built-in invariants, and the unit and integration tests for them. A single-actor `run_goals`. | About 14 hours |
| B. Traces | Trace schema and store, locator ladder, hashing and sketches, replay rules, heals and the cap, modes, the healed-trace write and `promote`, the budget ledger, and their tests. | About 9 hours |
| C. Multi-actor | The DSL and per-actor tasks, `cast` and `CastResolver`, `collide` with ordering and `repeat`, the oracle contract and `expect`, the pytest plugin, the swarm adapter and interface extension, and the planted-defect acceptance run. | About 13 hours |

Total: about 36 hours.

Slice A alone gives the swarm a single-actor executor with uploads and
invariants, so it can replace jev for those scenarios. Slice B adds the cost
reduction. Slice C adds what jev can't do. The acceptance run happens at the
end of slice C, in staff2solfa, and depends on the swarm's own slices being
merged.

Slice C is the largest. If it runs long, it splits at the oracle contract:
collisions and the DSL (about 7 hours), then oracles, the adapter, and
acceptance (about 6 hours).

## Open questions

Each item has a proposed answer, used in this spec until the user decides.

1. **The example's `==` line.** As written, `await expect...bar(3) == "s f m"`
   discards the comparison and can't fail, and ruff's `B015` flags it. The
   spec makes it work through an `Observed` ledger and adds `.equals(...)`.
   Should the canonical form be `.equals(...)`, with `==` kept as a
   convenience?
2. **Swarm interface extensions.** The spec asks for `Executor.max_actors`,
   `Scenario.script`, a `passed_healed` status, and oracle and invariant
   entries in `Result.checks`. The swarm spec is in review in another
   worktree. Should these go in its interface now, or does the adapter
   carry them privately at first?
3. **Adapter placement.** The adapter's mapping code lives in staff2solfa,
   with a swarm-free `run_goals` in sebby. The alternative is a duck-typed
   adapter in sebby that takes the host's types as arguments.
4. **Allowlist strength in attach mode.** The resolver-rules layer needs
   launch flags, which the swarm's plugin sets when it starts the browsers.
   Until the plugin passes them, attach mode relies on `Fetch` interception,
   which doesn't cover a WebSocket opened by a page. Should the swarm's
   launcher pass the flag?
5. **The default text model.** The default `TextModel` needs `sebby[llm]`
   (`litellm`), which the `browser` extra doesn't include. staff2solfa has it.
   Should `browser` include `llm`, or should the extra stay minimal?
6. **Chromium supply.** The package finds a Chromium and doesn't install one.
   staff2solfa has Playwright's browsers from `make e2e-install`. Is that
   the source to use, or should the package document a system Chrome?
7. **Default numbers.** Similarity of 0.80 and 0.50, a heal cap of 3, a
   collision gap of 25 ms, and eight actors are placeholders. Confirm or
   change them.
8. **Middle-band and refresh rules.** The spec re-decides one step when the
   target is unique but the page similarity is between 0.50 and 0.80, and it
   reports a lower-rung resolution as a `refresh`, not a heal. The agreed
   rules don't cover either case.
9. **Assertion authoring.** `ASSERT` accepts an expected value only if it
   appears in the goal text, and an `ASSERT` target that resolves to zero or
   several elements fails and doesn't heal. Is this stricter than intended?
10. **Billed-step counting.** `Cost.steps` counts model-decided steps only,
    so replays don't consume the swarm's `max_steps_total`. The interface
    says the budget counts "executor steps."
11. **Collision reset.** `repeat` and `order="both"` need a host-supplied
    `reset=`. Does staff2solfa's seeding support restoring one piece between
    runs?
12. **Trace paths for the swarm.** The adapter stores traces at
    `<trace_dir>/<scenario.id>.trace.json`, and `trace_dir` is adapter
    configuration, because the interface's `Scenario` has no file path.
13. **TypeSafe retry accounting.** The SDK retries by default. This spec
    counts retries as billed calls if the response metadata shows them. It
    doesn't verify that the SDK exposes an attempt count.
14. **Acceptance threshold.** "Lower cost per run" has no number. A proposal:
    a replay run makes at most one fifth of jev's `provider_calls` on the
    same batch.
15. **Release tags.** The spec assumes one `sebby` tag per slice, following
    the current `v0.2.0`, but doesn't fix the numbers.
