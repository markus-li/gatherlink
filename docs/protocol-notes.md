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

## Versioning

Every authenticated protocol context should include protocol version, feature
flags, session ID, service ID, path ID, and sequence number.

## Capability negotiation

Peers should eventually advertise protocol version, supported frame flags,
supported carriers, max payload MTU, receiver metrics version, time exchange
support, future fragmentation support, and future overlay helper support.

Capability information must not be exposed to unauthenticated scanners.

## Sequence spaces

Sequence-space design must support replay protection, dedupe, reorder windows,
receiver metrics, path migration, and peer failover.

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

## Obfuscation boundary

Obfuscation/framing sits below the aggregation protocol and above the carrier.
The aggregation protocol should not care whether the frame was transported over
raw UDP, stealth UDP, QUIC DATAGRAM, WSS/TLS, or TCP/TLS fallback.

## age boundary

age may be used for sealed config bundles, provisioning packages, at-rest
private keys, bootstrap tokens, and exports/backups. age must not be used for
per-packet transport security.
