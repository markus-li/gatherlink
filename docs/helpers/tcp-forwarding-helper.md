# TCP Forwarding Helper

## Purpose

The TCP forwarding helper provides simple one-to-one TCP port forwarding over
Gatherlink. It is an active helper priority and remains Python/control-plane
owned for the MVP.

Example:

```text
local 127.0.0.1:8080
  -> Gatherlink TCP forwarding helper
  -> Gatherlink UDP service transport
  -> remote TCP forwarding exit helper
  -> 10.0.0.20:80
```

## First Scope

- explicit local listen endpoint
- explicit remote target endpoint
- one-to-one bidirectional byte forwarding
- response traffic for established connections
- connection open/close/error events
- byte counters
- idle timeout and connect timeout
- clear diagnostics for refused connections, unreachable exits, and policy
  denial

Implemented first slice:

- `gatherlink helpers tcp-forward --listen 127.0.0.1:8080 --target 127.0.0.1:80`
  starts a foreground one-to-one TCP forwarder
- the helper uses Python `asyncio` streams only; no extra TCP proxy dependency
  is introduced
- connection, failure, close, and byte counters are tracked in the helper
- production forwarding requires an explicit Gatherlink service stream
  transport; `--lab-direct` is the only direct TCP bypass and is for local
  smoke tests
- `--gatherlink-service HOST:PORT` frames the TCP byte stream into a configured
  local Gatherlink UDP service endpoint
- `gatherlink helpers stream-exit --listen HOST:PORT --allow-host TARGET --allow-port PORT`
  runs the companion UDP exit that opens the explicit remote TCP target and
  returns response bytes through Gatherlink
- the remote Gatherlink exit helper and stream framing use the shared helper
  transport lifecycle rather than moving TCP behavior into Rust

## Transport Model

The helper carries TCP byte streams over Gatherlink's UDP service model. It does
not add TCP semantics to the Rust dataplane. Python helper code owns stream
framing, connection lifetime, backpressure, and exit behavior.

The first implementation should be deliberately narrow: one local listen maps to
one configured remote target through one remote exit helper.

Use Python `asyncio` streams first. Do not add a TCP proxy dependency unless the
standard library cannot provide the needed connection lifecycle, backpressure,
and diagnostics hooks.

## Not-Yet Scope

- general proxy framework
- L7 routing
- TLS termination
- shared SOCKS5 multiplexing unless a later design folds them together
- TCP semantics or stream proxying inside Rust
- transparent proxying
- dynamic target selection from packet contents

## Relationship To SOCKS5 Helper

TCP forwarding and SOCKS5 are separate helpers in the first implementation.
They may share lower-level stream framing later, but TCP forwarding should stay
simple and explicit: one local port, one remote target, bidirectional responses.
