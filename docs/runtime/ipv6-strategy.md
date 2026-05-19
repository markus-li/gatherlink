# IPv6 Strategy

## Purpose

Gatherlink should not accidentally become IPv4-only.

## Requirements

Support IPv4 and IPv6 in socket addresses, peer endpoints, DNS helper,
bootstrap resolution, path validation, diagnostics, overlay reachability
metadata, and generated configs.

## Dual stack

Dual-stack peers should test IPv4 raw UDP, IPv6 raw UDP, IPv4 WSS/QUIC, and IPv6
WSS/QUIC. Bootstrap should validate connect/auth, not just DNS answer presence.

## Preference

Default preference should be policy-driven and measured, not hardcoded.

## Diagnostics

Diagnostics should show resolved address, selected IP family, carrier/profile
used, and success/failure reason.
