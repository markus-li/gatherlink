# Policy Advisor

## Purpose

A future policy advisor may dynamically tune scheduler parameters from local
metrics and history.

It is not the scheduler.

## Correct layering

```text
metrics/history
  -> policy advisor
  -> scheduler parameters
  -> deterministic scheduler
  -> Rust dataplane
```

The advisor may suggest:

- path weights
- duplicate-small-packet thresholds
- carrier retry intervals
- failover sensitivity
- warmup duration
- WSS/QUIC penalties
- MTU probe aggressiveness
- anomaly flags
- failover sensitivity suggestions
- queue-age threshold suggestions

The scheduler remains deterministic and explainable.

## Local-first

The advisor should run locally by default.

Do not require a cloud AI service to make routing decisions.

Fleet/cloud systems may later aggregate anonymized opt-in patterns, but packet
routing must not depend on external AI.

## Compute model

Do not use an LLM for hot-path routing.

Useful techniques are lightweight:

- EWMA
- CUSUM anomaly detection
- smoothing
- online regression
- simple classifiers
- tiny gradient-boosted model
- path behavior classification

This can run periodically, e.g. every 1-10 seconds, as a background task.

## Timing

Do not implement before:

- metrics are stable
- scheduler behavior is trustworthy
- diagnostics are good
- real traces exist
- replayable scenarios exist

This is a future optimization layer, not v1.
