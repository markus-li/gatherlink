# Service Priority Full Design Notes

## Purpose

Gatherlink may prioritize Gatherlink services/links relative to each other.

This is not traffic QoS.

## Correct scope

Gatherlink decides:

```text
which Gatherlink service gets better Gatherlink paths/relays/failover
```

The firewall/router decides:

```text
which LAN traffic enters which Gatherlink service
```

## Examples

- site-to-site: priority 100
- business API link: priority 80
- internet exit: priority 40
- bulk backup: priority 10

## Effects

Higher-priority services may receive:

- healthier paths
- more stable relay chains
- faster failover
- more conservative failback
- better duplication policy
- lower tolerance for degraded carriers
- first access to scarce relay capacity
- better default route class

## Non-goals

Gatherlink must not implement:

- LAN packet classification
- DSCP enforcement
- shaping
- fair queuing between users
- firewall QoS policy
- DPI-based priority

Those belong to routers/firewalls.

## Diagnostic expectation

When a service gets preferred treatment, diagnostics should show:

- service priority
- selected route class
- selected paths/relays
- rejected lower-quality options
