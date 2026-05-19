# Protocol

Gatherlink v1 should use a compact versioned logical frame shape for plaintext
lab mode and other contexts that do not have authenticated session state yet.
The frame carries a visible protocol version plus endpoint semantics that cannot
be derived from local config context: frame kind, service id, path id, logical
sequence, optional fragment metadata, and payload.

In secure transport mode, the encrypted plaintext uses the compact v2 logical
frame, which is the same as v1 except it omits the visible `version` byte.
Version, suite, and key phase are selected by authenticated session context and
successful AEAD authentication. Unauthenticated observers and untrusted relays
see only `packet_type`, `receiver_index`, packet counter, ciphertext, and tag.
`service_id`, `path_id`, frame kind, control metadata, and virtual UDP payloads
are encrypted by default.

## V1 Versioned Logical Header

All integer fields are encoded big-endian.

| Offset | Size | Field | Notes |
| ---: | ---: | --- | --- |
| 0 | 1 | `version` | Current value: `1` |
| 1 | 1 | `kind_flags` | Bits `0..1` kind, bit `2` fragment-present, bits `3..7` reserved zero |
| 2 | 2 | `service_id` | Virtual UDP or internal Gatherlink service identifier |
| 4 | 2 | `path_id` | Logical path/carrier id, full `u16` range |
| 6 | 8 | `sequence` | Global per-session/service packet sequence number |
| 14 | variable | `payload` | Payload bytes, or fragment metadata followed by fragment payload |

The normal v1 logical header is 14 bytes.

`kind_flags`:

```text
bits 0..1: kind
  0 = data
  1 = control
  2 = batch
  3 = reserved
bit 2: fragment metadata present
bits 3..7: reserved, must be zero
```

When `fragment-present` is set, fixed metadata follows the 14-byte header:

```text
14     u32  datagram_id
18     u16  fragment_index
20     u16  fragment_count
22     u16  original_len
24     ...  fragment_payload
```

Fragmented v1 packets pay 24 bytes before fragment payload. There is no general
`header_len`; the fragment-present bit is the only hot-path variable header
indicator. Payload length is derived from the received plaintext datagram length
in plaintext lab mode, or from the authenticated plaintext length in secure
mode.

## V2 Decrypted Logical Header

V2 is the secure-mode decrypted logical header. It is v1 without the visible
`version` byte, because the authenticated session already selects the protocol
version.

```text
0      u8   kind_flags       bits 0..1 kind, bit 2 fragment-present
1      u16  service_id       virtual UDP or internal Gatherlink service
3      u16  path_id          logical path/carrier id
5      u64  sequence         logical per-session/service packet sequence
13     ...  payload          when fragment-present is unset
```

V2 normal overhead is 13 bytes. Fragmented v2 packets use the same fixed
fragment metadata immediately after the 13-byte header and pay 23 bytes before
fragment payload.

## Secure Transport Envelope

Secure transport has two layers:

- a clear crypto envelope for session lookup, AEAD, and replay protection
- the encrypted compact v2 logical frame for endpoint service/path/dedupe
  handling

Clear encrypted envelope:

```text
0      u8   packet_type        0x01 = encrypted data
1      u32  receiver_index     opaque session receiver index
5      u64  counter            per-direction packet counter
13     ...  ciphertext         encrypted v2 logical frame bytes
end-16 16B  tag                ChaCha20-Poly1305 tag
```

The clear envelope is 13 bytes, or 29 bytes including the AEAD tag. Untrusted
relays forward using explicit control-plane state keyed by `receiver_index` and
local tunnel/session context. They must not need plaintext `service_id`,
`path_id`, endpoint addresses, tenant names, policy names, or route labels to
forward end-to-end encrypted data.

Sink UDP carrier sockets must support multiple authenticated source peers on
the same bind address/port. The first secure demux key is the clear opaque
`receiver_index`; the authority for accepting the packet is successful AEAD
authentication against the compiled session selected by that index. The remote
UDP source tuple is a carrier observation and possible return-path hint, not a
trusted peer identity and not a reason to allocate a separate sink port.

