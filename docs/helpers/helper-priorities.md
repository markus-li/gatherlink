# Helper Priorities

## Purpose

This document records which helper areas should be developed now and which are
deferred. Helpers are Python/control-plane features unless a document explicitly
says otherwise. They must not move policy, privileged behavior, or protocol
parsing into the Rust dataplane without a later design decision.

When committing helper documentation, only stage the docs touched for that
helper decision. Other chats may be editing code or unrelated docs.

## Develop Now

Priority order:

1. time helper / system time setter
2. DNS helper
3. SOCKS5 helper
4. WireGuard helper
5. TCP forwarding helper
6. relay fabric helper
7. local status HTTP helper

### 1. Time Helper

The core Gatherlink process may derive internal time quality, peer-relative
offset, RTT, expiry windows, and diagnostics timestamps. It must not set system
time itself.

The time helper is the privileged bridge that may set system time from
Gatherlink-derived time. It is allowed only when explicitly enabled. It must
warn that system time is normally managed by an NTP agent such as chrony,
systemd-timesyncd, ntpd, or an appliance time service. Operators should not use
the Gatherlink time helper if such an agent is active.

First scope:

- explicit opt-in system time correction
- loud warning before use
- narrow privileged helper boundary
- clear diagnostics showing source quality and whether correction was applied

Not-yet scope:

- replacing NTP as the normal time authority
- automatic enablement
- packet-path time policy in Rust

### 2. DNS Helper

The DNS helper should expose a normal local resolver endpoint for tools such as
AdGuard Home, Unbound, dnsmasq, OPNsense, UniFi, and similar DNS frontends.
It is a connectivity helper, not a firewall DNS replacement and not a core
transport dependency.

First scope:

- local resolver endpoint
- cache and serve-stale behavior
- upstream policy that can use direct/tunnel/DoH choices
- diagnostics for upstream choice, cache state, and validation failures

Not-yet scope:

- becoming an enterprise DNS policy engine
- replacing existing DNS servers
- making core transport depend on DNS helper availability

### 3. SOCKS5 Helper

The SOCKS5 helper is Python-owned for the MVP. Use a maintained Python SOCKS5
server library where practical. The selected candidate is
`asyncio-socks-server`, because it is a SOCKS5 server library with asyncio
support and hook points suitable for integrating Gatherlink forwarding.

MVP protocol scope is SOCKS5 TCP CONNECT. SOCKS5 UDP ASSOCIATE is deferred.
When this document says SOCKS5 traffic travels "through UDP", it means the
helper carries proxy traffic over Gatherlink's UDP service transport to a
companion remote exit helper. It does not mean SOCKS5 UDP ASSOCIATE is part of
the first implementation.

First scope:

- local SOCKS5 server in Python
- TCP CONNECT support
- companion remote exit helper
- Gatherlink service framing between local proxy and remote exit
- explicit backpressure, connection lifetime, and diagnostics
- `gatherlink helpers stream-exit --diagnostics-jsonl ...` for durable helper
  stream diagnostics when running the companion exit in the foreground

Not-yet scope:

- SOCKS5 UDP ASSOCIATE
- Rust SOCKS5 parsing
- Rust proxy acceleration before profiling proves a bottleneck
- captive-portal-specific UX

### 4. WireGuard Helper

The WireGuard helper exists to make it easy to run VPNs using Gatherlink as the
transport/underlay. WireGuard-specific behavior, keys, interfaces, and tooling
should remain WireGuard-owned; Gatherlink should generate or coordinate the
service transport around it.

First scope:

- config generation or guidance for using WireGuard over Gatherlink services
- key/config material handling that respects WireGuard's own tooling
- diagnostics showing Gatherlink service mapping and expected UDP endpoints

Not-yet scope:

- replacing WireGuard tooling
- implementing WireGuard protocol behavior
- taking over system firewall/routing policy

### 5. TCP Forwarding Helper

The TCP forwarding helper provides simple 1:1 port forwarding over Gatherlink,
including response traffic. Example: local TCP port to a web server reachable on
the remote side through a Gatherlink service.

First scope:

- explicit local listen endpoint
- explicit remote target endpoint
- one-to-one stream forwarding
- connection and byte counters
- clear failure diagnostics
- the same companion stream exit and JSONL diagnostics path used by SOCKS5,
  so helper stream behavior is observable through one shared mechanism

Not-yet scope:

- general proxy framework
- L7 routing
- shared SOCKS5 multiplexing unless later design folds them together
- TCP semantics inside the Rust dataplane

### 6. Relay Fabric Helper

The relay fabric helper should start with discovery and health. It describes
candidate relays and their capabilities. Secure relay-session forwarding is
defined separately in `docs/protocol/relay-session-lifecycle.md`; the helper feeds
candidates and health into signed topology/control logic.

First scope:

- relay discovery from configured or signed sources
- relay health checks
- capability/region/endpoint metadata
- diagnostics for stale, degraded, incompatible, or disabled relays

Not-yet scope:

- overlay path planning
- dynamic relay-session requests outside signed topology/control context
- dynamic mesh magic
- relay as a mandatory cloud service

### 7. Local Status HTTP Helper

The status HTTP helper is a tiny local observability endpoint for labs and VM
bring-up. It reports the address it is listening on and the Gatherlink services
registered on the same machine, including `.hidden` records used for remote IPC
tests.

First scope:

- explicit local HTTP listen endpoint
- `/json` machine-readable status
- `/` and `/text` human-readable status
- service registry records including hidden records

Not-yet scope:

- remote unauthenticated exposure
- service control or mutation
- replacing `gatherlink services monitor`
- richer diagnostics sinks

### 8. Experimental Local REST Helper

The REST service should be a helper/control-plane sidecar, not core runtime or
dataplane logic. It exists to prepare future UI and local automation while the
CLI remains the primary supported control surface.

V1 scope:

- explicit CLI startup
- bind to `127.0.0.1` by default
- non-loopback bind only with a loud danger flag
- read APIs for service list/status/monitor-style structured facts
- write APIs for selected CLI-equivalent operations
- write APIs expire after one hour by default unless the helper is restarted
  from CLI
- responses must not expose secret key material
- all docs and startup output must mark it `EXPERIMENTAL`

Not-yet scope:

- remote unauthenticated management
- replacing the CLI
- stable public API guarantees
- browser UI as part of v1
- long-lived write access

## Deferred Helpers

The following helpers are deferred. Do not implement real behavior now. It is
acceptable to collect notes, preserve tiny stubs, or define narrow interfaces
when an active helper needs a placeholder, but those stubs must not grow runtime
behavior.

- captive portal helper
- IPsec NAT-T helper
- access policy helper
- policy advisor helper
- overlay routing helper
- overlay naming helper
- synthetic diagnostics helper
- identity/topology helper or full control-plane
- secrets / age helper area

Deferred means:

- no production behavior
- no broad API surface
- no privileged actions
- no Rust dataplane dependencies
- no hidden coupling to active helpers

## Cross-Cutting Rules

- Helpers fail independently; core transport keeps running.
- Helpers emit diagnostics rather than silently changing policy.
- Helpers should be Python-owned unless there is a measured packet-rate reason
  to move a tiny execution primitive into Rust.
- Privileged helpers must be narrow, explicit, and opt-in.
- Active helper docs should define first scope and not-yet scope before code is
  expanded.

## Library Selection

Project-wide dependency rules live in `docs/operations/library-selection.md`.

Active helper docs may record helper-specific dependency decisions, but those
decisions must follow the project-wide library selection policy. Deferred
helpers must not introduce new dependencies except tiny interfaces needed by
active helpers.
