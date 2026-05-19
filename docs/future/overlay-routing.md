# Overlay Routing

## Purpose

Overlay routing is future helper/control-plane functionality.

Gatherlink core remains:

```text
point-to-point virtual UDP services over carrier-aware paths
```

The overlay helper may plan how multiple point-to-point Gatherlink links are
combined into larger topologies.

## Core rule

The core dataplane does not dynamically invent routes.

Preferred model:

```text
overlay planner -> generated explicit service configs -> dataplane executes
```

This keeps the system explainable, testable, and debuggable.

## Node roles

A node may act as different roles per service:

- entry
- transit
- relay
- exit
- site-gateway
- diagnostic-only

A node does not have one fixed global role.

## Transit behavior

A transit node receives a Gatherlink frame and forwards it to another Gatherlink
connection instead of emitting the original UDP payload locally.

Example:

```text
client -> relay-a -> relay-b -> exit-node -> target
```

The transit node must not need to understand LAN policy or endpoint payload. For
secure transport it forwards only after relay-hop authentication, replay checks,
generation checks, and authorization against configured next-hop/session state.
See `docs/protocol/relay-session-lifecycle.md`.

## Reserved future concepts

Protocol/control-plane should leave space for:

- next_hop
- final_service_id
- hop_limit
- route_generation
- transit_allowed
- selected_exit
- selected_site_gateway
- service_priority
- allowed_next_hops
- route_class

`route_id` is removed. Secure transit/routing is represented by outer
routing/relay-hop headers and authenticated relay session state, not by
endpoint packet fields or compatibility route labels.

## Site-to-site

For site-to-site, Gatherlink may expose a virtual next-hop/gateway for a
firewall/router.

Example:

```text
Firewall route:
  10.20.0.0/16 via Gatherlink site-b gateway
```

Gatherlink carries the service. The firewall owns LAN routing policy, NAT, ACLs,
and segmentation.

## Exit-node routing

Exit nodes may be selected for:

- regional egress
- availability
- relay-chain health
- policy
- service priority
- carrier conditions
- cost/capacity hints

Exit selection is helper/control-plane behavior, not dataplane policy magic.

## Self-healing

Self-healing should mean:

```text
re-plan explicit route -> distribute/generate updated config -> execute
```

not:

```text
nodes invent hidden topology at runtime
```

## Hop redirects

Hop redirects can exist later, but should be constrained:

- authenticated
- topology-generation-aware
- loop-protected
- diagnostics-visible
- policy-checked
- bounded by hop limit

## Explicit non-goals

Overlay routing must not become:

- BGP/OSPF replacement
- LAN routing daemon
- firewall controller
- NAT controller
- L7 router
- hidden dynamic mesh
- opaque SD-WAN controller

## Diagnostics

Overlay planning must explain:

- selected path
- rejected alternatives
- health inputs
- priority inputs
- generation/version
- transit decisions
- exit decisions
- route invalidation reason