The v1 wire format carries `version`, but not separate header length, flags,
session id, routing label, or payload length fields. V2 omits `version` as
well.

## Encrypted Relay Routing

Gatherlink does not support plaintext routing labels for secure transport.
Relays must not forward by reading plaintext `service_id`, `path_id`,
endpoint addresses, tenant names, policy names, or route labels from data packets.
Those values are endpoint/control-plane semantics and stay encrypted until the
endpoint decrypts them. Relay routing uses the outer routing/relay-hop header
and authenticated relay session state.

Relay forwarding uses hop-level authenticated encryption around the end-to-end
packet:

```text
HopPacket {
  packet_type
  relay_receiver_index
  relay_counter
  ciphertext(InnerPacket)
  relay_tag
}

InnerPacket {
  packet_type
  endpoint_receiver_index
  endpoint_counter
  ciphertext(V2LogicalHeader || payload)
  endpoint_tag
}
```

The relay can authenticate and replay-check the hop packet, then forward the
inner packet according to the outer routing header and explicit authenticated
relay session state. The relay cannot decrypt
the inner endpoint packet and cannot see `service_id`, `path_id`, frame kind,
sequence, control metadata, or payload.

Relay receive order:

```text
read hop packet
lookup relay_receiver_index
AEAD authenticate/decrypt hop payload
check relay replay window
check relay session authorization, expiry, direction, rate limits
forward inner packet to the configured next hop
```

All failures are silent drops or local rate-limited diagnostics:

- unknown relay receiver index
- malformed hop packet
- failed hop AEAD authentication
- replayed relay counter
- expired relay session
- unauthorized direction or next hop
- rate limit exceeded
- malformed inner packet envelope

This prevents blind forwarding: a relay only forwards packets that authenticate
under a relay-hop session it was explicitly configured to accept. Endpoint
service routing happens only at the final decrypting peer:

```text
endpoint receiver_index -> endpoint session
AEAD decrypt endpoint packet
parse v2 logical frame
service_id + authenticated config/control context -> local service/exit
```

Each relay hop adds one 29-byte hop envelope. Normal overhead is therefore:

```text
direct secure path: 29-byte endpoint envelope + 13-byte v2 header = 42 bytes
one relay hop:      29-byte hop envelope + 42-byte endpoint packet = 71 bytes
two relay hops:     29 + 29 + 42 = 100 bytes
```

Batching remains the preferred way to amortize this overhead for small payloads.
The extra hop envelope is intentional: it buys authenticated relay admission,
relay replay protection, rate limiting, and no plaintext routing metadata.

## Service IDs

Service IDs are unsigned 16-bit integers. The full protocol space is
`0..65535`, but the low range is reserved so Gatherlink internals never collide
with user/application UDP services.

The reserved table names protocol lanes, not automatic implementation status.
Current code uses reserved service id `1` for the generic control metaband
and id `8` for production-owned remote status. Other named lanes are reserved
for future dedicated protocols unless a specific implementation doc says
otherwise. For example, DNS tunnel traffic uses an explicit configured user
service today, sink time and path labels ride inside control metadata, monitor
cadence is local IPC, config changes use restart or scheduler reapply, and
Noise/session provisioning is out-of-band CLI/file exchange.

| Range | Owner | Notes |
| ---: | --- | --- |
| `0` | invalid / unset | Used only before runtime config assigns an id |
| `1` | control metadata | Active generic control metaband |
| `2` | time sync | Reserved for a future dedicated time-sync lane; current sink time uses control metadata |
| `3` | internal DNS | Reserved for a future internal DNS lane; current DNS tunnel helper uses configured user services |
| `4` | path discovery / keepalive | Reserved for future dedicated peer/path liveness; current path labels use control metadata |
| `5` | diagnostics | Reserved for future monitor detail requests and diagnostic streams |
| `6` | config apply | Reserved for future safe reload/apply coordination |
| `7` | auth / crypto | Reserved for future in-band handshake traffic |
| `8` | remote status | V1-required lane for explicit temporary read-only IPC/status export |
| `9..255` | Gatherlink reserved | Future internal services |
| `256..65535` | user/application services | Normal configured UDP services |

