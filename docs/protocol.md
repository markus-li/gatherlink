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
| 34 | 8 | `sequence` | Per-session or per-service sequence number |
| 42 | 2 | `payload_len` | Payload bytes following the header |

The v1 base header is 44 bytes.

`header_len == 44` means the frame has no extension bytes. If `header_len` is
larger than 44, bytes `[44..header_len)` are the optional extension area and
the payload starts at `header_len`. V1 emits no extension bytes unless a feature
explicitly needs them, so ordinary data traffic pays no extension overhead.

## Path IDs

Path IDs are unsigned 16-bit integers. The full `0..65535` range is available
to the control plane. Human-readable path names stay in config and diagnostics;
wire frames carry only `path_id`.

Receiver-side per-path metrics should use the `path_id` from protocol metadata.
The Python control plane can map path ids to friendly names for monitor output.

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
