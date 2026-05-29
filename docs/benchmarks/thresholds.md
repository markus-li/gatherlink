# Benchmark Thresholds

This file tracks the current benchmark gates used when deciding whether a lab
run is acceptable or whether a tuning pass still has work to do. These numbers
are not protocol requirements; they are release and lab acceptance targets.

## Terms

- `pass_threshold`: the minimum result that lets a scenario count as passing.
  Falling below this means the run should fail the benchmark gate or require an
  explicit explanation.
- `performance_target`: the expected or desired result. Reaching this means the
  scenario is performing at the level we currently want, not merely scraping by.
- `baseline`: the comparison used for the ratio. For transport performance, the
  default baseline is userspace WireGuard measured in the same lab shape. For
  multipath rows this means equivalent WireGuard tunnels running concurrently
  across the same active path set, with the result summed for the path-set
  baseline.

The previous working name for `performance_target` was "reached expected
threshold". Use `performance_target` in docs, reports, and tooling because it is
shorter and clearer.

## Current Global Targets

| scope | baseline | pass_threshold | performance_target | notes |
| --- | --- | ---: | ---: | --- |
| Gatherlink raw UDP, one-hop | userspace WireGuard, concurrent same active paths and MTU | 80% | 90% | Raw Gatherlink should not be much slower than a comparable userspace encrypted UDP tunnel. |
| WireGuard over Gatherlink, one-hop | userspace WireGuard direct, concurrent same active paths and MTU | 75% | 90% | Includes nested WireGuard behavior, so the pass gate is lower until ordered single-flow multipath is fully proven. |
| Gatherlink raw UDP, untrusted relay | userspace WireGuard routed through equivalent hop shape | 70% | 90% | Relay adds hop unwrap/forward cost; target remains high because relay forwarding should stay cheap. |
| WireGuard over Gatherlink relay | userspace WireGuard routed through equivalent hop shape | 65% | 90% | Product-relevant but hardest current shape; use this to expose relay and reorder bottlenecks. |

## Three-Path WAN Profile Gates

These apply to `configs/lab/local-three-path.json` when the matching network
mode is active and the benchmark pressure is just above expected aggregate
capacity.

| profile | path MTU | payload | expected capacity | pressure | pass_threshold | performance_target | drop expectation |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `acceptance-300-500-700` | 1200 | 1200 | 1500 Mbit/s | 1550 Mbit/s | 80% delivered | 90% delivered | Clean synthetic capacity probe; sustained drops are expected once the UDP generator stays above true path capacity, but path split should stay close to 300/500/700. |
| `acceptance-uneven-high` | 1452 | 1438 | 2800 Mbit/s | 2900 Mbit/s | 70% delivered | 90% delivered | Clean synthetic high-pressure profile; use IPv6-safe max-normal frames to avoid measuring avoidable packet-rate overhead. |
| `realworld-fiber-plus-5g` | 1200 | 1200 | 930 Mbit/s | 950 Mbit/s | 75% delivered | 90% delivered | Useful delivery in this jitter/loss facsimile flattens around 930 Mbit/s; higher pressure is overload evidence. |
| `realworld-starlink-plus-5g` | 1200 | 1200 | 245 Mbit/s | 250 Mbit/s | 75% delivered | 90% delivered | Useful delivery in this loss/jitter facsimile tops out around 243 Mbit/s; higher pressure validates overload behavior, not scheduler quality. |
| `realworld-starlink-plus-2x5g` | 1200 | 1200 | 340 Mbit/s | 350 Mbit/s | 75% delivered | 90% delivered | All paths may contribute, but slower paths must not be overfilled. |

`path MTU` and `payload` describe the benchmark's Gatherlink frame and service
payload sizes. A profile only changes the Linux interface MTU when
`thresholds.json` also sets `shape_mtu`; lossy real-world facsimiles deliberately
avoid lowering the veth MTU because that changes the network-loss model rather
than just the Gatherlink packet size.

## Required Report Fields

Every benchmark report that claims pass/fail should record:

- scenario/profile name
- baseline name and measured baseline speed
- a separate equivalent WireGuard kernel baseline table
- a separate equivalent WireGuard userspace baseline table
- path MTU and generated payload size
- offered pressure
- delivered throughput
- delivery percentage
- ratio to baseline
- whether `pass_threshold` was met
- whether `performance_target` was met
- per-path delivered throughput
- per-path drops/missed packets
- scheduler mode and path capacity hints used at startup
- whether the run was cold-cache or warm-cache

Current performance ledgers, including
[`hyperv-performance-log.md`](hyperv-performance-log.md), must keep these gate
fields visible whenever a row is used as pass/fail, target, guardrail,
regression, or release evidence. Do not collapse gate status into prose-only
readings. If a table uses more than one baseline, keep separate compact gate
columns rather than merging the meanings.

## Current Notes

- The project-wide performance target is currently **90% of userspace
  WireGuard** in an equivalent topology.
- Kernel WireGuard remains useful as a host/network ceiling, but userspace
  WireGuard is the fairer performance target for Gatherlink's userland Rust
  dataplane.
- Gatherlink benchmark tables should not stand alone. Each result set needs
  equivalent WireGuard kernel and userspace baseline tables beside it, using
  single-path tests for single-path Gatherlink runs and simultaneous per-path
  tests for multipath Gatherlink runs.
- The three-path WAN profiles should run in both cold-cache and warm-cache
  modes. Cold-cache proves starting hints are reasonable; warm-cache proves
  auto-detected capacity improves subsequent runs.
- The synthetic non-real-world three-path profiles should run twice: once with
  the normal 1200 byte local-lab MTU/payload shape and once with jumbo path MTU
  9000 plus 8192 byte generated payloads. Real-world facsimiles stay at normal
  MTU by default.
- Three-path WAN profile `delivery` is delivered sink throughput divided by
  offered pressure. It is not a userspace-WireGuard-relative number; only
  transport comparison tables use userspace WireGuard as the default baseline.
- Forced-loss profiles should use separate thresholds. They are for counter and
  fail-closed validation, not throughput acceptance.
