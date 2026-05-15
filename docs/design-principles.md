# Design Principles

## Purpose

This document captures durable project principles. It should be treated as a
guardrail for future implementation and pull requests.

## Primary abstraction

Gatherlink provides virtual UDP services over a carrier-aware multipath fabric:

```text
local UDP listen -> Gatherlink fabric -> remote UDP emit
```

Everything else is either orchestration, observability, or optional helper logic.

## Keep the core narrow

The core must remain focused on virtual UDP services, aggregation frames,
carrier selection, obfuscation/framing, receiver metrics, path health, MTU
eligibility, replay protection, bounded queues, and diagnostics events.

The core must not become a firewall, NAT product, QoS/shaping engine, DPI
product, L7 router, general proxy ecosystem, or full SD-WAN controller.

## Python owns intelligence

Python owns config loading and validation, default expansion, path lifecycle,
carrier discovery, peer failover, DNS helper, diagnostics, hooks, time quality,
overlay planning, and scheduler scoring.

## Rust owns execution

Rust executes already-compiled runtime state in the high-speed dataplane.
Rust should not decide what a user config means, why a path is good or bad,
which relay is preferred, how DNS should resolve, or how overlay routes are
planned.

## Helpers are optional

Helpers may improve usability and expose semantic metadata, but they must not
define the core architecture.

## Explicit over magical

The project may generate configs, but runtime behavior should be explainable.
Prefer generated explicit node configs over hidden magic behavior.

## Normal tools remain normal

Gatherlink should work beside OPNsense, OpenWrt, UniFi, MikroTik, Fortinet,
Linux routers, AdGuard Home, Unbound, and dnsmasq.

## Non-root by default

Normal Gatherlink operation should not require raw sockets, TUN/TAP,
iptables/nftables, CAP_NET_ADMIN, policy routing, or root.

## Boring failure beats clever failure

Under uncertainty, prefer disabling a bad path, reducing scheduler weight,
using conservative MTU, emitting diagnostics, and preserving continuity.

## Open-source integrity

The useful intelligence should stay open source. Commercial value should come
from appliance hardware, managed relays, monitoring, updates, support, and UI.
