# Path Lifecycle

Paths are configured UDP carrier sockets. They are not services and they do not
make endpoint policy decisions.

## States

The current lifecycle vocabulary is deliberately small:

- configured: the path exists in config and runtime state
- bound: Rust has opened the local UDP carrier socket
- active: packets or control metadata have recently moved on the path
- degraded: telemetry shows loss, lower capacity, high latency, or failed sends
- disabled: Python policy has removed the path from scheduler eligibility
- stopped: the owning service has closed

Rust exposes counters and cheap facts. Python interprets those facts and decides
whether scheduler primitives should change.

## Directional Facts

Capacity, latency, and MTU are direction-specific. A path can be fast in one
direction and slow in the other. Control metadata should therefore keep transmit
and receive facts separate wherever possible:

- tx/rx bytes and packets
- tx/rx sampled speed
- tx/rx detected capacity
- tx/rx latency samples
- tx/rx MTU and payload budget

## Failure And Recovery

UDP loss is expected. Gatherlink should observe it, expose it, and adapt path
selection, but it should not pretend to provide reliable delivery for arbitrary
UDP payloads. Reliability belongs to the application protocol above Gatherlink
unless a future helper explicitly owns a reliable stream abstraction.

For local labs:

- `tools/wsl_shape_private_lan.sh` can rate-limit or drop path traffic
- `tools/run_wsl_mvp_acceptance.ps1` drops one WSL carrier path and verifies
  traffic still arrives over the remaining paths
- normal Gatherlink services remain unprivileged; only lab setup/shaping uses
  elevated network privileges

## Ownership

Python owns path interpretation, scheduling policy, and operator explanation.
Rust owns socket execution, packet counters, send failures, dedupe, replay, and
bounded queues.
