# Protocol

Gatherlink v1 uses a compact fixed base header for UDP-carried data and control
frames. The base header is intentionally small; future features such as
fragmentation should use optional extension headers rather than charging every
packet for fields that are normally unused.

## V1 Base Header

All integer fields are encoded big-endian.

| Offset | Size | Field | Notes |
| ---: | ---: | --- | --- |
| 0 | 1 | `version` | Current value: `1` |
| 1 | 1 | `kind` | `1=data`, `2=control`, `3=batch` |
| 2 | 2 | `header_len` | `38` when no optional extension bytes are present |
| 4 | 2 | `flags` | No v1 flags are currently defined |
| 6 | 16 | `session_id` | Authenticated peer/session context |
| 22 | 2 | `service_id` | Virtual UDP or internal Gatherlink service identifier |
| 24 | 2 | `path_id` | Logical path/carrier id, full `u16` range |
| 26 | 2 | `route_id` | Compact route/transit id for future overlay plans |
| 28 | 8 | `sequence` | Global per-session/service packet sequence number |
| 36 | 2 | `payload_len` | Payload bytes following the header |

The v1 base header is 38 bytes.

`header_len == 38` means the frame has no extension bytes. If `header_len` is
larger than 38, bytes `[38..header_len)` are the optional extension area and
the payload starts at `header_len`. V1 emits no extension bytes unless a feature
explicitly needs them, so ordinary data traffic pays no extension overhead.

## Service IDs

Service IDs are unsigned 16-bit integers. The full protocol space is
`0..65535`, but the low range is reserved so Gatherlink internals never collide
with user/application UDP services.

| Range | Owner | Notes |
| ---: | --- | --- |
| `0` | invalid / unset | Used only before runtime config assigns an id |
| `1` | control metadata | Generic control metaband |
| `2` | time sync | Sink time and internal clock sync |
| `3` | internal DNS | Reserved for the DNS helper path |
| `4` | path discovery / keepalive | Reserved for peer/path liveness |
| `5` | diagnostics | Reserved for monitor detail requests and diagnostic streams |
| `6` | config apply | Reserved for safe reload/apply coordination |
| `7` | auth / crypto | Reserved for future handshake traffic |
| `8` | remote status | Reserved for on-demand IPC/status export |
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

The fixed data header carries one global `u64` sequence number. It is global
instead of per-path so the receiver can detect cross-path missing packets,
duplicates, and out-of-order arrivals without extra data-header overhead.

Receivers compare sequence numbers with wraparound-safe arithmetic. A later
sequence creates a missing range; an older sequence within the receive window is
out of order, late, or duplicate. The counter space is `2^64` packets per
session/service, so wrap is practically unreachable but still part of the
protocol contract.

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

Batch frames coalesce multiple virtual UDP payloads that share the same
`session_id`, `service_id`, `path_id`, and `route_id`. This keeps small-packet
traffic efficient without making single-packet frames larger.

The batch frame payload is:

| Offset | Size | Field | Notes |
| ---: | ---: | --- | --- |
| 0 | 2 | `item_count` | Number of virtual UDP payloads in this batch |
| 2 | variable | `items` | Repeated `u16 payload_len` followed by payload bytes |

The frame header `sequence` is the first item's sequence number. Each following
item is implicitly `sequence + item_index` in payload order. Batches must not
mix paths because the base header carries exactly one `path_id`.

Batch overhead is `38 + 2 + (2 * item_count)` bytes plus the summed payload
bytes. A single data frame remains `38 + payload_len` bytes, so batching should
be used when coalescing saves header overhead while still respecting the
configured MTU.

## Control Metaband

Control frames (`kind=2`) carry a versioned, extensible metaband payload. This
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

The generic control metaband currently uses reserved service id `1`. Python
normally compiles that service's fanout to `0`, meaning every eligible path, so
Rust emits the same payload over all paths without knowing what control metadata
means. Heavier diagnostic or IPC data uses its own reserved service id, enabled
or requested by control metadata, so Python can compile normal one-path fanout
for it. Reserved service id `8` is the remote-status lane for on-demand IPC
snapshots. Its payload is Python-owned, and Rust only frames/sends/receives it
according to Python-compiled service/path scheduler settings.

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
  single-frame payload is `frame_mtu - 38` before optional extensions such as
  fragmentation. Python should passively recheck carrier/interface TX MTU at
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

Fragmentation uses optional extension bytes on `data` frames. It is not present
on normal data or batch frames.

Gatherlink should fragment only when the selected packet cannot fit a useful
whole-packet path, or when the path that can fit it is marked busy and another
path has available capacity. The Python control plane owns the live capacity
model; Rust consumes the compiled path MTU and busy hints at packet time.

The v1 fragment extension is a compact TLV:

| Size | Field | Notes |
| ---: | --- | --- |
| 1 | `extension_type` | `1=fragment` |
| 1 | `extension_len` | Current value: `10` |
| 4 | `datagram_id` | Reassembly key for one virtual UDP datagram |
| 2 | `fragment_index` | Zero-based fragment index |
| 2 | `fragment_count` | Number of fragments for the datagram |
| 2 | `original_len` | Original virtual UDP payload length |

The fragment extension adds 12 bytes to the header, so a path with MTU `M` can
carry `M - 38 - 12` bytes of virtual payload per fragment. Receivers reassemble
fragments by `datagram_id` and `fragment_index`; packet-level logs and counters
should report both the final virtual datagram and the fragment/frame work that
was needed to carry it.

## Future Extensions

`header_len` allows a frame to add extension bytes after the base header.
Fragmentation, richer route metadata, capability negotiation, optional
diagnostic fields, or security metadata should live there instead of expanding
the base v1 header. Implementations should keep extensions sparse and feature
gated: if a receiver can infer the value from config or control-plane state, it
does not belong on every data packet.
