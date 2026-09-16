# Changelog

## 0.2.0

- Add `sebby.judgement`: `make_client`/`record_usage` helpers wiring up
  TypeSafe's Python SDK with sebby's explicit-config convention and
  `sebby.llm`'s `UsageRecord` telemetry shape.

## 0.1.0

- Add `sebby.retry`: `retry_with_backoff` and `retry_once_on_server_error`.
- Add `sebby.llm`: `LLMClient` with multi-provider failover, Anthropic
  prompt-cache breakpoint marking, and pluggable usage telemetry.
