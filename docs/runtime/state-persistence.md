# State Persistence

## Purpose

Gatherlink should retain enough local state to recover quickly without turning
runtime state into hidden configuration.

## Persisted State

Persist:

- node identities
- trust roots
- signed topology/provisioning bundles
- helper config
- last-known peer endpoints
- relay health hints
- path MTU and path health hints
- peer capability cache
- time-quality history
- recent failure counters
- generated explicit configs
- sealed identity/private key material

DNS cache should be memory-only first. Persist it later only if there is a
specific operator need and the validation/freshness behavior is clear.

Relay health persists as hints only, never as authority. Signed topology,
configured policy, and authenticated control context decide what can be used.

## Do Not Persist As Authority

Never persist these as resumable truth:

- session keys
- replay windows
- AEAD counters
- temporary scheduler weights
- short-lived queue state
- active packet windows
- temporary degraded state
- helper runtime internals
- plaintext bootstrap secrets after redemption

After restart, sessions should be re-established and replay/counter state should
start from fresh authenticated session material.

## Storage location

Suggested paths:

```text
/etc/gatherlink/        static config
/var/lib/gatherlink/    persistent state/cache
/run/gatherlink/        sockets and volatile runtime state
/var/log/gatherlink/    optional local logs/events
```

## Atomicity and corruption

Persistent writes should be atomic where possible. If cache/state is corrupt,
ignore it, emit diagnostics, and continue with config/defaults.

## Secrets

Secrets at rest may be age-sealed where appropriate. Runtime decrypted material
must not be logged.

Use JSON for non-secret state, sealed bundles for secrets, and canonical CBOR
for signed artifacts where deterministic signing matters.
