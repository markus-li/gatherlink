# Relay Fabric

## Purpose

The relay fabric is the set of available relay/transit/exit nodes and their
capabilities.

It is future control-plane/helper functionality.

## Relay functions

A relay may provide:

- fallback when direct paths fail
- transit to another relay
- regional exit
- site gateway reachability
- carrier diversity
- Cloudflare/WSS endpoint for restricted environments
- direct UDP/QUIC endpoint for normal environments
- bootstrap metadata endpoint later
- time-quality peer later

## Relationship to peer failover

Peer failover handles switching between peers/relays.

Relay fabric provides the candidate set and health/capability metadata.

## Relationship to overlay routing

Overlay routing chooses relay paths.

Relay fabric describes available relays and their state.

## Relay metadata

Possible relay metadata:

- node identity
- region
- public endpoints per carrier
- WSS/QUIC/UDP support
- current health
- allowed transit
- allowed exit role
- capacity hints
- service priority support
- last-known-good paths
- supported protocol version
- supported capability set
- trust domain
- operator notes

## Relay health

Relay health should distinguish:

- reachable
- authenticated
- degraded
- carrier-specific failure
- overloaded
- disabled
- incompatible
- stale topology

## Non-goals

Relay fabric must not become:

- hidden dynamic mesh magic
- firewall policy engine
- payload inspection layer
- mandatory cloud service for self-hosted deployments
- opaque vendor-only infrastructure
