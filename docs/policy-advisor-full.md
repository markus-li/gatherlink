# Policy Advisor Full Design Notes

## Purpose

A future policy advisor may tune scheduler policy from rolling metrics.

It is not the scheduler.

## Layering

```text
metrics/history -> advisor -> scheduler parameters -> deterministic scheduler
```

## Possible outputs

- path weight suggestions
- carrier penalty suggestions
- retry interval adjustments
- warmup duration adjustments
- duplicate-small-packet threshold changes
- anomaly flags
- MTU probing suggestions
- failover sensitivity suggestions
- queue-age threshold suggestions

## Local-first

Policy advisor should run locally.

Do not require cloud AI for routing decisions.

## Compute model

Use lightweight models/statistics:

- EWMA
- CUSUM
- online regression
- simple classifiers
- tiny gradient boosting
- path behavior classification

Do not use LLMs for packet routing.

## Timing

Implement after metrics, diagnostics, and deterministic schedulers are mature.
