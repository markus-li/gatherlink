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
| 2 | 2 | `header_len` | `44` when no optional extension bytes are present |
| 4 | 2 | `flags` | No v1 flags are currently defined |
| 6 | 16 | `session_id` | Authenticated peer/session context |
| 22 | 8 | `service_id` | Virtual UDP service identifier |
| 30 | 2 | `path_id` | Logical path/carrier id, full `u16` range |
| 32 | 2 | `route_id` | Compact route/transit id for future overlay plans |
| 34 | 8 | `sequence` | Global per-session/service packet sequence number |
| 42 | 2 | `payload_len` | Payload bytes following the header |

The v1 base header is 44 bytes.

`header_len == 44` means the frame has no extension bytes. If `header_len` is
larger than 44, bytes `[44..header_len)` are the optional extension area and
the payload starts at `header_len`. V1 emits no extension bytes unless a feature
explicitly needs them, so ordinary data traffic pays no extension overhead.

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
`reordered_packets`, `packets_needing_reorder`, and `path_stats` keyed by
compact path id or Python-resolved path name. Python owns presentation, labels,
priority decisions, and scheduler policy; Rust only reports observed protocol
facts.
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

Batch overhead is `44 + 2 + (2 * item_count)` bytes plus the summed payload
bytes. A single data frame remains `44 + payload_len` bytes, so batching should
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
- peers can exchange `InternalClockSync` messages that mirror NTP's four
  timestamp model without changing system time. A requester sends `origin_us`
  from its process monotonic clock. The authoritative side stamps
  `receive_us` and `transmit_us` in its own internal monotonic clock and echoes
  the origin timestamp. The requester computes peer-relative offset and RTT in
  Python. This internal time model is intended for sliding windows, replay
  protection, telemetry windows, and later crypto policy.
- service status should expose control metadata telemetry, including sent and
  received frame/message/byte counts, last send/receive time, last source, and
  the current path-id/name, path capacity, path latency, and internal clock sync state

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
carry `M - 44 - 12` bytes of virtual payload per fragment. Receivers reassemble
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
