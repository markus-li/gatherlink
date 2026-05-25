# Diagnostics Events

## Purpose

Gatherlink should be easy to follow in a terminal while keeping the fast packet
path in Rust.

Rust emits structured state, counter, and event information. Python owns display,
logs, sinks, formatting, filtering, and operator-facing explanations.

JSONL is the first durable event sink. Prometheus, WebSocket, MQTT, and webhooks
may be added later, but they should consume the same structured events.

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

Silent network drops still increment local counters. Invalid crypto, relay, and
replay inputs must not trigger network responses, but operators should be able
to see local rate-limited counters and event samples.

For transport security, Rust reports aggregate silent-drop counters only. It
does not reveal whether a packet failed because it was malformed, used an
unknown receiver index, failed authentication, or hit replay protection. Python
turns counter increments into structured `crypto.auth_failed` diagnostics with a
`drop_family` of `transport_security`; finer labels may be added only when they
do not change fail-closed wire behavior.

Foreground service startup failures should emit structured diagnostics when a
sink is configured. Terminal text remains concise, while JSONL keeps facts such
as config path, error type, and normalized validation details for scripts and
later operator tooling.

Process-managed helper launches should emit helper lifecycle diagnostics into
the helper service diagnostics JSONL file. The runtime supervisor owns only the
process fact (`started` or `start_failed`); helper behavior such as stream
allow/deny, target reachability, and close counters stays inside the helper.

## Stable Event Codes

Events should have stable machine-readable codes from the first implementation.
Suggested initial codes include:

- `crypto.replay_drop`
- `crypto.auth_failed`
- `crypto.unknown_receiver_index`
- `relay.auth_failed`
- `relay.replay_drop`
- `relay.unknown_receiver_index`
- `relay.unauthorized_next_hop`
- `relay.expired_session`
- `relay.generation_stale`
- `relay.limit_exceeded`
- `relay.packet_too_large`
- `runtime.start_failed`
- `dns.dnssec_bogus`
- `dns.policy_denied`
- `dns.upstream_failed`
- `helper.time.set_failed`
- `helper.wireguard.plan`
- `helper.lifecycle.started`
- `helper.lifecycle.start_failed`
- `helper.stream.opened`
- `helper.stream.closed`
- `helper.stream.denied`
- `helper.stream.unreachable`
- `helper.stream.invalid_frame`
- `socks.exit_denied`
- `socks.exit_unreachable`
- `helper.status_http.started`
- `helper.status_http.non_loopback_bind`
- `helper.status_http.service_closed`
- `helper.status_http.write_denied`
- `helper.status_http.write_failed`
- `rekey.started`
- `rekey.succeeded`
- `rekey.rejected`
- `rekey.expired`

Helper warnings use the same event bus as dataplane warnings. This keeps
operator views, JSONL logs, and future metrics consistent.

Helper stream events are lifecycle and policy facts, not per-packet traces. A
stream companion exit should emit open, close, deny, unreachable, and malformed
frame events so operators can understand helper behavior without flooding the
diagnostics bus during normal payload forwarding.

Operator-facing "why" text should be generated from structured facts, not
handwritten one-off log strings. For example, a denied SOCKS request should
carry the helper, service, peer, policy rule, and decision reason as fields, and
the terminal view may render those fields as a sentence.

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
control plane falls back to signalling the recorded PID. A process-managed
service is not marked stopped until the PID exits; the fallback escalates from
TERM to KILL for detached child processes before clearing the PID slot.

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
- `drop`

This is enough for the local lab and leaves room for richer path metrics later.
Drop events are local diagnostics for silent network drops such as failed AEAD,
replay rejection, unknown receiver index, or relay authorization failure. They
must not imply that a packet-level error response was sent to the peer.


Carrier events include `carrier.ready`, `carrier.closed`, `carrier.connect_failed`, `carrier.datagram_sent`, `carrier.datagram_received`, and `carrier.datagram_dropped`. They are best-effort diagnostics and must not block carrier packet movement.
