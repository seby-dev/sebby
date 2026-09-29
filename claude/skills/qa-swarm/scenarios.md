# Scenario guide for the qa-tester

A scenario is a reusable test of one behavior. You write it as a `Scenario`, and the configured executor runs it. This guide names no executor, and a scenario file never does either.

## The format

A scenario file lives in `testers/<tester>/scenarios/test_<slug>.py`:

    import pytest

    from qa_swarm.executor import Actor, Budget, PageState, Scenario, assert_passed

    pytestmark = pytest.mark.qa_swarm

    GOAL = """\
    Click "Type sol-fa text directly". Set Key to "C" ...
    """


    def verify(page: PageState) -> dict[str, bool]:
        return {
            "on_results_screen": page.url.rstrip("/").endswith("/results"),
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

`STATE_FILE` and `SITE_URL` come from your charter.

## Rules

- **One behavior per scenario.** Name what a person does and what they see.
- **The goal is natural language.** Name the exact labels the page shows and the values to type. State the stop condition.
- **`verify` never trusts the agent's claim.** It reads the final page (`page.url`, `page.text`, `page.actions`) and returns named boolean checks. A scenario passes only if every check is true.
- **An upload needs a setup step.** The executor can't fill a file input. Seed the piece through the API in your test before you call `run_scenario`, or run a `playwright-cli` `upload` first (see `browser.md`), then let the executor take over from the resulting page.
- **Keep the budget honest.** `Budget.max_steps` is the most steps the scenario may spend, and the run's total budget is limited.
- **A flaky scenario isn't committed.** If it passes once and fails once, report that as a finding about the scenario. Don't hand it to the orchestrator as passing.

## Running a draft

Run it with `scripts/qa_env.sh scenario-run --run-dir <run> --tester <you> <draft>`. Read the result. A failed check is either a finding or a bad scenario. Rerun once with a corrected goal before you decide which, and say in each finding whether `verify` failed or the executor reported `blocked`.
