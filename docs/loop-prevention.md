# Loop Prevention

## Purpose

Multihop overlays require loop prevention.

Without it, self-healing route changes can create traffic loops.

## Required concepts

Future overlay/transit design should include:

- route_id
- route_generation
- hop_limit
- visited-node tracking if needed
- stale topology rejection
- duplicate route suppression
- clear diagnostics
- route invalidation events

## Hop limit

Every transit-capable frame should have a hop limit.

If hop limit reaches zero:

```text
drop -> increment counter -> emit diagnostic
```

## Route generations

Route plans should carry a generation/version.

Nodes should reject stale or incompatible route generations according to policy.

## Stale topology

If a node receives a route generation it does not understand, it should fail
closed for transit and emit diagnostics locally.

## Explicit planning

Loop prevention is much easier if topology is generated explicitly.

Gatherlink should avoid emergent dynamic routing in the core.
