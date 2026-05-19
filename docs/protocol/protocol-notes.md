# Protocol Notes

## Purpose

This document records protocol-level decisions before the exact wire format is
finalized.

## Frame classes

Gatherlink should have at least data frames and control frames. Data frames
carry virtual UDP payloads. Control frames carry path probes, receiver metrics,
capability negotiation, time exchange, peer/session state, and diagnostics.

## Public UDP silence

Public UDP listeners must behave like this:

```text
invalid packet -> silent drop
```

No unauthenticated version replies, error frames, or debug hints should be
emitted.

This is a protocol requirement, not only an implementation preference. A remote
scanner that lacks valid Gatherlink key material should not be able to tell the
difference between Gatherlink, an unused UDP port, and a firewall drop based on
Gatherlink responses. Passive observers can still see traffic; stealth here
means the receiver is non-oracular and silent to unauthenticated input.

Silent-drop cases include:

- unknown packet type
- no session for receiver index
- authentication failure under the receiver index's session context
- malformed length
- unknown receiver index
- failed cookie/MAC check
- failed handshake authentication
- failed AEAD authentication
- replayed counter
- expired or revoked identity
- topology/capability mismatch

## Versioning

Every authenticated protocol context should include protocol version, feature
flags, session identity/context, service ID, path ID, and sequence number.

No unauthenticated version negotiation is allowed on public UDP sockets.
Data packets do not carry a public version. The receiver index selects candidate
session state, and that state determines the protocol version, crypto suite, and
key phase to try. If AEAD authentication succeeds, the packet matched the
session context. If authentication fails, the packet is silently dropped.

Unsupported versions or suites are handled during signed topology processing or
authenticated handshake negotiation. Public UDP receive never emits a visible
version or suite response.

## Compact encrypted data packet

The default data packet is intentionally close to WireGuard's transport data
message: a tiny clear header, a per-direction counter, and an AEAD ciphertext.

```text
0      u8   packet_type        0x01 = encrypted data
1      u32  receiver_index     opaque session receiver index
5      u64  counter            per-direction packet counter
13     ...  ciphertext         encrypted compact Gatherlink v2 frame bytes
end-16 16B  tag                ChaCha20-Poly1305 tag
```

The clear data header is 13 bytes. Including the AEAD tag, data-packet overhead
outside the encrypted compact Gatherlink v2 frame is 29 bytes. Crypto suite,
protocol version, and key phase are authenticated session state, not per-packet
fields.
There is no separate public compatibility probe: decrypt/authenticate success is
the compatibility check.

AEAD associated data is:

```text
"GATHERLINK_DATA_V1" || clear_data_header
```

The ciphertext plaintext is the compact Gatherlink v2 logical frame:

```text
V2LogicalHeader || optional_fragment_metadata || payload
```

This keeps `service_id`, `path_id`, frame kind, control metadata, and virtual
UDP payloads encrypted by default. Relays must not derive forwarding decisions
from service ids because service ids are endpoint metadata inside the
ciphertext. Relay forwarding is explicit control-plane state keyed by
`receiver_index` and local tunnel/session context. Endpoint exit/service routing
happens after decryption from `service_id` plus authenticated config/control
context.

If a future relay mode needs per-hop label swapping, it should add a separate
per-hop wrapper instead of exposing endpoint service/path metadata in the
end-to-end data envelope.

## Encrypted relay routing

Plaintext routing labels are not supported for secure transport. An untrusted
relay must not need plaintext `service_id`, `path_id`, endpoint address, tenant
name, policy name, or `route_id` to forward data. Routing uses outer
routing/relay-hop headers plus authenticated relay session state.

Relay forwarding uses hop-level authentication:

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

The relay decrypts only the hop payload, not the endpoint payload. It forwards
the inner packet only after hop AEAD authentication, relay replay checks, and
relay authorization checks pass.

Relay receive order:

```text
lookup relay_receiver_index
AEAD authenticate/decrypt hop payload
check relay replay window
check authorization, direction, expiry, and rate limits
forward inner packet to configured next hop
```

All invalid relay inputs silently drop. Unknown receiver indexes, bad tags,
replayed counters, unauthorized directions, expired sessions, and malformed
inner envelopes must not be forwarded.

Endpoint routing happens only after endpoint decrypt:

```text
endpoint receiver_index -> endpoint session
AEAD decrypt endpoint packet
parse compact v2 logical frame
service_id + authenticated config/control context -> service/exit decision
```

Normal overhead:

```text
direct secure path: 42 bytes
one relay hop:      71 bytes
two relay hops:     100 bytes
```

## Compact v1/v2 logical frame

Plaintext lab mode uses compact v1, which includes a visible `version` byte.
Secure transport decrypts to compact v2, which is the same logical header
without the visible version byte because the authenticated session already
selects the protocol version.

V1 plaintext/lab header:

```text
0      u8   version          current value 1
1      u8   kind_flags       bits 0..1 kind, bit 2 fragment-present
2      u16  service_id       virtual UDP or internal Gatherlink service
4      u16  path_id          logical path/carrier id
6      u64  sequence         logical per-session/service packet sequence
14     ...  payload          when fragment-present is unset
```

V2 secure decrypted header:

