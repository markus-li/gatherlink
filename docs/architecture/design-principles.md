# Design Principles

## Purpose

This document captures durable project principles. It should be treated as a
guardrail for future implementation and pull requests. The canonical ownership
and boundary contract is `docs/architecture/architecture-contract.md`; this file
is the short human-readable version.

## Primary abstraction

Gatherlink provides virtual UDP services over a carrier-aware multipath fabric:

```text
local UDP listen -> Gatherlink fabric -> remote UDP emit
```

Everything else is either orchestration, observability, or optional helper logic.

## Keep The Core Narrow

Keep Gatherlink focused on virtual UDP services over a carrier-aware fabric.
Permanent exclusions and helper carve-outs live in
`docs/architecture/architecture-contract.md`.

## Keep Ownership Obvious

Python owns meaning; Rust executes compact facts. Detailed module boundaries
belong in `docs/architecture/source-map.md` and
`docs/architecture/architecture-contract.md`.

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

Normal Gatherlink operation should stay unprivileged. The exact host capability
boundary lives in `docs/architecture/architecture-contract.md`.

## Boring failure beats clever failure

Under uncertainty, prefer disabling a bad path, reducing scheduler weight,
using conservative MTU, emitting diagnostics, and preserving continuity.

## Open-source integrity

The useful intelligence should stay open source. Commercial value should come
from appliance hardware, managed relays, monitoring, updates, support, and UI.