Python config validation rejects explicit user service ids below `256`. If a
service does not specify an id, runtime expansion assigns deterministic ids from
`256` upward in config order while skipping any explicit ids already claimed by
the same config. Rust validates the same boundary again when it receives
compiled runtime DTOs.

Automatic service IDs are the recommended production mode. Gatherlink is a UDP
service transport, not a general VPN; one configured inbound UDP service maps to
one remote service target, and the automatically assigned service ID is only the
compact protocol label for that mapping. Explicit `service_id` values are
supported for deliberate protocol-level coordination, debugging, or tightly
managed interop, but they are not recommended for normal configs. Startup plans
and foreground service startup warn whenever a user service pins an explicit ID.

## Global Sequence Numbers

The fixed data header carries one `u64` sequence number. Within one authenticated
peer/session/service transmission scope it is global instead of per-path, so the
receiver can detect cross-path missing packets, duplicates, and out-of-order
arrivals without extra data-header overhead. A shared sink keeps independent
sequence spaces for each authenticated peer it sends to; packets intentionally
sent to another peer must not appear as local gaps.

Receivers compare sequence numbers with wraparound-safe arithmetic. A later
sequence creates a missing range; an older sequence within the receive window is
out of order, late, or duplicate. The counter space is `2^64` packets per
session/service, so wrap is practically unreachable but still part of the
protocol contract for each authenticated peer/session/service scope.

The receiver cannot attribute a missing sequence to a path from the missing data
packet itself, because the packet never arrived. Path attribution comes from the
control metaband described below.

## Path IDs

Path IDs are unsigned 16-bit integers. The full `0..65535` range is available
to the control plane. Human-readable path names stay in config and diagnostics;
wire frames carry only `path_id`.

Receiver-side per-path metrics should use the `path_id` from protocol metadata.
The Python control plane can map path ids to friendly names for monitor output.
Rust-owned telemetry should expose counters with the same service status shape
used by the Python lab harness: total `packets`, `bytes`, `missed_packets`,
`expected_duplicate_packets`, `unexpected_duplicate_packets`,
`reordered_packets`, `packets_needing_reorder`, and `path_stats` keyed by
compact path id or Python-resolved path name. Expected duplicates are intentional
fanout copies suppressed before application UDP emit; unexpected duplicates are
replays or duplicate carrier delivery outside the compiled fanout contract.
Python owns presentation, labels, priority decisions, and scheduler policy; Rust
only reports observed protocol facts.
Production telemetry keeps that boundary explicit. Rust reports directional
`tx_*` and `rx_*` counters, decoded control metadata, path ids, sequence facts,
and observed loss/reorder facts. Python converts peer-view control metadata
into local-view state, smooths capacity estimates, caches them, chooses path
weights, and sends compiled scheduler/runtime decisions back to Rust.
Reorder hold policy follows the same split: Python computes the per-node-pair
hold window from measured latency skew and jitter, clamps it to the configured
maximum, and sends Rust only the compiled hold time it should execute in the
fast path. Python's default minimum hold starts at `2ms`, while the default
maximum hold is `150ms` unless a node pair config overrides it. Rust should not
enforce either value as a constant; it should execute the hold time Python
compiled for the service/node pair.

## Batch Frames

Batch frames coalesce multiple virtual UDP payloads that share the same compact
logical context: `service_id` and `path_id` on the wire, plus authenticated
receiver/session/control context known locally after receive. This keeps
small-packet traffic efficient without making single-packet frames larger.

The batch frame payload is:

| Offset | Size | Field | Notes |
| ---: | ---: | --- | --- |
| 0 | 2 | `item_count` | Number of virtual UDP payloads in this batch |
| 2 | variable | `items` | Repeated `u16 payload_len` followed by payload bytes |

The frame header `sequence` is the first item's sequence number. Each following
item is implicitly `sequence + item_index` in payload order. Batches must not
mix paths because the base header carries exactly one `path_id`.

