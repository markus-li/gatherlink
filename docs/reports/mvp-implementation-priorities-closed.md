# MVP Implementation Priorities, Closed

This was the "next implementation priorities" handoff used during the MVP build.
It is now closed historical context, not a current priority list. Current work
belongs in `docs/reports/v0.9.1-roadmap.md` or
`docs/reports/future-roadmap-pipeline.md`.

## Priority 1: Hot scheduler reapply loop

At the time of this handoff, Python already expanded config, compiled scheduler
policy into runtime models, and started Rust-backed lab services over real path
transport sockets. The then-current production gap was the continuous loop that
converts live telemetry into updated scheduler/runtime primitives and
hot-reapplies them to Rust.

Boundary check:

- Python owns config, policy, defaults, path naming, and explanations.
- Rust receives only service DTOs, path DTOs, scheduler mode, service fanout
  primitives, and primitive per-path facts.
- Lab code should start production-shaped services and should not own scheduler policy.

Boundary check:

- Python owns smoothing, thresholds, path state, capacity detection, latency
  interpretation, and operator-facing reasons.
- Rust owns only cheap path selection, frame transport, and counters.

## Priority 2: Production diagnostics event bus

At the time of this handoff, service monitor was useful but diagnostics docs
still called out the event bus as a release gap. The proposed next layer was to
normalize Rust counter snapshots and service lifecycle events into Python
diagnostics events.

Boundary check:

- Rust emits structured counters/events without blocking.
- Python owns terminal formatting, log wording, sinks, and service registry IPC.

## Priority 5: IPv6 parity in runnable labs

Runtime planning and Rust socket tests already cover IPv6. The lab docs require
IPv6 from day one, so this handoff called for a runnable IPv6 mirror of the
local path lab after the Rust-backed runner existed.

Boundary check:

- Lab owns namespace/veth/IP setup only.
- Core runtime and dataplane must remain address-family neutral.

## Priority 6: Capability negotiation and authenticated control

Capability negotiation, crypto, and authenticated control are intentionally
future-looking in this closed handoff, but the protocol docs define the
direction.

Boundary check:

- Public UDP remains silent on invalid or unauthenticated packets.
- Rust validates envelopes and replay windows.
- Python owns negotiated policy, diagnostics, and downgrade explanations.
