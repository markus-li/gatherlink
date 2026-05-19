# Relay Trust Model

## Purpose

Gatherlink needs a clear model for what relays can and cannot see/do.

## Relay visibility

Depending on mode, relays may see source endpoint metadata, destination peer
metadata, timing, packet sizes, carrier/profile used, and service IDs if not
hidden inside the envelope.

Relays should not see plaintext virtual UDP payloads.

## End-to-end payloads

Payloads such as WireGuard are already encrypted by their own protocol.
Gatherlink transport envelope should still authenticate and protect Gatherlink
frames.

## Relay authority

Relays may be authorized to forward traffic, report metrics, participate in peer
failover, and act as exit/site-gateway for configured services. Relays must not
silently rewrite service intent.

Secure relay forwarding is hop-authenticated. Gatherlink does not support
plaintext routing labels for secure transport: relays must not forward by
reading plaintext service ids, path ids, route labels, endpoint addresses,
tenant names, or policy labels from data packets. `route_id` is removed.
Routing through untrusted peers uses outer routing/relay-hop headers and
authenticated relay session state.

An untrusted relay forwards only after authenticating a per-hop encrypted relay
packet, checking replay protection, and enforcing explicit control-plane relay
state. The inner endpoint packet remains end-to-end encrypted and is opaque to
the relay. The final decrypting endpoint alone maps `service_id` plus
authenticated config/control context to a local service or exit decision.

## Hosted relays

Hosted relays require tenant isolation, key lifecycle, audit events, rate limits,
abuse controls, and monitoring.

## Self-hosted relays

Self-hosted relays should be first-class and not artificially limited.