```text
0      u8   kind_flags       bits 0..1 kind, bit 2 fragment-present
1      u16  service_id       virtual UDP or internal Gatherlink service
3      u16  path_id          logical path/carrier id
5      u64  sequence         logical per-session/service packet sequence
13     ...  payload          when fragment-present is unset
```

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

When `fragment-present` is set, fixed metadata follows the compact header. V2
fragmented layout:

```text
13     u32  datagram_id
17     u16  fragment_index
19     u16  fragment_count
21     u16  original_len
23     ...  fragment_payload
```

Compact v1/v2 deliberately omit fields made redundant by secure session state
or local plaintext context:

- `version`: present in v1 plaintext; omitted from v2 because session context
  plus successful authentication selects it
- `header_len`: fixed compact base header plus fragment-present bit
- `flags`: compact `kind_flags` only
- `session_id`: `receiver_index` maps to authenticated session state
- `route_id`: removed completely; do not synthesize it for compatibility views
- `payload_len`: derived from authenticated plaintext length

Compatibility adapters may synthesize the older draft `FrameHeader` after
decryption by mapping `receiver_index` to `session_id` when needed and deriving
`payload_len` from the decrypted plaintext length. New code should use compact
v1 in plaintext mode and compact v2 after secure decryption.

Handshake packet types are reserved as:

```text
0x10 handshake initiation
0x11 handshake response
0x12 cookie reply / retry token
```

Only authenticated or anti-DoS-validated inputs may trigger a response. Ordinary
invalid input never receives a handshake error.

## Capability negotiation

Peers should eventually advertise protocol version, supported frame flags,
supported carriers, max payload MTU, receiver metrics version, time exchange
support, future fragmentation support, and future overlay helper support.

Capability information must not be exposed to unauthenticated scanners.
Capability negotiation occurs only inside authenticated encrypted control
messages or inside the authenticated handshake transcript.

## Sequence spaces

Sequence-space design must support replay protection, dedupe, reorder windows,
receiver metrics, path migration, and peer failover.

Transport packet counters and Gatherlink frame sequence numbers are distinct:

- transport `counter`: per-direction AEAD nonce/replay counter for one traffic
  key
- compact frame `sequence`: per-session/service ordering, dedupe, loss, and scheduler
  signal inside the encrypted frame

The transport counter is checked before the inner frame is trusted. The frame
sequence is processed only after AEAD authentication succeeds.

Normal multipath uses one peer crypto session across many paths. Intentional
fanout duplicates are encrypted separately:

```text
path A: transport counter = 100, inner frame sequence = 55
path B: transport counter = 101, inner frame sequence = 55
path C: transport counter = 102, inner frame sequence = 55
```

Replay protection rejects repeated encrypted transport packets with the same
counter/tag. It must not reject separately encrypted fanout copies only because
the decrypted inner frame has the same sequence. Those copies pass transport
replay checks and are handled by inner-frame dedupe.

Receive order:

```text
AEAD authenticate/decrypt
transport replay-window check
parse compact Gatherlink frame
dedupe by inner session/service/sequence policy
record expected duplicate or deliver first copy
```

Use separate crypto sessions only for separate trust domains, such as
relay-mediated sessions, exits/site gateways, bootstrap paths, or tenant
isolation.

## Fragmentation

The protocol may reserve fields/flags for future internal fragmentation, but MVP
does not need fragmentation. Initially skip paths that cannot carry a packet,
drop only when no eligible path exists, and emit MTU diagnostics.

## Receiver metrics

Receiver metrics should be compact and periodic, not per-packet ACKs. Metrics
may include last sequence received, received count, duplicate count,
out-of-order count, missing ranges/loss estimate, jitter, receive rate, and
auth/decode failures.

## Time exchange

Time exchange should use monotonic values for RTT and relative timing and
wall-clock values for offset estimation and event correlation.

## Anti-amplification

Never reply to unauthenticated UDP, keep unauthenticated processing cheap,
rate-limit expensive validation failures, and avoid larger responses than
requests unless the peer/session is authenticated.

Implementation order for public receive should be:

```text
read packet
check minimum fixed length
check packet_type
check receiver_index/cookie/MAC if applicable
run required authentication/decryption
check replay window
only then allocate session/control work or emit responses
```

All failure branches before authentication must converge on silent drop.

## Obfuscation boundary

Obfuscation/framing sits below the aggregation protocol and above the carrier.
The aggregation protocol should not care whether the frame was transported over
raw UDP, stealth UDP, direct QUIC DATAGRAM, HTTP/3 DATAGRAM, WSS/TLS, or
TCP/TLS fallback.

All carriers transport the same Gatherlink UDP-format carrier packet. Direct
QUIC DATAGRAM, HTTP/3 DATAGRAM, future TCP/TLS, future WSS, and obfuscation
profiles are outer wrappers only: they must not change Gatherlink headers,
encryption, replay protection, routing context, aggregation behavior, or service
semantics.

At the receiving sink, the carrier unwraps its outer transport and immediately
hands the recovered Gatherlink packet to normal receive handling. A packet that
arrives through direct QUIC DATAGRAM or HTTP/3 DATAGRAM must become
indistinguishable from the same packet arriving through raw UDP after unwrap.

## age boundary

age may be used for sealed config bundles, provisioning packages, at-rest
private keys, bootstrap tokens, and exports/backups. age must not be used for
per-packet transport security.
