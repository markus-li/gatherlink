# Future Design Notes

This directory holds future design notes that are not active implementation
authorization. Promote a topic into a release roadmap before building it.

## Current Files

| File | Purpose |
| --- | --- |
| `identity-and-topology.md` | future identity, trust-root, topology lifecycle, bootstrap, and fleet-distribution ideas |
| `overlay-routing.md` | future overlay route planning, transit behavior, exit routing, and route safety |
| `overlay-naming.md` | future overlay naming and MagicDNS-like ideas |
| `loop-prevention.md` | future multihop loop-prevention rules |
| `access-policy.md` | reusable future access-policy model for helpers, exits, relays, and topology generation |

## Rules

- Keep these as design notes until a release roadmap promotes them.
- Do not create runtime placeholder packages from future notes.
- Do not duplicate a concise doc with a `-full.md` copy. If a topic needs more
  detail, add it to the canonical file or split by a clearer subject name.
- Cross-link to canonical docs when a design note depends on current protocol,
  runtime, helper, or operations behavior.
