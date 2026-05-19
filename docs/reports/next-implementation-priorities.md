# Next implementation priorities

This list is based on the current architecture, scheduler, protocol, diagnostics,
time-sync, and lab documentation. The order favors proving the core production
boundary before adding more policy.

## Priority 1: Hot scheduler reapply loop

Python already expands config, compiles scheduler policy into runtime models,
and starts the Rust-backed lab services over real path transport sockets. The
next production gap is the continuous loop that converts live telemetry into
updated scheduler/runtime primitives and hot-reapplies them to Rust.

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

Service monitor is useful now, but diagnostics docs still call out the event bus
as a release gap. The next layer should normalize Rust counter snapshots and
service lifecycle events into Python diagnostics events.

Boundary check:

- Rust emits structured counters/events without blocking.
- Python owns terminal formatting, log wording, sinks, and service registry IPC.

## Priority 5: IPv6 parity in runnable labs

Runtime planning and Rust socket tests already cover IPv6. The lab docs require
IPv6 from day one, so a runnable IPv6 mirror of the local path lab should be
added after the Rust-backed runner exists.

Boundary check:

- Lab owns namespace/veth/IP setup only.
- Core runtime and dataplane must remain address-family neutral.

## Priority 6: Capability negotiation and authenticated control

Capability negotiation, crypto, and authenticated control are intentionally
future work, but the protocol docs define the direction. Add this after the
plaintext Rust-backed lab is trustworthy.

Boundary check:

- Public UDP remains silent on invalid or unauthenticated packets.
- Rust validates envelopes and replay windows.
- Python owns negotiated policy, diagnostics, and downgrade explanations.
