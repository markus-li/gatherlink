# State Persistence

## Purpose

Gatherlink should retain enough local state to recover quickly without turning
runtime state into hidden configuration.

## Persisted state

Potential persisted state includes last-known-good peer endpoint, carrier/profile,
path MTU, bootstrap cache, peer capability cache, time-quality history, path
health history, recent failure counters, sealed identity/private key material,
and generated explicit configs.

## Not authoritative truth

Do not persist transient scheduler weights, short-lived queue state, active
packet windows, temporary degraded state, or helper runtime internals as truth.

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
