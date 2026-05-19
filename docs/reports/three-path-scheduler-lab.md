# Three-path scheduler lab report

Status: historical report from the scheduler implementation period. It records
the deterministic scheduler matrix and saved lab evidence used to build the v0.9
scheduler. Current scheduler behavior is specified in `docs/runtime/scheduler.md`
and current release gates are tracked in `docs/operations/v0.9-release-checklist.md`.

This report combines deterministic scheduler-policy decisions with saved three-path lab snapshots. The deterministic matrix checks every Python scheduler policy and the Rust compile target it maps to. The lab snapshots check the current runnable local testbed: network shaping, bidirectional UDP traffic, per-path counters, control metadata, and NTP status.

## Scheduler policy matrix

| scenario | policy | rust target | selected path | reason |
| --- | --- | --- | --- | --- |
| clean-balanced | `round_robin` | `round_robin` | path-a | first eligible path in weighted sequence |
| clean-balanced | `weighted_round_robin` | `weighted_round_robin` | path-a | first eligible path in weighted sequence |
| clean-balanced | `srtt` | `lowest_latency` | path-a | lowest latency |
| clean-balanced | `lowest_latency` | `lowest_latency` | path-a | lowest latency |
| clean-balanced | `loss_aware` | `loss_aware` | path-a | lowest loss with latency tie-breaker |
| clean-balanced | `capacity_aware` | `capacity_aware` | path-c | highest TX capacity |
| clean-balanced | `least_queue` | `least_queue` | path-a | lowest queue pressure |
| clean-balanced | `earliest_completion_first` | `earliest_completion_first` | path-a | lowest latency plus transmit time |
| clean-balanced | `blocking_estimation` | `blocking_estimation` | path-a | lowest completion time plus reorder hold |
| clean-balanced | `balanced` | `balanced` | path-a | best hybrid capacity/latency/loss/queue score |
| clean-balanced | `adaptive` | `adaptive` | path-a | best hybrid capacity/latency/loss/queue score |
| loss-on-fast-path | `round_robin` | `round_robin` | path-a | first eligible path in weighted sequence |
| loss-on-fast-path | `weighted_round_robin` | `weighted_round_robin` | path-a | first eligible path in weighted sequence |
| loss-on-fast-path | `srtt` | `lowest_latency` | path-a | lowest latency |
| loss-on-fast-path | `lowest_latency` | `lowest_latency` | path-a | lowest latency |
| loss-on-fast-path | `loss_aware` | `loss_aware` | path-b | lowest loss with latency tie-breaker |
| loss-on-fast-path | `capacity_aware` | `capacity_aware` | path-c | highest TX capacity |
| loss-on-fast-path | `least_queue` | `least_queue` | path-a | lowest queue pressure |
| loss-on-fast-path | `earliest_completion_first` | `earliest_completion_first` | path-a | lowest latency plus transmit time |
| loss-on-fast-path | `blocking_estimation` | `blocking_estimation` | path-a | lowest completion time plus reorder hold |
| loss-on-fast-path | `balanced` | `balanced` | path-b | best hybrid capacity/latency/loss/queue score |
| loss-on-fast-path | `adaptive` | `adaptive` | path-b | best hybrid capacity/latency/loss/queue score |
| high-capacity-slow-path | `round_robin` | `round_robin` | path-a | first eligible path in weighted sequence |
| high-capacity-slow-path | `weighted_round_robin` | `weighted_round_robin` | path-a | first eligible path in weighted sequence |
| high-capacity-slow-path | `srtt` | `lowest_latency` | path-a | lowest latency |
| high-capacity-slow-path | `lowest_latency` | `lowest_latency` | path-a | lowest latency |
| high-capacity-slow-path | `loss_aware` | `loss_aware` | path-a | lowest loss with latency tie-breaker |
| high-capacity-slow-path | `capacity_aware` | `capacity_aware` | path-c | highest TX capacity |
| high-capacity-slow-path | `least_queue` | `least_queue` | path-a | lowest queue pressure |
| high-capacity-slow-path | `earliest_completion_first` | `earliest_completion_first` | path-b | lowest latency plus transmit time |
| high-capacity-slow-path | `blocking_estimation` | `blocking_estimation` | path-b | lowest completion time plus reorder hold |
| high-capacity-slow-path | `balanced` | `balanced` | path-b | best hybrid capacity/latency/loss/queue score |
| high-capacity-slow-path | `adaptive` | `adaptive` | path-b | best hybrid capacity/latency/loss/queue score |
| queue-pressure | `round_robin` | `round_robin` | path-a | first eligible path in weighted sequence |
| queue-pressure | `weighted_round_robin` | `weighted_round_robin` | path-a | first eligible path in weighted sequence |
| queue-pressure | `srtt` | `lowest_latency` | path-a | lowest latency |
| queue-pressure | `lowest_latency` | `lowest_latency` | path-a | lowest latency |
| queue-pressure | `loss_aware` | `loss_aware` | path-a | lowest loss with latency tie-breaker |
| queue-pressure | `capacity_aware` | `capacity_aware` | path-c | highest TX capacity |
| queue-pressure | `least_queue` | `least_queue` | path-b | lowest queue pressure |
| queue-pressure | `earliest_completion_first` | `earliest_completion_first` | path-a | lowest latency plus transmit time |
| queue-pressure | `blocking_estimation` | `blocking_estimation` | path-a | lowest completion time plus reorder hold |
| queue-pressure | `balanced` | `balanced` | path-a | best hybrid capacity/latency/loss/queue score |
| queue-pressure | `adaptive` | `adaptive` | path-a | best hybrid capacity/latency/loss/queue score |
| jitter-reorder-risk | `round_robin` | `round_robin` | path-a | first eligible path in weighted sequence |
| jitter-reorder-risk | `weighted_round_robin` | `weighted_round_robin` | path-a | first eligible path in weighted sequence |
| jitter-reorder-risk | `srtt` | `lowest_latency` | path-a | lowest latency |
| jitter-reorder-risk | `lowest_latency` | `lowest_latency` | path-a | lowest latency |
| jitter-reorder-risk | `loss_aware` | `loss_aware` | path-a | lowest loss with latency tie-breaker |
| jitter-reorder-risk | `capacity_aware` | `capacity_aware` | path-c | highest TX capacity |
| jitter-reorder-risk | `least_queue` | `least_queue` | path-a | lowest queue pressure |
| jitter-reorder-risk | `earliest_completion_first` | `earliest_completion_first` | path-a | lowest latency plus transmit time |
| jitter-reorder-risk | `blocking_estimation` | `blocking_estimation` | path-a | lowest completion time plus reorder hold |
| jitter-reorder-risk | `balanced` | `balanced` | path-a | best hybrid capacity/latency/loss/queue score |
| jitter-reorder-risk | `adaptive` | `adaptive` | path-a | best hybrid capacity/latency/loss/queue score |

