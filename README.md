# Gatherlink

Carrier-aware multipath UDP transport.

Gatherlink presents virtual UDP services over multiple logical paths and carriers.

Core primitive:

```text
local UDP listen -> multipath carrier fabric -> remote UDP emit
```

Gatherlink is intentionally not a firewall, VPN replacement, classic SD-WAN appliance,
DPI engine, QoS system, or general proxy framework.

Early runnable milestones are documented in:

- `docs/local-dual-path-lab.md`
- `docs/plaintext-security-mode.md`
- `docs/diagnostics-events.md`
