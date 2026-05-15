# Capability Negotiation

## Purpose

Gatherlink needs a clear way for peers to know what protocol features are
supported without exposing public unauthenticated fingerprints.

## Requirements

Capability negotiation should cover protocol version, frame format version,
control frame version, supported obfuscation profiles, supported carriers, max
payload MTU, receiver metrics version, replay window behavior, time exchange
support, future fragmentation support, and future overlay helper support.

## Authentication

Capability information must not be exposed to unauthenticated scanners. Public
UDP behavior remains invalid packet -> silent drop.

## Local diagnostics

When authenticated peers disagree, local diagnostics should show local version,
remote version, unsupported features, selected downgrade mode if any, and reason
connection failed if no compatible mode exists.

## Rolling upgrades

Design for old client to new relay, new client to old relay, mixed helper
support, and mixed metrics versions.

## Feature flags

Feature flags should be explicit and grouped by transport, security, metrics,
time, carrier, and overlay capability.
