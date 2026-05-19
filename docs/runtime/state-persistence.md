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

Debian v0.9 paths:

```text
/etc/gatherlink/        static config
/var/lib/gatherlink/    persistent state/cache
/run/gatherlink/        sockets and volatile runtime state
/var/log/gatherlink/    optional local logs/events
```

The Python control plane exposes these through the Debian compatibility backend,
`GatherlinkStatePaths`, and `PersistentStateStore`. Code should use those
helpers instead of scattering literal system paths through runtime, helper, or
CLI modules.

Current first-slice subpaths:

```text
/var/lib/gatherlink/identities/*.identity.json    private node identities, 0600
/var/lib/gatherlink/identities/*.public.json      public identity exports
/var/lib/gatherlink/bootstrap/endpoints.json      non-authoritative endpoint cache
/var/lib/gatherlink/bundles/*.signed.json         signed control-plane bundles
/var/lib/gatherlink/trust-roots/*.public.json     trusted public roots
/var/lib/gatherlink/hints/*.json                  non-authoritative runtime hints
/var/lib/gatherlink/secrets/*.sealed.json         passphrase-sealed local secrets, 0600
/var/log/gatherlink/*.jsonl                       durable diagnostics sinks
```

## Atomicity and corruption

Persistent writes should be atomic where possible. If cache/state is corrupt,
ignore it, emit diagnostics, and continue with config/defaults.

## Secrets

Secrets at rest may be passphrase-sealed where appropriate. Runtime decrypted
material must not be logged.

Use JSON for non-secret state, sealed JSON envelopes for local secrets, and
canonical CBOR for signed artifacts where deterministic signing matters.

Private JSON records that contain key material, such as
`*.identity.json` and pending handshake state, must be owner-only on Debian
(`0600`). The control plane fails closed when a private record is group- or
world-readable. Public identity exports and trust roots are intentionally
shareable and may be `0644`.

The v0.9 sealed-secret UX is intentionally noninteractive and scriptable:

```bash
export GATHERLINK_SECRET_PASSPHRASE='replace-with-local-secret'
gatherlink secrets secret-seal node.identity.json node.identity.sealed.json --label node-identity
gatherlink secrets secret-inspect node.identity.sealed.json
gatherlink secrets secret-open node.identity.sealed.json node.identity.opened.json --label node-identity
```

`secret-seal` reads only owner-only input JSON. `secret-open` writes owner-only
output JSON and prints only a redacted summary. `secret-inspect` never asks for
a passphrase and prints only envelope metadata. Sealed secret files remain local
operator artifacts; they are not signed topology, not session state, and not
runtime policy.

`PersistentStateStore` intentionally treats endpoint caches and runtime hints as
non-authoritative. They may speed startup or inform scheduling, but signed
topology, explicit config, and authenticated control context must still decide
what is allowed.

## State audit

Operators can inspect local state without printing secret material:

```bash
gatherlink secrets state-audit --state-dir .gatherlink/state
gatherlink secrets state-audit --state-dir .gatherlink/state --json
```

The audit checks:

- private identities are owner-only and internally consistent
- trust roots are valid public identity records
- signed bundles verify
- sealed-secret envelopes are owner-only and readable without opening them
- non-authoritative hints and endpoint caches are parseable JSON

Corrupt non-authoritative hints are warnings by default because runtime should
be able to ignore them and continue from explicit config. Use `--strict-hints`
when packaging or release checks should treat those warnings as errors. The
audit output is redacted and reports sealed artifacts only as envelope metadata,
never plaintext.
