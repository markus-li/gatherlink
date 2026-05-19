# HTTP/3 DATAGRAM Carrier Lab

## Purpose

This lab proves the HTTP/3 DATAGRAM carrier variant for v0.9.1.

HTTP/3 DATAGRAM is an additive carrier alongside direct QUIC DATAGRAM. It still
transports the exact same Gatherlink UDP-format carrier packet:

```text
client -> HTTP/3 DATAGRAM carrier -> Gatherlink sink -> normal Gatherlink receive path
```

Direct Gatherlink exposure must remain testable, and sample configs should keep
a no-proxy HTTP/3 DATAGRAM case. For public internet-facing deployments, prefer
placing the carrier behind Cloudflare Spectrum-style TCP/UDP protection and/or
Traefik UDP forwarding when those fit the chosen carrier path.

After HTTP/3 DATAGRAM unwrap, the recovered packet must be indistinguishable
from the same packet arriving through raw UDP or direct QUIC DATAGRAM.

## Required Behavior

The test must prove:

1. The same Gatherlink service works over HTTP/3 DATAGRAM.
2. The sink unwraps HTTP/3 DATAGRAM and enters the normal Gatherlink receive
   path.
3. HTTP/3 request/session machinery stays carrier-only.
4. No Gatherlink header, encryption, replay, routing, aggregation, or service
   behavior changes when HTTP/3 DATAGRAM is used.
5. Invalid HTTP/3 DATAGRAM setup, unsupported datagram negotiation, invalid
   auth, or malformed Gatherlink packets fail closed.
6. Diagnostics identify HTTP/3 DATAGRAM as the carrier.

## Not Plain HTTP/3 Proxying

Ordinary HTTP/3 request reverse proxying is not enough for this carrier.

The implementation must explicitly support HTTP/3 datagrams. HTTP request
metadata, paths, methods, streams, and sessions are outer carrier machinery
only. They must not become Gatherlink routing, identity, control, helper, or
packet-format state.

## Acceptance Steps

1. Start the Gatherlink sink with HTTP/3 DATAGRAM carrier enabled.
2. Start a peer configured for the HTTP/3 DATAGRAM carrier endpoint.
3. Verify that datagram support is negotiated.
4. Send the same payload used by UDP and direct QUIC carrier tests.
5. Verify counters, diagnostics, and payload delivery.
6. Compare the recovered receive-path behavior with UDP and direct QUIC runs.
7. Disable or break HTTP/3 datagram negotiation.
8. Verify fail-closed behavior and useful diagnostics.
9. Send malformed or unauthenticated traffic over the carrier.
10. Verify silent drop or fail-closed diagnostics without creating invalid
    Gatherlink state.

## Notes

- This lab does not introduce a new Gatherlink packet model.
- This lab does not authorize plaintext routing.
- CONNECT-UDP/MASQUE can be considered for v0.9.2 or later, but it is not the
  v0.9.1 HTTP/3 DATAGRAM carrier unless explicitly promoted.

## Current Code State

The v0.9.1 code includes a Python-owned HTTP/3 DATAGRAM carrier adapter. The
adapter binds local UDP sockets for the Rust dataplane and carries the resulting
opaque Gatherlink packet bytes through HTTP/3 DATAGRAM frames. The focused smoke
command is:

```bash
gatherlink lab carrier-smoke http3-datagram
```

When testing a UDP-capable layer-4 proxy such as Traefik, use:

```bash
gatherlink lab carrier-proxy-smoke http3-datagram --proxy traefik
```

This is still UDP forwarding at the proxy layer. The HTTP/3 DATAGRAM session is
terminated only by the Gatherlink carrier adapter, and the proxy does not gain
Gatherlink routing, service, identity, helper, or packet-format meaning.

The Rust-backed runner still rejects direct non-UDP carrier DTOs fail-closed.
That is intentional: Rust owns compact packet execution over local sockets, and
the Python adapter owns the standard HTTP/3 wrapper around those sockets.
