# Diagnostics

Diagnostics are structured facts first and terminal text second.

The current MVP path has three operator surfaces:

- `gatherlink services status NAME` for one live service snapshot
- `gatherlink services monitor NAME...` for continuously refreshed counters
- JSONL diagnostics from the diagnostics event bus

Stable event codes live in `docs/operations/diagnostics-events.md`. Producers
should publish `DiagnosticEvent` records through the bounded diagnostics bus
instead of writing one-off JSON or parsing terminal text. The bus must never
block dataplane or control loops; if a sink is slow, diagnostics are dropped and
the queue drop counter/event tells the operator what happened.

MVP producer coverage includes:

- startup warnings for plaintext/static/manual security choices
- service bind lifecycle events
- runtime startup failures
- scheduler reapply results and skips
- core counter snapshots
- runtime shutdown
- helper stream opened/closed/denied/unreachable/invalid-frame events

The service monitor is derived from the live service IPC status shape, not by
parsing logs. JSONL is the first durable sink. Future sinks such as Prometheus,
WebSocket, or a local API should consume the same event DTOs.

When adding diagnostics:

- choose an existing stable event code or add one in
  `docs/operations/diagnostics-events.md`
- keep payloads structured in `details`
- redact secrets, keys, tokens, and private endpoint material
- prefer counters and facts over prose-only messages
- keep helper diagnostics in helper-owned Python code
