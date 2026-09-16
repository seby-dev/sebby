# sebby

Shared Python utilities: a multi-provider LLM client with built-in Anthropic
prompt caching, plus retry helpers. Used across sebby's projects instead of
reimplementing the same patterns in each one.

## Install

    uv add git+https://github.com/seby-dev/sebby --tag v0.1.0

Pin to a tag or commit; bump deliberately.

## `sebby.retry`

    from sebby.retry import retry_with_backoff

    result = retry_with_backoff(
        lambda: call_something_flaky(),
        max_attempts=3,
        retryable_exceptions=(ConnectionError,),
    )

## `sebby.llm`

    from sebby.llm import LLMClient, ProviderConfig

    client = LLMClient(
        providers=[
            ProviderConfig(provider="anthropic", model="claude-sonnet-5"),
            ProviderConfig(provider="openai", model="gpt-5.1"),
        ],
        on_usage=lambda record: print(record),
    )

    response = client.complete(
        system="You are a helpful assistant.",
        messages=[{"role": "user", "content": "Hello"}],
    )

`LLMClient` tries providers in order, retrying each with backoff before
failing over to the next. For the `anthropic` provider, it automatically
marks a prompt-cache breakpoint on the last message and the last tool
schema, respecting Anthropic's four-breakpoint-per-request cap.

## Modules

| Module | Purpose |
|---|---|
| `sebby.retry` | Generic retry/backoff and retry-once-on-server-error helpers |
| `sebby.llm` | Multi-provider LLM client with Anthropic prompt caching |
