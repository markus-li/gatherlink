# TCP Forwarding And TCP Proxy Helper

## Purpose

The TCP forwarding helper provides simple one-to-one TCP port forwarding over
Gatherlink. The same helper family is also the future TCP-aware proxy shape for
TCP streams where Gatherlink should stay "in the know" at stream level without
moving TCP semantics into Rust. It is an active helper priority and remains
Python/control-plane owned.

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
- optional JSONL diagnostics sink for stream lifecycle and failure events

When promoted from the future pipeline, TCP-aware proxy scope must
include both explicit and transparent modes from the beginning:

- explicit mode: applications connect to a configured local listen endpoint,
  and the helper forwards to a configured target or named remote exit service
- transparent mode: an opt-in Debian firewall/policy-routing helper uses
  TPROXY-style interception to deliver selected TCP flows to the local helper
  while preserving the original destination
- the stream helper owns TCP connection lifetime, byte ordering, stream ids,
  backpressure, credits, open/close/reset, and per-stream diagnostics
- the traffic-split/firewall helper owns nftables/TPROXY rules, marks, policy
  routing, original-destination recovery, labels, cleanup, and privilege
  checks
- Gatherlink core still carries framed encrypted service payloads and receives
  only scheduler metadata such as stream id, traffic class, pressure, and path
  preference

Initial promoted tests may prove explicit mode first, but transparent mode is
part of the helper contract and must be designed so it can work rather than
bolted on as a separate product later.

## Future Hybrid TCP Proxy Plus WireGuard Profile

A useful future deployment profile is:

```text
TCP traffic
  -> transparent TCP proxy helper
  -> TCP-aware Gatherlink stream service

non-TCP traffic
  -> WireGuard
  -> WireGuard-over-Gatherlink service
```

This lets Gatherlink handle TCP with stream-level knowledge while leaving
non-TCP IP traffic, VPN behavior, keys, interfaces, and routes with WireGuard.
It avoids trying to make a single opaque WireGuard UDP flow behave like MPTCP
for high-BDP TCP traffic.

Boundary:

- the TCP proxy helper owns TCP stream framing and per-stream scheduler hints
- the WireGuard helper owns the non-TCP tunnel transport shape around
  WireGuard's own tooling
- the traffic-split/firewall helper owns the local Debian rules that decide
  which traffic enters the transparent TCP proxy and which traffic stays in
  WireGuard
- Gatherlink core still carries encrypted service payloads and does not inspect
  application protocols, raw TCP sequence numbers, or WireGuard payloads

When promoted, this should be tested first without transparent interception by
using explicit TCP proxy listeners and ordinary WireGuard-over-Gatherlink for
the remaining traffic.
Transparent interception can then be proven separately with the same TCP stream
service.

Implemented first slice:

- `gatherlink helpers tcp-forward --listen 127.0.0.1:8080 --target 127.0.0.1:80`
  starts a foreground one-to-one TCP forwarder
- the helper uses Python `asyncio` streams only; no extra TCP proxy dependency
  is introduced
- connection, failure, close, and byte counters are tracked in the helper
- lifecycle and failure events are emitted as structured helper diagnostics
  when `--diagnostics-jsonl` is provided
- production forwarding requires an explicit Gatherlink service stream
  transport; `--lab-direct` is the only direct TCP bypass and is for local
  smoke tests
- `--gatherlink-service HOST:PORT` frames the TCP byte stream into a configured
  local Gatherlink UDP service endpoint
- production `tcp-forward` refuses to start without `--gatherlink-service`
  unless `--lab-direct` is explicitly selected, so missing tunnel wiring is a
  startup configuration error
- `gatherlink helpers stream-exit --listen HOST:PORT --allow-host TARGET --allow-port PORT`
  runs the companion UDP exit that opens the explicit remote TCP target and
  returns response bytes through Gatherlink
- canonical config can declare `helpers.tcp_forward` with the local listen
  endpoint, the one-to-one remote target, and the Gatherlink service name;
  runtime expansion resolves that service to the local Gatherlink UDP stream
  endpoint for supervisors
- the remote Gatherlink exit helper and stream framing use the shared helper
  transport lifecycle rather than moving TCP behavior into Rust
- `tools/hyperv/run_socks5_vm_acceptance.sh` also starts `tcp-forward` and
  proves a VM A HTTP client can reach the VM B status HTTP helper through the
  Gatherlink tunnel and companion stream exit
- acceptance and production configs should allocate TCP forwarding its own
  helper-facing Gatherlink UDP service port. Sharing one
  `learned-single-source` service with another helper makes reply ownership
  ambiguous and is not a supported operational shape.

## Transport Model

The helper carries TCP byte streams over Gatherlink's UDP service model. It does
not add TCP semantics to the Rust dataplane. Python helper code owns stream
framing, connection lifetime, backpressure, and exit behavior.

The first implementation should be deliberately narrow: one local listen maps to
one configured remote target through one remote exit helper.

Use Python `asyncio` streams first. Do not add a TCP proxy dependency unless the
standard library cannot provide the needed connection lifecycle, backpressure,
and diagnostics hooks.

For TCP-aware proxying, stream frames should remain inside the encrypted
Gatherlink service payload. A compact frame shape should be enough:

```text
stream_id
frame_type: open | data | close | reset | credit | keepalive
flags: interactive | bulk | prefer_stable | allow_migrate
chunk_id
payload
```

Scheduler metadata exposed by the helper should be stream-level, not raw TCP
sequence-level:

```text
service_id
stream_id
traffic_class
flowlet_key = stream_id
path_preference
send_backlog
latency_sensitivity
```

The shared Gatherlink UDP stream adapter places bounded `traffic_class` and
`flowlet_key` hints on the helper open frame and emits them in stream-open
diagnostics. These are Python/helper facts for scheduler policy and operator
diagnostics. They are not Rust TCP semantics and they do not require inspecting
encrypted WireGuard or application payloads.

The adapter also emits bounded stream outcome facts from the companion exit:
`helper.stream.credit` reports advertised helper credit and current peak write
backlog, `helper.stream.closed` reports byte/frame counts and peak backlog, and
`helper.stream.reset` reports explicit reset reasons. These facts are meant for
Python diagnostics and future scheduler inputs. They are not reliability
signals for ordinary UDP payloads and they are not interpreted by Rust.

Default scheduling posture:

- keep one TCP stream on one path by default
- spread different TCP streams across available paths
- move a stream only after a safe idle/flowlet gap, explicit policy decision,
  or path failure
- prioritize open, close, reset, credit, and small interactive frames ahead of
  bulk data when the helper marks them that way
- expose per-stream path choice, backlog, stalls, resets, and close reasons in
  diagnostics

## Not-Yet Scope

- general proxy framework
- L7 routing
- TLS termination
- shared SOCKS5 multiplexing unless a later design folds them together
- TCP semantics or stream proxying inside Rust
- transparent mode implementation until the Debian TPROXY/firewall helper
  slice is promoted and tested
- dynamic L7 target selection from packet contents
- hidden TCP reliability for ordinary Gatherlink UDP services

## Relationship To SOCKS5 Helper

TCP forwarding and SOCKS5 are separate helpers in the first implementation.
They may share lower-level stream framing later, but TCP forwarding should stay
simple and explicit: one local port, one remote target, bidirectional responses.