Batch overhead is the compact logical header plus `2 + (2 * item_count)` bytes
and the summed payload bytes. In plaintext compact v1 that is
`14 + 2 + (2 * item_count)` bytes before payloads. In secure compact v2 that is
`13 + 2 + (2 * item_count)` bytes inside the AEAD plaintext. A single data frame
is `14 + payload_len` in compact v1 or `13 + payload_len` in compact v2, so
batching should be used when coalescing saves header overhead while still
respecting the configured MTU.

## Control Metaband

Control frames (`kind=1`) carry a versioned, extensible metaband payload. This
lane is for sparse peer telemetry and safe control data that should not increase
every data packet. The initial payload shape is:

| Size | Field | Notes |
| ---: | --- | --- |
| 1 | `control_version` | Current value: `1` |
| 2 | `message_count` | Number of metaband messages |
| variable | `messages` | Repeated `u8 type`, `u16 len`, `len` bytes |

Initial message types:

| Type | Payload | Purpose |
| ---: | --- | --- |
| 1 | `u64 first_sequence`, `u32 packet_count`, `u16 path_id` | Sender path assignment report |
| 2 | `u64 first_sequence`, `u32 packet_count` | Receiver missing global sequence range |
| 3 | `u16 path_id`, `u8 name_len`, UTF-8 name | Path id to friendly name metadata |
| 4 | `u16 path_id`, `u64 tx_bps`, `u64 rx_bps` | Directional path capacity estimate; zero means unknown |
| 5 | `u16 path_id`, `u32 tx_current_us`, `u32 tx_mean_us`, `u32 rx_current_us`, `u32 rx_mean_us` | Directional path latency estimate; zero means unknown |
| 6 | `u64 exchange_id`, `u16 path_id`, `u8 mode`, `u64 origin_us`, `u64 receive_us`, `u64 transmit_us` | Internal clock sync exchange; `mode=1 request`, `mode=2 response`, zero receive/transmit means absent |
| 8 | `u16 service_id`, `u8 name_len`, UTF-8 name | Service id to friendly service name metadata; never carries target IP/port |
| 9 | `u16 service_id`, `u8 target_len`, UTF-8 target | Endpoint assertion for verification only; never applied as config |
| 10 | `u16 service_id`, `u8 reason_len`, UTF-8 reason | Generic peer service-disable assertion; stops traffic for that service with loud diagnostics |
| 11 | `u16 path_id`, `u16 tx_link_mtu`, `u16 tx_frame_mtu`, `u16 rx_link_mtu`, `u16 rx_frame_mtu` | Directional passive path MTU observation; zero means unknown |
| 12 | `u16 service_id`, `u16 fanout`, `u32 fanout_below_bytes` | Python-owned service scheduler policy FYI; peer Python may compile this into local Rust receive expectations |

Rust handles reserved service ids mechanically. Any frame whose `service_id` is
in `0..255` is never emitted to an application UDP target; Rust records cheap
frame/path counters and queues the payload bytes for Python. Python owns every
reserved-service decoder and policy decision. If Python has no decoder for a
reserved service id, it logs a loud error and drops that payload. If a
non-reserved service id reaches the Python reserved dispatcher, that is treated
as a boundary bug or corrupted event; Python logs a loud error and drops it
instead of trying to reinterpret user traffic. This keeps future internal
services, such as remote status, internal DNS, config apply, and auth handshakes,
addable in Python without teaching Rust new semantics.

The normal Rust-backed service runner, not only lab services, must drain this
reserved-service queue and invoke the Python dispatcher. Decoded peer policy is
then compiled back into narrow Rust executor primitives such as service disable
or scheduler fanout. Rust still does not learn the meaning of the reserved
payload.

