# Identity And Topology Lifecycle

## Purpose

Larger Gatherlink deployments require identity and topology lifecycle planning.

## Identity lifecycle

Future identity helper/control-plane should cover:

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

## age usage

age may seal:

- provisioning bundles
- topology packages with secrets
- node identity exports
- bootstrap tokens

age is not packet transport crypto.
