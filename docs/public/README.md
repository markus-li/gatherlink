# Public Website Landing

This folder defines the static website content scope for Gatherlink. The site is
documentation-only: no hosted accounts, hosted control plane, remote management,
or telemetry collection.

## Landing Page Shape

The public landing page should say, in this order:

1. Gatherlink is a carrier-aware multipath UDP transport for Debian-first labs
   and small sites.
2. It carries local UDP services, including WireGuard helper setups, over one or
   more configured paths.
3. Python owns config, policy, helpers, diagnostics, and operator explanation.
   Rust owns packet execution, sockets, AEAD, replay, dedupe, queues, batching,
   fragmentation, counters, and cheap scheduling primitives.
4. It is not a hosted VPN service, firewall, DPI engine, full SD-WAN appliance,
   or hosted management product.
5. Current security and performance posture is evidence-driven and still
   unaudited by an external security review.

## Primary Links

- [GitHub releases](https://github.com/markus-li/gatherlink/releases)
- [Quickstart](../user/quickstart.md)
- [WireGuard multipath setup](../user/wireguard-multipath.md)
- [Security policy](../../SECURITY.md)
- [Benchmark methodology](../benchmarks/README.md)
- [Current benchmark evidence](../benchmarks/hyperv-performance-log.md)
- [Issue reporting](https://github.com/markus-li/gatherlink/issues/new/choose)
- [Troubleshooting](../user/troubleshooting.md)
- [Architecture contract](../architecture/architecture-contract.md)

## Public Caveats

- Debian is the supported and tested compatibility target.
- Normal Gatherlink services run unprivileged; lab setup and shaping may need
  elevated privileges.
- Static crypto is lab/manual fallback material. Authenticated provisioning is
  the normal secure path.
- The local REST/status API is protected by API keys, but it is still not a WAN
  management API.
- Performance claims must link to benchmark commands, date, topology, MTU,
  scheduler, path limits, and WireGuard or raw Gatherlink comparison baselines.

## Deployment

Use [Cloudflare Pages deployment notes](cloudflare-pages.md) for static hosting
only. Cloudflare hosting is independent from Gatherlink runtime behavior.
