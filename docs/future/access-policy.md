# Access Policy Helper

## Purpose

The access policy helper defines service-level constraints for Gatherlink helpers
and overlay paths.

It protects exit/transit behavior without becoming a firewall.

This helper is reusable. It can be consumed by WireGuard helpers, overlay routing,
IPsec NAT-T templates, SOCKS/TCP helpers, relay/exit helpers, and future topology
generation.

## Scope

Access policy applies to Gatherlink services and helper-generated configs.

It may restrict:

- destination IP/CIDR
- destination port
- protocol
- directionality
- allowed next hops
- allowed exits
- allowed transit
- maximum hop count
- service binding
- route class
- relay/exit eligibility

## Directionality

Supported direction concepts:

- bidirectional
- initiator_only
- responder_only
- one_way_out
- one_way_in

Most practical mode:

```text
initiator_only + established replies allowed
```

This protects a service boundary without turning Gatherlink into a general
firewall.

## Examples

Site-to-site API:

```json
{
  "service": "site-b-api",
  "direction": "initiator_only",
  "allow": [
    { "cidr": "10.20.5.10/32", "protocol": "tcp", "ports": [443] }
  ]
}
```

IPsec NAT-T:

```json
{
  "service": "ipsec-natt",
  "direction": "bidirectional",
  "allow": [
    { "cidr": "203.0.113.50/32", "protocol": "udp", "ports": [500, 4500] }
  ]
}
```

Transit restriction:

```json
{
  "service": "site-b",
  "transit": {
    "allowed_next_hops": ["relay-eu-2", "exit-se-1"],
    "max_hops": 3
  }
}
```

## Relationship to WireGuard helper

Access policy should exist as its own reusable helper.

The WireGuard helper may consume it to:

- validate intended prefixes
- generate route guidance
- warn about unsafe broad access
- document expected firewall rules

The core dataplane does not enforce arbitrary LAN firewall policy.

## Relationship to overlay routing

Overlay routing may consume access policy to know:

- which nodes can be used as transit
- which exits are allowed
- which prefixes are reachable
- which services may use which route classes
- which route plans are invalid before generation

## What this is not

This is not:

- DPI
- QoS
- LAN firewall
- NAT policy
- L7 policy
- enterprise ACL system

It is service-boundary intent and restriction.