## Saved lab runs

| run | sink packets | reply packets | missed | reordered | needs reorder | qdisc drops | ntp |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| clean | 1349 | 4349 | 0 | 0 | 0 | 0 | synchronized (time.cloudflare.com) |
| forced_drop | 1998 | 12498 | 3459 | 4 | 8 | 3459 | synchronized (time.cloudflare.com) |
| latency_skew | 2006 | 2006 | 3 | 1335 | 2673 | 0 | synchronized (time.cloudflare.com) |
| lossy_fast | 1957 | 1957 | 58 | 1322 | 2673 | 29 | synchronized (time.cloudflare.com) |
| saturated | 2020 | 11020 | 652 | 2 | 4 | 652 | synchronized (time.cloudflare.com) |

These snapshots were taken from fresh three-path lab starts for each named mode. The generated report is repeatable with `gatherlink lab scheduler-report --results-dir .lab/local-three-path/results-fresh`.

## Per-path evidence

| run | path | rx packets | tx packets | missed | qdisc drops | tx cap | rx cap | tx mean latency | rx mean latency |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| clean | path-a | 450 | 1450 | 0 | 0 | 50.0Mbit/s | - | 2.0ms | 2.0ms |
| clean | path-b | 450 | 1450 | 0 | 0 | 50.0Mbit/s | - | 2.1ms | 2.1ms |
| clean | path-c | 449 | 1449 | 0 | 0 | 50.0Mbit/s | - | 2.0ms | 2.0ms |
| forced_drop | path-a | 666 | 4166 | 1289 | 1289 | 50.0Mbit/s | - | 5.6ms | 5.6ms |
| forced_drop | path-b | 666 | 4166 | 2131 | 2131 | 50.0Mbit/s | - | 5.5ms | 5.5ms |
| forced_drop | path-c | 666 | 4166 | 39 | 39 | 50.0Mbit/s | - | 6.9ms | 6.9ms |
| latency_skew | path-a | 670 | 670 | 0 | 0 | 50.0Mbit/s | - | 13.6ms | 13.6ms |
| latency_skew | path-b | 670 | 670 | 0 | 0 | 50.0Mbit/s | - | 51.1ms | 51.1ms |
| latency_skew | path-c | 666 | 666 | 3 | 0 | 50.0Mbit/s | - | 139.3ms | 139.3ms |
| lossy_fast | path-a | 633 | 633 | 58 | 29 | 50.0Mbit/s | - | 13.8ms | 13.8ms |
| lossy_fast | path-b | 662 | 662 | 0 | 0 | 50.0Mbit/s | - | 40.5ms | 40.5ms |
| lossy_fast | path-c | 662 | 662 | 0 | 0 | 50.0Mbit/s | - | 58.2ms | 58.2ms |
| saturated | path-a | 674 | 3674 | 0 | 0 | 50.0Mbit/s | - | 6.7ms | 6.7ms |
| saturated | path-b | 673 | 3673 | 652 | 652 | 50.0Mbit/s | - | 9.4ms | 9.4ms |
| saturated | path-c | 673 | 3673 | 0 | 0 | 50.0Mbit/s | - | 6.2ms | 6.2ms |

## Readout

- `clean` should stay boring: no drops, no missed packets, and roughly even path use from the current lab sender.
- `saturated` and `forced_drop` should expose capacity limits as qdisc drops and receiver-side missed packets.
- `lossy_fast` is the important scheduler trap: latency-only scheduling likes path-a, while loss-aware, balanced, and adaptive policies move away from it in the deterministic matrix.
- `latency_skew` shows why capacity-only scheduling is risky. Path-c has room, but its delay/jitter makes reorder pressure visible and should influence BLEST/balanced/adaptive decisions.
- `least_queue` is structurally present, but production-quality queue depth still needs live Rust send-queue counters rather than only lab/interface pressure.

## Validation

- `ruff check python tests/python`: passed.
- `pytest -q`: passed.
- `cargo fmt --check`: passed.
- `cargo test -q`: passed, including the Rust scheduler primitive tests.

## Historical limitation at the time of this report

At the time this report was created, the remaining scheduler integration gap
was the live Python loop that converts telemetry into refreshed scheduler
primitives and hot-reapplies them to Rust during the run. That later moved into
the v0.9 implementation; use this report as evidence/rationale rather than as the
current scheduler backlog.
