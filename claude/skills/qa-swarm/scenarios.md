# Scenario guide for the qa-tester

A scenario is a reusable test of one behavior. You write it as a `Scenario`, and the configured executor runs it. This guide names no executor, and a scenario file never does either.

## The format

A scenario file lives in `testers/<tester>/scenarios/test_<slug>.py`:

    import os
    from pathlib import Path

    import pytest

    from qa_swarm.executor import Actor, Budget, PageState, Scenario, assert_passed

    pytestmark = pytest.mark.qa_swarm

    SITE_URL = os.environ["QA_SITE_URL"]
    STATE_FILE = Path(os.environ["QA_RUN_DIR"]) / "state" / "transcriber-a@choir.test.json"

    GOAL = """\
    Paste this sol-fa text into the paste box and read it ...
    """


    def verify(page: PageState) -> dict[str, bool]:
        return {
            "on_piece_screen": "/piece" in page.url,
            "has_download": any(a["label"].startswith("Download ") for a in page.actions),
            "no_error_text": "error" not in page.text.lower(),
        }


    def test_solfa_text_wizard(run_scenario):
        result = run_scenario(
            Scenario(
                id="solfa-text-wizard",
                goal=GOAL,
                verify=verify,
                actors=(Actor("transcriber-a", "transcriber-a@choir.test", STATE_FILE),),
                budget=Budget(max_steps=40),
                start_url=SITE_URL,
            )
        )
        assert_passed(result)

The example is illustrative: take the labels and routes from your charter and the app, not from this file. `scenario-run` sets `QA_SITE_URL` and `QA_RUN_DIR` for the scenario's process. Read both from the environment, and don't hard-code a port or a path, so a committed copy still runs. `STATE_FILE` must be a `Path`, because the plugin calls `read_text()` on it. The orchestrator's `session` command writes each state file to `<run folder>/state/<email>.json`.

## Rules

- **One behavior per scenario.** Name what a person does and what they see.
- **The goal is natural language.** Name the exact labels the page shows and the values to type. State the stop condition.
- **`verify` never trusts the agent's claim.** It reads the final page (`page.url`, `page.text`, `page.actions`) and returns named boolean checks. A scenario passes only if every check is true.
- **An upload needs a setup step.** The executor can't fill a file input. Seed the piece through the API in your test before you call `run_scenario`, or run a `playwright-cli` `upload` first (see `browser.md`), then let the executor take over from the resulting page.
- **Keep the budget honest.** `Budget.max_steps` is the most steps the scenario may spend, and the run's total budget is limited.
- **A flaky scenario isn't committed.** If it passes once and fails once, report that as a finding about the scenario. Don't hand it to the orchestrator as passing.

## Running a draft

Run it with `<qa_env.sh path> scenario-run --run-dir <run> --tester <you> <draft>`, where `<qa_env.sh path>` is the absolute path in your charter. Read the result. A failed check is either a finding or a bad scenario. Rerun once with a corrected goal before you decide which, and say in each finding whether `verify` failed or the executor reported `blocked`.
