# Scheduler

Gatherlink scheduling is split across Python and Rust.

Python owns scheduler policy, scoring, path state interpretation, weights,
operator-facing explanations, and future adaptive behavior. Rust owns only the
compiled hot-path scheduler state it needs to choose a path cheaply while
encoding packets.

## MVP Mode

The MVP scheduler mode is `round_robin`.

`round_robin` is intentionally simple:

- paths are visited in the order Python compiled into runtime config
- each datagram advances the next-path cursor
- Rust still applies MTU eligibility before emitting a frame
- path `enabled`, `state`, `weight`, and MTU are compiled by Python
- `active` MTU-eligible paths are preferred for whole packets
- if no path can carry the datagram whole, Rust may use a non-busy path for
  fragmentation
- `busy`, `drain`, and `disabled` are inputs from Python, not Rust policy
- `weight` repeats a path in the simple round-robin sequence

This gives the dataplane a real scheduling insertion point without moving
adaptive policy into Rust too early.

## Service Priority

Service priority belongs to configured Gatherlink services. It must not be
derived from packet inspection.

The current config accepts priority labels (`bulk`, `normal`, `high`,
`critical`) and Python compiles them into stable numeric runtime values. Rust
preserves those values at the service boundary, but it does not yet use them for
multi-service fairness because the first lab carries one virtual UDP service.

When multiple services share constrained paths, Python should compile the chosen
fairness policy into explicit runtime state before Rust executes it.

## Future Modes

Future scheduler modes may include fixed weights, weighted round-robin, least
queue, loss-aware, RTT-aware, service-priority aware, and receiver-metric driven
adaptive scheduling. Those modes should be compiled by Python into explicit
runtime state before Rust executes them.

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
