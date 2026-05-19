# Identity And Topology Lifecycle

## Purpose

Larger Gatherlink deployments require identity and topology lifecycle planning.

## Identity lifecycle

Gatherlink should start with static signed topology/provisioning bundles rather
than TOFU. WireGuard has no PKI in the protocol; peers are configured by static
public keys. Gatherlink should similarly keep trust explicit, but use a
per-deployment or per-fleet trust root so topology, roles, revocation, and
helper permissions can be signed.

Self-hosted small setups may use one local trust root.

Identity helper/control-plane should cover:

- node identity
- relay identity
- exit identity
- trust roots
- enrollment
- provisioning bundles
- rotation
- revocation
- expiration
- recovery/reset
- signed capability declarations

## Rotation And Revocation

Identity rotation should be auditable.

A normal rotation uses a new key with a signed transition from either the old
key or the trust root. If the old key is suspected compromised, the trust root
replaces it directly and revokes the old key.

Do not silently reuse the same `node_id` with unrelated key material. If a
stable logical node name is kept, the key transition must still be signed and
visible in diagnostics/audit state.

The initial revocation model should be topology-generation based. Handshake and
relay-session setup check the current signed topology/revocation generation.
Sessions should be short-lived enough for updates to take effect. Later versions
may add signed revocation lists.

## Topology lifecycle

Topology packages should support:

- topology version
- generation ID
- signer identity
- created/valid time
- nodes
- services
- relays
- exits
- allowed transit
- reachable prefixes
- service priorities
- access policies
- required capability versions

## Distribution model

Open-source baseline:

```text
generate explicit topology/config package
deploy it by external mechanism
```

Commercial/fleet mode may later add:

- signed config distribution
- staged rollout
- rollback
- audit log
- health validation
- fleet UI

## Bootstrap Tokens

Bootstrap tokens are enrollment aids, not data-plane credentials.

They should be:

- single-use
- expiring
- scoped to intended node, fleet, role, and capability set
- redeemed into signed topology/provisioning state
- audited

A bootstrap token alone must not authorize packet forwarding, relay transit, or
helper control.

## age usage

age may seal:

- provisioning bundles
- topology packages with secrets
- node identity exports
- bootstrap tokens

age is not packet transport crypto.
