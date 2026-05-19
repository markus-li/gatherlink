# Scheduler

Gatherlink scheduling is split across Python and Rust.

Python owns scheduler policy, scoring, path state interpretation, weights,
operator-facing explanations, and future adaptive behavior. Rust owns only the
compiled hot-path scheduler state it needs to choose a path cheaply while
encoding packets.

## Runtime Modes

The default scheduler mode is still `round_robin`, but the runtime contract now
has named modes that let Python choose a policy and keep Rust on primitive
execution:

- `round_robin`: visit compiled paths in order, applying weights and MTU/state eligibility
- `weighted_round_robin`: explicit alias for the weighted path sequence Rust already executes
- `srtt`: Python policy alias for lowest-smoothed-latency selection, inspired by MPTCP's default RTT-first behavior
- `lowest_latency`: pick the eligible path with the lowest compiled latency estimate
- `loss_aware`: pick the eligible path with the lowest compiled loss estimate
- `capacity_aware`: pick the eligible path with the highest compiled TX capacity estimate
- `least_queue`: reserved for queue-depth-driven selection as soon as live queue counters are wired into the send path
- `earliest_completion_first`: MPTCP ECF-inspired policy using latency plus estimated transmit time
- `blocking_estimation`: BLEST-inspired policy that avoids paths likely to create reorder blocking
- `balanced`: hybrid policy that combines capacity, latency, loss, and queue facts
- `adaptive`: Python scoring compiles live telemetry into weights, states, and primitive limits; Rust executes it like weighted round-robin

`round_robin` remains intentionally simple:

- paths are visited in the order Python compiled into runtime config
- each datagram advances the next-path cursor
- Rust still applies MTU eligibility before emitting a frame
- path `enabled`, `state`, `weight`, MTU, and primitive limits are compiled by Python
- `active` MTU-eligible paths are preferred for whole packets
- if no path can carry the datagram whole, Rust may use a non-busy path for
  fragmentation
- `busy`, `drain`, and `disabled` are inputs from Python, not Rust policy
- `weight` repeats a path in the simple round-robin sequence

This gives the dataplane real scheduling modes without moving adaptive policy
into Rust.

Service redundancy is a Python-owned policy that compiles to a tiny Rust fanout
primitive. Rust receives `fanout` and `fanout_below_bytes`: `fanout=1` means one
scheduled path, `fanout=0` means every eligible path, and values above one mean
that many eligible paths. `fanout_below_bytes=0` makes fanout apply to every
payload; otherwise larger payloads fall back to one scheduled path. Python can
therefore expose modes like `duplicate`, `duplicate_small`, or control metadata
all-path delivery without Rust learning those policy names.

## Primitive Contract

Python may use user config, lab qdisc counters, peer control metadata, service
priority, path history, and future helper facts to decide what each path should
do. Rust receives only cheap execution primitives:

- `tx_capacity_bps` and `rx_capacity_bps` for directional bandwidth estimates
- `latency_us` for the compact latency value used by packet-time selection
- `loss_ppm` for smoothed loss in parts per million
- `reorder_hold_us` for path-specific reorder timing selected by Python
- `max_in_flight_packets` and `max_in_flight_bytes` for bounded pressure limits
- `enabled`, `state`, `weight`, and `mtu` for direct path eligibility

The important boundary is that Python explains and changes these values. Rust
only follows them and reports counters back.

## Service Priority

Service priority belongs to configured Gatherlink services. It must not be
derived from packet inspection.

The current config accepts priority labels (`bulk`, `normal`, `high`,
`critical`) and Python compiles them into stable numeric runtime values. Rust
preserves those values at the service boundary, but it does not yet use them for
multi-service fairness because the first lab carries one virtual UDP service.

When multiple services share constrained paths, Python should compile the chosen
fairness policy into explicit runtime state before Rust executes it.

## Future Work

Service-priority-aware fairness and receiver-metric-driven adaptation should be
compiled by Python into explicit runtime state before Rust executes them.

Rust should not parse user config, discover links, score carriers, explain path
choices, or own failover policy. It should execute the scheduler state, count
what happened, and report enough structured diagnostics for Python to explain
the behavior.

## Queues and UDP

Gatherlink should keep a small bounded local scheduler queue. The queue exists
to smooth short scheduler decisions and preserve FIFO ordering while the runtime
chooses among currently eligible paths. It is not a reliability mechanism for
UDP.

Normal UDP payloads remain best-effort:

- if a path is temporarily busy and another eligible path has capacity, the
  queued packet may be redistributed FIFO-style to that path
- if the queue is full or the packet age exceeds policy, Gatherlink drops the
  packet, increments explicit Gatherlink drop counters, and emits diagnostics
- Gatherlink does not retransmit ordinary UDP packets unless a future service
  mode explicitly requests reliability

This keeps real-time UDP honest. Loss recovery belongs to the application or
upper protocol that chose UDP. Gatherlink's job is to make drops, queue depth,
queue age, path latency, and receiver missing-packet facts visible enough for
Python policy to adjust path weights or disable bad paths.
