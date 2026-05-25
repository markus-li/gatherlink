# Relay Fabric

## Purpose

The relay fabric is the set of available relay/transit/exit nodes and their
capabilities.

It is active helper work, but the first scope is discovery and health only.
Encrypted relay-session forwarding is specified in `docs/protocol/relay-session-lifecycle.md`
and `docs/protocol/protocol.md`. The relay-fabric helper discovers and scores candidates;
it does not make plaintext routing decisions.

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

## First Scope

- relay discovery from configured or signed sources
- relay health checks
- capability, region, endpoint, and carrier metadata
- diagnostics for stale, degraded, incompatible, overloaded, or disabled relays
- candidate export to authenticated control/topology logic

Implemented first slice:

- `gatherlink helpers relay-discover relays.json --required-capability v1`
  loads local relay metadata and prints candidates plus health diagnostics
- metadata includes node id, region, endpoints, capabilities, trust domain,
  transit/exit flags, disabled state, and operator notes
- health distinguishes disabled, incompatible, stale topology,
  carrier-specific failure, overloaded, reachable, and authenticated states
- the helper exports usable candidates to later authenticated
  control/topology logic, but it does not request sessions or choose routes

## Library Posture

Use standard HTTP/JSON/TLS tooling first for configured metadata endpoints and
health probes. Do not add a discovery framework unless a concrete discovery
source requires a maintained, narrow client library.

## Deferred

- overlay path planning
- dynamic mesh behavior
- dynamic relay-session requests outside signed topology/control context
- mandatory cloud relay service

## Boundary

Relay fabric must follow `docs/protocol/relay-session-lifecycle.md`,
`docs/protocol/relay-trust-model.md`, and
`docs/architecture/architecture-contract.md`. This helper doc records relay
discovery and health scope only.