The generic control metaband currently uses reserved service id `1`. Python
normally compiles that service's fanout to `0`, meaning every eligible path, so
Rust emits the same payload over all paths without knowing what control metadata
means. Heavier diagnostic or IPC data uses its own reserved service id, enabled
or requested by control metadata, so Python can compile normal one-path fanout
for it. Reserved service id `8` is the remote-status lane for on-demand IPC
snapshots. Its payload is Python-owned, and Rust only frames/sends/receives it
according to Python-compiled service/path scheduler settings. When a shared
sink receives an id `8` request from an authenticated peer, Rust attaches the
peer/session scope to the reserved-service event so Python can reply to that
exact peer without parsing carrier endpoints or teaching Rust the remote-status
protocol.
Reserved-service frames still advance Rust's cheap global sequence telemetry so
normal control/remote-status traffic does not appear as missing user-service
packets in operator counters.
Forward sequence gaps are exposed as reorder pressure, not confirmed loss,
until a timeout/expiry mechanism proves the skipped packets will not arrive.

Discovery/control metadata and remote IPC/status must stay separate:

- discovery is continuous, sparse, authenticated metadata on the control
  metaband
- discovery sends at a low baseline cadence and promptly on important changes
- discovery advertises stable facts such as service id/name mappings, path
  names, capacity, MTU, disabled-service assertions, and endpoint assertions
  for verification
- remote status uses reserved service id `8` only when explicitly requested by
  a local operator/tool
- remote status is read-only, temporary, and auto-expires when the requester
  stops refreshing it
- remote status may carry live counters and status snapshots; discovery should
  not stream those by default

This is enough for real telemetry:

- sender reports which global sequence ranges were sent on each path
- receiver reports which global sequence ranges are missing
- peers and Python monitors can correlate missing ranges back to path ids and
  friendly names
- receivers should use `PathMetadata` control messages for monitor row names
  when available, with local config or `path-id:<id>` labels only as fallback
- peers can advertise `PathCapacity` control messages with directional
  `tx_bps` and `rx_bps` estimates. A missing direction is encoded as zero, so
  early deployments can report only the side they actually measured.
- peers can advertise `PathLatency` control messages with directional current
  and rolling-mean latency in microseconds. Like capacity, these are peer-view
  facts on the wire; Python converts them into local `tx`/`rx` meaning before
  display or scheduler use.
- peers can advertise `PathMtu` control messages with directional MTU facts:
  `tx_link_mtu`, `tx_frame_mtu`, `rx_link_mtu`, and `rx_frame_mtu`. A missing
  direction is encoded as zero, just like capacity/latency. Python records peer
  TX as local RX, because MTU is direction-specific per path. The maximum
  normal single-frame payload is `frame_mtu - 14` for plaintext compact v1 and
  `frame_mtu - 13` for secure compact v2 plaintext before the outer AEAD
  envelope is applied. Fragmented packets use the fixed fragment metadata
  described below. Python should passively recheck carrier/interface TX MTU at
  startup and sparse intervals, and should actively re-probe only when a fault
  suggests the cached ceiling is stale.
- peers can exchange `InternalClockSync` messages that mirror NTP's four
  timestamp model without changing system time. A requester sends `origin_us`
  from its process monotonic clock. The authoritative side stamps
  `receive_us` and `transmit_us` in its own internal monotonic clock and echoes
  the origin timestamp. The requester computes peer-relative offset and RTT in
  Python. This internal time model is intended for sliding windows, replay
  protection, telemetry windows, and later crypto policy.
- service status should expose control metadata telemetry, including sent and
  received frame/message/byte counts, last send/receive time, last source, and
  the current path-id/name, service-id/name, path capacity, path latency, and internal clock sync state

Service metadata is intentionally only a friendly mapping from compact
`service_id` to service name. It does not set target IP addresses, target ports,
listen addresses, return endpoints, helper endpoint config, or any other routing
policy. Those remain explicit local config.

Service scheduler policy metadata is also Python-owned. When Python compiles a
local service scheduler, it should advertise the compact policy to the peer as
an FYI. The peer Python process records what the other side expects and may
compile the relevant receive expectation into local Rust. This is how expected
fanout duplicates stay distinct from unexpected duplicate/replay traffic without
making Rust infer policy from packet contents.

Application-facing UDP services use one local listen port per service. The
listen port identifies which configured service received the payload; all
services can then share the same per-path carrier sockets for outbound
Gatherlink frames. In lab wording, the traffic-emitting side is the **source**
and the receiving test endpoint is the **sink**. Existing config roles may still
say client/server where that is the clearer node-role description.

