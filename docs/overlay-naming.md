# Overlay Naming

## Purpose

Overlay naming gives users stable names for nodes, services, exits, sites, and
relays.

## Examples

- site-a
- site-b
- site-b.api
- exit-eu-west
- relay-sg-1
- office-primary
- vessel-starlink

## Requirements

Naming should support:

- human-readable diagnostics
- generated configs
- DNS helper integration
- overlay route planning
- identity binding
- collision detection
- stable aliases
- service discovery
- exit selection UX

## Dataplane boundary

Names are control-plane/helper concepts.

The dataplane should use compact IDs:

- node_id
- peer_id
- service_id
- path_id
- route_id

## MagicDNS-like idea

A future DNS helper may resolve overlay names to:

- local virtual service endpoints
- next-hop gateways
- exit helper endpoints
- diagnostic pages
- service aliases

This should remain optional and explicit.

## Avoid

Avoid making naming magic required for core transport operation.
