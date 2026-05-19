# Resource Guardrails

## Purpose

Gatherlink must remain stable under bad networks, abusive inputs, and helper
failures.

## Guardrail categories

Use limits for per-path queues, diagnostics queues, hook execution, DNS
concurrency, bootstrap attempts, carrier discovery attempts, replay windows,
reorder buffers, metrics history, and log volume.

## Queue policy

All queues should be bounded. When full: drop according to explicit policy,
increment counters, emit rate-limited diagnostics, and never block unrelated
paths.

## Hook policy

Hooks need timeout, debounce, rate limit, max concurrency, structured input, and
clear logs.

## DNS policy

DNS helper needs max concurrent queries, per-upstream timeout, cache size limits,
domain-set load limits, and serve-stale bounds.

## Diagnostics policy

Diagnostics must never break dataplane. If a sink is slow, buffer only up to a
limit, drop diagnostics if needed, mark sink degraded, and continue transport.

## Invalid packet policy

Invalid packets should be cheap to reject: silent drop, rate-limited counters, no
unauthenticated replies, and bounded validation work.
