# Helper Docs

Start with `helper-priorities.md`. It is the source of truth for which helpers
are active, deferred, or future-only.

## Active Helpers

| File | Purpose |
| --- | --- |
| `time-sync.md` | explicit time helper behavior and warnings |
| `dns-helper.md` | DNS helper, direct/tunnel upstreams, cache, IDNA, and DNSSEC posture |
| `socks5-helper.md` | SOCKS5 TCP CONNECT helper over Gatherlink transport |
| `tcp-forwarding-helper.md` | one-to-one TCP forwarding helper over Gatherlink transport |
| `wireguard-helper.md` | WireGuard-over-Gatherlink planning and UDP endpoint contract |
| `relay-fabric.md` | relay discovery and health helper |

## Deferred Helper Notes

| File | Purpose |
| --- | --- |
| `captive-portal-helper.md` | future captive portal login helper design |
| `ipsec-helper.md` | future IPsec NAT-T helper shape |
| `policy-advisor.md` | future local policy-advisor concept |

## Rules

- Helpers use Gatherlink transport; direct behavior is only for explicit lab
  smoke modes.
- Helper docs should not duplicate protocol, runtime, or platform ownership.
- Do not add `-full.md` companion docs. Keep the canonical helper doc complete
  enough, then split by clearer subject only if the doc becomes unwieldy.
