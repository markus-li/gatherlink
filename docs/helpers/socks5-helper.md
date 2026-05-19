# SOCKS5 Helper

## Purpose

The SOCKS5 helper provides application proxy access through Gatherlink without
turning the Rust dataplane into a proxy server. It is an optional Python helper
with a companion remote exit helper.

The MVP supports SOCKS5 TCP CONNECT. SOCKS5 UDP ASSOCIATE is deferred.

## Ownership

The SOCKS5 helper is Python-owned for the MVP.

Use a maintained Python SOCKS5 server library where practical. The selected
candidate is `asyncio-socks-server`, because it provides SOCKS5 server handling
with asyncio support and hook points that can be connected to Gatherlink helper
logic.

Gatherlink-specific code owns:

- policy and configuration
- mapping SOCKS requests to Gatherlink services/sessions
- framing traffic over Gatherlink's UDP service model
- remote exit companion behavior
- backpressure and connection lifetime
- diagnostics

Gatherlink should not implement SOCKS5 parsing in Rust for the MVP. Rust
acceleration is deferred until profiling shows Python proxy I/O or SOCKS
parsing is the bottleneck.

## Transport Model

SOCKS5 TCP CONNECT is accepted locally, then carried over Gatherlink to a remote
exit helper:

```text
local application
  -> local SOCKS5 helper
  -> Gatherlink UDP service transport
  -> remote SOCKS5 exit helper
  -> outbound TCP connection
```

When this helper says traffic moves over UDP, it means the proxy stream is
framed over Gatherlink's UDP service transport. It does not mean SOCKS5 UDP
ASSOCIATE is part of the MVP.

## First Scope

- local SOCKS5 server
- TCP CONNECT
- remote exit companion
- explicit service/session mapping
- connection open/close/error events
- byte counters
- backpressure behavior
- clear diagnostics for refused targets, unreachable exits, and policy denial
- optional JSONL diagnostics sink for policy decisions and stream lifecycle

Implemented first slice:

- `gatherlink helpers socks5-serve --listen 127.0.0.1:1080 --allow-host example.test --allow-port 443`
  starts a local SOCKS5 helper using `asyncio-socks-server`
- the library owns SOCKS5 protocol parsing; Gatherlink code owns policy,
  connection decisions, counters, and the exit connector abstraction
- production exits require an explicit Gatherlink service stream transport;
  `--lab-direct` is the only direct TCP bypass and is for local smoke tests
- `--gatherlink-service HOST:PORT` frames allowed CONNECT byte streams into a
  configured local Gatherlink UDP service endpoint
- `gatherlink helpers stream-exit --listen HOST:PORT --allow-host TARGET --allow-port PORT`
  runs the companion UDP exit that receives those helper stream frames from the
  peer's Gatherlink service target
- canonical config can declare `helpers.socks5` with the local SOCKS5 listen
  endpoint, the Gatherlink service name, and explicit `allow_hosts` /
  `allow_ports`; runtime expansion resolves that service to the local
  Gatherlink UDP stream endpoint for supervisors
- empty allow-lists deny all traffic so the helper cannot silently become an
  open proxy
- policy denials, unreachable exits, stream opens, and stream closes are emitted
  as structured helper diagnostics when `--diagnostics-jsonl` is provided
- `LabDirectTcpExitConnector` is available for local smoke tests; the connector
  interface remains the boundary between SOCKS5 policy and Gatherlink stream
  transport

## Deferred

- SOCKS5 UDP ASSOCIATE
- SOCKS4 compatibility
- authentication modes beyond what the chosen library provides easily
- Rust SOCKS parsing
- captive portal UX
- browser session management
- general L7 routing

## Exit Companion

The remote exit helper is responsible for opening the outbound connection and
returning response bytes through Gatherlink. It should be explicit about:

- allowed target hosts or networks
- allowed target ports
- DNS resolution location
- connection timeout
- idle timeout
- max concurrent connections
- max bytes or rate limits when configured

The exit helper must not silently become a general open proxy. Defaults should
be conservative and diagnostics should identify policy decisions.

## Library Decision

Use `asyncio-socks-server` as the first candidate dependency for server-side
SOCKS5 protocol handling. Re-evaluate only if its hook model blocks Gatherlink's
service/exit architecture.

Keep `python-socks` as a possible future dependency for exit-side upstream proxy
chaining. Do not use `siosocks` as the default unless its maintenance status
improves or its Sans-IO design becomes necessary.
