# Helper Docs

Start with [`helper-priorities.md`](helper-priorities.md). It is the source of
truth for which helpers are active, deferred, or future-only.

## Active Helpers

| File | Purpose |
| --- | --- |
| [`time-sync.md`](time-sync.md) | explicit time helper behavior and warnings |
| [`dns-helper.md`](dns-helper.md) | DNS helper, direct/tunnel upstreams, cache, IDNA, and DNSSEC posture |
| [`socks5-helper.md`](socks5-helper.md) | SOCKS5 TCP CONNECT helper over Gatherlink transport |
| [`tcp-forwarding-helper.md`](tcp-forwarding-helper.md) | one-to-one TCP forwarding helper over Gatherlink transport |
| [`wireguard-helper.md`](wireguard-helper.md) | WireGuard-over-Gatherlink planning and UDP endpoint contract |
| [`traffic-split-helper.md`](traffic-split-helper.md) | advanced local policy-routing split for dual WireGuard profiles |
| [`relay-fabric.md`](relay-fabric.md) | relay discovery and health helper |

## Deferred Helper Notes

| File | Purpose |
| --- | --- |
| [`captive-portal-helper.md`](captive-portal-helper.md) | future captive portal login helper design |
| [`ipsec-helper.md`](ipsec-helper.md) | future IPsec NAT-T helper shape |
| [`policy-advisor.md`](policy-advisor.md) | future local policy-advisor concept |

## Rules

- Helpers use Gatherlink transport; direct behavior is only for explicit lab
  smoke modes.
- Helper docs should not duplicate protocol, runtime, or platform ownership.
- Follow [`docs/operations/documentation-maintenance.md`](../operations/documentation-maintenance.md) for duplication,
  canonical-linking, and volatile-fact rules.
