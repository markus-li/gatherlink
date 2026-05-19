# Library Selection

## Purpose

Gatherlink should prefer proven libraries for mature protocol parsing,
cryptography, DNS behavior, proxy behavior, file formats, platform integration,
and other areas where correctness matters more than owning code.

Dependencies must be chosen deliberately. A library choice becomes part of the
project's operational and security posture.

## Default Rule

Libraries may be added without a separate design discussion only when they are:

- well maintained, with recent releases or active repository maintenance
- commonly used enough that operational risk is low
- appropriately licensed for this project
- focused on the actual need instead of pulling in a large framework
- compatible with Gatherlink's Python/Rust/runtime support targets
- easy to isolate so failures do not affect unrelated core transport behavior
- replaceable if project direction changes

If a library is niche, stale, unusually broad, security-sensitive, hard to
replace, or likely to shape Gatherlink's public API, ask before selecting it.

If the task is trivial and the standard library is enough, prefer the standard
library and document that choice.

## Security-Sensitive Areas

Do not hand-roll mature security-sensitive protocols unless the project has
explicitly chosen to do so.

Prefer established libraries for:

- cryptography primitives and AEAD
- Noise/handshake implementation
- canonical encoding used for signatures
- DNS packet parsing and DNSSEC validation
- SOCKS/proxy protocol parsing
- TLS/HTTP client behavior

Security libraries must have a stronger maintenance bar than convenience
libraries. If there is doubt, stop and ask.

## Core Boundary

A dependency must not blur Gatherlink's architecture boundary.

- Rust owns deterministic packet execution and tiny hot-path primitives.
- Python owns policy, orchestration, helper behavior, diagnostics, and config.
- Helpers should not force core transport to depend on optional helper
  dependencies.
- Privileged behavior must stay narrow and explicit even when a library makes it
  easy to do more.
