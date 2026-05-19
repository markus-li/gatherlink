# Diagnostics Events

## Purpose

Gatherlink should be easy to follow in a terminal while keeping the fast packet
path in Rust.

Rust emits structured state, counter, and event information. Python owns display,
logs, sinks, formatting, filtering, and operator-facing explanations.

## Boundary

Rust may emit:

- service bound
- service removed
- runtime config reapplied
- path registered
- datagram received
- frame encoded or decoded
- payload emitted
- packet or byte counters
- protocol/auth/drop counters
- socket or path errors

Python decides:

- how events appear in terminal output
- which events are written to logs
- which service record owns each log stream
- how warnings are worded
- which sinks receive diagnostics
- how events map back to config, paths, helpers, or operator action

Diagnostics must never block dataplane loops. If an event queue is full, Rust
should increment a dropped-diagnostics counter and keep moving packets.

## Initial Terminal View

The first local lab should expose enough information to understand behavior
without a debugger:

```text
client core: security.mode=none WARNING unauthenticated unencrypted traffic
client core: bound service udp-main listen=127.0.0.1:55180 target=...
client path path-a: registered local=10.80.1.1 remote=10.80.1.2
client path path-b: registered local=10.80.2.1 remote=10.80.2.2
client core: forwarded service=udp-main path=path-a packets=100 bytes=120000
```

The exact format can evolve, but the source of truth should be structured
events or counters, not hand-parsed prose.

## Service Logs

Any process started by the Python control plane should register a service folder
under `.gatherlink/services/` with a stable auto-generated name such as
`lab.local-dual-path` or `core.client-a`. Discovery should scan this directory,
not rewrite one shared registry file. Each service folder should keep:

- service name and kind
- process manager (`process` or `systemd`)
- `service.json` for mostly static identity and config metadata
- `current.pid` for the active process-managed PID
- `control.sock` for process-managed service IPC
- systemd unit name for systemd-managed services
- log source
- config path or other metadata needed to identify the service

Live state beyond the current PID should come through service-owned IPC rather
than by repeatedly rewriting the registry metadata. When a process-managed
service has a stale or dead PID, registry discovery should remove `current.pid`
and mark the service metadata as stale-cleaned instead of showing it as alive.

Operators should be able to use the same commands for labs, core services, and
helpers:

```bash
gatherlink services list
gatherlink services status lab.local-dual-path
gatherlink services logs lab.local-dual-path --follow
gatherlink services attach lab.local-dual-path
gatherlink services close lab.local-dual-path
```

For process-managed services, `status` and graceful `close` use JSON messages
over the service's `control.sock`. If IPC is unavailable during close, the
control plane may fall back to signalling the recorded PID and then clear the
PID slot.

Systemd-managed records are visible in the same list, but direct lifecycle
control stays with systemd. They should be registered from the same config used
by a non-systemd service, with only `--systemd` changing the manager mode:

```bash
gatherlink services register configs/examples/minimal-client.json --systemd
```

`attach` should stream `journalctl` in the foreground instead of spawning
another detached process.

## First Event Set

The first implementation should start with a small model:

- `warning`
- `service_bound`
- `config_reapplied`
- `packet_forwarded`
- `counter_snapshot`
- `shutdown`

This is enough for the local lab and leaves room for richer path metrics later.
