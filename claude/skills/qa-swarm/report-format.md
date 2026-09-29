# Finding format

Write one file per finding to `testers/<tester>/findings/<id>.md`. The orchestrator merges them into `REPORT.md`.

| Field | Content |
|---|---|
| ID | `<tester>-<number>`, where `<tester>` is your tester name: for example `qa-1-03`, or `adv-1-02` for the adversarial tester `adv-1`. |
| Tester and role | Your name, your agent role, and the identity role you acted as. |
| Severity | `blocker`, `high`, `medium`, or `low`. |
| Title | One line that names the symptom. |
| Method and route | The HTTP method and route template, when a request is involved. |
| Steps to reproduce | The exact commands or requests, in order, from a state file, or the scenario draft's path. |
| Expected and actual | What the spec or common sense expects, and what happened. |
| Collision | The scenario id, the method (barrier click, repeat click, parallel fetch, or parallel sessions), and the `barrier.sh spread` output, or `abandoned`. Only findings from a collision scenario have it. |
| Evidence | Screenshot paths, console lines, network entries, and response bodies, quoted or by path. |
| Reproduced | Left for the orchestrator to fill in: yes, no, or not attempted. |

## Severity

- **Blocker:** the feature can't be used, or data is lost or exposed across workspaces.
- **High:** a main flow fails, or a security control can be bypassed.
- **Medium:** a secondary flow fails, or a defect has a workaround.
- **Low:** cosmetic or minor friction.

A security finding's severity follows reachability: a flaw that a signed-out caller reaches from the browser rates higher than the same flaw that needs a Director's session or a direct call to the backend port.

## Before you write a finding

Check the known-artifacts list in your charter. The isolated instance differs from the deployed app in ways the list names, and a difference on the list isn't a bug.