Control metadata may carry endpoint assertions such as "peer thinks service 256
targets 127.0.0.1:51820", but only as verification facts. A receiver compares
those assertions against its explicit local config; if they mismatch, it stops
traffic for that service and emits loud operator errors. It must not use ordinary
control metadata to set or rewrite endpoint config. Safe remote config changes
require a separate authenticated, encrypted, signed config-apply protocol.

Service-disable assertions are the generic stop mechanism for peer policy. A
sink can say "I no longer want service 256", but the same message can also come
from a source, helper, or future signed policy module. Receivers must stop
accepting/emitting traffic for that service and make the reason visible in
service status and logs. The message still does not mutate local config; a later
config reload or authenticated policy update decides whether the service should
exist again.

Future message types can carry safe config-change proposals, capability
negotiation, path health samples, receiver watermarks, or helper state. Unknown
critical semantics should be negotiated before use; ordinary data frames must
remain valid without metaband messages.

Control send cadence is Python policy, not lab behavior and not Rust business
logic. The baseline cadence should send only what is required to keep services
working and scheduling correct: path identity, capacity, latency, missing-range
facts, receiver health, and clock/time metadata. Startup and active traffic can
use a faster refresh so peers converge quickly; established idle sessions should
back off to sparse refreshes, currently around 60 seconds. Operator diagnostics
may explicitly request a higher-rate monitor cadence from each running service.
That request is temporary; if a service has not received a refreshed monitor
request for 120 seconds, it returns to baseline cadence. The monitor refreshes
that request before the timeout, currently every 60 seconds, rather than sending
an IPC request on every screen refresh. This keeps normal control traffic
minimal while letting `gatherlink services monitor` ask for enough control
metadata to make live views useful.

MTU probing should be deliberately conservative and directional. The passive
interface/link MTU check is cheap enough at startup and every minute or so, but
it only proves local TX for that path. Peer control metadata supplies the local
RX view. Active path MTU probes should be triggered by faults such as repeated
`FrameExceedsPathMtu` errors, fragmentation suddenly becoming necessary for
packets that used to fit, missing packet bursts immediately after larger
frames, transport socket `EMSGSIZE` / message-too-long errors, a path/carrier
interface change, route change, peer reconnect, or explicit operator request.
Loss alone is not enough; combine it with frame-size correlation so congestion
is not misdiagnosed as an MTU drop.

## Fragmentation

Fragmentation uses the `fragment-present` bit on `data` frames. It is not
present on normal data or batch frames and does not use a generic extension
header.

Gatherlink should fragment only when the selected packet cannot fit a useful
whole-packet path, or when the path that can fit it is marked busy and another
path has available capacity. The Python control plane owns the live capacity
model; Rust consumes the compiled path MTU and busy hints at packet time.

When `fragment-present` is set, fixed metadata follows the compact logical
header:

| Size | Field | Notes |
| ---: | --- | --- |
| 4 | `datagram_id` | Reassembly key for one virtual UDP datagram |
| 2 | `fragment_index` | Zero-based fragment index |
| 2 | `fragment_count` | Number of fragments for the datagram |
| 2 | `original_len` | Original virtual UDP payload length |

The fixed fragment metadata is 10 bytes. A compact v1 fragmented frame has
`14 + 10 = 24` bytes before fragment payload; a compact v2 fragmented frame has
`13 + 10 = 23` bytes before fragment payload. A path with frame MTU `M` can
therefore carry `M - 24` bytes of compact v1 fragment payload or `M - 23` bytes
of compact v2 fragment payload before considering the outer secure envelope.
Receivers reassemble fragments by `datagram_id` and `fragment_index`;
packet-level logs and counters should report both the final virtual datagram and
the fragment/frame work that was needed to carry it.

## Future Extensions

Compact v1/v2 deliberately avoid a general `header_len` field. Future hot-path
wire changes should either fit the existing compact fields, use a new
authenticated frame version, or move sparse information into reserved services
such as control metadata, diagnostics, capability negotiation, or config apply.
If a receiver can infer a value from config or authenticated control-plane
state, it does not belong on every data packet.
